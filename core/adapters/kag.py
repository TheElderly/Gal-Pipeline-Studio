"""KAG（Kirikiri Adventure Game）剧本适配器 —— 首个具体引擎实现。

职责边界与取舍（轻量实用，杜绝过度设计）：

* 逐行文本层：对话行（可选 【角色名】 前缀）与旁白行生成
  TranslationUnit；`;` 注释、`*` 标签、`@` 命令与纯标记行属脚本骨架，
  不产生单元，经 ``units[0].metadata["kag_source"]`` 原文保存，
  回写时按 ``metadata["kag_line"]`` 行号原位替换，骨架逐行保真；
* 行内 ``[宏]`` 指令提取为 AtomicTag（position 为 raw_text 内偏移，
  与 LQA check_atomic_conservation 的锚点语义直接对齐）；回写按
  「夹取并前移」策略复写进译文：越界夹取到正文端点，乱序登记
  排序后前移防倒退，保证输出确定可复现；
* 编码策略：读取嗅探 UTF-8 → CP932（仅读取用）；回写统一 UTF-8
  带 BOM——Kirikiri 自带 BOM 嗅探，且简体中文译文超出 CP932 表示域；
* 已知边界（非遗漏，本切片不处理）：行内尾注释、``[[`` 转义字面量、
  ``*label|标题`` 的标题（场景标记名，非对话说话人）。
"""

import re
from pathlib import Path
from typing import Any

from .base import BaseEngineAdapter, EngineAdapterError
from .image import normalize_to_rgba_png32
from ..models.ir import GalIRProject, TranslationUnit
from ..models.status import TranslationStatus
from ..models.tags import AtomicTag

_SPEAKER_RE = re.compile(r"^【([^】]+)】")
_MACRO_RE = re.compile(r"\[[a-zA-Z][^\]\n]*\]")


def _decode(raw: bytes) -> tuple[str, str]:
    """嗅探解码：UTF-8 优先，失败回退 CP932；两者皆败属无法管辖。"""
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding).lstrip("\ufeff"), encoding
        except UnicodeDecodeError:
            continue
    raise EngineAdapterError("KAG 脚本既非 UTF-8 也非 CP932，无法解码")


def _reinsert_tags(base: str, tags: list[AtomicTag], prefix_len: int) -> str:
    """把登记的宏标记按 raw_text 偏移复写进 base（译文或原文正文）。

    位置语义：tag.position 相对 raw_text（含 【角色名】 前缀），此处
    减去前缀长度换算为正文相对偏移；译文长度改变后偏移仅是近似锚，
    越界夹取到 [0, len(base)]，排序后游标前移保证次序确定。
    """
    parts: list[str] = []
    cursor = 0
    for tag in sorted(tags, key=lambda t: t.position):
        rel = min(max(tag.position - prefix_len, 0), len(base))
        rel = max(rel, cursor)
        parts.append(base[cursor:rel])
        parts.append(tag.raw_tag)
        cursor = rel
    parts.append(base[cursor:])
    return "".join(parts)


class KagAdapter(BaseEngineAdapter):
    """KAG .ks 剧本适配器：抽取→Gal-IR→汉化回写。"""

    def detect(self, file_path: Path) -> bool:
        """前 512 字节嗅探 KAG 特征：需要 ≥2 类证据（注释/标签/命令/宏）。"""
        try:
            head = file_path.open("rb").read(512)
        except OSError:
            return False
        try:
            text = head.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = head.decode("cp932")
            except UnicodeDecodeError:
                return False
        kinds: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(";"):
                kinds.add("comment")
            elif stripped.startswith("*"):
                kinds.add("label")
            elif stripped.startswith("@"):
                kinds.add("command")
            elif _MACRO_RE.search(stripped):
                kinds.add("macro")
        return len(kinds) >= 2

    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        """逐行解析 .ks：对话/旁白成单元，宏标记登记为 AtomicTag。"""
        text, encoding = _decode(file_path.read_bytes())
        lines = text.split("\n")
        units: list[TranslationUnit] = []
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped[0] in ";*@":
                continue
            speaker_match = _SPEAKER_RE.match(stripped)
            speaker = speaker_match.group(1) if speaker_match else None
            prefix_len = speaker_match.end() if speaker_match else 0
            tags: list[AtomicTag] = []
            pieces: list[str] = []
            cursor = prefix_len
            for macro in _MACRO_RE.finditer(stripped, prefix_len):
                tags.append(
                    AtomicTag(
                        tag_id="macro", raw_tag=macro.group(0), position=macro.start()
                    )
                )
                pieces.append(stripped[cursor : macro.start()])
                cursor = macro.end()
            pieces.append(stripped[cursor:])
            extracted = "".join(pieces).strip()
            if not extracted:
                continue  # 纯标记行：无可译正文，不构成翻译单元
            units.append(
                TranslationUnit(
                    id=f"{file_path.stem}-{lineno:05d}",
                    speaker=speaker,
                    raw_text=stripped,
                    extracted_text=extracted,
                    atomic_tags=tags,
                    status=TranslationStatus.EXTRACTED,
                    metadata={
                        "kag_line": lineno,
                        "kag_indent": line[: len(line) - len(line.lstrip())],
                        "kag_encoding": encoding,
                    },
                )
            )
        if units:
            units[0].metadata["kag_source"] = text
        return GalIRProject(
            project_name=file_path.stem,
            source_lang="ja",
            target_lang="zh-CN",
            engine_type="kag",
            units=units,
        )

    def ir_to_asset(self, project: GalIRProject, output_dir: Path) -> Path:
        """按行号原位替换：骨架行原样，单元行以译文+复写标记重建。"""
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{project.project_name}.ks"
        if not project.units:
            target.write_text("", encoding="utf-8-sig")
            return target
        lines = str(project.units[0].metadata.get("kag_source", "")).split("\n")
        for unit in project.units:
            base = unit.translated_text if unit.translated_text is not None else unit.extracted_text
            prefix = f"【{unit.speaker}】" if unit.speaker else ""
            body = _reinsert_tags(base, unit.atomic_tags, len(prefix))
            lines[int(unit.metadata["kag_line"]) - 1] = unit.metadata["kag_indent"] + prefix + body
        target.write_text("\n".join(lines), encoding="utf-8-sig")
        return target

    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        """图像归一化直接复用共享引擎（纯内存、离线红线由其保证）。"""
        return normalize_to_rgba_png32(raw_bytes, **kwargs)
