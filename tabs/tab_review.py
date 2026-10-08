# -*- coding: utf-8 -*-
"""标签页：今日审核 —— 批量勾选 + 一键保存/删除的核心工作流。

2026-10-08 从 app.py 拆出（192 行）。这是整个应用最常用的页面，
连同它专用的两个辅助函数（_do_review_batch / _clear_selection）一起搬过来。
"""
from __future__ import annotations

import json
import time as _time

import streamlit as st

import crawler
import db
from db_cache import (
    _fetch_unreviewed_cached,
    _invalidate_caches,
    _invalidate_setting_cache,
)
from image_loader import _preload_images, _render_image
from text_utils import _bj_time, _md_escape, _safe_anchor

def _do_review_batch(article_ids, decision):
    """批量审核：连接断开时自动重建池并重试一次。返回 (是否成功, 影响行数)。"""
    import psycopg2 as _pg
    try:
        n = db.set_review_batch(article_ids, decision)
        return True, n
    except (_pg.OperationalError, _pg.InterfaceError):
        db._reset_pool()
        try:
            n = db.set_review_batch(article_ids, decision)
            return True, n
        except Exception:
            return False, 0
    except Exception:
        return False, 0


def _clear_selection(ids):
    """清空选中状态（批量操作后调用）。"""
    for aid in ids:
        st.session_state.pop(f"chk_{aid}", None)
    # 不能直接设 chk_all=False（widget 已实例化会报错），
    # 用 pop 移除该 key，rerun 后 checkbox 会以默认值 False 重建。
    st.session_state.pop("chk_all", None)

