"""解包业务编排 —— 封包清单 → 逐封包跨进程解包 → raw/scripts 落位。

数据流定位::

    EngineProfile（模块 0）+ Toolchain 解析的 GARbro 路径（C# 侧传入）
        │ list_archives / unpack_archive
        ▼
    .galpipeline/raw/scripts/{archive_stem}/…（工程树重扫即亮）

**逐封包多次 RPC 而非单次大任务**的编排决策：解包可能耗时数分钟，
单条长 RPC 会堵死 StdIO 分发器（后续 ping/翻译全部排队），且无法取消。
拆成「列清单 → 每封包一条短 RPC」后：
* 取消粒度 = 封包 —— C# 侧在批间检查 CancellationToken，用户取消即刻
  停发后续请求，不存在被阻塞的分发器，也没有失控的长任务；
* 进度 = 已处理/总数，真实可回传（不做假进度条）；
* 单封包失败不拖垮整批（跳过并上报，继续下一封包）。

幂等纪律：输出目录 ``raw/scripts/{stem}/`` 已存在且非空 → **跳过**
（重复解包既慢又会覆盖用户可能在产物上做的手工整理）。

工具中立纪律：本模块不认识 GARbro 的具体命令行 —— 命令模板按引擎
登记在 ``_UNPACK_TEMPLATES``（当前为 arc_unpacker 风格草案），接入真实
CLI（GARbro 生态 / YuriSizuku / arc_unpacker）时校准模板即可，编排零改动。
"""

from __future__ import annotations

from pathlib import Path

from core.adapters.base import EngineAdapterError
from core.archive.base import ExternalCliExtractor
from core.archive.workspace import workspace_paths

# 各引擎：封包 glob（识别）+ 解包命令模板（工具中立草案）
_UNPACK_RECIPES: dict[str, dict[str, object]] = {
    "kirikiri": {
        "archive_globs": ("*.xp3",),
        "arguments": ("x", "{archive}", "-o", "{output}"),
    },
    "bgi": {
        "archive_globs": ("*.arc",),
        "arguments": ("x", "{archive}", "-o", "{output}"),
    },
}

_EXCLUDED_DIRNAME = ".galpipeline"
"""工作区自身（含历史解包产物）不参与封包扫描 —— 重扫永远不被自己污染。"""


class UnpackOrchestrationError(EngineAdapterError):
    """解包编排失败：引擎无解包配方、封包不存在等。"""


def _recipe(engine_type: str | None) -> dict[str, object]:
    if engine_type is None:
        raise UnpackOrchestrationError("引擎未识别，无法确定封包扫描配方")
    recipe = _UNPACK_RECIPES.get(engine_type)
    if recipe is None:
        raise UnpackOrchestrationError(
            f"引擎 {engine_type} 暂无解包配方（封包资产须先经外部工具手动提取）")
    return recipe


def _iter_archive_files(game_dir: Path, engine_type: str) -> list[Path]:
    """按引擎配方收集根目录封包（浅扫 + 排除工作区自身）。"""
    recipe = _recipe(engine_type)
    found: list[Path] = []
    for pattern in recipe["archive_globs"]:  # type: ignore[union-attr]
        for file in game_dir.glob(pattern):
            if not file.is_file():
                continue
            if _EXCLUDED_DIRNAME in file.parts:
                continue
            found.append(file)
    return sorted(found, key=lambda f: f.name.lower())


def list_archives(game_dir: str, engine_type: str | None) -> dict:
    """列出待解包封包（相对 game_dir 的路径），供壳层逐封包调度。"""
    directory = Path(game_dir)
    if not directory.is_dir():
        raise UnpackOrchestrationError(f"游戏目录不存在：{game_dir}")
    archives = [
        str(f.relative_to(directory))
        for f in _iter_archive_files(directory, engine_type or "")
    ]
    return {"archives": archives}


def unpack_archive(
    game_dir: str,
    engine_type: str | None,
    archive: str,
    tool_path: str,
    timeout_seconds: float = 600.0,
) -> dict:
    """把单个封包解包到 .galpipeline/raw/scripts/{stem}/（幂等）。

    tool_path 由壳层经统一工具链解析后传入（本模块不做工具定位）；
    未装配 / 退出码异常 / 空产出分别以独立异常分型上抛，
    JSON-RPC 错误映射层统一归为 -32000 业务错误。
    """
    directory = Path(game_dir)
    if not directory.is_dir():
        raise UnpackOrchestrationError(f"游戏目录不存在：{game_dir}")
    recipe = _recipe(engine_type)

    archive_path = (directory / archive).resolve()
    if not archive_path.is_file():
        raise UnpackOrchestrationError(f"封包不存在：{archive}")
    if _EXCLUDED_DIRNAME in archive_path.parts:
        raise UnpackOrchestrationError(f"拒绝解包工作区自身内的文件：{archive}")

    raw_scripts = workspace_paths(directory)["raw_scripts"]
    raw_scripts.mkdir(parents=True, exist_ok=True)
    output_dir = raw_scripts / archive_path.stem
    if output_dir.exists() and any(output_dir.iterdir()):
        return {"output_dir": str(output_dir), "file_count": -1, "skipped": True}

    extractor = ExternalCliExtractor(
        engine_id=engine_type or "unknown",
        executable=tool_path,
        arguments=tuple(recipe["arguments"]),  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
    )
    result = extractor.extract(archive_path, output_dir)
    return {
        "output_dir": str(result.output_dir),
        "file_count": result.file_count,
        "skipped": False,
    }
