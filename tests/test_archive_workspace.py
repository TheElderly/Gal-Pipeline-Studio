"""模块 1（Archive & Workspace）契约测试 —— 工作区幂等 + 跨进程 CLI 通道。

跨进程纪律的验证方式：测试用 **Python 解释器自身充当外部解包工具**
（``[sys.executable, "-c", ...]``），真实走子进程往返 —— 不链接任何
GPL 工具，也不 mock subprocess，进程隔离/超时/退出码路径全部实跑。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.base import EngineAdapterError
from core.adapters.base import EngineAdapterError
from core.archive import (
    ArchiveExtractorError,
    ExternalToolUnavailableError,
    KirikiriXp3Extractor,
    init_workspace,
    load_workspace_profile,
    workspace_paths,
)
from core.profiler import profile_engine
from core.server.rpc import build_default_dispatcher


def fake_tool(exit_code: int, stderr: str = "") -> list[str]:
    """合成一个「外部解包工具」：把封包复制为产出目录里的 entry.bin。"""
    script = (
        "import shutil, sys\n"
        f"sys.stderr.write({stderr!r})\n"
        "shutil.copyfile(sys.argv[1], sys.argv[2] + '/entry.bin')\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", script]


class TestWorkspace:
    def test_init_creates_standard_layout_and_records_engine(self, tmp_path):
        (tmp_path / "data.xp3").write_bytes(b"XP3")
        profile = profile_engine(tmp_path)

        root = init_workspace(tmp_path, profile)
        paths = workspace_paths(tmp_path)

        assert root == paths["root"]
        for key in ("raw_scripts", "raw_images", "raw_voice", "translated", "project"):
            assert paths[key].exists(), f"标准工作区缺少 {key}"

        document = json.loads(paths["project"].read_text(encoding="utf-8"))
        assert document["schema"] == "galpipeline.workspace/1"
        assert document["engine"]["engine_type"] == "kirikiri"
        assert document["game_dir"] == str(tmp_path.resolve())

    def test_init_is_idempotent_and_never_clobbers_project_json(self, tmp_path):
        (tmp_path / "data.xp3").write_bytes(b"XP3")
        init_workspace(tmp_path, profile_engine(tmp_path))

        # 模拟用户/后续切片写入了工程元数据 —— 重跑侦察不得回滚它
        project = workspace_paths(tmp_path)["project"]
        project.write_text('{"schema": "user-edited"}', encoding="utf-8")

        init_workspace(tmp_path, profile_engine(tmp_path))
        assert json.loads(project.read_text(encoding="utf-8")) == {"schema": "user-edited"}

    def test_workspace_round_trips_profile(self, tmp_path):
        (tmp_path / "data.xp3").write_bytes(b"XP3")
        init_workspace(tmp_path, profile_engine(tmp_path))
        reloaded = load_workspace_profile(tmp_path)
        assert reloaded is not None and reloaded.engine_type == "kirikiri"

    def test_missing_game_dir_raises(self, tmp_path):
        with pytest.raises(EngineAdapterError):  # WorkspaceError 派生自 EngineAdapterError
            init_workspace(tmp_path / "nope", profile_engine(tmp_path))


class TestExternalCliChannel:
    def test_unconfigured_tool_raises_unavailable_not_generic_failure(self, tmp_path):
        """通道就绪、工具未装配：必须与「解包失败」严格分型。"""
        archive = tmp_path / "data.xp3"
        archive.write_bytes(b"XP3")
        extractor = KirikiriXp3Extractor()  # 未注入工具路径
        with pytest.raises(ExternalToolUnavailableError):
            extractor.extract(archive, tmp_path / "out")

    def test_missing_archive_raises_before_spawning(self, tmp_path):
        extractor = KirikiriXp3Extractor(executable="whatever-tool")
        with pytest.raises(ArchiveExtractorError, match="封包不存在"):
            extractor.extract(tmp_path / "ghost.xp3", tmp_path / "out")

    def test_real_subprocess_roundtrip_produces_files(self, tmp_path):
        """真实子进程往返：工具把封包复制为 entry.bin，契约统计产出数。"""
        archive = tmp_path / "data.xp3"
        archive.write_bytes(b"XP3-fake")
        tool = fake_tool(0)
        extractor = KirikiriXp3Extractor(
            executable=tool[0], arguments=(*tool[1:], "{archive}", "{output}")
        )

        result = extractor.extract(archive, tmp_path / "out")

        assert (tmp_path / "out" / "entry.bin").read_bytes() == b"XP3-fake"
        assert result.file_count == 1

    def test_nonzero_exit_surfaces_stderr(self, tmp_path):
        archive = tmp_path / "data.xp3"
        archive.write_bytes(b"XP3")
        tool = [sys.executable, "-c", "import sys; sys.stderr.write('boom: bad magic'); sys.exit(3)"]
        extractor = KirikiriXp3Extractor(
            executable=tool[0], arguments=(*tool[1:], "{archive}", "{output}")
        )
        with pytest.raises(ArchiveExtractorError, match="boom: bad magic"):
            extractor.extract(archive, tmp_path / "out")

    def test_hung_tool_is_terminated_by_timeout(self, tmp_path):
        """防挂死：外部工具 hang 住必须被超时终止并判失败（不留僵尸等待）。"""
        archive = tmp_path / "data.xp3"
        archive.write_bytes(b"XP3")
        hang = [sys.executable, "-c", "import time; time.sleep(60)"]
        extractor = KirikiriXp3Extractor(
            executable=hang[0], arguments=(*hang[1:],), timeout_seconds=1.0
        )
        with pytest.raises(ArchiveExtractorError, match="超时"):
            extractor.extract(archive, tmp_path / "out")

    def test_empty_output_is_rejected_even_on_success(self, tmp_path):
        """退出码 0 但产出目录为空 —— 拒绝把静默空产出当成功交付。"""
        archive = tmp_path / "data.xp3"
        archive.write_bytes(b"XP3")
        empty = [sys.executable, "-c", "import sys; sys.exit(0)"]
        extractor = KirikiriXp3Extractor(
            executable=empty[0], arguments=(*empty[1:], "{archive}", "{output}")
        )
        with pytest.raises(ArchiveExtractorError, match="空产出"):
            extractor.extract(archive, tmp_path / "out")

    def test_init_workspace_rpc_end_to_end(self, tmp_path):
        (tmp_path / "data.xp3").write_bytes(b"XP3")
        dispatcher = build_default_dispatcher()
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "init_workspace",
             "params": {"game_dir": str(tmp_path)}},
            ensure_ascii=False,
        )
        result = json.loads(dispatcher.handle_request(frame))["result"]
        assert result["profile"]["engine_type"] == "kirikiri"
        assert Path(result["workspace_root"], "project.json").is_file()
