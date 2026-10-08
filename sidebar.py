# -*- coding: utf-8 -*-
"""侧边栏：库存概览 / 语义对标库 / 数据维护 / 图片缓存 / 手动爬取 / 退出登录。

2026-10-08 从 app.py 拆出。原来这是一整个 `with st.sidebar:` 裸写在主流程里，
现在收敛成一个函数，主流程只留一行 render_sidebar(chroma_stats)。

拆之前先做过的校验（避免搬完才发现副作用）：
- 本段不引用 app.py 任何顶层函数/类 → 无循环依赖
- 本段定义的变量（s / stats / n / cc1 ...）经 AST 检查，后续代码虽重名但都会
  自行重新赋值，唯一"疑似泄漏"的 s 经核对是 SQL 里的 %s 占位符，属误判
  → 包进函数后变量作用域收窄，行为不变
"""
from __future__ import annotations

import os
import shutil
import time as _time
from datetime import date, timedelta

import streamlit as st

import config
import crawler
import db
from db_cache import (
    _get_setting_cached,
    _invalidate_caches,
    _invalidate_setting_cache,
    _stats_overview_cached,
)
from image_loader import (
    _cache_stats,
    _cleanup_expired_cache,
    _enforce_cache_size_limit,
    _CACHE_EXPIRE_DAYS,
    _CACHE_MAX_SIZE_MB,
    _IMG_CACHE,
    _IMG_CACHE_DIR,
)


