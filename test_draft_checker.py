"""draft_checker.check 规则命中断言测试。"""
import unittest

import draft_checker


class TestDraftChecker(unittest.TestCase):
    def test_fuzzy_time_detected(self):
        """模糊时间应被检出。"""
        result = draft_checker.check("标题", "近日公司举行了活动")
        issues = result["issues"]
        self.assertTrue(any("模糊时间" in i for i in issues), f"issues: {issues}")

    def test_vague_data_detected(self):
        """空泛数据应被检出。"""
        result = draft_checker.check("标题", "产量大幅提升")
        issues = result["issues"]
        self.assertTrue(any("空泛数据" in i for i in issues), f"issues: {issues}")

    def test_absolute_wording_detected(self):
        """绝对化用词应被检出。"""
        result = draft_checker.check("标题", "该技术达到了国内领先水平")
        issues = result["issues"]
        self.assertTrue(any("绝对化用词" in i for i in issues), f"issues: {issues}")

    def test_clean_draft_no_issues(self):
        """规范稿件应返回'未发现明显问题'。"""
        result = draft_checker.check(
            "公司产量提升",
            "1月1日，锦州石化公司催化装置产量同比增长百分之十五，装置运行平稳。"
        )
        self.assertEqual(result["issues"], ["未发现明显问题"])

    def test_weak_caption_detected(self):
        """空泛图片说明应被检出。"""
        result = draft_checker.check("标题", "1月1日，公司举行开工仪式。", caption="图为现场")
        issues = result["issues"]
        self.assertTrue(any("图片说明" in i for i in issues), f"issues: {issues}")

    def test_return_structure(self):
        """返回结构应包含 issues 和 versions。"""
        result = draft_checker.check("标题", "正文")
        self.assertIn("issues", result)
        self.assertIn("versions", result)
        self.assertIn("辽报版", result["versions"])
        self.assertIn("中石油版", result["versions"])
        self.assertIn("企业内网版", result["versions"])


if __name__ == "__main__":
    unittest.main()
