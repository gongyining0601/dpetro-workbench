"""题材黑名单 / 工业语境豁免 / 图片版按题判定 的测试。

对应 2026-10-07 新增的三条规则：
- 旅游/体育/娱乐/漫画/生活休闲题材命中即排除
- 命中黑名单词但标题里有明显工业/能源/安全词时豁免（不误杀）
- 人民日报「视觉」不再整版直通，改按标题关键词判定

这些规则全是纯字符串判断，不联网、不连库，跑得飞快。
"""
import unittest
from unittest.mock import patch

import crawler


class TestTopicBlacklist(unittest.TestCase):
    def test_tourism_hit(self):
        """旅游题材应被命中（这是用户反馈的漏网类型）。"""
        hit = crawler.match_topic_blacklist("乡村游点亮文旅市场")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "旅游")

    def test_sports_hit(self):
        """体育题材应被命中。"""
        hit = crawler.match_topic_blacklist("追逐梦想，在赛场淬炼提升（亚运纵横）")
        self.assertEqual(hit[0], "体育")

    def test_industrial_context_override(self):
        """真实误杀案例：标题有"旅游"但同时有"供电"→ 是电力保供稿，不排除。"""
        self.assertIsNone(
            crawler.match_topic_blacklist("国网本溪供电公司靠前服务保障旅游景区稳定供电")
        )

    def test_normal_petro_title_not_hit(self):
        """正常石化标题不应命中黑名单。"""
        self.assertIsNone(crawler.match_topic_blacklist("催化装置检修顺利完成"))

    def test_holiday_not_confused_with_baogong(self):
        """「假日」不进黑名单（假日保供是能源保供稿），「假期」才进。"""
        self.assertIsNone(crawler.match_topic_blacklist("假日保供的温暖底色"))
        self.assertIsNotNone(crawler.match_topic_blacklist("活力中国  和美假期"))

    def test_blacklist_returns_marker(self):
        """命中黑名单时 classify_article 返回特殊标记，交给闸门登记原因。"""
        self.assertEqual(
            crawler.classify_article("人民日报", "视觉", "海滨度假游客如织"),
            "_BLACKLIST_",
        )


class TestPhotoColumnJudgement(unittest.TestCase):
    """人民日报「视觉」版：不再整版直通，改按标题判定。"""

    def test_visual_not_passthrough(self):
        """标题无工业/科技等关键词 → 不再直通入库（交给 AI 兜底，这里 mock 成不相关）。"""
        with patch("crawler.ai_filter.is_relevant", return_value=(False, [])):
            self.assertIsNone(crawler.classify_article("人民日报", "视觉", "我的乡村，我的家园"))

    def test_visual_kept_by_industry_keyword(self):
        """视觉版里有工业词的照片稿要保留。"""
        self.assertEqual(
            crawler.classify_article("人民日报", "视觉", "钻井现场机器轰鸣"),
            "工业生产",
        )

    def test_visual_kept_by_baogong_keyword(self):
        """「假日保供」是能源保供题材，应保留（实测稿件，曾是误杀风险点）。"""
        self.assertEqual(
            crawler.classify_article("人民日报", "视觉", "假日保供的温暖底色"),
            "工业生产",
        )

    def test_other_column_still_passthrough(self):
        """其他栏目（如人民日报/要闻）仍走栏目直通，不受影响。"""
        self.assertEqual(
            crawler.classify_article("人民日报", "要闻", "随便一个标题"),
            "工业生产",
        )


if __name__ == "__main__":
    unittest.main()
