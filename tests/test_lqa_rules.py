"""core/lqa/rules.py 契约回归测试（静态质检冒烟固化）。

覆盖范围：

1. atomic_tags 守恒与越界检测：合法锚点放行、锚点字面不符、
   锚点区间越界；
2. paired_tags 计数配平与次序检测：合法成对放行、开闭计数失衡、
   开闭次序颠倒、闭标记缺失；
3. 「」直角引号与破折号——质检断言：配平/嵌套/交叉错配、
   成双/孤立/奇数连续游程、未翻译（None）跳过语义；
4. 聚合入口 run_static_rules：全合法单元零违例、unit_id 回填。
"""

import sys
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.lqa.rules import (
    RULE_ATOMIC_CONSERVATION,
    RULE_CJK_PUNCTUATION,
    RULE_PAIRED_BALANCE,
    check_atomic_conservation,
    check_cjk_punctuation,
    check_paired_balance,
    run_static_rules,
)
from core.models.ir import TranslationUnit
from core.models.tags import AtomicTag, PairedTag


class TestAtomicConservation:
    """原子标记锚点守恒：合法放行、字面不符、区间越界。"""

    def test_valid_anchor_passes(self):
        unit = TranslationUnit(
            id="a-ok",
            raw_text="AB%dC",
            extracted_text="ABC",
            atomic_tags=[AtomicTag(tag_id="fmt", raw_tag="%d", position=2)],
        )
        assert check_atomic_conservation(unit) == []

    def test_anchor_literal_mismatch_detected(self):
        """position 是合法数字但指向错误子串——违例在语义层。"""
        unit = TranslationUnit(
            id="a-mismatch",
            raw_text="AB%dC",
            extracted_text="ABC",
            atomic_tags=[AtomicTag(tag_id="fmt", raw_tag="%d", position=1)],
        )
        issues = check_atomic_conservation(unit)
        assert len(issues) == 1
        assert issues[0].rule_id == RULE_ATOMIC_CONSERVATION
        assert issues[0].severity == "error"
        assert issues[0].unit_id == "a-mismatch"
        assert "字面不符" in issues[0].message

    def test_anchor_out_of_bounds_detected(self):
        unit = TranslationUnit(
            id="a-oob",
            raw_text="AB",
            extracted_text="AB",
            atomic_tags=[AtomicTag(tag_id="fmt", raw_tag="%d", position=1)],
        )
        issues = check_atomic_conservation(unit)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "越界" in issues[0].message


class TestPairedBalance:
    """成对标记配平：合法放行、计数失衡、次序颠倒、闭标记缺失。"""

    def test_valid_pair_passes(self):
        unit = TranslationUnit(
            id="p-ok",
            raw_text="<b>強調</b>",
            extracted_text="強調",
            paired_tags=[PairedTag(start_tag="<b>", end_tag="</b>", inner_text="強調")],
        )
        assert check_paired_balance(unit) == []

    def test_count_imbalance_detected(self):
        """raw_text 中残留未登记的孤残闭标记——计数不配平。"""
        unit = TranslationUnit(
            id="p-imbalance",
            raw_text="<b>強調</b>尾部</b>",
            extracted_text="強調尾部",
            paired_tags=[PairedTag(start_tag="<b>", end_tag="</b>", inner_text="強調")],
        )
        issues = check_paired_balance(unit)
        assert len(issues) == 1
        assert issues[0].rule_id == RULE_PAIRED_BALANCE
        assert issues[0].severity == "error"
        assert "不配平" in issues[0].message

    def test_order_reversed_detected(self):
        unit = TranslationUnit(
            id="p-reversed",
            raw_text="</b>前缀<b>",
            extracted_text="前缀",
            paired_tags=[PairedTag(start_tag="<b>", end_tag="</b>", inner_text="前缀")],
        )
        issues = check_paired_balance(unit)
        assert len(issues) == 1
        assert "次序颠倒" in issues[0].message

    def test_missing_end_tag_detected(self):
        unit = TranslationUnit(
            id="p-missing",
            raw_text="<b>只有开标记",
            extracted_text="只有开标记",
            paired_tags=[PairedTag(start_tag="<b>", end_tag="</b>", inner_text="只有开标记")],
        )
        issues = check_paired_balance(unit)
        assert len(issues) == 1
        assert "不存在" in issues[0].message


class TestCjkPunctuation:
    """「」直角引号与破折号——断言：配平、嵌套、交叉、成双、奇偶。"""

    @pytest.mark.parametrize(
        ("text", "n_errors"),
        [
            ("「你好」——世界", 0),
            ("「外「内」终」——嵌套", 0),
            ("————", 0),
            ("「未闭合", 1),
            ("多余」的闭引号", 1),
            ("」先「后交叉", 2),
            ("孤—立", 1),
            ("三———连", 1),
            (None, 0),
        ],
        ids=[
            "平衡引号与成双破折号",
            "合法嵌套引号",
            "四连破折号放行",
            "未闭合开引号",
            "多余闭引号",
            "交叉错配双报",
            "孤立单破折号",
            "奇数三连破折号",
            "未翻译None跳过",
        ],
    )
    def test_quote_and_dash_assertions(self, text, n_errors):
        issues = check_cjk_punctuation(text)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == n_errors
        assert all(i.rule_id == RULE_CJK_PUNCTUATION for i in issues)

    def test_issue_shape_on_direct_text_call(self):
        issue = check_cjk_punctuation("孤—")[0]
        assert issue.rule_id == RULE_CJK_PUNCTUATION
        assert issue.severity == "error"
        assert issue.unit_id is None  # 直接吃 str 时无单元上下文


class TestAggregatedEntry:
    """聚合入口 run_static_rules：全合法零违例 + unit_id 回填。"""

    def test_fully_valid_unit_clean(self):
        unit = TranslationUnit(
            id="agg-ok",
            raw_text="<b>強調</b>%d",
            extracted_text="強調",
            atomic_tags=[AtomicTag(tag_id="fmt", raw_tag="%d", position=9)],
            paired_tags=[PairedTag(start_tag="<b>", end_tag="</b>", inner_text="強調")],
            translated_text="「强调」——完成",
        )
        assert run_static_rules(unit) == []

    def test_unit_id_propagated_to_punctuation_issues(self):
        unit = TranslationUnit(
            id="agg-bad",
            raw_text="テスト",
            extracted_text="テスト",
            translated_text="「未闭合",
        )
        issues = run_static_rules(unit)
        assert len(issues) == 1
        assert issues[0].rule_id == RULE_CJK_PUNCTUATION
        assert issues[0].unit_id == "agg-bad"
