"""漫画/插画识别的测试（用 PIL 合成图，不联网、不连库）。

真实样本不好找，这里用程序画三类图做对照：
- 手绘漫画样：大面积白底 + 少量黑线 → 应判为漫画
- 新闻照片样：色彩丰富的连续色调 → 不应判为漫画
- 数据表格样：白底 + 网格直线 → 白底很高但不是漫画（曾误判过，必须挡住）
"""
import io
import random
import unittest

from PIL import Image, ImageDraw

import image_guard


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _comic_like() -> bytes:
    """手绘漫画样：白底 + 简单线条，没有任何贯穿整幅的长直线。"""
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    d.ellipse([120, 80, 320, 260], outline="black", width=4)
    d.line([120, 300, 300, 300], fill="black", width=4)
    d.arc([330, 90, 470, 230], start=0, end=270, fill="black", width=4)
    return _to_bytes(img)


def _photo_like() -> bytes:
    """新闻照片样：连续色调的彩色噪声，几乎无纯白。"""
    random.seed(7)
    img = Image.new("RGB", (400, 300))
    img.putdata([
        (random.randint(30, 220), random.randint(30, 220), random.randint(30, 220))
        for _ in range(400 * 300)
    ])
    return _to_bytes(img)


def _table_like() -> bytes:
    """数据表格样：白底 + 横平竖直的网格线（2026-10-07 实测误判来源）。"""
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    for y in range(40, 400, 30):
        d.line([40, y, 560, y], fill="black", width=2)
    for x in range(40, 561, 65):
        d.line([x, 40, x, 380], fill="black", width=2)
    return _to_bytes(img)


class TestLooksLikeComic(unittest.TestCase):
    def test_comic_detected(self):
        is_comic, why = image_guard.looks_like_comic(_comic_like())
        self.assertTrue(is_comic)
        self.assertIn("白底", why)

    def test_photo_not_detected(self):
        is_comic, _why = image_guard.looks_like_comic(_photo_like())
        self.assertFalse(is_comic)

    def test_table_not_detected_as_comic(self):
        """表格截图白底也很高，但网格线多，不能当漫画排除掉。"""
        is_comic, _why = image_guard.looks_like_comic(_table_like())
        self.assertFalse(is_comic)

    def test_broken_bytes_returns_false(self):
        """坏数据不能抛异常（爬虫里任何异常都会打断抓取）。"""
        is_comic, _why = image_guard.looks_like_comic(b"not-an-image")
        self.assertFalse(is_comic)

    def test_gray_photo_not_detected(self):
        """黑白新闻照片：灰度渐变，不应判为漫画。"""
        img = Image.new("RGB", (400, 300))
        img.putdata([(i % 256, i % 256, i % 256) for i in range(400 * 300)])
        is_comic, _why = image_guard.looks_like_comic(_to_bytes(img))
        self.assertFalse(is_comic)


if __name__ == "__main__":
    unittest.main()
