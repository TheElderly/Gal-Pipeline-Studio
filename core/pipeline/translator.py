"""Galgame 翻译调度器 —— OpenAI 兼容接口 + LQA 即时门禁（Stage 3 核心）。

数据流：TranslationUnit(EXTRACTED) ──批次 Prompt──▶ 兼容 API
        ──清洗回填──▶ run_static_rules 即时断言
        ──▶ LQA_PASSED / LQA_FAILED（issues 落 metadata["lqa_issues"]）。

设计约束：

* 只写单元的 translated_text / status / metadata 三个字段；
  atomic_tags 与 paired_tags 是冻结审计凭据，本模块严禁触碰；
* 网络边界单点收敛在 _post（urllib 同步实现，零新增依赖），供
  Mock 注入与测试替换；超时/连接失败/JSON 损坏按 max_retries 重试，
  HTTP 429/5xx 瞬态错误按**指数退避**重试（``1s, 2s, 4s…`` 带随机抖动，
  尊重 Retry-After 响应头），其余 HTTP 错误与响应结构异常直接判死
  （TranslationAPIError）；
* 批次协议：请求按「编号. 【角色】正文」组装，响应按行解析「编号.
  译文」；缺失编号视为协议错误（error 级 → LQA_FAILED）；清洗只剥
  模型惯加的成对包裹引号与空白，「」等正文标点不越权改动；
* 术语约束注入：组装 System Prompt 时**动态提取本批次原文命中的
  Glossary Matches**，以标准化条目追加为约束块（见 _batch_glossary /
  _compose_system_prompt）。命中为空时逐字等同基线 Prompt —— 零冗余，
  不给模型任何多余的注意力开销；
* 长程滑窗上下文：User Prompt 顶部以隔离块注入「前序已定稿对白」
  （角色 + 译文，``context_lines`` 条，见 _compose_context_block），
  供模型理解人称指代与语气衔接；上下文在**发包计划期一次性快照**，
  并发乱序完成不可能反写后续子批的 Prompt；无前文时逐字零冗余；
* 受控并发池：子批经 ThreadPoolExecutor（上限 = ``concurrency_limit``）
  同时发出 —— _post 是同步 urllib（零新依赖），429 指数退避的 sleep
  发生在各 worker 线程内部，单任务自适应等待不阻塞其他健康任务；
  回填按**提交序（=全局索引序）**收敛，译文与剧本物理行绝对对齐；
* Token 真实计量：解析每个成功响应的 ``usage``（prompt/completion/total）
  并跨并发子批**原子累加**（互斥锁保护），经 ``last_usage`` 暴露 ——
  RPC 层随批次结果回传，壳层据此展示真实会话消耗。缺失 usage 字段
  的响应按零贡献处理（本地 Mock 常不带用量）。
"""

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..lqa.rules import LQAIssue, run_static_rules
from ..models.ir import TranslationUnit
from ..models.status import TranslationStatus
from ..tm.glossary import match_glossary

_RESPONSE_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、:：]\s*(.*)$")

_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
"""可安全重试的瞬态 HTTP 状态码：429 限流 + 网关/上游类 5xx。

429 是「请求太多」而非「请求错误」，服务端明确表达了**稍后可成功**；
500/502/503/504 同为瞬态。其余 4xx（401 鉴权 / 400 载荷 / 404 路径）
重试只是白烧配额，直接判死。
"""

_GLOSSARY_HEADER = (
    "【术语约束】下列术语必须严格采用指定译法，不得改写、不得只译一半："
)
"""术语约束块的表头。用「不得改写」而非「建议」：术语表条目是人工核定的硬约束，
软化措辞会让模型把它当参考而自由发挥，注入就白做了。"""

_CONTEXT_HEADER = "【前文背景参考（仅供理解语境，无需翻译）】"
"""滑窗上下文块的表头：显式声明「无需翻译」，防止模型把前文复述进输出。"""

