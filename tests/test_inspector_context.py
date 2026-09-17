"""Inspector 行级上下文的 Python 侧契约测试。

两块内容 —— 都是壳层 Inspector 的数据来源：

1. **术语表匹配**（``core/tm/glossary.py`` + ``match_glossary`` RPC）：
   最长优先、不重叠、偏移升序、无命中即空；结果按 unit_id 索引、省略空项。
2. **行内语音元数据**（``KagAdapter`` 的 ``[voice ...]`` 识别）：
   只认整词 ``voice``（``se`` / ``bgm`` / ``play`` 一律不算），
   且产物只进 ``metadata``，对回写保真与 LQA 锚点**零影响**。
"""

import json
import sys
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.kag import KagAdapter
from core.server.rpc import build_default_dispatcher
from core.tm.glossary import GLOSSARY, match_glossary

SAMPLE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample_act1.ks"


def rpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    dispatcher = build_default_dispatcher()
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        ensure_ascii=False,
    )
    return json.loads(dispatcher.handle_request(frame))


def terms(text: str) -> list[tuple[str, str]]:
    return [(m.source, m.target) for m in match_glossary(text)]


# ---------------------------------------------------------------------------
# 1. 术语表匹配
# ---------------------------------------------------------------------------


class TestGlossaryMatching:
    def test_longest_match_wins_at_same_position(self):
        """``指切りげんまん`` 与 ``指切り`` 同起点 —— 必须取更具体的那条。"""
        hits = match_glossary("「指切りげんまん、嘘ついたら針千本飲ます。」")

        sources = [m.source for m in hits]
        assert sources[0] == "指切りげんまん"
        assert "指切り" not in sources, "同起点的短术语不得与长术语并存"

    def test_matches_are_non_overlapping_and_offset_ascending(self):
        hits = match_glossary("真実を知る覚悟は、もうできているの？")

        assert [(m.source, m.target) for m in hits] == [("真実", "真相"), ("覚悟", "觉悟")]
        starts = [m.start for m in hits]
        assert starts == sorted(starts)
        # 区间两两不重叠
        for left, right in zip(hits, hits[1:]):
            assert left.end <= right.start, f"{left.source} 与 {right.source} 区间重叠"
        # 偏移可用来从原文取回术语字面量
        text = "真実を知る覚悟は、もうできているの？"
        for hit in hits:
            assert text[hit.start:hit.end] == hit.source

    def test_repeated_occurrence_is_reported_each_time(self):
        text = "約束、そして約束。"
        hits = match_glossary(text)

        assert [m.source for m in hits] == ["約束", "約束"]
        # 偏移以逐字扫描为准（不手算式索引）：两处都须能从原文取回术语字面量
        assert [text[m.start:m.end] for m in hits] == ["約束", "約束"]
        assert hits[0].start == 0 and hits[1].start == 6

    @pytest.mark.parametrize("text", [None, "", "   ", "伞", "no japanese term here"])
    def test_no_match_or_empty_input_returns_empty_list(self, text):
        assert match_glossary(text) == []

    def test_matching_is_deterministic(self):
        text = "「指切りげんまん」の約束と、針千本の誓い。"
        assert match_glossary(text) == match_glossary(text)

    def test_glossary_entries_are_immutable_and_wellformed(self):
        assert len(GLOSSARY) > 0
        for entry in GLOSSARY:
            assert entry.source.strip(), "术语表存在空源词"
            assert entry.target.strip(), f"{entry.source} 的译法为空"
            assert entry.note.strip(), f"{entry.source} 缺少来源标注"


class TestGlossaryRpc:
    def test_registered_and_keyed_by_unit_id(self):
        units = rpc("extract_to_ir", {"file_path": str(SAMPLE)})["result"]["project"]["units"]
        matches = rpc("match_glossary", {"units": units})["result"]["matches"]

        assert set(matches) <= {u["id"] for u in units}
        # 真实样本 8 行全部有术语命中（术语卡在默认样本上就有内容可展示）
        assert len(matches) == len(units) == 8
        assert matches["sample_act1-00027"] == [
            {"source": "指切りげんまん", "target": "拉钩上吊", "note": "Verified"},
            {"source": "針千本", "target": "一千根针", "note": "Verified"},
        ]

    def test_units_without_matches_are_omitted(self):
        unit = {
            "id": "x-00001",
            "speaker": None,
            "raw_text": "hello world",
            "extracted_text": "hello world",
            "atomic_tags": [],
            "paired_tags": [],
            "translated_text": None,
            "status": "EXTRACTED",
            "metadata": {},
        }
        result = rpc("match_glossary", {"units": [unit]})["result"]

        assert result["matches"] == {}, "无命中单元应被省略，而不是给空列表"

    def test_empty_unit_list_is_accepted(self):
        assert rpc("match_glossary", {"units": []})["result"] == {"matches": {}}

    def test_matching_reads_extracted_text_not_raw_text(self):
        """术语匹配针对可译正文：控制符不应干扰匹配窗口。"""
        unit = {
            "id": "x-00002",
            "speaker": "千代",
            "raw_text": "【千代】[ruby text=\"x\"]約束[p]",
            "extracted_text": "約束",
            "atomic_tags": [],
            "paired_tags": [],
            "translated_text": None,
            "status": "EXTRACTED",
            "metadata": {},
        }
        result = rpc("match_glossary", {"units": [unit]})["result"]

        assert result["matches"]["x-00002"][0]["source"] == "約束"


