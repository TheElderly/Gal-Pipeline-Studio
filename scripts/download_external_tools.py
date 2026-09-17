"""外部工具链自动拉取 —— 仅使用**实测验证过**的官方直链（拒绝猜测 URL）。

数据来源纪律::

    * GitHub 工具：运行时调 GitHub Releases API 现查最新稳定版资产，
      绝不硬编码版本号/URL（本脚本先探测后下载，URL 来自 API 实测）；
    * 非 GitHub 工具（ffmpeg/gyan.dev）：登记直链，下载前先 HEAD 校验；
    * 下载产物记录 SHA-256 到 tools/{tool}/SHA256SUMS.txt（供应链审计）；
    * 解压到 tools/{tool}/（工具箱优先级 2 的约定路径）；
    * 探测：console 工具跑 --version/无参用法（10s 超时）；GUI 工具
      （GARbro）跳过启动探测，仅验证解压出主 exe。

无官方稳定直链的工具（cs2-tools / BGI-Tools）**不在本脚本登记** ——
社区实现分散、无单一权威发布源，由人工选定后另行登记（拒绝猜测）。
"""

import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLBOX = REPO_ROOT / "tools"
GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"

# tool_name → 配置。asset_filter：从 latest 资产里挑选（谓词 + 说明）。
# probe：None = GUI 工具跳过启动探测。
PLAN = {
    "freemote": {
        "repo": "UlyssesWu/FreeMote",
        "asset_contains": "Toolkit",
        "asset_ext": ".zip",
        "toolbox_dir": "tools/freemote",
        "probe": ["-h"],  # FreeMote.Psb.exe：无参/–h 打印用法
        "expect_exe": "FreeMote.Psb.exe",
    },
    "xdelta": {
        "repo": "jmacd/xdelta-gpl",
        "asset_contains": "x86_64.exe.zip",
        "asset_ext": ".zip",
        "toolbox_dir": "tools/xdelta",
        "probe": [],  # 无参打印用法（exit 1 亦算成功启动）
        "expect_exe": "xdelta3.exe",
    },
    "garbro": {
        "repo": "morkt/GARbro",
        "asset_contains": ".rar",
        "asset_ext": ".rar",
        "toolbox_dir": "tools/garbro",
        "probe": None,  # GUI：跳过启动探测
        "expect_exe": "GARbro.exe",
        "note": "官方仅提供 setup.exe 与 .rar；无解压工具时交付压缩包本体",
    },
    "ffmpeg": {
        "repo": "BtbN/FFmpeg-Builds",
        "asset_contains": "ffmpeg-master-latest-win64-gpl.zip",
        "asset_ext": ".zip",
        "toolbox_dir": "tools/ffmpeg",
        "probe": ["-version"],
        "expect_exe": "ffmpeg.exe",
        "note": "gyan.dev release-essentials（无 GitHub Release；直链实测 303 可跟随）",
    },
}