_CONTEXT_NOTE = (
    "以下为该剧本已定稿的前文对白，仅供理解人称指代与语气衔接；"
    "严禁翻译、复述或续写这些内容，你的输出仍只包含当前批次的编号条目。"
)
"""滑窗上下文块的行为约束行：与表头一起构成双重隔离，
确保参考背景不会被混入「编号. 译文」的解析面。"""

_DEFAULT_SYSTEM_PROMPT = (
    "你是资深 Galgame 本地化译者。逐条翻译用户给出的编号文本，输出格式"
    "严格为「编号. 译文」，每行一条，不得合并、拆分或添加解释；保留原文"
    "标点风格与语气，控制符占位符必须原样保留。"
)


class TranslationAPIError(Exception):
    """翻译 API 不可用：重试耗尽、HTTP 错误或响应结构异常。"""


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """解析 429/503 响应的 ``Retry-After`` 头（秒）；缺失或非法返回 None。

    服务端显式给出的等待窗口比客户端的指数猜测更权威，取较大值即可
    同时尊重服务端意愿与退避语义。
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:  # noqa: BLE001 — 非标准头对象一律按缺失处理
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class TranslationConfig(BaseModel):
    """调度配置：OpenAI 兼容端点与批次/容错参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_base: str = Field(min_length=1, description="OpenAI 兼容端点根地址")
    api_key: str = ""
    model_name: str = Field(min_length=1)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = Field(
        default=None,
        description="推理力度（适配 DeepSeek-R1 / OpenAI o 系列 / GLM 推理模型）；None 不注入载荷",
    )
    system_prompt: str = Field(default=_DEFAULT_SYSTEM_PROMPT)
    batch_size: int = Field(default=8, ge=1)
    timeout_seconds: float = Field(default=30.0, ge=0.1)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(
        default=1.0,
        ge=0.0,
        description="指数退避基值（秒）：第 n 次重试前等待 base * 2^n + 抖动；"
        "测试置 0 可消除全部等待",
    )
    concurrency_limit: int = Field(
        default=3,
        ge=1,
        description="并发池上限：同时发出的子批请求数；1 即完全串行",
    )
    context_lines: int = Field(
        default=4,
        ge=0,
        le=20,
        description="滑窗上下文行数：注入的「前序已定稿对白」条数；0 关闭滑窗（零冗余）",
    )


def _batch_glossary(chunk: list[TranslationUnit]) -> list[tuple[str, str]]:
    """收集本批次原文命中的术语，返回 ``[(源词, 目标译法)]``。

    三条确定性纪律：

    * **去重**：同一术语在本批次多行出现只登记一次，避免约束块被同一句
      规则刷屏（术语表的价值是「约束」不是「词频统计」）；
    * **顺序稳定**：按「单元顺序 → 单元内首现偏移」的天然顺序插入 dict，
      输出的条目顺序可复现 —— 同批次恒得同一份 Prompt，便于缓存与断言；
    * **源词冲突取先见**：同一源词若被映射到不同译法（术语表自身矛盾），
      保留先出现的那条而不是抛异常 —— 翻译不该因为术语表数据问题整批失败。
    """
    seen: dict[str, str] = {}
    for unit in chunk:
        for match in match_glossary(unit.extracted_text):
            seen.setdefault(match.source, match.target)
    return list(seen.items())


def _finalized_entries(units: list[TranslationUnit]) -> list[tuple[str | None, str]]:
    """收集单元列表里的**已定稿对白**（有译文的），按原序返回 ``[(角色, 译文)]``。

    「已定稿」的口径刻意宽松：只要存在非空译文就算 —— 与壳层防丢失
    判定（HasUnsavedTranslations）同一倾向：宁可多给一行参考，也不让
    模型对着真空猜指代。半成品译文同样是有效语境。
    """
    entries: list[tuple[str | None, str]] = []
    for unit in units:
        text = (unit.translated_text or "").strip()
        if text:
            entries.append((unit.speaker, text))
    return entries