# ---------------------------------------------------------------------------
# 2. 行内语音元数据
# ---------------------------------------------------------------------------


VOICE_SCRIPT = (
    "; voice metadata fixture\n"
    "*start\n"
    '@bg storage="room.png" time=800\n'
    '【千代】[voice file="vo_00004.ogg" duration_ms=1850 comp_ms=200]「やあ」[p]\n'
    '【主人公】[voice storage="vo_00005.ogg"]「うん」[r]\n'
    "波の音だけが流れていた。[p]\n"
    '【千代】[se storage="se_wave.ogg"]「音だけ」[p]\n'
    '【千代】[bgm storage="bgm_theme.ogg"]「曲だけ」[p]\n'
    '【千代】[voice]「資源名なし」[p]\n'
    "*end\n"
)


@pytest.fixture(scope="module")
def voiced_project(tmp_path_factory) -> object:
    workdir = tmp_path_factory.mktemp("voice")
    script = workdir / "voice_scene.ks"
    script.write_text(VOICE_SCRIPT, encoding="utf-8")
    adapter = KagAdapter()
    project = adapter.extract_to_ir(script)
    return {"adapter": adapter, "script": script, "project": project, "workdir": workdir}


class TestVoiceMetadata:
    def test_full_params_are_captured(self, voiced_project):
        unit = voiced_project["project"].units[0]

        assert unit.metadata["audio"] == {
            "file": "vo_00004.ogg",
            "duration_ms": 1850,
            "comp_ms": 200,
        }

    def test_storage_alias_is_accepted(self, voiced_project):
        unit = voiced_project["project"].units[1]

        assert unit.metadata["audio"] == {"file": "vo_00005.ogg"}

    def test_narration_line_has_no_audio_metadata(self, voiced_project):
        unit = voiced_project["project"].units[2]

        assert "audio" not in unit.metadata, "旁白行不该凭空获得配音线索"

    @pytest.mark.parametrize("index", [3, 4])
    def test_sound_effect_and_bgm_macros_do_not_count_as_voice(self, voiced_project, index):
        """``[se]`` / ``[bgm]`` 描述音效与背景乐，不构成「本行有人声」。

        把它们算成配音，会让音频卡对绝大多数行误报「可播放」。
        """
        unit = voiced_project["project"].units[index]

        assert "audio" not in unit.metadata

    def test_voice_macro_without_asset_is_ignored(self, voiced_project):
        unit = voiced_project["project"].units[5]

        assert "audio" not in unit.metadata, "无资源名的 [voice] 不足以构成可播放线索"

    def test_audio_metadata_does_not_disturb_text_or_tags(self, voiced_project):
        unit = voiced_project["project"].units[0]

        # 宏登记与正文剥离必须与无语音时完全一致
        assert [t.raw_tag for t in unit.atomic_tags] == [
            '[voice file="vo_00004.ogg" duration_ms=1850 comp_ms=200]',
            "[p]",
        ]
        assert unit.extracted_text == "「やあ」"

    def test_audio_metadata_leaves_writeback_byte_identical(self, voiced_project):
        """元数据是**纯增量**字段：不参与 raw_text 重建，回写必须与源文件一致。"""
        adapter = voiced_project["adapter"]
        script = voiced_project["script"]
        project = voiced_project["project"]

        output = adapter.ir_to_asset(project, voiced_project["workdir"] / "out")

        def normalize(raw: bytes) -> bytes:
            body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
            return body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

        assert normalize(output.read_bytes()) == normalize(script.read_bytes())

    def test_bundled_sample_has_no_voice_metadata(self):
        """如实记录：内置样本不含 voice 宏，故 8 行全部走静音分支。

        这不是缺陷 —— 而是「适配器没报音频信息」时应有的表现。
        """
        project = KagAdapter().extract_to_ir(SAMPLE)

        assert len(project.units) == 8
        assert not any("audio" in unit.metadata for unit in project.units)
