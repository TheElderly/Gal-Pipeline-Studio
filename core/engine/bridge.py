"""EngineToolchainBridge —— 外部工具链中继调度的双向串联桥。

流水线定位（架构裁决：本地化工作站 ≠ 逆向工程库）::

    专有封包 ──(社区 CLI dump)──▶ 中间文本 ──(中继适配器)──▶ Gal-IR
    专有封包 ◀──(社区 CLI 回编译)── 中间文本 ◀──(适配器回写)── 译文 Gal-IR

本模块承载**横向的进程调度**：按配方渲染命令模板、拉起 CLI、治理进程
生命周期、校验产物真实存在。纵向的格式转换（中间文本 ⇄ Gal-IR）归
``core/adapters/relay/`` 的三适配器；引擎能力边界（能否回封）归
``recipes.py`` —— 三者各管一段，严禁互相越权。

进程治理契约（与 ``core/archive/base.py`` 的解包通道同一纪律）::

    * 超时硬击杀：默认 60s，超时即 ``taskkill /F /T``（Windows，连
      孙进程一起带走）或 ``kill()``（POSIX），杜绝僵尸/悬挂句柄；
    * 非零退出码：完整截获 stdout/stderr 入受控异常（尾部 400 字符）；
    * 产物校验：dump 产物 = 输出目录内**新增的**最大后缀匹配文件（快照
      差分，杜绝拾取历史残留）；compile 产物 = 目标文件必须存在且非 0 字节；
    * 工具路径由 C# ``ToolchainRuntime``（四级瀑布流）解析后**显式注入**
      —— 路径解析不在 Python 侧重复实现（单一事实源）。
"""

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from core.adapters.base import EngineAdapterError
from core.adapters.relay.recipes import RelayRecipe, get_recipe


class RelayToolchainError(EngineAdapterError):
    """中继调度失败：模板未校准、工具启动失败、超时、非零退出或产物缺失。

    ``pid`` 携带超时被击杀的进程号（非超时场景为 None），供测试与
    诊断确认「僵尸进程清理」真实发生。
    """

    def __init__(self, message: str, pid: int | None = None) -> None:
        super().__init__(message)
        self.pid = pid


_STDERR_TAIL = 400


