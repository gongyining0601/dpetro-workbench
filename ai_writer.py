"""AI 辅助写稿模块。

使用 Silicon Flow 的 deepseek-chat（免费模型）生成新闻稿初稿。
复用现有 TENCENTCLOUD_API_KEY，零额外成本。
"""
from __future__ import annotations

import json
import os
import re

import requests

_TC_BASE = "https://tokenhub.tencentmaas.com/v1/chat/completions"
_MODEL = "deepseek-v4-flash-202605"


def _extract_json(text: str) -> str | None:
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else None


def write_article(topic: str, angle: str = "", word_count: int = 800,
                  target_media: str = "中国石油报", facts: str = "") -> dict:
    """AI 生成新闻稿初稿。"""
    api_key = os.getenv("TENCENTCLOUD_API_KEY", "")
    if not api_key:
        return {"title": "", "body": "", "ok": False, "error": "未配置 TENCENTCLOUD_API_KEY"}

    sys_prompt = (
        "你是一位资深的中国新闻记者，擅长撰写石油石化行业新闻稿。"
        "要求：导语突出核心信息，正文逻辑清晰，语言简洁有力，"
        "数据准确有据，符合新闻写作规范。"
    )

    user_msg = f"请撰写一篇约{word_count}字的新闻稿。\n\n选题：{topic}\n目标媒体：{target_media}\n"
    if angle:
        user_msg += f"写作角度：{angle}\n"
    if facts:
        user_msg += f"已知事实/数据：{facts}\n"
    user_msg += "\n请以 JSON 格式返回，字段为 title（标题）和 body（正文）。正文用纯文本，段落间用换行分隔。"

    try:
        resp = requests.post(
            _TC_BASE,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": _MODEL,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.7,
                "max_tokens": word_count * 2,
                "thinking": {"type": "disabled"},
            },
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        raw = _extract_json(content)
        if raw:
            data = json.loads(raw)
            return {"title": data.get("title", ""), "body": data.get("body", content), "ok": True, "error": ""}
        return {"title": "", "body": content, "ok": True, "error": ""}
    except Exception as e:
        return {"title": "", "body": "", "ok": False, "error": str(e)}
