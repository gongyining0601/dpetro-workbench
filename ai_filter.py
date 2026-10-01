"""AI 初选：爬虫入库前用 LLM 判断文章是否与石油石化行业相关。

调 Silicon Flow chat API（复用 embedding 的 key），用 Qwen2.5-7B-Instruct
免费模型判断相关性 + 打行业标签。失败/超时/未启用时降级放行（relevant=True,
tags=[]），避免爬虫因 AI 调用失败而卡死——人工审核兜底。
"""
from __future__ import annotations

import json
import re

import requests

import config


_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"Authorization": f"Bearer {config.SF_API_KEY}"})
    return _session


SYSTEM_PROMPT = (
    "你是石油石化行业的内容审核助手。判断给你的一篇新闻是否与石油石化行业"
    "相关（包括但不限于：上游勘探开发、炼油化工、油气储运、成品油销售、"
    "天然气、石化新材料、设备检修、安全生产、环保治理、党建+生产、"
    "国企改革、地方能源产业等）。锦州石化及其关联企业的任何报道都算相关。"
    "严格 JSON 输出，不要任何解释。"
)

USER_TEMPLATE = (
    "判断下面这篇新闻是否与石油石化行业相关，并打 0-3 个行业标签。\n"
    "标题：{title}\n摘要：{summary}\n\n"
    "输出 JSON：{{\"relevant\": true/false, \"tags\": [\"标签1\", ...]}}\n"
    "标签候选：勘探、炼油、化工、新材料、油气储运、成品油销售、天然气、"
    "设备检修、安全生产、环保、党建、改革、地方能源、锦州石化。"
)


def is_relevant(title: str, summary: str | None, *, body: str | None = None,
                timeout: int = 20) -> tuple[bool, list[str]]:
    """判断文章是否与石油石化行业相关。返回 (relevant, tags)。

    失败/超时/未启用时降级返回 (True, [])——放行入库，由人工审核兜底。
    """
    if not config.AI_FILTER_ENABLED:
        return True, []
    if not config.SF_API_KEY:
        print("  [AI 初选] 未配置 SILICONFLOW_API_KEY，放行")
        return True, []

    text = (summary or "").strip() or (body or "")[:200]
    if not title and not text:
        return True, []

    payload = {
        "model": config.SF_CHAT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(
                title=title or "(无标题)", summary=text or "(无摘要)")},
        ],
        "temperature": 0.1,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }

    try:
        r = _get_session().post(config.SF_CHAT_URL, json=payload, timeout=timeout)
        if r.status_code != 200:
            print(f"  [AI 初选] HTTP {r.status_code}，放行：{r.text[:120]}")
            return True, []
        data = r.json()
        content = data["choices"][0]["message"]["content"]
        relevant, tags = _parse_json(content)
        tag_str = f" tags={tags}" if tags else ""
        print(f"  [AI 初选] relevant={relevant}{tag_str} | {title}")
        return relevant, tags
    except Exception as e:
        print(f"  [AI 初选] 异常放行：{e}")
        return True, []


def _parse_json(content: str) -> tuple[bool, list[str]]:
    """解析 LLM 输出。容错：取第一个 {...} 块。"""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        return True, []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return True, []
    relevant = bool(obj.get("relevant", True))
    tags = obj.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t).strip() for t in tags if str(t).strip()][:3]
    return relevant, tags


if __name__ == "__main__":
    # 自检：跑几个样例验证 API 通不通、判断准不准
    samples = [
        ("锦州石化春检攻坚 装置一次开车成功",
         "催化裂化装置检修圆满收官，VOCs治理改造完成"),
        ("市民公园荷花池盛开吸引游客",
         "市区公园荷花进入盛花期，周末游客增多"),
        ("国家发改委部署冬季天然气保供",
         "发改委召开会议部署冬季天然气保供工作"),
    ]
    for t, s in samples:
        rel, tags = is_relevant(t, s)
        print(f"  -> relevant={rel} tags={tags} | {t}")
