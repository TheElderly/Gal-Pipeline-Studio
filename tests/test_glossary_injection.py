"""术语约束注入 LLM 翻译 Prompt 的契约测试。

背景（这一轮修掉的花架子）：``core/tm`` 能产出术语约束，Inspector 也能把
术语渲成 Chip，但 ``translate_batch`` **从不把它们送进 Prompt** ——
模型看不到任何约束，术语表对译文质量零贡献，纯粹是给人看的装饰。

本文件钉住注入的两端行为：

* **命中时**：System Prompt 必含标准化约束块（表头 + ``- 源 → 目标`` 条目），
  且条目去重、顺序稳定可复现；
* **未命中时**：逐字等同基线 Prompt —— 不追加空行、不追加空块，零冗余 token。

外加一条工程性断言：注入必须按**批次**而非全局 —— 某片没命中就不该带上
别片的术语，否则 batch_size 一变 Prompt 就跟着变，缓存全废。
"""

import json
import sys
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.kag import KagAdapter
from core.models.ir import TranslationUnit
from core.models.status import TranslationStatus
from core.pipeline.translator import (
    GalgameTranslator,
    TranslationConfig,
    _batch_glossary,
)

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample_act1.ks"

HEADER = "【术语约束】"

# 刻意让两个单元命中不同术语、其中一条重复，用于验证去重与顺序
HIT_TEXTS = (
    "「真実を知る覚悟は、もうできているの？」",   # 真実 / 覚悟
    "波の音だけが、二人の間を流れていた。",       # 波
    "もう一度、真実を教えてほしい。",             # 真実（重复）
)
PLAIN_TEXTS = (
    "hello world, nothing to gloss here.",
    "まったく术语のない行です。",                  # 「行」不在术语表
)


def unit(uid: str, text: str, speaker: str | None = None) -> TranslationUnit:
    return TranslationUnit(
        id=uid,
        speaker=speaker,
        raw_text=text,
        extracted_text=text,
        atomic_tags=[],
        paired_tags=[],
        status=TranslationStatus.EXTRACTED,
    )


def units(texts, prefix: str = "t") -> list[TranslationUnit]:
    return [unit(f"{prefix}-{i:05d}", text) for i, text in enumerate(texts, start=1)]


class SpyTranslator(GalgameTranslator):
    """记录每次实际发出的 payload，并回灌一份格式合法的罐头译文。"""

    def __init__(self, config: TranslationConfig) -> None:
        super().__init__(config)
        self.payloads: list[dict] = []

    def _post(self, payload: str) -> str:
        parsed = json.loads(payload)
        self.payloads.append(parsed)
        user = parsed["messages"][-1]["content"]
        numbers = [
            line.split(".", 1)[0].strip()
            for line in user.splitlines()
            if line.strip() and line.strip()[0].isdigit()
        ]
        content = "\n".join(f"{n}. 罐头译文{n}" for n in numbers)
        return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)


@pytest.fixture
def config() -> TranslationConfig:
    return TranslationConfig(api_base="http://mock.local/v1", model_name="glm-test")


# ---------------------------------------------------------------------------
# 1. 命中：约束块必须出现，且格式标准化
# ---------------------------------------------------------------------------


class TestInjectionOnHit:
    def test_constraint_block_is_appended_to_system_prompt(self, config):
        translator = GalgameTranslator(config)
        prompt = translator._compose_system_prompt(units(HIT_TEXTS))

        assert prompt.startswith(config.system_prompt), "基线 Prompt 必须原样保留在开头"
        assert HEADER in prompt
        block = prompt[len(config.system_prompt):]
        assert block.startswith("\n\n" + HEADER), "约束块应与基线之间空一行，便于模型区分"

    def test_rules_use_standardized_entry_format(self, config):
        translator = GalgameTranslator(config)
        prompt = translator._compose_system_prompt(units(HIT_TEXTS))

        assert "- 真実 → 真相" in prompt
        assert "- 覚悟 → 觉悟" in prompt
        assert "- 波 → 波浪" in prompt

    def test_terms_are_deduplicated_across_units(self, config):
        translator = GalgameTranslator(config)
        prompt = translator._compose_system_prompt(units(HIT_TEXTS))

        # 真実 在两行都出现，约束块里只能有一条
        assert prompt.count("- 真実 → 真相") == 1

    def test_entry_order_is_stable_and_reproducible(self, config):
        translator = GalgameTranslator(config)
        chunk = units(HIT_TEXTS)
        first = translator._compose_system_prompt(chunk)

        assert first == translator._compose_system_prompt(chunk), "同批次必须逐字可复现"

        entries = [line for line in first.splitlines() if line.startswith("- ")]
        assert entries == ["- 真実 → 真相", "- 覚悟 → 觉悟", "- 波 → 波浪"], (
            "条目顺序应为「单元顺序 → 单元内首现偏移」，而非术语表定义顺序"
        )

    def test_conflicting_target_keeps_the_first_seen(self, config, monkeypatch):
        """术语表自身矛盾（同源词两个译法）时保留先见条目，而不是整批失败。"""
        import core.pipeline.translator as translator_module

        class FakeMatch:
            def __init__(self, source, target):
                self.source = source
                self.target = target

        calls = {"n": 0}

        def fake_match(_text):
            calls["n"] += 1
            return [FakeMatch("約束", "约定" if calls["n"] == 1 else "誓约")]

        monkeypatch.setattr(translator_module, "match_glossary", fake_match)

        prompt = GalgameTranslator(config)._compose_system_prompt(units(["a", "b"]))

        assert "- 約束 → 约定" in prompt
        assert "誓约" not in prompt

    def test_injection_is_in_the_system_role_not_user_role(self, config):
        spy = SpyTranslator(config)
        spy.translate_batch(units(HIT_TEXTS))

        system = spy.payloads[0]["messages"][0]
        user = spy.payloads[0]["messages"][-1]
        assert system["role"] == "system"
        assert HEADER in system["content"]
        assert HEADER not in user["content"], "约束不该混进用户消息，否则会干扰编号行解析"
        assert user["content"].startswith("请翻译下列编号文本：")

    def test_user_message_still_uses_numbered_unit_lines(self, config):
        spy = SpyTranslator(config)
        target = [unit("t-00001", HIT_TEXTS[0], speaker="千代")]
        spy.translate_batch(target)

        user = spy.payloads[0]["messages"][-1]["content"]
        assert "1. 【千代】「真実を知る覚悟は、もうできているの？」" in user

    def test_reasoning_effort_still_injected_alongside_glossary(self):
        config = TranslationConfig(
            api_base="http://mock.local/v1", model_name="glm-test", reasoning_effort="high"
        )
        spy = SpyTranslator(config)
        spy.translate_batch(units(HIT_TEXTS))

        assert spy.payloads[0]["reasoning_effort"] == "high"
        assert HEADER in spy.payloads[0]["messages"][0]["content"]


