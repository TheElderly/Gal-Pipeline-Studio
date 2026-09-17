"""EngineToolchainBridge 调度器契约测试。

断言面::

    1. 模板渲染：krkr_psb / cs2 的 dump/compile 命令按配方插值，
       argv 逐 token 核对（垫片把收到的参数写进日志文件回读断言）；
    2. 产物链路：dump 产出中间文件（快照差分识别新增产物）、
       compile 产出目标文件，缺一即受控报错；
    3. 进程治理：超时树杀（pid 确认死亡）、非零退出码截获 stderr、
       产物缺失 / 0 字节防御、工具路径失效防御；
    4. 能力闸门：generic_garbro compile 精准阻断（子进程零拉起）；
       模板未校准配方（bgi）受控拒绝。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.relay.recipes import RelayCapabilityError, get_recipe  # noqa: E402
from core.engine import EngineToolchainBridge, RelayToolchainError  # noqa: E402


# ---------------------------------------------------------------------------
# cmd 垫片：真实子进程承载（同 unpack/convert 通道的测试先例）
# ---------------------------------------------------------------------------


def _make_shim(directory: Path, name: str, script: str) -> str:
    """生成 cmd 垫片：python 脚本经 cmd 承载，argv 经 %* 全量透传。"""
    tool_py = directory / f"{name}_tool.py"
    tool_py.write_text(script, encoding="utf-8")
    wrapper = directory / f"{name}.cmd"
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{tool_py}" %*\r\n',
        encoding="utf-8",
    )
    return str(wrapper)


def _dump_shim(
    directory: Path,
    fail: int | None = None,
    no_product: bool = False,
    product: str = "decompiled.json",
) -> str:
    log = directory / "dump-argv.json"
    script = (
        "import json, sys, pathlib\n"
        f"log = {json.dumps(str(log))}\n"
        "pathlib.Path(log).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        + (f"sys.exit({fail})\n" if fail is not None else "")
        + (
            "pass\n"
            if no_product
            else "out = sys.argv[sys.argv.index('-o') + 1]\n"
            f"pathlib.Path(out, {json.dumps(product)}).write_text("
            "'{\"text\": \"x\"}', encoding='utf-8')\n"
        )
    )
    return _make_shim(directory, "fmdump", script)


def _compile_shim(directory: Path, zero_byte: bool = False, fail: int | None = None) -> str:
    script = (
        "import sys, pathlib\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        f"pathlib.Path(out).write_text('' if {zero_byte} else 'PSB-BINARY', encoding='utf-8')\n"
        + (f"sys.stderr.write('simulated failure'); sys.exit({fail})\n" if fail is not None else "")
    )
    return _make_shim(directory, "fmbuild", script)


class TestDumpPath:
    """正向 dump：模板插值、产物识别、目录快照差分。"""

    def test_krkr_psb_renders_template_and_returns_product(self, tmp_path):
        log = tmp_path / "argv.json"
        tool = _make_shim(tmp_path, "fmdump", (
            "import json, sys, pathlib\n"
            f"pathlib.Path({json.dumps(str(log))}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            "pathlib.Path(out, 'scene.json').write_text('{\"text\": \"x\"}', encoding='utf-8')\n"
        ))
        source = tmp_path / "scene.psb"
        source.write_bytes(b"PSB")
        out_dir = tmp_path / "intermediate"

        product = EngineToolchainBridge().dump_to_intermediate(
            "krkr_psb", source, out_dir, tool)

        assert product == out_dir / "scene.json"
        assert product.read_text(encoding="utf-8") == '{"text": "x"}'
        argv = json.loads(log.read_text(encoding="utf-8"))
        # 校准后的 FreeMote 规范：PsbDecompile -o {output_dir} {input}
        assert argv == ["-o", str(out_dir), str(source)], (
            f"krkr_psb dump 模板必须逐 token 渲染，实际 {argv}"
        )

    def test_cs2_template_renders_subcommand_style(self, tmp_path):
        tool = _dump_shim(tmp_path, product="mark.txt")  # relay.marked → .txt 产物
        source = tmp_path / "data.int"
        source.write_bytes(b"INT")
        out_dir = tmp_path / "out"

        product = EngineToolchainBridge().dump_to_intermediate(
            "cs2", source, out_dir, tool)

        assert product == out_dir / "mark.txt"
        argv = json.loads((tmp_path / "dump-argv.json").read_text(encoding="utf-8"))
        assert argv[0] == "-i" and "-o" in argv, (
            "cs2 模板为 cs2_decompile 实测风格：-i <input> -o <dir> -md")

    def test_stale_product_is_not_picked(self, tmp_path):
        """历史残留（本轮未触碰）绝不被当作本次产物；同名覆盖的新产物必须胜出。"""
        out_dir = tmp_path / "work"
        out_dir.mkdir()
        stale = out_dir / "leftover.json"
        stale.write_text("old-stale", encoding="utf-8")  # 本轮未触碰的残留
        overwritten = out_dir / "decompiled.json"
        overwritten.write_text("old-content", encoding="utf-8")  # 将被同名覆盖
        tool = _dump_shim(tmp_path)
        source = tmp_path / "scene.psb"
        source.write_bytes(b"PSB")

        product = EngineToolchainBridge().dump_to_intermediate(
            "krkr_psb", source, out_dir, tool)

        assert product == overwritten  # 同名覆盖 = 新产物（mtime 窗口语义）
        assert product.read_text(encoding="utf-8") == '{"text": "x"}'
        assert stale.read_text(encoding="utf-8") == "old-stale"


class TestCompilePath:
    """反向回编译：能力闸门、模板插值、产物校验。"""

    def test_krkr_psb_compile_produces_target(self, tmp_path):
        tool = _compile_shim(tmp_path)
        intermediate = tmp_path / "scene.json"
        intermediate.write_text('{"text": "訳"}', encoding="utf-8")
        target = tmp_path / "patch2.psb"

        result = EngineToolchainBridge().compile_from_intermediate(
            "krkr_psb", intermediate, target, tool)

        assert result == target
        assert target.read_text(encoding="utf-8") == "PSB-BINARY"

    def test_generic_garbro_is_blocked_before_any_process(self, tmp_path):
        """supports_recompile=False：能力闸门先行，子进程一个都不拉起。"""
        intermediate = tmp_path / "x.json"
        intermediate.write_text("{}", encoding="utf-8")
        before = {p.name for p in tmp_path.iterdir()}

        with pytest.raises(RelayCapabilityError) as excinfo:
            EngineToolchainBridge().compile_from_intermediate(
                "generic_garbro", intermediate, tmp_path / "out.arc", "unused-tool")

        assert "不具备回编译能力" in str(excinfo.value)
        assert {p.name for p in tmp_path.iterdir()} - before == set()  # 零副作用

    def test_uncalibrated_template_is_controlled_rejection(self, tmp_path):
        """bgi 配方 supports_recompile=True 但模板未校准：受控拒绝而非裸跑。"""
        intermediate = tmp_path / "script.tsv"
        intermediate.write_text("1\tA\t原文\t\n", encoding="utf-8")

        with pytest.raises(RelayToolchainError, match="未校准"):
            EngineToolchainBridge().compile_from_intermediate(
                "bgi", intermediate, tmp_path / "out.arc", "any-tool")

        pack = tmp_path / "pack.arc"
        pack.write_bytes(b"ARC")
        with pytest.raises(RelayToolchainError, match="未校准"):
            EngineToolchainBridge().dump_to_intermediate(
                "bgi", pack, tmp_path / "out", "any-tool")


class TestProcessGovernance:
    """进程治理契约：超时树杀 / 非零退出 / 产物防御 / 路径失效。"""

    def test_timeout_kills_process_tree(self, tmp_path):
        # 垫片前台 ping 30 秒：桥接器 1s 超时 → taskkill /T 树杀
        tool = _make_shim(tmp_path, "slowdump", (
            "import subprocess, sys\n"
            "subprocess.run(['ping', '-n', '30', '127.0.0.1'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        ))
        source = tmp_path / "scene.psb"
        source.write_bytes(b"PSB")

        exception = None
        try:
            EngineToolchainBridge(timeout_seconds=1.0).dump_to_intermediate(
                "krkr_psb", source, tmp_path / "out", tool)
        except RelayToolchainError as exc:
            exception = exc
        assert exception is not None and "超时" in str(exception)
        assert exception.pid is not None

        # 树杀落地核实：垫片进程已从进程表消失（轮询 ≤10s）
        deadline = time.time() + 10
        while time.time() < deadline:
            tasklist = str(Path(os.environ["WINDIR"]) / "System32" / "tasklist.exe")
            probe = subprocess.run(
                [tasklist, "/FI", f"PID eq {exception.pid}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            if f" {exception.pid} " not in (probe.stdout or "").replace("\xa0", " "):
                break
            time.sleep(0.3)
        else:
            pytest.fail("超时击杀后垫片进程仍存活 —— 树杀未生效")

    def test_nonzero_exit_captures_stderr(self, tmp_path):
        tool = _compile_shim(tmp_path, fail=3)
        intermediate = tmp_path / "scene.json"
        intermediate.write_text("{}", encoding="utf-8")

        with pytest.raises(RelayToolchainError) as excinfo:
            EngineToolchainBridge().compile_from_intermediate(
                "krkr_psb", intermediate, tmp_path / "out.psb", tool)

        assert "退出码 3" in str(excinfo.value)
        assert "simulated failure" in str(excinfo.value)  # stderr 完整截获

    def test_missing_product_is_rejected(self, tmp_path):
        tool = _dump_shim(tmp_path, no_product=True)
        source = tmp_path / "scene.psb"
        source.write_bytes(b"PSB")

        with pytest.raises(RelayToolchainError, match="未产出"):
            EngineToolchainBridge().dump_to_intermediate(
                "krkr_psb", source, tmp_path / "out", tool)

    def test_zero_byte_product_is_rejected(self, tmp_path):
        tool = _compile_shim(tmp_path, zero_byte=True)
        intermediate = tmp_path / "scene.json"
        intermediate.write_text("{}", encoding="utf-8")

        with pytest.raises(RelayToolchainError, match="0 字节"):
            EngineToolchainBridge().compile_from_intermediate(
                "krkr_psb", intermediate, tmp_path / "out.psb", tool)

    def test_invalid_tool_path_is_defensive(self, tmp_path):
        source = tmp_path / "scene.psb"
        source.write_bytes(b"PSB")

        with pytest.raises(RelayToolchainError, match="外部工具不可用"):
            EngineToolchainBridge().dump_to_intermediate(
                "krkr_psb", source, tmp_path / "out", r"C:\definitely\missing\tool.exe")

    def test_unknown_recipe_key_is_controlled(self, tmp_path):
        with pytest.raises(Exception) as excinfo:
            EngineToolchainBridge().dump_to_intermediate(
                "dreamcast", tmp_path / "x", tmp_path / "out", "tool")
        assert "未知中继配方" in str(excinfo.value)
