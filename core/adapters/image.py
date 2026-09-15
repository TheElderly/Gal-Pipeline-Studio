"""离线图像归一化模块 —— 私有格式/BMP 原始字节 → RGBA PNG-32 字节。

数据流定位（契约先行）::

    适配器 (core/adapters) 从封包抽取图像 ──▶ 私有格式/BMP 原始字节
                                                    │ normalize_to_rgba_png32
                                                    │ （本模块，纯内存旁路）
                                                    ▼
                                    RGBA PNG-32 字节 ──▶ WinUI 壳层预览 /
                                    IR metadata 交付 / LQA 视觉比对

实现规范性约束（与 BaseEngineAdapter.normalize_image 的 MUST 条款一一对应）：

* 全程 io.BytesIO 内存作业：本模块根本不 import tempfile，不产生任何
  磁盘临时文件；不发起任何网络调用（技术红线第 2 条：多媒体资产
  100% 本地离线）；
* Pillow 解不开的字节一律抛 ImageNormalizeError（派生自
  EngineAdapterError），底层原始异常经 ``raise ... from`` 链入，
  绝不允许裸异常穿透到 JSON-RPC 错误映射层；
* 解码后立即 ``load()`` 强制完整解码，把截断/损坏暴露在归一化入口
  而非下游编码阶段。

BMP 内存压榨要点（本模块逐一处理的典型坑）：

* **调色板（P）/灰度（L）/BI_BITFIELDS 32bpp 带 alpha**：调色板图的
  transparency 索引在 ``convert("RGBA")`` 中展开为真正的 alpha=0 像素；
  带 AlphaMask 的 BI_BITFIELDS BMP（BITMAPV4HEADER 起）由 Pillow 依据
  颜色掩码识别为 RGBA，四通道逐字节往返保真；
* **行序自下而上与 stride 对齐**：正高度 BMP 的 bottom-up 行序、每行
  按 4 字节对齐的 stride 补位，均由 Pillow 解码器就地处理，本模块不
  重复造轮子；``assume_bgra_alpha`` 手工解码路径则按
  ``(width * 4 + 3) & ~3`` 显式计算 stride 并按高度符号翻转行序；
* **无 alpha 语义的输入（RGB / L / CMYK / YCbCr / ...）**：统一展平为
  RGBA 并合成全不透明（alpha=255），保证下游拿到的字节流形态单一；
* **PNG tRNS 透明键（L / RGB 模式）**： Pillow 读取此类 PNG 时不自动
  升级 alpha 通道，本模块按透明键逐像素比对生成 alpha 掩码后合并，
  避免透明语义在展平阶段静默丢失；
* **巨图防御**：保留 Pillow DecompressionBomb 默认阈值，超限在解码期
  抛错并归一为 ImageNormalizeError，防止单张资产压穿流水线内存。

已知边界（刻意决策，非遗漏）：BI_RGB 32bpp BMP 的第 4 字节按 BMP
规范是保留位而非 alpha，Pillow 按规范将其丢弃（实测写读往返即丢）。
部分游戏引擎魔改该字节承载真 alpha，但自动启发式提取存在歧义——
编码器留下的全零填充会被误判为「整图全透明」，破坏保守正确性。
故该语义必须由调用方以 ``assume_bgra_alpha=True`` 显式声明后才激活。
"""

from io import BytesIO
from struct import unpack_from

from PIL import Image, ImageChops, UnidentifiedImageError

from .base import EngineAdapterError

__all__ = ["ImageNormalizeError", "normalize_to_rgba_png32"]

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
"""PNG 文件魔数：归一化输出的字节前缀，交付前做防御性自检。"""

_ALPHA_CARRIER_MODES = frozenset({"RGBA", "RGBa", "LA", "La", "PA"})
"""Pillow 中 alpha 独立成通道（或预乘）的模式集合，展平时必须保真。"""


class ImageNormalizeError(EngineAdapterError):
    """图像归一化失败：Pillow 无法识别、解码中断、参数非法或编码异常。

    一切底层异常（UnidentifiedImageError / OSError / ValueError /
    DecompressionBombError / struct.error / ...）都经 ``raise ... from``
    链入本类而非裸穿透，保证 JSON-RPC 错误映射层凭一句
    ``except EngineAdapterError`` 统一识别并分级上报。
    """


