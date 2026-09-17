"""端到端集成测试 —— 真实 JSON-RPC 帧驱动的「加载 → 审校 → 导出」闭环。

与 ``test_e2e_pipeline.py`` 的区别（两者互补，不是重复）：

* 本文件**不经由 Python 函数直接调用适配器**，而是把请求序列化成真正的
  JSON-RPC 2.0 行帧喂给 ``RpcDispatcher.handle_request`` —— 覆盖壳层实际
  走的那条路（帧解析 → 参数绑定 → 异常映射 → 响应序列化）；
* 断言面包含**逐行逐字节的骨架无损性**与**往返幂等**，即回写产物必须能
  被适配器重新抽取回与原抽取完全一致的单元结构。

链路::

    tests/fixtures/sample_act1.ks（真实 KAG：BOM + CRLF + 注音宏 + 跳转标签）
        ──detect_format──▶ 认领 kag
        ──extract_to_ir──▶ 8 个 EXTRACTED 单元（宏登记为 AtomicTag）
        ──run_lqa────────▶ 人工译文即时质检（error 阻断 / warning 留痕）
        ──ir_to_asset────▶ 汉化 .ks（骨架逐行保真 + 宏逐字节复写 + BOM）
        ──extract_to_ir──▶ 幂等：id / 说话人 / 宏登记 / 正文集合全部一致
"""

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.server.rpc import build_default_dispatcher

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPO_ROOT / "tests" / "fixtures" / "sample_act1.ks"
"""内置真实 KAG 样本：覆盖注释 / 跳转标签 / bg 与 wait 命令 / 具名对话 /
旁白 / 注音宏 / 行内字号控制符 / 多行连续对话。"""

# 真实样本逐行抽取的期望结构（写死为常量而非运行时推导 —— 用被测代码的
# 输出当期望值等于自证，这里刻意与实现解耦）。
EXPECTED_UNIT_IDS = [
    "sample_act1-00012",
    "sample_act1-00014",
    "sample_act1-00015",
    "sample_act1-00017",
    "sample_act1-00018",
    "sample_act1-00025",
    "sample_act1-00027",
    "sample_act1-00028",
]

EXPECTED_SPEAKERS = ["千代", None, None, "主人公", "主人公", None, "千代", "主人公"]

EXPECTED_MACROS = [
    ['[ruby text="しんじつ"]', "[p]"],
    ["[r]"],
    ["[font size=24]", "[font size=default]", "[p]"],
    ['[ruby text="ちよ"]', "[r]"],
    ['[ruby text="まこと"]', "[p]"],
    ["[p]"],
    ['[ruby text="ゆびきり"]', "[r]", "[p]"],
    ["[p]"],
]

EXPECTED_SOURCE_TEXTS = [
    "「真実を知る覚悟は、もうできているの？」",
    "思い出すのは、あの雨の夜のことだ。",
    "傘を折られた私は、濡れたまま家の扉の前に立っていた。",
    "「あの時、千代は何も言わなかった。」",
    "「ただ、誠を貫くことだけを選んだ。」",
    "波の音だけが、二人の間を流れていた。",
    "「指切りげんまん、嘘ついたら針千本飲ます。」",
    "「……その約束、今でも守っている。」",
]

MANUAL_TRANSLATIONS = [
    "「知道真相的觉悟，早就已经做好了吧？」",
    "我回想起的，是那个雨夜的事。",
    "被打折了伞的我，就那样浑身湿透地站在家门之前。",
    "「那个时候，千代什么也没有说。」",
    "「只是选择了贯彻诚实这一条路。」",
    "只有波浪的声音，在两人之间流淌着。",
    "「拉钩上吊，说谎的话就吞一千根针。」",
    "「……那个约定，我至今仍然守着。」",
]

KAG_SKELETON_LINES = {
    1: "; ============================================================",
    7: "*scene_start",
    8: '@bg storage="room.png" time=800',
    9: "@wait time=400",
    11: "; ---- 第一幕：真相 ----",
    20: "; ---- 第二幕：夜の海岸 ----",
    21: "*scene_coast",
    22: '@bg storage="coast_night.png" time=1200',
    23: "@cm",
    30: "*scene_end",
}
"""骨架行（注释 / 跳转标签 / @ 命令）—— 回写后必须逐字节原样。"""


