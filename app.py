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


# 文本小工具（北京时间显示 / Markdown 转义 / 安全链接）：
# 2026-10-08 已拆出到 text_utils.py，多个标签页共用，这里保持再导出。
from text_utils import _bj_time, _md_escape, _safe_anchor


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




# 数据库查询缓存层：2026-10-08 已拆出到 db_cache.py，
# 下面保持符号再导出，调用点不用改。
from db_cache import (
    _fetch_image_articles_cached,
    _fetch_reviewed_cached,
    _fetch_unreviewed_cached,
    _get_setting_cached,
    _init_db_cached,
    _invalidate_caches,
    _invalidate_draft_cache,
    _invalidate_setting_cache,
    _list_drafts_cached,
    _list_excluded_cached,
    _stats_overview_cached,
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


# ---------------- 图文素材上传配置 ----------------
_UPLOAD_MAX_MB = 10      # 单张上传大小上限
_UPLOAD_MAX_DIM = 1920  # 超过此边长自动缩放
_UPLOAD_PAGE_SIZE = 12  # 每页展示数量


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

# ----- Tab 4: 常规日历 + 投稿记录 ----
# 2026-10-08 已拆出到 tabs/tab_calendar.py
from tabs.tab_calendar import render_calendar

with tab_calendar:
    render_calendar()

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
# 2026-10-24 已拆出到 tabs/tab_writing.py
from tabs.tab_writing import render_writing

with tab_writing:
    render_writing()

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
