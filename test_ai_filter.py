"""ai_filter 模块测试。

覆盖第三次测评后新增的关键逻辑：
- 三态返回：(True, angles) 相关 / (False, []) 判定无关 / None AI 失败
- 硬规则兜底（正向石化词、负向非石化词），不依赖 LLM
- 熔断：连续失败达阈值后直接返回 None，不再调用 API
- 双 API 回退：智谱失败后回退硅基流动
- JSON 解析容错：脏输出不应抛异常
"""
import json
import unittest
from unittest.mock import MagicMock, patch

import ai_filter
import config


def _mock_post(payload=None, status=200):
    """构造 requests.Session.post 的返回值。"""
    resp = MagicMock()
    resp.status_code = status
    if payload is not None:
        resp.json.return_value = payload
    return resp


def _ai_payload(relevant: bool, angles=None):
    """构造大模型返回的 OpenAI 风格响应。"""
    content = json.dumps({"relevant": relevant, "angles": angles or []}, ensure_ascii=False)
    return {"choices": [{"message": {"content": content}}]}


class TestHardRules(unittest.TestCase):
    """硬规则兜底：命中即返回，不应触发任何网络请求。"""

    def setUp(self):
        ai_filter._reset_failures()

    def test_petro_keyword_returns_relevant(self):
        self.assertEqual(ai_filter.is_relevant("锦州石化春检圆满收官"), (True, ["石化"]))

    def test_drilling_keyword_returns_relevant(self):
        self.assertEqual(ai_filter.is_relevant("辽河油田钻井提速"), (True, ["油田"]))

    def test_negative_keyword_returns_not_relevant(self):
        self.assertEqual(ai_filter.is_relevant("某地煤矿安全生产检查"), (False, []))

    def test_negative_keyword_no_ai_call(self):
        """命中负向硬规则时不应发起网络请求。"""
        with patch.object(ai_filter._session, "post") as m:
            ai_filter.is_relevant("城市光伏发电项目并网")
            m.assert_not_called()

    def test_zhonghaiyou_maps_to_industry_tag(self):
        """中石油/中石化因含「石油」「石化」会先被前置关键词命中，
        只有「中海油」会走行业动态分支——此用例锁定该行为。"""
        self.assertEqual(ai_filter.is_relevant("中海油宣布新发现"), (True, ["行业动态"]))


class TestThreeStateReturn(unittest.TestCase):
    """三态返回：区分「判定无关」与「AI 失败」。"""

    def setUp(self):
        ai_filter._reset_failures()

    def test_ai_relevant(self):
        with patch.object(ai_filter._session, "post", return_value=_mock_post(_ai_payload(True, ["安全生产"]))):
            self.assertEqual(ai_filter.is_relevant("某装置完成检修"), (True, ["安全生产"]))

    def test_ai_not_relevant_returns_false_not_none(self):
        """关键：AI 正常工作但判定无关时返回 (False, [])，不能返回 None。"""
        with patch.object(ai_filter._session, "post", return_value=_mock_post(_ai_payload(False))):
            self.assertEqual(ai_filter.is_relevant("某地铁线路开通"), (False, []))

    def test_api_error_returns_none(self):
        """API 全部失败时返回 None（调用方据此跳过而非丢弃）。"""
        with patch.object(config, "ZHIPU_API_KEY", "k"), patch.object(config, "SF_API_KEY", "k"):
            with patch.object(ai_filter._session, "post", return_value=_mock_post(status=500)):
                self.assertIsNone(ai_filter.is_relevant("某装置检修完成"))

    def test_failure_resets_on_success(self):
        """成功后失败计数应清零。"""
        with patch.object(config, "ZHIPU_API_KEY", "k"):
            with patch.object(ai_filter._session, "post", return_value=_mock_post(status=500)):
                ai_filter.is_relevant("某装置检修完成")
            self.assertGreater(ai_filter.get_failure_status()["consecutive_failures"], 0)
            with patch.object(ai_filter._session, "post", return_value=_mock_post(_ai_payload(True))):
                ai_filter.is_relevant("某装置检修完成")
            self.assertEqual(ai_filter.get_failure_status()["consecutive_failures"], 0)


class TestCircuitBreaker(unittest.TestCase):
    """熔断：连续失败达阈值后短路，避免无效调用。"""

    def setUp(self):
        ai_filter._reset_failures()

    def test_circuit_opens_after_threshold(self):
        with patch.object(config, "ZHIPU_API_KEY", "k"), patch.object(config, "SF_API_KEY", "k"):
            with patch.object(ai_filter._session, "post", return_value=_mock_post(status=500)):
                for _ in range(ai_filter.FAIL_CIRCUIT_BREAKER):
                    ai_filter.is_relevant("某装置检修完成")
            status = ai_filter.get_failure_status()
            self.assertTrue(status["circuit_open"])
            self.assertEqual(status["consecutive_failures"], ai_filter.FAIL_CIRCUIT_BREAKER)

    def test_open_circuit_skips_api_call(self):
        """熔断打开后即使 API 恢复也不应再发起请求。"""
        with patch.object(config, "ZHIPU_API_KEY", "k"), patch.object(config, "SF_API_KEY", "k"):
            with patch.object(ai_filter._session, "post", return_value=_mock_post(status=500)):
                for _ in range(ai_filter.FAIL_CIRCUIT_BREAKER):
                    ai_filter.is_relevant("某装置检修完成")
            with patch.object(ai_filter._session, "post") as m:
                self.assertIsNone(ai_filter.is_relevant("某装置检修完成"))
                m.assert_not_called()


class TestResponseParsing(unittest.TestCase):
    """大模型输出解析的容错能力。"""

    def test_plain_json(self):
        self.assertEqual(ai_filter._parse_ai_response('{"relevant": true, "angles": ["a"]}'),
                         {"relevant": True, "angles": ["a"]})

    def test_fenced_json(self):
        self.assertEqual(ai_filter._parse_ai_response('```json\n{"relevant": false}\n```'),
                         {"relevant": False})

    def test_garbage_returns_empty(self):
        """脏输出不应抛异常，应返回空 dict。"""
        self.assertEqual(ai_filter._parse_ai_response("抱歉，我无法回答"), {})

    def test_empty_string(self):
        self.assertEqual(ai_filter._parse_ai_response(""), {})


if __name__ == "__main__":
    unittest.main()
