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
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from ..adapters.base import EngineAdapterError, UnsupportedFormatError
from ..adapters.kag import KagAdapter
from ..models.ir import GalIRProject, TranslationUnit
from ..pipeline.translator import GalgameTranslator, TranslationConfig

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
BUSINESS_ERROR = -32000
"""业务错误约定码：EngineAdapterError 家族统一映射于此。"""


class _RpcFailure(Exception):
    """分发器内部控制流：携带 JSON-RPC 错误码的协议级失败。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    adapters: dict[str, Any] = {"kag": KagAdapter()}
    dispatcher = RpcDispatcher()
    register = dispatcher.register

    register("ping", lambda: "pong")

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

    def translate_batch(units: list[dict], config: dict) -> list[dict]:
        validated = [TranslationUnit.model_validate(unit) for unit in units]
        translator = GalgameTranslator(TranslationConfig(**config))
        return [u.model_dump(mode="json") for u in translator.translate_batch(validated)]

    def ir_to_asset(project: dict, output_dir: str) -> dict:
        proj = GalIRProject.model_validate(project)
        adapter = adapters.get(proj.engine_type)
        if adapter is None:
            raise UnsupportedFormatError(f"未知引擎类型：{proj.engine_type}")
        return {"output_path": str(adapter.ir_to_asset(proj, Path(output_dir)))}

    register("detect_format", detect_format)
    register("extract_to_ir", extract_to_ir)
    register("translate_batch", translate_batch)
    register("ir_to_asset", ir_to_asset)
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
