"""媒体转换管道的异常分型（统一继承 EngineAdapterError → RPC -32000）。"""

from ..adapters.base import EngineAdapterError


class MediaConversionError(EngineAdapterError):
    """图像转换失败：解码错误、产出校验失败或外部工具异常。"""


class UnsupportedImageVariantError(MediaConversionError):
    """私有图像变体不被内置解码器支持（如 TLG6），需经外部工具链通道。"""
