"""分发包启动引导：免转区拉起（Locale Emulator 通道）。

模块边界::

    * 本模块**只做启动描述与命令构造**，不启动任何进程 —— 拉起动作由壳层
      （桌面端 / 安装脚本）执行，符合「Python 侧零进程特例」的既有纪律；
    * LEProc 属部署机外部工具（tools/locale_emulator/），不在补丁分发包内：
      缺失时**自动降级为直启**并把降级原因写进诊断，绝不静默失败。

LEProc 命令行契约（**取自 LEProc.exe 二进制内嵌 Usage 原文**，非文档臆测）::

    Usage: LEProc.exe
        path
        -run path [args]            Run an application with it's own profile.
        -runas guid path [args]     Run an application with a global profile of specific Guid.
        -manage path                Modify the profile of one application.
        -global                     Open Global Profile Manager.

    profile 选择顺序（LEProc 内建三级回退）：
    (i) 应用自身 profile → (ii) 首个全局 profile → (iii) 默认 ja-JP profile。
"""

from .launch_config import (
    DEFAULT_LOCALE_PROFILE_GUID,
    LE_PROC_NAME,
    LaunchConfig,
    LaunchDiagnostics,
)

__all__ = [
    "DEFAULT_LOCALE_PROFILE_GUID",
    "LE_PROC_NAME",
    "LaunchConfig",
    "LaunchDiagnostics",
]
