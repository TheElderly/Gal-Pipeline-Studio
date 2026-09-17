"""Milestone E2E Penetration —— 真实端到端全链路穿透（KiriKiri / PSB 主线）。

样本方案（诚实前提，必读）
--------------------------
仓库内**没有也不应有**商业游戏资产（版权 + 不可复现）。因此本测试的样本是
**引擎原生格式的准真实样本**：剧本容器由**真实 FreeMote PsBuild.exe** 从
语义 JSON 编译成真 ``.psb``（magic ``PSB\\0``），图像包含真实 PNG 与真实
TLG5（仓库内 tlg-rs 语义合成编码器，由 ``core/media/tlg5.py`` 解码器闭环
校验）。所有解包 / 回编译 / 差分动作**全部由真实 CLI 完成**，无任何替身：

    PsbDecompile.exe（真机 dump）· PsBuild.exe（真机回编译）
    xdelta3.exe 3.2.0（真机 encode/decode）· LEProc.exe（转区启动契约）

穿透阶段
--------
    0 样本装配：真 PsBuild 编译真 PSB + 真 PNG + 真 TLG5；
    1 资产提取：真 PsbDecompile dump → 引擎原生 JSON；
    2 文本流转：Gal-IR 抽取 → 译文注入（含控制符全携带）→ LQA 门禁 →
      外科回写 → 真机回编译 → 真机回读复核语义完整性；
    3 图像流转：TLG5 内置通道解码落 PNG + 真实图片资产进入 localized/；
    4 增量交付：Dirty-Only 收集 → OverlayPatch 暂存 + manifest/CONSTALL；
      XDeltaDiff 差分（真机生成 + 管道内还原验证 + **本测试独立回放复核**）；
    5 启动引导：LaunchConfig 免转区命令构造 / 启动器渲染 / 降级路径。

无 GUI 依赖：全流程纯命令行；真实工具缺失时整文件跳过（不伪造通过）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _TESTS_DIR.parent
for _candidate in (_REPO_ROOT, _TESTS_DIR):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from core.adapters.relay.json_relay import JsonAdapter  # noqa: E402
from core.adapters.relay.recipes import PATCH_XDELTA, RelayRecipe, get_recipe  # noqa: E402
from core.engine import EngineToolchainBridge  # noqa: E402
from core.launch.launch_config import LaunchConfig, default_le_proc_path  # noqa: E402
from core.lqa.rules import check_control_conservation  # noqa: E402
from core.media.pipeline import detect_image_format, tlg_to_png  # noqa: E402
from core.packaging.collector import collect_dirty_report  # noqa: E402
from core.packaging.pipeline import PackagingPipeline  # noqa: E402
from test_media_convert import encode_tlg5  # noqa: E402  （仓库内 TLG5 合成编码器）

# ---------------------------------------------------------------------------
# 真实工具装配探测（缺失即整文件跳过，绝不伪造通过）
# ---------------------------------------------------------------------------

_FREEMOTE = _REPO_ROOT / "tools" / "freemote"
_XDELTA = _REPO_ROOT / "tools" / "xdelta" / "xdelta3.exe"
_LEPROC = Path(default_le_proc_path(_REPO_ROOT))

_PSB_DECOMPILE = _FREEMOTE / "PsbDecompile.exe"
_PS_BUILD = _FREEMOTE / "PsBuild.exe"

_TOOLS_READY = all(path.is_file() for path in (_PSB_DECOMPILE, _PS_BUILD, _XDELTA))
_MISSING = [p.name for p in (_PSB_DECOMPILE, _PS_BUILD, _XDELTA) if not p.is_file()]

pytestmark = pytest.mark.skipif(
    not _TOOLS_READY,
    reason=f"真实工具链未装配（缺 {', '.join(_MISSING)}）；E2E 穿透需真机二进制",
)

# ---------------------------------------------------------------------------
# 样本内容：覆盖真实编码/语法边界（全角引号、KAG 宏、控制符、省略号、破折号）
# ---------------------------------------------------------------------------

_SAMPLE_SCENARIO = {
    "ver": 1.1,
    "res": {"bg": "bg/rooftop.png", "hash": 3735928559},
    "scenes": [
        {"name": "千代", "text": "「真実を知る覚悟は、もうできているの？」"},
        {"name": "千代", "text": "[ruby text=しんじつ]真実[/ruby]は、いつも静かに降りてくる。"},
        {"name": None, "text": "風が二人の間を静かに吹き抜けていく……"},
        {"name": "健一", "text": "――そうか。じゃあ、行こうか。"},
    ],
}

_ORIGINAL_LINE = "「真実を知る覚悟は、もうできているの？」"
_TRANSLATED_LINE = "「你已经做好知晓真相的觉悟了吗？」"
_RUBY_LINE = "[ruby text=しんじつ]真実[/ruby]は、いつも静かに降りてくる。"
# 控制符**全携带**形态（LQA 三态语义中的合法通过态：数量与顺序与原文一致）
_RUBY_TRANSLATED = "[ruby text=しんじつ]真相[/ruby]，总是静静地降临。"
_REPORT_PATH = _REPO_ROOT / ".pytest_temp" / "e2e_penetration_report.json"
_STAGE_LOG: dict[str, object] = {
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "tools": {
        "psb_decompile": str(_PSB_DECOMPILE),
        "ps_build": str(_PS_BUILD),
        "xdelta3": str(_XDELTA),
        "leproc": str(_LEPROC),
    },
    "stages": [],
}
_STAGE_T0 = time.perf_counter()


def _note(stage: str, message: str, **evidence: object) -> None:
    """阶段回显：控制台 + **落盘结构化报告**（含耗时与哈希证据，可归档核对）。

    Windows 宿主对 print 的捕获行为不稳定（ConPTY 下早期用例输出可能丢失），
    因此把「各阶段流转回显 + 实测耗时 + 哈希证据」写成 JSON 报告 ——
    报告里的每个数字都是本次运行真实测量值，验收报告直接引用不手写。
    """
    elapsed = time.perf_counter() - _STAGE_T0
    entry: dict[str, object] = {"stage": stage, "message": message, "elapsed_s": round(elapsed, 2)}
    entry.update(evidence)
    cast_stages = _STAGE_LOG["stages"]
    assert isinstance(cast_stages, list)
    cast_stages.append(entry)

    print(f"[E2E] {stage} · {message}（+{elapsed:.2f}s）", flush=True)
    try:
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REPORT_PATH.write_text(
            json.dumps(_STAGE_LOG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # 报告落盘失败不得影响测试判定


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 真实 PNG / TLG5 样本写出（纯标准库，零第三方依赖）
# ---------------------------------------------------------------------------

def _png_bytes(width: int, height: int, seed: int) -> bytes:
    """写出真实 PNG（8-bit RGBA，无滤波），像素由 seed 决定的确定性渐变。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            raw += bytes((
                (x * 3 + seed) & 0xFF,
                (y * 5 + seed * 2) & 0xFF,
                ((x + y) * 2 + seed * 3) & 0xFF,
                0xFF,
            ))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


