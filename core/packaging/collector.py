"""资产变动收集器 —— Dirty-Only 极小体积纪律的第一道闸。

原则：补丁里**只允许出现真正变化的东西**。未改动的语音/视频等大体积
资产绝不进入交付物 —— 这不是优化，是纪律。

判定算法（两级）::

    1. 快路径：size 与 mtime_ns 都与原版一致 → 视为未改动（拷贝
       语义下 copy2 保留 mtime，合法快捷）；
    2. 精路径：其余一律 SHA-256 内容比对 —— mtime 变了但内容没变
       的文件（重打包工具触碰过、又被还原）必须判「干净」，
       **mtime 永远不作为脏判定的依据，只作为免哈希的快捷通道**。

逆向中间产物机拦（2026-09-17 真实穿透发现后补强）::

    汉化流水线的 dump/回写中间件（``scene.json`` / ``scene.psb.json`` /
    ``*.hazuki.txt`` …）一旦被顺手写进游戏资产目录，就会以 ``added``
    身份混进补丁、交付给终端用户（实测 Dirty 由 3 项膨胀到 5 项）。

    拦截规则采用**最小误伤判据**：

    * 只拦 ``added``（原版树中不存在）且命中中间产物样式的文件 ——
      原版树里本来就有的 ``.json`` 属于**引擎原生资产**，改动它可能是
      合法汉化，绝不擅自剔除；
    * 命中即记 WARNING 日志（含命中样式与原因）+ 从补丁载荷剔除，
      并在 manifest 里留痕（``excluded_intermediates``）——
      「剔除」永远是可见的，不静默；
    * 确有引擎把 ``.json`` 当原生资产且**新增**时，用 ``keep_globs``
      显式白名单放行（此时视为资产而非中间件）。
"""

import fnmatch
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

_KIND_ADDED = "added"
_KIND_MODIFIED = "modified"
_KIND_REMOVED = "removed"

logger = logging.getLogger("galpipeline.packaging.collector")

# 中间产物过滤清单：(样式, 语义) —— 顺序敏感：具体样式在前，兜底样式在后，
# 命中即用第一个匹配的语义作为剔除理由（日志可读性）。
INTERMEDIATE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("*.resx.json", "FreeMote 反编译伴生元数据（资源索引）"),
    ("*.psb.json", "PSB 语义中间件（回编译输入）"),
    ("*.hazuki.txt", "BGI Hazuki 项目中间件（apply 输入）"),
    ("*.dump", "工具原始转储"),
    ("*.tmp", "临时文件"),
    ("*~", "编辑器备份"),
    ("*.orig", "合并冲突残留"),
    ("*.bak", "编辑器备份"),
    ("*.json", "通用 JSON 中间件（索引 / 视图 / 配置中间产物）"),
)


@dataclass(frozen=True)
class AssetDiff:
    """单个资产的变动记录（相对路径以 localized 树为基准）。

    ``source`` 指向 localized 树中的实际文件（打包管道据此复制/差分），
    随记录一起流转 —— 严禁用模块级注册表在收集与打包之间传状态。
    """

    relative_path: str
    kind: str  # added / modified / removed
    size: int
    sha256: str | None  # removed 时为 None
    source: Path


@dataclass(frozen=True)
class IntermediateArtifact:
    """被机拦剔除的中间产物（可审计的剔除留痕）。"""

    relative_path: str
    matched_pattern: str
    reason: str


@dataclass(frozen=True)
class DirtyCollection:
    """一次收集的完整结果：可交付变动 + 被剔除的中间产物。"""

    diffs: list[AssetDiff] = field(default_factory=list)
    intermediates: list[IntermediateArtifact] = field(default_factory=list)

    @property
    def payload(self) -> list[AssetDiff]:
        """进入补丁载荷的变动（removed 仅在 manifest 披露，不入载荷）。"""
        return [diff for diff in self.diffs if diff.kind != _KIND_REMOVED]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_intermediate(
    relative_path: str, *, keep_globs: Sequence[str] = (),
) -> tuple[str, str] | None:
    """判定相对路径是否属于逆向中间产物。

    返回 ``(命中样式, 语义)``；不属于中间产物（或命中白名单）返回 None。
    """
    name = Path(relative_path).name
    for pattern in keep_globs:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_path, pattern):
            return None  # 显式白名单：视为资产
    for pattern, reason in INTERMEDIATE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return pattern, reason
    return None


