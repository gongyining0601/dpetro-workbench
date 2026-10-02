"""AI 稿件过滤：调用 Silicon Flow 兼容接口判断稿件是否与锦州石化相关。

设计要点：
- 只做"是否相关"的二分类 + 可选的角度标签，给"相关"稿打 1~3 个角度标签。
- 失败安全：单次失败标记为"相关"（避免漏稿），由人工 5 分钟审核兜底。
- 连续失败熔断：连续 FAIL_CIRCUIT_BREAKER 次失败后切换为保守拒绝（relevant=False），
  防止 API 欠费/故障时无关稿大量涌入。成功一次自动重置计数。
- 严格 JSON 解析：用括号配对栈提取首个完整 JSON 对象，避免贪婪匹配误判。
"""
from __future__ import annotations

import json
import os
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config


def _make_session() -> requests.Session:
    """带重试的 Session：LLM API 网络抖动时自动重试。"""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=["POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_session = _make_session()

# 连续失败熔断阈值：超过此次数后返回 relevant=False
FAIL_CIRCUIT_BREAKER = 5

# 模块级连续失败计数（crawler 单进程内有效）
_consecutive_failures = 0

SYSTEM_PROMPT = (
    "你是锦州石化（中国石油锦州石化分公司，位于辽宁锦州）的宣传编辑。"
    "给定一篇媒体稿件的标题与正文，判断它是否可作为锦州石化新闻宣传的参考选题。"
    "只输出 JSON：{"
    '\"relevant\": true/false, '
    '\"angles\": [\"角度1\", \"角度2\"]'
    "}。"
    "【判定原则：严格把关，非石油石化行业一律 relevant=false。"
    "拿不准时一律 relevant=false，宁可错杀，不可放过。】"
    "relevant=true 仅当（满足任一）："
    "1. 明确属于石油石化产业链：油气勘探开发、采油钻井、炼油化工（乙烯/催化/加氢/"
    "重整/常减压/焦化/烷基化/芳烃）、天然气/LNG/储气库、油气管道、油气销售、"
    "石化装备制造、石化科研院所；"
    "2. 直接提到中国石油/中石化/中海油/中石油/锦州石化/辽河油田/大庆油田等"
    "石油石化系统内单位；"
    "3. 能源化工主题且主体是石化企业：CCUS、碳达峰碳中和、氢能、光伏风电新能源"
    "（仅当主体是石化企业时）。"
    "relevant=false 的情形（常见）：煤炭、电力、冶金、建材、农业种养殖、文学副刊、"
    "公安政法、娱乐体育、社会民生（非能源）、教育医疗、消费财经（非工业）、"
    "建筑施工、交通运输（非油气管道）、信息技术。"
    "注意：即使提到\"安全生产/春检/秋检/隐患排查/党建/班组/提质增效\"等通用词，"
    "只要主体不是石油石化企业，一律 relevant=false。"
    "angles 最多 3 个简短中文标签（如：勘探、炼化、天然气、CCUS、保供、设备、安全、"
    "人物、新材料、党建），irrelevant 时为空数组。不要输出 JSON 以外的任何文字。"
)


def _extract_first_json(text: str) -> str | None:
    """用括号配对栈提取首个完整 JSON 对象，避免贪婪正则误匹配。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '\"':
                in_string = False
            continue
        if ch == '\"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_ai_response(content: str) -> dict:
    """解析 Silicon Flow 返回的 JSON 文本，失败返回 {}。"""
    raw = _extract_first_json(content)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _decide_on_failure() -> tuple[bool, list[str]]:
    """API 失败时一律拒绝：非石化稿不采纳，宁可漏不可放。
    API 恢复后爬虫会自动补爬，不会永久漏稿。"""
    global _consecutive_failures
    _consecutive_failures += 1
    return (False, [])


def get_failure_status() -> dict:
    """供 UI/日志查询当前熔断状态。"""
    return {
        "consecutive_failures": _consecutive_failures,
        "circuit_open": _consecutive_failures >= FAIL_CIRCUIT_BREAKER,
        "threshold": FAIL_CIRCUIT_BREAKER,
    }


def _reset_failures():
    """API 调用成功时重置计数。"""
    global _consecutive_failures
    _consecutive_failures = 0


def is_relevant(title: str, summary: str = "", body_text: str = "") -> tuple[bool, list[str]]:
    """调用 Silicon Flow 过滤。返回 (是否相关, 角度标签列表)。

    失败时根据连续失败次数决定放行或拒绝（见 _decide_on_failure）。
    """
    api_key = os.getenv("SILICONFLOW_API_KEY", "")

    # 硬规则兜底：标题/正文命中石化行业关键词直接判相关，不依赖 LLM
    _PETRO_KEYWORDS = (
        # 石油石化核心产业链词（明确指向石化行业）
        "石油", "石化", "炼化", "炼油", "油田", "油井", "钻井", "井场", "采油",
        "乙烯", "催化", "加氢", "重整", "常减压", "焦化", "烷基化", "芳烃",
        "天然气", "煤层气", "LNG", "储气库", "保供", "调峰",
        # 石油石化系统内单位
        "中国石油", "中石化", "中海油", "中石油", "昆仑", "长庆", "塔里木",
        "大庆", "胜利", "辽河", "锦州石化", "锦州石油",
        # 能源化工（仅石化相关）
        "CCUS", "碳中和", "碳达峰", "氢能", "新材料", "高端化工", "精细化工",
    )
    _check_text = f"{title} {summary} {body_text}"
    for kw in _PETRO_KEYWORDS:
        if kw in _check_text:
            # 命中硬规则：返回相关，标签取命中的关键词
            return (True, [kw] if kw not in ("中国石油", "中石化", "中海油", "中石油") else ["行业动态"])

    if not api_key:
        return _decide_on_failure()

    body_excerpt = (body_text or "")[:2000]
    user_prompt = f"标题：{title}\n摘要：{summary or '无'}\n正文：{body_excerpt}"

    try:
        resp = _session.post(
            config.SF_CHAT_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.SF_CHAT_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 300,
            },
            timeout=30,
        )
    except requests.RequestException:
        return _decide_on_failure()

    if resp.status_code != 200:
        return _decide_on_failure()

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return _decide_on_failure()

    parsed = _parse_ai_response(content)
    relevant = bool(parsed.get("relevant", False))
    angles = parsed.get("angles") or []
    if not isinstance(angles, list):
        angles = []
    angles = [str(a)[:20] for a in angles if a][:3]

    # 成功解析，重置失败计数
    _reset_failures()
    return (relevant, angles)


if __name__ == "__main__":
    print(is_relevant("锦州石化春检圆满收官"))
