# -*- coding: utf-8 -*-
"""文本小工具：北京时间换算 / Markdown 转义 / 安全链接。

2026-10-08 从 app.py 拆出。这三个都是无副作用的纯函数，而且几乎每个标签页都要用。
不先抽出来的话，各个 Tab 模块就得反过来依赖主文件，形成循环依赖——
所以它是 Tab 拆分的前置模块。

- _bj_time      库里存的是 UTC，直接显示会让使用者误判成"没保存"，统一转北京时间
- _md_escape    第三方内容（标题/URL）进 markdown 前必须转义，防注入、防排版错乱
- _safe_anchor  只放行 http/https，并对失效链接给出人话提示而非裸露 URL
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from image_loader import _normalize_display_url


def _bj_time(iso_str: str | None) -> str:
    """把库里存的 UTC 时间转成北京时间显示（使用者在中国，看 UTC 会误判成"没保存"）。"""
    if not iso_str:
        return ""
    try:
        return (datetime.fromisoformat(iso_str) + timedelta(hours=8)).strftime("%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        return str(iso_str)[:19].replace("T", " ")


def _md_escape(s: str) -> str:
    """转义 markdown 特殊字符，防止第三方内容（标题/URL）注入。"""
    if not s:
        return ""
    return re.sub(r"([\\*_{}\[\]()#+.\!|>])", r"\\\1", str(s))


def _safe_anchor(label: str, url: str) -> str:
    """生成安全的 markdown 链接文本，仅允许 http/https 协议。

    中国石油报是 SPA 数字报，单篇无独立 URL，锚点定位不生效，
    正文已在库中，故直接返回提示文本，不展示无效链接。
    """
    safe_url = _normalize_display_url((url or "").strip())
    if not safe_url.startswith(("http://", "https://")):
        return f"{_md_escape(label)}：{_md_escape(safe_url)}"
    if "epaper.cnpc.com.cn" in safe_url:
        return "中国石油报数字报（无单篇链接，正文见下方）"
    return f"[{_md_escape(label)}]({safe_url})"