def _rgba_pixels(width: int, height: int, seed: int) -> bytes:
    """与 PNG 路径同源的像素缓冲（TLG5 样本用，保证两通道可对比）。"""
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes((
                (x * 3 + seed) & 0xFF,
                (y * 5 + seed * 2) & 0xFF,
                ((x + y) * 2 + seed * 3) & 0xFF,
                0xFF,
            ))
    return bytes(out)


# ---------------------------------------------------------------------------
# 样本装配
# ---------------------------------------------------------------------------

@dataclass
class PenetrationSample:
    root: Path
    raw: Path
    localized: Path
    psb: Path
    decompiled: Path
    tlg: Path
    ui_png: Path

    @property
    def work(self) -> Path:
        """工作区：dump 中间件与回编译中转产物**必须**落在这里。

        穿透发现（2026-09-17）：中间产物一旦写进资产树，Dirty-Only 收集器
        会把 `scene.json` / `scene.psb.json` 判成「新增资产」混进补丁 ——
        真实汉化流水线最容易踩的「资产层级错配」暗礁，故固化为工作区纪律。
        """
        return self.root / "work"


def _apply_translation_and_recompile(
    sample: PenetrationSample, bridge: EngineToolchainBridge, project,
) -> Path:
    """译文 → 中间件（work/）→ 真机回编译 → 只把最终资产复制进 localized/scenario/。"""
    work_scenario = sample.work / "scenario"
    written = JsonAdapter().ir_to_asset(project, work_scenario)
    built = bridge.compile_from_intermediate(
        "krkr_psb", written, sample.work / "scene.psb", str(_PS_BUILD))
    target = sample.localized / "scenario" / "scene.psb"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, target)
    return target


