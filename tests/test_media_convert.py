"""core/media 私有图像互转管道测试（合成 TLG5 逐像素比对 + 垫片桩）。

验证策略：

* **合成编码器**按 tlg-rs encode 语义构造 TLG5 字节流（mod-256 差分
  四平面 + 块大小索引表 + mark/size 块循环），覆盖 raw 与 Slide LZSS
  两条块路径、colors 3/4、多块、SDS 前置头 —— 解码产物与原图逐像素
  比对（无损性的唯一直接证据）；
* **Slide LZSS** 用自实现贪心编码器（literal + 12bit match + 0xF 扩展
  长度）与解码器互检，钉死位序与镜像区写规则；
* **外部工具通道**用 cmd 垫片（同解包编排测试风格）验证参数组装与
  异常分型；TLG6 的「未装配工具 → 装配引导」边界单独钉死。
"""

import json
import struct
import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from core.adapters.base import UnsupportedFormatError
from core.media.errors import MediaConversionError, UnsupportedImageVariantError
from core.media.pipeline import convert_image, detect_image_format, tlg_to_png
from core.media.tlg5 import SlideLzss, decode_tlg5

SLIDE_N = 4096
SLIDE_M = 273


# ---------------------------------------------------------------------------
# Slide LZSS 编码器（贪心：literal + 12bit match + 0xF 扩展长度）
# ---------------------------------------------------------------------------


class SlideEncoderPy:
    """与 SlideLzss 对偶的贪心编码器（测试专用，O(n·N) 可接受）。"""

    def __init__(self) -> None:
        self.text = bytearray(SLIDE_N + SLIDE_M - 1)
        self.s = 0

    def __init__(self) -> None:
        self.text = bytearray(SLIDE_N + SLIDE_M - 1)
        self.s = 0
        self.group_bounds: list[int] = []  # 每个完整 flag 组在输出流中的边界

    def encode(self, data: bytes) -> bytes:
        out = bytearray()
        i = 0
        while i < len(data):
            flags = 0
            group = bytearray()
            for bit in range(8):
                if i >= len(data):
                    break
                best_len, best_pos = 0, 0
                limit = min(len(data) - i, SLIDE_M)
                if limit >= 3:
                    for cand in range(SLIDE_N):
                        length = 0
                        while (
                            length < limit
                            and self.text[(cand + length) & (SLIDE_N - 1)] == data[i + length]
                        ):
                            length += 1
                        # 关键约束（对齐 tlg-rs get_match 的 lim 截断）：match 引用
                        # 区间不得跨越写指针 self.s —— 跨越即引用「未写字典空洞」，
                        # 解码端该区域是初始 0 而非历史数据，必然损坏。
                        if cand <= self.s and cand + length > self.s:
                            length = self.s - cand
                        if length > best_len:
                            best_len, best_pos = length, cand
                if best_len >= 3:
                    # 位=1 表示 match（与 tlg-rs decode 语义一致）
                    flags |= 1 << bit
                    if best_len <= 17:
                        group += bytes(
                            [best_pos & 0xFF, (best_pos >> 8) | ((best_len - 3) << 4)]
                        )
                    else:
                        group += bytes(
                            [best_pos & 0xFF, (best_pos >> 8) | 0xF0, best_len - 18]
                        )
                    self._emit(data[i : i + best_len])
                    i += best_len
                else:
                    # 位=0 表示 literal
                    group.append(data[i])
                    self._emit(data[i : i + 1])
                    i += 1
            out += bytes([flags]) + bytes(group)
            self.group_bounds.append(len(out))
        return bytes(out)

    def _emit(self, chunk: bytes) -> None:
        for c in chunk:
            if self.s < SLIDE_M - 1:
                self.text[self.s + SLIDE_N] = c
            self.text[self.s] = c
            self.s = (self.s + 1) & (SLIDE_N - 1)


# ---------------------------------------------------------------------------
# TLG5 合成编码器（按 tlg-rs encode 语义：mod-256 差分四平面）
# ---------------------------------------------------------------------------


def _delta_planes(
    width: int, height: int, pixels: bytes, colors: int
) -> list[bytearray]:
    """RGBA/RGB/L 像素 → R-G/G/B-G(/A) mod-256 差分平面（tlg-rs encode 语义）。

    严格对齐 encode.rs：prevcl 是**未取模 int 链**（cl = cur - upper 的
    精确整差分），只有写平面时才 `as u8`（mod 256）。
    """
    planes = [bytearray(width * height) for _ in range(colors)]
    for y in range(height):
        prev_cl = [0] * colors
        for x in range(width):
            base = (y * width + x) * colors
            cur = [pixels[base + c] for c in range(colors)]
            upper = (
                [pixels[((y - 1) * width + x) * colors + c] for c in range(colors)]
                if y > 0
                else [0] * colors
            )
            cl = [cur[c] - upper[c] for c in range(colors)]  # int，不取模
            val = [(cl[c] - prev_cl[c]) & 0xFF for c in range(colors)]
            prev_cl = cl
            if colors == 1:
                planes[0][y * width + x] = val[0]
            else:
                planes[0][y * width + x] = (val[0] - val[1]) & 0xFF  # R-G
                planes[1][y * width + x] = val[1]  # G
                planes[2][y * width + x] = (val[2] - val[1]) & 0xFF  # B-G
                if colors == 4:
                    planes[3][y * width + x] = val[3]  # A
    return planes


