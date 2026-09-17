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
import re
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from pydantic import ValidationError

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
    """固定回复 Mock：注入罐头译文内容并记录最近一次载荷。"""

    def __init__(self, config: TranslationConfig, content: str):
        super().__init__(config)
        self._content = content
        self.calls = 0
        self.last_payload: dict | None = None

    def _post(self, payload: str) -> str:
        self.calls += 1
        self.last_payload = json.loads(payload)
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
    # retry_backoff_seconds=0：单测零等待（退避数值由 TestRateLimitBackoff 专项断言）
    return TranslationConfig(
        api_base="http://mock.local/v1", model_name="glm-test", retry_backoff_seconds=0
    )


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
                api_base="http://mock.local/v1",
                model_name="glm-test",
                max_retries=1,
                retry_backoff_seconds=0,
            )
        )
        with pytest.raises(TranslationAPIError):
            exhausted.translate_batch([_unit("r-2", "試験")])
        assert exhausted.calls == 2  # 1 次首发 + 1 次重试，随后判死


class RateLimitedTranslator(GalgameTranslator):
    """按脚本依次抛 HTTPError，之后回正常响应；记录每次收到的退避指令。"""

    def __init__(self, config: TranslationConfig, codes: list[int]):
        super().__init__(config)
        self._codes = list(codes)
        self.calls = 0
        self.sleeps: list[float] = []

    def _post(self, payload: str) -> str:
        self.calls += 1
        if self._codes:
            code = self._codes.pop(0)
            headers = {"Retry-After": "7"} if code == 429 and self.retry_after else None
            raise urllib.error.HTTPError(
                "http://mock.local/v1", code, "transient", headers, None
            )
        return json.dumps({"choices": [{"message": {"content": "1. 好"}}]})

    retry_after = False


class TestRateLimitBackoff:
    """429/5xx 瞬态感知与指数退避（历史缺陷：HTTPError 一击即毙）。"""

    @pytest.fixture(autouse=True)
    def _deterministic_sleep(self, monkeypatch):
        """捕获 sleep 调用并冻结抖动，让退避数值可精确断言。"""
        import core.pipeline.translator as translator_module

        recorded: list[float] = []
        monkeypatch.setattr(
            translator_module.time, "sleep", lambda seconds: recorded.append(seconds)
        )
        monkeypatch.setattr(translator_module.random, "uniform", lambda low, high: 0.0)
        self.recorded = recorded

    def _fast_config(self, **overrides) -> TranslationConfig:
        return TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            retry_backoff_seconds=1.0,
            **overrides,
        )

    def test_429_retried_with_exponential_backoff_then_success(self):
        flaky = RateLimitedTranslator(self._fast_config(), [429, 429])
        flaky.translate_batch([_unit("b-1", "試験")])

        assert flaky.calls == 3  # 首发 + 2 次退避重试
        assert flaky._codes == []
        assert self.recorded == [1.0, 2.0], "退避序列必须是 1s → 2s 的指数增长"

    def test_429_honors_retry_after_header(self):
        translator = RateLimitedTranslator(self._fast_config(max_retries=1), [429])
        translator.retry_after = True

        translator.translate_batch([_unit("b-2", "試験")])

        assert self.recorded == [7.0], "Retry-After=7 必须压过退避基值 1s"

    def test_5xx_is_also_treated_as_transient(self):
        flaky = RateLimitedTranslator(self._fast_config(), [503, 500])
        flaky.translate_batch([_unit("b-3", "試験")])
        assert flaky.calls == 3
        assert self.recorded == [1.0, 2.0]

    def test_non_retryable_http_error_dies_immediately(self):
        translator = RateLimitedTranslator(self._fast_config(max_retries=5), [400])
        with pytest.raises(TranslationAPIError, match="HTTP 400"):
            translator.translate_batch([_unit("b-4", "試験")])
        assert translator.calls == 1, "4xx（除 429）重试只是白烧配额"
        assert self.recorded == []

    def test_429_exhausted_retries_raises(self):
        translator = RateLimitedTranslator(self._fast_config(max_retries=2), [429, 429, 429])
        with pytest.raises(TranslationAPIError, match="重试 3 次后仍失败"):
            translator.translate_batch([_unit("b-5", "試験")])
        assert translator.calls == 3
        assert self.recorded == [1.0, 2.0]


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


