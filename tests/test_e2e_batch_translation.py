"""批量翻译调度闭环的端到端集成测试（走真实 JSON-RPC 帧 + 环回 LLM 桩）。

与 ``test_e2e_pipeline.py`` / ``test_e2e_rpc_roundtrip.py`` 的分工：

* ``test_e2e_pipeline.py``：进程内直调适配器与翻译器，验证**回写保真**；
* ``test_e2e_rpc_roundtrip.py``：走 RPC 帧，验证**加载 → 审校 → 导出**往返无损；
* 本文件：走 RPC 帧 + 真实 HTTP 回环端点，验证**批量翻译这一步本身**
  —— 空译文批次推入 → 批次返回 → 译文填入 → LQA 自动校验 → 待译计数衰减。

链路::

    8 个 EXTRACTED 单元（译文全空）
        ──translate_batch(整批)──▶ 7 LQA_PASSED + 1 LQA_FAILED（刻意破坏的引号）
        ──run_lqa(复审)──────────▶ 状态与 metadata 必须与内联门禁**逐条一致**
        ──translate_batch(分片)──▶ 各片只回传自己的单元，id 键对齐
        ──translate_batch(修复后)─▶ 失败行转绿，且**陈旧 lqa_issues 必须被清除**

最后一条是本文件的重点：``translator._apply`` 曾经在「本轮无违例」时不清理
``metadata["lqa_issues"]``，导致上一轮失败留下的 error 条目长期挂在绿色的
已通过胶囊上（壳层 IssueTooltip 会把红色错误气泡画在绿底上）。
"""

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.server.rpc import build_default_dispatcher

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample_act1.ks"

# 源文（剥离宏后的正文）→ 罐头译文。其中「拉钩上吊」一条**刻意缺失闭直角引号**，
# 作为 LQA 门禁的拦截靶子（与 tests/fixtures/mock_llm_server.py 同一设计）。
GOOD_TRANSLATIONS = {
    "「真実を知る覚悟は、もうできているの？」": "「知道真相的觉悟，早就已经做好了吧？」",
    "思い出すのは、あの雨の夜のことだ。": "我回想起的，是那个雨夜的事。",
    "傘を折られた私は、濡れたまま家の扉の前に立っていた。": "被打折了伞的我，就那样浑身湿透地站在家门之前。",
    "「あの時、千代は何も言わなかった。」": "「那个时候，千代什么也没有说。」",
    "「ただ、誠を貫くことだけを選んだ。」": "「只是选择了贯彻诚实这一条路。」",
    "波の音だけが、二人の間を流れていた。": "只有波浪的声音，在两人之间流淌着。",
    "「……その約束、今でも守っている。」": "「……那个约定，我至今仍然守着。」",
}
DAMAGED_SOURCE = "「指切りげんまん、嘘ついたら針千本飲ます。」"
DAMAGED_REPLY = "「拉钩上吊，说谎的话就吞一千根针。"  # 缺闭引号」→ cjk_punctuation error
REPAIRED_REPLY = "「拉钩上吊，说谎的话就吞一千根针。」"

ALL_TRANSLATIONS = {**GOOD_TRANSLATIONS, DAMAGED_SOURCE: REPAIRED_REPLY}
"""8 条源文全部有规范译文的完整映射（未破坏引号），用于切片与全绿路径。"""

_PROMPT_LINE = re.compile(r"^\s*(\d+)\s*[.、:：]\s*(?:【[^】]*】)?(.*)$")


