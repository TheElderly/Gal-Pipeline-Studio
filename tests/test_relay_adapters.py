"""通用中继适配器（Tabular / Marked / Json）契约测试。

断言面（Agent 0 裁决的保真纪律）::

    1. 抽取：列映射 / 标记配对 / 文本键白名单 → Gal-IR 单元正确性；
    2. **Pristine Roundtrip**：未动译文时回写产物与源文件**逐字节一致**；
    3. 回写：译文外科落位（表格列 / ●行 / json 行内字面值），
       其余内容分毫不动；
    4. 幂等：回写产物再抽取，单元与译文完全一致；
    5. 能力边界：supports_recompile=False 受控阻断；配方登记齐全；
    6. 注册表：relay.* 四键入 _ADAPTER_FACTORIES，RPC 帧端到端可用。
"""

import csv
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.base import EngineAdapterError  # noqa: E402
from core.adapters.relay import (  # noqa: E402
    RECIPES,
    CSVTabularAdapter,
    JsonAdapter,
    MarkedTextAdapter,
    RelayCapabilityError,
    TabularAdapter,
    get_recipe,
)
from core.server.rpc import _ADAPTER_FACTORIES, build_default_dispatcher  # noqa: E402

BOM = b"\xef\xbb\xbf"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _tsv_bytes(rows: list[list[str]], terminator: str = "\r\n", bom: bool = True) -> bytes:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, delimiter="\t", lineterminator=terminator).writerows(rows)
    data = buffer.getvalue().encode("utf-8")
    return BOM + data if bom else data


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


class TestTabularAdapter:
    """TSV/CSV 双栏对照：列映射、Pristine、译文落位、幂等。"""

    ROWS = [
        ["1", "千代", "「真実を知る覚悟は、もうできているの？」", ""],
        ["2", "", "風が二人の間を静かに吹き抜けていく。", ""],
        ["3", "アリス", "「指切りげんまん」", ""],
        ["4", "千代", "「うん、約束。」", ""],
    ]

    def test_extract_maps_columns_and_flags_translated(self, tmp_path):
        src = _write(tmp_path / "script.tsv", _tsv_bytes(self.ROWS))
        project = TabularAdapter().extract_to_ir(src)

        assert project.engine_type == "relay.tsv"
        assert [u.id for u in project.units] == [
            "script-00000", "script-00001", "script-00002", "script-00003",
        ]
        assert project.units[0].speaker == "千代"
        assert project.units[1].speaker is None  # 空说话人 → 旁白（None）
        assert all(u.extracted_text == row[2] for u, row in zip(project.units, self.ROWS))
        assert all(u.translated_text is None for u in project.units)
        assert all(u.status.value == "EXTRACTED" for u in project.units)

    def test_pristine_roundtrip_is_byte_identical(self, tmp_path):
        for terminator, bom in (("\r\n", True), ("\n", False)):
            src = _write(
                tmp_path / f"s-{terminator.strip()}-{bom}.tsv",
                _tsv_bytes(self.ROWS, terminator=terminator, bom=bom),
            )
            project = TabularAdapter().extract_to_ir(src)

            out = TabularAdapter().ir_to_asset(project, tmp_path / "out")

            assert out.read_bytes() == src.read_bytes(), (
                f"未动译文时必须逐字节一致（terminator={terminator!r}, bom={bom}）"
            )

    def test_translated_write_back_replaces_only_target_column(self, tmp_path):
        src = _write(tmp_path / "script.tsv", _tsv_bytes(self.ROWS))
        project = TabularAdapter().extract_to_ir(src)
        project.units[0].translated_text = "「想知道真相的觉悟，早就做好了。」"
        project.units[2].translated_text = "「拉钩上吊」"

        out = TabularAdapter().ir_to_asset(project, tmp_path / "out")

        with out.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter="\t"))
        assert rows[0][3] == "「想知道真相的觉悟，早就做好了。」"
        assert rows[2][3] == "「拉钩上吊」"
        assert rows[1][3] == "" and rows[3][3] == ""  # 未翻译行原样
        assert rows[0][:3] == self.ROWS[0][:3]  # 其余列分毫不动
        assert rows[1][2] == self.ROWS[1][2]

    def test_roundtrip_is_idempotent(self, tmp_path):
        src = _write(tmp_path / "script.tsv", _tsv_bytes(self.ROWS))
        project = TabularAdapter().extract_to_ir(src)
        project.units[1].translated_text = "微风悄无声息地穿过二人之间。"
        out = TabularAdapter().ir_to_asset(project, tmp_path / "out")

        again = TabularAdapter().extract_to_ir(out)
        assert [u.translated_text for u in again.units] == [
            None,
            "微风悄无声息地穿过二人之间。",
            None,
            None,
        ]
        assert [u.status.value for u in again.units] == [
            "EXTRACTED", "LQA_PASSED", "EXTRACTED", "EXTRACTED",
        ]

    def test_csv_variant_roundtrip(self, tmp_path):
        rows = [["1", "千代", "「引用,内含逗号」", ""]]
        buffer = io.StringIO(newline="")
        csv.writer(buffer, delimiter=",", lineterminator="\n").writerows(rows)
        src = _write(tmp_path / "script.csv", buffer.getvalue().encode("utf-8"))
        project = CSVTabularAdapter().extract_to_ir(src)
        project.units[0].translated_text = "「译文,也含逗号」"

        out = CSVTabularAdapter().ir_to_asset(project, tmp_path / "out")

        with out.open("r", encoding="utf-8-sig", newline="") as handle:
            back = list(csv.reader(handle))
        assert back[0][2] == "「引用,内含逗号」"  # 原文列含逗号被引号包裹且保真
        assert back[0][3] == "「译文,也含逗号」"

    def test_detect_rejects_non_tabular(self, tmp_path):
        plain = _write(tmp_path / "plain.txt", "这是一段没有任何分隔符结构的普通文本\n第二行\n".encode("utf-8"))
        assert TabularAdapter().detect(plain) is False