def encode_tlg5(
    width: int,
    height: int,
    pixels: bytes,
    colors: int = 4,
    block_height: int = 4,
    compress: str = "raw",
    sds: bool = False,
) -> bytes:
    """构造 TLG5 字节流（测试专用写出器；compress = raw / slide）。"""
    out = bytearray()
    if sds:
        out += b"TLG0.0\x00sds\x1a" + b"\x00\x00\x00\x00"
    out += b"TLG5.0\x00raw\x1a\x00"
    out += bytes([colors])
    out += struct.pack("<iii", width, height, block_height)
    block_count = (height - 1) // block_height + 1
    size_table_pos = len(out)
    out += b"\x00" * (block_count * 4)  # 索引表占位

    encoder = SlideEncoderPy()
    for start in range(0, height, block_height):
        block_start = len(out)
        rows = min(block_height, height - start)
        span_start = start * width
        span_len = rows * width
        planes = _delta_planes(
            width,
            height,
            pixels,
            colors,
        )
        for c in range(colors):
            plane = bytes(planes[c][span_start : span_start + span_len])
            if compress == "slide":
                payload = encoder.encode(plane)
                out += b"\x00" + struct.pack("<i", len(payload)) + payload
            else:
                out += b"\x01" + struct.pack("<i", len(plane)) + plane
        # 回填真实块大小（tlg-rs 校验 consumed == block_size 的那张表）
        struct.pack_into("<I", out, size_table_pos + (start // block_height) * 4, len(out) - block_start)
    return bytes(out)


# ---------------------------------------------------------------------------
# Slide LZSS：位序 / match / 扩展长度
# ---------------------------------------------------------------------------


class TestSlideLzss:
    def test_literal_only_roundtrip(self):
        data = bytes((i * 37 + 11) & 0xFF for i in range(300))
        assert SlideLzss().decompress(SlideEncoderPy().encode(data)) == data

    def test_match_paths_and_extension_roundtrip(self):
        # 前缀重复段触发 match；长重复段触发 0xF 扩展长度；尾部随机段触发 literal
        import random

        rng = random.Random(20260917)
        data = b"ABCD" * 40 + b"\x00" * 60 + bytes(rng.randrange(256) for _ in range(120))
        data += b"XYZ" * 30 + bytes(rng.randrange(256) for _ in range(50))

        encoded = SlideEncoderPy().encode(data)
        assert SlideLzss().decompress(encoded) == data

    def test_decoder_state_persists_across_chunks(self):
        """LZSS 字典跨块跨通道持续：分块解码必须与整体解码一致。"""
        import random

        rng = random.Random(7)
        data = bytes(rng.randrange(256) for _ in range(400)) + b"LOOP" * 20
        encoder = SlideEncoderPy()
        encoded = encoder.encode(data)
        assert SlideLzss().decompress(encoded) == data

        # 模拟「逐块送解」（TLG5 块边界 = flag 组边界）：共享同一解压器实例，
        # 字典与写位置跨块持续 —— 严禁在组中间切分（那不是合法的流边界）。
        decoder = SlideLzss()
        bounds = encoder.group_bounds
        cut = bounds[len(bounds) // 2]
        joined = decoder.decompress(encoded[:cut]) + decoder.decompress(encoded[cut:])
        assert joined == data


# ---------------------------------------------------------------------------
# TLG5 图像解码：合成文件逐像素比对
# ---------------------------------------------------------------------------


def _pixels_rgba(width: int, height: int) -> bytes:
    import random

    rng = random.Random(4242)
    return bytes(rng.randrange(256) for _ in range(width * height * 4))


def _pixels_rgb(width: int, height: int) -> bytes:
    import random

    rng = random.Random(4242)
    return bytes(rng.randrange(256) for _ in range(width * height * 3))


class TestTlg5Decode:
    @pytest.mark.parametrize("colors,mode", [(4, "raw"), (3, "raw"), (4, "slide"), (3, "slide")])
    def test_synthetic_tlg5_roundtrip_pixel_exact(self, tmp_path, colors, mode):
        width, height = 11, 9  # 非 64 倍数：强制多块 + 尾块截断路径
        src = tmp_path / "sample.tlg"
        # 像素源与 colors 同源交错（colors=3 时喂 RGB 字节，杜绝采样错位）
        pixels = _pixels_rgba(width, height) if colors == 4 else _pixels_rgb(width, height)
        src.write_bytes(encode_tlg5(width, height, pixels, colors=colors, compress=mode))

        meta = tlg_to_png(src, tmp_path / "out.png")

        assert meta["width"] == width and meta["height"] == height
        decoded = Image.open(tmp_path / "out.png")
        layout = "RGBA" if colors == 4 else "RGB"
        assert decoded.mode == layout
        assert decoded.tobytes() == pixels, "解码产物必须与原图逐像素一致"

    def test_sds_prefixed_header_is_skipped(self, tmp_path):
        width, height = 4, 4
        pixels = _pixels_rgba(width, height)
        src = tmp_path / "sds.tlg"
        src.write_bytes(encode_tlg5(width, height, pixels, sds=True))

        meta = tlg_to_png(src, tmp_path / "out.png")

        assert meta["format"] == "TLG5"
        assert Image.open(tmp_path / "out.png").tobytes() == bytes(
            pixels[i * 4 + c]
            for i in range(width * height)
            for c in range(4)
        )

    def test_detect_format(self, tmp_path):
        assert detect_image_format(b"TLG5.0\x00raw\x1a\x00...") == "TLG5"
        assert detect_image_format(b"TLG6.0\x00raw\x1a\x00...") == "TLG6"
        assert detect_image_format(b"TLG0.0\x00sds\x1a\x00\x00\x00\x00TLG5.0\x00raw\x1a\x00") == "TLG5"
        assert detect_image_format(b"\x89PNG\r\n\x1a\n") is None


# ---------------------------------------------------------------------------
# 管道分型：TLG6 引导 / 非 TLG / 外部工具垫片
# ---------------------------------------------------------------------------


class TestPipelineTyping:
    def test_tlg6_without_tool_raises_guidance_error(self, tmp_path):
        src = tmp_path / "v6.tlg"
        src.write_bytes(b"TLG6.0\x00raw\x1a\x00" + b"\x00" * 32)
        with pytest.raises(UnsupportedImageVariantError, match="外部工具链"):
            convert_image(src, tmp_path / "out.png")

    def test_non_tlg_input_rejected_as_unsupported_format(self, tmp_path):
        src = tmp_path / "fake.tlg"
        src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        with pytest.raises(UnsupportedFormatError):
            convert_image(src, tmp_path / "out.png")

    def test_external_tool_channel_with_cmd_shim(self, tmp_path):
        """TLG6 + 垫片工具：参数模板逐位对齐，产物落盘即成功。"""
        src = tmp_path / "v6.tlg"
        src.write_bytes(b"TLG6.0\x00raw\x1a\x00" + b"\x00" * 32)
        tool_py = tmp_path / "converter.py"
        tool_py.write_text(
            "import shutil, sys\n"
            "shutil.copyfile(sys.argv[1], sys.argv[2])\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        wrapper = tmp_path / "converter.cmd"
        # 模板 `<tool> "<src>" -o "<dst>"` → 批处理参数：%1=src、%3=dst
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{tool_py}" %1 %3\r\n',
            encoding="utf-8",
        )

        meta = convert_image(src, tmp_path / "out.png", tool_path=str(wrapper))

        assert meta["format"] == "TLG6"
        assert (tmp_path / "out.png").exists()

    def test_external_tool_failure_maps_to_conversion_error(self, tmp_path):
        src = tmp_path / "v6.tlg"
        src.write_bytes(b"TLG6.0\x00raw\x1a\x00" + b"\x00" * 32)
        wrapper = tmp_path / "broken.cmd"
        wrapper.write_text("@echo off\r\nexit /b 3\r\n", encoding="utf-8")

        with pytest.raises(MediaConversionError, match="exit 3"):
            convert_image(src, tmp_path / "out.png", tool_path=str(wrapper))

    def test_external_tool_without_output_maps_to_conversion_error(self, tmp_path):
        src = tmp_path / "v6.tlg"
        src.write_bytes(b"TLG6.0\x00raw\x1a\x00" + b"\x00" * 32)
        wrapper = tmp_path / "silent.cmd"
        wrapper.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

        with pytest.raises(MediaConversionError, match="未产出"):
            convert_image(src, tmp_path / "out.png", tool_path=str(wrapper))


# ---------------------------------------------------------------------------
# convert_image RPC 帧端到端
# ---------------------------------------------------------------------------


class TestConvertImageRpc:
    def test_rpc_frame_end_to_end(self, tmp_path):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from core.server.rpc import build_default_dispatcher

        width, height = 5, 3
        pixels = _pixels_rgba(width, height)
        src = tmp_path / "rpc.tlg"
        src.write_bytes(encode_tlg5(width, height, pixels))
        dst = tmp_path / "rpc-out.png"

        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "convert_image",
                "params": {"src_path": str(src), "dst_path": str(dst)},
            }
        )
        response = json.loads(build_default_dispatcher().handle_request(frame))

        assert "error" not in response
        result = response["result"]
        assert result["format"] == "TLG5"
        assert result["width"] == width and result["height"] == height
        assert Path(result["dst_path"]).exists()
        assert Image.open(result["dst_path"]).tobytes() == bytes(
            pixels[i * 4 + c]
            for i in range(width * height)
            for c in range(4)
        )