def render_sidebar(chroma_stats: dict) -> None:
    """渲染整个侧边栏。

    chroma_stats：由主流程调用 topic_matcher.ensure_index_synced() 的结果传入，
    告诉侧边栏"语义对标库现在是健康的还是回退到了关键词检索"。
    """
    with st.sidebar:
        st.subheader("📊 库存概览")
        s = _stats_overview_cached()
        for k, v in s.items():
            st.metric(k, v)
        st.divider()
        st.caption("数据库：Supabase PostgreSQL（云端）")
        st.caption(f"演示种子开关：{'开' if config.DEMO_SEED_ON_EMPTY else '关'}")
        st.caption("提示：要在审核台看到真实稿件，需在 config.py 里填好栏目 URL 并运行 `python crawler.py`。")
        st.divider()
        st.subheader("🧠 语义对标库")
    if chroma_stats.get("fallback"):
        st.warning("回退关键词检索（Silicon Flow API 或向量库初始化失败）")
        if chroma_stats.get("error"):
            st.caption(f"错误：{chroma_stats['error']}")
    else:
        st.metric("已建索引稿件", chroma_stats.get("in_index", 0))
        st.caption(
            f"本次：新增 {chroma_stats.get('added', 0)} / "
            f"删除 {chroma_stats.get('removed', 0)}"
        )
        st.caption("嵌入：智谱 embedding-3（Zhipu API）")
        if chroma_stats.get("error"):
            st.caption(f"⚠️ 同步警告：{chroma_stats['error']}")
        st.divider()
        st.subheader("🧹 数据维护")
        if st.button("清理历史非图片新闻", key="btn_cleanup_nonphoto", width="stretch"):
            st.session_state["_confirm_cleanup"] = True
            st.rerun()
        if st.session_state.get("_confirm_cleanup"):
            st.warning("⚠️ 将删除 has_image=FALSE 或 body_text >300 字的稿件，不可恢复。")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("✅ 确认清理", key="btn_cleanup_confirm", type="primary", width="stretch"):
                    try:
                        n = db.cleanup_non_photo_news()
                        st.success(f"已清理 {n} 条非图片新闻稿件")
                        _invalidate_caches()
                    except Exception as e:
                        st.error(f"清理失败：{e}")
                    st.session_state.pop("_confirm_cleanup", None)
                    st.rerun()
            with c2:
                if st.button("取消", key="btn_cleanup_cancel", width="stretch"):
                    st.session_state.pop("_confirm_cleanup", None)
                    st.rerun()
        # 图片缓存管理
        st.divider()
        st.subheader("🗄 图片缓存管理")
        stats = _cache_stats()
        st.caption(f"缓存文件：{stats['count']} 个 · 总大小：{stats['total_mb']} MB · "
                   f"最旧：{stats['oldest_days']} 天前 · 过期阈值：{_CACHE_EXPIRE_DAYS} 天 · "
                   f"上限：{_CACHE_MAX_SIZE_MB} MB")
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("🧹 清理过期缓存", key="btn_clean_expired", width="stretch"):
                n = _cleanup_expired_cache()
                _enforce_cache_size_limit()
                st.success(f"已清理 {n} 个过期缓存文件")
        with cc2:
            if st.button("🗑 清空全部缓存", key="btn_clear_all_cache", width="stretch"):
                st.session_state["_confirm_clear_cache"] = True
        if st.session_state.get("_confirm_clear_cache"):
            st.warning("⚠️ 将删除所有图片缓存文件，下次访问需重新下载。确认继续？")
            ccc1, ccc2 = st.columns(2)
            with ccc1:
                if st.button("✅ 确认清空", key="btn_clear_cache_confirm",
                             type="primary", width="stretch"):
                    import shutil
                    try:
                        shutil.rmtree(_IMG_CACHE_DIR)
                        os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
                        _IMG_CACHE.clear()
                        st.success("已清空全部图片缓存")
                    except Exception as e:
                        st.error(f"清空失败：{e}")
                    st.session_state.pop("_confirm_clear_cache", None)
            with ccc2:
                if st.button("取消", key="btn_clear_cache_cancel", width="stretch"):
                    st.session_state.pop("_confirm_clear_cache", None)
                    st.rerun()
        # 手动爬取
        st.divider()
        st.subheader("🔄 手动爬取")
        if st.button("📅 立即爬取今日", key="btn_crawl_today", type="primary", width="stretch"):
            with st.spinner("正在爬取各媒体今日稿件（约 1-3 分钟）..."):
                try:
                    stats = crawler.crawl_all()
                    db.set_setting("last_crawl_date", _time.strftime("%Y-%m-%d"))
                    _invalidate_setting_cache()
                    _invalidate_caches()
                    st.success(f"爬取完成：新增 {stats.get('added', 0)} 条，"
                               f"跳过 {stats.get('skipped', 0)} 条，"
                               f"无图过滤 {stats.get('skipped_no_image', 0)} 条")
                except Exception as e:
                    st.error(f"爬取失败：{e}")
        st.caption("补爬多日：选择起止日期，重复稿件自动跳过")
        last_crawl = _get_setting_cached("last_crawl_date")
        try:
            _default_start = date.fromisoformat(last_crawl) + timedelta(days=1) if last_crawl else date.today()
        except (ValueError, TypeError):
            _default_start = date.today()
        _default_start = min(_default_start, date.today())
        col_s, col_e = st.columns(2)
        with col_s:
            crawl_start = st.date_input("起始日期", value=_default_start, key="crawl_start")
        with col_e:
            crawl_end = st.date_input("结束日期", value=date.today(), key="crawl_end")
        if st.button("🚀 开始范围爬取", key="btn_crawl_range", width="stretch"):
            if crawl_start > crawl_end:
                st.error("起始日期不能晚于结束日期")
            else:
                days = (crawl_end - crawl_start).days + 1
                with st.spinner(f"正在爬取 {crawl_start} ~ {crawl_end}（共 {days} 天）..."):
                    try:
                        stats = crawler.crawl_date_range(crawl_start, crawl_end)
                        db.set_setting("last_crawl_date", crawl_end.isoformat())
                        _invalidate_setting_cache()
                        _invalidate_caches()
                        st.success(f"范围爬取完成：新增 {stats.get('added', 0)} 条，"
                                   f"跳过 {stats.get('skipped', 0)} 条，"
                                   f"无图过滤 {stats.get('skipped_no_image', 0)} 条")
                    except Exception as e:
                        st.error(f"爬取失败：{e}")
        # 退出登录
        if config.AUTH_ENABLED and st.session_state.get("authenticated"):
            st.divider()
            if st.button("🚪 退出登录", key="btn_logout", width="stretch"):
                st.session_state.pop("authenticated", None)
                st.rerun()
