"""解包器抽象基类与跨进程 CLI 通道 —— 引擎特异性与模块 1 之间的唯一接缝。

契约与 ``core/adapters/base.py``（脚本适配器）刻意同构但相互独立：
适配器回答「怎么把脚本变成 Gal-IR」，解包器回答「怎么把封包变成标准
工作区」—— 两者是流水线的前后两级，不是一个抽象的两半。

实现方规范性约束（MUST / MUST NOT）：

* extract 必须以独立子进程承载外部工具（GPL 物理隔离），本包内的
  Python 代码绝不复制/链接 GPL 实现；
* 子进程必须有超时与输出目录校验 —— 外部工具挂死或静默空产出都
  必须以异常收场，严禁把半成品当成功交付；
* 一切失败派生自 :class:`ArchiveExtractorError`（其自身派生自
  ``EngineAdapterError``），供 JSON-RPC 错误映射层统一识别。
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from core.adapters.base import EngineAdapterError


class ArchiveExtractorError(EngineAdapterError):
    """解包失败：工具不可用、超时、非零退出或产出缺失。"""


class ExternalToolUnavailableError(ArchiveExtractorError):
    """跨进程通道已打通、但外部解包工具未装配（未给出可执行文件路径）。"""


@dataclass(frozen=True)
class ArchiveEntry:
    """封包条目（list 阶段的只读视图；真正的枚举由外部工具完成）。"""

    path: str
    size: int


@dataclass(frozen=True)
class ExtractResult:
    """一次解包的交付：产物根目录 + 落盘文件计数。"""

    output_dir: Path
    file_count: int


class BaseArchiveExtractor(ABC):
    """封包解包器抽象基类：一种封包格式对应一个实现。"""

    @abstractmethod
    def extract(self, archive_path: Path, output_dir: Path) -> ExtractResult:
        """把 archive_path 解包到 output_dir，返回产物清单统计。"""


class ExternalCliExtractor(BaseArchiveExtractor):
    """跨进程 CLI 解包通道（GPL 隔离的唯一形态）。

    子类只需给出**命令模板**：可执行文件路径 + 参数列表，其中
    ``{archive}`` / ``{output}`` 占位符由本类在调用时替换。进程以
    ``timeout_seconds`` 治理挂死；退出码非零或产出目录无文件均判失败。

    工具路径是**显式注入**的（构造参数），默认 ``None`` —— 通道就绪
    而工具未装配时抛 :class:`ExternalToolUnavailableError`，把「缺装
    依赖」与「解包失败」两种状态严格分开，绝不静默假装成功。
    """

    def __init__(
        self,
        engine_id: str,
        executable: str | None = None,
        arguments: tuple[str, ...] = ("{archive}", "-o", "{output}"),
        timeout_seconds: float = 600.0,
    ) -> None:
        self.engine_id = engine_id
        self.executable = executable
        self.arguments = arguments
        self.timeout_seconds = timeout_seconds

    def _render_command(self, archive_path: Path, output_dir: Path) -> list[str]:
        if not self.executable:
            raise ExternalToolUnavailableError(
                f"{self.engine_id} 的跨进程解包通道已就绪，但尚未装配外部工具："
                "请在设置或调用方显式注入可执行文件路径（GPL 隔离：仅 CLI 调用，"
                "本工程不内置、不链接其代码）"
            )
        rendered = [
            arg.replace("{archive}", str(archive_path)).replace("{output}", str(output_dir))
            for arg in self.arguments
        ]
        return [self.executable, *rendered]

    def extract(self, archive_path: Path, output_dir: Path) -> ExtractResult:
        archive_path = Path(archive_path)
        output_dir = Path(output_dir)
        if not archive_path.is_file():
            raise ArchiveExtractorError(f"封包不存在：{archive_path}")

        command = self._render_command(archive_path, output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ArchiveExtractorError(
                f"解包工具超时（>{self.timeout_seconds:.0f}s）已终止："
                f"{self.executable} {archive_path.name}"
            ) from exc
        except OSError as exc:
            raise ArchiveExtractorError(
                f"解包工具无法启动（{self.executable}）：{exc}"
            ) from exc

        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise ArchiveExtractorError(
                f"解包工具退出码 {completed.returncode}：{self.executable} "
                f"{archive_path.name} — {stderr[-400:] or '（无 stderr 输出）'}"
            )

        produced = sum(1 for p in output_dir.rglob("*") if p.is_file())
        if produced == 0:
            raise ArchiveExtractorError(
                f"解包工具报告成功但产出目录为空：{output_dir} —— 拒绝把空产出当成功交付"
            )
        return ExtractResult(output_dir=output_dir, file_count=produced)


class KirikiriXp3Extractor(ExternalCliExtractor):
    """KiriKiri .xp3 解包通道：GARbro CLI 预留位（工具路径显式注入后即激活）。

    典型装配：``KirikiriXp3Extractor(executable="garbro.exe", arguments=("x", "{archive}", "-o", "{output}"))``
    —— 本工程只发起子进程并校验产物，与 GPL 实现零代码耦合。
    """

    def __init__(self, executable: str | None = None, **kwargs) -> None:
        super().__init__(
            engine_id="kirikiri",
            executable=executable,
            arguments=kwargs.pop("arguments", ("x", "{archive}", "-o", "{output}")),
            **kwargs,
        )
