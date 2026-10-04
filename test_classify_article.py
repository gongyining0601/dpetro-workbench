"""classify_article 分类逻辑测试：正/负样本断言。"""
import unittest
from unittest.mock import patch

import crawler


class TestClassifyArticle(unittest.TestCase):
    def test_category_map_exact_match(self):
        """CATEGORY_MAP 中存在的「源/栏目」应直接返回对应类别。"""
        # 中国石油报/炼化新材料 → 炼油化工新材料
        result = crawler.classify_article("中国石油报", "炼化新材料", "任意标题")
        self.assertEqual(result, "炼油化工新材料")

    def test_keyword_fallback_refinery(self):
        """标题含炼油关键词应归入炼油化工新材料。"""
        result = crawler.classify_article("某报", "某栏目", "催化装置改造顺利完成")
        self.assertEqual(result, "炼油化工新材料")

    def test_keyword_fallback_safety(self):
        """标题含安全关键词应归入安全生产。"""
        result = crawler.classify_article("某报", "某栏目", "开展安全隐患排查治理")
        self.assertEqual(result, "安全生产")

    def test_keyword_fallback_tech(self):
        """标题含科技关键词应归入科技创新。"""
        result = crawler.classify_article("某报", "某栏目", "技术攻关取得突破")
        self.assertEqual(result, "科技创新")

    def test_keyword_fallback_ai(self):
        """标题含 AI 关键词应归入人工智能。"""
        result = crawler.classify_article("某报", "某栏目", "人工智能大模型应用")
        self.assertEqual(result, "人工智能")

    @patch("crawler.ai_filter.is_relevant", return_value=(False, []))
    def test_unrelated_article_returns_none(self, _mock_ai):
        """无关稿件在 AI 也判定不相关时返回 None。"""
        result = crawler.classify_article("某报", "某栏目", "今天天气真好适合出游")
        self.assertIsNone(result)

    @patch("crawler.ai_filter.is_relevant", return_value=(True, ["行业动态"]))
    def test_ai_fallback_returns_general(self, _mock_ai):
        """规则未命中但 AI 判定相关时归入'综合'。"""
        result = crawler.classify_article("某报", "某栏目", "能源行业发展趋势分析")
        self.assertEqual(result, "综合")

    def test_body_text_keyword_match(self):
        """标题无关键词但正文有关键词时应匹配。"""
        result = crawler.classify_article("某报", "某栏目", "无关键词标题", body_text="炼油厂检修工作有序推进")
        self.assertEqual(result, "炼油化工新材料")


if __name__ == "__main__":
    unittest.main()