def _stray_intermediates(relative_paths: list[str]) -> list[str]:
    """交付树里的中间产物（.json / *.resx.json）—— 层级错配的显式探测。"""
    return [p for p in relative_paths if p.lower().endswith(".json")]


def _assemble_sample(root: Path) -> PenetrationSample:
    """装配准真实样本：真 PsBuild 编译真 PSB + 真 PNG + 真 TLG5，并铺汉化基线树。"""
    raw = root / "raw"
    (raw / "scenario").mkdir(parents=True)
    (raw / "images").mkdir(parents=True)

    # 1) 剧本：语义 JSON → 真机 PsBuild → 真 .psb
    semantic = raw / "scenario" / "scene.psb.json"
    semantic.write_text(
        json.dumps(_SAMPLE_SCENARIO, ensure_ascii=False, indent=2), encoding="utf-8")
    psb = raw / "scenario" / "scene.psb"
    EngineToolchainBridge().compile_from_intermediate(
        "krkr_psb", semantic, psb, str(_PS_BUILD))

    # 2) UI 图：真实 PNG
    ui_png = raw / "images" / "ui_title.png"
    ui_png.write_bytes(_png_bytes(96, 64, seed=7))

    # 3) 场景底图：真实 TLG5（未压缩块写出器，语义与 tlg-rs encode 一致）
    width, height = 64, 32
    tlg = raw / "images" / "bg_scene.tlg"
    tlg.write_bytes(encode_tlg5(
        width, height, _rgba_pixels(width, height, seed=3),
        colors=4, block_height=8, compress="raw"))

    # 4) 先清中间产物，再铺汉化基线 —— **顺序即纪律**：
    #    若先 copytree 再删除，中间 JSON 会被复制进汉化树，随后以 added 身份
    #    进入补丁交付物（本文件穿透发现的真实暗礁，见 stage4 暗礁用例）。
    semantic.unlink()  # 中间 JSON 不入交付树

    localized = root / "localized"
    shutil.copytree(raw, localized)
    return PenetrationSample(
        root=root, raw=raw, localized=localized, psb=psb,
        decompiled=root / "decompiled", tlg=tlg, ui_png=ui_png)


@pytest.fixture()
def sample(tmp_path: Path) -> PenetrationSample:
    return _assemble_sample(tmp_path)


# ===========================================================================
# 阶段 0：样本装配
# ===========================================================================

class TestStage0SampleAssembly:
    """样本必须是引擎原生二进制（真机编译产物），而非伪造字节。"""

    def test_sample_psb_is_real_engine_binary(self, sample: PenetrationSample):
        blob = sample.psb.read_bytes()
        assert blob[:4] == b"PSB\x00", "样本剧本不是真 PSB 容器"
        assert sample.psb.stat().st_size > 0
        _note("阶段0", f"真机 PsBuild 产出 {sample.psb.name}（{sample.psb.stat().st_size} B）",
              psb_sha256=_sha256(sample.psb), psb_size=sample.psb.stat().st_size)

    def test_sample_images_are_real_formats(self, sample: PenetrationSample):
        assert sample.ui_png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert detect_image_format(sample.tlg.read_bytes()) == "TLG5"
        _note("阶段0", "样本图像：真 PNG + 真 TLG5（双格式）",
              png_sha256=_sha256(sample.ui_png), tlg_sha256=_sha256(sample.tlg),
              tlg_format=detect_image_format(sample.tlg.read_bytes()))


# ===========================================================================
# 阶段 1：资产提取（真 CLI）
# ===========================================================================

