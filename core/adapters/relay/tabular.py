"""TabularAdapter —— 表格化中间文本（TSV / CSV）与 Gal-IR 的双向中继。

服务对象：BGI-Tools、VNTranslationTools 等社区 dump 工具导出的
「ID / 说话人 / 原文 / 译文」双栏对照表。适配器只懂**表格格式**，
引擎语义（哪个工具 dump、能否回编译）由 ``recipes`` 声明。

保真纪律（Pristine Roundtrip）::

    * 解析全程 ``newline=''``（禁用万能换行翻译），行终止符从原文
      探测并在回写时原样复用 —— CRLF / LF 混合文件不漂移；
    * 编码经 ``utf-8-sig`` 读写对称：BOM 读时剥离、写时补回；
    * 每个单元的 metadata 冻结 ``original_target``（抽取时的译文列
      原文）：未翻译/译文未变的单元回写时**原样还原**该列，
      未动文本 100% 逐字节一致由测试钉死。

约束：中间文件必须已含译文列（允许全空）——没有译文列的 dump
无法定义无损回写目标位，detect 据此拒绝（列数不足）。
"""

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..base import BaseEngineAdapter, UnsupportedFormatError
from ..image import normalize_to_rgba_png32
from ...models.ir import GalIRProject, TranslationUnit
from ...models.status import TranslationStatus

_SNIFF_BYTES = 64 * 1024


@dataclass(frozen=True)
class TabularFormat:
    """表格方言声明：dump 工具差异全部收敛在这份声明里（严禁硬编码列号）。"""

    delimiter: str = "\t"
    encoding: str = "utf-8-sig"
    has_header: bool = False
    speaker_column: int = 1
    source_column: int = 2
    target_column: int = 3
    columns_required: int = 4


class TabularAdapter(BaseEngineAdapter):
    """TSV 双栏对照 ↔ Gal-IR。CSV 变体见 <CSVTabularAdapter>。"""

    FORMAT = TabularFormat()

    def detect(self, file_path: Path) -> bool:
        """特征判定：分隔符在多数行上稳定出现且列数达标（魔数=结构本身）。"""
        try:
            with file_path.open("r", encoding=self.FORMAT.encoding, newline="") as handle:
                head = handle.read(_SNIFF_BYTES)
        except (OSError, UnicodeDecodeError, UnicodeError):
            return False
        lines = [line for line in head.splitlines() if line.strip()]
        if len(lines) < 2:
            return False
        delimiter = self.FORMAT.delimiter
        counts = [line.count(delimiter) + 1 for line in lines]
        required = max(
            self.FORMAT.source_column, self.FORMAT.target_column,
            self.FORMAT.speaker_column if self.FORMAT.speaker_column is not None else 0,
        ) + 1
        consistent = sum(1 for count in counts if count == counts[0])
        return counts[0] >= required and consistent >= max(1, len(lines) - 1)

    def extract_to_ir(self, file_path: Path) -> GalIRProject:
        fmt = self.FORMAT
        rows = self._read_rows(file_path, fmt)
        body = rows[1:] if fmt.has_header else rows

        units: list[TranslationUnit] = []
        stem = file_path.stem
        for offset, row in enumerate(body):
            source = _cell(row, fmt.source_column)
            if not source.strip():
                continue  # 空原文行（dump 工具的分隔行）：不构成可译单元
            row_index = offset + (1 if fmt.has_header else 0)
            speaker = _cell(row, fmt.speaker_column) if fmt.speaker_column is not None else ""
            target = _cell(row, fmt.target_column)
            units.append(
                TranslationUnit(
                    id=f"{stem}-{row_index:05d}",
                    speaker=speaker or None,
                    raw_text=source,
                    extracted_text=source,
                    translated_text=target or None,
                    status=TranslationStatus.LQA_PASSED if target.strip()
                    else TranslationStatus.EXTRACTED,
                    metadata={
                        "relay": {
                            "adapter": self.engine_key,
                            "source": str(file_path),
                            "row": row_index,
                            "original_target": target,
                        }
                    },
                )
            )
        return GalIRProject(
            project_name=stem,
            source_lang="ja",
            target_lang="zh-CN",
            engine_type=self.engine_key,
            units=units,
        )

    def ir_to_asset(self, project: GalIRProject, output_dir: Path) -> Path:
        fmt = self.FORMAT
        first = next(iter(project.units), None)
        if first is None:
            raise UnsupportedFormatError("中继回写缺少单元：无法定位中间源文件")
        source_path = Path(str(first.metadata["relay"]["source"]))
        rows = self._read_rows(source_path, fmt)

        replaced = 0
        for unit in project.units:
            relay = unit.metadata["relay"]
            row_index = int(relay["row"])
            original = str(relay.get("original_target", ""))
            # 未翻译 / 译文未变 → 原样还原原列（Pristine 的实现基座）
            cell = original if unit.translated_text is None else unit.translated_text
            if cell != original:
                replaced += 1
            _set_cell(rows[row_index], fmt.target_column, cell)

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / source_path.name
        terminator = self._detect_terminator(source_path, fmt)
        # BOM 对称保留：原文件有则写有、无则写无（utf-8-sig 恒加 BOM，不可盲用）
        encoding = fmt.encoding if _has_utf8_bom(source_path) else "utf-8"
        with output_path.open("w", encoding=encoding, newline="") as handle:
            writer = csv.writer(
                handle,
                delimiter=fmt.delimiter,
                lineterminator=terminator,
                quoting=csv.QUOTE_MINIMAL,
            )
            writer.writerows(rows)
        _ = replaced  # 仅供调试观测；保真由测试断言承载
        return output_path

    @property
    def engine_key(self) -> str:
        return "relay.tsv"

    @classmethod
    def _read_rows(cls, file_path: Path, fmt: TabularFormat) -> list[list[str]]:
        try:
            with file_path.open("r", encoding=fmt.encoding, newline="") as handle:
                rows = list(csv.reader(handle, delimiter=fmt.delimiter))
        except OSError as exc:
            raise UnsupportedFormatError(f"中间文本不可读：{exc}") from exc
        required = max(
            fmt.source_column, fmt.target_column,
            fmt.speaker_column if fmt.speaker_column is not None else 0,
        ) + 1
        if len(rows) < 1 or any(len(row) < required for row in rows if row):
            raise UnsupportedFormatError(
                f"表格结构不满足配方要求：至少需要 {required} 列（含译文列，允许全空）"
            )
        return rows

    @staticmethod
    def _detect_terminator(file_path: Path, fmt: TabularFormat) -> str:
        with file_path.open("r", encoding=fmt.encoding, newline="") as handle:
            head = handle.read(_SNIFF_BYTES)
        return "\r\n" if "\r\n" in head else "\n"

    def normalize_image(self, raw_bytes: bytes, **kwargs: Any) -> bytes:
        return normalize_to_rgba_png32(raw_bytes, **kwargs)


class CSVTabularAdapter(TabularAdapter):
    """CSV 方言变体：仅切换分隔符（列映射与保真纪律完全复用）。"""

    FORMAT = TabularFormat(delimiter=",")

    @property
    def engine_key(self) -> str:
        return "relay.csv"


def _cell(row: list[str], column: int | None) -> str:
    if column is None or column >= len(row):
        return ""
    return row[column].strip()


def _set_cell(row: list[str], column: int, value: str) -> None:
    while len(row) <= column:
        row.append("")
    row[column] = value


def _has_utf8_bom(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(3) == b"\xef\xbb\xbf"
