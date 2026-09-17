"""TLG5 无损图像解码器（纯 Python，零外部依赖）。

实现依据（双源交叉验证，规格细节见包级 docstring）：

* Kirikiri 官方 C++ 解码器：krkrz-mingw/krglhtlg ``LoadTLG.cpp``
  （头部 colors/width/height/blockheight、块大小索引表跳过、
  mark/size 块循环、B/G/R/A 平面、b+=g 与 r+=g 预测、行内累加）；
* ``tlg-rs`` crate 的 slide/decode.rs 与 tlg5/decode.rs
  （Slide LZSS 位序 / len 扩展 / 镜像区写规则的权威实现，
  以及 mod-256 差分平面的逐像素还原语义）。

与 krkr 读取行为一致的两条宽容纪律：

* 块大小索引表**跳过不校验**（krkr SetPosition 直跳——部分第三方
  写出器的索引值有误，校验会把可读文件拒之门外）；
* 解压平面长度按「实际需要」下限校验，尾部长度不较真。
"""

from __future__ import annotations

import struct

from .errors import MediaConversionError, UnsupportedImageVariantError

TLG5_RAW_MAGIC = b"TLG5.0\x00raw\x1a\x00"
TLG6_RAW_MAGIC = b"TLG6.0\x00raw\x1a\x00"
SDS_MAGIC = b"TLG0.0\x00sds\x1a"
SDS_HEADER_SIZE = 15

_SLIDE_N = 4096
_SLIDE_M = 273  # 18 + 255：扩展 match 的最大长度（镜像区 272 字节）


class SlideLzss:
    """TLG5 的 modified LZSS（Slide）解压器。

    * flag 字节 LSB-first：**1 = match（2 字节）**、0 = literal（1 字节）；
    * match：``pos = ((b2 & 0x0F) << 8) | b1``（12bit）、
      ``len = (b2 >> 4) & 0x0F``；len==0xF 时扩展为 ``18 + 下一字节``，
      否则 ``len += 3``；
    * 字典缓冲 4096 环形 + 272 字节镜像（写 literal/match 字节时若写位
      落在镜像范围内需同步双写——扩展 match 会从镜像区读历史字节）；
    * 状态（字典与写位置）跨块跨通道持续，由调用方持有同一实例。
    """

    __slots__ = ("_text", "_s")

    def __init__(self) -> None:
        self._text = bytearray(_SLIDE_N + _SLIDE_M - 1)
        self._s = 0

    def decompress(self, data: bytes | bytearray) -> bytes:
        """按 flag 组展开解压：每组 1 字节 flags + 至多 8 个条目。

        flags 的 bit0..bit7 依次对应条目（LSB first）；1 = literal、
        0 = match。数据耗尽即终止（尾部不足 8 条的组按实际处理）。
        """
        out = bytearray()
        i, n = 0, len(data)
        text, s = self._text, self._s
        while i < n:
            flags = data[i]
            i += 1
            for bit in range(8):
                if i >= n:
                    break
                if flags & (1 << bit):
                    # match（位=1）：pos 12bit + len nibble（0xF 扩展为 18+1 字节）
                    if i + 2 > n:
                        i = n
                        break
                    b1, b2 = data[i], data[i + 1]
                    i += 2
                    pos = ((b2 & 0x0F) << 8) | b1
                    length = (b2 >> 4) & 0x0F
                    if length == 0x0F:
                        if i >= n:
                            i = n
                            break
                        length = 18 + data[i]
                        i += 1
                    else:
                        length += 3
                    for _ in range(length):
                        c = text[pos]
                        out.append(c)
                        if s < _SLIDE_M - 1:
                            text[s + _SLIDE_N] = c
                        text[s] = c
                        s = (s + 1) & (_SLIDE_N - 1)
                        pos = (pos + 1) & (_SLIDE_N - 1)
                else:
                    # literal（位=0）：单字节直出并写入字典
                    c = data[i]
                    i += 1
                    out.append(c)
                    if s < _SLIDE_M - 1:
                        text[s + _SLIDE_N] = c
                    text[s] = c
                    s = (s + 1) & (_SLIDE_N - 1)
        self._s = s
        return bytes(out)


