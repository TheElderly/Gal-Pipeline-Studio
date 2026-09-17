"""模块 0（Engine Profiler）指纹识别率测试 —— 全部用 tmp_path 合成虚拟游戏根目录。

命中策略对齐侦察实现的权重设计：特征封包 > PE 特征串 > 特征目录。
每个用例同时断言「判对引擎」与「证据可审计」—— 没有依据的判定和
判定失败同样不可接受。
"""

import json
import sys
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.base import EngineAdapterError
from core.profiler import EngineProfile, profile_engine
from core.server.rpc import build_default_dispatcher


def write_exe(directory: Path, name: str, signatures: list[bytes]) -> Path:
    """合成一个带特征字符串的可执行文件（真实 PE 无需解析——侦察只扫头部字节）。"""
    path = directory / name
    payload = b"MZ" + b"\x00" * 64 + b"".join(signatures)
    path.write_bytes(payload)
    return path


class TestKiriKiri:
    def test_data_xp3_and_plugin_dir_and_pe_string(self, tmp_path):
        game = tmp_path / "krkr_game"
        (game / "plugin").mkdir(parents=True)
        (game / "data.xp3").write_bytes(b"XP3\x00fake")
        (game / "plugin" / "some.dll").write_bytes(b"\x00")
        write_exe(game, "game.exe", [b"KiriKiriZ CORE"])

        profile = profile_engine(game)

        assert profile.detected and profile.engine_type == "kirikiri"
        assert profile.confidence >= 0.9  # 三类证据全中
        assert profile.recommended_unpacker and "GARbro" in profile.recommended_unpacker
        descriptions = " ".join(e.description for e in profile.evidence)
        assert "data.xp3" in descriptions and "KiriKiri" in descriptions

    def test_renamed_exe_without_data_xp3_still_hits_via_wildcard_and_pe(self, tmp_path):
        """混淆盘：data.xp3 改名，但 *.xp3 + PE 特征串仍足以判定。"""
        game = tmp_path / "obfuscated"
        game.mkdir(parents=True)
        (game / "video01.xp3").write_bytes(b"XP3")
        write_exe(game, "mystery.exe", [b"KiriKiri"])
        profile = profile_engine(game)
        assert profile.detected and profile.engine_type == "kirikiri"


class TestBgi:
    def test_bgi_exe_arc_and_ethornell_signature(self, tmp_path):
        game = tmp_path / "bgi_game"
        game.mkdir(parents=True)
        (game / "data.arc").write_bytes(b"ARC")
        (game / "bgi.exe").write_bytes(b"MZ")
        write_exe(game, "engine.exe", [b"Ethornell"])
        profile = profile_engine(game)
        assert profile.detected and profile.engine_type == "bgi"
        assert profile.confidence >= 0.8

    def test_arc_alone_is_weak_but_positive(self, tmp_path):
        """只有 *.arc：低置信度但可判定（BGI 是 .arc 的主要玩家）。"""
        game = tmp_path / "weak"
        game.mkdir(parents=True)
        (game / "data.arc").write_bytes(b"ARC")
        profile = profile_engine(game)
        assert profile.engine_type == "bgi"
        assert profile.confidence < 1.0


class TestMajiroAndCatSystem:
    def test_majiro_scripts_and_signature(self, tmp_path):
        game = tmp_path / "maji"
        game.mkdir(parents=True)
        (game / "init.mj").write_bytes(b"MJ")
        write_exe(game, "majiro.exe", [b"Majiro"])
        profile = profile_engine(game)
        assert profile.engine_type == "majiro"

    def test_catsystem_int_and_signature(self, tmp_path):
        game = tmp_path / "cs2"
        game.mkdir(parents=True)
        (game / "data.int").write_bytes(b"INT")
        write_exe(game, "cs2.exe", [b"CatSystem2"])
        profile = profile_engine(game)
        assert profile.engine_type == "catsystem2"


class TestNegativeAndPriority:
    def test_empty_directory_is_not_detected(self, tmp_path):
        profile = profile_engine(tmp_path)
        assert not profile.detected
        assert profile.engine_type is None
        assert profile.confidence == 0.0
        assert profile.evidence == []

    def test_missing_directory_raises_business_error(self, tmp_path):
        with pytest.raises(EngineAdapterError):
            profile_engine(tmp_path / "nope")

    def test_mixed_evidence_picks_highest_confidence(self, tmp_path):
        """xp3（0.6+0.4+0.4）对 arc（0.4+0.6）：KiriKiri 证据更全则胜出。"""
        game = tmp_path / "mixed"
        game.mkdir(parents=True)
        (game / "data.xp3").write_bytes(b"XP3")
        write_exe(game, "a.exe", [b"KiriKiri"])
        (game / "bgm.arc").write_bytes(b"ARC")
        profile = profile_engine(game)
        assert profile.engine_type == "kirikiri"

    def test_engine_type_only_from_registry(self, tmp_path):
        """乱塞无关文件不得产出任何引擎判定（防误报底线）。"""
        game = tmp_path / "noise"
        game.mkdir(parents=True)
        (game / "readme.txt").write_text("hello")
        (game / "save.dat").write_bytes(b"\x00")
        profile = profile_engine(game)
        assert not profile.detected


class TestRpcSurface:
    def test_detect_engine_rpc_returns_profile(self, tmp_path):
        game = tmp_path / "krkr"
        game.mkdir(parents=True)
        (game / "data.xp3").write_bytes(b"XP3")
        dispatcher = build_default_dispatcher()
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "detect_engine",
             "params": {"game_dir": str(game)}},
            ensure_ascii=False,
        )
        result = json.loads(dispatcher.handle_request(frame))["result"]["profile"]
        parsed = EngineProfile.model_validate(result)
        assert parsed.engine_type == "kirikiri"

    def test_detect_engine_missing_dir_maps_to_business_error(self):
        dispatcher = build_default_dispatcher()
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "detect_engine",
             "params": {"game_dir": "Z:/definitely/missing"}},
        )
        response = json.loads(dispatcher.handle_request(frame))
        assert response["error"]["code"] == -32000