class TestStage1AssetExtraction:
    def test_real_psbdecompile_dump_extracts_engine_json(self, sample: PenetrationSample):
        bridge = EngineToolchainBridge()
        dumped = bridge.dump_to_intermediate(
            "krkr_psb", sample.psb, sample.decompiled, str(_PSB_DECOMPILE))

        # 真机命名约定：产物 scene.json（伴随 scene.resx.json 由 stem 消歧胜出）
        assert dumped.name == "scene.json"
        text = dumped.read_text(encoding="utf-8")
        assert _ORIGINAL_LINE in text
        assert _RUBY_LINE in text
        _note("阶段1", f"真机 dump → {dumped.name}（{dumped.stat().st_size} B）",
              dumped_sha256=_sha256(dumped), dumped_size=dumped.stat().st_size)


# ===========================================================================
# 阶段 2：文本流转（Gal-IR → LQA → 外科回写 → 真机回编译 → 真机复核）
# ===========================================================================

class TestStage2TextSurgeryAndRecompile:
    def _prepared(self, sample: PenetrationSample):
        bridge = EngineToolchainBridge()
        dumped = bridge.dump_to_intermediate(
            "krkr_psb", sample.psb, sample.decompiled, str(_PSB_DECOMPILE))
        project = JsonAdapter().extract_to_ir(dumped)
        return bridge, project

    def test_gal_ir_extraction_matches_sample_script(self, sample: PenetrationSample):
        _, project = self._prepared(sample)
        assert len(project.units) == len(_SAMPLE_SCENARIO["scenes"])
        assert [u.speaker for u in project.units] == ["千代", "千代", None, "健一"]
        assert project.units[0].raw_text == _ORIGINAL_LINE
        _note("阶段2", f"Gal-IR 抽取 {len(project.units)} 条对白（角色归属正确）",
              units=len(project.units), speakers=[u.speaker for u in project.units])

    def test_control_token_semantics_on_relay_pipeline(self, sample: PenetrationSample):
        """穿透发现：relay.json 管线与 KAG 管线的控制符语义**不同**。

        KAG 管线是「剥离式 + 宏登记表」：译文携带已登记宏 = 全携带通过。
        relay.json 管线**不登记宏**（文本整串透传，适配器不解释引擎语法），
        因此译文里出现同族 token 时必须被 advisory（warning）提示 ——
        既不静默放过，也不升级为阻断级 error。真正的守恒保证来自
        「真机回读宏字面量恰好一次」（见下一用例）。
        """
        _, project = self._prepared(sample)
        ruby_unit = next(u for u in project.units if "[ruby" in (u.raw_text or ""))
        ruby_unit.translated_text = _RUBY_TRANSLATED

        issues = check_control_conservation(ruby_unit)
        assert issues, "relay 管线未登记宏：携带宏必须给出 advisory，不得静默"
        assert {issue.severity for issue in issues} == {"warning"}, "advisory 不得升级为阻断"
        _note("阶段2", f"LQA 控制符语义：relay 管线携带宏 → {len(issues)} 条 advisory（不阻断导出）",
              advisories=[i.message for i in issues])

    def test_surgical_writeback_survives_real_recompile(self, sample: PenetrationSample):
        bridge, project = self._prepared(sample)
        for unit in project.units:
            if unit.raw_text == _ORIGINAL_LINE:
                unit.translated_text = _TRANSLATED_LINE
            elif unit.raw_text == _RUBY_LINE:
                unit.translated_text = _RUBY_TRANSLATED
            else:
                unit.translated_text = f"【译】{unit.raw_text}"

        patched = _apply_translation_and_recompile(sample, bridge, project)
        assert patched.read_bytes()[:4] == b"PSB\x00"

        # 真机回读复核：译文落进二进制，原文消失，非文本节点逐字不动
        verify_dir = sample.root / "verify"
        verified_file = bridge.dump_to_intermediate(
            "krkr_psb", patched, verify_dir, str(_PSB_DECOMPILE))
        verified = verified_file.read_text(encoding="utf-8")

        assert _TRANSLATED_LINE in verified and _ORIGINAL_LINE not in verified
        assert verified.count("[ruby text=しんじつ]") == 1, "控制符必须恰好复写一次（零重复注入）"
        assert "しんじつ" in verified and "真相" in verified
        assert '"ver": 1.1' in verified, "非文本节点（ver）真机往返后必须原样保留"
        assert "3735928559" in verified, "非文本节点（res.hash）真机往返后必须原样保留"
        _note("阶段2", f"真机回编译 + 回读复核通过（{verified_file.name}，译文在位/非文本节点守恒）",
              source_psb_sha256=_sha256(sample.psb), patched_psb_sha256=_sha256(patched),
              patched_psb_size=patched.stat().st_size,
              macro_occurrences=verified.count("[ruby text=しんじつ]"),
              non_text_nodes_preserved=('"ver": 1.1' in verified and "3735928559" in verified))