class TestReasoningEffortPayload:
    """reasoning_effort 载荷注入契约（DeepSeek-R1 / o 系列 / GLM 推理模型适配）。"""

    def test_injected_into_payload_when_set(self):
        cfg = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            reasoning_effort="high",
        )
        mock = MockTranslator(cfg, "1. 好")
        mock.translate_batch([_unit("re-1", "テスト")])
        assert mock.last_payload is not None
        assert mock.last_payload["reasoning_effort"] == "high"

    def test_omitted_from_payload_when_none(self, config):
        mock = MockTranslator(config, "1. 好")
        mock.translate_batch([_unit("re-2", "テスト")])
        assert mock.last_payload is not None
        assert "reasoning_effort" not in mock.last_payload

    @pytest.mark.parametrize("value", ["low", "medium", "high", "max"])
    def test_allowed_values_accepted(self, value):
        cfg = TranslationConfig(
            api_base="http://mock.local/v1", model_name="glm-test", reasoning_effort=value
        )
        assert cfg.reasoning_effort == value

    def test_invalid_value_rejected_at_construction(self):
        with pytest.raises(ValidationError):
            TranslationConfig(
                api_base="http://mock.local/v1",
                model_name="glm-test",
                reasoning_effort="ultra",
            )


# ---------------------------------------------------------------------------
# 长程滑窗上下文（Sliding Window Context）
# ---------------------------------------------------------------------------

_CONTEXT_HEADER = "【前文背景参考（仅供理解语境，无需翻译）】"


