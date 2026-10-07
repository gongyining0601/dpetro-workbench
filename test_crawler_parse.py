"""crawler 解析层测试（纯函数，不依赖网络）。

覆盖爬虫最核心且此前零测试的解析逻辑：
- content_hash：去重根基，哈希不一致会导致重复入库
- _normalize_url：SSL 证书不匹配域名的降级规则
- _lookback_days：回溯窗口，决定断爬后能补回多久的稿件
- extract_links：列表页链接提取（「候选链接为 0」的直接成因）
- _is_photo_news 第一层：新闻性过滤
"""
import unittest
from datetime import date, timedelta

from bs4 import BeautifulSoup

import crawler


class TestContentHash(unittest.TestCase):
    """去重哈希：决定同一篇稿子是否会被重复入库。"""

    def test_same_text_same_hash(self):
        self.assertEqual(crawler.content_hash("锦州石化检修"), crawler.content_hash("锦州石化检修"))

    def test_different_text_different_hash(self):
        self.assertNotEqual(crawler.content_hash("稿件A"), crawler.content_hash("稿件B"))

    def test_empty_returns_none(self):
        self.assertIsNone(crawler.content_hash(""))
        self.assertIsNone(crawler.content_hash(None))

    def test_hash_is_hex_sha1(self):
        h = crawler.content_hash("测试")
        self.assertEqual(len(h), 40)
        int(h, 16)


class TestNormalizeUrl(unittest.TestCase):
    """URL 规范化：SSL 证书不匹配域名降级为 http。"""

    def test_ssl_broken_domain_downgraded(self):
        url = "https://www.ccin.com.cn/news/123"
        self.assertEqual(crawler._normalize_url(url), "http://www.ccin.com.cn/news/123")

    def test_normal_https_unchanged(self):
        url = "https://example.com/news/123"
        self.assertEqual(crawler._normalize_url(url), url)

    def test_plain_http_unchanged(self):
        self.assertEqual(crawler._normalize_url("http://a.com/x"), "http://a.com/x")

    def test_empty_safe(self):
        self.assertEqual(crawler._normalize_url(""), "")
        self.assertIsNone(crawler._normalize_url(None))


class TestLookbackDays(unittest.TestCase):
    """回溯窗口：AI 或网络故障后能否补回稿件，取决于此。"""

    def test_default_window_is_seven_days(self):
        days = crawler._lookback_days()
        self.assertEqual(len(days), 7)

    def test_includes_today_and_is_descending(self):
        days = crawler._lookback_days(5)
        self.assertEqual(days[0], date.today())
        self.assertEqual(days[-1], date.today() - timedelta(days=4))
        self.assertEqual(days, sorted(days, reverse=True))

    def test_custom_window(self):
        self.assertEqual(len(crawler._lookback_days(14)), 14)


class TestExtractLinks(unittest.TestCase):
    """列表页解析：候选链接为 0 通常源于此处规则不匹配。"""

    def _html(self, links):
        return "".join(f'<a href="{u}">{t}</a>' for t, u in links)

    def test_extracts_article_links(self):
        html = self._html([("锦州石化完成年度检修", "/content/2026-10/01.html")])
        links = crawler.extract_links(html, "http://paper.example.com/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "锦州石化完成年度检修")
        self.assertEqual(links[0].url, "http://paper.example.com/content/2026-10/01.html")

    def test_relative_url_is_joined(self):
        html = self._html([("油田产量创新高", "news/888.html")])
        links = crawler.extract_links(html, "http://paper.example.com/index.html")
        self.assertTrue(links[0].url.startswith("http://paper.example.com/"))

    def test_filters_navigation_links(self):
        """不含文章特征的 URL 应被过滤（列表页/栏目首页）。"""
        html = self._html([("首页导航", "/index.html"), ("栏目入口", "/section/")])
        self.assertEqual(len(crawler.extract_links(html, "http://paper.example.com/")), 0)

    def test_filters_short_titles(self):
        """标题少于 4 字视为无效链接。"""
        html = self._html([("短题", "/content/1.html")])
        self.assertEqual(len(crawler.extract_links(html, "http://paper.example.com/")), 0)

    def test_deduplicates(self):
        html = self._html([("同一篇稿件", "/content/1.html"), ("同一篇稿件", "/content/1.html")])
        self.assertEqual(len(crawler.extract_links(html, "http://paper.example.com/")), 1)

    def test_digital_id_pattern_accepted(self):
        """长数字 ID 也视为文章链接。"""
        html = self._html([("炼化装置技改收效", "/202610/t20261001_123456.html")])
        self.assertEqual(len(crawler.extract_links(html, "http://paper.example.com/")), 1)


class TestIsPhotoNewsFirstLayer(unittest.TestCase):
    """图片新闻判定第一层：先确认「是一条新闻」。"""

    def _soup(self):
        return BeautifulSoup("<p>正文</p>", "html.parser")

    def _body(self, n=100):
        return "锦州石化检修现场作业全面展开。" * n

    def test_rejects_too_short_title(self):
        self.assertFalse(crawler._is_photo_news(self._soup(), self._body(), 1, title="检修"))

    def test_rejects_too_long_title(self):
        self.assertFalse(crawler._is_photo_news(self._soup(), self._body(), 1, title="题" * 61))

    def test_rejects_non_news_keyword(self):
        self.assertFalse(crawler._is_photo_news(self._soup(), self._body(), 1, title="关于设备检修的公告"))

    def test_rejects_short_body(self):
        self.assertFalse(crawler._is_photo_news(self._soup(), "太短", 1, title="锦州石化完成检修"))

    def test_accepts_valid_photo_news(self):
        """有图 + 有图注 + 合规标题正文 → 判定为图片新闻。"""
        html = '<figure><img src="a.jpg" alt="检修现场作业"><figcaption>检修现场作业</figcaption></figure>'
        soup = BeautifulSoup(html, "html.parser")
        self.assertTrue(
            crawler._is_photo_news(soup, self._body(30), 1, title="锦州石化完成年度检修")
        )


if __name__ == "__main__":
    unittest.main()