def normalize_newlines(raw: bytes) -> bytes:
    """剥离 BOM 并把行尾归一为 LF：跨平台可比的「内容字节」形态。

    适配器回写走 ``Path.write_text``，在 Windows 上会把 ``\\n`` 翻译为
    ``os.linesep``（CRLF）；样本本身也是 CRLF。故无损性断言应建立在
    「内容字节」而非「行尾约定」之上。
    """
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    return body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


class Rpc:
    """把请求序列化成真实 JSON-RPC 行帧并解析响应（等价壳层的收发路径）。"""

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

    def ok(self, method: str, params: dict | None = None) -> object:
        """断言成功帧并取出 result。"""
        response = self.call(method, params)
        assert "error" not in response, f"{method} 失败：{response.get('error')}"
        return response["result"]

    def error_code(self, method: str, params: dict | None = None) -> int:
        response = self.call(method, params)
        assert "error" in response, f"{method} 期望失败帧，实际成功：{response.get('result')!r}"
        return response["error"]["code"]


@pytest.fixture(scope="module")
def loop(tmp_path_factory) -> dict:
    """一次性跑通加载 → 无人审校导出 → 人工审校导出 → 再抽取，供分项断言。"""
    workdir = tmp_path_factory.mktemp("rpc-roundtrip")
    scene = workdir / "sample_act1.ks"
    scene.write_bytes(SAMPLE.read_bytes())

    rpc = Rpc()
    detection = rpc.ok("detect_format", {"file_path": str(scene)})
    extracted = rpc.ok("extract_to_ir", {"file_path": str(scene)})
    project = extracted["project"]

    # --- 路径 A：未经翻译直接回写（应当与源文件内容完全一致） ---
    pristine_dir = workdir / "pristine"
    pristine = Path(rpc.ok("ir_to_asset", {"project": project, "output_dir": str(pristine_dir)})["output_path"])
    pristine_reextract = rpc.ok("extract_to_ir", {"file_path": str(pristine)})["project"]

    # --- 路径 B：模拟人工内联审校（改译文 → 即时质检 → 导出） ---
    clocked = json.loads(json.dumps(project))  # 深拷贝，避免污染路径 A 的信封
    for unit, translation in zip(clocked["units"], MANUAL_TRANSLATIONS):
        unit["translated_text"] = translation
    checked = rpc.ok("run_lqa", {"units": clocked["units"]})

    translated_dir = workdir / "localized"
    exported = Path(
        rpc.ok("ir_to_asset", {"project": clocked, "output_dir": str(translated_dir)})["output_path"]
    )
    reextracted = rpc.ok("extract_to_ir", {"file_path": str(exported)})["project"]

    return {
        "workdir": workdir,
        "scene": scene,
        "detection": detection,
        "extracted": extracted,
        "project": project,
        "pristine": pristine,
        "pristine_reextract": pristine_reextract,
        "checked": checked,
        "exported": exported,
        "reextracted": reextracted,
    }


class TestEngineDetectionAndExtraction:
    """① 引擎嗅探与 ② 单元抽取（宏登记为原子标记）。"""

    def test_detect_format_claims_kag_sample(self, loop):
        assert loop["detection"] == {"detected": True, "adapter": "kag"}

    def test_extract_units_speakers_and_stats(self, loop):
        assert loop["extracted"]["unit_count"] == len(EXPECTED_UNIT_IDS)
        assert loop["extracted"]["stats"]["by_status"] == {"EXTRACTED": len(EXPECTED_UNIT_IDS)}
        units = loop["project"]["units"]
        assert [u["id"] for u in units] == EXPECTED_UNIT_IDS
        assert [u["speaker"] for u in units] == EXPECTED_SPEAKERS

    def test_inline_macros_registered_as_atomic_tags(self, loop):
        registration = [[t["raw_tag"] for t in u["atomic_tags"]] for u in loop["project"]["units"]]
        assert registration == EXPECTED_MACROS

    def test_extracted_text_strips_macros_and_prefix(self, loop):
        assert [u["extracted_text"] for u in loop["project"]["units"]] == EXPECTED_SOURCE_TEXTS