class TestSlidingWindowContext:
    """前序已定稿对白注入 User Prompt 隔离块（首批无前文时零冗余）。"""

    def test_explicit_context_injected_as_isolated_block(self, config):
        """显式传入的已定稿前文 → User Prompt 顶部隔离块；System Prompt 不受污染。"""
        mock = MockTranslator(config, "1. 译文")
        units = [_unit("s-1", "本文")]

        mock.translate_batch(
            units,
            context=[
                {"speaker": "アリス", "text": "前文セリフ一"},
                {"text": "旁白行"},  # 无角色的叙述行
                {"speaker": "ミナ"},  # 缺 text → 跳过
                {"speaker": "X", "text": "   "},  # 纯空白 → 跳过
                "garbage",  # 非 dict → 跳过
                {"speaker": "ボブ", "text": "前文セリフ三"},
            ],
        )

        user = mock.last_payload["messages"][-1]["content"]
        system = mock.last_payload["messages"][0]["content"]
        assert user.startswith(_CONTEXT_HEADER)
        assert "严禁翻译、复述" in user, "隔离块必须显式禁止翻译/复述背景"
        assert "【アリス】前文セリフ一" in user
        assert "旁白行" in user and "【ミナ】" not in user  # 畸形项被静默跳过
        assert "前文セリフ三" in user
        assert user.split("\n\n")[-1].startswith("请翻译下列编号文本："), (
            "编号正文必须完整保留在隔离块之后"
        )
        assert "1. 本文" in user
        assert system == config.system_prompt, "上下文只进 User 角色，System 保持干净"

    def test_first_call_without_history_is_verbatim_baseline(self, config):
        """无任何前文 → User Prompt 逐字等同基线（零冗余，不追加空块）。"""
        mock = MockTranslator(config, "1. 译文")

        mock.translate_batch([_unit("s-2", "「だめ」")])

        user = mock.last_payload["messages"][-1]["content"]
        assert user == "请翻译下列编号文本：\n1. 「だめ」"
        assert "前文背景" not in user

    def test_intra_call_prior_finalized_units_become_context(self, config):
        """直调管线场景：批内先行定稿的单元自动成为后续子批的滑窗前文。"""
        chunked = TranslationConfig(
            api_base="http://mock.local/v1", model_name="glm-test", batch_size=1
        )
        spy = PayloadSpyTranslator(chunked, "1. 新訳")
        u0 = _unit("s-3", "「対象A」")
        u0.translated_text = "既存訳A"  # 抽取期已定稿（复译 / 人工审校过）
        u1 = _unit("s-4", "「対象B」")

        spy.translate_batch([u0, u1])

        assert len(spy.payloads) == 2, "batch_size=1 × 2 单元应产生 2 次往返"
        payload_a = next(p for p in spy.payloads if "対象A" in p["messages"][-1]["content"])
        payload_b = next(p for p in spy.payloads if "対象B" in p["messages"][-1]["content"])
        assert "前文背景" not in payload_a["messages"][-1]["content"], (
            "首个子批之前无任何已定稿前文，必须零冗余"
        )
        assert "既存訳A" in payload_b["messages"][-1]["content"], (
            "第二个子批必须把先行定稿的 对象A 译文作为滑窗前文注入"
        )

    def test_context_window_truncates_to_last_n(self, config):
        """前文超过 context_lines 时只保留最近 N 条（滑窗语义）。"""
        windowed = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            context_lines=2,
        )
        mock = MockTranslator(windowed, "1. 译文")
        history = [{"speaker": f"sp{i}", "text": f"前文{i}"} for i in range(4)]

        mock.translate_batch([_unit("s-5", "本文")], context=history)

        user = mock.last_payload["messages"][-1]["content"]
        assert "前文2" in user and "前文3" in user, "必须保留最近两条"
        assert "前文0" not in user and "前文1" not in user, "窗口外的旧前文必须被裁掉"

    def test_context_lines_zero_disables_window(self, config):
        """context_lines=0 显式关闭滑窗：即使给了前文也零注入。"""
        off = TranslationConfig(
            api_base="http://mock.local/v1", model_name="glm-test", context_lines=0
        )
        mock = MockTranslator(off, "1. 译文")

        mock.translate_batch([_unit("s-6", "本文")], context=[{"speaker": "A", "text": "前文"}])

        assert mock.last_payload["messages"][-1]["content"] == "请翻译下列编号文本：\n1. 本文"

    def test_context_is_snapshot_not_live_view(self, config):
        """上下文在计划期快照：并发下先完成子批的译文不得泄漏进后续子批载荷。"""
        chunked = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            batch_size=1,
            concurrency_limit=3,
        )
        spy = PayloadSpyTranslator(chunked, "1. 訳")
        units = [_unit("s-7a", "「甲」"), _unit("s-7b", "「乙」"), _unit("s-7c", "「丙」")]

        spy.translate_batch(units)

        assert len(spy.payloads) == 3
        for payload in spy.payloads:
            user = payload["messages"][-1]["content"]
            assert "【前文背景参考" not in user, (
                "三个单元都是抽取期新文（无已定稿前文）——任何子批都不得出现上下文块"
            )


class PayloadSpyTranslator(GalgameTranslator):
    """线程安全载荷记录器：按到达序收集每次调用的完整 payload。"""

    def __init__(self, config: TranslationConfig, content: str):
        super().__init__(config)
        self._content = content
        self.payloads: list[dict] = []

    def _post(self, payload: str) -> str:
        self.payloads.append(json.loads(payload))
        return json.dumps(
            {"choices": [{"message": {"content": self._content}}]}, ensure_ascii=False
        )


# ---------------------------------------------------------------------------
# 受控并发池（Concurrency Pool）
# ---------------------------------------------------------------------------

_TEXT_A = "「あ」"
_TEXT_B = "「い」"
_TEXT_C = "「う」"
_TEXT_D = "「え」"