class StubLlm:
    """127.0.0.1 环回 OpenAI 兼容 /v1/chat/completions 桩。

    解析批次提示词里的编号行，按「正文 → 译文」映射逐条应答；
    ``omit`` 中的编号刻意不回，用于模拟批次协议失败（响应缺编号）。
    记录实际收到的请求数，供断言客户端切片行为。

    每个成功响应固定携带 ``USAGE``（prompt 12 / completion 34 / total 46），
    供 Token 计量链路的端到端断言。
    """

    USAGE = {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}

    def __init__(self, translations: dict[str, str], omit: set[str] | None = None) -> None:
        self.translations = translations
        self.omit = omit or set()
        self.requests = 0
        self.last_prompt = ""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.rstrip("/") != "/v1/chat/completions":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                prompt = payload["messages"][-1]["content"]
                outer.requests += 1
                outer.last_prompt = prompt
                self._reply(200, json.dumps(
                    {
                        "choices": [{"message": {"content": outer.compose(prompt)}}],
                        "usage": dict(outer.USAGE),
                    },
                    ensure_ascii=False,
                ))

            def _reply(self, code: int, text: str) -> None:
                body = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def compose(self, prompt: str) -> str:
        """按编号行组装「编号. 译文」应答，跳过 omit 中的正文。"""
        lines: list[str] = []
        for raw in prompt.splitlines():
            match = _PROMPT_LINE.match(raw)
            if not match:
                continue
            number, body = match.group(1), match.group(2).strip()
            if body in self.omit or body not in self.translations:
                continue
            lines.append(f"{number}. {self.translations[body]}")
        return "\n".join(lines)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class Rpc:
    """把请求序列化成真实 JSON-RPC 行帧并解析响应。"""

    def __init__(self) -> None:
        self.dispatcher = build_default_dispatcher()
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}},
            ensure_ascii=False,
        )
        return json.loads(self.dispatcher.handle_request(frame))

    def ok(self, method: str, params: dict | None = None):
        response = self.call(method, params)
        assert "error" not in response, f"{method} 失败：{response.get('error')}"
        return response["result"]


def translate_ok(rpc: Rpc, units: list[dict], cfg: dict) -> list[dict]:
    """translate_batch 的 units 通道便捷封装（usage 断言由专项测试承载）。"""
    return rpc.ok("translate_batch", {"units": units, "config": cfg})["units"]


def pending_count(units: list[dict]) -> int:
    """Python 侧的待译计数镜像：EXTRACTED 且译文空白（与 C# 统计层同口径）。

    刻意在此独立重述一遍 —— 若两端口径漂移，这两处计数就会对不上，
    而壳层按钮角标正是照它显示的。
    """
    return sum(
        1 for u in units
        if u["status"] == "EXTRACTED" and not (u.get("translated_text") or "").strip()
    )


@pytest.fixture(scope="module")
def extracted() -> list[dict]:
    """真实样本抽取出的 8 个单元（全 EXTRACTED、译文全空）。"""
    return Rpc().ok("extract_to_ir", {"file_path": str(SAMPLE)})["project"]["units"]


def config(stub: StubLlm, batch_size: int | None = None) -> dict:
    payload = {"api_base": stub.api_base, "model_name": "stub-llm", "timeout_seconds": 10.0}
    if batch_size is not None:
        payload["batch_size"] = batch_size
    return payload


def fresh(units: list[dict]) -> list[dict]:
    """深拷贝单元并清空译文，恢复到「刚抽取」的待译状态。"""
    clone = json.loads(json.dumps(units))
    for unit in clone:
        unit["translated_text"] = None
        unit["status"] = "EXTRACTED"
        unit["metadata"].pop("lqa_issues", None)
    return clone


# ---------------------------------------------------------------------------
# 1. 前置态：批次推入前必须是一整批空译文
# ---------------------------------------------------------------------------


class TestPrecondition:
    def test_extracted_script_starts_fully_untranslated(self, extracted):
        assert len(extracted) == 8
        assert [u["status"] for u in extracted] == ["EXTRACTED"] * 8
        assert [u["translated_text"] for u in extracted] == [None] * 8
        assert pending_count(extracted) == 8, "初始待译数必须等于总行数"


# ---------------------------------------------------------------------------
# 2. 批次推入 → 译文填入 → LQA 自动校验
# ---------------------------------------------------------------------------


