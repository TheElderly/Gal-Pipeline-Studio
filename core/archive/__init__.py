"""资产容器与解包器抽象（模块 1）—— 统一调用契约 + 跨进程 CLI 通道。

数据流定位::

    EngineProfile（模块 0）──选型──▶ BaseArchiveExtractor 实现
                                        │ extract(archive, out_dir)
                                        ▼
                            标准工作区 .galpipeline/raw/…

GPL 物理隔离红线（技术红线第 3 条）的落地形态：本包**只定义契约**与
**跨进程 CLI 调用通道**（``ExternalCliExtractor``），绝不链接、绝不
import 任何 GPL 解包实现（GARbro / FreeMote / YuriSizuku）—— 它们以
独立进程被调用，交付物只有落盘文件。
"""

from core.archive.base import (
    ArchiveEntry,
    ArchiveExtractorError,
    BaseArchiveExtractor,
    ExtractResult,
    ExternalToolUnavailableError,
    ExternalCliExtractor,
    KirikiriXp3Extractor,
)
from core.archive.workspace import (
    WORKSPACE_DIRNAME,
    init_workspace,
    load_workspace_profile,
    workspace_paths,
)

__all__ = [
    "ArchiveEntry",
    "ArchiveExtractorError",
    "BaseArchiveExtractor",
    "ExtractResult",
    "ExternalToolUnavailableError",
    "ExternalCliExtractor",
    "KirikiriXp3Extractor",
    "WORKSPACE_DIRNAME",
    "init_workspace",
    "load_workspace_profile",
    "workspace_paths",
]
