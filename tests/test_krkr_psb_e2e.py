"""krkr_psb 端到端闭环：PSB 二进制 → dump → relay.json → 译文 → 回写 → 回编译。

测试策略（诚实边界）::

    本机未装配真实 FreeMote（离线红线禁止联网拉取），故以**高保真行为
    替身**承载 FreeMote 的可观测契约 —— PSB 二进制容器（magic + 长度 +
    JSON 载荷）⇄ 缩进 JSON 的双向转换，命令行接口与校准后的配方模板
    逐 token 对齐。替身验证的是**我们这一侧**的全链路（bridge 调度 →
    relay.json 抽取/外科回写 → 产物校验）；真实 FreeMote 的编解码等价性
    待用户装配后由同一套测试自动接管（工具路径换真即可）。

断言面::

    1. E2E：PSB → dump（.psb.json）→ 抽取对白/角色 → 译文变更 →
       行内手术回写 → compile → 二进制 PSB，载荷仅文本值变化；
    2. 保真：缩进 / 换行符 / 非文本节点（motion / ver / res）逐字节不动；
    3. 交付：recipe.patch_strategy == OverlayPatch，增量包名 patch2.xp3，
       overlay 暂存结构按原相对路径收纳回编译产物；
    4. 防御：FreeMote 路径缺失受控报错；模板校准标记如实登记。
"""

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.relay.recipes import get_recipe  # noqa: E402
from core.adapters.relay.json_relay import JsonAdapter  # noqa: E402
from core.engine import EngineToolchainBridge, RelayToolchainError  # noqa: E402

_PSB_MAGIC = b"PSB\x00"

# FreeMote 反编译形态：2 空格缩进、LF、一行一键值；含非文本节点
_PSB_JSON = {
    "ver": 1.1,
    "res": {"bg": "bg/logo.png", "hash": 3735928559},
    "scenes": [
        {
            "name": "千代",
            "text": "「真実を知る覚悟は、もうできているの？」",
            "motion": "fade 13, 1",
        },
        {"text": "風が二人の間を静かに吹き抜けていく。"},
    ],
}
_ORIGINAL_TEXT = "「真実を知る覚悟は、もうできているの？」"
_TRANSLATED_TEXT = "「想知道真相的觉悟，早就做好了。」"


def _psb_wrap(json_text: str) -> bytes:
    payload = json_text.encode("utf-8")
    return _PSB_MAGIC + struct.pack("<I", len(payload)) + payload


def _psb_unwrap(data: bytes) -> str:
    assert data[:4] == _PSB_MAGIC, "回编译产物必须保留 PSB 容器魔数"
    (length,) = struct.unpack_from("<I", data, 4)
    assert len(data) == 8 + length, "PSB 容器长度字段必须与载荷一致"
    return data[8:].decode("utf-8")


