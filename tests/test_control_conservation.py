"""译文侧控制符携带模式审计（``check_control_conservation``）的契约测试。

与 ``test_lqa_rules.py`` 的分工：那边钉 raw_text 侧的锚点/配平/标点，
这边专钉**译文与控制符登记表之间的携带模式** —— 宏被删改、部分携带、
篡改参数、乱序、重复注入必须在 LQA 层被拦下，而不是流到回写边界。

背景（三态语义的由来，见规则 docstring）：当前 KAG 管线是「剥离式」——
译文规范形态是零宏纯散文，宏由回写器机械复写。因此本文件同时钉住：

* 剥离式规范形态必须**继续通过**（回归保护：旧夹具不许被新规则击穿）；
* 人工/模型把宏带进译文时，携带模式必须被精确审计（新增护城河）；
* 篡改变体（同族参数被改）必须 error，无关方括号记号只 warning。
"""

import json
import sys
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lqa.rules import (
    RULE_CONTROL_CONSERVATION,
    check_control_conservation,
    run_static_rules,
)
from core.models.ir import TranslationUnit
from core.models.status import TranslationStatus
from core.models.tags import AtomicTag
from core.pipeline.translator import GalgameTranslator, TranslationConfig


def _unit(
    uid: str,
    raw: str,
    tags: list[str],
    translated: str | None,
    speaker: str = "アリス",
) -> TranslationUnit:
    """构造带宏登记的单元：raw_text 含宏，extracted_text 为剥宏正文。"""
    extracted = raw
    for tag in tags:
        extracted = extracted.replace(tag, "", 1)
    return TranslationUnit(
        id=uid,
        speaker=speaker,
        raw_text=raw,
        extracted_text=extracted,
        atomic_tags=[
            AtomicTag(tag_id="macro", raw_tag=tag, position=raw.find(tag))
            for tag in tags
        ],
        translated_text=translated,
        status=TranslationStatus.EXTRACTED,
    )


def _errors(issues):
    return [i for i in issues if i.severity == "error"]


def _warnings(issues):
    return [i for i in issues if i.severity == "warning"]


# ---------------------------------------------------------------------------
# 1. 剥离式规范形态（当前 KAG 管线的 norm）：必须继续全绿
# ---------------------------------------------------------------------------


class TestStrippedMode:
    def test_pure_prose_translation_with_registered_macros_passes(self):
        """规范形态：登记了宏、译文是零宏散文 —— 回写器机械复写，LQA 放行。"""
        unit = _unit("s-1", "「だめ」[r]", ["[r]"], "「不行」")
        assert check_control_conservation(unit) == []

    def test_multi_macro_stripped_translation_passes(self):
        unit = _unit(
            "s-2",
            "泣くな[r][ruby text=\"はやく\"]早く言って[p]",
            ["[r]", '[ruby text="はやく"]', "[p]"],
            "别哭呀快点说",
            speaker=None,
        )
        assert check_control_conservation(unit) == []

    def test_macro_free_unit_with_clean_translation_passes(self):
        unit = _unit("s-3", "「ただ」", [], "「只是」")
        assert check_control_conservation(unit) == []

    def test_none_translation_skips_entirely(self):
        unit = _unit("s-4", "「だめ」[r]", ["[r]"], None)
        assert check_control_conservation(unit) == []

    def test_aggregate_includes_control_conservation_before_punctuation(self):
        """run_static_rules 的聚合序：control_conservation 先于 cjk_punctuation。"""
        # 同时造两类违例：部分携带（control）+ 缺闭引号（cjk）
        unit = _unit("s-5", "「だめ」[r][p]", ["[r]", "[p]"], "「不行[r]他说")
        issues = run_static_rules(unit)
        ids = [i.rule_id for i in issues]
        assert "control_conservation" in ids and "cjk_punctuation" in ids
        assert ids.index("control_conservation") < ids.index("cjk_punctuation")


# ---------------------------------------------------------------------------
# 2. 全携带模式（未来模型自带宏 / 人工完整粘贴）：守恒即通过
# ---------------------------------------------------------------------------


class TestCarriedMode:
    def test_full_carry_with_matching_order_passes(self):
        unit = _unit("c-1", "「だめ」[r][p]", ["[r]", "[p]"], "「不行[r]呢[p]」")
        assert check_control_conservation(unit) == []

    def test_repeated_literal_full_carry_passes(self):
        """同一字面量登记两次，译文也必须恰好携带两次。"""
        unit = _unit("c-2", "あ[r]い[r]う", ["[r]", "[r]"], "阿[r]衣[r]乌")
        assert check_control_conservation(unit) == []

    def test_reordered_macros_are_blocked(self):
        unit = _unit("c-3", "「だめ」[r][p]", ["[r]", "[p]"], "「不行[p]呢[r]」")
        issues = _errors(check_control_conservation(unit))
        assert len(issues) == 1
        assert "相对顺序" in issues[0].message

    def test_over_carried_macro_is_blocked(self):
        unit = _unit("c-4", "「だめ」[r]", ["[r]"], "「不行[r]呢[r]」")
        issues = _errors(check_control_conservation(unit))
        assert len(issues) == 1
        assert "数量超出" in issues[0].message


# ---------------------------------------------------------------------------
# 3. 部分携带（最危险形态）：回写复写叠加 → 重复注入 + 失锚
# ---------------------------------------------------------------------------