class TestLosslessWriteback:
    """③ 骨架逐字节无损 ④ 宏守恒 ⑤ BOM 与行尾 ⑥ 往返幂等。"""

    def test_pristine_roundtrip_is_content_byte_identical(self, loop):
        """未改动任何译文时，回写产物与源文件必须是同一份「内容字节」。"""
        assert normalize_newlines(loop["pristine"].read_bytes()) == normalize_newlines(
            loop["scene"].read_bytes()
        )

    def test_skeleton_lines_verbatim_after_manual_edit(self, loop):
        """人工改过译文后，骨架行（注释 / 标签 / 命令）仍须逐字节原样。"""
        rendered = normalize_newlines(loop["exported"].read_bytes()).decode("utf-8").split("\n")
        for lineno, expected in KAG_SKELETON_LINES.items():
            assert rendered[lineno - 1] == expected, f"第 {lineno} 行骨架被破坏"

    def test_macros_conserved_and_translation_landed(self, loop):
        """每行的宏多重集必须与登记一致，且正文恰好是人工译文。"""
        rendered = normalize_newlines(loop["exported"].read_bytes()).decode("utf-8").split("\n")
        units = loop["project"]["units"]
        for unit, expected_macros, translation in zip(units, EXPECTED_MACROS, MANUAL_TRANSLATIONS):
            line = rendered[int(unit["metadata"]["kag_line"]) - 1]
            found = Counter(t["raw_tag"] for t in unit["atomic_tags"])
            assert Counter(expected_macros) == found, f"{unit['id']} 宏登记自相矛盾"
            for macro in expected_macros:
                assert macro in line, f"{unit['id']} 回写行丢失宏 {macro}"
            # 宏是**原位复写进正文**的，故不能断言「前缀+译文」是连续子串；
            # 正确断言是：剥离全部宏后，行内容恰好等于「说话人前缀 + 人工译文」。
            body = line
            for macro in expected_macros:
                body = body.replace(macro, "")
            prefix = f"【{unit['speaker']}】" if unit["speaker"] else ""
            assert body == prefix + translation, f"{unit['id']} 译文未按原位落盘"

    def test_export_carries_utf8_bom_without_duplicate_carriage_return(self, loop):
        raw = loop["exported"].read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), "导出文件缺少 UTF-8 BOM"
        assert b"\r\r" not in raw, "出现 \\r\\r 双回车：CRLF 归一化与写入换行翻译发生叠加"
        assert raw.count(b"\n") == normalize_newlines(loop["scene"].read_bytes()).count(b"\n"), (
            "行数发生漂移，骨架被撑破或吞行"
        )

    def test_roundtrip_is_idempotent(self, loop):
        """对回写产物再抽取，结构必须与首次抽取完全一致（幂等闭环）。"""
        again = loop["pristine_reextract"]
        assert [u["id"] for u in again["units"]] == EXPECTED_UNIT_IDS
        assert [u["speaker"] for u in again["units"]] == EXPECTED_SPEAKERS
        assert [u["extracted_text"] for u in again["units"]] == EXPECTED_SOURCE_TEXTS
        assert [[t["raw_tag"] for t in u["atomic_tags"]] for u in again["units"]] == EXPECTED_MACROS

    def test_reextract_of_translated_export_recovers_manual_edits(self, loop):
        """汉化产物再抽取：正文必须恰好是人工资讯，宏登记与说话人不变。"""
        again = loop["reextracted"]
        assert [u["id"] for u in again["units"]] == EXPECTED_UNIT_IDS
        assert [u["speaker"] for u in again["units"]] == EXPECTED_SPEAKERS
        assert [u["extracted_text"] for u in again["units"]] == MANUAL_TRANSLATIONS
        assert [[t["raw_tag"] for t in u["atomic_tags"]] for u in again["units"]] == EXPECTED_MACROS


