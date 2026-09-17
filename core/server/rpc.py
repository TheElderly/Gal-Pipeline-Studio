"""无缓冲 StdIO JSON-RPC 2.0 服务端 —— WinUI 3 壳层 ⇄ Python 核心的唯一 IPC 面。

通信基准（行式协议）::

    壳层逐行写入 JSON-RPC 2.0 请求 ──▶ serve_stdio() 逐行读取
        ──▶ RpcDispatcher.handle_request(raw_json) 纯内存分发
        ──▶ 逐行回写响应并立即 flush（无缓冲契约）

帧格式与错误码严格对齐 JSON-RPC 2.0 规范：成功 ``{"jsonrpc","id",
"result"}``；失败 ``{"jsonrpc","id","error":{code,message[,data]}}``；
规范错误码 -32700/-32600/-32601/-32602/-32603，业务错误约定 -32000
（EngineAdapterError 及其派生）。

协议裁量（刻意决策，非遗漏）：

* 行式协议：一行一个请求对象，不支持批量数组（json.dumps 序列化
  永不内嵌裸换行，行分帧天然安全）；
* 通知（无 id 的请求）按规范执行但永不回应，方法级错误同样静默；
* 参数仅支持具名对象形态（POSIX 数组形态返回 -32602）——IPC 方法
  全部为关键字参数，语义自描述。

防御契约：handle_request 全链路异常拦截，任何未捕获异常都归一为
标准错误帧（-32603 兜底），服务进程绝不闪退；serve_stdio 以 EOF
自然退出，可被壳层安全终止。
"""

import inspect
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Final

from pydantic import ValidationError

from ..adapters.base import BaseEngineAdapter, EngineAdapterError, UnsupportedFormatError
from ..adapters.kag import KagAdapter
from ..adapters.relay import CSVTabularAdapter, JsonAdapter, MarkedTextAdapter, TabularAdapter
from ..archive.orchestration import list_archives as list_archives_entries
from ..archive.orchestration import unpack_archive as unpack_archive_entry
from ..archive.workspace import init_workspace as init_workspace_dirs
from ..lqa.rules import run_static_rules
from ..media.pipeline import convert_image as convert_image_entry
from ..models.ir import GalIRProject, TranslationUnit
from ..models.status import TranslationStatus
from ..pipeline.translator import GalgameTranslator, TranslationAPIError, TranslationConfig
from ..profiler.engine_profiler import profile_engine
from ..tm.glossary import match_glossary as match_glossary_terms

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
BUSINESS_ERROR = -32000
"""业务错误约定码：EngineAdapterError / TranslationAPIError 家族统一映射于此。"""

_MODELS_TIMEOUT_SECONDS = 15.0
"""fetch_models 单次 GET 的网络超时。"""

_ADAPTER_FACTORIES: Final[dict[str, Callable[[], BaseEngineAdapter]]] = {
    "kag": KagAdapter,
    # 通用中继适配器（社区工具中间文本 → Gal-IR）：格式与引擎能力正交，
    # 引擎语义（dump/回编译工具、补丁形态）由 core/adapters/relay/recipes.py 声明。
    "relay.tsv": TabularAdapter,
    "relay.csv": CSVTabularAdapter,
    "relay.marked": MarkedTextAdapter,
    "relay.json": JsonAdapter,
}
"""引擎类型 → 适配器构造器的注册表：新增引擎只改这张表，不动调度逻辑。

值为「构造器」而非「实例」，以保留 ``build_default_dispatcher`` 每次
调用产出独立实例的既有契约（适配器当前无状态，但契约不该依赖这一点）。
键必须与 ``GalIRProject.engine_type`` 的字面量一致 —— ``ir_to_asset``
正是按该键 O(1) 查表选适配器。
"""


def _build_adapters() -> dict[str, BaseEngineAdapter]:
    """按注册表实例化全部适配器（每次调用独立实例，供单次分发器独占）。"""
    return {name: factory() for name, factory in _ADAPTER_FACTORIES.items()}


