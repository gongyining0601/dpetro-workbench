"""三道闸门 gate_article 的测试（不连数据库，db 调用用替身）。

gate_article 是四个 crawl_* 共用的过滤入口，决定一篇稿子是入库还是被排除。
它一旦出错，表现就是"该进的没进 / 不该进的进来了"，所以用测试钉死。

注意：测试里把 db.save_excluded 换成替身，既避免连库（跨境连接会抖），
又能检查"被排除的稿件到底有没有登记、登记的原因对不对"。
"""
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import crawler

# 有图注的图文稿 HTML（第一层"是新闻"+第二层"是图片新闻"都能过）
_HTML = ("<html><body><figure><img src='x.jpg' alt='施工现场'>"
         "<figcaption>图为检修施工现场</figcaption></figure></body></html>")
# 正文 ≥50 字才过第一层（"是新闻吗"这条硬要求），下面这段约 66 字
_BODY = ("检修现场机器轰鸣，施工人员正在对装置管线进行防腐保温作业，"
         "逐点检查每一道焊口，确保装置一次开车成功并长期稳定运行，"
         "这是本次春季检修的关键节点。")
# 旅游正文（不含任何工业词）：用来测"真黑名单"，否则会被工业语境豁免掉
_TOUR_BODY = ("秋日的山村层林尽染，游客沿着新修的步道缓步而行，"
              "民宿小院飘出饭菜香，乡村旅游带动村民稳定增收，"
              "不少人家把老屋改成了客房，节假日一房难求。")
# 中性正文（不含任何行业关键词）：用来测"规则都没命中"的兜底路径
_NEUTRAL_BODY = ("这是一段没有任何行业关键词的普通说明文字，用来测试规则兜底路径，"
                 "长度超过五十字以便通过第一层判断，内容平淡无奇。")


def _soup():
    return BeautifulSoup(_HTML, "html.parser")


class TestGateArticle(unittest.TestCase):
    def setUp(self):
        # 用替身接住"登记已排除"的调用，逐个用例检查原因
        self.saved = []

        def _fake_save_excluded(**kw):
            self.saved.append(kw)
            return True

        patcher = patch("crawler.db.save_excluded", side_effect=_fake_save_excluded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _call(self, **kw):
        base = dict(
            source="辽宁日报", column="要闻", title="催化装置完成检修",
            url="http://example.com/1", body_text=_BODY, image_count=1,
            soup=_soup(), stats={},
        )
        base.update(kw)
        return crawler.gate_article(**base)

    # ---------- 放行 ----------
    def test_keeps_normal_photo_news(self):
        cat, pending = self._call()
        self.assertEqual(cat, "工业生产")
        self.assertFalse(pending)
        self.assertEqual(self.saved, [])   # 不该登记已排除

    def test_ai_failed_keeps_article_as_pending(self):
        """AI 故障时稿件要入库并标记待人工确认，不能丢（第四次测评 P0 遗留项）。"""
        with patch("crawler.classify_article", return_value="_AI_FAILED_"):
            cat, pending = self._call()
        self.assertTrue(pending)
        self.assertEqual(self.saved, [])

    # ---------- 排除并登记原因 ----------
    def test_blacklist_excluded_with_reason(self):
        cat, _pending = self._call(title="乡村游点亮文旅市场", body_text=_TOUR_BODY)
        self.assertIsNone(cat)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]["reason_code"], "topic_blacklist")
        self.assertIn("旅游", self.saved[0]["reason"])

    def test_not_photo_news_excluded_with_reason(self):
        """有图但正文太短 → 不是图片新闻，登记原因（人话说明）。"""
        cat, _pending = self._call(body_text="太短了", image_count=1)
        self.assertIsNone(cat)
        self.assertEqual(self.saved[0]["reason_code"], "not_photo_news")
        self.assertIn("正文", self.saved[0]["reason"])

    def test_not_in_5cats_excluded_with_reason(self):
        """栏目不在映射表、标题也没关键词、AI 也判不相关 → 排除并写明原因。"""
        with patch("crawler.ai_filter.is_relevant", return_value=(False, [])):
            cat, _pending = self._call(source="某报", column="某栏目",
                                       title="今天天气不错风和日丽",
                                       body_text=_NEUTRAL_BODY)
        self.assertIsNone(cat)
        self.assertEqual(self.saved[0]["reason_code"], "not_in_5cats")

    def test_comic_excluded_with_reason(self):
        def _fake_comic_check(urls):
            return True, "白底占比 70%、活跃颜色 12 种"

        cat, _pending = self._call(comic_check=_fake_comic_check)
        self.assertIsNone(cat)
        self.assertEqual(self.saved[0]["reason_code"], "comic")
        self.assertIn("漫画", self.saved[0]["reason"])

    def test_comic_check_not_called_for_other_sources(self):
        """漫画识别只对 config.COMIC_CHECK_SOURCES 里的来源（辽宁日报）生效。"""
        calls = []

        def _fake_comic_check(urls):
            calls.append(urls)
            return True, "像漫画"

        cat, _pending = self._call(source="中国化工报", column="科技",
                                   comic_check=_fake_comic_check)
        self.assertIsNotNone(cat)      # 中国化工报不做漫画识别，正常放行
        self.assertEqual(calls, [])

    # ---------- 纯文字稿 ----------
    def test_pure_text_article_not_recorded(self):
        """纯文字稿（一张图都没有）默认不登记，避免把已排除列表淹掉。"""
        cat, _pending = self._call(body_text="短", image_count=0)
        self.assertIsNone(cat)
        self.assertEqual(self.saved, [])   # 没登记

    # ---------- 统计计数 ----------
    def test_stats_counted(self):
        stats = {}
        self._call(stats=stats, title="乡村游点亮文旅市场", body_text=_TOUR_BODY)
        self.assertEqual(stats.get("skipped_category"), 1)

    def test_industrial_context_not_killed(self):
        """标题带旅游词但正文是工业题材 → 豁免，正常入库（真实误杀案例的回归测试）。"""
        cat, _pending = self._call(
            title="国网本溪供电公司靠前服务保障旅游景区稳定供电",
            body_text=_BODY,
        )
        self.assertIsNotNone(cat)
        self.assertEqual(self.saved, [])


if __name__ == "__main__":
    unittest.main()
