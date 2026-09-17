"""启动引导配置（LaunchConfig）—— 分发包「一键免转区拉起」的唯一事实来源。

为什么不把命令行散落在安装脚本里：转区启动有三个易错点，必须集中表达并
可静态核对 ——

1. **profile 选择**：``-run`` 依赖 LEProc 的「自身 profile → 首个全局
   profile → 默认 ja-JP」三级回退；要确定性则必须显式 ``-runas <guid>``。
2. **路径与参数边界**：游戏路径、游戏自身参数、LEProc 开关三者混排，
   任何拼接都会在含空格路径（真实雷区：``Gal-Pipeline Studio``）上崩掉，
   因此对外只暴露 **argv 列表**，字符串拼接仅存在于 .bat 渲染这一处。
3. **降级纪律**：部署机没装 Locale Emulator 时，必须仍然可用（直启），
   并且把「为什么没用转区」写清楚 —— 静默降级比不能启动更糟。
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

LE_PROC_NAME = "LEProc.exe"
"""Locale Emulator 的启动器进程名（外部工具，位于 tools/locale_emulator/）。"""

DEFAULT_LOCALE_PROFILE_GUID: str | None = None
"""缺省不使用 GUID 强绑定：交给 LEProc 的三级 profile 回退。

需要确定性时必须显式传入 ``-runas`` 的 GUID（从部署机的 LEConfig.xml 读取）。
"""


@dataclass
class LaunchDiagnostics:
    """启动能力的静态诊断（不启动进程，只回答「能不能拉起」）。"""

    game_executable_found: bool
    le_proc_available: bool
    used_locale_emulator: bool
    messages: list[str] = field(default_factory=list)

    @property
    def localized_launch_ready(self) -> bool:
        """是否具备「一键免转区」能力（游戏本体 + LEProc 都在）。"""
        return self.game_executable_found and self.le_proc_available

    def summary(self) -> str:
        return "；".join(self.messages) if self.messages else "启动配置有效"


@dataclass(frozen=True)
class LaunchConfig:
    """分发包启动引导配置。

    :param game_executable: 游戏可执行文件（相对分发包根的路径或绝对路径）。
    :param le_proc_path: 部署机上的 ``LEProc.exe`` 路径；None/不存在 → 直启降级。
    :param locale_profile_guid: 全局 profile GUID（``-runas`` 用）；None → ``-run``。
    :param extra_args: 传给**游戏本体**的附加参数（LEProc 会原样透传）。
    :param working_dir: 工作目录（相对分发包根解析）。
    """

    game_executable: str
    le_proc_path: str | None = None
    locale_profile_guid: str | None = DEFAULT_LOCALE_PROFILE_GUID
    extra_args: tuple[str, ...] = ()
    working_dir: str | None = None

    # ------------------------------------------------------------------
    # 命令构造
    # ------------------------------------------------------------------

    def build_command(self, dist_root: str | Path | None = None) -> list[str]:
        """构造拉起 argv（免转区优先，LE 不可用则直启）。"""
        game = self._resolve(self.game_executable, dist_root)
        extra = [str(arg) for arg in self.extra_args]

        le_proc = self._resolve_optional(self.le_proc_path, dist_root)
        if le_proc is None:
            return [game, *extra]

        if self.locale_profile_guid:
            return [le_proc, "-runas", self.locale_profile_guid, game, *extra]
        return [le_proc, "-run", game, *extra]

    def uses_locale_emulator(self, dist_root: str | Path | None = None) -> bool:
        return self._resolve_optional(self.le_proc_path, dist_root) is not None

    def render_launcher(self, dist_root: str | Path | None = None) -> str:
        """渲染一键启动器（.bat）：转区优先 + 直启回退，两支都写清楚。"""
        game = self._resolve(self.game_executable, dist_root)
        le_proc = self._resolve_optional(self.le_proc_path, dist_root)
        extra = " ".join(shlex.quote(str(arg)) for arg in self.extra_args)
        cwd_line = (
            f'cd /d "{self._resolve(self.working_dir, dist_root)}"\n'
            if self.working_dir else ""
        )

        lines = [
            "@echo off",
            "rem 本启动器由 Gal-Pipeline Studio 生成：优先免转区（Locale Emulator），",
            "rem 未检测到 LEProc 时自动直启（若游戏本体无需转区，直启同样可用）。",
            "chcp 65001 >nul",
            cwd_line.rstrip("\n"),
        ]
        if le_proc:
            if self.locale_profile_guid:
                le_argv = f'"{le_proc}" -runas {self.locale_profile_guid} "{game}" {extra}'.rstrip()
            else:
                le_argv = f'"{le_proc}" -run "{game}" {extra}'.rstrip()
            lines += [
                f'if exist "{le_proc}" (',
                f"    {le_argv}",
                "    exit /b %errorlevel%",
                ")",
                "echo [warn] 未找到 Locale Emulator，回退直启。",
            ]
        else:
            lines.append("rem 未配置 Locale Emulator：直接启动游戏本体。")
        lines.append(f'start "" "{game}" {extra}'.rstrip())
        return "\r\n".join(line for line in lines if line != "") + "\r\n"

    def write_launcher(
        self, dist_root: str | Path, filename: str = "启动游戏.bat",
    ) -> Path:
        """把启动器写入分发包根（幂等覆盖）。"""
        root = Path(dist_root)
        root.mkdir(parents=True, exist_ok=True)
        target = root / filename
        target.write_text(self.render_launcher(root), encoding="utf-8")
        return target

    # ------------------------------------------------------------------
    # 静态诊断
    # ------------------------------------------------------------------

    def validate(self, dist_root: str | Path | None = None) -> LaunchDiagnostics:
        """回答「分发包是否具备一键免转区拉起能力」（不启动任何进程）。"""
        game = Path(self._resolve(self.game_executable, dist_root))
        le_proc = self._resolve_optional(self.le_proc_path, dist_root)

        messages: list[str] = []
        game_ok = game.is_file()
        messages.append(f"游戏本体：{game}" + ("" if game_ok else "（缺失）"))

        if le_proc is None:
            messages.append("Locale Emulator：未装配 → 启动器自动直启（无转区）")
        else:
            messages.append(f"Locale Emulator：{le_proc}")

        if not game_ok:
            messages.append("错误：游戏本体不存在，无法拉起")

        return LaunchDiagnostics(
            game_executable_found=game_ok,
            le_proc_available=le_proc is not None,
            used_locale_emulator=le_proc is not None,
            messages=messages,
        )

    # ------------------------------------------------------------------
    # 内部：路径解析
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve(value: str | None, dist_root: str | Path | None) -> str:
        """相对路径按分发包根解析为绝对路径；绝对路径原样返回。"""
        if value is None:
            raise ValueError("目标路径为空")
        path = Path(value)
        if path.is_absolute() or dist_root is None:
            return str(path)
        return str(Path(dist_root) / path)

    @classmethod
    def _resolve_optional(
        cls, value: str | None, dist_root: str | Path | None,
    ) -> str | None:
        """解析可选的 LE 路径：未配置或物理不存在一律返回 None（触发降级）。"""
        if not value:
            return None
        resolved = cls._resolve(value, dist_root)
        if not Path(resolved).is_file():
            return None
        return resolved

    def environment_note(self) -> str:
        """部署提示（写入 INSTALL/启动器注释，帮助排障）。"""
        if self.locale_profile_guid:
            return (
                "转区通道：LEProc -runas <GUID>（确定性全局 profile；"
                "GUID 需存在于部署机 LEConfig.xml）"
            )
        return (
            "转区通道：LEProc -run（按 LEProc 三级回退：应用自身 profile → "
            "首个全局 profile → 默认 ja-JP）"
        )


def default_le_proc_path(toolbox_root: str | Path) -> str:
    """按工具箱约定拼出 LEProc 路径（tools/locale_emulator/LEProc.exe）。

    仅做**路径约定拼装**，不做探测 —— 物理存在性由调用方经工具链解析确认
    （与 dump/compile 同一纪律：Python 侧零路径探测特例）。
    """
    return str(Path(toolbox_root) / "tools" / "locale_emulator" / LE_PROC_NAME)


def is_windows() -> bool:
    """启动引导的平台判定（LEProc 仅 Windows 可执行）。"""
    return os.name == "nt"
