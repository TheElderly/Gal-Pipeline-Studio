"""逆向中间产物机拦护栏测试（collector 资产边界防御）。

背景
----
2026-09-17 真实 E2E 穿透发现：dump/回写中间件（``scene.json`` /
``scene.psb.json``）一旦落进汉化资产树，会以 ``added`` 身份混进补丁交付物
（Dirty 由 3 项膨胀到 5 项）。本文件把「机拦剔除 + 留痕可见」固化为回归护栏，
并同时钉住**最小误伤判据**：

* 只拦「原版树中不存在」的新增文件 —— 原版树里本来就有的 ``.json``
  属引擎原生资产，改动它可能是合法汉化，绝不擅自剔除；
* 剔除必须有 WARNING 日志 + manifest 留痕（``excluded_intermediates``），
  绝不静默；
* 确有引擎把新增 ``.json`` 当原生资产时，``keep_globs`` 可显式放行。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.packaging.collector import (  # noqa: E402
    INTERMEDIATE_PATTERNS,
    classify_intermediate,
    collect_dirty_assets,
    collect_dirty_report,
)
from core.packaging.pipeline import PackagingPipeline  # noqa: E402

_LOGGER = "galpipeline.packaging.collector"


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "raw"
    localized = tmp_path / "localized"
    for root in (raw, localized):
        (root / "scenario").mkdir(parents=True)
        (root / "images").mkdir(parents=True)
        (root / "scenario" / "scene.ks").write_text("original", encoding="utf-8")
        (root / "images" / "cg01.png").write_bytes(b"PNG original")
    return raw, localized


# ---------------------------------------------------------------------------
# 判定单元：样式语义与优先级
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("relative", "expected_pattern"),
    [
        ("scenario/scene.psb.json", "*.psb.json"),
        ("scenario/scene.resx.json", "*.resx.json"),
        ("scenario/scene.hazuki.txt", "*.hazuki.txt"),
        ("dump/scene.dump", "*.dump"),
        ("tmp/scratch.tmp", "*.tmp"),
        ("scenario/scene.ks.bak", "*.bak"),
        ("scenario/scene.ks~", "*~"),
        ("data/view.json", "*.json"),
    ],
)
def test_classify_matches_expected_pattern(relative: str, expected_pattern: str):
    """具体样式优先于兜底样式：命中理由必须精确（可读的剔除留痕）。"""
    hit = classify_intermediate(relative)
    assert hit is not None, f"{relative} 应被判定为中间产物"
    assert hit[0] == expected_pattern
    assert hit[1], "命中必须携带语义说明"


def test_normal_assets_are_not_classified():
    """正常资产（脚本 / 图像 / 音频）绝不能被误判。"""
    for relative in (
        "scenario/scene.ks", "images/cg01.png", "voice/line001.ogg",
        "images/ui_title.tlg", "movie/op.wmv",
    ):
        assert classify_intermediate(relative) is None, f"{relative} 被误判为中间产物"


# ---------------------------------------------------------------------------
# 机拦行为：剔除 + WARNING 日志 + 留痕
# ---------------------------------------------------------------------------

def test_intermediates_are_filtered_with_warning(tmp_path, caplog):
    raw, localized = _trees(tmp_path)
    stray = localized / "scenario" / "scene.psb.json"
    stray.write_text("{}", encoding="utf-8")
    (localized / "scenario" / "scene.json").write_text("{}", encoding="utf-8")
    (localized / "images" / "cg01.png").write_bytes(b"PNG modified")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        report = collect_dirty_report(raw, localized)

    payload_paths = sorted(diff.relative_path for diff in report.payload)
    assert payload_paths == ["images/cg01.png"], f"中间产物必须被剔除：{payload_paths}"
    assert sorted(item.relative_path for item in report.intermediates) == [
        "scenario/scene.json", "scenario/scene.psb.json"]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("scene.psb.json" in r.getMessage() for r in warnings), "剔除必须留 WARNING"
    assert any("共剔除 2 个" in r.getMessage() for r in warnings), "必须给出汇总告警"


def test_legacy_collect_dirty_assets_also_filters(tmp_path):
    """兼容入口同样已剔净（旧调用方不会重新踩雷）。"""
    raw, localized = _trees(tmp_path)
    (localized / "data").mkdir()
    (localized / "data" / "index.json").write_text("{}", encoding="utf-8")

    diffs = collect_dirty_assets(raw, localized)
    assert diffs == [], "新增 .json 中间件不得出现在兼容入口的返回里"


def test_modified_json_existing_in_raw_is_kept_as_asset(tmp_path):
    """最小误伤判据：原版树中存在的 .json 是引擎原生资产，改它不剔除。"""
    raw, localized = _trees(tmp_path)
    (raw / "data").mkdir()
    (localized / "data").mkdir()
    (raw / "data" / "script.json").write_text('{"v": 1}', encoding="utf-8")
    (localized / "data" / "script.json").write_text('{"v": 1, "zh": "译文"}', encoding="utf-8")

    report = collect_dirty_report(raw, localized)
    assert [d.relative_path for d in report.payload] == ["data/script.json"]
    assert report.payload[0].kind == "modified"
    assert report.intermediates == [], "原生资产绝不能被机拦剔除"


def test_keep_globs_whitelists_new_json_assets(tmp_path):
    """白名单放行：确属原生资产的新增 .json 可按资产交付（显式声明）。"""
    raw, localized = _trees(tmp_path)
    (localized / "data").mkdir()
    (localized / "data" / "extra.json").write_text("{}", encoding="utf-8")

    report = collect_dirty_report(raw, localized)
    assert report.intermediates, "默认应被机拦"

    whitelisted = collect_dirty_report(raw, localized, keep_globs=("extra.json",))
    assert [d.relative_path for d in whitelisted.payload] == ["data/extra.json"]
    assert whitelisted.intermediates == []


def test_hazuki_and_dump_variants_are_filtered(tmp_path):
    """覆盖其余样式：BGI Hazuki 项目中间件 / 转储 / 临时文件。"""
    raw, localized = _trees(tmp_path)
    (localized / "scenario").mkdir(exist_ok=True)
    (localized / "scenario" / "sys.hazuki.txt").write_text("proj", encoding="utf-8")
    (localized / "scenario" / "raw.dump").write_text("dump", encoding="utf-8")
    (localized / "scenario" / ".verify.tmp").write_text("tmp", encoding="utf-8")

    report = collect_dirty_report(raw, localized)
    assert report.payload == []
    assert {item.matched_pattern for item in report.intermediates} == {
        "*.hazuki.txt", "*.dump", "*.tmp"}


# ---------------------------------------------------------------------------
# 管道集成：交付面留痕
# ---------------------------------------------------------------------------

def test_pipeline_manifest_records_exclusions(tmp_path):
    raw, localized = _trees(tmp_path)
    # 真实汉化产物：脚本被改 + 图片被改
    (localized / "scenario" / "scene.ks").write_text("translated", encoding="utf-8")
    (localized / "images" / "cg01.png").write_bytes(b"PNG modified")
    # 被顺手写进资产树的中间件
    (localized / "scenario" / "scene.psb.json").write_text("{}", encoding="utf-8")
    (localized / "scenario" / "scene.resx.json").write_text("{}", encoding="utf-8")

    result = PackagingPipeline().build(
        "krkr_psb", raw_dir=raw, localized_dir=localized,
        dist_dir=tmp_path / "dist", game_id="guard-check")

    assert result.dirty_count == 2, "中间产物不得计入交付资产数"
    assert sorted(result.excluded_intermediates) == [
        "scenario/scene.psb.json", "scenario/scene.resx.json"]

    manifest = json.loads((result.dist_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dirty_count"] == 2
    excluded = {item["path"]: item for item in manifest["excluded_intermediates"]}
    assert set(excluded) == {"scenario/scene.psb.json", "scenario/scene.resx.json"}
    assert excluded["scenario/scene.psb.json"]["matched_pattern"] == "*.psb.json"
    assert excluded["scenario/scene.psb.json"]["reason"]

    staged = sorted(
        str(p.relative_to(result.dist_dir)).replace("\\", "/")
        for p in result.dist_dir.rglob("*") if p.is_file())
    assert not [p for p in staged if p.endswith(".json") and "manifest" not in p], (
        f"暂存树不得出现中间产物：{staged}")


def test_pipeline_survives_tree_with_only_intermediates(tmp_path):
    """极端情形：这棵树的全部新增都是中间件 → 载荷为空，受控交付而非崩溃。"""
    raw, localized = _trees(tmp_path)
    (localized / "scenario" / "scene.psb.json").write_text("{}", encoding="utf-8")

    result = PackagingPipeline().build(
        "krkr_psb", raw_dir=raw, localized_dir=localized,
        dist_dir=tmp_path / "dist", game_id="only-intermediates")

    assert result.dirty_count == 0
    assert result.excluded_intermediates == ["scenario/scene.psb.json"]
    assert (result.dist_dir / "manifest.json").is_file()


def test_pattern_list_is_documented_and_ordered():
    """过滤清单必须带语义且具体样式在前（保证命中理由可读、可审计）。"""
    assert INTERMEDIATE_PATTERNS, "过滤清单不得为空"
    assert all(reason for _, reason in INTERMEDIATE_PATTERNS), "每条样式都要有语义说明"
    patterns = [p for p, _ in INTERMEDIATE_PATTERNS]
    assert patterns[-1] == "*.json", "兜底样式必须排在最后"
    assert "*.psb.json" in patterns and "*.resx.json" in patterns
