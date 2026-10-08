"""Streamlit 审核台主入口。

启动：
    streamlit run app.py

6 个 Tab：
1. 今日审核   —— 5 分钟审核新稿（保存 / 删除）
2. 历史已审   —— 看过去审核结果，复盘
3. 常规日历   —— 未来两周常规选题预警 + 命中率
4. 素材对标   —— 选题对标（语义检索）+ 图文素材库
5. 撰稿中心   —— 行者撰稿（AI写稿）+ 成稿体检（质量检查）
6. 使用说明   —— 操作指南

投稿记录功能在第 3 个 tab 同屏管理（与命中率一起看）。

2026-10-03 新增：访问密码登录门控（auth.py）。首次使用需设置密码，之后每次访问需登录。
"""
from __future__ import annotations

import json
import os
import re
import time
import base64
import hashlib
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st

import config
import db
import auth
import calendar_engine
import topic_matcher
import draft_checker
import ai_writer
import crawler

st.set_page_config(page_title="行者", layout="wide")


def _bj_time(iso_str: str | None) -> str:
    """把库里存的 UTC 时间转成北京时间显示（使用者在中国，看 UTC 会误判成"没保存"）。

    定义在文件最前面：多个标签页都要用，而 Streamlit 是从上到下顺序执行的。
    """
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


# 图片下载/缓存/失败归因：2026-10-08 已整体拆出到 image_loader.py，
# 这里只做符号再导出，调用点保持原样不用改。
from image_loader import (
    _archived_data_uri,
    _cache_stats,
    _cleanup_expired_cache,
    _disk_cache_key,
    _disk_cache_path,
    _enforce_cache_size_limit,
    _img_to_data_uri,
    _is_image_bytes,
    _last_cache_cleanup,
    _load_disk_cache,
    _normalize_display_url,
    _preload_images,
    _render_image,
    _save_disk_cache,
    _CACHE_CLEANUP_INTERVAL_SEC,
    _CACHE_EXPIRE_DAYS,
    _CACHE_MAX_SIZE_MB,
    _FAIL_RETRY_SECONDS,
    _IMG_CACHE,
    _IMG_CACHE_DIR,
    _IMG_FAIL_REASON,
    _MAX_IMG_BYTES,
    _last_cache_cleanup,
)


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


# ==================== 性能优化：缓存包装 ====================
# 2026-10-03 新增：用 Streamlit 缓存减少重复数据库查询
# - init_db: 整个会话只执行一次（建表是幂等的）
# - 读查询: ttl 缓存，写操作后手动清空
@st.cache_resource
def _init_db_cached():
    db.init_db()


@st.cache_data(ttl=30)
def _stats_overview_cached():
    return db.stats_overview()


# 配置项（如"上次爬取日期"）原来每次交互都要查两次库，跨境往返各约 0.4 秒，
# 而它一天才变一次 —— 缓存住，写的时候清。
@st.cache_data(ttl=300)
def _get_setting_cached(key: str, _v: int = 0):
    return db.get_setting(key)


# 草稿列表：原来每次点击都会查一次库（即使抽屉没打开，Streamlit 也会渲染里面的内容）。
# 缓存 + 写操作后清空，既省掉往返，又保证"存完立刻能看到"。
@st.cache_data(ttl=120)
def _list_drafts_cached(limit: int = 30, _v: int = 0):
    return [dict(d) for d in db.list_drafts(limit=limit)]


# 这些列表一天才被爬虫更新一次；每次交互都回源查库（跨境约 0.5 秒/次）纯属浪费。
# 放长缓存，写操作后由 _invalidate_caches() 主动清空，保证改完立刻能看到。
@st.cache_data(ttl=30)
def _fetch_unreviewed_cached(limit=30):
    return db.fetch_unreviewed(limit=limit)


@st.cache_data(ttl=60)
def _fetch_reviewed_cached(limit=200):
    return db.fetch_reviewed(limit=limit)


@st.cache_data(ttl=60)
def _fetch_image_articles_cached(limit=200):
    return db.fetch_image_articles(limit)


# 已排除列表：爬虫写、页面读，一天才变一次，同样走缓存 + 写后失效
@st.cache_data(ttl=60)
def _list_excluded_cached(limit: int = 60, _v: int = 0):
    return [dict(r) for r in db.list_excluded(limit=limit)]


def _invalidate_caches():
    """审核/删除/投稿等写操作后调用，清空所有数据缓存，确保列表立即刷新。"""
    _stats_overview_cached.clear()
    _fetch_unreviewed_cached.clear()
    _fetch_reviewed_cached.clear()
    _fetch_image_articles_cached.clear()
    _list_excluded_cached.clear()


def _invalidate_draft_cache():
    """草稿发生增删改后调用：列表缓存失效，下一轮重新读库（保证存完立刻可见）。"""
    _list_drafts_cached.clear()


def _invalidate_setting_cache():
    """配置项（如 last_crawl_date）被改写后调用。"""
    _get_setting_cached.clear()


st.title("🧭 行者")