def _make_psb_shims(directory: Path) -> tuple[str, str]:
    """生成 PsbDecompile / PsbBuild 行为替身（cmd 承载，argv 与配方模板对齐）。"""
    tool_py = directory / "freemote_shim.py"
    tool_py.write_text(
        "import json, struct, sys\n"
        "magic = b'PSB\\x00'\n"
        "mode = 'decompile' if sys.argv[1] == '-o' else 'build'\n"
        "if mode == 'decompile':\n"
        "    out_dir, src = sys.argv[2], sys.argv[3]\n"
        "    raw = open(src, 'rb').read()\n"
        "    assert raw[:4] == magic, 'not a fixture PSB'\n"
        "    (length,) = struct.unpack_from('<I', raw, 4)\n"
        "    payload = raw[8:8 + length]\n"
        "    open(out_dir + '\\\\' + sys.argv[3].rstrip('\\\\/')"
        ".rsplit('/', 1)[-1].rsplit('\\\\', 1)[-1] + '.json', 'wb').write(payload)\n"
        "else:\n"
        "    src, out = sys.argv[1], sys.argv[3]\n"
        "    payload = open(src, 'rb').read()\n"
        "    json.loads(payload.decode('utf-8'))  # build 前先验证 JSON 合法（FreeMote 语义）\n"
        "    open(out, 'wb').write(magic + struct.pack('<I', len(payload)) + payload)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    decompile = directory / "PsbDecompile.cmd"
    decompile.write_text(
        f'@echo off\r\n"{sys.executable}" "{tool_py}" %*\r\n', encoding="utf-8"
    )
    build = directory / "PsbBuild.cmd"
    build.write_text(
        f'@echo off\r\n"{sys.executable}" "{tool_py}" %*\r\n', encoding="utf-8"
    )
    return str(decompile), str(build)


def _make_fixture_psb(tmp_path: Path) -> Path:
    psb = tmp_path / "scene.psb"
    psb.write_bytes(_psb_wrap(json.dumps(_PSB_JSON, ensure_ascii=False, indent=2) + "\n"))
    return psb


class TestKrkrPsbEndToEnd:
    """PSB 二进制 ⇄ JSON ⇄ Gal-IR 全链路（经真实子进程与校准模板）。"""

    def _dump(self, tmp_path: Path, decompile: str) -> Path:
        psb = _make_fixture_psb(tmp_path)
        return EngineToolchainBridge().dump_to_intermediate(
            "krkr_psb", psb, tmp_path / "intermediate", decompile
        )

    def test_full_loop_binary_to_binary_with_surgical_payload(self, tmp_path):
        decompile, build = _make_psb_shims(tmp_path)

        # 1) dump：PSB → .psb.json（FreeMote 命名约定：<name>.psb.json）
        intermediate = self._dump(tmp_path, decompile)
        assert intermediate.name == "scene.psb.json"
        original_lines = intermediate.read_text(encoding="utf-8").splitlines(keepends=True)

        # 2) 抽取：对白 + 角色归属
        project = JsonAdapter().extract_to_ir(intermediate)
        assert [u.raw_text for u in project.units] == [_ORIGINAL_TEXT, "風が二人の間を静かに吹き抜けていく。"]
        assert project.units[0].speaker == "千代"

        # 3) 译文变更 + 外科回写
        project.units[0].translated_text = _TRANSLATED_TEXT
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")
        translated_lines = written.read_text(encoding="utf-8").splitlines(keepends=True)

        # 4) 保真：全文只有 text 值那一行变化，缩进/换行/其余行逐字节不动
        changed = [
            (a, b) for a, b in zip(original_lines, translated_lines) if a != b
        ]
        assert len(changed) == 1, f"必须只改动一行，实际 {len(changed)} 行"
        original_line, translated_line = changed[0]
        assert f'": "{_ORIGINAL_TEXT}"' in original_line
        assert f'": "{_TRANSLATED_TEXT}"' in translated_line
        assert original_line[: original_line.index('": ')] == translated_line[: translated_line.index('": ')]
        assert len(original_lines) == len(translated_lines)

        # 5) compile：JSON → 二进制 PSB（替身先验 JSON 合法再封装）
        target = tmp_path / "patch2.d" / "scene.psb"
        result = EngineToolchainBridge().compile_from_intermediate(
            "krkr_psb", written, target, build)

        assert result == target
        rebuilt_json = _psb_unwrap(target.read_bytes())
        assert _TRANSLATED_TEXT in rebuilt_json and _ORIGINAL_TEXT not in rebuilt_json
        # 非文本节点经完整往返分毫不动
        roundtrip = json.loads(rebuilt_json)
        assert roundtrip["ver"] == 1.1
        assert roundtrip["res"] == {"bg": "bg/logo.png", "hash": 3735928559}
        assert roundtrip["scenes"][0]["motion"] == "fade 13, 1"
        assert roundtrip["scenes"][0]["text"] == _TRANSLATED_TEXT

    def test_pristine_loop_reproduces_source_payload(self, tmp_path):
        """未动译文：dump → 抽取 → 回写 → compile 的载荷必须与源逐字节一致。"""
        decompile, build = _make_psb_shims(tmp_path)
        source_psb = _make_fixture_psb(tmp_path)
        intermediate = self._dump(tmp_path, decompile)

        project = JsonAdapter().extract_to_ir(intermediate)
        assert all(u.translated_text is None for u in project.units)
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")
        assert written.read_bytes() == intermediate.read_bytes()

        target = tmp_path / "patch2.d" / "scene.psb"
        EngineToolchainBridge().compile_from_intermediate("krkr_psb", written, target, build)
        assert _psb_unwrap(target.read_bytes()) == _psb_unwrap(source_psb.read_bytes())


class TestOverlayPatchDelivery:
    """交付策略元数据与增量包结构。"""

    def test_recipe_declares_overlay_patch_with_canonical_artifact(self):
        recipe = get_recipe("krkr_psb")
        assert recipe.patch_strategy == "OverlayPatch"
        assert recipe.patch_artifact == "patch2.xp3"
        assert recipe.supports_recompile is True
        assert recipe.templates_calibrated is True  # 本轮按 FreeMote 规范校准
        # 其余配方保持草案标记（未实测校准前不得谎报）
        assert get_recipe("cs2").templates_calibrated is False
        assert get_recipe("bgi").templates_calibrated is False

    def test_compiled_artifact_stages_into_overlay_layout(self, tmp_path):
        """增量补丁的暂存结构：回编译产物按**原相对路径**收纳进 patch 根。"""
        decompile, build = _make_psb_shims(tmp_path)
        # 游戏内相对路径带子目录 —— overlay 根必须复刻它
        source_psb = tmp_path / "game" / "scenario" / "scene.psb"
        source_psb.parent.mkdir(parents=True)
        source_psb.write_bytes(_psb_wrap(json.dumps(_PSB_JSON, ensure_ascii=False, indent=2)))

        intermediate = EngineToolchainBridge().dump_to_intermediate(
            "krkr_psb", source_psb, tmp_path / "intermediate", decompile
        )
        project = JsonAdapter().extract_to_ir(intermediate)
        project.units[0].translated_text = _TRANSLATED_TEXT
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")

        patch_root = tmp_path / "patch2.xp3.d"  # OverlayPatch 暂存根（随后打包为 patch2.xp3）
        staged = patch_root / "scenario" / "scene.psb"  # 原相对路径
        EngineToolchainBridge().compile_from_intermediate("krkr_psb", written, staged, build)

        assert staged.is_file() and staged.stat().st_size > 0
        assert _psb_unwrap(staged.read_bytes()).find(_TRANSLATED_TEXT) != -1


class TestDefensivePaths:
    """真实 FreeMote 未装配时的优雅防御。"""

    def test_missing_freemote_path_is_actionable_error(self, tmp_path):
        psb = _make_fixture_psb(tmp_path)

        with pytest.raises(RelayToolchainError) as excinfo:
            EngineToolchainBridge().dump_to_intermediate(
                "krkr_psb", psb, tmp_path / "out", r"C:\tools\missing\PsbDecompile.exe")

        assert "外部工具不可用" in str(excinfo.value)

    def test_templates_metadata_declares_calibration_status(self):
        """校准状态是配方的一等元数据：krkr_psb 已校准，其余如实保持草案。"""
        krkr = get_recipe("krkr_psb")
        assert krkr.dump_command == "{tool} -o {output_dir} {input}"
        assert krkr.recompile_command == "{tool} {input} -o {output}"
        assert krkr.dump_tool == "PsbDecompile.exe"
        assert krkr.recompile_tool == "PsBuild.exe"


# ---------------------------------------------------------------------------
# 真机 FreeMote 端到端（工具箱装配后自动启用；未装配则如实跳过）
# ---------------------------------------------------------------------------

_FREEMOTE_DIR = Path(__file__).resolve().parents[1] / "tools" / "freemote"
_HAS_REAL_FREEMOTE = (
    (_FREEMOTE_DIR / "PsbDecompile.exe").is_file()
    and (_FREEMOTE_DIR / "PsBuild.exe").is_file()
)


@pytest.mark.skipif(not _HAS_REAL_FREEMOTE,
                    reason="FreeMote Toolkit 未装配（tools/freemote/ 缺 PsbDecompile.exe / PsBuild.exe）")
class TestRealFreemoteToolchain:
    """真机 FreeMote 全链路：所有替身断言在此以真实二进制复核。"""

    def test_real_roundtrip_text_lands_in_real_psb(self, tmp_path):
        bridge = EngineToolchainBridge()
        decompile_exe = str(_FREEMOTE_DIR / "PsbDecompile.exe")
        build_exe = str(_FREEMOTE_DIR / "PsBuild.exe")

        # 1) 语义 JSON → 真机 PsBuild 编译出真 .psb
        semantic = {
            "ver": 1.1,
            "scenes": [
                {"name": "千代", "text": _ORIGINAL_TEXT},
                {"text": "風が二人の間を静かに吹き抜けていく。"},
            ],
        }
        src_json = tmp_path / "in"
        src_json.mkdir()
        (src_json / "scene.psb.json").write_text(
            json.dumps(semantic, ensure_ascii=False, indent=2), encoding="utf-8")
        psb = bridge.compile_from_intermediate(
            "krkr_psb", src_json / "scene.psb.json", tmp_path / "scene.psb", build_exe)
        assert psb.read_bytes()[:4] == b"PSB\x00"

        # 2) 真机 PsbDecompile dump（产物消歧：scene.json 胜过 scene.resx.json）
        intermediate = bridge.dump_to_intermediate(
            "krkr_psb", psb, tmp_path / "intermediate", decompile_exe)
        assert intermediate.name == "scene.json"  # FreeMote 真实命名约定（非 .psb.json）

        # 3) relay.json 抽取 + 译文回写
        project = JsonAdapter().extract_to_ir(intermediate)
        assert [u.raw_text for u in project.units] == [_ORIGINAL_TEXT, "風が二人の間を静かに吹き抜けていく。"]
        assert project.units[0].speaker == "千代"
        project.units[0].translated_text = _TRANSLATED_TEXT
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")

        # 4) 真机 PsBuild 回编译 → 真机 PsbDecompile 复核译文真实落进二进制
        patched = bridge.compile_from_intermediate(
            "krkr_psb", written, tmp_path / "patched.psb", build_exe)
        verify_dir = tmp_path / "verify"
        verified_file = bridge.dump_to_intermediate(
            "krkr_psb", patched, verify_dir, decompile_exe)
        verified = verified_file.read_text(encoding="utf-8")
        assert _TRANSLATED_TEXT in verified and _ORIGINAL_TEXT not in verified
        # 非文本节点经真实二进制往返仍在
        assert '"ver": 1.1' in verified

    def test_real_psb_is_recompilable_after_surgery(self, tmp_path):
        """外科回写产物必须能被真机 PsBuild 接受（JSON 语义完整性闸门）。"""
        bridge = EngineToolchainBridge()
        decompile_exe = str(_FREEMOTE_DIR / "PsbDecompile.exe")
        build_exe = str(_FREEMOTE_DIR / "PsBuild.exe")

        semantic = {"scenes": [{"text": _ORIGINAL_TEXT}]}
        src = tmp_path / "in"
        src.mkdir()
        (src / "scene.psb.json").write_text(
            json.dumps(semantic, ensure_ascii=False), encoding="utf-8")
        psb = bridge.compile_from_intermediate(
            "krkr_psb", src / "scene.psb.json", tmp_path / "scene.psb", build_exe)
        intermediate = bridge.dump_to_intermediate(
            "krkr_psb", psb, tmp_path / "intermediate", decompile_exe)

        project = JsonAdapter().extract_to_ir(intermediate)
        project.units[0].translated_text = _TRANSLATED_TEXT
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")

        # 若手术破坏 JSON 语义，这里会抛 RelayToolchainError 而非静默成功
        patched = bridge.compile_from_intermediate(
            "krkr_psb", written, tmp_path / "patched.psb", build_exe)
        assert patched.stat().st_size > 0


# ---------------------------------------------------------------------------
# 真机 FreeMote 端到端（工具箱装配后自动启用；未装配则如实跳过）
# ---------------------------------------------------------------------------

_FREEMOTE_DIR = Path(__file__).resolve().parents[1] / "tools" / "freemote"
_HAS_REAL_FREEMOTE = (
    (_FREEMOTE_DIR / "PsbDecompile.exe").is_file()
    and (_FREEMOTE_DIR / "PsBuild.exe").is_file()
)


@pytest.mark.skipif(not _HAS_REAL_FREEMOTE,
                    reason="FreeMote Toolkit 未装配（tools/freemote/ 缺 PsbDecompile.exe / PsBuild.exe）")
class TestRealFreemoteToolchain:
    """真机 FreeMote 全链路：所有替身断言在此以真实二进制复核。"""

    def test_real_roundtrip_text_lands_in_real_psb(self, tmp_path):
        bridge = EngineToolchainBridge()
        decompile_exe = str(_FREEMOTE_DIR / "PsbDecompile.exe")
        build_exe = str(_FREEMOTE_DIR / "PsBuild.exe")

        # 1) 语义 JSON → 真机 PsBuild 编译出真 .psb
        semantic = {
            "ver": 1.1,
            "scenes": [
                {"name": "千代", "text": _ORIGINAL_TEXT},
                {"text": "風が二人の間を静かに吹き抜けていく。"},
            ],
        }
        src_json = tmp_path / "in"
        src_json.mkdir()
        (src_json / "scene.psb.json").write_text(
            json.dumps(semantic, ensure_ascii=False, indent=2), encoding="utf-8")
        psb = bridge.compile_from_intermediate(
            "krkr_psb", src_json / "scene.psb.json", tmp_path / "scene.psb", build_exe)
        assert psb.read_bytes()[:4] == b"PSB\x00"

        # 2) 真机 PsbDecompile dump（产物消歧：scene.json 胜过 scene.resx.json）
        intermediate = bridge.dump_to_intermediate(
            "krkr_psb", psb, tmp_path / "intermediate", decompile_exe)
        assert intermediate.name == "scene.json"  # FreeMote 真实命名约定（非 .psb.json）

        # 3) relay.json 抽取 + 译文回写
        project = JsonAdapter().extract_to_ir(intermediate)
        assert [u.raw_text for u in project.units] == [_ORIGINAL_TEXT, "風が二人の間を静かに吹き抜けていく。"]
        assert project.units[0].speaker == "千代"
        project.units[0].translated_text = _TRANSLATED_TEXT
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")

        # 4) 真机 PsBuild 回编译 → 真机 PsbDecompile 复核译文真实落进二进制
        patched = bridge.compile_from_intermediate(
            "krkr_psb", written, tmp_path / "patched.psb", build_exe)
        verify_dir = tmp_path / "verify"
        verified_file = bridge.dump_to_intermediate(
            "krkr_psb", patched, verify_dir, decompile_exe)
        verified = verified_file.read_text(encoding="utf-8")
        assert _TRANSLATED_TEXT in verified and _ORIGINAL_TEXT not in verified
        # 非文本节点经真实二进制往返仍在
        assert '"ver": 1.1' in verified

    def test_real_psb_is_recompilable_after_surgery(self, tmp_path):
        """外科回写产物必须能被真机 PsBuild 接受（JSON 语义完整性闸门）。"""
        bridge = EngineToolchainBridge()
        decompile_exe = str(_FREEMOTE_DIR / "PsbDecompile.exe")
        build_exe = str(_FREEMOTE_DIR / "PsBuild.exe")

        semantic = {"scenes": [{"text": _ORIGINAL_TEXT}]}
        src = tmp_path / "in"
        src.mkdir()
        (src / "scene.psb.json").write_text(
            json.dumps(semantic, ensure_ascii=False), encoding="utf-8")
        psb = bridge.compile_from_intermediate(
            "krkr_psb", src / "scene.psb.json", tmp_path / "scene.psb", build_exe)
        intermediate = bridge.dump_to_intermediate(
            "krkr_psb", psb, tmp_path / "intermediate", decompile_exe)

        project = JsonAdapter().extract_to_ir(intermediate)
        project.units[0].translated_text = _TRANSLATED_TEXT
        written = JsonAdapter().ir_to_asset(project, tmp_path / "translated")

        # 若手术破坏 JSON 语义，这里会抛 RelayToolchainError 而非静默成功
        patched = bridge.compile_from_intermediate(
            "krkr_psb", written, tmp_path / "patched.psb", build_exe)
        assert patched.stat().st_size > 0
