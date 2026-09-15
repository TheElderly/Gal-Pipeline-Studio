"""core/adapters/kag.py 契约回归测试（KagAdapter 自测固化）。

覆盖范围：

1. detect 判定：典型 KAG 内容被认领（含重命名 .txt 的后缀无关性），
   纯文本拒绝，缺失文件安全返回 False；
2. extract_to_ir 提取：【角色名】剥离、行内 [宏] 登记 AtomicTag
   （锚点与 LQA 守恒语义对齐）、纯控制符行按 IR 准入规则跳过；
3. ir_to_asset 回写保真：译步行宏标记复写、注释/跳转标签/@ 命令
   骨架逐字保留、未翻译行按原文逐字节重建；
4. 编码规范：导出文件带 UTF-8 BOM（Kirikiri 嗅探安全交集）。
"""

import sys
from pathlib import Path

import pytest

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.kag import KagAdapter
from core.lqa.rules import check_atomic_conservation
from core.models.status import TranslationStatus

KAG_SAMPLE = (
    "; KAG scenario\n"
    "*start\n"
    "@layopt layer=0 visible=false\n"
    '【アリス】泣くな…[ruby text="しずか"]静かに[r]\n'
    "これはテストです[p]\n"
    "[r][p]\n"
    "【ボブ】[r]それでも[r]\n"
    "; tail comment\n"
)


@pytest.fixture
def adapter() -> KagAdapter:
    return KagAdapter()


@pytest.fixture
def ks_file(tmp_path: Path) -> Path:
    """以 CP932 编码写入样本脚本，顺带覆盖源编码嗅探回退路径。"""
    target = tmp_path / "scene.ks"
    target.write_bytes(KAG_SAMPLE.encode("cp932"))
    return target


class TestDetect:
    """detect：特征证据制判定，与文件后缀彻底解耦。"""

    def test_claims_typical_kag_script(self, adapter, ks_file):
        assert adapter.detect(ks_file) is True

    def test_suffix_independent(self, adapter, ks_file, tmp_path):
        renamed = tmp_path / "scene.txt"
        renamed.write_bytes(ks_file.read_bytes())
        assert adapter.detect(renamed) is True

    def test_rejects_plain_text(self, adapter, tmp_path):
        plain = tmp_path / "plain.ks"
        plain.write_bytes(b"hello [world] plain ascii\n")
        assert adapter.detect(plain) is False

    def test_rejects_missing_file(self, adapter, tmp_path):
        assert adapter.detect(tmp_path / "ghost.ks") is False


class TestExtractToIr:
    """extract_to_ir：角色名、宏标记登记、纯控制符行准入规则。"""

    @pytest.fixture
    def project(self, adapter, ks_file):
        return adapter.extract_to_ir(ks_file)

    def test_speaker_extraction(self, project):
        alice, narration, bob = project.units
        assert alice.speaker == "アリス"
        assert narration.speaker is None  # 旁白契约
        assert bob.speaker == "ボブ"

    def test_macros_registered_as_atomic_tags(self, project):
        alice = project.units[0]
        assert [t.raw_tag for t in alice.atomic_tags] == [
            '[ruby text="しずか"]',
            "[r]",
        ]
        assert alice.extracted_text == "泣くな…静かに"
        assert alice.status is TranslationStatus.EXTRACTED
        assert check_atomic_conservation(alice) == []  # 锚点与 LQA 守恒语义对齐

    def test_pure_control_line_skipped(self, project):
        """[r][p] 行无可译正文，不构成翻译单元。"""
        assert len(project) == 3
        assert all(unit.raw_text != "[r][p]" for unit in project)


class TestIrToAsset:
    """ir_to_asset：原位回写保真与编码规范。"""

    @pytest.fixture
    def written_lines(self, adapter, ks_file) -> list[str]:
        project = adapter.extract_to_ir(ks_file)
        alice = project.units[0]
        alice.translated_text = "别哭……静一点"
        alice.status = TranslationStatus.TRANSLATED
        out = adapter.ir_to_asset(project, ks_file.parent / "out")
        assert out == ks_file.parent / "out" / "scene.ks"
        return out.read_text(encoding="utf-8-sig").split("\n")

    def test_translated_unit_macros_rewritten(self, written_lines):
        assert written_lines[3] == "【アリス】别哭……[ruby text=\"しずか\"]静一点[r]"

    def test_skeleton_lines_preserved_verbatim(self, written_lines):
        assert written_lines[0] == "; KAG scenario"
        assert written_lines[1] == "*start"  # 跳转标签，丢一字即断流
        assert written_lines[2] == "@layopt layer=0 visible=false"
        assert written_lines[5] == "[r][p]"  # 纯控制符行同样属骨架，原样保留
        assert written_lines[7] == "; tail comment"

    def test_untranslated_line_rebuilt_identically(self, written_lines):
        assert written_lines[4] == "これはテストです[p]"

    def test_output_carries_utf8_bom(self, adapter, ks_file):
        project = adapter.extract_to_ir(ks_file)
        out = adapter.ir_to_asset(project, ks_file.parent / "out2")
        assert out.read_bytes().startswith(b"\xef\xbb\xbf")