class ConcurrentMockTranslator(GalgameTranslator):
    """线程安全 Mock：按原文罐头应答，支持前置钩子 / 错误脚本 / 完成序记录。

    * ``mapping``：原文 → 罐头译文（键缺失 = 测试自身写错，KeyError 即红）；
    * ``before_reply(key)``：应答前钩子（测试用其实现阻塞与完成序编排）；
    * ``error_script``：``[(原文, http_code)]`` 队列——命中即抛 HTTPError
      一次，之后该原文恢复正常应答（模拟 429 单任务限流）。
    """

    def __init__(
        self,
        config: TranslationConfig,
        mapping: dict[str, str],
        before_reply=None,
        error_script: list[tuple[str, int]] | None = None,
    ):
        super().__init__(config)
        self._mapping = dict(mapping)
        self._before_reply = before_reply
        self._errors = list(error_script or [])
        self._lock = threading.Lock()
        self.payloads: list[str] = []
        self.completion_order: list[str] = []

    def _post(self, payload: str) -> str:
        user = json.loads(payload)["messages"][-1]["content"]
        with self._lock:
            self.payloads.append(user)
        entries = {
            int(m.group(1)): m.group(2)
            for line in user.splitlines()
            if (m := re.match(r"^(\d+)\.\s+(.*)$", line))
        }
        key = next(iter(entries.values()))
        with self._lock:
            hit = next(((t, c) for t, c in self._errors if t == key), None)
            if hit is not None:
                self._errors.remove(hit)
        if hit is not None:
            raise urllib.error.HTTPError(
                "http://mock.local/v1", hit[1], "transient", None, None
            )
        if self._before_reply is not None:
            self._before_reply(key)
        with self._lock:
            self.completion_order.append(key)
        reply = "\n".join(f"{n}. {self._mapping[t]}" for n, t in sorted(entries.items()))
        return json.dumps(
            {"choices": [{"message": {"content": reply}}]}, ensure_ascii=False
        )


class TestConcurrencyPool:
    """受控并发：乱序完成保序、并发度有界、429 退避不阻塞健康任务。"""

    def _config(self, **overrides) -> TranslationConfig:
        base = dict(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            batch_size=1,
            retry_backoff_seconds=1.0,
        )
        base.update(overrides)
        return TranslationConfig(**base)

    def test_out_of_order_completion_preserves_row_alignment(self, config):
        """并发发包、A 最慢完成：回填必须按全局索引对齐，不许串行错位。"""
        release_a = threading.Event()
        order: list[str] = []
        lock = threading.Lock()

        def before_reply(key: str) -> None:
            if key == _TEXT_A:
                assert release_a.wait(5), (
                    "子批 A 迟迟未被放行 —— 其余子批没有并发先行完成，调度退化了串行"
                )
            with lock:
                order.append(key)
                if sum(1 for k in order if k != _TEXT_A) == 2:
                    release_a.set()

        mock = ConcurrentMockTranslator(
            self._config(concurrency_limit=3),
            {_TEXT_A: "訳A", _TEXT_B: "訳B", _TEXT_C: "訳C"},
            before_reply=before_reply,
        )
        u_a, u_b, u_c = _unit("c-A", _TEXT_A), _unit("c-B", _TEXT_B), _unit("c-C", _TEXT_C)

        mock.translate_batch([u_a, u_b, u_c])

        # 乱序完成：B/C 先落地，A 最后 —— 但各自的译文必须各归其位
        assert mock.completion_order[-1] == _TEXT_A
        assert set(mock.completion_order[:2]) == {_TEXT_B, _TEXT_C}
        assert u_a.translated_text == "訳A"
        assert u_b.translated_text == "訳B"
        assert u_c.translated_text == "訳C"
        assert u_a.status is TranslationStatus.LQA_PASSED

    def test_concurrency_is_bounded_by_limit(self, config):
        """并发度必须被 limit 精确钳制：不是串行（1），也不是无界（单元数）。"""
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}

        def before_reply(_key: str) -> None:
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.05)  # 拉开重叠窗口，让并发度可观测
            with lock:
                state["active"] -= 1

        mock = ConcurrentMockTranslator(
            self._config(concurrency_limit=2),
            {t: f"訳{i}" for i, t in enumerate((_TEXT_A, _TEXT_B, _TEXT_C, _TEXT_D))},
            before_reply=before_reply,
        )
        units = [_unit(f"p-{i}", t) for i, t in enumerate((_TEXT_A, _TEXT_B, _TEXT_C, _TEXT_D))]

        mock.translate_batch(units)

        assert state["max_active"] == 2, (
            f"4 子批 × limit=2 应观测到恰好 2 路并发，实际 {state['max_active']}"
        )
        assert [u.translated_text for u in units] == ["訳0", "訳1", "訳2", "訳3"]

    def test_429_backoff_does_not_block_healthy_tasks(self, monkeypatch):
        """并发下 429：单任务在自身线程退避，健康任务照常完成（不被阻塞）。"""
        import core.pipeline.translator as translator_module

        recorded: list[float] = []
        b_done = threading.Event()

        def fake_sleep(seconds: float) -> None:
            recorded.append(seconds)
            # A 在退避等待 —— 此时 B 必须已在并发执行并完成，否则本断言超时：
            # 若退避阻塞了整个批次（串行退化），这里 5s 内等不到 B 而直接红
            assert b_done.wait(5), (
                "A 退避期间 B 未能完成 —— 退避阻塞了健康任务，并发隔离失效"
            )

        monkeypatch.setattr(translator_module.time, "sleep", fake_sleep)
        monkeypatch.setattr(translator_module.random, "uniform", lambda low, high: 0.0)

        def before_reply(key: str) -> None:
            if key == _TEXT_B:
                b_done.set()

        mock = ConcurrentMockTranslator(
            self._config(concurrency_limit=2),
            {_TEXT_A: "訳A", _TEXT_B: "訳B"},
            before_reply=before_reply,
            error_script=[(_TEXT_A, 429)],
        )
        u_a, u_b = _unit("k-A", _TEXT_A), _unit("k-B", _TEXT_B)

        mock.translate_batch([u_a, u_b])

        assert recorded == [1.0], "仅 A 触发一次退避（1s 基值），B 全程零等待"
        assert u_a.translated_text == "訳A", "429 重试后 A 仍须正确回填"
        assert u_b.translated_text == "訳B"