class _RpcFailure(Exception):
    """分发器内部控制流：携带 JSON-RPC 错误码的协议级失败。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def fetch_models(api_base: str, api_key: str | None = None) -> dict:
    """向 OpenAI 兼容端点发起 ``GET {api_base}/models`` 并归一化模型清单。

    异常分类（全部收拢为 TranslationAPIError → JSON-RPC -32000，消息
    区分失败类别）：

    * 网络错误 / 超时（URLError、TimeoutError）；
    * 鉴权失败（HTTP 401）与其他 HTTP 错误；
    * 非 JSON / 非标准 OpenAI 响应结构。
    """
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/models",
        headers={"Accept": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_MODELS_TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise TranslationAPIError("鉴权失败（HTTP 401）：请检查 API Key") from exc
        raise TranslationAPIError(f"模型列表请求失败（HTTP {exc.code}）") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise TranslationAPIError(f"模型列表请求失败（网络错误/超时）：{reason}") from exc
    try:
        models = [item["id"] for item in json.loads(raw)["data"]]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise TranslationAPIError("响应不是标准 OpenAI 模型列表 JSON") from exc
    return {"models": models}


class RpcDispatcher:
    """纯内存 JSON-RPC 2.0 行式分发器：注册方法表 + 全链路异常拦截。"""

    def __init__(self) -> None:
        self._methods: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, handler: Callable[..., Any]) -> None:
        self._methods[name] = handler

    def handle_request(self, raw_json: str) -> str:
        """处理一帧请求并返回一帧响应；通知返回空串（规范禁止回应）。"""
        try:
            req = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            return self._error(None, PARSE_ERROR, f"JSON 解析失败：{exc}")
        if not isinstance(req, dict):
            return self._error(None, INVALID_REQUEST, "请求必须是 JSON 对象（行式协议不支持批量数组）")
        req_id = req.get("id")
        if "id" in req and not isinstance(req_id, (str, int, float)) and req_id is not None:
            return self._error(None, INVALID_REQUEST, "id 必须为 string、number 或 null")
        if req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return self._error(req_id, INVALID_REQUEST, "缺少或非法的 jsonrpc/method 字段")

        is_notification = "id" not in req
        try:
            result = self._invoke(req["method"], req.get("params"))
        except Exception as exc:
            if is_notification:
                return ""
            code, message = self._map_exception(exc)
            return self._error(req_id, code, message)
        if is_notification:
            return ""
        return json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}, ensure_ascii=False)

    def _invoke(self, method: str, params: Any) -> Any:
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise _RpcFailure(INVALID_PARAMS, "params 仅支持具名参数对象")
        handler = self._methods.get(method)
        if handler is None:
            raise _RpcFailure(METHOD_NOT_FOUND, f"未知方法：{method}")
        try:
            bound = inspect.signature(handler).bind(**params)
        except TypeError as exc:
            raise _RpcFailure(INVALID_PARAMS, f"参数绑定失败：{exc}") from exc
        return handler(*bound.args, **bound.kwargs)

    @staticmethod
    def _map_exception(exc: Exception) -> tuple[int, str]:
        """异常 → (错误码, 消息) 的唯一映射点（防御顺序敏感）。"""
        if isinstance(exc, _RpcFailure):
            return exc.code, str(exc)
        if isinstance(exc, TranslationAPIError):
            return BUSINESS_ERROR, str(exc)
        if isinstance(exc, EngineAdapterError):
            return BUSINESS_ERROR, str(exc)
        if isinstance(exc, ValidationError):
            return INVALID_PARAMS, f"参数校验失败：{exc}"
        if isinstance(exc, FileNotFoundError):
            return INVALID_PARAMS, f"文件不存在：{exc.filename}"
        return INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> str:
        return json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        )


def build_default_dispatcher() -> RpcDispatcher:
    """组装注册全部核心业务方法的默认分发器（每次调用独立实例）。"""
    adapters = _build_adapters()
    dispatcher = RpcDispatcher()
    register = dispatcher.register

    register("ping", lambda: "pong")
    register("fetch_models", fetch_models)

    def detect_format(file_path: str) -> dict:
        for name, adapter in adapters.items():
            if adapter.detect(Path(file_path)):
                return {"detected": True, "adapter": name}
        return {"detected": False, "adapter": ""}

    def extract_to_ir(file_path: str) -> dict:
        for name, adapter in adapters.items():
            if adapter.detect(Path(file_path)):
                project = adapter.extract_to_ir(Path(file_path))
                dump = project.model_dump(mode="json")
                by_status: dict[str, int] = {}
                for unit in dump["units"]:
                    by_status[unit["status"]] = by_status.get(unit["status"], 0) + 1
                return {
                    # 完整模型 dump：可直接回灌 ir_to_asset，保证跨进程往返自洽
                    "project": dump,
                    "project_id": project.project_name,
                    "unit_count": len(dump["units"]),
                    "stats": {"total": len(dump["units"]), "by_status": by_status},
                }
        raise UnsupportedFormatError(f"无已注册适配器认领该文件：{file_path}")

    def translate_batch(
        units: list[dict], config: dict, context: list[dict] | None = None
    ) -> dict:
        """批量翻译调度。

        ``context``（可选）：滑窗上下文 ``[{"speaker": ..., "text": ...}]``
        —— 调用方（壳层）从**本调用之前已定稿**的对白里截取的参考行；
        Sidecar 按批次注入 User Prompt 的隔离块，仅供模型理解指代。
        缺省时回退为批内先行定稿条目（直调管线场景）。

        响应形如 ``{"units": [...], "usage": {...}}``：units 为状态刷新后
        的单元列表，usage 为本批次真实 Token 消耗（跨子批原子累加，
        缺 usage 字段的响应按零贡献）。
        """
        validated = [TranslationUnit.model_validate(unit) for unit in units]
        translator = GalgameTranslator(TranslationConfig(**config))
        updated = translator.translate_batch(validated, context=context)
        usage = getattr(translator, "last_usage", None) or {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        return {
            "units": [u.model_dump(mode="json") for u in updated],
            "usage": usage,
        }

    def ir_to_asset(project: dict, output_dir: str) -> dict:
        proj = GalIRProject.model_validate(project)
        adapter = adapters.get(proj.engine_type)
        if adapter is None:
            raise UnsupportedFormatError(f"未知引擎类型：{proj.engine_type}")
        return {"output_path": str(adapter.ir_to_asset(proj, Path(output_dir)))}

    def run_lqa(units: list[dict]) -> list[dict]:
        """对一批单元重跑静态 LQA 规则，原地刷新 status 与 metadata["lqa_issues"]。

        人工内联审校（Inline Editing）后的**唯一复核入口**：译文一旦在
        壳层被改动，引号配平 / 破折号成双 / 宏锚点守恒必须重新判定 ——
        否则 status 会停留在抽取期快照，绿色胶囊沦为装饰。规则实现完全
        复用 core.lqa.rules.run_static_rules（零 LLM / 零 token / 零 I/O），
        本方法只负责状态机推进与 metadata 落痕。

        语义与 translator 的即时门禁一致（error 阻断 / warning 放行留痕），
        并额外显式处理「译文被人工清空」这一回退场景：回归 EXTRACTED 待译态，
        而不是因标点规则整条跳过而被误判为通过。
        """
        refreshed: list[dict] = []
        for payload in units:
            unit = TranslationUnit.model_validate(payload)
            if not (unit.translated_text or "").strip():
                unit.status = TranslationStatus.EXTRACTED
                unit.metadata.pop("lqa_issues", None)
            else:
                issues = run_static_rules(unit)
                unit.status = (
                    TranslationStatus.LQA_FAILED
                    if any(issue.severity == "error" for issue in issues)
                    else TranslationStatus.LQA_PASSED
                )
                if issues:
                    unit.metadata["lqa_issues"] = [
                        {
                            "rule_id": issue.rule_id,
                            "severity": issue.severity,
                            "message": issue.message,
                        }
                        for issue in issues
                    ]
                else:
                    # 违例已全部修复：清除陈旧留痕，避免前台残留错误气泡
                    unit.metadata.pop("lqa_issues", None)
            refreshed.append(unit.model_dump(mode="json"))
        return refreshed

    def match_glossary(units: list[dict]) -> dict:
        """按单元抽取正文匹配术语表，返回 ``{"matches": {unit_id: [...]}}``。

        与 ``translate_batch`` 的分工：本方法只做**只读的术语标注**，
        不改动单元的任何字段。之所以做成批量 RPC 而非逐行调用，是因为
        匹配依据是 ``extracted_text``（原文），而原文在抽取后就不再变化 ——
        载入时一次取回、整表缓存即可，选中行时零往返，Inspector 才能即时响应。

        无命中的单元从结果里省略（而非给空列表），让回包只承载有效信息。
        将来接入 SQLite WAL 记忆库时，只需替换 core.tm 的实现，
        本方法契约与壳层消费方式均不变。
        """
        matches: dict[str, list[dict]] = {}
        for payload in units:
            unit = TranslationUnit.model_validate(payload)
            found = match_glossary_terms(unit.extracted_text)
            if found:
                matches[unit.id] = [
                    {"source": match.source, "target": match.target, "note": match.note}
                    for match in found
                ]
        return {"matches": matches}

    def detect_engine(game_dir: str) -> dict:
        """模块 0：对游戏根目录做只读指纹侦察，产出结构化引擎画像。

        与 ``detect_format`` 的分工：后者认领**单个文件**（脚本嗅探），
        本方法认领**整作目录**（封包 + PE 特征）。侦察纯只读、限定浅层，
        不执行任何游戏程序、不解包。
        """
        return {"profile": profile_engine(Path(game_dir)).model_dump(mode="json")}

    def init_workspace(game_dir: str) -> dict:
        """模块 1：在游戏根目录创建标准 .galpipeline 工作区（幂等）。

        引擎画像来自现场侦察（而非调用方转交），保证 project.json 与
        磁盘上的真实特征一致；已存在的工程元数据绝不覆盖。
        """
        directory = Path(game_dir)
        profile = profile_engine(directory)
        root = init_workspace_dirs(directory, profile)
        return {
            "workspace_root": str(root),
            "existed": (root / "project.json").exists(),
            "profile": profile.model_dump(mode="json"),
        }

    def list_archives(game_dir: str, engine_type: str | None = None) -> dict:
        """模块 1 编排第一步：列出待解包封包（壳层逐封包调度解包）。

        逐封包短 RPC 而非单次大任务：解包可能耗时数分钟，长 RPC 会堵死
        分发器且无法取消 —— 拆开后取消粒度=封包、进度=已处理/总数。
        """
        return list_archives_entries(game_dir, engine_type)

    def unpack_archive(
        game_dir: str,
        engine_type: str | None,
        archive: str,
        tool_path: str,
        timeout_seconds: float = 600.0,
    ) -> dict:
        """模块 1 编排第二步：解包单个封包到 raw/scripts/{stem}/（幂等）。

        tool_path 必须由壳层经统一工具链解析后传入（本侧不做工具定位）；
        工具未装配 / 退出码异常 / 空产出分别分型上抛，统一映射 -32000。
        """
        return unpack_archive_entry(
            game_dir, engine_type, archive, tool_path, timeout_seconds
        )

    register("detect_format", detect_format)
    register("extract_to_ir", extract_to_ir)
    register("translate_batch", translate_batch)
    register("ir_to_asset", ir_to_asset)
    register("run_lqa", run_lqa)
    register("match_glossary", match_glossary)
    register("detect_engine", detect_engine)
    register("init_workspace", init_workspace)
    register("list_archives", list_archives)
    register("unpack_archive", unpack_archive)
    register("convert_image", convert_image_entry)
    return dispatcher


def serve_stdio(
    dispatcher: RpcDispatcher | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> None:
    """无缓冲行式服务主循环：逐行读请求、逐行写响应并立即 flush。

    stdin/stdout 参数供测试注入；默认接管 sys.stdin/sys.stdout 并
    重配置为 UTF-8。EOF 自然退出，供壳层安全终止。
    """
    if stdin is None or stdout is None:
        try:
            sys.stdin.reconfigure(encoding="utf-8")
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    dispatcher = dispatcher or build_default_dispatcher()
    for line in stdin:
        stripped = line.strip()
        if not stripped:
            continue
        response = dispatcher.handle_request(stripped)
        if response:
            stdout.write(response + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve_stdio()