def render_review() -> None:
    st.subheader("今日新稿（5 分钟审核法）")
    st.caption("勾选多条 → 顶部「批量保存/删除」一次处理；保存=入资料库，删除=直接清理。")
    rows = _fetch_unreviewed_cached(limit=30)
    if not rows:
        st.info("暂无待审稿件。点击下方按钮立即爬取，或确认 config.py 已填栏目 URL。")
        if st.button("🔄 立即爬取今日稿件", type="primary"):
            with st.spinner("正在爬取各媒体稿件（约 1-3 分钟）..."):
                try:
                    stats = crawler.crawl_all()
                    db.set_setting("last_crawl_date", _time.strftime("%Y-%m-%d"))
                    _invalidate_setting_cache()
                    st.toast(
                        f"完成：新增 {stats['added']} 条，跳过 {stats['skipped']} 条",
                        icon="📰",
                    )
                except Exception as e:
                    st.error(f"爬取失败：{e}")
            _invalidate_caches()
            st.rerun()
    else:
        all_ids = [r["id"] for r in rows]

        # 全选 on_change 回调：同步所有 chk_{id} session_state
        def _toggle_all(*_):
            v = st.session_state.get("chk_all", False)
            for aid in all_ids:
                st.session_state[f"chk_{aid}"] = v

        # 顶部工具栏：全选 + 批量按钮
        sel_cols = st.columns([1, 3])
        with sel_cols[0]:
            st.checkbox("全选", key="chk_all", on_change=_toggle_all)
        # 实时统计选中数（从 session_state 读，全选 on_change 已同步过）
        selected_ids = [aid for aid in all_ids if st.session_state.get(f"chk_{aid}", False)]
        n_sel = len(selected_ids)
        with sel_cols[1]:
            st.caption(f"已选 {n_sel} / {len(all_ids)} 条")
        # 批量按钮单独一行，确保小屏也能完整显示
        btn_cols = st.columns([1, 1, 4])
        with btn_cols[0]:
            if st.button(f"💾 批量保存({n_sel})", key="btn_batch_save",
                         type="primary", disabled=(n_sel == 0), width="stretch"):
                ok, n = _do_review_batch(selected_ids, "保存")
                if ok:
                    st.session_state["_force_vec_sync"] = True
                    st.toast(f"已批量保存 {n} 条", icon="📁")
                    _clear_selection(selected_ids)
                    _invalidate_caches()
                else:
                    st.error("连接失败，请重试")
                st.rerun()
        with btn_cols[1]:
            if st.button(f"🗑 批量删除({n_sel})", key="btn_batch_del",
                         disabled=(n_sel == 0), width="stretch"):
                st.session_state["_pending_del_ids"] = list(selected_ids)
                st.rerun()

        # 删除二次确认
        if st.session_state.get("_pending_del_ids"):
            pending = st.session_state["_pending_del_ids"]
            st.warning(f"⚠️ 确认删除 {len(pending)} 条？删除不可恢复。")
            conf_cols = st.columns([1, 1, 4])
            with conf_cols[0]:
                if st.button("✅ 确认删除", key="btn_confirm_del", type="primary", width="stretch"):
                    ok, n = _do_review_batch(pending, "删除")
                    if ok:
                        st.toast(f"已批量删除 {n} 条", icon="🗑")
                        _clear_selection(pending)
                        _invalidate_caches()
                    else:
                        st.error("连接失败，请重试")
                    st.session_state.pop("_pending_del_ids", None)
                    st.rerun()
            with conf_cols[1]:
                if st.button("取消", key="btn_cancel_del", width="stretch"):
                    st.session_state.pop("_pending_del_ids", None)
                    st.rerun()

        # 并发预加载所有稿件图片（避免串行下载拖慢首屏）
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

        # 渲染每条稿件
        for r in rows:
            with st.container(border=True):
                cols = st.columns([0.4, 9.6])
                with cols[0]:
                    st.checkbox("选", key=f"chk_{r['id']}")
                with cols[1]:
                    st.markdown(f"**{_md_escape(r['title'])}**")
                    st.caption(
                        f"{r['source_name']} · {r['column_name']} · "
                        f"{r['publish_date'] or '日期不详'} · 作者: {r['author'] or '不详'}"
                    )
                    if r.get("ai_pending"):
                        st.warning(
                            "⚠️ 待确认：AI 判定时接口故障，未归入 5 类题材，"
                            "请人工判断后再保存或删除。",
                            icon="⚠️",
                        )
                    with st.expander("摘要 / 详情"):
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
                        st.write(r["summary"] or "（无摘要）")
                        if r["body_text"]:
                            st.text_area("正文预览（前 500 字）",
                                         r["body_text"][:500], height=160,
                                         disabled=True, key=f"body_{r['id']}")
                        st.markdown(_safe_anchor("原文链接", r['url']))
                        # 单条操作按钮
                        act_cols = st.columns([1, 1, 4])
                        with act_cols[0]:
                            if st.button("💾 保存", key=f"save_{r['id']}",
                                         type="primary", width="stretch"):
                                try:
                                    db.set_review(r["id"], "保存")
                                    st.session_state["_force_vec_sync"] = True
                                    _invalidate_caches()
                                    st.toast("已保存", icon="📁")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"保存失败：{e}")
                        with act_cols[1]:
                            if st.button("🗑 删除", key=f"del_{r['id']}",
                                         width="stretch"):
                                st.session_state["_pending_del_single"] = r["id"]
                                st.rerun()
            # 单条删除二次确认
            if st.session_state.get("_pending_del_single") == r["id"]:
                st.warning(f"⚠️ 确认删除「{r['title'][:30]}…」？删除不可恢复。")
                conf = st.columns([1, 1, 8])
                with conf[0]:
                    if st.button("✅ 确认删", key=f"cfm_del_{r['id']}",
                                 type="primary", width="stretch"):
                        try:
                            db.set_review(r["id"], "删除")
                            _invalidate_caches()
                            st.toast("已删除", icon="🗑")
                        except Exception as e:
                            st.error(f"删除失败：{e}")
                        st.session_state.pop("_pending_del_single", None)
                        st.rerun()
                with conf[1]:
                    if st.button("取消", key=f"cancel_del_{r['id']}", width="stretch"):
                        st.session_state.pop("_pending_del_single", None)
                        st.rerun()
