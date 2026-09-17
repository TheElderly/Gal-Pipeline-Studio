"""PackagingPipeline —— 按配方 patch_strategy 路由的交付打包管道。

三条管道（与 recipes.patch_strategy 一一对应）::

    OverlayPatch   → Dirty 资产按原相对路径收纳进增量暂存根
                     （patch2.xp3.d / update.int.d），装配了引擎打包工具
                     （Xp3Pack / cs2-tools）时自动产出增量容器；
    LooseDirectory → Dirty 资产按原相对路径铺成解压即用覆盖目录；
    XDeltaDiff     → xdelta3 对「原版完整封包 ⇄ 汉化完整封包」生成轻量
                     差分包，并**当场做还原验证**（-d 回放 → 哈希必须一致）。

公共交付面：``dist/{game_id}_patch/`` 下 = 策略载荷 + ``manifest.json``
（文件清单 / 哈希 / 策略 / 打包状态）+ ``INSTALL.txt``（极简安装说明）。

进程治理复用 :class:`EngineToolchainBridge`（超时树杀 / 退出码 / stderr），
工具路径由调用方经 C# ToolchainRuntime 解析后注入 —— 与 dump/compile
同一纪律，Python 侧零路径探测特例。
"""

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core.adapters.base import EngineAdapterError
from core.adapters.relay.recipes import RelayRecipe, get_recipe
from core.engine.bridge import EngineToolchainBridge

from .collector import DirtyCollection, collect_dirty_report

_STRATEGY_OVERLAY = "OverlayPatch"
_STRATEGY_LOOSE = "LooseDirectory"
_STRATEGY_XDELTA = "XDeltaDiff"

_STDERR_TAIL = 400


class PackagingError(EngineAdapterError):
    """打包失败：策略无载荷、差分还原验证不一致等。"""


@dataclass
class PackagingResult:
    """一次打包的交付摘要。"""

    dist_dir: Path
    strategy: str
    dirty_count: int = 0
    skipped_count: int = 0
    staged_paths: list[Path] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    manifest_path: Path | None = None

    # 被机拦剔除的逆向中间产物（相对路径）——「剔除」必须可见，不静默
    excluded_intermediates: list[str] = field(default_factory=list)