def _index_tree(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file()
    }


def _collect(
    raw_dir: str | Path, localized_dir: str | Path, keep_globs: Sequence[str],
) -> DirtyCollection:
    raw_root = Path(raw_dir)
    localized_root = Path(localized_dir)
    if not localized_root.is_dir():
        return DirtyCollection()

    raw_index = _index_tree(raw_root)
    diffs: list[AssetDiff] = []
    intermediates: list[IntermediateArtifact] = []

    for localized_path in sorted(localized_root.rglob("*")):
        if not localized_path.is_file():
            continue
        relative = localized_path.relative_to(localized_root).as_posix()
        raw_path = raw_index.get(relative)
        new_stat = localized_path.stat()

        if raw_path is None:
            # 机拦：仅对「原版树中不存在」的新增文件判定中间产物样式。
            # 原版树里存在的同类文件属引擎原生资产，改动它可能是合法汉化，
            # 绝不擅自剔除（最小误伤判据）。
            hit = classify_intermediate(relative, keep_globs=keep_globs)
            if hit is not None:
                pattern, reason = hit
                intermediates.append(IntermediateArtifact(relative, pattern, reason))
                logger.warning(
                    "剔除逆向中间产物，不进入补丁载荷：%s（命中样式 %s，%s）",
                    relative, pattern, reason,
                )
                continue
            diffs.append(AssetDiff(relative, _KIND_ADDED, new_stat.st_size,
                                   _sha256(localized_path), localized_path))
            continue

        raw_stat = raw_path.stat()
        if (raw_stat.st_size == new_stat.st_size
                and raw_stat.st_mtime_ns == new_stat.st_mtime_ns):
            continue  # 快路径：size + mtime 双一致，免哈希

        if _sha256(raw_path) != _sha256(localized_path):
            diffs.append(AssetDiff(relative, _KIND_MODIFIED, new_stat.st_size,
                                   _sha256(localized_path), localized_path))
        # 哈希一致：内容没变（仅 mtime 被触碰），Dirty-Only 判干净

    for relative, raw_path in sorted(raw_index.items()):
        if not (localized_root / relative).exists():
            diffs.append(AssetDiff(relative, _KIND_REMOVED, raw_path.stat().st_size,
                                   None, raw_path))

    if intermediates:
        logger.warning(
            "本次打包共剔除 %d 个逆向中间产物（已留痕于 manifest.excluded_intermediates）：%s",
            len(intermediates),
            ", ".join(item.relative_path for item in intermediates),
        )

    return DirtyCollection(diffs=diffs, intermediates=intermediates)


def collect_dirty_report(
    raw_dir: str | Path, localized_dir: str | Path, *, keep_globs: Sequence[str] = (),
) -> DirtyCollection:
    """收集变动并给出完整报告（含被剔除的中间产物留痕）。

    :param keep_globs: 显式白名单（如某引擎原生资产就是 ``*.json``）——
        命中白名单的文件按**资产**处理，不做中间产物剔除。
    """
    return _collect(raw_dir, localized_dir, keep_globs)


def collect_dirty_assets(
    raw_dir: str | Path, localized_dir: str | Path, *, keep_globs: Sequence[str] = (),
) -> list[AssetDiff]:
    """比对原版解包树与汉化产物树，挑出真正被修改/新增的资产。

    返回按相对路径排序的变动清单（added + modified；removed 单独以
    kind 标记供 manifest 披露，但**不进入补丁载荷**——增量覆盖无法
    表达删除）。逆向中间产物在返回前已被机拦剔除（WARNING 走 logging，
    结构化留痕请用 :func:`collect_dirty_report`）。两棵树都不存在对应
    文件时返回空表。
    """
    return _collect(raw_dir, localized_dir, keep_globs).diffs
