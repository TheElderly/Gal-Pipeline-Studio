"""引擎判定器（Engine Profiler）—— 模块 0：游戏根目录 → 结构化引擎画像。

数据流定位::

    游戏根目录（拖入整机目录，而非单个脚本）
        │ profile_engine(game_dir)
        ▼
    EngineProfile（engine_type / confidence / 匹配依据 / 推荐解包器）
        │ 供 core/archive 的解包器选型与 .galpipeline/project.json 落盘
        ▼
    模块 1：资产解包（跨进程 CLI，GPL 物理隔离）

指纹策略（参考 YuriSizuku / VNTranslationTools 的分层证据思路）：

* **目录特征**：特征封包名（``data.xp3`` / ``*.arc`` / ``*.int`` / ``*.mj``）
  与特征子目录（``plugin/``）—— 权重最高，封包是引擎身份的硬证据；
* **PE 字符串**：根目录可执行文件前 64KB 内的引擎特征字节
  （``KiriKiri`` / ``Ethornell`` / ``Majiro`` / ``CatSystem``）—— 次级证据，
  覆盖「封包改名」的混淆盘；
* **置信度**：命中证据权重求和并夹取到 [0, 1]，多引擎并存时取最高分，
  平分按规则注册序（确定可复现，绝不随机挑选）。

纪律：纯只读侦察 —— 只 list 目录、只读文件头部字节，不执行任何游戏
程序、不解包、不写盘；全部证据（命中了什么、为何判定）进 Profile，
判定过程可审计。
"""

from core.profiler.engine_profiler import (
    EngineEvidence,
    EngineProfile,
    ProfileEngineError,
    profile_engine,
)

__all__ = [
    "EngineEvidence",
    "EngineProfile",
    "ProfileEngineError",
    "profile_engine",
]