def _validate_background(
    background: tuple[int, int, int] | None,
) -> None:
    """校验背景合成色参数：必须为 0~255 整数构成的三元组。"""
    if background is None:
        return
    if not isinstance(background, tuple) or len(background) != 3:
        raise ImageNormalizeError(
            f"background 必须为 (r, g, b) 形式的三元组，实际为 {background!r}"
        )
    for channel in background:
        if not isinstance(channel, int) or isinstance(channel, bool) or not 0 <= channel <= 255:
            raise ImageNormalizeError(
                f"background 各通道必须为 0~255 的整数，实际为 {background!r}"
            )


def _apply_trns_key(img: Image.Image) -> Image.Image:
    """把 L/RGB 模式图像的 PNG tRNS 透明键展开为逐像素 alpha 掩码。

    Pillow 读取带 tRNS 块的真彩/灰度 PNG 时仅记录透明键于
    ``img.info["transparency"]``，不升级 alpha 通道；此处与透明键做
    逐像素绝对差比对，差为零者置 alpha=0、否则置 255。绝对差通道
    均非负，亮度归零当且仅当三通道全零，故掩码判定无歧义。
    """
    base = img.convert("RGB")
    key = img.info["transparency"]
    if isinstance(key, int):
        key = (key, key, key)
    diff = ImageChops.difference(base, Image.new("RGB", base.size, key))
    alpha = diff.convert("L").point(lambda v: 0 if v == 0 else 255)
    return Image.merge("RGBA", (*base.split(), alpha))


def _materialize_rgba(img: Image.Image) -> tuple[Image.Image, bool]:
    """把任意 Pillow 图像展平为 RGBA，返回 ``(rgba 图, 是否含 alpha 语义)``。

    alpha 语义来源有三：独立 alpha 通道（RGBA/LA/PA 等载体模式）、
    调色板 transparency 索引（P + info["transparency"]）、真彩/灰度
    tRNS 透明键（L/RGB + info["transparency"]）。三者一律保真；
    其余模式（RGB / CMYK / YCbCr / I / F / ...）合成全不透明。
    """
    mode = img.mode
    if mode == "P":
        return img.convert("RGBA"), "transparency" in img.info
    if mode in _ALPHA_CARRIER_MODES:
        return img.convert("RGBA"), True
    if mode in ("L", "RGB") and "transparency" in img.info:
        return _apply_trns_key(img), True
    try:
        return img.convert("RGBA"), False
    except ValueError:
        # 个别罕见模式无直达 RGBA 的转换矩阵，经 RGB 中转展平；
        # 中转仍失败则由外层统一归一为 ImageNormalizeError。
        return img.convert("RGB").convert("RGBA"), False


def _decode_bgra_alpha_bmp(payload: bytes) -> Image.Image:
    """按 BGRA 语义直接解码 32bpp BI_RGB BMP（仅由 assume_bgra_alpha 激活）。

    针对游戏引擎魔改 BMP：格式头声明 BI_RGB（第 4 字节为保留位），
    实际却在每像素第 4 字节塞入真 alpha。本函数绕过 Pillow 的规范
    解码，自解析文件头/信息头后以 BGRA raw decoder 重建 RGBA 图，
    按高度符号处理行序（正高度 bottom-up 翻转）并显式计算 stride。

    前置条件不满足（非 BMP、非 32bpp、非 BI_RGB、像素数据截断）即抛
    ImageNormalizeError —— 显式声明与实际格式不符属于调用方契约违约，
    必须硬性防呆阻断，绝不静默回退掩盖真相。
    """
    if len(payload) < 34 or payload[:2] != b"BM":
        raise ImageNormalizeError(
            "assume_bgra_alpha=True 但输入不是 BMP 文件（缺失 'BM' 魔数）"
        )
    header_size = unpack_from("<I", payload, 14)[0]
    if header_size < 40:
        raise ImageNormalizeError(
            f"assume_bgra_alpha=True 不支持 BITMAPCOREHEADER（header_size={header_size}）"
        )
    width, height = unpack_from("<ii", payload, 18)
    _planes, bit_count = unpack_from("<HH", payload, 26)
    compression = unpack_from("<I", payload, 30)[0]
    offset_bits = unpack_from("<I", payload, 10)[0]
    if bit_count != 32 or compression != 0:
        raise ImageNormalizeError(
            "assume_bgra_alpha=True 仅适用 32bpp BI_RGB BMP，"
            f"实际 bit_count={bit_count}, compression={compression}"
        )
    if width <= 0 or height == 0:
        raise ImageNormalizeError(f"BMP 尺寸非法：{width}x{height}")
    pixel_height = abs(height)
    stride = (width * 4 + 3) & ~3  # 行按 4 字节对齐（32bpp 下天然成立，仍显式计算）
    needed = offset_bits + stride * pixel_height
    if len(payload) < needed:
        raise ImageNormalizeError(
            f"BMP 像素数据截断：按 {width}x{height}x32bpp 需 {needed} 字节，"
            f"实际仅 {len(payload)} 字节"
        )
    pixel_data = payload[offset_bits:needed]
    plane = Image.frombytes("RGBA", (width, pixel_height), pixel_data, "raw", "BGRA")
    if height > 0:  # 正高度：bottom-up 行序，翻转为自上而下
        plane = plane.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return plane


