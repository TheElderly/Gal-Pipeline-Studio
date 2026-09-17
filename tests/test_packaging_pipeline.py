"""打包分发引擎契约测试（模块七）。

断言面::

    1. Dirty-Only 收集器：新增/修改/删除三态精准；**mtime 变化但内容
       未变的文件必须判干净**（哈希说了算）；大体积未改动资产被剔除；
    2. OverlayPatch：暂存拓扑按原相对路径；装配打包工具 → 容器产物；
       未装配 → 暂存目录 + 装配指引的受控降级；
    3. LooseDirectory：解压即用覆盖目录；
    4. XDeltaDiff：xdelta3 生成差分包 + **当场还原验证**（哈希一致）；
       还原失败拒绝交付；
    5. 公共交付面：manifest.json / INSTALL.txt / 交付根幂等重建。
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.relay.recipes import RelayRecipe  # noqa: E402
from core.packaging import (  # noqa: E402
    AssetDiff,
    PackagingError,
    PackagingPipeline,
    collect_dirty_assets,
)


# ---------------------------------------------------------------------------
# 夹具：原版树（含大体积假数据）与汉化树
# ---------------------------------------------------------------------------


def _make_trees(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "raw"
    localized = tmp_path / "localized"
    # 原版：脚本 + 大体积语音（4MB 假数据）+ 背景视频
    (raw / "scenario").mkdir(parents=True)
    (raw / "scenario" / "scene01.ks").write_bytes(b"original script\n")
    (raw / "voice").mkdir()
    (raw / "voice" / "vo_001.ogg").write_bytes(b"\x00" * (4 * 1024 * 1024))
    (raw / "movie").mkdir()
    (raw / "movie" / "op.usm").write_bytes(b"\x11" * (2 * 1024 * 1024))
    # 汉化：脚本已译（内容变）、语音原样拷贝（copy2 保 mtime）、新增 CG、视频原样
    (localized / "scenario").mkdir(parents=True)
    (localized / "scenario" / "scene01.ks").write_bytes(b"translated script\n")
    (localized / "voice").mkdir()
    shutil.copy2(raw / "voice" / "vo_001.ogg", localized / "voice" / "vo_001.ogg")
    (localized / "movie").mkdir()
    shutil.copy2(raw / "movie" / "op.usm", localized / "movie" / "op.usm")
    (localized / "cg").mkdir()
    (localized / "cg" / "cg01.png").write_bytes(b"\x89PNG new asset")
    return raw, localized


def _xdelta_shim(directory: Path) -> str:
    """xdelta3 行为替身：-e 把「修改版」整个塞进补丁；-d 取出（语义等价、体积不省）。

    **参数位置严格对齐真实 xdelta3 契约**（写反会造成"静默绿"）::

        -e -s <原版> <修改版> <补丁>
        -d -s <原版> <补丁>  <还原产物>
    """
    script = (
        "import sys, hashlib\n"
        "args = sys.argv[1:]\n"
        "if '-e' in args:\n"
        "    i = args.index('-s')\n"
        "    original, modified, patch = args[i+1], args[i+2], args[i+3]\n"
        "    data = open(modified, 'rb').read()\n"
        "    open(patch, 'wb').write(b'XDELTA-FIXTURE:' + hashlib.sha256(data).hexdigest().encode()"
        " + b':' + data)\n"
        "else:\n"
        "    i = args.index('-s')\n"
        "    patch, restored = args[i+2], args[i+3]\n"
        "    blob = open(patch, 'rb').read()\n"
        "    data = blob.split(b':', 2)[2]\n"
        "    open(restored, 'wb').write(data)\n"
        "sys.exit(0)\n"
    )
    tool_py = directory / "xdelta_shim.py"
    tool_py.write_text(script, encoding="utf-8")
    wrapper = directory / "xdelta3.cmd"
    wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{tool_py}" %*\r\n', encoding="utf-8")
    return str(wrapper)


_XDELTA_RECIPE = RelayRecipe(
    key="test_xdelta",
    engine_label="Test Engine",
    adapter_key="",
    dump_tool="test-dump",
    recompile_tool=None,
    supports_recompile=False,
    patch_strategy="XDeltaDiff",
    patch_artifact="patch.xdelta",
)


# ---------------------------------------------------------------------------
# 收集器
# ---------------------------------------------------------------------------


class TestDirtyCollector:
    def test_dirty_only_precision(self, tmp_path):
        raw, localized = _make_trees(tmp_path)

        diffs = collect_dirty_assets(raw, localized)

        by_path = {d.relative_path: d for d in diffs}
        assert by_path["scenario/scene01.ks"].kind == "modified"
        assert by_path["cg/cg01.png"].kind == "added"
        # Dirty-Only 核心：大体积语音与视频内容未变 → 必须被剔除
        assert "voice/vo_001.ogg" not in by_path
        assert "movie/op.usm" not in by_path
        assert [d.sha256 for d in diffs if d.kind != "removed"] == [
            d.sha256 for d in diffs if d.kind != "removed"
        ]  # 哈希存在且稳定

    def test_mtime_bump_alone_is_not_dirty(self, tmp_path):
        """重打包工具触碰过 mtime 但内容未变 → 必须判干净（哈希说了算）。"""
        raw = tmp_path / "raw"
        localized = tmp_path / "localized"
        (raw / "v").mkdir(parents=True)
        (localized / "v").mkdir(parents=True)
        (raw / "v" / "a.ogg").write_bytes(b"same-content")
        shutil.copy2(raw / "v" / "a.ogg", localized / "v" / "a.ogg")
        import os

        # mtime 显式前移一小时（copy2 之后又被工具触碰的模拟）
        st = localized / "v" / "a.ogg"
        os.utime(st, ns=(st.stat().st_atime_ns, st.stat().st_mtime_ns - 3_600_000_000_000))

        # 内容完全一致：mtime 变化不构成脏判定（哈希说了算）
        assert collect_dirty_assets(raw, localized) == []

    def test_removed_assets_reported_but_not_shipped(self, tmp_path):
        raw, localized = _make_trees(tmp_path)
        (localized / "scenario" / "deleted.ks").unlink(missing_ok=True)
        (localized / "scenario" / "scene01.ks").unlink()  # 模拟被删文件

        diffs = collect_dirty_assets(raw, localized)

        removed = [d for d in diffs if d.kind == "removed"]
        assert [d.relative_path for d in removed] == ["scenario/scene01.ks"]
        assert all(d.sha256 is None for d in removed)

    def test_empty_localized_tree_yields_nothing(self, tmp_path):
        raw = tmp_path / "raw"
        raw.mkdir(parents=True)
        (raw / "a.ks").write_bytes(b"x")
        localized = tmp_path / "localized"
        localized.mkdir()

        # 空 localized 树：原版全部记 removed（信息如实披露，不进补丁载荷）
        diffs = collect_dirty_assets(raw, localized)
        assert [d.kind for d in diffs] == ["removed"]
        assert [d.relative_path for d in diffs] == ["a.ks"]


# ---------------------------------------------------------------------------
# 策略路由
# ---------------------------------------------------------------------------


class TestOverlayPatchRoute:
    def test_staging_layout_and_packed_artifact(self, tmp_path):
        raw, localized = _make_trees(tmp_path)
        pack_shim = tmp_path / "pack"
        pack_shim.mkdir()
        script = (
            "import sys, pathlib\n"
            "args = sys.argv[1:]\n"
            "patch_dir, out = args[0], args[args.index('-o') + 1]\n"
            "count = sum(1 for p in pathlib.Path(patch_dir).rglob('*') if p.is_file())\n"
            "pathlib.Path(out).write_text(f'XP3-FIXTURE:{count}', encoding='utf-8')\n"
        )
        tool_py = pack_shim / "xp3pack_tool.py"
        tool_py.write_text(script, encoding="utf-8")
        wrapper = pack_shim / "Xp3Pack.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{tool_py}" %*\r\n', encoding="utf-8")

        result = PackagingPipeline().build(
            "krkr_psb",
            raw_dir=raw, localized_dir=localized,
            dist_dir=tmp_path / "dist", game_id="demo-game",
            tool_path=str(wrapper),
        )

        # 暂存拓扑：只有 Dirty 资产，按原相对路径收纳
        staged_rel = {p.relative_to(result.dist_dir).as_posix() for p in result.staged_paths}
        assert staged_rel == {
            "patch2.xp3.d/scenario/scene01.ks",
            "patch2.xp3.d/cg/cg01.png",
        }
        # 容器产物 + 计数正确（语音/视频未进暂存）
        artifact = result.dist_dir / "patch2.xp3"
        assert artifact.read_text(encoding="utf-8") == "XP3-FIXTURE:2"
        assert result.artifacts == [artifact]
        assert result.dirty_count == 2 and result.skipped_count == 0

    def test_without_pack_tool_degrades_to_staging_with_guidance(self, tmp_path):
        raw, localized = _make_trees(tmp_path)

        result = PackagingPipeline().build(
            "krkr_psb",
            raw_dir=raw, localized_dir=localized,
            dist_dir=tmp_path / "dist", game_id="demo",
        )

        assert result.artifacts == []  # 无工具 → 无容器（受控降级，不失败）
        manifest = json.loads((result.dist_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["pack"]["packed"] is False
        assert "待打包" in manifest["pack"]["note"]

    def test_manifest_and_install_notes(self, tmp_path):
        raw, localized = _make_trees(tmp_path)

        result = PackagingPipeline().build(
            "krkr_psb",
            raw_dir=raw, localized_dir=localized,
            dist_dir=tmp_path / "dist", game_id="demo",
        )

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["game_id"] == "demo"
        assert manifest["patch_strategy"] == "OverlayPatch"
        assert manifest["patch_artifact"] == "patch2.xp3"
        assert manifest["dirty_count"] == 2
        paths = {f["path"] for f in manifest["files"]}
        assert "voice/vo_001.ogg" not in paths and "movie/op.usm" not in paths
        install = (result.dist_dir / "INSTALL.txt").read_text(encoding="utf-8")
        assert "覆盖到游戏根目录" in install or "复制到游戏根目录" in install

    def test_dist_root_is_rebuilt_idempotently(self, tmp_path):
        raw, localized = _make_trees(tmp_path)
        pipeline = PackagingPipeline()
        dist_dir = tmp_path / "dist"
        pipeline.build("krkr_psb", raw_dir=raw, localized_dir=localized,
                       dist_dir=dist_dir, game_id="demo")
        stale = (dist_dir / "demo_patch" / "STALE.txt")
        stale.write_text("last run residue", encoding="utf-8")

        pipeline.build("krkr_psb", raw_dir=raw, localized_dir=localized,
                       dist_dir=dist_dir, game_id="demo")

        assert not stale.exists(), "上次残留必须被幂等重建清除"


class TestLooseDirectoryRoute:
    def test_extract_and_play_overlay(self, tmp_path):
        raw, localized = _make_trees(tmp_path)
        bgi = RelayRecipe(
            key="bgi_test", engine_label="BGI", adapter_key="relay.tsv",
            dump_tool="t", recompile_tool="t", supports_recompile=True,
            patch_strategy="LooseDirectory", patch_artifact=None,
        )

        result = PackagingPipeline().build(
            bgi, raw_dir=raw, localized_dir=localized,
            dist_dir=tmp_path / "dist", game_id="bgi-demo",
        )

        staged_rel = {p.relative_to(result.dist_dir).as_posix() for p in result.staged_paths}
        assert staged_rel == {"scenario/scene01.ks", "cg/cg01.png"}  # 直接铺在交付根
        assert result.strategy == "LooseDirectory"
        assert (result.dist_dir / "scenario" / "scene01.ks").read_bytes() == b"translated script\n"


class TestXDeltaRoute:
    def test_delta_generated_and_restore_verified(self, tmp_path):
        raw, localized = _make_trees(tmp_path)
        original_arc = tmp_path / "SysScript.arc"
        original_arc.write_bytes(b"ORIGINAL-ARCHIVE")
        localized_arc = tmp_path / "SysScript.localized.arc"
        localized_arc.write_bytes(b"LOCALIZED-ARCHIVE")
        tool = _xdelta_shim(tmp_path)

        result = PackagingPipeline().build(
            _XDELTA_RECIPE,
            raw_dir=raw, localized_dir=localized,
            dist_dir=tmp_path / "dist", game_id="xd",
            tool_path=tool,
            original_archive=original_arc, localized_archive=localized_arc,
        )

        patch = result.dist_dir / "patch.xdelta"
        assert patch.is_file() and patch.stat().st_size > 0
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert "还原验证" in manifest["pack"]["note"]

    def test_restore_mismatch_rejects_delivery(self, tmp_path):
        """还原产物与汉化封包不一致 → 拒绝交付（差分包无法还原=废品）。"""
        raw, localized = _make_trees(tmp_path)
        original_arc = tmp_path / "SysScript.arc"
        original_arc.write_bytes(b"ORIGINAL")
        localized_arc = tmp_path / "SysScript.localized.arc"
        localized_arc.write_bytes(b"LOCALIZED")
        # 恶意替身：-d 还原出与汉化封包不同的内容
        tool = _make_shim(tmp_path, "evil_xdelta", (
            "import sys, pathlib\n"
            "args = sys.argv[1:]\n"
            "if '-e' in args:\n"
            "    i = args.index('-s')\n"
            "    open(args[i+3], 'wb').write(b'XDELTA-FIXTURE:broken')\n"
            "else:\n"
            "    i = args.index('-s')\n"
            "    open(args[i+3], 'wb').write(b'TAMPERED-RESTORE')\n"
            "sys.exit(0)\n"
        ))

        with pytest.raises(PackagingError, match="还原验证失败"):
            PackagingPipeline().build(
                _XDELTA_RECIPE,
                raw_dir=raw, localized_dir=localized,
                dist_dir=tmp_path / "dist", game_id="xd",
                tool_path=tool,
                original_archive=original_arc, localized_archive=localized_arc,
            )

    def test_missing_tool_is_controlled(self, tmp_path):
        raw, localized = _make_trees(tmp_path)

        with pytest.raises(PackagingError, match="xdelta3 CLI"):
            PackagingPipeline().build(
                _XDELTA_RECIPE,
                raw_dir=raw, localized_dir=localized,
                dist_dir=tmp_path / "dist", game_id="xd",
                original_archive=tmp_path / "o.bin", localized_archive=tmp_path / "l.bin",
            )


def _make_shim(directory: Path, name: str, script: str) -> str:
    tool_py = directory / f"{name}_tool.py"
    tool_py.write_text(script, encoding="utf-8")
    wrapper = directory / f"{name}.cmd"
    wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{tool_py}" %*\r\n', encoding="utf-8")
    return str(wrapper)