# ===========================================================================
# 阶段 3：图像流转（TLG5 通道 + 图片资产进入汉化树）
# ===========================================================================

class TestStage3MediaPipeline:
    def test_tlg5_internal_channel_decodes_to_png(self, sample: PenetrationSample):
        target = sample.root / "media" / "bg_scene.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = tlg_to_png(sample.tlg, target)

        assert meta["format"] == "TLG5" and meta["width"] == 64 and meta["height"] == 32
        assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        _note("阶段3", f"TLG5 内置通道 → PNG（{meta['width']}×{meta['height']}，{target.stat().st_size} B）",
              source_tlg_sha256=_sha256(sample.tlg), decoded_png_sha256=_sha256(target),
              decoded_size=target.stat().st_size)

    def test_external_edit_lands_in_localized_tree(self, sample: PenetrationSample):
        """模拟外部修图：改像素重编码（同名覆盖）+ 新增资产。"""
        edited = sample.localized / "images" / "ui_title.png"
        edited.write_bytes(_png_bytes(96, 64, seed=99))  # 与原版不同像素
        banner = sample.localized / "images" / "ui_banner.png"
        banner.write_bytes(_png_bytes(160, 48, seed=42))

        assert edited.read_bytes() != sample.ui_png.read_bytes()
        assert banner.is_file() and banner.stat().st_size > 0
        _note("阶段3", "图片资产入树：1 张同名覆盖（改像素）+ 1 张新增",
              original_png_sha256=_sha256(sample.ui_png), edited_png_sha256=_sha256(edited),
              added_png_sha256=_sha256(banner))


# ===========================================================================
# 阶段 4：增量与差分交付（Dirty-Only + xdelta3 真机差分）
# ===========================================================================

