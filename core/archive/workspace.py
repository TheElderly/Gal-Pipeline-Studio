"""标准工作区（.galpipeline/）—— 整作工程化的落盘容器。

目录规范（与 docs/AI_HANDOFF.md 的模块 1 定义一致）::

    <game_dir>/.galpipeline/
        project.json        # 工程元数据与引擎画像（模块 0 的落盘点）
        raw/
            scripts/        # 原始解包脚本
            images/         # 原始立绘 / 背景 / 差分
            voice/          # 原始语音
        translated/         # 生成 / 回封产物

幂等纪律：``init_workspace`` 对已存在的目录**绝不覆盖** —— 重跑只补建
缺失的目录，project.json 已存在则原样保留（工程元数据是用户资产，
重跑侦察不回滚历史）。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.adapters.base import EngineAdapterError
from core.profiler.engine_profiler import EngineProfile

WORKSPACE_DIRNAME = ".galpipeline"

_RAW_SUBDIRS = ("scripts", "images", "voice")


class WorkspaceError(EngineAdapterError):
    """工作区创建失败：游戏目录不存在等。"""


def workspace_paths(game_dir: Path) -> dict[str, Path]:
    """返回标准工作区的全部路径（不创建，仅供展示与测试对账）。"""
    root = Path(game_dir) / WORKSPACE_DIRNAME
    return {
        "root": root,
        "project": root / "project.json",
        "translated": root / "translated",
        **{f"raw_{name}": root / "raw" / name for name in _RAW_SUBDIRS},
    }


def init_workspace(game_dir: Path, profile: EngineProfile) -> Path:
    """在游戏目录下创建标准工作区，把引擎画像落盘为 project.json。

    幂等：目录只补建、project.json 不覆盖。返回工作区根路径。
    """
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        raise WorkspaceError(f"游戏目录不存在：{game_dir}")

    paths = workspace_paths(game_dir)
    paths["root"].mkdir(parents=True, exist_ok=True)
    for key, path in paths.items():
        if key.startswith("raw_"):
            path.mkdir(parents=True, exist_ok=True)
    paths["translated"].mkdir(parents=True, exist_ok=True)

    if paths["project"].exists():
        return paths["root"]  # 幂等：不回滚既有工程元数据

    document = {
        "schema": "galpipeline.workspace/1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "game_dir": str(game_dir.resolve()),
        "engine": profile.model_dump(mode="json"),
    }
    paths["project"].write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return paths["root"]


def load_workspace_profile(game_dir: Path) -> EngineProfile | None:
    """读取既有工程记录的引擎画像；工作区不存在时返回 None。"""
    paths = workspace_paths(game_dir)
    if not paths["project"].is_file():
        return None
    document = json.loads(paths["project"].read_text(encoding="utf-8"))
    return EngineProfile.model_validate(document["engine"])
