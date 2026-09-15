"""core/models 契约回归测试（Batch 1.1 / 1.2 自测资产固化）。

覆盖范围与切片承诺一一对应：

1. TranslationStatus 枚举契约（str 混入、按值反解、序列化字面值）；
   AtomicTag / PairedTag 构造正例、frozen、序列化往返，以及
   空串 / 纯空白 / 负偏移 / 未知字段的构造期拒绝；
2. TranslationUnit 构造正例与 validate_assignment 写入期拦截
   （空提取正文、空白译文等非法改写不得半写生效）；
3. GalIRProject id 全局唯一性：构造期、整体重赋值期双重拦截；
4. GalIRProject 容器协议：get_unit O(1) 检索、__len__、__iter__、__contains__；
5. dump_json / model_validate_json 跨进程序列化往返一致性
   （含私有索引不落盘、再序列化字节稳定）。
"""

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.models.ir import GalIRProject, TranslationUnit
from core.models.status import TranslationStatus
from core.models.tags import AtomicTag, PairedTag

NL = chr(92) + "n"  # 换行控制符的脚本字面形式：反斜杠 + n 两个字符


def _mini_project(*units: TranslationUnit) -> GalIRProject:
    """构造最小合法项目容器，供唯一性等专项断言复用。"""
    return GalIRProject(
        project_name="迷你工程",
        source_lang="ja",
        target_lang="zh-CN",
        engine_type="psb",
        units=list(units),
    )


# ---------------------------------------------------------------------------
# 固定样本
# ---------------------------------------------------------------------------


@pytest.fixture
def ruby_tag() -> AtomicTag:
    return AtomicTag(tag_id="ruby", raw_tag="[ruby text=しずか]", position=28)


@pytest.fixture
def color_pair() -> PairedTag:
    return PairedTag(start_tag="<color=0xFF0000>", end_tag="</color>", inner_text="泣くな…")


@pytest.fixture
def alice_unit(ruby_tag: AtomicTag, color_pair: PairedTag) -> TranslationUnit:
    return TranslationUnit(
        id="scn01-000042",
        speaker="アリス",
        raw_text="<color=0xFF0000>泣くな…</color>[ruby text=しずか]%d" + NL,
        extracted_text="泣くな…静か",
        atomic_tags=[
            ruby_tag,
            AtomicTag(tag_id="format_int", raw_tag="%d", position=43),
            AtomicTag(tag_id="newline", raw_tag=NL, position=45),
        ],
        paired_tags=[color_pair],
        metadata={"psb_offset": 123456},
    )


@pytest.fixture
def narration_unit() -> TranslationUnit:
    return TranslationUnit(id="scn01-000043", raw_text="……。", extracted_text="……。")


@pytest.fixture
def system_unit() -> TranslationUnit:
    return TranslationUnit(id="scn01-000044", raw_text="……", extracted_text="……")


@pytest.fixture
def project(alice_unit, narration_unit) -> GalIRProject:
    return GalIRProject(
        project_name="苍之彼方的四重奏",
        source_lang="ja",
        target_lang="zh-CN",
        engine_type="psb",
        units=[alice_unit, narration_unit],
    )


# ---------------------------------------------------------------------------
# 1a. TranslationStatus 枚举契约
# ---------------------------------------------------------------------------


class TestTranslationStatus:
    """枚举契约：成员集合钉死、str 混入、按值反解与序列化字面值。"""

    def test_member_set_is_pinned(self):
        """六个状态成员与顺序是不可协商契约，增删改名必须显式过审。"""
        assert [s.name for s in TranslationStatus] == [
            "RAW",
            "EXTRACTED",
            "TRANSLATED",
            "LQA_PASSED",
            "LQA_FAILED",
            "EXPORTED",
        ]

    def test_str_mixin_direct_equality(self):
        assert TranslationStatus.RAW == "RAW"
        assert TranslationStatus.LQA_PASSED == "LQA_PASSED"

    def test_lookup_by_value_returns_member(self):
        assert TranslationStatus("EXPORTED") is TranslationStatus.EXPORTED

    def test_json_serializes_as_plain_literal(self):
        """str 混入使枚举可直接落入标准 json 报文，输出裸字面值。"""
        assert json.dumps({"status": TranslationStatus.RAW}) == '{"status": "RAW"}'


# ---------------------------------------------------------------------------
# 1b. AtomicTag / PairedTag 契约
# ---------------------------------------------------------------------------