class PackagingPipeline:
    """按配方策略路由的补丁打包管道（Dirty-Only 纪律的执行者）。"""

    def __init__(self, timeout_seconds: float = 300.0) -> None:
        self._bridge = EngineToolchainBridge(timeout_seconds=timeout_seconds)

    def build(
        self,
        recipe: str | RelayRecipe,
        *,
        raw_dir: str | Path,
        localized_dir: str | Path,
        dist_dir: str | Path,
        game_id: str,
        tool_path: str | None = None,
        original_archive: str | Path | None = None,
        localized_archive: str | Path | None = None,
    ) -> PackagingResult:
        """收集 Dirty 资产并按策略打包，返回交付摘要。"""
        resolved = recipe if isinstance(recipe, RelayRecipe) else get_recipe(recipe)
        collection = collect_dirty_report(raw_dir, localized_dir)
        dirty = collection.diffs
        shippable = collection.payload

        dist_root = Path(dist_dir) / f"{_sanitize(game_id)}_patch"
        if dist_root.exists():
            shutil.rmtree(dist_root)  # 交付根幂等重建：上次残留绝不混入本次补丁
        dist_root.mkdir(parents=True)

        if resolved.patch_strategy == _STRATEGY_OVERLAY:
            result, note, packed = self._build_overlay(resolved, dirty, dist_root, tool_path)
        elif resolved.patch_strategy == _STRATEGY_LOOSE:
            result, note, packed = self._build_loose(dirty, dist_root)
        elif resolved.patch_strategy == _STRATEGY_XDELTA:
            result, note, packed = self._build_xdelta(
                resolved, dist_root, tool_path, original_archive, localized_archive)
        else:
            raise PackagingError(f"未知交付策略：{resolved.patch_strategy}")

        result.dirty_count = len(shippable)
        result.skipped_count = len(dirty) - len(shippable)
        result.excluded_intermediates = [
            item.relative_path for item in collection.intermediates]
        self._write_manifest(
            dist_root, resolved, game_id, dirty, result, note, packed, collection)
        self._write_install_notes(dist_root, resolved, result)
        return result

    # ------------------------------------------------------------------
    # 策略管道
    # ------------------------------------------------------------------

    def _build_overlay(
        self, recipe: RelayRecipe, dirty, dist_root: Path, tool_path: str | None,
    ) -> tuple[PackagingResult, str, bool]:
        # 暂存根命名：patch2.xp3 → patch2.xp3.d（容器与暂存一一对应）
        artifact_name = recipe.patch_artifact or "patch"
        staging = dist_root / f"{artifact_name}.d"
        staged = self._stage_dirty(dirty, staging)

        artifacts: list[Path] = []
        packed = False
        if recipe.pack_command and tool_path:
            artifact = dist_root / artifact_name
            command = self._render_pack(recipe.pack_command, tool_path, staging, artifact)
            self._bridge.run_tool(command, verb="pack")
            if not artifact.is_file() or artifact.stat().st_size == 0:
                raise PackagingError(f"打包工具报告成功但产物缺失或为 0 字节：{artifact}")
            artifacts.append(artifact)
            packed = True
            note = f"已由 {Path(tool_path).name} 打包为 {artifact_name}（Dirty-Only）。"
        elif recipe.pack_command is None:
            note = "配方未登记 pack_command：交付暂存目录（按相对路径覆盖游戏根同样生效）。"
        else:
            note = (
                "增量容器待打包：装配引擎打包工具（经工具链解析注入 tool_path）后"
                "自动产出 " + artifact_name + "；暂存目录当前即可按相对路径覆盖使用。"
            )

        result = PackagingResult(
            dist_dir=dist_root,
            strategy=recipe.patch_strategy,
            staged_paths=staged,
            artifacts=artifacts,
        )
        return result, note, packed

    def _build_loose(self, dirty, dist_root: Path) -> tuple[PackagingResult, str, bool]:
        staged = self._stage_dirty(dirty, dist_root)
        note = "散文件交付：整个补丁目录解压覆盖到游戏根即可生效。"
        result = PackagingResult(
            dist_dir=dist_root,
            strategy=_STRATEGY_LOOSE,
            staged_paths=staged,
        )
        return result, note, False

    def _build_xdelta(
        self, recipe: RelayRecipe, dist_root: Path, tool_path: str | None,
        original_archive: str | Path | None, localized_archive: str | Path | None,
    ) -> tuple[PackagingResult, str, bool]:
        if not tool_path:
            raise PackagingError(
                "XDeltaDiff 管道需要 xdelta3 CLI：请经工具链解析后注入 tool_path")
        if not original_archive or not localized_archive:
            raise PackagingError(
                "XDeltaDiff 管道需要 original_archive（原版完整封包）与 "
                "localized_archive（汉化完整封包）两个输入")

        original = Path(original_archive)
        localized = Path(localized_archive)
        if not original.is_file() or not localized.is_file():
            raise PackagingError(f"差分输入缺失：{original} / {localized}")

        patch_file = dist_root / (recipe.patch_artifact or "patch.xdelta")
        # 生成：xdelta3 -e -f -s original localized patch
        self._bridge.run_tool(
            [tool_path, "-e", "-f", "-s", str(original), str(localized), str(patch_file)],
            verb="xdelta encode")
        if not patch_file.is_file() or patch_file.stat().st_size == 0:
            raise PackagingError(f"xdelta3 报告成功但差分包缺失或为 0 字节：{patch_file}")

        # 还原验证：xdelta3 -d -f -s <原版完整封包> <补丁> <还原产物> → 哈希必须与汉化封包一致
        # 参数顺序是 xdelta3 的硬契约（-s 后才是补丁输入）：写反会得到
        # "not a VCDIFF input: XD3_INVALID_INPUT"（真实穿透测试抓出的缺陷）。
        restored = dist_root / ".verify.tmp"
        try:
            self._bridge.run_tool(
                [tool_path, "-d", "-f", "-s", str(original), str(patch_file), str(restored)],
                verb="xdelta verify")
            if not restored.is_file() or _sha256(restored) != _sha256(localized):
                raise PackagingError(
                    "差分包还原验证失败：xdelta3 -d 产物与汉化封包哈希不一致 —— "
                    "拒绝交付无法还原的差分包")
        finally:
            restored.unlink(missing_ok=True)

        note = "差分包已生成并通过还原验证（还原产物与汉化封包 SHA-256 一致）。"
        result = PackagingResult(
            dist_dir=dist_root,
            strategy=_STRATEGY_XDELTA,
            artifacts=[patch_file],
        )
        return result, note, False

    # ------------------------------------------------------------------
    # 公共件
    # ------------------------------------------------------------------

    @staticmethod
    def _stage_dirty(dirty, staging_root: Path) -> list[Path]:
        staged: list[Path] = []
        for diff in dirty:
            if diff.kind == "removed":
                continue  # 增量覆盖无法表达删除，removed 仅在 manifest 披露
            target = staging_root / Path(diff.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(diff.source, target)
            staged.append(target)
        return staged

    @staticmethod
    def _render_pack(template: str, tool_path: str, patch_dir: Path, output: Path) -> list[str]:
        command = []
        for token in template.split():
            token = (token.replace("{tool}", tool_path)
                          .replace("{patch_dir}", str(patch_dir))
                          .replace("{output}", str(output)))
            if "{" in token and "}" in token:
                raise PackagingError(f"pack 模板含未知占位符：{token}")
            command.append(token)
        return command

    @staticmethod
    def _write_manifest(
        dist_root: Path, recipe: RelayRecipe, game_id: str, dirty,
        result: PackagingResult, note: str, packed: bool,
        collection: DirtyCollection | None = None,
    ) -> Path:
        staged_dir = (
            str(result.staged_paths[0].parent.relative_to(dist_root))
            if result.staged_paths else None
        )
        manifest = {
            "game_id": game_id,
            "recipe": recipe.key,
            "engine": recipe.engine_label,
            "patch_strategy": result.strategy,
            "patch_artifact": recipe.patch_artifact,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "dirty_count": result.dirty_count,
            "skipped_unchanged": result.skipped_count,
            "pack": {"packed": packed, "note": note},
            # 机拦留痕：被剔除的逆向中间产物（含命中样式与原因），
            # 让「没进补丁的东西」同样可审计 —— 剔除绝不静默。
            "excluded_intermediates": [
                {
                    "path": item.relative_path,
                    "matched_pattern": item.matched_pattern,
                    "reason": item.reason,
                }
                for item in (collection.intermediates if collection else [])
            ],
            "staged_dir": staged_dir,
            "artifacts": [str(path.relative_to(dist_root)) for path in result.artifacts],
            "files": [
                {
                    "path": diff.relative_path,
                    "kind": diff.kind,
                    "size": diff.size,
                    "sha256": diff.sha256,
                }
                for diff in dirty
            ],
        }
        manifest_path = dist_root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result.manifest_path = manifest_path
        return manifest_path

    @staticmethod
    def _write_install_notes(dist_root: Path, recipe: RelayRecipe, result: PackagingResult) -> None:
        if result.strategy == _STRATEGY_OVERLAY and result.artifacts:
            body = (
                f"将本目录下的 {result.artifacts[0].name} 复制到游戏根目录"
                "（或按引擎增量包规范安装），启动游戏即生效。\n"
            )
        elif result.strategy == _STRATEGY_OVERLAY:
            staged_dir = (
                str(result.staged_paths[0].parent.relative_to(dist_root))
                if result.staged_paths else "（空）"
            )
            body = (
                f"将 {staged_dir} 内的文件按相对路径覆盖到游戏根目录即可生效。\n"
                "（增量容器打包工具装配后，可自动产出容器产物。）\n"
            )
        elif result.strategy == _STRATEGY_XDELTA:
            body = (
                "使用 xdelta3 应用补丁：\n"
                "  xdelta3 -d -f -s <原版完整封包> patch.xdelta <输出路径>\n"
                "（本补丁已经过还原验证：还原产物与汉化封包 SHA-256 一致。）\n"
            )
        else:
            body = "将本目录内的文件按相对路径覆盖到游戏根目录即可生效。\n"
        (dist_root / "INSTALL.txt").write_text(body, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize(game_id: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in game_id).strip("_")
    return cleaned or "game"