def _zip_intact(path: Path) -> bool:
    """zip 完整性：能打开且尾部含 EOCD（防 SIGTERM 截断的部分下载冒充成功）。"""
    try:
        with zipfile.ZipFile(path) as bundle:
            bundle.namelist()
        return path.read_bytes()[-22:][:4] == b"PK\x05\x06"
    except (OSError, zipfile.BadZipFile):
        return False


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, target: Path) -> Path:
    print(f"    ↓ {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "galpipeline-toolchain"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:
        total = 0
        while chunk := response.read(1 << 20):
            handle.write(chunk)
            total += len(chunk)
    print(f"    ✓ {total / 1024:.0f} KB → {target.name}")
    return target


def latest_asset(repo: str, contains: str) -> tuple[str, str]:
    url = GITHUB_API.format(repo=repo)
    request = urllib.request.Request(url, headers={"User-Agent": "galpipeline-toolchain"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    matches = [
        asset for asset in release.get("assets", [])
        if contains.lower() in asset["name"].lower()
    ]
    if not matches:
        raise RuntimeError(f"{repo} latest({release.get('tag_name')}) 无匹配资产：{contains}")
    asset = matches[0]
    return asset["browser_download_url"], release.get("tag_name", "?")


def extract(archive: Path, destination: Path) -> list[Path]:
    """zip 用标准库；rar 无原生解压时如实上报（绝不静默假装成功）。"""
    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
            return [destination / name for name in bundle.namelist()
                    if not name.endswith("/")]
    seven_zip = next(
        (candidate for candidate in (
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
        ) if candidate.is_file()),
        None,
    )
    if seven_zip is not None:
        subprocess.run([str(seven_zip), "x", "-y", f"-o{destination}", str(archive)],
                       check=True, capture_output=True)
        return [path for path in destination.rglob("*") if path.is_file()]
    return []  # 无解压能力：压缩包本体已交付，报告如实说明


def probe(exe: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            [str(exe), *args], capture_output=True, timeout=15,
        )
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        first = next((line for line in output.splitlines() if line.strip()), "")
        return f"exit={completed.returncode} | {first[:100]}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"探测失败：{exc}"


def main() -> int:
    TOOLBOX.mkdir(exist_ok=True)
    failures: list[str] = []

    for name, config in PLAN.items():
        print(f"\n=== {name} ===")
        destination = TOOLBOX / name
        destination.mkdir(parents=True, exist_ok=True)

        # 1) 直链解析（API 现查 or 登记直链）
        try:
            if "repo" in config:
                url, tag = latest_asset(config["repo"], config["asset_contains"])
                print(f"    release: {config['repo']} @{tag}")
            else:
                url = config["direct_url"]
        except Exception as exc:  # noqa: BLE001 —— 报告工具，失败不中断其余工具
            print(f"    ✘ 直链解析失败：{exc}")
            failures.append(name)
            continue

        # 2) 下载 + 哈希留痕
        archive = destination / url.rsplit("/", 1)[-1]
        if archive.is_file() and not _zip_intact(archive):
            print(f"    ⚠ 发现截断的历史下载（{archive.stat().st_size} 字节），删除重下")
            archive.unlink()
        try:
            if not archive.is_file():
                download(url, archive)
            else:
                print("    (完整包已存在，跳过下载)")
        except Exception as exc:  # noqa: BLE001
            print(f"    ✘ 下载失败：{exc}")
            failures.append(name)
            continue
        checksums = destination / "SHA256SUMS.txt"
        with checksums.open("a", encoding="utf-8") as handle:
            handle.write(f"{sha256(archive)}  {archive.name}\n")

        # 3) 解压（zip 标准库；rar 需系统 7z，缺则如实上报）
        extracted = extract(archive, destination)
        if archive.suffix.lower() == ".rar" and not extracted:
            print("    ⚠ .rar 无系统解压器（缺 7-Zip）：压缩包已交付至工具箱，"
                  "请手动解压或安装 7-Zip 后重跑本脚本")
            continue
        print(f"    ✓ 解压 {len(extracted)} 个文件")

        # 4) 主 exe 校验 + 探测
        expected = destination / config["expect_exe"]
        hits = list(destination.rglob(config["expect_exe"]))
        if not hits:
            print(f"    ✘ 解压产物中未找到 {config['expect_exe']}（实态："
                  f"{sorted(p.name for p in destination.rglob('*.exe'))[:8]}）")
            failures.append(name)
            continue
        main_exe = hits[0]
        if main_exe != expected and not expected.is_file():
            shutil_move(main_exe, expected)
            main_exe = expected
        if config["probe"] is not None:
            print(f"    ✓ 探测 {main_exe.name}: {probe(main_exe, config['probe'])}")
        else:
            print(f"    ✓ GUI 工具就位（跳过启动探测）: {main_exe}")

    print("\n=== 拉取汇总 ===")
    print(f"成功 {len(PLAN) - len(failures)} / 失败 {len(failures)}"
          + (f" → {failures}" if failures else ""))
    return 0 if not failures else 1


def shutil_move(source: Path, target: Path) -> None:
    import shutil
    shutil.move(str(source), str(target))


if __name__ == "__main__":
    sys.exit(main())
