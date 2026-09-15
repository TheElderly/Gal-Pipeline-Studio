"""core/adapters/image.py 契约回归测试（内存转码冒烟固化）。

覆盖范围：

1. 合法 BMP 内存转 RGBA PNG-32：RGB 真彩 / 灰度 L / 1x1 边界尺寸，
   像素值往返保真、输出恒为 PNG 魔数 + RGBA 模式；
2. 非法/损坏字节流：空字节、残缺魔数、类型违约一律抛
   ImageNormalizeError，且异常挂接 EngineAdapterError 分类学、
   底层异常经 raise ... from 链入。
"""

import sys
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

# 项目尚未打包（无 pyproject.toml），此处把仓库根注入 sys.path，
# 保证 `pytest` 与 `python -m pytest` 两种唤起方式的导入行为一致。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.adapters.base import EngineAdapterError
from core.adapters.image import ImageNormalizeError, normalize_to_rgba_png32

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestValidBmpNormalize:
    """合法 BMP → RGBA PNG-32：全程内存、像素逐点保真。"""

    @pytest.mark.parametrize(
        ("mode", "size", "samples"),
        [
            ("RGB", (3, 3), {(0, 0): (200, 30, 40), (2, 2): (10, 20, 30)}),
            ("L", (2, 2), {(0, 0): 0, (1, 0): 77, (0, 1): 200, (1, 1): 255}),
            ("RGB", (1, 1), {(0, 0): (0, 0, 0)}),
        ],
        ids=["RGB真彩BMP", "灰度BMP", "1x1边界尺寸"],
    )
    def test_valid_bmp_roundtrip(self, mode, size, samples):
        img = Image.new(mode, size, next(iter(samples.values())))
        for xy, value in samples.items():
            img.putpixel(xy, value)
        buf = BytesIO()
        img.save(buf, format="BMP")

        out = normalize_to_rgba_png32(buf.getvalue())

        assert out.startswith(PNG_MAGIC)
        revived = Image.open(BytesIO(out))
        assert revived.format == "PNG"
        assert revived.mode == "RGBA"
        assert revived.size == size
        for xy, value in samples.items():
            expected = (value, value, value, 255) if mode == "L" else (*value, 255)
            assert revived.getpixel(xy) == expected


class TestInvalidInputRejected:
    """非法/损坏字节流：构造期入口一律防呆拦截。"""

    @pytest.mark.parametrize(
        "evil",
        [b"", b"BM", b"\x89PNG\r\n\x1a\nbroken-tail", "字符串不是字节"],
        ids=["空字节", "仅BM魔数", "PNG魔数残骸", "类型违约"],
    )
    def test_invalid_input_raises(self, evil):
        with pytest.raises(ImageNormalizeError):
            normalize_to_rgba_png32(evil)

    def test_error_taxonomy_and_cause_chain(self):
        """异常分类学挂接 + 底层异常链入，不允裸异常穿透。"""
        with pytest.raises(ImageNormalizeError) as excinfo:
            normalize_to_rgba_png32(b"\x00garbage")
        assert isinstance(excinfo.value, EngineAdapterError)
        assert excinfo.value.__cause__ is not None