def normalize_to_rgba_png32(
    raw_bytes: bytes,
    *,
    background: tuple[int, int, int] | None = None,
    assume_bgra_alpha: bool = False,
) -> bytes:
    """将任意 Pillow 可解码的图像原始字节归一化为 RGBA PNG-32 字节。

    全程 io.BytesIO 内存作业：不落任何磁盘临时文件、不发起任何网络
    调用（离线安全红线）。RGBA（每通道 8 bit）经 PNG 无损编码即为
    PNG-32，像素值逐字节往返保真。

    参数：
        raw_bytes: 封包内提取的图像原始字节（BMP / 私有格式等）。
        background: 可选 keyword-only 背景合成色 ``(r, g, b)``。默认
            None 表示 alpha 原样保留；显式给定时，仅当输入确实携带
            alpha 语义（含透明调色板索引、tRNS 透明键等）才按 alpha
            权重把透明区压平到该底色（输出 alpha 恒为 255），无
            alpha 语义的输入本身已不透明，不做冗余合成。
        assume_bgra_alpha: 可选 keyword-only 开关。仅当已知输入为
            32bpp BI_RGB 魔改 BMP（第 4 字节实为 alpha）时置 True，
            详见模块 docstring 的边界决策记录。

    返回：
        以 PNG 魔数 ``\\x89PNG\\r\\n\\x1a\\n`` 开头的 RGBA PNG-32 字节流。

    抛出：
        ImageNormalizeError: Pillow 无法识别/解码、BMP 魔改声明与实际
            格式不符、像素数据截断、参数非法或 PNG 编码失败；底层
            原始异常一律经 ``raise ... from`` 链入。

    注意：
        Pillow 的 DecompressionBomb 巨图防护保留默认阈值，超限同样
        归一为本异常；解码源为内存 BytesIO，不持有 OS 文件句柄，
        生命周期交由引用计数回收，不产生任何磁盘痕迹。
    """
    if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
        raise ImageNormalizeError(
            "raw_bytes 必须为 bytes 或 bytes-like 对象，"
            f"实际为 {type(raw_bytes).__name__}"
        )
    payload = bytes(raw_bytes)
    _validate_background(background)

    if assume_bgra_alpha:
        img = _decode_bgra_alpha_bmp(payload)
    else:
        try:
            img = Image.open(BytesIO(payload))
            img.load()  # 立即完整解码，把截断/损坏挡在归一化入口
        except UnidentifiedImageError as exc:
            raise ImageNormalizeError(
                f"Pillow 无法将 {len(payload)} 字节的输入识别为任何已知图像格式"
            ) from exc
        except Exception as exc:
            raise ImageNormalizeError(
                f"Pillow 打开/解码输入字节失败（{type(exc).__name__}: {exc}）"
            ) from exc

    try:
        rgba, had_alpha = _materialize_rgba(img)
        if background is not None and had_alpha:
            base = Image.new("RGBA", rgba.size, (*background, 255))
            rgba = Image.alpha_composite(base, rgba)
        out = BytesIO()
        rgba.save(out, format="PNG")
        result = out.getvalue()
    except ImageNormalizeError:
        raise
    except Exception as exc:
        raise ImageNormalizeError(
            f"归一化展平/编码阶段失败（{type(exc).__name__}: {exc}）"
        ) from exc

    if not result.startswith(_PNG_MAGIC):  # 交付前防御性自检（理论不可达）
        raise ImageNormalizeError("内部一致性断言失败：输出缺少 PNG 魔数")
    return result
