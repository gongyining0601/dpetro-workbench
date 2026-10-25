"""AI 视觉题材分类 vision_filter 的测试（不连库、不调 API，网络/DB 用替身）。

覆盖四件事：
1. 来源启停判断（VISION_SOURCES 空=全部启用）
2. JSON 解析（括号配对栈取首个完整对象）
3. 保守策略（全部判拒绝才排除；任一保留/拿不准即保留；未配 Key 放行）
4. gate_article 第四道视觉闸门：命中拒绝 → 登记已排除(reason_code=vision_reject) + 计数
"""
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import crawler
import vision_filter

_HTML = ("<html><body><figure><img src='x.jpg' alt='现场'>"
         "<figcaption>图为检修施工现场</figcaption></figure></body></html>")
_BODY = ("检修现场机器轰鸣，施工人员正在对装置管线进行防腐保温作业，"
         "逐点检查每一道焊口，确保装置一次开车成功并长期稳定运行，"
         "这是本次春季检修的关键节点。")
_URLS = '["http://img.example.com/a.jpg","http://img.example.com/b.jpg"]'


def _soup():
    return BeautifulSoup(_HTML, "html.parser")


class TestIsEnabledFor(unittest.TestCase):
    def test_disabled_when_flag_off(self):
        with patch.object(vision_filter.config, "VISION_FILTER_ENABLED", False):
            self.assertFalse(vision_filter.is_enabled_for("人民日报"))

    def test_empty_sources_means_all(self):
        with patch.object(vision_filter.config, "VISION_FILTER_ENABLED", True):
            with patch.object(vision_filter.config, "VISION_SOURCES", set()):
                self.assertTrue(vision_filter.is_enabled_for("人民日报"))
                self.assertTrue(vision_filter.is_enabled_for("辽宁日报"))

    def test_source_in_whitelist(self):
        with patch.object(vision_filter.config, "VISION_SOURCES", {"辽宁日报"}):
            self.assertTrue(vision_filter.is_enabled_for("辽宁日报"))
            self.assertFalse(vision_filter.is_enabled_for("人民日报"))


class TestExtractJson(unittest.TestCase):
    def test_extracts_first_object(self):
        self.assertEqual(vision_filter._extract_json('{"label":"安全生产演练"}'),
                         '{"label":"安全生产演练"}')

    def test_extracts_from_noisy_text(self):
        out = vision_filter._extract_json('前文{"label":"漫画插画"}后文')
        self.assertEqual(out, '{"label":"漫画插画"}')

    def test_returns_none_when_no_object(self):
        self.assertIsNone(vision_filter._extract_json("没有括号"))


class TestClassifyArticleImages(unittest.TestCase):
    """保守策略：全部拒绝才排除；任一保留/拿不准/无Key 都放行。"""

    def _call(self, **kw):
        base = dict(image_urls=_URLS)
        base.update(kw)
        return vision_filter._classify_article_images(base["image_urls"])

    def test_all_reject_excluded(self):
        with patch.object(vision_filter.config, "ZHIPU_API_KEY", "test-key"):
            with patch.object(vision_filter.config, "VISION_MAX_IMAGES_PER_ARTICLE", 2):
                with patch("vision_filter._classify_one", side_effect=["reject", "reject"]):
                    ok, why = self._call()
        self.assertTrue(ok)
        self.assertIn("无关题材", why)

    def test_any_accept_keeps(self):
        with patch.object(vision_filter.config, "ZHIPU_API_KEY", "test-key"):
            with patch("vision_filter._classify_one", side_effect=["accept", "reject"]):
                ok, _ = self._call()
        self.assertFalse(ok)

    def test_uncertain_image_keeps(self):
        """API 失败/拿不准 → 保守放行，绝不误杀。"""
        with patch.object(vision_filter.config, "ZHIPU_API_KEY", "test-key"):
            with patch("vision_filter._classify_one", side_effect=[None, "reject"]):
                ok, _ = self._call()
        self.assertFalse(ok)

    def test_no_key_keeps(self):
        with patch.object(vision_filter.config, "ZHIPU_API_KEY", ""):
            ok, _ = self._call()
        self.assertFalse(ok)

    def test_no_urls_keeps(self):
        with patch.object(vision_filter.config, "ZHIPU_API_KEY", "test-key"):
            ok, _ = self._call(image_urls="[]")
        self.assertFalse(ok)


class TestGateVisionGate(unittest.TestCase):
    """gate_article 第四道视觉闸门接线。"""

    def setUp(self):
        self.saved = []

        def _fake_save_excluded(**kw):
            self.saved.append(kw)
            return True

        patcher = patch("crawler.db.save_excluded", side_effect=_fake_save_excluded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, vision_check=None, stats=None):
        if stats is None:
            stats = {}
        return crawler.gate_article(
            source="人民日报", column="视觉", title="催化装置完成检修",
            url="http://example.com/1", body_text=_BODY, image_count=1,
            soup=_soup(), image_urls=_URLS, stats=stats, vision_check=vision_check,
        )

    def test_vision_reject_excluded_and_counted(self):
        stats = {}
        cat, _pending = self._call(vision_check=lambda u: (True, "领导会见"), stats=stats)
        self.assertIsNone(cat)
        self.assertEqual(self.saved[0]["reason_code"], "vision_reject")
        self.assertIn("领导会见", self.saved[0]["reason"])
        self.assertEqual(stats.get("skipped_vision"), 1)

    def test_vision_accept_keeps(self):
        cat, _pending = self._call(vision_check=lambda u: (False, ""))
        self.assertIsNotNone(cat)
        self.assertEqual(self.saved, [])

    def test_no_vision_check_skips(self):
        """不传 vision_check（来源未启用）→ 不做视觉判定，正常放行。"""
        cat, _pending = self._call()
        self.assertIsNotNone(cat)
        self.assertEqual(self.saved, [])


class TestLabelToVerdict(unittest.TestCase):
    """模型返回的类别文本 → accept/reject 的"含词"映射（模型会整组返回，精确匹配太脆）。"""

    def test_full_allowed_list_is_accept(self):
        # 实测：模型对工业图返回整组保留类 → 应判 accept
        full = "石油化工生产现场/设备装置/科技创新实验/安全生产演练/工程施工/能源保供"
        self.assertEqual(vision_filter._label_to_verdict(full), "accept")

    def test_full_reject_list_is_reject(self):
        # 实测同形态：领导会见图整组拒绝类 → 应判 reject
        full = "领导会见/考察调研合影/会议座谈特写/漫画插画/旅游风景/体育娱乐/美食生活/其他非工业场景"
        self.assertEqual(vision_filter._label_to_verdict(full), "reject")

    def test_single_reject_term_rejects(self):
        self.assertEqual(vision_filter._label_to_verdict("领导会见"), "reject")
        self.assertEqual(vision_filter._label_to_verdict("旅游风景"), "reject")

    def test_single_allow_term_accepts(self):
        self.assertEqual(vision_filter._label_to_verdict("安全生产演练"), "accept")

    def test_ambiguous_fails_open_to_accept(self):
        # 拿不准 → 保守放行，绝不误杀
        self.assertEqual(vision_filter._label_to_verdict("无法判断"), "accept")


class TestMakeVisionCheck(unittest.TestCase):
    def test_returns_callable_with_tuple(self):
        fn = vision_filter.make_vision_check()
        self.assertTrue(callable(fn))
        with patch("vision_filter._classify_article_images",
                   return_value=(False, "")):
            self.assertEqual(fn(_URLS), (False, ""))


if __name__ == "__main__":
    unittest.main()
