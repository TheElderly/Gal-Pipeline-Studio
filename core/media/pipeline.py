"""图像转换管道编排：格式嗅探 → 内置解码 / 外部工具通道 → PNG 落盘。

* TLG5：内置解码（tlg5.py，零外部依赖）；
* TLG6 及未来私有变体：外部工具链 CLI 通道（命令模板按变体登记，
  工具路径由壳层经统一工具链解析后传入 —— 本侧不做工具定位，与
  解包编排同一纪律）；未装配工具时抛带装配引导语义的变体错误；
* 非 TLG 输入直接 UnsupportedFormatError（壳层应对 png/jpg 直接预览，
  不该把它们送进本管道）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..adapters.base import EngineAdapterError, UnsupportedFormatError
from .errors import MediaConversionError, UnsupportedImageVariantError
from .tlg5 import TLG5_RAW_MAGIC, TLG6_RAW_MAGIC, SDS_MAGIC, SDS_HEADER_SIZE, decode_tlg5

_DEFAULT_TIMEOUT_SECONDS = 120.0

# 外部转换命令模板（按图像变体登记；{tool}/{src}/{dst} 由调用侧填充）。
# 草案对齐 arc_conv 风格：`<tool> <src> -o <dst>` —— 接入真实工具时校准即可，
# 编排零改动（与解包配方同一演进路径）。
# 全部占位符必须带引号：仓库路径含空格（如 "Gal-Pipeline Studio"）时
# shell 调用不做引号保护就会在空格处截断（真实踩坑：--basetemp 挪进仓库根后暴露）。
_CONVERT_RECIPES: dict[str, str] = {
    "TLG6": '"{tool}" "{src}" -o "{dst}"',
}


def detect_image_format(data: bytes) -> str | None:
    """嗅探私有图像格式（支持 15 字节 SDS 头前置），未知返回 None。"""
    if data.startswith(SDS_MAGIC):
        data = data[SDS_HEADER_SIZE:]
    if data.startswith(TLG5_RAW_MAGIC):
        return "TLG5"
    if data.startswith(TLG6_RAW_MAGIC):
        return "TLG6"
    return None


def tlg_to_png(src_path: str | Path, dst_path: str | Path) -> dict:
    """把 TLG5 文件无损转换为 PNG，返回元数据（width/height/format）。

    像素级无损：解码产物与 Pillow RGBA/RGB/L 逐字节对齐后落盘。
    """
    src = Path(src_path)
    dst = Path(dst_path)
    data = src.read_bytes()
    fmt = detect_image_format(data)
    if fmt == "TLG6":
        raise UnsupportedImageVariantError(
            "TLG6 需要外部工具链解码（GARbro 等跨进程 CLI）——"
            "请在「工具链」中装配支持的转换工具后重试"
        )
    if fmt != "TLG5":
        raise UnsupportedFormatError(f"不是 TLG 图像：{src.name}")

    width, height, layout, pixels = decode_tlg5(data)

    # Pillow 局部导入：media 管线只在真正落盘时才触碰图像库
    from PIL import Image

    image = Image.frombytes(layout, (width, height), pixels)
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.save(dst, format="PNG")
    return {"dst_path": str(dst), "width": width, "height": height, "format": "TLG5"}


def _convert_with_external_tool(
    src: Path, dst: Path, variant: str, tool_path: str, timeout_seconds: float
) -> dict:
    """外部工具链 CLI 通道：跨进程调用、退出码与空产出双重校验。"""
    template = _CONVERT_RECIPES.get(variant)
    if template is None:
        raise UnsupportedImageVariantError(f"没有登记 {variant} 的外部转换命令模板")

    dst.parent.mkdir(parents=True, exist_ok=True)
    command = template.format(tool=tool_path, src=str(src), dst=str(dst))
    try:
        completed = subprocess.run(
            command, shell=True, capture_output=True, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaConversionError(
            f"外部转换工具超时（>{timeout_seconds:.0f}s）：{tool_path}"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MediaConversionError(
            f"外部转换工具失败（exit {completed.returncode}）：{stderr or tool_path}"
        )
    if not dst.exists() or dst.stat().st_size == 0:
        raise MediaConversionError("外部转换工具报告成功但未产出目标文件")

    return {"dst_path": str(dst), "width": 0, "height": 0, "format": variant}


def convert_image(
    src_path: str | Path,
    dst_path: str | Path,
    tool_path: str | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """统一转换入口：TLG5 走内置解码；其余私有变体走外部工具链通道。

    ``tool_path`` 由壳层经统一工具链（IToolResolver）解析后传入；
    缺省且变体不被内置支持时，抛带装配引导语义的变体错误。
    """
    src = Path(src_path)
    dst = Path(dst_path)
    data = src.read_bytes()
    fmt = detect_image_format(data)

    if fmt == "TLG5":
        return tlg_to_png(src, dst)

    if fmt == "TLG6":
        if not tool_path:
            raise UnsupportedImageVariantError(
                "TLG6 需要外部工具链解码（GARbro 等跨进程 CLI）——"
                "请在「工具链」中装配支持的转换工具后重试"
            )
        return _convert_with_external_tool(src, dst, "TLG6", tool_path, timeout_seconds)

    raise UnsupportedFormatError(f"不支持的图像格式：{src.name}")


__all__ = [
    "MediaConversionError",
    "UnsupportedImageVariantError",
    "EngineAdapterError",
    "detect_image_format",
    "convert_image",
    "tlg_to_png",
]
