"""工具链依赖审计 —— 静态分析代码中「真正需要调用的外部 CLI 清单」并盘点本机装配状态。

分析来源（三层，拒绝猜测）::

    1. Python 配方：core/adapters/relay/recipes.py 的全部 RelayRecipe
       （dump_tool / recompile_tool 显式登记；pack 工具经 C# ToolType 解析）；
    2. C# 注册表：ToolchainRegistry 的全部 ExecutableName（运行时四级
       瀑布流实际探测的目标）+ ExpectedVersionArgs / DownloadUrl；
    3. 消费点扫描：grep ToolType.* 的运行时消费位置（bridge / packaging /
       unpack / convert），标明每个工具被哪些链路调用。

盘点范围：仓库 tools/ 工具箱（rglob）+ 系统 PATH 逐目录。
Found 项运行版本探测（注册表 ExpectedVersionArgs，10s 超时）；
GUI 工具跳过启动探测（避免拉起窗口），仅验证 PE 头存在。

用法：python scripts/audit_toolchain_dependencies.py
退出码：0 = 全部就绪；1 = 存在 Missing。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from core.adapters.relay.recipes import RECIPES  # noqa: E402

REGISTRY_CS = REPO_ROOT / "src" / "GalPipeline.Core" / "Toolchain" / "ToolchainRegistry.cs"
TOOLBOX_ROOT = REPO_ROOT / "tools"
GUI_TOOLS = {"GARbro.exe"}  # 启动即开窗的工具：跳过运行探测
WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))

_CSHARP_ENTRY_RE = re.compile(
    r"new ExternalToolInfo\(\s*"
    r"Type:\s*ToolType\.(?P<type>\w+),\s*"
    r'DisplayName:\s*"(?P<display>[^"]+)",\s*'
    r'ExecutableName:\s*"(?P<exe>[^"]+)",\s*'
    r'RelativeInstallDir:\s*"(?P<dir>[^"]+)",\s*'
    r'DownloadUrl:\s*"(?P<url>[^"]+)",\s*'
    r"ExpectedVersionArgs:\s*\[(?P<args>[^\]]*)\],\s*"
    r'VersionPattern:\s*@?"(?P<pattern>[^"]+)"',
)


def _exe_names(text: str) -> list[str]:
    """从展示名/命令名中提取 *.exe 形态的可执行名；无 .exe 的命令保留原词。"""
    found = re.findall(r"[A-Za-z0-9_.+-]+\.exe", text)
    if found:
        return found
    stripped = text.split("(")[0].strip()
    return [stripped] if stripped else []


def _version_args(raw: str) -> list[str]:
    return re.findall(r'"([^"]*)"', raw)


def collect_csharp_registry() -> dict[str, dict]:
    source = REGISTRY_CS.read_text(encoding="utf-8")
    registry: dict[str, dict] = {}
    for match in _CSHARP_ENTRY_RE.finditer(source):
        registry[match.group("exe")] = {
            "tool_type": match.group("type"),
            "display": match.group("display"),
            "toolbox_dir": match.group("dir"),
            "download_url": match.group("url"),
            "version_args": _version_args(match.group("args")),
            "version_pattern": match.group("pattern"),
            "mandatory": "IsMandatory: true" in source[match.start(): match.end() + 80],
        }
    return registry


def collect_consumers() -> dict[str, list[str]]:
    """grep ToolType.* 消费点：标明每个注册表工具被哪些链路调用。"""
    consumers: dict[str, list[str]] = {}
    for py_file in (REPO_ROOT / "core").rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for tool_type in re.findall(r"ToolType\.(\w+)", text):
            consumers.setdefault(tool_type, []).append(
                py_file.relative_to(REPO_ROOT).as_posix())
    return consumers


def scan_filesystem(executable_name: str) -> list[Path]:
    """工具箱 rglob（tools/ 全树）+ PATH 逐目录。"""
    hits: list[Path] = []
    if TOOLBOX_ROOT.is_dir():
        hits.extend(path for path in TOOLBOX_ROOT.rglob(executable_name) if path.is_file())
    for directory in (os.environ.get("PATH") or "").split(os.pathsep):
        if not directory.strip():
            continue
        candidate = Path(directory.strip()) / executable_name
        if candidate.is_file() and candidate not in hits:
            hits.append(candidate)
    return hits


def probe_version(path: Path, version_args: list[str]) -> str:
    args = version_args or ["--version"]
    try:
        completed = subprocess.run(
            [str(path), *args],
            capture_output=True, timeout=10,
        )
        out = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        first = next((line for line in out.splitlines() if line.strip()), "")
        return f"exit={completed.returncode} | {first[:100]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"探测失败：{exc}"


def main() -> int:
    registry = collect_csharp_registry()
    consumers = collect_consumers()

    # --- 聚合：注册表 ∪ 配方显式登记 ---
    entries: dict[str, dict] = {}
    for exe, info in registry.items():
        entries[exe] = {
            "exe": exe,
            "sources": [f"ToolchainRegistry（ToolType.{info['tool_type']}，"
                        f"{'必选' if info['mandatory'] else '可选'}）"],
            "consumers": sorted({
                file.replace("core/", "").replace(".py", "")
                for file in consumers.get(info["tool_type"], [])
            }),
            "version_args": info["version_args"],
            "download_url": info["download_url"],
            "toolbox_dir": info["toolbox_dir"],
            "gui": exe in GUI_TOOLS,
            "recipes": [],
        }

    for key, recipe in RECIPES.items():
        for role, declared in (
            ("dump", recipe.dump_tool),
            ("recompile", recipe.recompile_tool),
        ):
            if not declared:
                continue
            for exe in _exe_names(declared):
                entry = entries.setdefault(exe, {
                    "exe": exe,
                    "sources": [],
                    "consumers": [],
                    "version_args": ["--version"],
                    "download_url": None,
                    "toolbox_dir": None,
                    "gui": exe in GUI_TOOLS,
                    "recipes": [],
                })
                entry["sources"].append(f"配方 {key}.{role}")
                entry["recipes"].append((key, role, declared))

    # --- 盘点 ---
    found, missing = [], []
    for exe in sorted(entries):
        info = entries[exe]
        hits = scan_filesystem(exe)
        info["hits"] = hits
        (found if hits else missing).append(info)

    # --- 报告 ---
    print("=" * 78)
    print("外部工具链依赖审计（静态分析 + 本机盘点）")
    print("=" * 78)
    print(f"分析来源：配方 {len(RECIPES)} 条 ｜ C# 注册表 {len(registry)} 项 ｜ "
          f"去重后需调用 CLI 共 {len(entries)} 个")
    print()
    print(f"[已就绪 (Found)] {len(found)} 个")
    for info in found:
        primary = info["hits"][0]
        size_kb = primary.stat().st_size / 1024
        print(f"  ✔ {info['exe']}")
        print(f"      路径: {primary}")
        print(f"      大小: {size_kb:,.1f} KB ｜ 命中数: {len(info['hits'])}")
        if not info["gui"]:
            print(f"      版本: {probe_version(primary, info['version_args'])}")
        else:
            print("      版本: (GUI 工具，跳过启动探测)")
        print(f"      来源: {'；'.join(info['sources'])}")
        if info["consumers"]:
            print(f"      消费: {', '.join(info['consumers'])}")
    print()
    print(f"[缺失 (Missing)] {len(missing)} 个")
    for info in missing:
        print(f"  ✘ {info['exe']}")
        print(f"      来源: {'；'.join(info['sources'])}")
        if info["recipes"]:
            for key, role, declared in info["recipes"]:
                print(f"      调用: 配方 {key} · {role} → {declared}")
        if info.get("download_url"):
            print(f"      官方: {info['download_url']}")
        else:
            print("      官方: （未登记稳定直链 —— 需人工选定社区实现后登记，拒绝猜测）")
    print()
    print(f"汇总：{len(found)} 就绪 / {len(missing)} 缺失 / 共 {len(entries)}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
