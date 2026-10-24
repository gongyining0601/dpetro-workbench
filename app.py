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

import streamlit as st

import config
import db
import crawler
import topic_matcher

st.set_page_config(page_title="行者", layout="wide")


# 数据库查询缓存层：2026-10-08 已拆出到 db_cache.py
from db_cache import (
    _get_setting_cached,
    _init_db_cached,
    _invalidate_caches,
    _invalidate_setting_cache,
)

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
# 2026-10-08 已拆出到 auth_ui.py（首次设密码 / 登录校验 / 失败限流）。
# 未通过校验时，require_auth() 内部会 st.stop()，不会走到下面的主界面。
from auth_ui import require_auth

require_auth()
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
# 2026-10-08 已拆出到 sidebar.py：库存概览 / 语义对标库 / 数据维护 /
# 图片缓存管理 / 手动爬取 / 退出登录，统一由 render_sidebar() 渲染
from sidebar import render_sidebar

render_sidebar(_chroma_stats)


# ---------------- Tabs ----------------
tab_review, tab_history, tab_calendar, tab_material, tab_writing, tab_help = st.tabs(
    ["✅ 今日审核", "🗂 历史已审", "📅 常规日历", "📚 素材对标", "✍️ 撰稿中心", "❓ 使用说明"]
)


# ----- Tab 1: 今日审核 -----
# 2026-10-08 已拆出到 tabs/tab_review.py
from tabs.excluded_panel import render_excluded_panel
from tabs.tab_review import render_review

with tab_review:
    render_review()
    # 「已排除」原是独立标签页，现收在本页底部折叠区：
    # 它是过滤规则的安全网——误杀了能从这里找回，功能必须留，
    # 但不值得单独占一个主导航位置，需要时点开即可。
    with st.expander("🚫 已排除稿件（规则拦下的，可恢复）", expanded=False):
        render_excluded_panel()

# ----- Tab 2: 历史已审 ----
# 2026-10-08 已拆出到 tabs/tab_history.py
from tabs.tab_history import render_history

with tab_history:
    render_history()

# ----- Tab 3: 常规日历 + 投稿记录 ----
# 2026-10-08 已拆出到 tabs/tab_calendar.py
from tabs.tab_calendar import render_calendar

with tab_calendar:
    render_calendar()

# ----- Tab 4: 素材对标（选题对标 + 图文素材）-----
# 2026-10-24 已拆出到 tabs/tab_material.py（上传配置常量一并搬走）
from tabs.tab_material import render_material

with tab_material:
    render_material()

# ----- Tab 5: 撰稿中心（行者撰稿 + 成稿体检）-----
# 2026-10-24 已拆出到 tabs/tab_writing.py
from tabs.tab_writing import render_writing

with tab_writing:
    render_writing()

# ----- Tab 6: 使用说明 -----
# 2026-10-24 已拆出到 tabs/tab_help.py
from tabs.tab_help import render_help

with tab_help:
    render_help()
