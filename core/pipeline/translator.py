"""Galgame 翻译调度器 —— OpenAI 兼容接口 + LQA 即时门禁（Stage 3 核心）。

数据流：TranslationUnit(EXTRACTED) ──批次 Prompt──▶ 兼容 API
        ──清洗回填──▶ run_static_rules 即时断言
        ──▶ LQA_PASSED / LQA_FAILED（issues 落 metadata["lqa_issues"]）。

设计约束：

* 只写单元的 translated_text / status / metadata 三个字段；
  atomic_tags 与 paired_tags 是冻结审计凭据，本模块严禁触碰；
* 网络边界单点收敛在 _post（urllib 同步实现，零新增依赖），供
  Mock 注入与测试替换；超时/连接失败/JSON 损坏按 max_retries 重试，
  HTTP 错误与响应结构异常直接判死（TranslationAPIError）；
* 批次协议：请求按「编号. 【角色】正文」组装，响应按行解析「编号.
  译文」；缺失编号视为协议错误（error 级 → LQA_FAILED）；清洗只剥
  模型惯加的成对包裹引号与空白，「」等正文标点不越权改动。
"""

import json
import re
import urllib.error
import urllib.request

from pydantic import BaseModel, ConfigDict, Field

from ..lqa.rules import LQAIssue, run_static_rules
from ..models.ir import TranslationUnit
from ..models.status import TranslationStatus

_RESPONSE_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、:：]\s*(.*)$")

_DEFAULT_SYSTEM_PROMPT = (
    "你是资深 Galgame 本地化译者。逐条翻译用户给出的编号文本，输出格式"
    "严格为「编号. 译文」，每行一条，不得合并、拆分或添加解释；保留原文"
    "标点风格与语气，控制符占位符必须原样保留。"
)


class TranslationAPIError(Exception):
    """翻译 API 不可用：重试耗尽、HTTP 错误或响应结构异常。"""


class TranslationConfig(BaseModel):
    """调度配置：OpenAI 兼容端点与批次/容错参数。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_base: str = Field(min_length=1, description="OpenAI 兼容端点根地址")
    api_key: str = ""
    model_name: str = Field(min_length=1)
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    system_prompt: str = Field(default=_DEFAULT_SYSTEM_PROMPT)
    batch_size: int = Field(default=8, ge=1)
    timeout_seconds: float = Field(default=30.0, ge=0.1)
    max_retries: int = Field(default=2, ge=0)


class GalgameTranslator:
    """核心调度器：组 Prompt → 调 API → 清洗回填 → LQA 即时门禁。"""

    def __init__(self, config: TranslationConfig) -> None:
        self._config = config

    def translate_batch(self, units: list[TranslationUnit]) -> list[TranslationUnit]:
        size = self._config.batch_size
        for start in range(0, len(units), size):
            chunk = units[start : start + size]
            reply = self._post_with_retry(self._build_payload(chunk))
            try:
                content = str(reply["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                raise TranslationAPIError(f"响应结构异常：{reply!r}") from exc
            translations = self._parse_response(content)
            for number, unit in enumerate(chunk, start=1):
                self._apply(unit, translations.get(number))
        return units

    def _build_payload(self, chunk: list[TranslationUnit]) -> str:
        lines = [
            f"{n}. 【{u.speaker}】{u.extracted_text}" if u.speaker
            else f"{n}. {u.extracted_text}"
            for n, u in enumerate(chunk, start=1)
        ]
        return json.dumps(
            {
                "model": self._config.model_name,
                "temperature": self._config.temperature,
                "messages": [
                    {"role": "system", "content": self._config.system_prompt},
                    {"role": "user", "content": "请翻译下列编号文本：\n" + "\n".join(lines)},
                ],
            },
            ensure_ascii=False,
        )

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
        last: Exception | None = None
        for _ in range(self._config.max_retries + 1):
            try:
                return json.loads(self._post(payload))
            except urllib.error.HTTPError as exc:
                raise TranslationAPIError(f"API HTTP {exc.code}: {exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                last = exc
        raise TranslationAPIError(
            f"重试 {self._config.max_retries + 1} 次后仍失败：{last}"
        ) from last

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
