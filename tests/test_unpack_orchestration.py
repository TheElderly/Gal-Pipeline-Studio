"""解包业务编排（list_archives / unpack_archive）的契约测试。

外部解包工具用 **cmd 垫片 + Python 复制脚本**充当（真实子进程往返，
命令模板参数逐位对齐生产配方 `x / {archive} / -o / {output}`），
不 mock subprocess；幂等 / 失败分型 / 工作区排除全部实跑验证。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.base import EngineAdapterError
from core.archive.orchestration import list_archives, unpack_archive
from core.server.rpc import build_default_dispatcher


@pytest.fixture
def kirikiri_game(tmp_path):
    """纯封包状态的游戏目录：2 个 xp3 + 1 个工作区内的历史产物（必须被排除）。"""
    (tmp_path / "data.xp3").write_bytes(b"XP3-A")
    (tmp_path / "patch.xp3").write_bytes(b"XP3-B")
    ws_artifact = tmp_path / ".galpipeline" / "raw" / "scripts" / "old.xp3"
    ws_artifact.parent.mkdir(parents=True)
    ws_artifact.write_bytes(b"XP3-OLD")
    return tmp_path


def make_wrapper_tool(directory: Path) -> str:
    """合成「CLI 解包工具」：cmd 垫片转发到 Python 复制脚本。

    生产命令模板为 (x, {archive}, -o, {output})，垫片按位接收：
    %1=x %2={archive} %3=-o %4={output}，转手给复制脚本 argv[1]/argv[2]。
    """
    tool_py = directory / "unpacker.py"
    tool_py.write_text(
        "import shutil, sys\n"
        "shutil.copyfile(sys.argv[1], sys.argv[2] + '/extracted.dat')\n"
        "sys.exit(0)\n",
        encoding="utf-8")
    wrapper = directory / "unpacker.cmd"
    # 生产模板 (x, {archive}, -o, {output}) → %1=x %2=archive %3=-o %4=output；
    # 复制脚本只需要 archive 与 output 两位
    wrapper.write_text(
        f'@echo off\r\n"{sys.executable}" "{tool_py}" %2 %4\r\n',
        encoding="utf-8")
    return str(wrapper)


class TestListArchives:
    def test_lists_packaged_archives_excluding_workspace(self, kirikiri_game):
        result = list_archives(str(kirikiri_game), "kirikiri")

        # 工作区内部的历史文件绝不参与扫描（重扫不被自己污染）
        assert result["archives"] == ["data.xp3", "patch.xp3"]

    def test_bgi_uses_arc_pattern(self, tmp_path):
        (tmp_path / "data.arc").write_bytes(b"ARC")
        (tmp_path / "data.xp3").write_bytes(b"XP3")  # 非本引擎配方，不收

        result = list_archives(str(tmp_path), "bgi")

        assert result["archives"] == ["data.arc"]

    def test_unknown_engine_raises(self, tmp_path):
        (tmp_path / "data.xp3").write_bytes(b"XP3")
        with pytest.raises(EngineAdapterError, match="解包配方"):
            list_archives(str(tmp_path), "unknown-engine")

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(EngineAdapterError, match="不存在"):
            list_archives(str(tmp_path / "nope"), "kirikiri")


class TestUnpackArchive:
    def test_unpack_lands_in_raw_scripts_with_recipe_arguments(
            self, kirikiri_game, tmp_path):
        """真实子进程往返：产物落 raw/scripts/{stem}/，模板参数逐位对齐。"""
        tool = make_wrapper_tool(tmp_path)

        result = unpack_archive(
            str(kirikiri_game), "kirikiri", "data.xp3", tool_path=tool)

        assert result["skipped"] is False
        assert result["file_count"] == 1
        assert (kirikiri_game / ".galpipeline" / "raw" / "scripts"
                / "data" / "extracted.dat").read_bytes() == b"XP3-A"

    def test_idempotent_skip_on_existing_output(self, kirikiri_game):
        out_dir = kirikiri_game / ".galpipeline" / "raw" / "scripts" / "data"
        out_dir.mkdir(parents=True)
        (out_dir / "already.txt").write_text("x")

        result = unpack_archive(
            str(kirikiri_game), "kirikiri", "data.xp3", tool_path="whatever")

        assert result["skipped"] is True
        assert result["file_count"] == -1
        assert (out_dir / "already.txt").read_text() == "x"  # 不覆盖既有产物

    def test_missing_archive_raises(self, kirikiri_game):
        with pytest.raises(EngineAdapterError, match="封包不存在"):
            unpack_archive(
                str(kirikiri_game), "kirikiri", "ghost.xp3", tool_path="tool")

    def test_workspace_internal_file_is_rejected(self, kirikiri_game):
        with pytest.raises(EngineAdapterError, match="工作区"):
            unpack_archive(
                str(kirikiri_game), "kirikiri",
                str(Path(".galpipeline") / "raw" / "scripts" / "old.xp3"),
                tool_path="tool")


class TestRpcSurface:
    def test_list_and_unpack_via_rpc_frames(self, kirikiri_game, tmp_path):
        """帧级端到端：清单 → 逐封包解包 → 幂等重解跳过。"""
        tool = make_wrapper_tool(tmp_path)
        dispatcher = build_default_dispatcher()

        def call(method, params):
            frame = json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                ensure_ascii=False)
            return json.loads(dispatcher.handle_request(frame))

        listed = call("list_archives",
                      {"game_dir": str(kirikiri_game), "engine_type": "kirikiri"})
        assert listed["result"]["archives"] == ["data.xp3", "patch.xp3"]

        unpacked = call("unpack_archive", {
            "game_dir": str(kirikiri_game), "engine_type": "kirikiri",
            "archive": "data.xp3", "tool_path": tool,
        })
        assert unpacked["result"]["skipped"] is False
        assert unpacked["result"]["file_count"] == 1

        again = call("unpack_archive", {
            "game_dir": str(kirikiri_game), "engine_type": "kirikiri",
            "archive": "data.xp3", "tool_path": tool,
        })
        assert again["result"]["skipped"] is True

    def test_missing_tool_maps_to_business_error_with_guidance(self, kirikiri_game):
        dispatcher = build_default_dispatcher()
        frame = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "unpack_archive",
            "params": {"game_dir": str(kirikiri_game), "engine_type": "kirikiri",
                       "archive": "data.xp3", "tool_path": None},
        }, ensure_ascii=False)
        response = json.loads(dispatcher.handle_request(frame))
        assert response["error"]["code"] == -32000
        # 工具未装配的引导语义必须穿透到错误消息（ExternalToolUnavailableError 分型）
        assert "装配" in response["error"]["message"]