# ---- 观感优化：运行时不要整片糊白 ----
# Streamlit 在脚本重跑时，会把每个区块的透明度压到 0.33（字都看不清），
# 每次点按钮都"白一下"，在跨境访问时尤其明显。这里改成轻微淡化：
# 既能提示"正在处理"，内容又始终看得清。
st.markdown(
    """
    <style>
    [data-stale="true"] {
        opacity: .78 !important;
        transition: opacity .2s ease !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# 启动时初始化数据库（带重试：连接池失效时用户可点击重试恢复）
# 用 @st.cache_resource 保证整个会话只跑一次，避免每次交互都查库建表
_db_ok = False
for _attempt in range(2):  # 自动重试1次（连接池可能刚重建）
    try:
        _init_db_cached()
        _db_ok = True
        break
    except Exception as e:
        _db_err = e
        db._reset_pool()  # 连接可能失效，重建池后重试
if not _db_ok:
    # DbConnectionError 的 message 本身已经是给终端用户看的中文说明，
    # 直接展示即可；其它异常才需要补上类型名方便排障。
    if isinstance(_db_err, db.DbConnectionError):
        _tip = str(_db_err)
    else:
        _tip = f"{type(_db_err).__name__}: {_db_err}"
    st.error(f"⚠️ {_tip}")
    st.caption("别慌，你的数据都还在。多数情况下等十几秒再点一次就好了。")
    if st.button("🔄 重试连接", type="primary"):
        st.rerun()
    st.stop()


# ---------------- 访问密码登录门控 ----------------
# AUTH_ENABLED=False 时跳过登录（本地调试用）
if config.AUTH_ENABLED:
    _authed = st.session_state.get("authenticated", False)

    if not _authed:
        _has_pw = auth.has_access_password()

        if not _has_pw:
            # 首次使用：设置访问密码
            st.info("🔐 首次使用，请设置访问密码（整个应用只有一个密码，务必牢记）。")
            with st.form("set_password_form", clear_on_submit=True):
                _pw1 = st.text_input("设置访问密码", type="password", placeholder="至少 8 位")
                _pw2 = st.text_input("确认密码", type="password")
                _set_submitted = st.form_submit_button("✅ 设置密码", type="primary")
            if _set_submitted:
                if not _pw1:
                    st.error("密码不能为空")
                elif _pw1 != _pw2:
                    st.error("两次输入的密码不一致")
                else:
                    try:
                        auth.set_access_password(_pw1)
                        st.session_state["authenticated"] = True
                        st.success("密码设置成功，已自动登录")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
            st.stop()

        else:
            # 已有密码：登录（带失败限流：5 次失败后锁定 5 分钟）
            _max_failures = 5
            _lock_seconds = 300  # 5 分钟
            _fail_count = st.session_state.get("_login_fail_count", 0)
            _lock_until = st.session_state.get("_login_lock_until", 0)
            _now = time.time()

            if _now < _lock_until:
                _remaining = int(_lock_until - _now)
                st.error(f"🔒 登录失败次数过多，已锁定 {_remaining} 秒后重试")
                st.stop()

            with st.form("login_form", clear_on_submit=True):
                _pw = st.text_input("🔐 请输入访问密码", type="password")
                _login_submitted = st.form_submit_button("登录", type="primary")
            if _login_submitted:
                if auth.verify_access_password(_pw):
                    st.session_state["authenticated"] = True
                    st.session_state["_login_fail_count"] = 0
                    st.session_state["_login_lock_until"] = 0
                    st.rerun()
                else:
                    _fail_count += 1
                    st.session_state["_login_fail_count"] = _fail_count
                    if _fail_count >= _max_failures:
                        st.session_state["_login_lock_until"] = _now + _lock_seconds
                        st.error(f"密码错误，已连续失败 {_fail_count} 次，锁定 {_lock_seconds // 60} 分钟")
                    else:
                        st.error(f"密码错误，还剩 {_max_failures - _fail_count} 次尝试机会")
            st.caption("提示：忘记密码需联系管理员重置（清空 app_setting 表中 access_password 记录）。")
            st.stop()
# ---------------- 登录门控结束 ----------------


import time as _time
import threading as _threading

# 清理过期未审核稿件（每 5 分钟跑一次，避免每次交互都查库）
# 非阻塞：失败不影响审核台使用，只记日志
_CLEANUP_INTERVAL = 300  # 5 分钟
_last_cleanup = st.session_state.get("_last_cleanup_time", 0)
if (_time.time() - _last_cleanup) > _CLEANUP_INTERVAL:
    try:
        _cleaned = db.cleanup_old_unreviewed()
        st.session_state["_last_cleanup_time"] = _time.time()
        if _cleaned:
            config.logger.info(f"清理 {_cleaned} 条过期未审核稿件")
    except Exception as _e:
        config.logger.warning(f"cleanup_old_unreviewed 失败（不影响审核台使用）: {_e}")


# 启动时自动爬取：若今天还没爬过，后台线程跑一次 crawler（不阻塞 UI）
_today_str = _time.strftime("%Y-%m-%d")
if not st.session_state.get("crawl_started_today"):
    try:
        _last_crawl = _get_setting_cached("last_crawl_date")
    except Exception:
        _last_crawl = None
    if _last_crawl != _today_str:
        def _bg_crawl():
            try:
                crawler.crawl_all()
                db.set_setting("last_crawl_date", _today_str)
                _invalidate_setting_cache()
                # 后台爬完也让列表缓存失效，否则新稿要等缓存过期才显示。
                # 这里跑在非脚本线程里，包 try 防止清缓存异常影响爬虫结果落库。
                try:
                    _invalidate_caches()
                except Exception as _ce:
                    config.logger.warning(f"清缓存失败（不影响数据）: {_ce}")
                config.logger.info("后台自动爬取完成")
            except Exception as _e:
                config.logger.warning(f"后台自动爬取失败: {_e}")
        _t = _threading.Thread(target=_bg_crawl, daemon=True)
        _t.start()
        st.session_state["crawl_started_today"] = True
        config.logger.info("已启动后台自动爬取线程")

# 向量索引同步：仅在审核写入后（_force_vec_sync=True）触发，避免每会话定时全量同步
_force = st.session_state.get("_force_vec_sync", False)
if _force:
    try:
        _chroma_stats = topic_matcher.ensure_index_synced()
        st.session_state["_force_vec_sync"] = False
    except Exception as e:
        _chroma_stats = {"error": str(e), "fallback": True}
else:
    _chroma_stats = st.session_state.get("_vec_sync_stats", {"fallback": False, "in_index": 0})
st.session_state["_vec_sync_stats"] = _chroma_stats


# ---------------- 侧边状态 ----------------
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
    if _chroma_stats.get("fallback"):
        st.warning("回退关键词检索（Silicon Flow API 或向量库初始化失败）")
        if _chroma_stats.get("error"):
            st.caption(f"错误：{_chroma_stats['error']}")
    else:
        st.metric("已建索引稿件", _chroma_stats.get("in_index", 0))
        st.caption(
            f"本次：新增 {_chroma_stats.get('added', 0)} / "
            f"删除 {_chroma_stats.get('removed', 0)}"
        )
        st.caption("嵌入：智谱 embedding-3（Zhipu API）")
        if _chroma_stats.get("error"):
            st.caption(f"⚠️ 同步警告：{_chroma_stats['error']}")
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


# ---------------- 图文素材上传配置 ----------------
_UPLOAD_MAX_MB = 10      # 单张上传大小上限
_UPLOAD_MAX_DIM = 1920  # 超过此边长自动缩放
_UPLOAD_PAGE_SIZE = 12  # 每页展示数量


# ---------------- Tabs ----------------
tab_review, tab_history, tab_excluded, tab_calendar, tab_material, tab_writing, tab_help = st.tabs(
    ["✅ 今日审核", "🗂 历史已审", "🚫 已排除", "📅 常规日历", "📚 素材对标", "✍️ 撰稿中心", "❓ 使用说明"]
)


# ----- Tab 1: 今日审核（批量 checkbox 审核 UI） -----
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


with tab_review:
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


# ----- Tab 2: 历史已审 -----
with tab_history:
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


# ----- Tab 3: 已排除（过滤留痕，可恢复） -----
with tab_excluded:
    st.subheader("已排除稿件")
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


# ----- Tab 4: 常规日历 + 投稿记录 -----
with tab_calendar:
    st.subheader("未来两周常规选题预警")
    try:
        upcoming = calendar_engine.upcoming_topics(horizon_days=14)
    except Exception as _e:
        upcoming = []
        st.warning(f"日历加载失败：{_e}")
    if not upcoming:
        st.info("未来两周没有触发常规选题。可在 config.py 的 ROUTINE_TOPICS_SEED 中追加。")
    else:
        for t in upcoming:
            st.markdown(
                f"**{t['topic']}** "
                f"`{t['status']}` 推荐版面：{t['recommended_column']} "
                f"(提前 {t['lead_days']} 天)"
            )
            st.caption(t["note"])

    st.divider()
    st.subheader("用稿规律（命中率）")
    try:
        hit = calendar_engine.hit_rate_by_column()
    except Exception as _e:
        hit = []
        st.warning(f"命中率统计加载失败：{_e}")
    if hit:
        st.dataframe(hit, width="stretch", hide_index=True)
    else:
        st.info("还没有投稿记录。下面录一条试试。")

    st.divider()
    st.subheader("📝 新增投稿记录")
    with st.form("add_sub", clear_on_submit=True):
        cs = st.columns([2, 2, 2, 2])
        topic = cs[0].text_input("选题")
        media = cs[1].selectbox("目标媒体", ["中国石油报", "辽宁日报", "企业内网"])
        col = cs[2].text_input("目标版面")
        result = cs[3].selectbox("结果", ["待审", "录用", "退稿"])
        note = st.text_input("备注")
        if st.form_submit_button("保存投稿记录") and topic:
            try:
                with db.get_conn() as c:
                    cur = db.conn_cursor(c)
                    cur.execute(
                        "INSERT INTO submission"
                        "(topic, target_media, target_column, submitted_at, "
                        "result, note) VALUES (%s,%s,%s,%s,%s,%s)",
                        (topic, media, col or None, db.now_iso(), result, note or None),
                    )
                st.success("已保存")
                _invalidate_caches()
                st.rerun()
            except Exception as e:
                st.error(f"保存失败：{e}")


# ----- Tab 4: 素材对标（选题对标 + 图文素材）-----
with tab_material:
    sub_match, sub_image = st.tabs(["🎯 选题对标", "📷 图文素材"])
    with sub_match:
        st.subheader("选题对标器（语义检索）")
        st.caption("用大白话描述你的选题即可（不用精确命中标题词），AI 按语义找最接近的已发稿。只在你标过「相关/借鉴」的稿件里检索。")
        kw_str = st.text_input("你的选题/素材", placeholder="如：春季装置检修里一名催化主操的故事")
        if kw_str:
            kws = [k.strip() for k in kw_str.replace("，", " ").split() if k.strip()]
            matches = topic_matcher.match(kws, top_k=5)
            if not matches:
                st.warning("没找到相似稿件——审核台多审几篇，库里有了再回来。")
            else:
                st.markdown("### 相似已发稿")
                for m in matches:
                    st.markdown(
                        f"- **{_md_escape(m['title'])}** "
                        f"`{_md_escape(m['source'])}/{_md_escape(m['column'])}` "
                        f"_{_md_escape(str(m['publish_date'] or ''))}_ "
                        f"[相关度 {m['score']}]"
                    )
                    st.caption(_safe_anchor(f"链接（{m['decision']}）", m['url']))
            st.markdown("### 角度建议")
            with st.spinner("AI 生成角度建议中…（失败会自动回退到规则建议）"):
                advice = topic_matcher.angle_advice(kws, matches)
            for a in advice:
                st.markdown(f"- {a}")


    
    with sub_image:
        st.subheader("📷 图文素材库")
        st.caption("所有带图片的稿件（不限行业），可作图片新闻参考。")

        # ----- 用户上传图片（Pillow 验证 + 压缩）-----
        st.markdown("#### 上传本地图片")
        st.caption(
            f"支持 jpg/jpeg/png/gif，单张 ≤ {_UPLOAD_MAX_MB}MB；"
            f"超过 {_UPLOAD_MAX_DIM}px 自动缩小、质量 85%。"
        )
        uploaded = st.file_uploader(
            "选择图片",
            type=["jpg", "jpeg", "png", "gif"],
            accept_multiple_files=True,
            key="img_uploader",
        )
        if uploaded:
            try:
                from PIL import Image
            except ImportError:
                st.error("缺少 Pillow 依赖，请在虚拟环境执行：.venv\\Scripts\\pip install Pillow")
                st.stop()
            try:
                os.makedirs(config.UPLOAD_DIR, exist_ok=True)
            except (OSError, PermissionError):
                st.error("当前环境文件系统只读，无法保存上传图片。请在本地运行或配置可写目录后使用此功能。")
                st.stop()
            saved, errors = [], []
            for f in uploaded:
                # 大小校验
                if len(f.getbuffer()) > _UPLOAD_MAX_MB * 1024 * 1024:
                    errors.append(f"{f.name}：超过 {_UPLOAD_MAX_MB}MB 限制")
                    continue
                try:
                    f.seek(0)
                    img = Image.open(f)
                    img.verify()  # 验证是真图片
                    f.seek(0)
                    img = Image.open(f)  # verify 后需重开
                    # 缩放
                    if max(img.size) > _UPLOAD_MAX_DIM:
                        img.thumbnail((_UPLOAD_MAX_DIM, _UPLOAD_MAX_DIM))
                    # 模式归一（保证 JPEG 能存）
                    if img.mode in ("RGBA", "P", "LA"):
                        img = img.convert("RGB")
                    # 落盘
                    ts = _time.strftime("%Y%m%d_%H%M%S")
                    safe_name = re.sub(r'[\\/:*?"<>|]', '_', f.name)
                    save_path = os.path.join(config.UPLOAD_DIR, f"{ts}_{safe_name}")
                    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else "jpg"
                    if ext == "png":
                        img.save(save_path, "PNG", optimize=True)
                    else:
                        # jpg/jpeg/gif 一律存为 JPEG
                        img.save(save_path, "JPEG", quality=85, optimize=True)
                    saved.append(save_path)
                except Exception as e:
                    errors.append(f"{f.name}：无法识别为图片 ({e})")
            if saved:
                st.success(f"已上传 {len(saved)} 张")
                cols = st.columns(min(len(saved), 4))
                for i, p in enumerate(saved):
                    with cols[i % len(cols)]:
                        st.image(p, caption=os.path.basename(p), width="stretch")
            if errors:
                st.error("以下文件上传失败：")
                for e in errors:
                    st.markdown(f"- {_md_escape(e)}")

        # ----- 已上传图片：筛选 + 分页 + 逐张/批量删除 -----
        if os.path.isdir(config.UPLOAD_DIR):
            local_imgs = sorted(
                [os.path.join(config.UPLOAD_DIR, f) for f in os.listdir(config.UPLOAD_DIR)
                 if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif"))],
                key=os.path.getmtime,
                reverse=True,
            )
            search = st.text_input("🔍 按文件名筛选", key="img_search", placeholder="输入关键字过滤")
            if search:
                local_imgs = [p for p in local_imgs if search.lower() in os.path.basename(p).lower()]
            if local_imgs:
                total = len(local_imgs)
                total_pages = max(1, (total + _UPLOAD_PAGE_SIZE - 1) // _UPLOAD_PAGE_SIZE)
                page = min(st.session_state.get("img_page", 0), total_pages - 1)
                start = page * _UPLOAD_PAGE_SIZE
                page_imgs = local_imgs[start:start + _UPLOAD_PAGE_SIZE]
                st.caption(f"共 {total} 张 · 第 {page + 1}/{total_pages} 页")

                # 网格：每行 3 张，每张配元数据 + 逐张删 + 多选框
                # key 统一用文件名 basename（唯一标识），避免删除/翻页后索引错位导致 widget 状态错乱
                sel_keys = []  # [(key, path)] 用于批量删
                for row_start in range(0, len(page_imgs), 3):
                    row = page_imgs[row_start:row_start + 3]
                    cols = st.columns(3)
                    for j, p in enumerate(row):
                        bn = os.path.basename(p)
                        sel_key = f"sel_{bn}"
                        del_key = f"del_{bn}"
                        sel_keys.append((sel_key, p))
                        with cols[j]:
                            st.image(p, width="stretch")
                            st.caption(f"📄 {bn}")
                            st.caption(
                                f"{os.path.getsize(p) // 1024} KB · "
                                f"{_time.strftime('%m-%d %H:%M', _time.localtime(os.path.getmtime(p)))}"
                            )
                            if st.button("🗑 删除", key=del_key):
                                try:
                                    os.remove(p)
                                    # 清理残留的 checkbox 状态，避免下次渲染时 key 仍为 True
                                    st.session_state.pop(sel_key, None)
                                    st.toast(f"已删除 {bn}", icon="🗑")
                                except Exception as e:
                                    st.error(f"删除失败：{e}")
                                st.rerun()
                            st.checkbox("选中", key=sel_key)

                # 批量删除条
                st.divider()
                n_sel = sum(1 for k, _ in sel_keys if st.session_state.get(k, False))
                bc1, bc2 = st.columns(2)
                with bc1:
                    bulk_label = f"🗑 删除选中({n_sel})" if n_sel else "🗑 删除选中"
                    if st.button(bulk_label, key="bulk_del", disabled=(n_sel == 0)):
                        st.session_state["_bulk_confirm"] = True
                        st.rerun()
                with bc2:
                    if st.button("清空本页选择", key="sel_clear"):
                        # 用 pop 而非直接设 False：widget 已渲染时设其 key 会抛 StreamlitAPIException
                        for k, _ in sel_keys:
                            st.session_state.pop(k, None)
                        st.session_state["_bulk_confirm"] = False
                        st.rerun()

                # 批量删除二次确认
                if st.session_state.get("_bulk_confirm", False):
                    st.warning(f"⚠️ 确认删除本页选中的 {n_sel} 张？此操作不可撤销。")
                    kc1, kc2 = st.columns(2)
                    with kc1:
                        if st.button("✅ 确认删除", key="bulk_yes", type="primary"):
                            for k, p in sel_keys:
                                if st.session_state.get(k, False) and os.path.exists(p):
                                    try:
                                        os.remove(p)
                                    except Exception:
                                        pass
                            for k, _ in sel_keys:
                                st.session_state.pop(k, None)
                            st.session_state["_bulk_confirm"] = False
                            st.toast("批量删除完成", icon="🗑")
                            st.rerun()
                    with kc2:
                        if st.button("取消", key="bulk_no"):
                            st.session_state["_bulk_confirm"] = False
                            st.rerun()

                # 分页按钮
                pc1, pc2, pc3 = st.columns([1, 2, 1])
                with pc1:
                    if st.button("⬅ 上一页", key="pg_prev", disabled=(page == 0)):
                        st.session_state["img_page"] = page - 1
                        st.rerun()
                with pc2:
                    st.caption(f"第 {page + 1} / {total_pages} 页")
                with pc3:
                    if st.button("下一页 ➡", key="pg_next", disabled=(page >= total_pages - 1)):
                        st.session_state["img_page"] = page + 1
                        st.rerun()
            else:
                st.info("暂无已上传图片。先在上面上传几张试试。")

        imgs = _fetch_image_articles_cached(200)
        st.metric("图文稿件", len(imgs))
        # 并发预加载所有稿件图片
        all_img_urls = []
        for a in imgs:
            raw = a.get("image_urls")
            if raw:
                try:
                    urls = json.loads(raw) if isinstance(raw, str) else raw
                    all_img_urls.extend(urls[:5])
                except (json.JSONDecodeError, TypeError):
                    pass
        _preload_images(all_img_urls)

        for a in imgs:
            with st.expander(f"[{a['source_name']}/{a['column_name']}] {a['title']}", expanded=False):
                st.caption(f"{a['publish_date'] or ''}")
                # 展示图片
                img_urls_raw = a.get("image_urls")
                if img_urls_raw:
                    try:
                        img_urls = json.loads(img_urls_raw) if isinstance(img_urls_raw, str) else img_urls_raw
                        if img_urls:
                            for u in img_urls[:5]:
                                _render_image(u)
                    except (json.JSONDecodeError, TypeError):
                        pass
                # 正文（图片新闻的 summary 是 body_text 的前缀，避免重复只显示 body_text）
                if a.get("body_text"):
                    st.markdown(_md_escape(a["body_text"][:1000]))
                elif a.get("summary"):
                    st.markdown(_md_escape(a["summary"]))
                if a.get("url"):
                    st.markdown(_safe_anchor("原文链接", a["url"]))

    


# ----- Tab 5: 撰稿中心（行者撰稿 + 成稿体检）-----

def _set_draft_content(title: str, body: str) -> None:
    """统一入口：改写当前稿件内容时，必须连同"带 key 的输入框"一起更新。

    这是 Streamlit 最容易中招的坑：控件一旦带了 key，就会记住自己上一次的值，
    之后 `value=` 参数不再生效。只改 session_state 里的源变量（draft_title/body）
    没用——界面继续显示旧内容，下一轮还会把旧内容反向写回 session_state。
    典型症状：点开另一条草稿却还是上一篇、改过的名字被改回去。
    """
    st.session_state["draft_title"] = title
    st.session_state["draft_body"] = body
    st.session_state["draft_title_in"] = title
    st.session_state["draft_body_in"] = body


def _draft_save_status(dirty: bool) -> str:
    """草稿保存状态文案。dirty=True 表示内容与库里不一致（有未入库的改动）。"""
    if dirty:
        return "⚠️ 有未保存的修改（点「保存草稿」立即入库）"
    saved_at = st.session_state.get("draft_saved_at")
    if saved_at:
        return f"✅ 已保存 · {_bj_time(saved_at)}（北京时间）· 存于云端草稿箱"
    return "尚未保存"


with tab_writing:
    sub_writer, sub_check = st.tabs(["✍️ 行者撰稿", "📝 成稿体检"])
    with sub_writer:
        st.subheader("✍️ 行者撰稿")
        st.caption("AI 辅助生成新闻稿初稿（智谱 GLM-4.7-Flash，失败回退腾讯云 deepseek）。"
                   "稿件存入云端草稿库，换电脑、关页面都能找回。")

        # 跨 rerun 的一次性提示（保存成功等）在这里落地，避免刷新后提示消失
        _flash = st.session_state.pop("_flash_msg", None)
        if _flash:
            st.success(_flash)

        # ---------- 我的草稿（云端持久化，避免刷新/关闭即丢失） ----------
        _pending_del = st.session_state.get("_pending_del_draft")
        with st.expander("📂 我的草稿（点名字打开 · ✏️改名 · 🗑删除）", expanded=False):
            # 容错：草稿库读取失败不应连带整页报错（旧容器/网络抖动时曾出现）
            # 走缓存：每次点击都回源查库太亏；增删改后会主动清缓存，立刻可见。
            try:
                _drafts = _list_drafts_cached(30)
                _draft_err = None
            except Exception as _e:  # noqa: BLE001
                _drafts, _draft_err = None, str(_e)[:200]
            if _draft_err:
                st.warning("⚠️ 草稿库暂时读不到（不影响其它功能）。稍后刷新重试即可。")
                st.caption(f"技术细节：{_draft_err}")
            elif not _drafts:
                st.caption("暂无草稿。生成或修改稿件后会自动保存到这里。")
            else:
                # ---------- 改名（草稿列表里直接改名字，不必先载入） ----------
                _renaming = st.session_state.get("_renaming_draft")
                _rtarget = next((x for x in _drafts if x["id"] == _renaming), None) if _renaming else None
                if _renaming and _rtarget is None:
                    st.session_state.pop("_renaming_draft", None)
                if _rtarget is not None:
                    with st.container(border=True):
                        st.markdown("**✏️ 修改草稿名字**")
                        _new_name = st.text_input(
                            "新名字", value=_rtarget["title"] or "",
                            key="draft_rename_in", max_chars=60,
                            placeholder="给它起个好找的名字，如：冬供稿_辽报版_v3",
                        )
                        _rn1, _rn2 = st.columns(2)
                        with _rn1:
                            if st.button("✅ 保存名字", key="btn_rename_ok", type="primary"):
                                _nn = (_new_name or "").strip()
                                if not _nn:
                                    st.warning("名字不能为空")
                                else:
                                    # 只改名字，正文/选题等原样带回，避免误伤内容
                                    _rid = db.save_draft(
                                        title=_nn,
                                        body=_rtarget["body"] or "",
                                        topic=_rtarget["topic"] or "",
                                        angle=_rtarget["angle"] or "",
                                        target_media=_rtarget["target_media"] or "",
                                        draft_id=_rtarget["id"],
                                    )
                                    if _rid:
                                        _invalidate_draft_cache()
                                        # 改的若是正在编辑那条，连显示、输入框和比对基准一起同步，
                                        # 否则编辑框会拿旧名字把它盖回去
                                        if st.session_state.get("current_draft_id") == _rtarget["id"]:
                                            st.session_state["draft_title"] = _nn
                                            st.session_state["draft_title_in"] = _nn
                                            st.session_state["_saved_title"] = _nn
                                            st.session_state["draft_saved_at"] = db.now_iso()
                                        st.session_state["_flash_msg"] = f"✅ 草稿名字已改为「{_nn[:24]}」。"
                                        st.session_state.pop("_renaming_draft", None)
                                        st.rerun()
                                    else:
                                        st.error("改名失败，请稍后重试（稿件内容未动）。")
                        with _rn2:
                            if st.button("取消", key="btn_rename_cancel"):
                                st.session_state.pop("_renaming_draft", None)
                                st.rerun()

                for _d in _drafts:
                    _is_cur = st.session_state.get("current_draft_id") == _d["id"]
                    _c1, _c2, _c3 = st.columns([6.2, 0.9, 0.9])
                    with _c1:
                        _label = ("▶ " if _is_cur else "") + (_d["title"] or "（无标题）")[:32]
                        if st.button(_label, key=f"draft_load_{_d['id']}", width="stretch"):
                            # 关键：连 widget key 一起刷新，否则编辑框会停在上一篇
                            _set_draft_content(_d["title"] or "", _d["body"] or "")
                            st.session_state["draft_topic"] = _d["topic"] or ""
                            st.session_state["draft_angle"] = _d["angle"] or ""
                            st.session_state["draft_target"] = _d["target_media"] or ""
                            st.session_state["current_draft_id"] = _d["id"]
                            st.session_state["_saved_title"] = _d["title"] or ""
                            st.session_state["_saved_body"] = _d["body"] or ""
                            # 载入的草稿本来就是库里已有的，带上入库时间，
                            # 否则状态栏会误显示"尚未保存"（曾把使用者绕晕）
                            st.session_state["draft_saved_at"] = _d["updated_at"]
                            st.session_state["_flash_msg"] = (
                                f"已载入草稿「{(_d['title'] or '无标题')[:24]}」，"
                                "可直接编辑或让 AI 修改。"
                            )
                            st.rerun()
                        st.caption(
                            f"{_bj_time(_d['updated_at'])} · "
                            f"{_d['target_media'] or '未指定媒体'}"
                            + ("　·　当前打开" if _is_cur else "")
                        )
                    with _c2:
                        if st.button("✏️", key=f"draft_rename_{_d['id']}", help="修改这个草稿的名字"):
                            st.session_state["_renaming_draft"] = _d["id"]
                            st.rerun()
                    with _c3:
                        if st.button("🗑", key=f"draft_del_{_d['id']}", help="删除这个草稿"):
                            st.session_state["_pending_del_draft"] = _d["id"]
                            st.rerun()
                if _pending_del:
                    st.warning(f"⚠️ 确认删除草稿 #{_pending_del}？删除后不可恢复。")
                    _dc1, _dc2 = st.columns(2)
                    with _dc1:
                        if st.button("✅ 确认删除", key="btn_draft_del_ok", type="primary"):
                            try:
                                _del_ok = bool(db.delete_draft(_pending_del))
                                _del_err = None
                            except Exception as _e:  # noqa: BLE001
                                _del_ok, _del_err = False, str(_e)[:150]
                            if _del_ok:
                                _invalidate_draft_cache()
                                st.session_state.pop("_pending_del_draft", None)
                                if st.session_state.get("current_draft_id") == _pending_del:
                                    st.session_state["current_draft_id"] = None
                                st.rerun()
                            else:
                                st.error(f"删除失败（草稿仍在）：{_del_err or '未找到该草稿'}")
                                st.session_state.pop("_pending_del_draft", None)
                    with _dc2:
                        if st.button("取消", key="btn_draft_del_cancel"):
                            st.session_state.pop("_pending_del_draft", None)
                            st.rerun()

        with st.form("writer_form"):
            topic = st.text_input("选题关键词*", placeholder="如：春检、安全月、冬季保供")
            col1, col2 = st.columns(2)
            with col1:
                angle = st.text_input("写作角度（可选）", placeholder="如：人物故事、数据对比")
                target = st.selectbox("目标媒体", ["中国石油报", "辽宁日报", "企业内网"])
            with col2:
                # 2026-10-07 调整：短消息稿也要能写（最低 100 字），
                # 上限收到 1000（原来 2000 太长，报社用稿多在 300~800 字）。
                # 想改上限就动这里的 1000 一个数字。
                word_count = st.slider("目标字数", 100, 1000, 800, 50)
            facts = st.text_area("已知事实/数据（可选）", placeholder="如：处理量同比+15%，创历史新高")
            submitted = st.form_submit_button("生成初稿", type="primary")
        if submitted:
            if not topic.strip():
                st.warning("请输入选题关键词")
            else:
                with st.spinner("AI 正在撰写..."):
                    r = ai_writer.write_article(topic, angle, word_count, target, facts)
                if r["ok"]:
                    _set_draft_content(r["title"] or "", r["body"])
                    st.session_state["draft_topic"] = topic
                    st.session_state["draft_angle"] = angle
                    st.session_state["draft_target"] = target
                    # 生成即入库：避免还没来得及点保存就丢了
                    _new_id = db.save_draft(
                        title=r["title"] or "", body=r["body"],
                        topic=topic, angle=angle, target_media=target,
                    )
                    if _new_id:
                        _invalidate_draft_cache()
                        st.session_state["current_draft_id"] = _new_id
                        st.session_state["_saved_title"] = r["title"] or ""
                        st.session_state["_saved_body"] = r["body"]
                        st.session_state["draft_saved_at"] = db.now_iso()
                        st.session_state["_flash_msg"] = (
                            "✍️ 初稿已生成并入库（先存一份保底，不会丢）。"
                            "如需再改，可在下方编辑后点「保存草稿」。"
                        )
                        # 重跑一次，让上方「我的草稿」立刻能看到这条新稿
                        st.rerun()
                    else:
                        st.warning("初稿已生成，但入库失败。请点下方「💾 保存草稿」重试，"
                                   "或先复制正文保底。")
                else:
                    st.error(f"生成失败：{r['error']}")

        # 初稿展示 + AI 修改（在表单外，支持反复修改）
        if st.session_state.get("draft_body"):
            st.divider()
            st.markdown("#### 📄 当前稿件")
            # 这两个输入框只由 key 管值（见 _set_draft_content），不再同时传 value=，
            # 否则 Streamlit 每轮都会刷一条"Session State 与默认值冲突"的告警。
            # setdefault 是兜底：万一外部没同步 key，也能显示正确内容而不是空白。
            st.session_state.setdefault("draft_title_in", st.session_state.get("draft_title", ""))
            st.session_state.setdefault("draft_body_in", st.session_state.get("draft_body", ""))
            cur_title = st.text_input("标题", key="draft_title_in")
            cur_body = st.text_area("正文", height=400, key="draft_body_in")
            # 同步编辑后的值回 session_state，供 AI 修改读取
            st.session_state["draft_title"] = cur_title
            st.session_state["draft_body"] = cur_body

            # ---------- 保存控制条：使用者自己决定什么时候存 ----------
            _dirty = (cur_title, cur_body) != (
                st.session_state.get("_saved_title"), st.session_state.get("_saved_body")
            )
            _sc1, _sc2, _sc3 = st.columns([1.1, 1.2, 2.0])
            with _sc1:
                _click_save = st.button(
                    "💾 保存草稿", type="primary", key="btn_save_draft",
                    help="把当前标题和正文存入云端草稿箱，存完立刻刷新下方/上方草稿列表",
                )
            with _sc2:
                _click_save_as = st.button(
                    "📄 另存为新草稿", key="btn_saveas_draft",
                    help="保留当前这条不动，另存一份新草稿（相当于留一个版本快照）",
                )
            with _sc3:
                _autosave = st.toggle(
                    "自动保存（改动即入库）", value=True, key="draft_autosave",
                    help="开启：正文一改就自动入库，最保险；关闭：只有点「保存草稿」才入库，"
                         "完全由你决定，但关页面前请记得点保存。",
                )

            # 谁触发保存：手动 > 另存 > 自动兜底
            _save_mode = None
            if _click_save:
                _save_mode = "manual"
            elif _click_save_as:
                _save_mode = "copy"
            elif _dirty and _autosave:
                _save_mode = "auto"

            if _save_mode:
                _did = db.save_draft(
                    title=cur_title, body=cur_body,
                    topic=st.session_state.get("draft_topic", ""),
                    angle=st.session_state.get("draft_angle", ""),
                    target_media=st.session_state.get("draft_target", ""),
                    # 另存为：不带 draft_id → 库里新增一条，原稿不动
                    draft_id=None if _save_mode == "copy" else st.session_state.get("current_draft_id"),
                )
                if _did:
                    _invalidate_draft_cache()
                    st.session_state["current_draft_id"] = _did
                    st.session_state["_saved_title"] = cur_title
                    st.session_state["_saved_body"] = cur_body
                    st.session_state["draft_saved_at"] = db.now_iso()
                    if _save_mode == "manual":
                        st.session_state["_flash_msg"] = "✅ 已保存到云端草稿箱。"
                    elif _save_mode == "copy":
                        st.session_state["_flash_msg"] = (
                            "✅ 已另存为新草稿（原稿保持不变）。可在「我的草稿」中切换。"
                        )
                    # 关键：保存后立刻重跑一次，让草稿列表立即出现这一条，
                    # 不用再手动刷新页面（此前列表渲染在上方，本轮拿不到新数据）
                    st.rerun()
                else:
                    st.error("保存失败（稿件未入库）。请检查网络后重试，或先手动复制一份正文保底。")

            st.caption(_draft_save_status(_dirty))

            st.markdown("#### 🔧 AI 修改")
            rev_instr = st.text_area(
                "修改要求",
                placeholder="如：把导语改得更有冲击力；增加一段人物对话；压缩到 500 字以内；语气更口语化…",
                height=80,
                key="rev_instr",
            )
            rc1, rc2 = st.columns([1, 3])
            with rc1:
                if st.button("🤖 AI 按要求修改", type="primary"):
                    if not rev_instr.strip():
                        st.warning("请先填写修改要求")
                    else:
                        with st.spinner("AI 正在修改..."):
                            rr = ai_writer.revise_article(cur_title, cur_body, rev_instr)
                        if rr["ok"]:
                            _set_draft_content(rr["title"], rr["body"])
                            # 关键：先落库、再给提示、最后只 rerun 一次，
                            # 这样提示不会因为中途重跑而被丢掉
                            if st.session_state.get("draft_autosave", True):
                                _rid = db.save_draft(
                                    title=rr["title"], body=rr["body"],
                                    topic=st.session_state.get("draft_topic", ""),
                                    angle=st.session_state.get("draft_angle", ""),
                                    target_media=st.session_state.get("draft_target", ""),
                                    draft_id=st.session_state.get("current_draft_id"),
                                )
                                if _rid:
                                    _invalidate_draft_cache()
                                    st.session_state["current_draft_id"] = _rid
                                    st.session_state["_saved_title"] = rr["title"]
                                    st.session_state["_saved_body"] = rr["body"]
                                    st.session_state["draft_saved_at"] = db.now_iso()
                                    st.session_state["_flash_msg"] = "✅ AI 修改完成，已自动保存到草稿箱。"
                                else:
                                    st.session_state["_flash_msg"] = (
                                        "✅ AI 修改完成，但自动保存失败。请点「💾 保存草稿」。"
                                    )
                            else:
                                st.session_state["_flash_msg"] = (
                                    "✅ AI 修改完成。自动保存已关闭，请点「💾 保存草稿」入库。"
                                )
                            st.rerun()
                        else:
                            st.error(f"修改失败：{rr['error']}")
            with rc2:
                if st.button("🗑 清空稿件"):
                    # 二次确认：原来一点就清，手滑即丢稿
                    st.session_state["_confirm_clear_draft"] = True
                    st.rerun()
            if st.session_state.get("_confirm_clear_draft"):
                st.warning(
                    "⚠️ 确认清空当前稿件？云端草稿库里的历史版本不受影响，"
                    "可在上方「我的草稿」中找回。"
                )
                cc1, cc2 = st.columns(2)
                with cc1:
                    if st.button("✅ 确认清空", key="btn_clear_draft_ok", type="primary"):
                        st.session_state.pop("draft_title", None)
                        st.session_state.pop("draft_body", None)
                        st.session_state["current_draft_id"] = None
                        # 连同输入框自己的 key 一起清，否则下次写新稿还会冒出旧内容
                        st.session_state.pop("draft_title_in", None)
                        st.session_state.pop("draft_body_in", None)
                        st.session_state.pop("_saved_title", None)
                        st.session_state.pop("_saved_body", None)
                        st.session_state.pop("draft_saved_at", None)
                        st.session_state.pop("_confirm_clear_draft", None)
                        st.session_state["_flash_msg"] = (
                            "已清空当前编辑区。云端草稿箱里的稿件没有删除，"
                            "可在上方「我的草稿」随时载入。"
                        )
                        st.rerun()
                with cc2:
                    if st.button("取消", key="btn_clear_draft_cancel"):
                        st.session_state.pop("_confirm_clear_draft", None)
                        st.rerun()
            with st.expander("❓ 怎么用（保存规则一看就懂）", expanded=False):
                st.markdown(
                    "- **生成初稿时会先存一份到草稿箱**（保底，不会丢）。\n"
                    "- 之后的改动要不要存，由你决定：\n"
                    "    - **自动保存打开（默认）**：正文一改就自动入库，最省心。\n"
                    "    - **自动保存关闭**：只有点「💾 保存草稿」才入库。"
                    "状态栏会提示「⚠️ 有未保存的修改」，关页面前记得点一下。\n"
                    "- **💾 保存草稿**：把当前标题+正文存进草稿箱，**存完草稿列表立即更新**，不用刷新页面。\n"
                    "- **📄 另存为新草稿**：原稿不动，再存一份新的（相当于留一个版本快照，"
                    "改坏了可以回去拿旧的）。\n"
                    "- **📂 我的草稿**：点名字即可载入继续编辑；带「▶」的是你当前正在编辑的那条。\n"
                    "    - **✏️ 改名**：不用载入，直接在列表里给它换名字（正文内容不受影响）。\n"
                    "    - **🗑 删除**：删掉这条（有二次确认）。\n"
                    "    - 也可以在正文上方直接改「标题」再保存，效果一样。\n"
                    "- 直接改正文框也行，改完可以让 AI 按你的要求再改一遍。"
                )
    # ----- Tab 8: 使用说明 -----

    with sub_check:
        st.subheader("📝 成稿体检器")
        st.caption("投稿前自检：模糊时间 / 空泛数据 / 绝对化用词 / 图片说明质量。")
        title = st.text_input("稿件标题")
        text = st.text_area("稿件正文", height=220)
        caption = st.text_input("图片说明", placeholder="如：图为催化主操张三在调整反应温度")
        mode = st.radio("校对模式", ["快速模式（仅规则）", "深度模式（规则+AI校对）"], horizontal=True)
        if st.button("开始体检", type="primary") and (title or text):
            result = draft_checker.check(title or "", text or "", caption or "")
            st.markdown("### 🐛 问题清单")
            for i in result["issues"]:
                st.markdown(f"- {_md_escape(i)}")
            if "深度" in mode:
                with st.spinner("AI 校对中..."):
                    ai_r = draft_checker.ai_proofread(title or "", text or "", caption or "")
                st.markdown("### 🤖 AI 深度校对")
                _ai_issues = ai_r.get("ai_issues", [])
                if ai_r.get("ok"):
                    if isinstance(_ai_issues, dict):
                        if not any(_ai_issues.values()):
                            st.success("AI 校对未发现问题")
                        for _cat, _items in _ai_issues.items():
                            if _items:
                                with st.expander(f"{_cat} ({len(_items)})", expanded=False):
                                    for _i in _items:
                                        st.markdown(f"- {_md_escape(_i)}")
                    else:
                        for _i in _ai_issues:
                            st.markdown(f"- {_md_escape(_i)}")
                else:
                    for _i in (_ai_issues if isinstance(_ai_issues, list) else [_ai_issues]):
                        st.error(_md_escape(str(_i)))
            st.markdown("### 🧭 三版适配")
            ver_data: dict = {}
            if "深度" in mode:
                for _ver in ["辽报版", "中石油版", "企业内网版"]:
                    with st.spinner(f"{_ver} AI 量身适配中..."):
                        ver_data[_ver] = draft_checker.version_advice(
                            title or "", text or "", caption or "", _ver
                        )
            else:
                for _ver, _adv in result["versions"].items():
                    ver_data[_ver] = {"advice": _adv, "lead_example": "", "source": "heuristic"}
            for _ver, _d in ver_data.items():
                with st.expander(f"{_ver} · {_d.get('source', 'heuristic')}", expanded=False):
                    for _a in _d.get("advice", []):
                        st.markdown(f"- {_md_escape(_a)}")
                    if _d.get("lead_example"):
                        st.markdown("**改写后导语示例：**")
                        st.info(_d["lead_example"])
        elif not (title or text):
            st.info("先填标题或正文，再点体检。")

    


with tab_help:
    st.subheader("🧭 行者 · 使用说明")

    st.markdown("""
### 🎯 软件定位

**行者** 是锦州石化新闻投稿辅助工具，覆盖 **找选题 → 写稿 → 体检 → 投稿记录** 全流程。

---

### 🔐 访问密码

首次打开应用时，系统会提示设置**访问密码**（整个应用只有一个密码）。
设置后每次访问都需要输入密码登录。

- **忘记密码**：需在数据库中执行 `DELETE FROM app_setting WHERE key='access_password';` 清空，重启后重新设置。
- **本地调试跳过登录**：在 `.env` 中设置 `AUTH_ENABLED=0`。

---

### 📑 六个栏目

| 栏目 | 用途 | 何时用 |
|------|------|--------|
| ✅ 今日审核 | 浏览 AI 筛选的行业稿件，标记 保存/删除 | 每天 |
| 🗂 历史已审 | 查看标记过的稿件，展开看全文 | 写稿前查参考 |
| 📅 常规日历 | 选题提醒 + 投稿记录 + 命中率统计 | 投稿后 |
| 📚 素材对标 | 选题对标（语义检索）+ 图文素材库 | 写稿前 |
| ✍️ 撰稿中心 | 行者撰稿（AI写稿）+ 成稿体检（质量检查） | 写稿中/后 |
| ❓ 使用说明 | 本页 | 随时 |

---

### 🔄 日常工作流

```
① 今日审核 → 标记 保存/删除
     ↓
② 素材对标 → 选题对标找参考，图文素材找配图
     ↓
③ 撰稿中心 → 行者撰稿生成初稿 → 成稿体检检查质量
     ↓
④ 常规日历 → 记录投稿，跟踪命中率
```

---

### 📋 各栏目要点

#### ① 今日审核
- 浏览 AI 筛选后的石油石化行业稿件
- 🟢 保存：题材可用，保留到历史已审
- 🚫 删除：直接删除，不保留记录（不可恢复）

#### ② 历史已审
- 所有已保存的稿件都在这里
- 点击折叠条展开看全文，含原文链接

#### ③ 常规日历
- 未来 30 天选题提醒（春检、安全月、冬季保供等）
- 填写投稿记录（选题、媒体、栏目、结果）
- 按栏目统计中稿率

#### ④ 素材对标
- **选题对标**：输入关键词（如"春检"），AI 语义检索最相关的 5 篇历史稿 + 写作角度建议
- **图文素材**：浏览带图片的稿件作参考，支持上传本地图片

#### ⑤ 撰稿中心
- **行者撰稿**：输入选题关键词、写作角度、目标媒体，AI 生成新闻稿初稿
- **稿件会自动保存**：初稿生成后即存入云端草稿库；之后每次编辑也会自动存。
  刷新页面、关掉标签页、换台电脑都不丢，可在「📂 我的草稿」中点标题继续写
- **清空稿件有二次确认**：清空只影响当前编辑区，云端历史草稿仍可找回
- **成稿体检**：
  - 快速模式：检查模糊时间、空泛数据、绝对化用词
  - 深度模式：规则检查 + AI 校对
  - 自动生成三版适配建议（辽宁日报 / 中国石油报 / 企业内网）

---

### 💰 成本说明

| 功能 | 服务商 | 费用 |
|------|--------|------|
| 数据库 | Supabase | 免费（500MB） |
| AI 写稿主力 | 智谱 GLM-4.7-Flash | 免费模型，永久可用 |
| AI 写稿/校对后备 | 腾讯云 TokenHub（deepseek） | 免费额度 100 万 tokens（90 天） |
| AI 初选主力 | 智谱 GLM-4.7-Flash | 免费模型，永久可用 |
| AI 初选后备 | Silicon Flow（Qwen） | 永久免费模型 |
| 语义嵌入 | 智谱 embedding-3 | 0.5 元/百万 tokens |

> 目前**零成本运行**。智谱免费模型永久可用；腾讯云额度 90 天后需关注，到期前会报错提醒；智谱 429 限流时会自动回退后备链路，不影响使用。

---

### ⏰ 数据更新

- 爬虫每日 **07:00** 自动抓取《中国石油报》《辽宁日报》
- AI 自动过滤，只保留石化行业相关稿件

---

### ❓ 常见问题

**Q：AI 写稿/校对用的是什么模型？**
A：默认智谱 GLM-4.7-Flash（免费，永久可用）；智谱失败/429 限流时自动回退腾讯云 TokenHub 的 deepseek-v4-flash-202605（免费额度 100 万 tokens，90 天有效期）。

**Q：点「无关」后文章去哪了？**
A：直接删除，不保留记录。确认无关再点。

**Q：待审列表都是石化相关的吗？**
A：是的。AI 过滤只保留石油石化产业链相关的稿件。

**Q：AI 校对返回空怎么办？**
A：先检查智谱 API key（ZHIPU_API_KEY）是否有效；智谱 429 限流时会自动回退腾讯云，再查腾讯云 API key（TENCENTCLOUD_API_KEY）与额度。
    """)
