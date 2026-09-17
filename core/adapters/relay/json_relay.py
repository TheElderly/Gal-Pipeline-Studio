"""JsonAdapter —— 结构化文本树（FreeMote PSB 反编译导出）与 Gal-IR 的双向中继。

中间文本形态::

    FreeMote.Psb.exe 的 PsbDecompile 产出**行导向**的缩进 JSON
    （一行一个键值对）。本适配器按「文本键白名单」抽取对白与
    名字节点 —— 白名单之外的一切键（脚本体、坐标、资源路径、
    版本号……）天然锁定：既不进 Gal-IR，也不可能在回写时被触碰。

    "name": "千代",
    "text": "「真実を知る覚悟は、もうできているの？」",

保真纪律（Pristine Roundtrip）::

    **不做 json.loads/dumps 全文往返** —— stdlib 序列化会重排缩进
    与转义风格，逐字节一致立刻破产。回写是**行内外科替换**：仅对
    命中单元所在行的字符串字面值做转义级替换（缩进 / 尾逗号 /
    注释 / 其余行分毫不动）。未翻译单元整行原样保留。
    语义等价由「编译器（FreeMote）重解析」兜底，格式保真由外科
    替换保证 —— 两头都不欠账。

名字节点的归属语义：``name`` 行**先于** ``text`` 行出现时，该名字
挂接为随后第一个文本单元的 speaker（PSB 对白对象的典型排布）。
"""

import json
import re
from pathlib import Path
from typing import Any

from ..base import BaseEngineAdapter, UnsupportedFormatError
from ..image import normalize_to_rgba_png32
from ...models.ir import GalIRProject, TranslationUnit
from ...models.status import TranslationStatus

DEFAULT_TEXT_KEYS = ("text", "message", "msg", "original")
DEFAULT_NAME_KEYS = ("name", "speaker", "who", "character")

_LINE_RE = re.compile(
    r'^(\s*)"(?P<key>(?:[^"\\]|\\.)+)"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"(?P<tail>,?)\s*$'
)


class JsonAdapter(BaseEngineAdapter):
    """行导向 JSON 文本树 ↔ Gal-IR（非文本节点锁定）。"""

    def __init__(
        self,
        text_keys: tuple[str, ...] = DEFAULT_TEXT_KEYS,
        name_keys: tuple[str, ...] = DEFAULT_NAME_KEYS,
    ) -> None:
        self._text_keys = frozenset(text_keys)
        self._name_keys = frozenset(name_keys)
        self._key_pattern = re.compile(
            r'"(?P<key>(?:[^"\\]|\\.)+)"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"'
        )

    def detect(self, file_path: Path) -> bool:
        """特征判定：JSON 起始 + 存在文本键字符串行（结构 + 语义双确认）。"""
        try:
            text = file_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError, UnicodeError):
            return False
        stripped = text.lstrip()
        if not stripped.startswith("{"):
            return False
        return any(
            (match := self._key_pattern.search(line)) is not None
            and self._unquote(match.group("key")) in self._text_keys
            for line in text.splitlines()
        )

    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        lines = self._read_lines(file_path)
        units: list[TranslationUnit] = []
        pending_speaker: str | None = None
        stem = file_path.stem

        for index, line in enumerate(lines):
            match = self._key_pattern.search(line)
            if match is None:
                continue
            key = self._unquote(match.group("key"))
            value = self._unquote(match.group("value"))
            if key in self._name_keys:
                pending_speaker = value or None
                continue
            if key not in self._text_keys or not value.strip():
                pending_speaker = None if key in self._name_keys else pending_speaker
                continue

            units.append(
                TranslationUnit(
                    id=f"{stem}-{index:05d}",
                    speaker=pending_speaker,
                    raw_text=value,
                    extracted_text=value,
                    status=TranslationStatus.EXTRACTED,
                    metadata={
                        "relay": {
                            "adapter": self.engine_key,
                            "source": str(file_path),
                            "line": index,
                            "key": key,
                            "original_line": line,
                        }
                    },
                )
            )
            pending_speaker = None

        return GalIRProject(
            project_name=stem,
            source_lang="ja",
            target_lang="zh-CN",
            engine_type=self.engine_key,
            units=units,
        )

    def ir_to_asset(self, project: GalIRProject, output_dir: Path) -> Path:
        first = next(iter(project.units), None)
        if first is None:
            raise UnsupportedFormatError("中继回写缺少单元：无法定位中间源文件")
        source_path = Path(str(first.metadata["relay"]["source"]))
        lines = self._read_lines(source_path)

        for unit in project.units:
            relay = unit.metadata["relay"]
            original_line = str(relay["original_line"])
            if unit.translated_text is None or unit.translated_text == unit.raw_text:
                continue  # 未翻译 / 译文同原文：整行原样保留（Pristine 基座）
            key = str(relay["key"])
            escaped = json.dumps(unit.translated_text, ensure_ascii=False)[1:-1]
            # 行内外科替换：缩进 / 尾逗号 / 键名分毫不动，仅换字符串字面值
            lines[int(relay["line"])] = self._replace_value(original_line, key, escaped)

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / source_path.name
        encoding = "utf-8-sig" if _has_utf8_bom(source_path) else "utf-8"
        output_path.write_text("".join(lines), encoding=encoding, newline="")
        return output_path

    @property
    def engine_key(self) -> str:
        return "relay.json"

    def _replace_value(self, line: str, key: str, escaped_value: str) -> str:
        """行内替换指定键的字符串字面值，缩进 / 尾逗号 / 键名分毫不动。"""
        pattern = re.compile(
            r'("(?P<key>(?:[^"\\]|\\.)+)"\s*:\s*")(?P<value>(?:[^"\\]|\\.)*)(")'
        )
        for match in pattern.finditer(line):
            if self._unquote(match.group("key")) == key:
                start, end = match.span("value")
                return line[:start] + escaped_value + line[end:]
        # 抽取与回写读的是同一文件，理论不可达；防御性报错优于静默跳过
        raise UnsupportedFormatError(f"回写定位失败：行内未找到键 {key!r}")

    @staticmethod
    def _unquote(raw: str) -> str:
        try:
            return json.loads(f'"{raw}"')
        except json.JSONDecodeError:
            return raw

    @classmethod
    def _read_lines(cls, file_path: Path) -> list[str]:
        try:
            text = file_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise UnsupportedFormatError(f"中间文本不可读：{exc}") from exc
        return text.splitlines(keepends=True)

    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        return normalize_to_rgba_png32(raw_bytes, **kwargs)


def _has_utf8_bom(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(3) == b"\xef\xbb\xbf"