def _read_i32(data: bytes | bytearray, pos: int) -> tuple[int, int]:
    if pos + 4 > len(data):
        raise MediaConversionError("TLG5 数据在头部字段处被截断")
    return struct.unpack_from("<i", data, pos)[0], pos + 4


def decode_tlg5(data: bytes | bytearray) -> tuple[int, int, str, bytes]:
    """解码 TLG5 字节流，返回 ``(width, height, layout, pixels)``。

    ``layout`` 为 ``"RGB"`` / ``"RGBA"`` / ``"L"``（Pillow frombytes 直用），
    ``pixels`` 为按 layout 交错排列的原始像素字节。
    """
    if data.startswith(SDS_MAGIC):
        data = data[SDS_HEADER_SIZE:]
    if not data.startswith(TLG5_RAW_MAGIC):
        raise UnsupportedImageVariantError("输入不是 TLG5 图像数据")

    pos = len(TLG5_RAW_MAGIC)
    colors = data[pos]
    pos += 1
    if colors not in (1, 3, 4):
        raise MediaConversionError(f"TLG5 不支持的通道数：{colors}")

    width, pos = _read_i32(data, pos)
    height, pos = _read_i32(data, pos)
    block_height, pos = _read_i32(data, pos)
    if width <= 0 or height <= 0:
        raise MediaConversionError(f"TLG5 尺寸非法：{width}x{height}")
    if block_height <= 0:
        raise MediaConversionError(f"TLG5 blockheight 非法：{block_height}")

    block_count = (height - 1) // block_height + 1
    pos += block_count * 4  # 块大小索引表：与 krkr 一致，跳过不校验

    layout = {1: "L", 3: "RGB", 4: "RGBA"}[colors]
    stride = width * colors
    output = bytearray(stride * height)
    slide = SlideLzss()

    plane_need_full = width * block_height
    for y_blk in range(0, height, block_height):
        rows_in_block = min(block_height, height - y_blk)
        plane_need = width * rows_in_block
        planes: list[bytes] = []
        for _ in range(colors):
            if pos + 5 > len(data):
                raise MediaConversionError("TLG5 数据在块标记处被截断")
            mark = data[pos]
            size = struct.unpack_from("<i", data, pos + 1)[0]
            pos += 5
            if size < 0 or pos + size > len(data):
                raise MediaConversionError("TLG5 数据在块载荷处被截断")
            payload = bytes(data[pos : pos + size])
            pos += size
            if mark == 0:
                plane = slide.decompress(payload)
            else:
                plane = payload
            if len(plane) < plane_need:
                raise MediaConversionError(
                    f"TLG5 平面解压不足：需要 {plane_need} 字节，实得 {len(plane)}"
                )
            planes.append(plane[:plane_need] if plane_need < plane_need_full else plane)

        # 逐像素还原：plane（mod-256 差分）→ 行内差分链 → 加上一行
        for row_in_block in range(rows_in_block):
            y = y_blk + row_in_block
            prev_cl = [0] * colors
            out_base = y * stride
            upper_base = (y - 1) * stride if y > 0 else -1
            for x in range(width):
                p_index = row_in_block * width + x
                if colors == 1:
                    values = (planes[0][p_index],)
                else:
                    g = planes[1][p_index]
                    # 平面语义：[0]=R-G、[1]=G、[2]=B-G、[3]=A（mod-256 差分）
                    r = (planes[0][p_index] + g) & 0xFF
                    b = (planes[2][p_index] + g) & 0xFF
                    values = (r, g, b, planes[3][p_index]) if colors == 4 else (r, g, b)
                for c in range(colors):
                    cl = (prev_cl[c] + values[c]) & 0xFF
                    prev_cl[c] = cl
                    upper = output[upper_base + x * colors + c] if y > 0 else 0
                    output[out_base + x * colors + c] = (upper + cl) & 0xFF

    return width, height, layout, bytes(output)
