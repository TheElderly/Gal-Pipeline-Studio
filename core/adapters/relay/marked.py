"""MarkedTextAdapter —— 汉化组经典标记文本与 Gal-IR 的双向中继。

中间文本形态（canonical）::

    ○0001○原文セリフ
    ●0001●译文行（可缺省 = 待翻译）

    其余任意行（注释、空行、场景标题……）为**不透明行**：抽取时
    跳过、回写时逐字节保留。

保真纪律（Pristine Roundtrip）::

    * 全文按 ``splitlines(keepends=True)`` 处理，行终止符不归一；
    * 单元 metadata 冻结 ``target_line_raw``（●行原文，含终止符）：
      未翻译或译文未变的单元回写时**原样还原**该行；
    * 新增译文的 ● 行插在对应 ○ 行之后，终止符复用该 ○ 行的；
    * 序号宽度（如 0001 的 4 位）从原文探测，回写保持同宽零填充。

标记模板可覆写：社区工具的变体（全角数字、不同圈号）通过子类
覆写 ``SOURCE_MARKER`` / ``TARGET_MARKER`` 承载，严禁硬编码分支。
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..base import BaseEngineAdapter, UnsupportedFormatError
from ..image import normalize_to_rgba_png32
from ...models.ir import GalIRProject, TranslationUnit
from ...models.status import TranslationStatus


@dataclass(frozen=True)
class _SourceHit:
    number: int
    width: int
    line_index: int
    text: str


class MarkedTextAdapter(BaseEngineAdapter):
    """○N○/●N● 标记文本 ↔ Gal-IR。"""

    SOURCE_MARKER = re.compile(r"^○(\d+)○\s?(.*)$", re.DOTALL)
    TARGET_MARKER = re.compile(r"^●(\d+)●\s?(.*)$", re.DOTALL)

    def detect(self, file_path: Path) -> bool:
        """特征判定：存在 ≥1 行 ○N○ 源标记（与编码无关，结构即魔数）。"""
        try:
            text = file_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError, UnicodeError):
            return False
        return any(self.SOURCE_MARKER.match(line) for line in text.splitlines())

    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        lines = self._read_lines(file_path)
        units: list[TranslationUnit] = []
        by_number: dict[int, TranslationUnit] = {}
        stem = file_path.stem

        for index, line in enumerate(lines):
            # 行终止符不参与标记匹配（keepends 只为回写保真，语义看内容）
            content = line.rstrip("\r\n")
            source_match = self.SOURCE_MARKER.match(content)
            if source_match is None:
                # ●行可能先于/独立于 ○行出现：记录候选，按序号配对
                target_match = self.TARGET_MARKER.match(content)
                if target_match is not None:
                    existing = by_number.get(int(target_match.group(1)))
                    if existing is not None and existing.translated_text is None:
                        self._attach_target(existing, index, line, target_match)
                continue

            number = int(source_match.group(1))
            unit = TranslationUnit(
                id=f"{stem}-{number:0{len(source_match.group(1))}d}",
                speaker=None,
                raw_text=source_match.group(2),
                extracted_text=source_match.group(2),
                status=TranslationStatus.EXTRACTED,
                metadata={
                    "relay": {
                        "adapter": self.engine_key,
                        "source": str(file_path),
                        "source_line": index,
                        "number": number,
                        "width": len(source_match.group(1)),
                    }
                },
            )
            units.append(unit)
            by_number[number] = unit

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
            original_line = relay.get("target_line_raw")
            if unit.translated_text is None or (
                original_line is not None
                and unit.translated_text == self._target_text(str(original_line))
            ):
                continue  # 未翻译 / 译文未变：整行原样保留（Pristine 基座）

            number = int(relay["number"])
            width = int(relay["width"])
            replacement = f"●{number:0{width}d}●{unit.translated_text}"
            target_index = relay.get("target_line")
            if target_index is not None:
                lines[int(target_index)] = replacement + self._line_ending(
                    lines[int(target_index)]
                )
            else:
                source_index = int(relay["source_line"])
                lines.insert(
                    source_index + 1,
                    replacement + self._line_ending(lines[source_index]),
                )
                self._shift_line_numbers(project, unit.id, 1)

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / source_path.name
        encoding = "utf-8-sig" if _has_utf8_bom(source_path) else "utf-8"
        output_path.write_text("".join(lines), encoding=encoding, newline="")
        return output_path

    @property
    def engine_key(self) -> str:
        return "relay.marked"

    @classmethod
    def _read_lines(cls, file_path: Path) -> list[str]:
        try:
            text = file_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise UnsupportedFormatError(f"中间文本不可读：{exc}") from exc
        # keepends：行终止符不归一，Pristine 的前提
        return text.splitlines(keepends=True)

    @staticmethod
    def _line_ending(line: str) -> str:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n") or line.endswith("\r"):
            return line[-1]
        return "\n"

    @staticmethod
    def _target_text(raw_line: str) -> str:
        match = MarkedTextAdapter.TARGET_MARKER.match(raw_line.rstrip("\r\n"))
        return match.group(2) if match else raw_line

    @staticmethod
    def _attach_target(
        unit: TranslationUnit, line_index: int, raw_line: str, match: re.Match[str]
    ) -> None:
        text = match.group(2)
        unit.translated_text = text or None
        unit.status = TranslationStatus.LQA_PASSED if text.strip() else TranslationStatus.EXTRACTED
        relay = unit.metadata["relay"]
        relay["target_line"] = line_index
        relay["target_line_raw"] = raw_line

    @staticmethod
    def _shift_line_numbers(project: GalIRProject, mutated_id: str, delta: int) -> None:
        """插入 ● 行后，同文件后续单元的行号整体平移（行索引审计凭据保持真实）。"""
        mutated_relay = next(
            u for u in project.units if u.id == mutated_id
        ).metadata["relay"]
        inserted_at = int(mutated_relay["source_line"])
        for other in project.units:
            relay = other.metadata["relay"]
            if other.id == mutated_id:
                continue
            if int(relay["source_line"]) > inserted_at:
                relay["source_line"] = int(relay["source_line"]) + delta
            if relay.get("target_line") is not None and int(relay["target_line"]) > inserted_at:
                relay["target_line"] = int(relay["target_line"]) + delta

    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        return normalize_to_rgba_png32(raw_bytes, **kwargs)


def _has_utf8_bom(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(3) == b"\xef\xbb\xbf"