class TestPartialCarry:
    def test_partial_carry_is_blocked_with_missing_list(self):
        unit = _unit(
            "p-1",
            "「だめ」[r][p]",
            ["[r]", "[p]"],
            "「不行[r]呢」",
        )
        issues = _errors(check_control_conservation(unit))
        assert len(issues) == 1
        assert "部分携带" in issues[0].message
        assert "[p]" in issues[0].message  # 缺失清单必须点名

    def test_partial_carry_message_warns_about_double_insertion(self):
        unit = _unit("p-2", "「だめ」[r][p]", ["[r]", "[p]"], "「[r]不行」")
        (issue,) = _errors(check_control_conservation(unit))
        assert "重复注入" in issue.message

    def test_run_static_rules_marks_partial_carry_failed(self):
        """端到端语义：部分携带单元经 run_static_rules 必须产生 error 级违例。"""
        unit = _unit("p-3", "「だめ」[r][p]", ["[r]", "[p]"], "「不行[p]」")
        issues = run_static_rules(unit)
        assert any(
            i.rule_id == RULE_CONTROL_CONSERVATION and i.severity == "error"
            for i in issues
        )


# ---------------------------------------------------------------------------
# 4. 篡改与未登记 token
# ---------------------------------------------------------------------------


class TestTampering:
    def test_same_family_variant_is_error(self):
        """登记 [ruby text="ゆびきり"]，译文写 [ruby text="ちよ"] —— 同族篡改。"""
        unit = _unit(
            "t-1",
            '「ゆびきり」[ruby text="ゆびきり"]',
            ['[ruby text="ゆびきり"]'],
            '「拉钩[ruby text="ちよ"]」',
        )
        issues = _errors(check_control_conservation(unit))
        assert any("篡改" in i.message for i in issues)

    def test_unrelated_bracket_token_is_warning_only(self):
        """与登记表无关的方括号记号（如 [注1]）只 advisory，不阻断。"""
        unit = _unit("t-2", "「ただ」", [], "「只是[注1]」")
        issues = check_control_conservation(unit)
        assert _errors(issues) == []
        assert len(_warnings(issues)) == 1

    def test_unregistered_token_with_empty_registry_is_warning(self):
        unit = _unit("t-3", "「ただ」", [], "「只是[注1]呢」")
        issues = check_control_conservation(unit)
        assert _errors(issues) == []
        assert len(_warnings(issues)) == 1


# ---------------------------------------------------------------------------
# 5. 管线集成：translate_batch / 人工自带宏 与 模型吃宏 两条路
# ---------------------------------------------------------------------------


class MockTranslator(GalgameTranslator):
    """固定回复 Mock：覆写 _post 注入罐头译文（与 test_translator 同法）。"""

    def __init__(self, config: TranslationConfig, content: str):
        super().__init__(config)
        self._content = content

    def _post(self, payload: str) -> str:
        return json.dumps(
            {"choices": [{"message": {"content": self._content}}]}, ensure_ascii=False
        )


class TestPipelineIntegration:
    def test_human_carried_macros_pass_the_gate(self):
        """人工把宏完整带进译文（全携带）：门禁放行，状态 LQA_PASSED。"""
        unit = _unit("i-1", "「だめ」[r][p]", ["[r]", "[p]"], None)
        MockTranslator(
            TranslationConfig(api_base="http://mock.local/v1", model_name="glm-test"),
            "1. 「不行[r]呢[p]」",
        ).translate_batch([unit])
        assert unit.status is TranslationStatus.LQA_PASSED
        assert "lqa_issues" not in unit.metadata

    def test_dropped_macro_is_caught_as_control_violation(self):
        """回归核心：模型答了**部分**宏（吃掉一个）→ control_conservation 阻断。

        该形态只能由「人工把宏敲进编辑框」或未来模型自带宏的引擎流产生
        （当前剥离式 Prompt 不给模型看宏），但门禁必须两路都拦。
        """
        unit = _unit("i-2", "「だめ」[r][p]", ["[r]", "[p]"], None)
        MockTranslator(
            TranslationConfig(api_base="http://mock.local/v1", model_name="glm-test"),
            "1. 「不行[r]呢」",
        ).translate_batch([unit])
        assert unit.status is TranslationStatus.LQA_FAILED
        issues = unit.metadata["lqa_issues"]
        assert any(
            i["rule_id"] == RULE_CONTROL_CONSERVATION and i["severity"] == "error"
            for i in issues
        )

    def test_stripped_pipeline_reply_still_passes(self):
        """回归保护：现行管线（模型回纯散文）不许被新规则误伤。"""
        unit = _unit("i-3", "「だめ」[r][p]", ["[r]", "[p]"], None)
        MockTranslator(
            TranslationConfig(api_base="http://mock.local/v1", model_name="glm-test"),
            "1. 「不行呢」",
        ).translate_batch([unit])
        assert unit.status is TranslationStatus.LQA_PASSED


# ---------------------------------------------------------------------------
# 6. 参数化回归：剥离态在多宏组合下保持零违例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "tags", "translation"),
    [
        ("「あ」[p]", ["[p]"], "「啊」"),
        ("あ[r]い[r]う", ["[r]", "[r]"], "阿衣乌"),
        ('ゆびきり[ruby text="げんまん"]げんまん[p]', ['[ruby text="げんまん"]', "[p]"], "拉钩上吊"),
        ("[font size=24]大[font size=default]小[p]", ["[font size=24]", "[font size=default]", "[p]"], "大大小小"),
    ],
)
def test_stripped_mode_matrix(raw, tags, translation):
    unit = _unit("m-x", raw, tags, translation, speaker=None)
    assert check_control_conservation(unit) == []
