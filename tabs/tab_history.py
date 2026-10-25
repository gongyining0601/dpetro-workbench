# -*- coding: utf-8 -*-
"""标签页：历史已审 —— 翻看过去审核通过的稿件，并可剔除。

2026-10-08 从 app.py 拆出。
2026-10-25 性能瘦身：limit 200→50；收起的 expander 不再预渲染/传输全部图片
（改为"查看图片"按钮按需加载该条）；新增复选框批量删除。
慢的根因：原来 200 条 × 最多 5 图，即使 expander 收起，Streamlit 仍把每条图的
base64 拼进页面并跨境传输，一次 rerun 传几十 MB。改成按需加载后只传用户点开的那条。
"""
from __future__ import annotations

import json

import streamlit as st

import db
from db_cache import _fetch_reviewed_cached, _invalidate_caches
from image_loader import _render_image
from text_utils import _md_escape, _safe_anchor

LIMIT = 50


def _parse_urls(raw):
    """从 article.image_urls（JSON 字符串或 list）解析出最多 5 个图 URL。"""
    if not raw:
        return []
    try:
        u = json.loads(raw) if isinstance(raw, str) else raw
        return u[:5] if isinstance(u, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _clear_hist_checks():
    """批量删后清掉残留的复选框状态（否则 rerun 后旧勾选还在）。"""
    for k in list(st.session_state.keys()):
        if k.startswith("hist_chk_"):
            del st.session_state[k]


def render_history() -> None:
    st.subheader("已审稿件")
    rows = _fetch_reviewed_cached(limit=LIMIT)
    if not rows:
        st.info("还没有审核记录。去「今日审核」审几篇试试。")
        return

    # ---- 批量删除二次确认（渲染列表前显示在顶部）----
    pending = st.session_state.get("_pending_hist_bulk_del")
    if pending:
        st.warning(f"⚠️ 确认从资料库删除选中的 {len(pending)} 条稿件？删除不可恢复。")
        bc = st.columns([1, 1, 8])
        with bc[0]:
            if st.button("✅ 确认删", key="hist_bulk_cfm", type="primary", width="stretch"):
                n = 0
                for rid in pending:
                    try:
                        db.set_review(rid, "删除")
                        n += 1
                    except Exception:
                        pass
                _invalidate_caches()
                _clear_hist_checks()
                st.session_state.pop("_pending_hist_bulk_del", None)
                st.toast(f"已删除 {n} 条", icon="🗑")
                st.rerun()
        with bc[1]:
            if st.button("取消", key="hist_bulk_cancel", width="stretch"):
                st.session_state.pop("_pending_hist_bulk_del", None)
                st.rerun()
        st.divider()

    # ---- 逐条渲染：复选框 + expander（图按需加载，不预渲染）----
    selected = []
    for r in rows:
        rid = r["id"]
        cks, cexp = st.columns([0.5, 11.5])
        with cks:
            if st.checkbox("选", key=f"hist_chk_{rid}", label_visibility="collapsed"):
                selected.append(rid)
        with cexp:
            tag = {"保存": "📁 保存"}.get(r["decision"], r["decision"])
            with st.expander(f"{tag} | {r['title']} | {r['source_name']}/{r['column_name']}"):
                if r.get("publish_date"):
                    st.caption(f"发布日期：{r['publish_date']}  |  审核时间：{r['reviewed_at']}")
                st.markdown(_safe_anchor("原文链接", r['url']))
                # 图片：默认不渲染（收起时也不传输 base64），点按钮才加载该条
                imgs = _parse_urls(r.get("image_urls"))
                if imgs:
                    show_key = f"hist_showimg_{rid}"
                    if not st.session_state.get(show_key):
                        if st.button(f"🖼 查看图片（{len(imgs)}）", key=f"hist_showbtn_{rid}"):
                            st.session_state[show_key] = True
                            st.rerun()
                    else:
                        for u in imgs:
                            _render_image(u)
                if r.get("body_text"):
                    st.markdown(_md_escape(r["body_text"]))
                else:
                    st.info("（无正文内容）")
                # 单条删除
                if st.button("🗑 删除此稿件", key=f"hist_del_{rid}", width="stretch"):
                    st.session_state["_pending_hist_del"] = rid
                    st.rerun()
        # 单条删除二次确认（expander 外，占满整行）
        if st.session_state.get("_pending_hist_del") == rid:
            st.warning(f"⚠️ 确认从资料库删除「{r['title'][:30]}…」？删除不可恢复。")
            hc = st.columns([1, 1, 8])
            with hc[0]:
                if st.button("✅ 确认删", key=f"hist_cfm_{rid}", type="primary", width="stretch"):
                    try:
                        db.set_review(rid, "删除")
                        _invalidate_caches()
                        st.toast("已从资料库删除", icon="🗑")
                    except Exception as e:
                        st.error(f"删除失败：{e}")
                    st.session_state.pop("_pending_hist_del", None)
                    st.rerun()
            with hc[1]:
                if st.button("取消", key=f"hist_cancel_{rid}", width="stretch"):
                    st.session_state.pop("_pending_hist_del", None)
                    st.rerun()

    # ---- 底部批量删除按钮（选中 ≥1 才出现）----
    if selected:
        st.divider()
        if st.button(f"🗑 批量删除选中（{len(selected)}）", type="primary", width="stretch"):
            st.session_state["_pending_hist_bulk_del"] = selected
            st.rerun()