class TestRunLqaGate:
    """⑦ run_lqa 即时质检：人工改坏译文必须被拦下，修复后必须被放行。"""

    @staticmethod
    def _unit(translated: str | None) -> dict:
        return {
            "id": "gate-00001",
            "speaker": "千代",
            "raw_text": "【千代】「約束」は、守るためにある。",
            "extracted_text": "「約束」は、守るためにある。",
            "atomic_tags": [],
            "paired_tags": [],
            "translated_text": translated,
            "status": "EXTRACTED",
            "metadata": {},
        }

    def test_unbalanced_quote_is_blocked(self, loop):
        rpc = Rpc()
        units = rpc.ok("run_lqa", {"units": [self._unit("「约定是——为了遵守而存在的。")]})
        assert units[0]["status"] == "LQA_FAILED"
        issues = units[0]["metadata"]["lqa_issues"]
        assert any(i["rule_id"] == "cjk_punctuation" and i["severity"] == "error" for i in issues)

    def test_odd_dash_run_is_blocked(self, loop):
        rpc = Rpc()
        units = rpc.ok("run_lqa", {"units": [self._unit("约定——是为了遵守—而存在的。")]})
        assert units[0]["status"] == "LQA_FAILED"

    def test_repaired_translation_passes_and_clears_stale_issues(self, loop):
        rpc = Rpc()
        broken = self._unit("「约定是为了遵守而存在的。")
        damaged = rpc.ok("run_lqa", {"units": [broken]})
        assert damaged[0]["status"] == "LQA_FAILED"

        # 模拟人工修好引号后再次失焦复检：必须绿，且陈旧违例留痕被清除
        repaired = dict(broken, translated_text="「约定是为了遵守而存在的。」")
        fixed = rpc.ok("run_lqa", {"units": [repaired]})
        assert fixed[0]["status"] == "LQA_PASSED"
        assert "lqa_issues" not in fixed[0]["metadata"]

    def test_cleared_translation_returns_to_extracted(self, loop):
        """人工清空译文是回退场景：回归待译态，不得因规则空过而误判为通过。"""
        rpc = Rpc()
        units = rpc.ok("run_lqa", {"units": [self._unit(None)]})
        assert units[0]["status"] == "EXTRACTED"

    def test_manual_translations_in_full_loop_all_pass(self, loop):
        statuses = [u["status"] for u in loop["checked"]]
        assert statuses == ["LQA_PASSED"] * len(MANUAL_TRANSLATIONS)


class TestProtocolErrorFrames:
    """⑧ 异常帧拦截：业务错误与协议错误必须落到各自的标准错误码。"""

    def test_unknown_method_maps_to_method_not_found(self):
        assert Rpc().error_code("no_such_method") == -32601

    def test_garbage_file_extract_maps_to_business_error(self, tmp_path):
        garbage = tmp_path / "garbage.ks"
        garbage.write_text("no kag features here\n", encoding="utf-8")
        assert Rpc().error_code("extract_to_ir", {"file_path": str(garbage)}) == -32000

    def test_missing_file_extract_maps_to_business_error(self):
        assert Rpc().error_code("extract_to_ir", {"file_path": r"Z:\nope\missing.ks"}) == -32000

    def test_unknown_engine_type_maps_to_business_error(self, tmp_path):
        payload = {
            "project_name": "x",
            "source_lang": "ja",
            "target_lang": "zh-CN",
            "engine_type": "psb",
            "units": [],
        }
        assert Rpc().error_code(
            "ir_to_asset", {"project": payload, "output_dir": str(tmp_path)}
        ) == -32000

    def test_positional_params_rejected(self):
        """行式协议刻意只支持具名参数对象（语义自描述）。"""
        response = json.loads(
            build_default_dispatcher().handle_request(
                '{"jsonrpc":"2.0","id":1,"method":"run_lqa","params":[]}'
            )
        )
        assert response["error"]["code"] == -32602

    def test_malformed_json_maps_to_parse_error(self):
        response = json.loads(build_default_dispatcher().handle_request("{not json"))
        assert response["error"]["code"] == -32700
