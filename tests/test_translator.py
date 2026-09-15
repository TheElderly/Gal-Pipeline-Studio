"""core/pipeline/translator.py 契约回归测试（Mock API 自测固化）。

覆盖范围（Mock 注入方式：继承 GalgameTranslator 覆写 _post 网络边界）：

1. 正常流转：批次翻译回填 translated_text，状态推进 LQA_PASSED；
2. 质检拦截：缺失闭引号」的坏译文 → LQA_FAILED，
   metadata["lqa_issues"] 记录 cjk_punctuation 错误；
3. 容错重试：网络抖动重试后成功；重试耗尽抛 TranslationAPIError；
4. 契约守恒：翻译完成后 atomic_tags / paired_tags 零篡改
   （内容快照相等 + 列表对象同一性）。
"""

import json
import sys
import urllib.error
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models.ir import TranslationUnit
from core.models.status import TranslationStatus
from core.models.tags import AtomicTag, PairedTag
from core.pipeline.translator import (
    GalgameTranslator,
    TranslationAPIError,
    TranslationConfig,
)


class MockTranslator(GalgameTranslator):
    """固定回复 Mock：注入罐头译文内容。"""

    def __init__(self, config: TranslationConfig, content: str):
        super().__init__(config)
        self._content = content
        self.calls = 0

    def _post(self, payload: str) -> str:
        self.calls += 1
        return json.dumps(
            {"choices": [{"message": {"content": self._content}}]}, ensure_ascii=False
        )


class FlakyTranslator(GalgameTranslator):
    """前 failures 次抛 URLError 模拟网络抖动，之后回正常响应。"""

    def __init__(self, config: TranslationConfig, failures: int):
        super().__init__(config)
        self.failures = failures
        self.calls = 0

    def _post(self, payload: str) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise urllib.error.URLError("网络抖动")
        return json.dumps({"choices": [{"message": {"content": "1. 好"}}]})


class BrokenJsonTranslator(GalgameTranslator):
    """恒返非 JSON 文本：验证重试耗尽后的判死路径。"""

    def __init__(self, config: TranslationConfig):
        super().__init__(config)
        self.calls = 0

    def _post(self, payload: str) -> str:
        self.calls += 1
        return "not-json-at-all"


@pytest.fixture
def config() -> TranslationConfig:
    return TranslationConfig(api_base="http://mock.local/v1", model_name="glm-test")


def _unit(uid: str, extracted: str, speaker: str | None = None) -> TranslationUnit:
    return TranslationUnit(
        id=uid, speaker=speaker, raw_text=extracted, extracted_text=extracted
    )


class TestNormalFlow:
    """正常流转：批次回填 + 状态推进。"""

    def test_batch_translation_fills_and_passes(self, config):
        u1 = _unit("n-1", "「泣くな」と囁いた", speaker="アリス")
        u2 = _unit("n-2", "——始まりの朝")
        mock = MockTranslator(config, "1. 「别哭」她低声说\n2. ——初始的早晨")

        result = mock.translate_batch([u1, u2])

        assert result[0] is u1 and result[1] is u2  # 原对象原位返回
        assert mock.calls == 1  # 两单元同批一次调用
        assert u1.translated_text == "「别哭」她低声说"
        assert u2.translated_text == "——初始的早晨"
        assert u1.status is TranslationStatus.LQA_PASSED
        assert u2.status is TranslationStatus.LQA_PASSED
        assert "lqa_issues" not in u2.metadata  # 全清不写 issues


class TestLqaInterception:
    """质检拦截：坏译文阻断并留痕。"""

    def test_missing_closing_quote_blocked(self, config):
        unit = _unit("q-1", "「だめ」")
        mock = MockTranslator(config, "1. 「不行")  # 缺失闭引号」

        mock.translate_batch([unit])

        assert unit.status is TranslationStatus.LQA_FAILED
        issues = unit.metadata["lqa_issues"]
        assert issues[0]["rule_id"] == "cjk_punctuation"
        assert issues[0]["severity"] == "error"
        assert "「" in issues[0]["message"]


class TestFaultTolerance:
    """容错重试：抖动自愈与耗尽判死。"""

    def test_network_jitter_retried_then_success(self, config):
        unit = _unit("r-1", "試験")
        flaky = FlakyTranslator(config, failures=2)  # max_retries 默认 2

        flaky.translate_batch([unit])

        assert flaky.calls == 3  # 1 次首发 + 2 次重试
        assert unit.status is TranslationStatus.LQA_PASSED

    def test_retry_exhausted_raises_translation_api_error(self, config):
        exhausted = BrokenJsonTranslator(
            TranslationConfig(
                api_base="http://mock.local/v1", model_name="glm-test", max_retries=1
            )
        )
        with pytest.raises(TranslationAPIError):
            exhausted.translate_batch([_unit("r-2", "試験")])
        assert exhausted.calls == 2  # 1 次首发 + 1 次重试，随后判死


class TestContractConservation:
    """契约守恒：审计凭据零篡改。"""

    def test_atomic_and_paired_tags_untouched(self, config):
        unit = TranslationUnit(
            id="c-1",
            speaker="アリス",
            raw_text="<b>「だめ」</b>と言った[r]",
            extracted_text="「だめ」と言った",
            atomic_tags=[
                AtomicTag(tag_id="macro", raw_tag="[r]", position=15)
            ],
            paired_tags=[
                PairedTag(start_tag="<b>", end_tag="</b>", inner_text="「だめ」")
            ],
        )
        atomic_before = [t.model_dump() for t in unit.atomic_tags]
        paired_before = [p.model_dump() for p in unit.paired_tags]
        atomic_list_id = id(unit.atomic_tags)
        paired_list_id = id(unit.paired_tags)

        MockTranslator(config, "1. 「不行」她说").translate_batch([unit])

        assert unit.status is TranslationStatus.LQA_PASSED  # 凭据合法 + 译文干净
        assert [t.model_dump() for t in unit.atomic_tags] == atomic_before
        assert [p.model_dump() for p in unit.paired_tags] == paired_before
        assert id(unit.atomic_tags) == atomic_list_id  # 列表对象未被替换
        assert id(unit.paired_tags) == paired_list_id