class TestBatchTranslationGate:
    def test_batch_fills_translations_and_runs_lqa(self, extracted):
        units = fresh(extracted)
        stub = StubLlm({**GOOD_TRANSLATIONS, DAMAGED_SOURCE: DAMAGED_REPLY})
        try:
            result = translate_ok(Rpc(), units, config(stub))
        finally:
            stub.stop()

        by_id = {u["id"]: u for u in result}
        assert [u["id"] for u in result] == [u["id"] for u in units], "返回顺序必须与请求一致"

        # 8 条全部拿回译文（没有露空的编号）
        assert all((u.get("translated_text") or "").strip() for u in result)

        # 刻意的引号破坏必须被内联门禁拦下：7 通过 / 1 不合格
        damaged = by_id["sample_act1-00027"]
        assert damaged["status"] == "LQA_FAILED"
        assert damaged["translated_text"] == DAMAGED_REPLY
        assert [u["status"] for u in result].count("LQA_PASSED") == 7

        issues = damaged["metadata"]["lqa_issues"]
        assert any(
            i["rule_id"] == "cjk_punctuation" and i["severity"] == "error" for i in issues
        ), "未配平的直角引号必须被判为 cjk_punctuation error"

    def test_passing_units_carry_no_issue_metadata(self, extracted):
        units = fresh(extracted)
        stub = StubLlm({**GOOD_TRANSLATIONS, DAMAGED_SOURCE: DAMAGED_REPLY})
        try:
            result = translate_ok(Rpc(), units, config(stub))
        finally:
            stub.stop()

        offenders = [u["id"] for u in result if u["status"] == "LQA_PASSED" and "lqa_issues" in u["metadata"]]
        assert not offenders, f"已通过单元残留违例留痕（前台会画红色错误气泡）：{offenders}"

    def test_pending_count_decays_to_zero_after_batch(self, extracted):
        units = fresh(extracted)
        assert pending_count(units) == 8

        stub = StubLlm({**GOOD_TRANSLATIONS, DAMAGED_SOURCE: DAMAGED_REPLY})
        try:
            result = translate_ok(Rpc(), units, config(stub))
        finally:
            stub.stop()

        # LQA_FAILED 携带译文 → 不计入待译；故批次后待译必然归零
        assert pending_count(result) == 0
        # 往返不变量：待译 + 已译 = 总数
        assert pending_count(result) + sum(
            1 for u in result if (u.get("translated_text") or "").strip()
        ) == len(result)


# ---------------------------------------------------------------------------
# 3. 复审一致性：内联门禁 ⇄ 独立 run_lqa 必须逐条吻合
# ---------------------------------------------------------------------------


class TestRecheckAgreement:
    def test_standalone_run_lqa_agrees_with_inline_gate(self, extracted):
        """壳层在批次后立刻调 run_lqa 复审；两个门禁若不一致，必有其一失效。"""
        units = fresh(extracted)
        stub = StubLlm({**GOOD_TRANSLATIONS, DAMAGED_SOURCE: DAMAGED_REPLY})
        try:
            batch = translate_ok(Rpc(), units, config(stub))
        finally:
            stub.stop()

        rechecked = {u["id"]: u for u in Rpc().ok("run_lqa", {"units": batch})}

        for unit in batch:
            after = rechecked[unit["id"]]
            assert after["status"] == unit["status"], f"{unit['id']} 两处门禁的结论不一致"
            assert ("lqa_issues" in after["metadata"]) == ("lqa_issues" in unit["metadata"]), (
                f"{unit['id']} 两处门禁的留痕不一致"
            )

    def test_repair_then_retranslate_clears_stale_issue_metadata(self, extracted):
        """回归：修复后重翻必须清掉上一轮的 error 留痕，否则绿胶囊上会挂红气泡。

        ``translator._apply`` 曾经只在「本轮有违例」时写 ``lqa_issues``，
        本轮无违例时既不写也不清 —— 陈旧条目会一直留在 metadata 里。
        """
        units = fresh(extracted)
        damaged_index = next(
            i for i, u in enumerate(units) if u["extracted_text"] == DAMAGED_SOURCE
        )

        # 第一轮：故意答错，制造一次失败留痕
        broken = StubLlm({**GOOD_TRANSLATIONS, DAMAGED_SOURCE: DAMAGED_REPLY})
        try:
            after_first = translate_ok(Rpc(), units, config(broken))
        finally:
            broken.stop()

        failed = after_first[damaged_index]
        assert failed["status"] == "LQA_FAILED"
        assert "lqa_issues" in failed["metadata"], "前置条件未成立：第一轮应留下违例"

        # 第二轮：模型答对，重翻同一单元
        repaired = StubLlm({**GOOD_TRANSLATIONS, DAMAGED_SOURCE: REPAIRED_REPLY})
        try:
            after_second = Rpc().ok(
                "translate_batch", {"units": [failed], "config": config(repaired)}
            )
        finally:
            repaired.stop()

        fixed = after_second["units"][0]
        assert fixed["status"] == "LQA_PASSED"
        assert fixed["translated_text"] == REPAIRED_REPLY
        assert "lqa_issues" not in fixed["metadata"], (
            "修复后仍残留违例留痕 —— 壳层会把红色错误气泡画在绿色的已通过胶囊上"
        )