# ---------------------------------------------------------------------------
# Token 真实计量（跨并发子批原子累加）
# ---------------------------------------------------------------------------

class UsageScriptedTranslator(GalgameTranslator):
    """按脚本逐次应答：usage 字典（或异常/None=无 usage 字段），线程安全。"""

    def __init__(self, config: TranslationConfig, script: list[dict | Exception | None]):
        super().__init__(config)
        self._script = list(script)
        self._lock = threading.Lock()
        self.calls = 0

    def _post(self, payload: str) -> str:
        with self._lock:
            self.calls += 1
            item = self._script.pop(0) if self._script else None
        if isinstance(item, Exception):
            raise item
        reply: dict = {"choices": [{"message": {"content": "1. 好"}}]}
        if item is not None:
            reply["usage"] = item
        return json.dumps(reply)


class TestTokenUsage:
    """真实 usage 提取 → 跨子批原子累加 → last_usage 回传（消灭伪仪表盘的下半场）。"""

    def test_usage_accumulates_across_concurrent_chunks(self):
        """3 子批并发收包：累计值必须等于各批之和（互斥锁保证原子性）。"""
        cfg = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            batch_size=1,
            concurrency_limit=3,
        )
        mock = UsageScriptedTranslator(cfg, [
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        ])
        units = [_unit(f"t-{i}", text) for i, text in enumerate(("「あ」", "「い」", "「う」"))]

        mock.translate_batch(units)

        assert mock.calls == 3
        assert mock.last_usage == {
            "prompt_tokens": 111,
            "completion_tokens": 57,
            "total_tokens": 168,
        }, "跨并发子批必须原子累加，不许丢账或串账"

    def test_retry_rounds_do_not_double_count(self):
        """429 重试的失败响应无 usage —— 计入点在成功收包处，天然不重复计费。"""
        cfg = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            max_retries=3,
            retry_backoff_seconds=0,
        )
        mock = UsageScriptedTranslator(cfg, [
            urllib.error.HTTPError("http://mock.local/v1", 429, "limit", None, None),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ])

        mock.translate_batch([_unit("t-r", "試験")])

        assert mock.calls == 2  # 首发 429 + 一次退避重试成功
        assert mock.last_usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_missing_usage_counts_zero(self, config):
        """本地 Mock 常不带 usage：缺失按零贡献，绝不伪造数字。"""
        mock = UsageScriptedTranslator(config, [None])
        mock.translate_batch([_unit("t-n", "試験")])
        assert mock.last_usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def test_usage_resets_between_calls(self, config):
        """直调复用同一 translator：last_usage 语义是「本次调用」，不得带残留。"""
        mock = UsageScriptedTranslator(config, [
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        ])
        mock.translate_batch([_unit("t-1", "一")])
        mock.translate_batch([_unit("t-2", "二")])
        assert mock.last_usage == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    def test_malformed_usage_values_clamp_to_zero(self, config):
        """负数 / 非法类型一律按 0 钳制 —— 计量可以缺失，不可以是错的。"""
        mock = UsageScriptedTranslator(config, [
            {"prompt_tokens": -5, "completion_tokens": "abc", "total_tokens": None},
        ])
        mock.translate_batch([_unit("t-m", "試験")])
        assert mock.last_usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


