"""图片归档模块的测试：压缩、版面裁图、下载失败兜底。

对应 2026-10-07 的中国石油报图片归档改造：
- 原图下载 → 压缩（长边 1200 / JPEG q80）→ 存库
- 原图被源站下架时 → 用版面整版图 + 坐标裁出文章区域兜底

测试不联网：下载函数用替身，图片用 PIL 合成，存库用替身。
"""
import io
import unittest
from unittest.mock import patch

from PIL import Image

import image_archive


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestShouldArchive(unittest.TestCase):
    def test_whitelist_source(self):
        self.assertTrue(image_archive.should_archive("中国石油报"))

    def test_other_source_unchanged(self):
        """其他来源保持原样（直接读源站），不额外占空间。"""
        self.assertFalse(image_archive.should_archive("辽宁日报"))
        self.assertFalse(image_archive.should_archive("人民日报"))


class TestCompress(unittest.TestCase):
    def test_resize_to_max_edge(self):
        raw = _png_bytes(Image.new("RGB", (2400, 1600), "red"))
        data, mime, w, h = image_archive.compress(raw)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(max(w, h), 1200)
        self.assertTrue(data[:2] == b"\xff\xd8")   # JPEG 魔数

    def test_small_image_not_upscaled(self):
        raw = _png_bytes(Image.new("RGB", (300, 200), "blue"))
        _data, _mime, w, h = image_archive.compress(raw)
        self.assertEqual((w, h), (300, 200))

    def test_transparent_png_becomes_white_bg(self):
        """透明 PNG 转 JPEG 不能变黑块，要铺白底。"""
        img = Image.new("RGBA", (200, 120), (255, 0, 0, 0))
        _data, _mime, w, h = image_archive.compress(_png_bytes(img))
        out = Image.open(io.BytesIO(_data)).convert("RGB")
        self.assertEqual(out.getpixel((10, 10)), (255, 255, 255))

    def test_compressed_much_smaller(self):
        """压缩要真的压小：1600x1200 的图，原始像素数据是 5.7MB，压完应远小于它。"""
        noisy = Image.new("RGB", (1600, 1200))
        noisy.putdata([
            ((i * 7) % 256, (i * 13) % 256, (i * 29) % 256)
            for i in range(1600 * 1200)
        ])
        raw_pixels = 1600 * 1200 * 3
        data, _m, _w, _h = image_archive.compress(_png_bytes(noisy))
        self.assertLess(len(data), raw_pixels // 4)   # 至少压到原始像素数据的 1/4


class TestCropBoardRegion(unittest.TestCase):
    """中国石油报兜底：从版面整版图按百分比坐标裁出文章区域。"""

    def setUp(self):
        image_archive.clear_board_cache()
        # 400x200 的整版图：左半红、右半蓝
        board = Image.new("RGB", (400, 200), "white")
        board.paste(Image.new("RGB", (200, 200), "red"), (0, 0))
        board.paste(Image.new("RGB", (200, 200), "blue"), (200, 0))
        self.board_bytes = _png_bytes(board)

    def test_crop_left_half(self):
        with patch.object(image_archive, "_download", return_value=self.board_bytes):
            out = image_archive.crop_board_region("http://x/board.jpg", "0,0,50,0,50,100,0,100")
        img = Image.open(io.BytesIO(out)).convert("RGB")
        # JPEG 是有损的，红色会变成 254 左右，不能拿等值比，按色相比
        r, g, b = img.getpixel((img.width // 2, img.height // 2))
        self.assertGreater(r, 200)
        self.assertLess(g, 60)
        self.assertLess(b, 60)

    def test_crop_polygon_more_than_4_points(self):
        """不规则版式的文章坐标有 9 个顶点（18 个值），取外接矩形。"""
        with patch.object(image_archive, "_download", return_value=self.board_bytes):
            out = image_archive.crop_board_region(
                "http://x/board.jpg",
                "55,10,55,20,55,40,55,60,10,60,10,80,10,90,55,90,55,95",
            )
        img = Image.open(io.BytesIO(out))
        self.assertGreater(img.width, 60)
        self.assertGreater(img.height, 40)

    def test_bad_coord_raises(self):
        with patch.object(image_archive, "_download", return_value=self.board_bytes):
            with self.assertRaises(RuntimeError):
                image_archive.crop_board_region("http://x/board.jpg", "1,2,3")

    def test_board_image_cached(self):
        """同一期报纸的整版图只下一次（2.6MB，别重复下）。"""
        with patch.object(image_archive, "_download", return_value=self.board_bytes) as m:
            image_archive.crop_board_region("http://x/board.jpg", "0,0,50,0,50,100,0,100")
            image_archive.crop_board_region("http://x/board.jpg", "50,0,100,0,100,100,50,100")
        self.assertEqual(m.call_count, 1)


class TestArchiveOne(unittest.TestCase):
    def setUp(self):
        image_archive.clear_board_cache()
        self.saved = []

        def _fake_save(**kw):
            self.saved.append(kw)
            return True

        self.p_save = patch.object(image_archive.db, "save_image_asset", side_effect=_fake_save)
        self.p_save.start()
        self.addCleanup(self.p_save.stop)

    def test_normal_archive(self):
        raw = _png_bytes(Image.new("RGB", (800, 600), "green"))
        with patch.object(image_archive, "_download", return_value=raw), \
             patch.object(image_archive.db, "get_image_asset", return_value=None):
            ok = image_archive.archive_one("http://x/a.jpg", "中国石油报")
        self.assertTrue(ok)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["mime"], "image/jpeg")

    def test_fallback_crop_when_original_gone(self):
        """原图被源站下架（下载失败）时，用兜底函数拿图——这是中国石油报的关键路径。"""
        board = Image.new("RGB", (400, 200), "white")
        board.paste(Image.new("RGB", (200, 200), "red"), (0, 0))

        def _boom(_url, timeout=20):
            raise RuntimeError("HTTP 404")

        with patch.object(image_archive, "_download", side_effect=_boom), \
             patch.object(image_archive.db, "get_image_asset", return_value=None):
            ok = image_archive.archive_one(
                "http://x/gone.jpg", "中国石油报",
                fallback_fn=lambda: image_archive.crop_board_region("http://x/b.jpg", "0,0,50,0,50,100,0,100"),
            )
        # 兜底函数内部也会 _download（同样被 mock 成失败），所以这里应失败但不抛异常
        self.assertFalse(ok)

    def test_skip_if_already_archived(self):
        """已归档过的图不重复下载、不重复占空间。"""
        with patch.object(image_archive.db, "get_image_asset", return_value={"data": b"x"}), \
             patch.object(image_archive, "_download") as m:
            ok = image_archive.archive_one("http://x/a.jpg", "中国石油报")
        self.assertFalse(ok)
        m.assert_not_called()

    def test_archive_failure_never_raises(self):
        """归档失败绝不能打断抓取主流程。"""
        with patch.object(image_archive, "_download", side_effect=RuntimeError("网络断了")), \
             patch.object(image_archive.db, "get_image_asset", return_value=None):
            ok = image_archive.archive_one("http://x/a.jpg", "中国石油报")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