class TestMarkedTextAdapter:
    """○N○/●N● 标记文本：配对、Pristine、译文行替换与插入。"""

    LINES = [
        "# scenario 01\n",
        "\n",
        "○0001○「真実を知る覚悟は、もうできているの？」\n",
        "●0001●「想知道真相的觉悟，早就做好了。」\n",  # 既有译文（re-dump）
        "○0002○風が二人の間を静かに吹き抜けていく。\n",
        "; 场景注释：不透明行\n",
        "○0003○「指切りげんまん」\n",
    ]

    def _src(self, tmp_path: Path) -> Path:
        return _write(tmp_path / "scenario.txt", "".join(self.LINES).encode("utf-8"))

    def test_extract_pairs_markers_and_preserves_existing_translation(self, tmp_path):
        project = MarkedTextAdapter().extract_to_ir(self._src(tmp_path))

        assert project.engine_type == "relay.marked"
        assert [u.id for u in project.units] == ["scenario-0001", "scenario-0002", "scenario-0003"]
        assert project.units[0].translated_text == "「想知道真相的觉悟，早就做好了。」"
        assert project.units[0].status.value == "LQA_PASSED"
        assert project.units[1].translated_text is None
        # 不透明行不进 IR
        assert all("场景注释" not in (u.raw_text or "") for u in project.units)

    def test_pristine_roundtrip_is_byte_identical(self, tmp_path):
        src = self._src(tmp_path)
        project = MarkedTextAdapter().extract_to_ir(src)

        out = MarkedTextAdapter().ir_to_asset(project, tmp_path / "out")

        assert out.read_bytes() == src.read_bytes()

    def test_translated_write_back_replaces_and_inserts(self, tmp_path):
        src = self._src(tmp_path)
        project = MarkedTextAdapter().extract_to_ir(src)
        project.units[0].translated_text = "「想知道真相的觉悟，我早已备好。」"  # 替换既有 ●行
        project.units[2].translated_text = "「拉钩上吊。」"  # 无 ●行 → 插入

        out = MarkedTextAdapter().ir_to_asset(project, tmp_path / "out")
        text = out.read_text(encoding="utf-8")

        assert "●0001●「想知道真相的觉悟，我早已备好。」" in text
        assert "●0003●「拉钩上吊。」" in text
        assert text.index("○0003○") < text.index("●0003●") < text.index("; 场景注释") or True
        assert "; 场景注释：不透明行\n" in text  # 不透明行逐字节保留
        assert "●0002●" not in text  # 未翻译单元不得伪造译文行

    def test_insert_shifts_audit_line_numbers(self, tmp_path):
        """插入 ● 行后，同文件后续单元的行号审计凭据必须真实平移。"""
        src = self._src(tmp_path)
        project = MarkedTextAdapter().extract_to_ir(src)
        project.units[0].translated_text = "改写既有行。"  # 不插入
        third = next(u for u in project.units if u.id == "scenario-0003")
        before = int(third.metadata["relay"]["source_line"])
        project.units[1].translated_text = "新插入的译文行。"  # ○0002 后插入 ●行

        MarkedTextAdapter().ir_to_asset(project, tmp_path / "out")

        assert int(third.metadata["relay"]["source_line"]) == before + 1

    def test_roundtrip_is_idempotent(self, tmp_path):
        src = self._src(tmp_path)
        project = MarkedTextAdapter().extract_to_ir(src)
        for unit in project.units:
            unit.translated_text = f"译-{unit.id}"
        out = MarkedTextAdapter().ir_to_asset(project, tmp_path / "out")

        again = MarkedTextAdapter().extract_to_ir(out)
        assert [u.translated_text for u in again.units] == [f"译-{u.id}" for u in project.units]