class TestAtomicTagContract:
    """原子标记：构造正例、frozen、序列化与按值哈希；非法输入构造期全拒。"""

    def test_valid_construction(self, ruby_tag):
        assert ruby_tag.tag_id == "ruby"
        assert ruby_tag.raw_tag == "[ruby text=しずか]"
        assert ruby_tag.position == 28

    def test_frozen_rejects_in_place_mutation(self, ruby_tag):
        with pytest.raises(ValidationError):
            ruby_tag.position = 1

    def test_json_roundtrip_and_value_hash(self, ruby_tag):
        revived = AtomicTag.model_validate_json(ruby_tag.model_dump_json())
        assert revived == ruby_tag
        assert hash(revived) == hash(ruby_tag)
        assert len({ruby_tag, revived}) == 1  # 同值实例在集合中天然去重

    @pytest.mark.parametrize(
        "overrides",
        [
            {"tag_id": ""},
            {"tag_id": "   "},
            {"raw_tag": ""},
            {"raw_tag": " \t "},
            {"position": -1},
            {"position": -100},
            {"unexpected": 1},
        ],
        ids=["空tag_id", "空白tag_id", "空raw_tag", "空白raw_tag", "负偏移-1", "负偏移-100", "未知字段"],
    )
    def test_invalid_construction_rejected(self, overrides):
        base = {"tag_id": "newline", "raw_tag": NL, "position": 0}
        with pytest.raises(ValidationError):
            AtomicTag(**{**base, **overrides})


class TestPairedTagContract:
    """成对标记：开闭标记必须非空白；inner_text 是可译正文，允许空串。"""

    def test_valid_construction(self, color_pair):
        assert color_pair.start_tag == "<color=0xFF0000>"
        assert color_pair.end_tag == "</color>"
        assert color_pair.inner_text == "泣くな…"

    def test_empty_inner_text_is_legal(self):
        pair = PairedTag(start_tag="<b>", end_tag="</b>", inner_text="")
        assert pair.inner_text == ""

    def test_frozen_rejects_in_place_mutation(self, color_pair):
        with pytest.raises(ValidationError):
            color_pair.inner_text = "x"

    def test_json_roundtrip(self, color_pair):
        assert PairedTag.model_validate_json(color_pair.model_dump_json()) == color_pair

    @pytest.mark.parametrize(
        "overrides",
        [
            {"start_tag": ""},
            {"start_tag": "  "},
            {"end_tag": ""},
            {"end_tag": " \t "},
            {"unexpected": 1},
        ],
        ids=["空start", "空白start", "空end", "空白end", "未知字段"],
    )
    def test_invalid_construction_rejected(self, overrides):
        base = {"start_tag": "<b>", "end_tag": "</b>", "inner_text": "文本"}
        with pytest.raises(ValidationError):
            PairedTag(**{**base, **overrides})


# ---------------------------------------------------------------------------
# 2. TranslationUnit 契约
# ---------------------------------------------------------------------------


class TestTranslationUnitContract:
    """翻译单元：构造正例/默认值、构造期拒绝、写入期拦截与合法状态推进。"""

    def test_minimal_construction_defaults(self):
        unit = TranslationUnit(id="u-1", raw_text="……。", extracted_text="……。")
        assert unit.speaker is None  # None 即旁白契约
        assert unit.atomic_tags == [] and unit.paired_tags == []
        assert unit.translated_text is None
        assert unit.status is TranslationStatus.RAW
        assert unit.metadata == {}

    def test_full_construction(self, alice_unit):
        assert alice_unit.id == "scn01-000042"
        assert alice_unit.speaker == "アリス"
        assert len(alice_unit.atomic_tags) == 3
        assert alice_unit.atomic_tags[1].position == 43
        assert alice_unit.paired_tags[0].inner_text == "泣くな…"
        assert alice_unit.metadata == {"psb_offset": 123456}
        assert alice_unit.raw_text.endswith(NL)

    def test_mutable_defaults_not_shared(self, narration_unit, system_unit):
        """default_factory 必须产出独立容器，杜绝跨实例可变默认值泄漏。"""
        assert narration_unit.metadata is not system_unit.metadata
        assert narration_unit.atomic_tags is not system_unit.atomic_tags
        narration_unit.metadata["k"] = "v"
        assert system_unit.metadata == {}

    @pytest.mark.parametrize(
        "overrides",
        [
            {"id": ""},
            {"id": "   "},
            {"raw_text": ""},
            {"raw_text": " "},
            {"extracted_text": ""},
            {"extracted_text": "  "},
            {"speaker": ""},
            {"speaker": "\t"},
            {"translated_text": ""},
            {"translated_text": "   "},
            {"unexpected": 1},
        ],
        ids=[
            "空id", "空白id",
            "空raw", "空白raw",
            "空提取正文", "空白提取正文",
            "空speaker", "空白speaker",
            "空译文", "空白译文",
            "未知字段",
        ],
    )
    def test_invalid_construction_rejected(self, overrides):
        base = {"id": "u-1", "raw_text": "テスト", "extracted_text": "テスト"}
        with pytest.raises(ValidationError):
            TranslationUnit(**{**base, **overrides})

    def test_assignment_allows_pipeline_progression(self, alice_unit):
        """合法推进放行：状态前移与译文回填必须畅通。"""
        alice_unit.status = TranslationStatus.TRANSLATED
        alice_unit.translated_text = "别哭……"
        assert alice_unit.status is TranslationStatus.TRANSLATED
        assert alice_unit.translated_text == "别哭……"

    @pytest.mark.parametrize(
        ("field", "evil"),
        [
            ("translated_text", ""),
            ("translated_text", "   "),
            ("extracted_text", ""),
            ("extracted_text", " \t "),
            ("id", ""),
            ("id", "   "),
            ("raw_text", ""),
            ("speaker", ""),
        ],
        ids=[
            "空译文写入", "空白译文写入",
            "空提取正文写入", "空白提取正文写入",
            "空id写入", "空白id写入",
            "空raw写入", "空speaker写入",
        ],
    )
    def test_assignment_intercepts_invalid_writes(self, alice_unit, field, evil):
        """validate_assignment 写入期拦截：非法值不得残留半写状态。"""
        before = getattr(alice_unit, field)
        with pytest.raises(ValidationError):
            setattr(alice_unit, field, evil)
        assert getattr(alice_unit, field) == before