# ---------------------------------------------------------------------------
# 4. 切片调度：客户端按片往返，每片只回传自己的单元
# ---------------------------------------------------------------------------


class TestChunkedDispatch:
    def test_chunks_are_independent_and_id_keyed(self, extracted):
        """壳层的分批循环依赖：每次 RPC 只处理收到的单元，且 id 原样回传。"""
        units = fresh(extracted)
        stub = StubLlm(ALL_TRANSLATIONS)
        rpc = Rpc()
        try:
            collected: list[dict] = []
            usage_totals: list[dict] = []
            for start in range(0, len(units), 3):
                chunk = units[start:start + 3]
                part = rpc.ok(
                    "translate_batch",
                    {"units": chunk, "config": config(stub, batch_size=len(chunk))},
                )
                assert [u["id"] for u in part["units"]] == [u["id"] for u in chunk], (
                    "分片返回必须与请求片逐条对齐（回填靠 id 定位）"
                )
                collected.extend(part["units"])
                usage_totals.append(part["usage"])
        finally:
            stub.stop()

        assert stub.requests == 3, f"3 片应产生 3 次 HTTP 往返，实际 {stub.requests} 次"
        assert [u["id"] for u in collected] == [u["id"] for u in units]
        assert all(u["status"] == "LQA_PASSED" for u in collected)
        assert pending_count(collected) == 0
        # 桩的每个成功响应固定携带 usage 46 → 3 片成功请求应累计 138
        assert stub.requests == 3 and usage_totals, "每片都应回传 usage 统计"
        assert all(u["total_tokens"] == StubLlm.USAGE["total_tokens"] for u in usage_totals)

    def test_empty_chunk_returns_empty_without_http_roundtrip(self):
        """0 单元的批次是合法但空洞的请求：不得产生任何 HTTP 往返。"""
        stub = StubLlm(ALL_TRANSLATIONS)
        try:
            response = Rpc().call("translate_batch", {"units": [], "config": config(stub)})
        finally:
            stub.stop()

        assert response["result"]["units"] == []
        assert response["result"]["usage"]["total_tokens"] == 0
        assert stub.requests == 0


# ---------------------------------------------------------------------------
# 5. 批次协议失败：响应缺编号
# ---------------------------------------------------------------------------


class TestProtocolFailure:
    def test_missing_number_marks_protocol_failure_without_fake_text(self, extracted):
        """模型漏答某条时，该单元必须被判失败且**不得**填入任何伪译文。"""
        units = fresh(extracted)
        stub = StubLlm(GOOD_TRANSLATIONS, omit={DAMAGED_SOURCE})
        try:
            result = translate_ok(Rpc(), units, config(stub))
        finally:
            stub.stop()

        victim = next(u for u in result if u["extracted_text"] == DAMAGED_SOURCE)
        assert victim["status"] == "LQA_FAILED"
        assert not (victim.get("translated_text") or "").strip(), "缺失编号不得被填入空串以外的内容"
        issues = victim["metadata"]["lqa_issues"]
        assert any(i["rule_id"] == "translator_protocol" and i["severity"] == "error" for i in issues)
        assert len(result) == len(units), "协议失败也必须回传全部单元以维持 id 对齐"

    def test_run_lqa_downgrades_protocol_failure_to_extracted(self, extracted):
        """这条差异正是壳层「只对已获译文的行送 run_lqa」过滤规则的依据。

        无译文单元若一并送 run_lqa，会被规整成 EXTRACTED（待译态），
        从而洗掉「批次协议失败」这个有效诊断 —— 故壳层刻意跳过它们。
        """
        units = fresh(extracted)
        stub = StubLlm(GOOD_TRANSLATIONS, omit={DAMAGED_SOURCE})
        try:
            batch = translate_ok(Rpc(), units, config(stub))
        finally:
            stub.stop()

        victim = next(u for u in batch if u["extracted_text"] == DAMAGED_SOURCE)
        assert victim["status"] == "LQA_FAILED"

        rechecked = Rpc().ok("run_lqa", {"units": [victim]})[0]
        assert rechecked["status"] == "EXTRACTED"
        assert "lqa_issues" not in rechecked["metadata"]