def _pack_container(root: Path, dest: Path, relative_paths: list[str]) -> Path:
    """确定性完整封包容器（Xp3Pack 未装配，故以纯字节容器承载差分输入）。

    容器格式与 XP3 无关，只用于**证明 xdelta 通道**：路径长度 + 路径 +
    内容长度 + 内容，按排序写入 → 同等输入必得同等字节（可复现）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as out:
        for rel in sorted(relative_paths):
            blob = (root / rel).read_bytes()
            encoded = rel.encode("utf-8")
            out.write(struct.pack("<II", len(encoded), len(blob)))
            out.write(encoded)
            out.write(blob)
    return dest


class TestStage4IncrementalDelivery:
    def _prepare_localized(self, sample: PenetrationSample) -> list[str]:
        """把阶段 2/3 的汉化产物落进 localized 树，返回期望的 Dirty 相对路径。"""
        edited = sample.localized / "images" / "ui_title.png"
        edited.write_bytes(_png_bytes(96, 64, seed=99))
        (sample.localized / "images" / "ui_banner.png").write_bytes(_png_bytes(160, 48, seed=42))
        return ["images/ui_banner.png", "images/ui_title.png", "scenario/scene.psb"]

    def test_dirty_only_collection_excludes_untouched_assets(self, sample: PenetrationSample):
        bridge, project = TestStage2TextSurgeryAndRecompile()._prepared(sample)
        for unit in project.units:
            unit.translated_text = f"【译】{unit.raw_text}"
        _apply_translation_and_recompile(sample, bridge, project)
        expected = self._prepare_localized(sample)

        report = collect_dirty_report(sample.raw, sample.localized)
        dirty = report.diffs
        dirty_paths = sorted(d.relative_path for d in dirty)

        assert dirty_paths == sorted(expected), f"Dirty 集合不符：{dirty_paths}"
        assert not any("bg_scene.tlg" in p for p in dirty_paths), "未改动资产绝不允许进入补丁"
        assert all(d.sha256 and len(d.sha256) == 64 for d in dirty if d.kind != "removed")
        assert report.intermediates == [], "规范工作流下不应产生被机拦的中间产物"
        _note("阶段4", f"Dirty-Only 收集：{len(dirty_paths)} 项（未改动 TLG5 被正确排除）",
              dirty_paths=dirty_paths, dirty_sha256={d.relative_path: d.sha256 for d in dirty},
              untouched_excluded=all("bg_scene.tlg" not in p for p in dirty_paths),
              machine_filtered_intermediates=len(report.intermediates))

    def test_stray_intermediate_is_machine_filtered_and_audited(self, sample: PenetrationSample):
        """暗礁收口（穿透发现 A + D2 机拦）：误入资产树的中间产物被自动剔除并留痕。

        2026-09-17 穿透首次暴露：dump 中间件（scene.json）与回写中间件
        （scene.psb.json）若被顺手写进游戏资产目录，会以 added 身份混进补丁。
        现在 collector 内置中间产物机拦：命中即 WARNING + 从载荷剔除 +
        manifest.excluded_intermediates 留痕 —— 剔除可见，绝不静默。
        """
        stray = sample.localized / "scenario" / "scene.psb.json"
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("{}\n", encoding="utf-8")

        report = collect_dirty_report(sample.raw, sample.localized)
        payload_paths = sorted(diff.relative_path for diff in report.payload)

        assert "scenario/scene.psb.json" not in payload_paths, "中间产物不得进入补丁载荷"
        assert [item.relative_path for item in report.intermediates] == [
            "scenario/scene.psb.json"]
        assert report.intermediates[0].matched_pattern == "*.psb.json"
        # 独立守卫（不依赖产品实现）：即便载荷被上游改动，交付面也不得出现中间产物
        assert _stray_intermediates(payload_paths) == []
        _note("阶段4", "机拦护栏：误入资产树的中间产物被剔除并留痕（不进补丁）")

    def test_overlay_patch_delivery_with_manifest(self, sample: PenetrationSample):
        bridge, project = TestStage2TextSurgeryAndRecompile()._prepared(sample)
        for unit in project.units:
            unit.translated_text = f"【译】{unit.raw_text}"
        _apply_translation_and_recompile(sample, bridge, project)
        self._prepare_localized(sample)

        dist = sample.root / "dist"
        result = PackagingPipeline().build(
            "krkr_psb", raw_dir=sample.raw, localized_dir=sample.localized,
            dist_dir=dist, game_id="e2e-penetration")

        assert result.strategy == "OverlayPatch"
        staging = dist / "e2e-penetration_patch" / "patch2.xp3.d"
        assert (staging / "scenario" / "scene.psb").is_file(), "重编译剧本必须按原相对路径入暂存"
        assert (staging / "images" / "ui_banner.png").is_file()
        assert (staging / "images" / "bg_scene.tlg").exists() is False, "干净资产不得进暂存"

        manifest = json.loads((dist / "e2e-penetration_patch" / "manifest.json").read_text("utf-8"))
        assert manifest["patch_strategy"] == "OverlayPatch"
        assert manifest["dirty_count"] == result.dirty_count == 3
        for entry in manifest["files"]:
            if entry["kind"] == "removed":
                continue
            staged = staging / entry["path"]
            assert _sha256(staged) == entry["sha256"], f"manifest 哈希与实物不符：{entry['path']}"
        assert (dist / "e2e-penetration_patch" / "INSTALL.txt").is_file()
        _note("阶段4", f"OverlayPatch 交付：暂存 {len(result.staged_paths)} 文件 + manifest 哈希逐项自洽",
              staged_files=sorted(str(x.relative_to(result.dist_dir)) for x in result.staged_paths),
              manifest_sha256_verified=len([e for e in manifest["files"] if e["kind"] != "removed"]),
              manifest_dirty_count=manifest["dirty_count"])

    def test_xdelta_diff_generation_and_independent_replay(self, sample: PenetrationSample):
        bridge, project = TestStage2TextSurgeryAndRecompile()._prepared(sample)
        for unit in project.units:
            unit.translated_text = f"【译】{unit.raw_text}"
        _apply_translation_and_recompile(sample, bridge, project)
        self._prepare_localized(sample)

        # 完整封包（原版 / 汉化）→ 真机 xdelta3 差分
        rels = sorted(
            str(p.relative_to(sample.raw)).replace("\\", "/")
            for p in sample.raw.rglob("*") if p.is_file())
        original_pack = _pack_container(sample.raw, sample.root / "packs" / "original.pack", rels)
        localized_pack = _pack_container(
            sample.localized, sample.root / "packs" / "localized.pack", rels)
        assert original_pack.read_bytes() != localized_pack.read_bytes()

        recipe = RelayRecipe(
            key="e2e_xdelta", engine_label="KiriKiri / PSB (xdelta)",
            adapter_key="relay.json", dump_tool="PsbDecompile.exe",
            recompile_tool="PsBuild.exe", supports_recompile=True,
            patch_strategy=PATCH_XDELTA, patch_artifact="e2e_patch.xdelta",
            notes="E2E 穿透专用：真机 xdelta3 差分通道（完整封包为确定性容器）。",
        )
        dist = sample.root / "dist-xdelta"
        result = PackagingPipeline().build(
            recipe, raw_dir=sample.raw, localized_dir=sample.localized,
            dist_dir=dist, game_id="e2e-penetration",
            tool_path=str(_XDELTA),
            original_archive=original_pack, localized_archive=localized_pack)

        assert result.strategy == "XDeltaDiff"
        patch = dist / "e2e-penetration_patch" / "e2e_patch.xdelta"
        assert patch.is_file() and patch.stat().st_size > 0, "差分包缺失"
        # 差分必须是「轻量」的：远小于汉化完整封包
        assert patch.stat().st_size < localized_pack.stat().st_size

        # 独立回放复核（不复用管道内部验证）：真机 xdelta3 -d → SHA-256 必须一致
        replayed = sample.root / "replay.pack"
        subprocess.run(
            [str(_XDELTA), "-d", "-f", "-s", str(original_pack), str(patch), str(replayed)],
            check=True, capture_output=True, timeout=120)
        assert _sha256(replayed) == _sha256(localized_pack), "补丁回放产物与汉化封包不一致"

        install = (dist / "e2e-penetration_patch" / "INSTALL.txt").read_text("utf-8")
        assert "xdelta3 -d" in install
        _note(
            "阶段4",
            f"XDeltaDiff：补丁 {patch.stat().st_size} B / 封包 {localized_pack.stat().st_size} B"
            f"（压缩至 {patch.stat().st_size * 100.0 / localized_pack.stat().st_size:.1f}%），"
            "独立回放哈希一致",
            original_pack_sha256=_sha256(original_pack),
            localized_pack_sha256=_sha256(localized_pack),
            patch_sha256=_sha256(patch),
            replayed_pack_sha256=_sha256(replayed),
            patch_size=patch.stat().st_size,
            localized_pack_size=localized_pack.stat().st_size,
            replay_hash_match=_sha256(replayed) == _sha256(localized_pack))


# ===========================================================================
# 阶段 5：启动引导（免转区）
# ===========================================================================

class TestStage5LaunchConfig:
    def test_locale_emulator_command_contract(self, sample: PenetrationSample):
        assert _LEPROC.is_file(), "LEProc.exe 未装配（tools/locale_emulator/）"

        config = LaunchConfig(game_executable="game.exe", le_proc_path=str(_LEPROC))
        assert config.build_command(sample.root) == [str(_LEPROC), "-run", str(sample.root / "game.exe")]

        pinned = LaunchConfig(
            game_executable="game.exe", le_proc_path=str(_LEPROC),
            locale_profile_guid="0F3B9A6C-0000-0000-0000-000000000001",
            extra_args=("--windowed",))
        command = pinned.build_command(sample.root)
        assert command[:3] == [str(_LEPROC), "-runas", "0F3B9A6C-0000-0000-0000-000000000001"]
        assert command[-1] == "--windowed", "游戏参数必须置于路径之后（LEProc 原样透传）"
        _note("阶段5", "免转区命令契约：-run / -runas <guid> 双形态与参数透传位序正确")

    def test_launcher_and_diagnostics_are_deliverable(self, sample: PenetrationSample):
        game = sample.localized / "game.exe"
        game.write_bytes(b"MZ")  # 占位游戏本体（仅用于能力诊断的物理存在性）

        config = LaunchConfig(game_executable="game.exe", le_proc_path=str(_LEPROC))
        diagnostics = config.validate(sample.localized)
        assert diagnostics.localized_launch_ready
        assert "Locale Emulator" in diagnostics.summary()

        launcher = config.write_launcher(sample.localized)
        body = launcher.read_text(encoding="utf-8")
        assert str(_LEPROC) in body and "-run" in body
        assert "回退直启" in body, "启动器必须写明降级分支（部署机未装 LE 时仍可用）"
        _note("阶段5", f"启动器已落盘 {launcher.name}；诊断：{diagnostics.summary()}")

    def test_missing_locale_emulator_degrades_to_direct_launch(self, sample: PenetrationSample):
        game = sample.localized / "game.exe"
        game.write_bytes(b"MZ")

        degraded = LaunchConfig(
            game_executable="game.exe", le_proc_path=str(sample.root / "no-such-LEProc.exe"))
        assert degraded.build_command(sample.localized) == [str(game)], "LE 缺失必须降级为直启"
        assert not degraded.validate(sample.localized).localized_launch_ready
        body = degraded.render_launcher(sample.localized)
        assert "未配置 Locale Emulator" in body
        _note("阶段5", "降级路径验证：LEProc 缺失 → 直启 + 诊断如实披露（不静默失败）")


# ===========================================================================
# 整链路：单次运行穿透全部阶段
# ===========================================================================

class TestFullChainPenetration:
    def test_single_run_penetration_across_all_stages(self, sample: PenetrationSample):
        _note("整链路", "开始单次运行穿透（无人工干预，全自动）")
        bridge = EngineToolchainBridge()

        # 1 提取
        dumped = bridge.dump_to_intermediate(
            "krkr_psb", sample.psb, sample.decompiled, str(_PSB_DECOMPILE))
        # 2 文本 + 回编译
        project = JsonAdapter().extract_to_ir(dumped)
        for unit in project.units:
            unit.translated_text = (
                _TRANSLATED_LINE if unit.raw_text == _ORIGINAL_LINE else f"【译】{unit.raw_text}")
        patched = _apply_translation_and_recompile(sample, bridge, project)
        # 3 图像
        png = sample.root / "media" / "bg_scene.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        meta = tlg_to_png(sample.tlg, png)
        (sample.localized / "images" / "ui_title.png").write_bytes(_png_bytes(96, 64, seed=99))
        (sample.localized / "images" / "ui_banner.png").write_bytes(_png_bytes(160, 48, seed=42))
        # 4 交付
        dist = sample.root / "dist-full"
        result = PackagingPipeline().build(
            "krkr_psb", raw_dir=sample.raw, localized_dir=sample.localized,
            dist_dir=dist, game_id="e2e-full")
        staging = dist / "e2e-full_patch" / "patch2.xp3.d"
        # 5 启动引导
        game = sample.localized / "game.exe"
        game.write_bytes(b"MZ")
        launcher = LaunchConfig(
            game_executable="game.exe", le_proc_path=str(_LEPROC)).write_launcher(sample.localized)

        # 全链路不变量
        assert patched.read_bytes()[:4] == b"PSB\x00"
        assert meta["format"] == "TLG5" and png.stat().st_size > 0
        assert result.strategy == "OverlayPatch" and result.dirty_count == 3
        assert (staging / "scenario" / "scene.psb").is_file()
        delivered = sorted(
            str(p.relative_to(staging)).replace("\\", "/")
            for p in staging.rglob("*") if p.is_file())
        assert not _stray_intermediates(delivered), f"中间产物混入交付树：{delivered}"
        assert launcher.is_file() and "-run" in launcher.read_text(encoding="utf-8")

        _note(
            "整链路",
            f"穿透完成：真机 dump → IR({len(project.units)} 条) → 回编译 → TLG5/PNG → "
            f"Dirty {result.dirty_count} 项 → 暂存补丁 → 免转区启动器（{patched.stat().st_size} B PSB）")
