# -*- coding: utf-8 -*-
"""标签页：历史已审 —— 翻看过去审核通过的稿件，并可剔除。

2026-10-08 从 app.py 拆出。
"""
from __future__ import annotations

import json

import streamlit as st

import db
from db_cache import _fetch_reviewed_cached, _invalidate_caches
from image_loader import _preload_images, _render_image
from text_utils import _md_escape, _safe_anchor

def render_history() -> None:
    st.subheader("已审稿件")
    rows = _fetch_reviewed_cached(limit=200)
    if not rows:
        st.info("还没有审核记录。去「今日审核」审几篇试试。")
    else:
        # 并发预加载所有稿件图片
        all_img_urls = []
        for r in rows:
            raw = r.get("image_urls")
            if raw:
                try:
                    urls = json.loads(raw) if isinstance(raw, str) else raw
                    all_img_urls.extend(urls[:5])
                except (json.JSONDecodeError, TypeError):
                    pass
        _preload_images(all_img_urls)

        for r in rows:
            tag = {"保存": "📁 保存"}.get(r["decision"], r["decision"])
            with st.expander(f"{tag} | {r['title']} | {r['source_name']}/{r['column_name']}"):
                if r.get("publish_date"):
                    st.caption(f"发布日期：{r['publish_date']}  |  审核时间：{r['reviewed_at']}")
                st.markdown(_safe_anchor("原文链接", r['url']))
                # 展示图片（image_urls 是 JSON 字符串数组）
                img_urls_raw = r.get("image_urls")
                if img_urls_raw:
                    try:
                        img_urls = json.loads(img_urls_raw) if isinstance(img_urls_raw, str) else img_urls_raw
                        if img_urls:
                            for u in img_urls[:5]:
                                _render_image(u)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if r.get("body_text"):
                    st.markdown(_md_escape(r["body_text"]))
                else:
                    st.info("（无正文内容）")
                # 删除按钮（从已审库中移除）
                if st.button("🗑 删除此稿件", key=f"hist_del_{r['id']}", width="stretch"):
                    st.session_state["_pending_hist_del"] = r["id"]
                    st.rerun()
            # 历史删除二次确认
            if st.session_state.get("_pending_hist_del") == r["id"]:
                st.warning(f"⚠️ 确认从资料库删除「{r['title'][:30]}…」？删除不可恢复。")
                hconf = st.columns([1, 1, 8])
                with hconf[0]:
                    if st.button("✅ 确认删", key=f"hist_cfm_{r['id']}",
                                 type="primary", width="stretch"):
                        try:
                            db.set_review(r["id"], "删除")
                            _invalidate_caches()
                            st.toast("已从资料库删除", icon="🗑")
                        except Exception as e:
                            st.error(f"删除失败：{e}")
                        st.session_state.pop("_pending_hist_del", None)
                        st.rerun()
                with hconf[1]:
                    if st.button("取消", key=f"hist_cancel_{r['id']}", width="stretch"):
                        st.session_state.pop("_pending_hist_del", None)
                        st.rerun()