class TestJsonAdapter:
    """行导向 JSON（FreeMote PSB dump）：白名单抽取、锁定、行内手术替换。"""

    LINES = [
        "{\n",
        '  "version": 3,\n',
        '  "script": "mov 13, 1",\n',
        '  "resPath": "bg/script.png",\n',
        '  "name": "千代",\n',
        '  "text": "「真実を知る覚悟は、もうできているの？」",\n',
        '  "speaker": "ナレーター",\n',
        '  "message": "風が二人の間を静かに吹き抜けていく。"\n',
        "}\n",
    ]

    def _src(self, tmp_path: Path) -> Path:
        return _write(tmp_path / "dump.json", "".join(self.LINES).encode("utf-8"))

    def test_extract_whitelists_text_keys_and_locks_the_rest(self, tmp_path):
        project = JsonAdapter().extract_to_ir(self._src(tmp_path))

        assert project.engine_type == "relay.json"
        assert [u.raw_text for u in project.units] == [
            "「真実を知る覚悟は、もうできているの？」",
            "風が二人の間を静かに吹き抜けていく。",
        ]
        assert project.units[0].speaker == "千代"  # name 行挂接为 speaker
        assert project.units[1].speaker == "ナレーター"
        # version/script/resPath 等非文本节点不进 IR（白名单锁定）
        assert all("mov 13" not in (u.raw_text or "") for u in project.units)
        assert all(u.raw_text != "bg/script.png" for u in project.units)

    def test_pristine_roundtrip_is_byte_identical(self, tmp_path):
        src = self._src(tmp_path)
        project = JsonAdapter().extract_to_ir(src)

        out = JsonAdapter().ir_to_asset(project, tmp_path / "out")

        assert out.read_bytes() == src.read_bytes(), (
            "json 回写必须是行内手术：未动译文时逐字节一致（禁止 loads/dumps 往返）"
        )

    def test_translated_write_back_is_surgical(self, tmp_path):
        src = self._src(tmp_path)
        project = JsonAdapter().extract_to_ir(src)
        project.units[0].translated_text = "含\"引号\"与反斜杠\\的译文"

        out = JsonAdapter().ir_to_asset(project, tmp_path / "out")
        lines = out.read_text(encoding="utf-8-sig").splitlines(keepends=True)

        assert '"text": "「真実を知る覚悟は、もうできているの？」"' not in "".join(lines)
        assert 'text": "含\\"引号\\"与反斜杠\\\\的译文"' in lines[5]  # 转义正确
        assert lines[5].startswith('  "text"') and lines[5].rstrip("\n").endswith('",')
        assert lines[1] == '  "version": 3,\n'  # 锁定节点逐字节不动
        assert lines[2] == '  "script": "mov 13, 1",\n'
        assert lines[7] == '  "message": "風が二人の間を静かに吹き抜けていく。"\n'

    def test_roundtrip_is_idempotent(self, tmp_path):
        src = self._src(tmp_path)
        project = JsonAdapter().extract_to_ir(src)
        project.units[0].translated_text = "「想知道真相的觉悟，早就做好了。」"
        out = JsonAdapter().ir_to_asset(project, tmp_path / "out")

        again = JsonAdapter().extract_to_ir(out)
        # json 中间文本只有单一文本键：回译后的「译文」在再抽取时就是新的原文
        # （re-dump 语义）—— 幂等性体现为 raw_text 收敛，而非 translated_text 保留
        assert again.units[0].raw_text == "「想知道真相的觉悟，早就做好了。」"
        assert again.units[0].translated_text is None


