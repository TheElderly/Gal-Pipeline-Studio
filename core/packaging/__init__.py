"""打包分发引擎 —— Dirty-Only 补丁的收集、路由与交付（模块七）。"""

from .collector import (
    INTERMEDIATE_PATTERNS,
    AssetDiff,
    DirtyCollection,
    IntermediateArtifact,
    classify_intermediate,
    collect_dirty_assets,
    collect_dirty_report,
)
from .pipeline import PackagingError, PackagingPipeline, PackagingResult

__all__ = [
    "INTERMEDIATE_PATTERNS",
    "AssetDiff",
    "DirtyCollection",
    "IntermediateArtifact",
    "PackagingError",
    "PackagingPipeline",
    "PackagingResult",
    "classify_intermediate",
    "collect_dirty_assets",
    "collect_dirty_report",
]