class EngineToolchainBridge:
    """配方驱动的双向 CLI 调度器：dump 中间文件 / 回编译目标资产。

    命令模板来自 ``RelayRecipe.dump_command`` / ``recompile_command``
    （**草案**——占位符独占 token，接入真实工具时按实测校准）。
    ``tool_path`` 必须由调用方显式注入（C# ToolchainRuntime 解析产物）；
    能力闸门（``supports_recompile``）在 compile 路径上先行检查，
    无中生有的子进程一秒都不会被拉起。
    """

    def __init__(self, timeout_seconds: float = 60.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须为正")
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # 正向：封包 → 中间文本
    # ------------------------------------------------------------------

    def dump_to_intermediate(
        self,
        recipe_key: str,
        source_file: str | Path,
        output_dir: str | Path,
        tool_path: str,
    ) -> Path:
        """按配方 dump 出中间文本，返回产出的中间文件路径。"""
        recipe = get_recipe(recipe_key)
        source = Path(source_file)
        out_dir = Path(output_dir)
        if not source.is_file():
            raise RelayToolchainError(f"源文件不存在：{source}")
        if recipe.intermediate_suffix is None:
            raise RelayToolchainError(
                f"配方 {recipe.key} 未绑定中继适配器（纯只读兜底），无法产出中间文本"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        command = self._render(recipe.dump_command, recipe, tool_path, {
            "{input}": str(source),
            "{output_dir}": str(out_dir),
        })
        self._execute(command, verb="dump", label=f"{recipe.dump_tool} [{recipe.key}]")

        produced = self._newest_product(
            out_dir, recipe.intermediate_suffix, started_at, expected_stem=source.stem)
        if produced is None:
            raise RelayToolchainError(
                f"dump 工具报告成功但未产出 {recipe.intermediate_suffix} 中间文件"
                f"（输出目录 {out_dir}）—— 拒绝把空产出当成功交付"
            )
        return produced

    # ------------------------------------------------------------------
    # 反向：中间文本 → 目标资产（能力闸门先行）
    # ------------------------------------------------------------------

    def compile_from_intermediate(
        self,
        recipe_key: str,
        intermediate_file: str | Path,
        target_output_file: str | Path,
        tool_path: str,
    ) -> Path:
        """按配方回编译中间文本为目标资产，返回产物路径。"""
        recipe = get_recipe(recipe_key)
        # 能力闸门：无编译器的配方在此受控阻断 —— 子进程一个都不会被拉起
        recipe.assert_can_recompile()
        intermediate = Path(intermediate_file)
        target = Path(target_output_file)
        if not intermediate.is_file():
            raise RelayToolchainError(f"中间文件不存在：{intermediate}")

        target.parent.mkdir(parents=True, exist_ok=True)
        command = self._render(recipe.recompile_command, recipe, tool_path, {
            "{input}": str(intermediate),
            "{output}": str(target),
        })
        self._execute(command, verb="compile", label=f"{recipe.recompile_tool} [{recipe.key}]")

        if not target.is_file() or target.stat().st_size == 0:
            raise RelayToolchainError(
                f"回编译工具报告成功但产物缺失或为 0 字节：{target} "
                "—— 拒绝把空产出当成功交付"
            )
        return target

    # ------------------------------------------------------------------
    # 公共工具执行口（打包/差分等后续编排复用同一套进程治理）
    # ------------------------------------------------------------------

    def run_tool(self, command: list[str], verb: str = "tool") -> None:
        """以本桥的进程治理执行任意外部工具命令（超时树杀 / 退出码 / stderr 截获）。"""
        self._execute(command, verb, label=Path(command[0]).name)

    # ------------------------------------------------------------------
    # 进程治理与渲染（私有）
    # ------------------------------------------------------------------

    def _render(
        self,
        template: str | None,
        recipe: RelayRecipe,
        tool_path: str,
        values: dict[str, str],
    ) -> list[str]:
        if not template:
            raise RelayToolchainError(
                f"配方 {recipe.key} 的命令模板未校准（当前为草案占位）——"
                "接入真实社区工具并按实测登记 dump_command / recompile_command 后即可打通"
            )
        if not tool_path or not Path(tool_path).is_file():
            raise RelayToolchainError(
                f"外部工具不可用：{tool_path or '（未注入）'} —— "
                "请先在设置页工具链面板配置，或装配到程序目录 tools/ 下"
            )
        tokens = template.split()
        rendered = []
        for token in tokens:
            if "{tool}" in token:
                token = token.replace("{tool}", tool_path)
            for key, value in values.items():
                if key in token:
                    token = token.replace(key, value)
            leftover = re.search(r"\{[a-zA-Z_]+\}", token)
            if leftover:
                raise RelayToolchainError(
                    f"配方 {recipe.key} 命令模板含未知占位符：{leftover.group(0)}"
                )
            rendered.append(token)
        return rendered

    def _execute(self, command: list[str], verb: str, label: str) -> None:
        # 输出走临时文件而非 PIPE：超时树杀后管道读取线程会随句柄失效
        # 抛竞态异常（pytest 线程警告的来源），文件承载则彻底无此问题，
        # 且超时路径也能把已产生的输出尾部带进异常（诊断信息不丢失）。
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
            except OSError as exc:
                raise RelayToolchainError(f"外部工具无法启动（{command[0]}）：{exc}") from exc

            try:
                proc.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                # 树杀：taskkill /F /T（Windows）连孙进程一起带走；POSIX 直接 kill
                killed = True
                try:
                    if _is_windows():
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True, check=False,
                        )
                    else:
                        proc.kill()
                except OSError:
                    killed = False
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise RelayToolchainError(
                    f"{verb} 工具超时（>{self.timeout_seconds:.0f}s）已终止："
                    f"{label} —— 悬挂进程已清理"
                    + ("" if killed else "（树杀失败，请人工检查进程表）")
                    + f"｜输出尾部：{_tail_file(stderr_file) or _tail_file(stdout_file) or '（无）'}",
                    pid=proc.pid,
                ) from exc

            if proc.returncode != 0:
                stderr = _tail_file(stderr_file)
                raise RelayToolchainError(
                    f"{verb} 工具退出码 {proc.returncode}：{command[0]} — "
                    f"{stderr or '（无 stderr 输出）'}"
                )

    @staticmethod
    def _newest_product(
        directory: Path, suffix: str, started_at: float,
        expected_stem: str | None = None,
    ) -> Path | None:
        """识别本次运行的产物：后缀匹配 + mtime 落在启动时刻之后。

        用 mtime 窗口而非路径差分——真实 CLI 会**同名覆盖**中间文件，
        路径差分会把合法的新产物误判为旧残留。

        ``expected_stem``：真实工具常产出**多个**同后缀文件（实测
        PsbDecompile 会同时产出 ``scene.json`` 与 ``scene.resx.json``），
        stem 与输入匹配者优先——这是真机实测校准出的消歧规则。
        """
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix.lower() == suffix
            and path.stat().st_size > 0
            and path.stat().st_mtime >= started_at - 1.0  # 1s 时钟容忍
        ]
        if not candidates:
            return None
        if expected_stem is not None:
            stem_matches = [path for path in candidates
                            if path.stem.lower() == expected_stem.lower()]
            if stem_matches:
                return max(stem_matches, key=lambda path: path.stat().st_mtime)
        return max(candidates, key=lambda path: path.stat().st_mtime)


def _is_windows() -> bool:
    return os.name == "nt"


def _tail_file(handle) -> str:
    """读回子进程输出文件的尾部（诊断信息；空文件返回空串）。"""
    try:
        handle.seek(0)
        data = handle.read()
        return data.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL:]
    except OSError:
        return ""


def _tail_file(handle) -> str:
    """读回子进程输出文件的尾部（诊断信息；空文件返回空串）。"""
    try:
        handle.seek(0)
        data = handle.read()
        return data.decode("utf-8", errors="replace").strip()[-_STDERR_TAIL:]
    except OSError:
        return ""