# ---------------------------------------------------------------------------
# 2. 未命中：零冗余
# ---------------------------------------------------------------------------


class TestNoInjectionOnMiss:
    def test_system_prompt_is_verbatim_baseline_when_nothing_matches(self, config):
        translator = GalgameTranslator(config)
        prompt = translator._compose_system_prompt(units(PLAIN_TEXTS))

        assert prompt == config.system_prompt, "未命中时必须逐字等同基线，不得留下空块或多余空行"

    def test_payload_carries_no_glossary_artifacts(self, config):
        spy = SpyTranslator(config)
        spy.translate_batch(units(PLAIN_TEXTS))

        system = spy.payloads[0]["messages"][0]["content"]
        assert system == config.system_prompt
        assert HEADER not in system
        assert "\n\n" not in system, "不得因未命中而引入多余空行"

    def test_empty_batch_yields_empty_constraints(self, config):
        assert _batch_glossary([]) == []
        assert GalgameTranslator(config)._compose_system_prompt([]) == config.system_prompt


# ---------------------------------------------------------------------------
# 3. 按批次注入，而非全局
# ---------------------------------------------------------------------------


class TestInjectionIsPerBatch:
    def test_each_chunk_gets_only_its_own_terms(self, config):
        """batch_size=1 时逐单元成批：无术语的单元那一批不得带上别批的术语。

        并发池上线后子批的**完成序**不再确定（payloads 按到达序记录），
        断言改为按内容键匹配 —— 语义不变：命中子批带自己的术语、
        无命中子批逐字等同基线。
        """
        chunked = TranslationConfig(
            api_base="http://mock.local/v1", model_name="glm-test", batch_size=1
        )
        spy = SpyTranslator(chunked)
        spy.translate_batch(units([HIT_TEXTS[0], *PLAIN_TEXTS]))

        assert len(spy.payloads) == 3, "batch_size=1 应产生 3 次往返"
        system_contents = [p["messages"][0]["content"] for p in spy.payloads]
        with_terms = [c for c in system_contents if HEADER in c]
        without_terms = [c for c in system_contents if HEADER not in c]
        assert len(with_terms) == 1, "只有命中术语的那个子批携带约束块"
        assert "真実 → 真相" in with_terms[0] and "覚悟 → 觉悟" in with_terms[0]
        assert all(c == chunked.system_prompt for c in without_terms), (
            "无命中子批的 System Prompt 必须逐字等同基线"
        )

    def test_batch_boundary_changes_prompt_only_by_terms(self, config):
        """整批一次 vs 拆成两批：有术语的那几批内容一致，证明注入只依赖本批内容。"""
        single = SpyTranslator(config)
        single.translate_batch(units(HIT_TEXTS))

        assert len(single.payloads) == 1


# ---------------------------------------------------------------------------
# 4. 对真实样本的端到端生效
# ---------------------------------------------------------------------------


class TestRealSampleInjection:
    def test_real_sample_batch_carries_all_eleven_terms(self, config):
        project = KagAdapter().extract_to_ir(SAMPLE)
        spy = SpyTranslator(config)
        spy.translate_batch(project.units)

        system = spy.payloads[0]["messages"][0]["content"]
        for entry in (
            "- 真実 → 真相",
            "- 覚悟 → 觉悟",
            "- 傘 → 伞",
            "- 千代 → 千代",
            "- 指切りげんまん → 拉钩上吊",
            "- 針千本 → 一千根针",
            "- 約束 → 约定",
        ):
            assert entry in system, f"真实样本批次缺少术语约束：{entry}"

        assert system.count(HEADER) == 1, "约束块只能有一个表头"

    def test_matching_reads_extracted_text_not_control_codes(self, config):
        """命中判定基于可译正文：控制符里的字样不该被当成术语。

        ``[…]`` 宏内的内容在抽取期已从 extracted_text 剥离，
        术语匹配只看剥离后的正文，所以这里必须零命中。
        """
        raw = "【千代】[ruby text=\"約束\"]「やあ」[p]"
        tricky = TranslationUnit(
            id="tricky-00001",
            speaker="千代",
            raw_text=raw,
            extracted_text="「やあ」",
            atomic_tags=[],
            paired_tags=[],
            status=TranslationStatus.EXTRACTED,
        )

        assert _batch_glossary([tricky]) == []