# ---------------------------------------------------------------------------
# Token 真实计量（跨并发子批原子累加）
# ---------------------------------------------------------------------------

class UsageScriptedTranslator(GalgameTranslator):
    """按脚本逐次应答：usage 字典（或异常/None=无 usage 字段），线程安全。"""

    def __init__(self, config: TranslationConfig, script: list[dict | Exception | None]):
        super().__init__(config)
        self._script = list(script)
        self._lock = threading.Lock()
        self.calls = 0

    def _post(self, payload: str) -> str:
        with self._lock:
            self.calls += 1
            item = self._script.pop(0) if self._script else None
        if isinstance(item, Exception):
            raise item
        reply: dict = {"choices": [{"message": {"content": "1. 好"}}]}
        if item is not None:
            reply["usage"] = item
        return json.dumps(reply)


class TestTokenUsage:
    """真实 usage 提取 → 跨子批原子累加 → last_usage 回传（消灭伪仪表盘的下半场）。"""

    def test_usage_accumulates_across_concurrent_chunks(self):
        """3 子批并发收包：累计值必须等于各批之和（互斥锁保证原子性）。"""
        cfg = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            batch_size=1,
            concurrency_limit=3,
        )
        mock = UsageScriptedTranslator(cfg, [
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        ])
        units = [_unit(f"t-{i}", text) for i, text in enumerate(("「あ」", "「い」", "「う」"))]

        mock.translate_batch(units)

        assert mock.calls == 3
        assert mock.last_usage == {
            "prompt_tokens": 111,
            "completion_tokens": 57,
            "total_tokens": 168,
        }, "跨并发子批必须原子累加，不许丢账或串账"

    def test_retry_rounds_do_not_double_count(self):
        """429 重试的失败响应无 usage —— 计入点在成功收包处，天然不重复计费。"""
        cfg = TranslationConfig(
            api_base="http://mock.local/v1",
            model_name="glm-test",
            max_retries=3,
            retry_backoff_seconds=0,
        )
        mock = UsageScriptedTranslator(cfg, [
            urllib.error.HTTPError("http://mock.local/v1", 429, "limit", None, None),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ])

        mock.translate_batch([_unit("t-r", "試験")])

        assert mock.calls == 2  # 首发 429 + 一次退避重试成功
        assert mock.last_usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    def test_missing_usage_counts_zero(self, config):
        """本地 Mock 常不带 usage：缺失按零贡献，绝不伪造数字。"""
        mock = UsageScriptedTranslator(config, [None])
        mock.translate_batch([_unit("t-n", "試験")])
        assert mock.last_usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def test_usage_resets_between_calls(self, config):
        """直调复用同一 translator：last_usage 语义是「本次调用」，不得带残留。"""
        mock = UsageScriptedTranslator(config, [
            {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        ])
        mock.translate_batch([_unit("t-1", "一")])
        mock.translate_batch([_unit("t-2", "二")])
        assert mock.last_usage == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    def test_malformed_usage_values_clamp_to_zero(self, config):
        """负数 / 非法类型一律按 0 钳制 —— 计量可以缺失，不可以是错的。"""
        mock = UsageScriptedTranslator(config, [
            {"prompt_tokens": -5, "completion_tokens": "abc", "total_tokens": None},
        ])
        mock.translate_batch([_unit("t-m", "試験")])
        assert mock.last_usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
