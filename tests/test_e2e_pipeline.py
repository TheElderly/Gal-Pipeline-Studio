"""端到端集成测试 —— 完整本地化生命周期（KAG 抽取 → 翻译 → LQA → 回写）。

验证链路::

    真实 .ks 文本 ──KagAdapter.extract_to_ir──▶ GalIRProject(EXTRACTED)
        ──MockTranslator.translate_batch──▶ LQA_PASSED + 译文回填
        ──KagAdapter.ir_to_asset──▶ 汉化 .ks（BOM / 骨架保真 / 宏原位）

期望回写行按 _reinsert_tags 的确定性算法逐字符推演：
[r] 与 [ruby] 的复写位置 = raw_text 偏移减说话人前缀长，越界夹取。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.kag import KagAdapter
from core.models.status import TranslationStatus
from core.pipeline.translator import GalgameTranslator, TranslationConfig

KAG_SAMPLE = (
    "; 章节头注释 —— 骨架保真断言对象\n"
    "*chapter1_start\n"
    "@bg storage=bg01 time=1000\n"
    "【アリス】泣くな[r][ruby text=\"はやく\"]早く言って\n"
    "風が強い日だった[p]\n"
    "【ボブ】「行こう」[r]彼は言った\n"
    "; 场景尾部注释\n"
)

MOCK_REPLIES = "1. 别哭呀快点说\n2. 风很大的一天\n3. 「走吧」他低声说"


class MockTranslator(GalgameTranslator):
    """固定回复 Mock：覆写 _post 注入罐头译文。"""

    def __init__(self, config: TranslationConfig, content: str):
        super().__init__(config)
        self._content = content

    def _post(self, payload: str) -> str:
        return json.dumps(
            {"choices": [{"message": {"content": self._content}}]}, ensure_ascii=False
        )


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory) -> SimpleNamespace:
    """一次性跑通完整生命周期，各阶段产物供六个断言面分项钉住。"""
    workdir = tmp_path_factory.mktemp("e2e")
    ks_file = workdir / "scenario.ks"
    ks_file.write_text(KAG_SAMPLE, encoding="utf-8")

    adapter = KagAdapter()
    project = adapter.extract_to_ir(ks_file)
    extract_statuses = [u.status for u in project.units]

    config = TranslationConfig(api_base="http://mock.local/v1", model_name="glm-test")
    MockTranslator(config, MOCK_REPLIES).translate_batch(project.units)

    output = adapter.ir_to_asset(project, workdir / "out")
    lines = output.read_text(encoding="utf-8-sig").split("\n")
    return SimpleNamespace(
        project=project,
        extract_statuses=extract_statuses,
        output=output,
        lines=lines,
    )


class TestFullLocalizationLifecycle:
    """六个断言面分别钉住各阶段契约。"""

    def test_extraction_produces_extracted_units(self, pipeline):
        units = pipeline.project.units
        assert [u.id for u in units] == ["scenario-00004", "scenario-00005", "scenario-00006"]
        assert pipeline.extract_statuses == [TranslationStatus.EXTRACTED] * 3
        assert [u.speaker for u in units] == ["アリス", None, "ボブ"]
        assert units[0].extracted_text == "泣くな早く言って"

    def test_translation_batch_lqa_passed(self, pipeline):
        units = pipeline.project.units
        assert all(u.status is TranslationStatus.LQA_PASSED for u in units)
        assert [u.translated_text for u in units] == [
            "别哭呀快点说",
            "风很大的一天",
            "「走吧」他低声说",
        ]

    def test_output_carries_utf8_bom(self, pipeline):
        assert pipeline.output.read_bytes().startswith(b"\xef\xbb\xbf")

    def test_skeleton_lines_verbatim(self, pipeline):
        lines = pipeline.lines
        assert lines[0] == "; 章节头注释 —— 骨架保真断言对象"
        assert lines[1] == "*chapter1_start"  # 跳转标签逐字保真
        assert lines[2] == "@bg storage=bg01 time=1000"
        assert lines[6] == "; 场景尾部注释"

    def test_speakers_and_translations_backfilled(self, pipeline):
        # [r] 与 [ruby] 在原文中紧密相邻（泣くな | [r][ruby] | 早く言って），
        # 换算到正文坐标系后锚点同为 3，必须连续复写于同一位置。
        # 早期实现漏扣「排在该宏之前的宏长度」，会把 [ruby] 冲到行尾，
        # 使注音失去挂载对象 —— 故此处断言的是**相邻性**而非宽松的包含。
        assert pipeline.lines[3] == "【アリス】别哭呀[r][ruby text=\"はやく\"]快点说"
        assert pipeline.lines[4] == "风很大的一天[p]"
        assert pipeline.lines[5] == "【ボブ】「走吧」他[r]低声说"

    def test_inline_macros_preserved_in_place(self, pipeline):
        assert "[r]" in pipeline.lines[3] and '[ruby text="はやく"]' in pipeline.lines[3]
        # 相邻性断言：两个相邻宏在原文中零间隔，复写后必须仍零间隔（次序不得被冲散）
        assert '[r][ruby text="はやく"]' in pipeline.lines[3]
        assert "[p]" in pipeline.lines[4]
        assert "[r]" in pipeline.lines[5]
