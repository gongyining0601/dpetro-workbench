# -*- coding: utf-8 -*-
"""已排除稿件面板：原来是一个独立标签页，改为嵌在「今日审核」底部的折叠区。

2026-10-08 调整（议题1）：它本质是过滤规则的安全网——系统替你拦下的稿件
写清楚「为什么被拦」，规则误杀了可以一键恢复。这个功能不能没有，
但没必要在主导航单独占一个标签位，收起来更清爽，需要时点开即可。
"""
from __future__ import annotations

import json

import streamlit as st

import db
from db_cache import _invalidate_caches, _list_excluded_cached
from image_loader import _render_image
from text_utils import _bj_time, _md_escape

def render_excluded_panel() -> None:
    # 标题由外层的 st.expander 提供，这里不再重复 subheader
    st.caption(
        "抓取时被规则拦下的稿件都记在这里，每一条都写明「为什么被拦」。"
        "规则要是误杀了，点「↩️ 恢复」它就回到「今日审核」重新走流程。"
    )
    _EX_LABEL = {
        "topic_blacklist": "🚫 题材黑名单",
        "not_photo_news": "📄 不是图片新闻",
        "not_in_5cats": "🧭 不在 5 类题材",
        "comic": "🎨 疑似漫画/插画",
        "ai_failed": "🤖 AI 判定失败",
    }
    try:
        ex_rows = _list_excluded_cached(60)
        ex_stats = db.excluded_stats()
    except Exception as e:
        st.error(f"读取已排除列表失败：{e}")
        ex_rows, ex_stats = [], {}
    if not ex_rows:
        st.info("暂无被排除的稿件。下一次抓取后，被规则拦下的稿件会出现在这里。")
    else:
        if ex_stats:
            st.caption(
                "原因分布："
                + " ｜ ".join(f"{_EX_LABEL.get(k, k)} {v} 条" for k, v in ex_stats.items())
            )
        codes = sorted({r["reason_code"] for r in ex_rows})
        pick = st.selectbox(
            "按原因筛选", ["全部"] + [_EX_LABEL.get(c, c) for c in codes],
            key="ex_filter",
        )
        if pick != "全部":
            _want = next(c for c in codes if _EX_LABEL.get(c, c) == pick)
            ex_rows = [r for r in ex_rows if r["reason_code"] == _want]
        show_img = st.checkbox("显示图片（加载会慢一些）", key="ex_show_img")
        for r in ex_rows[:60]:
            label = _EX_LABEL.get(r["reason_code"], r["reason_code"])
            with st.expander(f"{label} | {r['title']} | {r['source_name']}/{r['column_name']}"):
                st.caption(f"排除原因：{r['reason'] or '（未记录）'}")
                st.caption(f"抓取时间：{_bj_time(r.get('crawled_at'))}")
                if r.get("body_snippet"):
                    st.markdown(_md_escape(r["body_snippet"][:300]))
                if show_img and r.get("image_urls"):
                    try:
                        _us = json.loads(r["image_urls"])
                        for u in _us[:2]:
                            _render_image(u)
                    except (json.JSONDecodeError, TypeError):
                        pass
                b1, b2 = st.columns(2)
                with b1:
                    if st.button("↩️ 恢复入库", key=f"ex_restore_{r['id']}", width="stretch"):
                        try:
                            new_id = db.restore_excluded(r["id"])
                            if new_id:
                                _invalidate_caches()
                                st.toast("已恢复到「今日审核」", icon="↩️")
                            else:
                                st.warning(
                                    "恢复失败：栏目「"
                                    f"{r['source_name']}/{r['column_name']}"
                                    "」在库里找不到了（config 改过栏目名？），"
                                    "或者这篇已经入库过。"
                                )
                        except Exception as e:
                            st.error(f"恢复失败：{e}")
                        st.rerun()
                with b2:
                    if st.button("🗑 删除记录", key=f"ex_del_{r['id']}", width="stretch"):
                        st.session_state["_pending_ex_del"] = r["id"]
                        st.rerun()
            if st.session_state.get("_pending_ex_del") == r["id"]:
                st.warning(f"⚠️ 确认删除「{r['title'][:30]}…」的排除记录？删除后不再可恢复。")
                ec = st.columns([1, 1, 8])
                with ec[0]:
                    if st.button("✅ 确认删", key=f"ex_cfm_{r['id']}",
                                 type="primary", width="stretch"):
                        try:
                            db.delete_excluded(r["id"])
                            _list_excluded_cached.clear()
                            st.toast("已删除该排除记录", icon="🗑")
                        except Exception as e:
                            st.error(f"删除失败：{e}")
                        st.session_state.pop("_pending_ex_del", None)
                        st.rerun()
                with ec[1]:
                    if st.button("取消", key=f"ex_cancel_{r['id']}", width="stretch"):
                        st.session_state.pop("_pending_ex_del", None)
                        st.rerun()
