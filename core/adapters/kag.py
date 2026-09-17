"""KAG（Kirikiri Adventure Game）剧本适配器 —— 首个具体引擎实现。

职责边界与取舍（轻量实用，杜绝过度设计）：

* 逐行文本层：对话行（可选 【角色名】 前缀）与旁白行生成
  TranslationUnit；`;` 注释、`*` 标签、`@` 命令与纯标记行属脚本骨架，
  不产生单元，经 ``units[0].metadata["kag_source"]`` 原文保存，
  回写时按 ``metadata["kag_line"]`` 行号原位替换，骨架逐行保真；
* 行内 ``[宏]`` 指令提取为 AtomicTag（position 为 raw_text 内偏移，
  与 LQA check_atomic_conservation 的锚点语义直接对齐）；回写时先把
  raw_text 偏移换算到**剥离宏之后的正文坐标系**（减前缀长度，并逐个
  扣除排在该宏之前的宏长度），再按「夹取并前移」策略复写：越界夹取到
  正文端点，乱序登记排序后前移防倒退，保证输出确定可复现；
* 编码策略：读取嗅探 UTF-8 → CP932（仅读取用）；回写统一 UTF-8
  带 BOM——Kirikiri 自带 BOM 嗅探，且简体中文译文超出 CP932 表示域；
* 行内 ``[voice ...]`` 宏（若存在）额外解析为 ``metadata["audio"]``
  （``file`` + 可选 ``duration_ms`` / ``comp_ms``），供壳层 Inspector 音频卡消费；
  该字段是**纯增量**的，不参与 raw_text 重建，对回写与 LQA 锚点零影响；
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
_MACRO_NAME_RE = re.compile(r"^\[\s*([a-zA-Z][a-zA-Z0-9_]*)")

_VOICE_MACRO_NAMES = frozenset({"voice"})
"""被视为「本行有配音」的宏名（整词匹配，小写比较）。

刻意不含 ``se`` / ``bgm`` / ``play`` —— 它们描述音效与背景乐，
不构成「这一行有人声」的证据；把它们算进来会让音频卡对绝大多数行误报。
"""

_AUDIO_FILE_PARAM_RE = re.compile(r'(?:file|storage|src)\s*=\s*"([^"]+)"')
_AUDIO_QUOTED_RE = re.compile(r'"([^"]+)"')
_AUDIO_NUMBER_PARAM_RES = {
    "duration_ms": re.compile(r"duration_ms\s*=\s*(\d+)"),
    "comp_ms": re.compile(r"comp_ms\s*=\s*(\d+)"),
}


def _decode(raw: bytes) -> tuple[str, str]:
    """嗅探解码：UTF-8 优先，失败回退 CP932；换行统一归一为 LF。

    不归一的话，CRLF 源文件的骨架行会携带尾部 \\r 进入 kag_source，
    回写时与 write_text 的换行翻译叠加成 \\r\\r\\n 双回车，骨架被撑破。
    """
    for encoding in ("utf-8", "cp932"):
        try:
            text = raw.decode(encoding).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
        return text.replace("\r\n", "\n").replace("\r", "\n"), encoding
    raise EngineAdapterError("KAG 脚本既非 UTF-8 也非 CP932，无法解码")


def _reinsert_tags(base: str, tags: list[AtomicTag], prefix_len: int) -> str:
    """把登记的宏标记按 raw_text 偏移复写进 base（译文或原文正文）。

    位置换算（关键）：``tag.position`` 相对 raw_text，而 raw_text **本身包含
    这些宏**；base 则是已把全部宏剥离后的正文。因此只减前缀长度是不够的
    —— 还必须扣除「排在该宏之前的全部宏长度」，锚点才落在 base 的坐标系上。

    漏掉这一步的后果（实测于 tests/fixtures/sample_act1.ks）：multi-macro 行的
    锚点会全部超出 ``len(base)`` 而被统一夹到行尾，``[font size=default]``
    与 ``[r]`` 这类复位/换行宏整体后移，未改动任何译文即已改变渲染语义。

    译文长度改变后该锚点仍只是近似位置：越界夹取到 ``[0, len(base)]``，
    排序后游标前移保证次序确定，输出可复现。
    """
    parts: list[str] = []
    cursor = 0
    removed = 0
    for tag in sorted(tags, key=lambda t: t.position):
        rel = min(max(tag.position - prefix_len - removed, 0), len(base))
        rel = max(rel, cursor)
        parts.append(base[cursor:rel])
        parts.append(tag.raw_tag)
        cursor = rel
        removed += len(tag.raw_tag)
    parts.append(base[cursor:])
    return "".join(parts)


def _voice_metadata(tags: list[AtomicTag]) -> dict[str, Any] | None:
    """从行内宏里提取语音元数据；无语音宏时返回 None。

    只认宏名**整词**等于 ``voice`` 的行内指令，形如::

        [voice file="vo_00012.ogg" duration_ms=1850 comp_ms=200]

    取舍说明：这是一条确定的语法规则，不是启发式猜测 —— ``se`` / ``bgm`` /
    ``play`` 一律不算，它们描述音效与背景乐，不构成「本行有人声」的证据。
    之所以把这条便利做进适配器，是因为 ``metadata["audio"]`` 的消费方
    （Inspector 音频卡）需要有内容可展示；引擎适配器若能从更可靠的来源
    （封包伴生索引、语音清单）拿到音频信息，应直接写入同名字段，
    本函数只作为缺省兜底。

    产物只进 ``metadata``，**不触碰 raw_text / extracted_text / atomic_tags**，
    因此对回写保真与 LQA 锚点守恒零影响。
    """
    for tag in tags:
        name_match = _MACRO_NAME_RE.match(tag.raw_tag)
        if name_match is None or name_match.group(1).lower() not in _VOICE_MACRO_NAMES:
            continue
        file_match = _AUDIO_FILE_PARAM_RE.search(tag.raw_tag) or _AUDIO_QUOTED_RE.search(
            tag.raw_tag
        )
        if file_match is None:
            continue  # [voice] 未给资源名：不足以构成可播放线索，忽略
        audio: dict[str, Any] = {"file": file_match.group(1)}
        for key, pattern in _AUDIO_NUMBER_PARAM_RES.items():
            number = pattern.search(tag.raw_tag)
            if number is not None:
                audio[key] = int(number.group(1))
        return audio
    return None


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
            metadata: dict[str, Any] = {
                "kag_line": lineno,
                "kag_indent": line[: len(line) - len(line.lstrip())],
                "kag_encoding": encoding,
            }
            audio = _voice_metadata(tags)
            if audio is not None:
                metadata["audio"] = audio
            units.append(
                TranslationUnit(
                    id=f"{file_path.stem}-{lineno:05d}",
                    speaker=speaker,
                    raw_text=stripped,
                    extracted_text=extracted,
                    atomic_tags=tags,
                    status=TranslationStatus.EXTRACTED,
                    metadata=metadata,
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