def _normalize_context(
    context: list[dict] | None, window: int
) -> list[tuple[str | None, str]]:
    """归一化调用方显式传入的滑窗上下文（RPC ``context`` 参数）。

    每项形如 ``{"speaker": str | None, "text": str}``；畸形项（非 dict、
    缺 text、纯空白）**静默跳过**而不是整批失败 —— 上下文是增益信息，
    不该因一条脏数据拖垮翻译。截取最近 window 条。
    """
    if not context or window <= 0:
        return []
    entries: list[tuple[str | None, str]] = []
    for item in context:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        speaker = item.get("speaker")
        entries.append((speaker if isinstance(speaker, str) else None, text))
    return entries[-window:]


def _compose_context_block(entries: list[tuple[str | None, str]]) -> str | None:
    """组装滑窗上下文隔离块；空序列返回 None（调用方零冗余直落基线）。"""
    if not entries:
        return None
    lines = [f"【{speaker}】{text}" if speaker else text for speaker, text in entries]
    return "\n".join([_CONTEXT_HEADER, _CONTEXT_NOTE, *lines])


class GalgameTranslator:
    """核心调度器：组 Prompt → 调 API → 清洗回填 → LQA 即时门禁。"""

    def __init__(self, config: TranslationConfig) -> None:
        self._config = config
        self._usage_lock = threading.Lock()
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    @property
    def last_usage(self) -> dict[str, int]:
        """最近一次 ``translate_batch`` 的真实 Token 消耗（跨子批原子累加值）。

        每次调用开始时清零重计；响应缺失 ``usage`` 字段按零贡献处理。
        RPC 层据此随批次结果回传，壳层累加为会话总消耗。
        """
        with self._usage_lock:
            return dict(self._usage)

    @staticmethod
    def _extract_usage(reply: dict) -> dict[str, int]:
        """从响应提取 usage 三元组；缺失/畸形字段一律按 0 计（不猜、不伪造）。

        计量可以缺失，不可以是错的：负数钳 0、非数值按 0 —— 任何畸形
        都不得让翻译批次本身失败。
        """

        def as_int(value: object) -> int:
            try:
                return max(0, int(value))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return 0

        usage = reply.get("usage") if isinstance(reply, dict) else None
        raw = usage if isinstance(usage, dict) else {}
        return {
            "prompt_tokens": as_int(raw.get("prompt_tokens")),
            "completion_tokens": as_int(raw.get("completion_tokens")),
            "total_tokens": as_int(raw.get("total_tokens")),
        }

    def _accumulate_usage(self, usage: dict[str, int]) -> None:
        with self._usage_lock:
            for key, value in usage.items():
                self._usage[key] += value

    def translate_batch(
        self,
        units: list[TranslationUnit],
        context: list[dict] | None = None,
    ) -> list[TranslationUnit]:
        """批量翻译：组 Prompt（滑窗上下文 + 术语）→ 受控并发调 API → 清洗回填。

        ``context``：调用方显式提供的已定稿前文（RPC 传入，形如
        ``[{"speaker": ..., "text": ...}]``）—— 跨调用滑窗的唯一来源；
        缺省时回退为本批 units 内**先行定稿**的条目（直调管线场景）。

        并发语义（``concurrency_limit`` > 1 时）：

        * 所有子批的 Prompt（含滑窗上下文与术语约束）在**发包计划期
          一次性快照** —— 乱序完成不可能反写后续子批的载荷；
        * ThreadPoolExecutor 上限即并发池上限；429 退避的 sleep 发生在
          各 worker 线程内部，单任务自适应等待不阻塞其他健康任务；
        * 回填按提交序（=全局索引序）收敛，任一子批失败整批以
          TranslationAPIError 判死（与串行语义一致）。
        """
        if not units:
            return units
        window = self._config.context_lines
        leading = _normalize_context(context, window)
        size = self._config.batch_size

        # 会话计量重置：last_usage 语义是「本次调用的消耗」，直调复用
        # 同一 translator 时不得带上一次的残留。
        with self._usage_lock:
            self._usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        plans: list[tuple[list[TranslationUnit], list[tuple[str | None, str]]]] = []
        for start in range(0, len(units), size):
            chunk = units[start : start + size]
            entries = leading + _finalized_entries(units[:start])
            plans.append((chunk, entries[-window:] if window > 0 else []))

        workers = min(self._config.concurrency_limit, len(plans))
        if workers <= 1:
            for chunk, entries in plans:
                self._translate_chunk(chunk, entries)
            return units

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._translate_chunk, chunk, entries)
                for chunk, entries in plans
            ]
            for future in futures:  # 按提交序收敛：译文与物理行绝对对齐
                future.result()
        return units

    def _translate_chunk(
        self, chunk: list[TranslationUnit], context_entries: list[tuple[str | None, str]]
    ) -> None:
        """单个子批的完整往返：组载荷 → 重试收包 → 按编号回填本子批单元。"""
        reply = self._post_with_retry(self._build_payload(chunk, context_entries))
        # usage 只在成功响应上存在（429 重试的失败响应无用量）——
        # 计入位置在成功收包处，天然不重复计费
        self._accumulate_usage(self._extract_usage(reply))
        try:
            content = str(reply["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationAPIError(f"响应结构异常：{reply!r}") from exc
        translations = self._parse_response(content)
        for number, unit in enumerate(chunk, start=1):
            self._apply(unit, translations.get(number))

    def _build_payload(
        self,
        chunk: list[TranslationUnit],
        context_entries: list[tuple[str | None, str]] = (),
    ) -> str:
        lines = [
            f"{n}. 【{u.speaker}】{u.extracted_text}" if u.speaker
            else f"{n}. {u.extracted_text}"
            for n, u in enumerate(chunk, start=1)
        ]
        payload: dict = {
            "model": self._config.model_name,
            "temperature": self._config.temperature,
            "messages": [
                {"role": "system", "content": self._compose_system_prompt(chunk)},
                {"role": "user", "content": self._compose_user_prompt(chunk, context_entries)},
            ],
        }
        if self._config.reasoning_effort is not None:
            payload["reasoning_effort"] = self._config.reasoning_effort
        return json.dumps(payload, ensure_ascii=False)

    def _compose_user_prompt(
        self,
        chunk: list[TranslationUnit],
        context_entries: list[tuple[str | None, str]] = (),
    ) -> str:
        """User Prompt = 可选的滑窗上下文隔离块 + 编号待译正文。

        上下文只进 User 角色且带显式「无需翻译」约束 —— System Prompt
        保持只承载身份与术语约束，编号行解析面（「编号. 译文」）不被
        参考背景污染。无上下文时逐字等同基线（零冗余）。
        """
        lines = [
            f"{n}. 【{u.speaker}】{u.extracted_text}" if u.speaker
            else f"{n}. {u.extracted_text}"
            for n, u in enumerate(chunk, start=1)
        ]
        body = "请翻译下列编号文本：\n" + "\n".join(lines)
        block = _compose_context_block(list(context_entries))
        return f"{block}\n\n{body}" if block else body

    def _compose_system_prompt(self, chunk: list[TranslationUnit]) -> str:
        """基线 System Prompt + 本批次命中的术语约束块。

        术语表此前只在 Inspector 里当 Chip 展示、**从不进 Prompt** ——
        译者（模型）看不到任何约束，术语表对译文质量零贡献，属于典型的花架子。
        这里把命中项真正送达模型。

        未命中时返回**逐字等同**基线 Prompt 的字符串（不追加空行、不追加空块），
        保证「无术语」路径与注入功能上线前完全一致 —— 零回归风险、零冗余 token。
        """
        constraints = _batch_glossary(chunk)
        if not constraints:
            return self._config.system_prompt
        block = "\n".join(
            [_GLOSSARY_HEADER, *(f"- {source} → {target}" for source, target in constraints)]
        )
        return f"{self._config.system_prompt}\n\n{block}"

    @staticmethod
    def _parse_response(content: str) -> dict[int, str]:
        """按行解析「编号. 译文」；只剥成对包裹引号与空白，「」不动。"""
        results: dict[int, str] = {}
        for line in content.splitlines():
            match = _RESPONSE_LINE_RE.match(line)
            if not match:
                continue
            text = match.group(2).strip()
            for left, right in (('"', '"'), ("\u201c", "\u201d")):
                if len(text) >= 2 and text.startswith(left) and text.endswith(right):
                    text = text[1:-1].strip()
            if text:
                results[int(match.group(1))] = text
        return results

    def _post(self, payload: str) -> str:
        request = urllib.request.Request(
            f"{self._config.api_base.rstrip('/')}/chat/completions",
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._config.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as resp:
            return resp.read().decode("utf-8")

    def _post_with_retry(self, payload: str) -> dict:
        """带瞬态感知的稳健重试：429/5xx 指数退避，其余错误一击判死。

        历史缺陷：HTTPError 一律当场抛死 —— 429（Too Many Requests）本意
        是「稍后再来」，一击即毙会让整个翻译批次因限流窗口全军覆没。
        现按 ``base * 2^attempt + jitter`` 退避（默认 1s/2s/4s…），服务端
        给了 ``Retry-After`` 时取两者较大值；重试次数仍由 max_retries 治理。
        """
        last: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                return json.loads(self._post(payload))
            except urllib.error.HTTPError as exc:
                if exc.code not in _RETRYABLE_HTTP_CODES:
                    raise TranslationAPIError(f"API HTTP {exc.code}: {exc.reason}") from exc
                last = exc
                delay = self._retry_delay(attempt, _retry_after_seconds(exc))
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
                delay = self._retry_delay(attempt, None)
            if attempt < self._config.max_retries:
                time.sleep(delay)
        raise TranslationAPIError(
            f"重试 {self._config.max_retries + 1} 次后仍失败：{last}"
        ) from last

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        """第 attempt 次重试前的等待秒数：指数退避 ∨ Retry-After，再加抖动。

        抖动上限 0.25s（且不超过基值的 10%）：打散并发客户端的重试节拍、
        避免惊群，又不至于让单发等待显著拉长。测试把基值置 0 即得零等待
        的确定性路径。
        """
        delay = self._config.retry_backoff_seconds * (2**attempt)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay + random.uniform(0.0, min(0.25, delay * 0.1))

    @staticmethod
    def _apply(unit: TranslationUnit, translated: str | None) -> None:
        """回填译文并执行 LQA 即时门禁；warning 放行、error 阻断。"""
        issues: list[LQAIssue] = []
        text = (translated or "").strip()
        if text:
            unit.translated_text = text
            issues.extend(run_static_rules(unit))
        else:
            issues.append(LQAIssue(
                rule_id="translator_protocol", severity="error",
                message=f"单元 {unit.id}：批次响应缺失可用译文", unit_id=unit.id,
            ))
        unit.status = (
            TranslationStatus.LQA_FAILED
            if any(i.severity == "error" for i in issues)
            else TranslationStatus.LQA_PASSED
        )
        if issues:
            unit.metadata["lqa_issues"] = [
                {"rule_id": i.rule_id, "severity": i.severity, "message": i.message}
                for i in issues
            ]
        else:
            # 复核通过必须清除陈旧留痕：单元上一轮失败留下的 error 条目若不清，
            # 壳层 IssueTooltip 会继续把红色错误气泡挂在绿色的已通过胶囊上。
            unit.metadata.pop("lqa_issues", None)