# ---------------------------------------------------------------------------
# 3 & 4. GalIRProject 唯一性与容器协议
# ---------------------------------------------------------------------------


class TestGalIRProjectContainer:
    """项目容器：唯一性双重拦截、O(1) 检索、容器协议与索引自愈。"""

    def test_valid_construction_and_lookup(self, project, alice_unit, narration_unit):
        assert project.get_unit(alice_unit.id) is alice_unit
        assert project.get_unit(narration_unit.id) is narration_unit

    def test_missing_id_raises_key_error(self, project):
        with pytest.raises(KeyError, match="ghost-id"):
            project.get_unit("ghost-id")

    def test_duplicate_id_rejected_at_construction(self, alice_unit):
        with pytest.raises(ValidationError, match="id 重复"):
            _mini_project(alice_unit, alice_unit.model_copy())

    def test_duplicate_id_rejected_on_wholesale_reassignment(self, project, alice_unit):
        with pytest.raises(ValidationError, match="id 重复"):
            project.units = [alice_unit, alice_unit.model_copy()]

    def test_wholesale_reassignment_rebuilds_index_eagerly(
        self, project, narration_unit, system_unit
    ):
        """等长替换场景：长度漂移检测无法兜底，必须依赖赋值期即时重建。"""
        project.units = [narration_unit, system_unit]
        assert project.get_unit(system_unit.id) is system_unit
        with pytest.raises(KeyError):
            project.get_unit("scn01-000042")

    def test_container_protocol(self, project, alice_unit, narration_unit):
        assert len(project) == 2
        assert list(project) == [alice_unit, narration_unit]
        assert alice_unit.id in project
        assert narration_unit in project  # 实例形态判断
        assert "ghost-id" not in project
        assert None not in project and 42 not in project  # 非法类型返回 False 而非报错

    def test_index_self_heals_after_external_append(self, project, system_unit):
        """绕过校验的直接 append 后，读取凭长度漂移自愈重建索引。"""
        project.units.append(system_unit)
        assert project.get_unit(system_unit.id) is system_unit
        assert system_unit.id in project
        assert len(project) == 3

    def test_empty_project_is_legal(self):
        assert len(_mini_project()) == 0
        assert list(_mini_project()) == []


# ---------------------------------------------------------------------------
# 5. 跨进程序列化往返
# ---------------------------------------------------------------------------


class TestCrossProcessRoundTrip:
    """JSON 跨进程边界：深度等价、字节稳定、私有索引不落盘。"""

    def test_project_deep_roundtrip_equality(self, project):
        revived = GalIRProject.model_validate_json(project.model_dump_json())
        assert revived == project  # 逐字段深度等价
        assert revived.units[0] is not project.units[0]  # 全新对象图（跨进程语义）
        assert revived.get_unit("scn01-000042") is revived.units[0]

    def test_roundtrip_preserves_nested_contract_state(self, project):
        revived = GalIRProject.model_validate_json(project.model_dump_json())
        unit = revived.units[0]
        assert unit.status is TranslationStatus.RAW
        assert unit.metadata == {"psb_offset": 123456}
        assert unit.paired_tags[0].inner_text == "泣くな…"
        assert unit.raw_text.endswith(NL)
        with pytest.raises(ValidationError):  # 内嵌标记往返后仍保持 frozen
            unit.atomic_tags[0].position = 0

    def test_reserialization_is_byte_stable(self, project):
        """再序列化字节稳定：同一对象图两次落盘结果必须逐字节一致。"""
        revived = GalIRProject.model_validate_json(project.model_dump_json())
        assert revived.model_dump_json() == project.model_dump_json()

    def test_private_index_never_serialized(self, project):
        assert "_index" not in project.model_dump_json()

    def test_unit_json_roundtrip(self, alice_unit):
        revived = TranslationUnit.model_validate_json(alice_unit.model_dump_json())
        assert revived == alice_unit
        assert revived.id == "scn01-000042"