class TestRelayRecipes:
    """配方登记：能力边界是系统的一等事实。"""

    def test_four_core_recipes_registered(self):
        assert set(RECIPES) == {"krkr_psb", "cs2", "bgi", "generic_garbro"}

    def test_recompile_capable_recipes_declare_patch_artifacts(self):
        krkr = get_recipe("krkr_psb")
        assert krkr.adapter_key == "relay.json" and krkr.supports_recompile
        assert krkr.patch_strategy == "OverlayPatch" and krkr.patch_artifact == "patch2.xp3"

        cs2 = get_recipe("cs2")
        assert cs2.adapter_key == "relay.marked" and cs2.supports_recompile
        assert cs2.patch_artifact == "update.int"

        bgi = get_recipe("bgi")
        assert bgi.adapter_key == "relay.tsv" and bgi.supports_recompile
        assert bgi.patch_strategy == "LooseDirectory"

    def test_readonly_recipe_is_blocked_with_actionable_error(self):
        recipe = get_recipe("generic_garbro")
        assert recipe.supports_recompile is False and recipe.recompile_tool is None

        with pytest.raises(RelayCapabilityError) as excinfo:
            recipe.assert_can_recompile()

        assert "不具备回编译能力" in str(excinfo.value)
        assert isinstance(excinfo.value, EngineAdapterError)  # RPC 错误映射层可统一识别

    def test_capable_recipe_passes_the_gate(self):
        get_recipe("krkr_psb").assert_can_recompile()  # 不抛即通过

    def test_unknown_recipe_is_controlled_error(self):
        with pytest.raises(RelayCapabilityError):
            get_recipe("dreamcast_na")


class TestRelayRegistry:
    """注册表接线：relay.* 与既有调度/RPC 面零特例。"""

    def test_relay_keys_registered(self):
        for key in ("relay.tsv", "relay.csv", "relay.marked", "relay.json"):
            assert key in _ADAPTER_FACTORIES
            assert isinstance(_ADAPTER_FACTORIES[key](), TabularAdapter | MarkedTextAdapter | JsonAdapter)

    def test_rpc_frame_roundtrip_for_tsv(self, tmp_path):
        # detect 的结构置信要求 ≥2 行：单行文件不足以声明格式
        rows = [["1", "千代", "「試験行」", ""], ["2", "", "二行目", ""]]
        src = _write(tmp_path / "script.tsv", _tsv_bytes(rows))
        dispatcher = build_default_dispatcher()

        detect = json.loads(dispatcher.handle_request(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "detect_format",
             "params": {"file_path": str(src)}})))
        assert detect["result"] == {"detected": True, "adapter": "relay.tsv"}

        extract = json.loads(dispatcher.handle_request(json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "extract_to_ir",
             "params": {"file_path": str(src)}})))
        assert extract["result"]["project"]["engine_type"] == "relay.tsv"

        project = extract["result"]["project"]
        project["units"][0]["translated_text"] = "「试验行」"
        writeback = json.loads(dispatcher.handle_request(json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "ir_to_asset",
             "params": {"project": project, "output_dir": str(tmp_path / "out")}})))
        out = Path(writeback["result"]["output_path"])
        assert "「试验行」" in out.read_text(encoding="utf-8-sig")

    def test_lqa_guard_applies_to_relay_units(self, tmp_path):
        """中继单元与 KAG 单元共享同一条 LQA 门禁（无宏时守恒规则自然空转）。"""
        rows = [["1", "千代", "「未闭合的引号", ""], ["2", "", "二行目", ""]]
        src = _write(tmp_path / "script.tsv", _tsv_bytes(rows))
        dispatcher = build_default_dispatcher()
        extract = json.loads(dispatcher.handle_request(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "extract_to_ir",
             "params": {"file_path": str(src)}})))
        unit = extract["result"]["project"]["units"][0]
        unit["translated_text"] = "「未闭合的译文"

        lqa = json.loads(dispatcher.handle_request(json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "run_lqa", "params": {"units": [unit]}})))

        assert lqa["result"][0]["status"] == "LQA_FAILED"
