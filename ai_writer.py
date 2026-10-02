"""AI 辅助写稿模块。

默认使用智谱 GLM 免费模型（GLM-4.7-Flash）生成新闻稿初稿；
若智谱调用失败或未配置 Key，自动回退到腾讯云 deepseek（原方案）。
"""
from __future__ import annotations

import json
import os
import re

import requests

import config

# 智谱 OpenAI 兼容地址
_ZHIPU_BASE = config.ZHIPU_CHAT_URL
_ZHIPU_MODEL = config.ZHIPU_CHAT_MODEL

# 腾讯云后备（原方案）
_TC_BASE = "https://tokenhub.tencentmaas.com/v1/chat/completions"
_TC_MODEL = "deepseek-v4-flash-202605"


def _extract_json(text: str) -> str | None:
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
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _sys_prompt() -> str:
    return (
        "你是一位资深的中国新闻记者，擅长撰写石油石化行业新闻稿。"
        "要求：导语突出核心信息，正文逻辑清晰，语言简洁有力，"
        "数据准确有据，符合新闻写作规范。"
    )


def write_article(topic: str, angle: str = "", word_count: int = 800,
                  target_media: str = "中国石油报", facts: str = "") -> dict:
    """AI 生成新闻稿初稿：默认智谱 GLM，失败回退腾讯 deepseek。"""
    user_msg = f"请撰写一篇约{word_count}字的新闻稿。\n\n选题：{topic}\n目标媒体：{target_media}\n"
    if angle:
        user_msg += f"写作角度：{angle}\n"
    if facts:
        user_msg += f"已知事实/数据：{facts}\n"
    user_msg += "\n请以 JSON 格式返回，字段为 title（标题）和 body（正文）。正文用纯文本，段落间用换行分隔。"

    def _call(base: str, api_key: str, model: str) -> dict | None:
        try:
            resp = requests.post(
                base,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _sys_prompt()},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.7,
                    "max_tokens": min(word_count * 2, 8192),
                    "thinking": {"type": "disabled"},
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _parse(data: dict) -> dict:
        content = data["choices"][0]["message"]["content"]
        raw = _extract_json(content)
        if raw:
            parsed = json.loads(raw)
            return {"title": parsed.get("title", ""), "body": parsed.get("body", content), "ok": True, "error": ""}
        return {"title": "", "body": content, "ok": True, "error": ""}

    # 1) 主力：智谱 GLM（免费）
    zp_key = os.getenv("ZHIPU_API_KEY", "")
    if zp_key:
        data = _call(_ZHIPU_BASE, zp_key, _ZHIPU_MODEL)
        if data:
            try:
                return _parse(data)
            except Exception:
                pass

    # 2) 后备：腾讯云 deepseek（原方案）
    config.logger.info("ai_writer: 智谱调用失败或未配 Key，回退到腾讯云 deepseek")
    tc_key = os.getenv("TENCENTCLOUD_API_KEY", "")
    if not tc_key:
        return {"title": "", "body": "", "ok": False, "error": "智谱与腾讯云 Key 均未配置"}
    data = _call(_TC_BASE, tc_key, _TC_MODEL)
    if not data:
        return {"title": "", "body": "", "ok": False, "error": "写稿请求失败"}
    try:
        return _parse(data)
    except Exception as e:
        return {"title": "", "body": "", "ok": False, "error": str(e)}
