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
from datetime import date, timedelta
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


def _md_escape(s: str) -> str:
    """转义 markdown 特殊字符，防止第三方内容（标题/URL）注入。"""
    if not s:
        return ""
    return re.sub(r"([\\*_{}\[\]()#+.\!|>])", r"\\\1", str(s))


def _normalize_display_url(url: str) -> str:
    """渲染前规范化 URL：ccin.com.cn 的 https 统一转 http，避免浏览器 SSL 警告。"""
    if not url:
        return url
    if url.startswith("https://") and "ccin.com.cn" in url:
        return "http://" + url[len("https://"):]
    return url


_IMG_CACHE: dict[str, str] = {}
_MAX_IMG_BYTES = 3 * 1024 * 1024  # 单图 3MB 上限，避免超大图拖慢页面
# 磁盘持久化缓存目录（Streamlit 重启后仍有效，避免重复下载）
_IMG_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".img_cache")
os.makedirs(_IMG_CACHE_DIR, exist_ok=True)
# 缓存管理配置
_CACHE_EXPIRE_DAYS = 30          # 缓存文件过期天数
_CACHE_MAX_SIZE_MB = 500         # 缓存目录最大总大小（MB），超限删除最旧文件
_CACHE_CLEANUP_INTERVAL_SEC = 3600  # 自动清理间隔（秒），避免每次请求都扫描


def _cache_stats() -> dict:
    """获取磁盘缓存统计信息。"""
    try:
        files = [f for f in os.listdir(_IMG_CACHE_DIR)
                 if os.path.isfile(os.path.join(_IMG_CACHE_DIR, f))]
        total_size = 0
        oldest_mtime = None
        for f in files:
            fp = os.path.join(_IMG_CACHE_DIR, f)
            size = os.path.getsize(fp)
            total_size += size
            mtime = os.path.getmtime(fp)
            if oldest_mtime is None or mtime < oldest_mtime:
                oldest_mtime = mtime
        return {
            "count": len(files),
            "total_mb": round(total_size / (1024 * 1024), 2),
            "oldest_days": round((time.time() - oldest_mtime) / 86400, 1) if oldest_mtime else 0,
        }
    except Exception:
        return {"count": 0, "total_mb": 0, "oldest_days": 0}


def _cleanup_expired_cache() -> int:
    """清理过期缓存文件，返回删除的文件数。"""
    now = time.time()
    expire_sec = _CACHE_EXPIRE_DAYS * 86400
    removed = 0
    try:
        for f in os.listdir(_IMG_CACHE_DIR):
            fp = os.path.join(_IMG_CACHE_DIR, f)
            if not os.path.isfile(fp):
                continue
            if now - os.path.getmtime(fp) > expire_sec:
                try:
                    os.remove(fp)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


def _enforce_cache_size_limit() -> int:
    """缓存目录超限时删除最旧的文件，返回删除的文件数。"""
    max_bytes = _CACHE_MAX_SIZE_MB * 1024 * 1024
    try:
        files = []
        for f in os.listdir(_IMG_CACHE_DIR):
            fp = os.path.join(_IMG_CACHE_DIR, f)
            if os.path.isfile(fp):
                files.append((fp, os.path.getmtime(fp), os.path.getsize(fp)))
        total = sum(s for _, _, s in files)
        if total <= max_bytes:
            return 0
        # 按修改时间从旧到新排序，删除最旧的直到达标
        files.sort(key=lambda x: x[1])
        removed = 0
        for fp, _, size in files:
            if total <= max_bytes:
                break
            try:
                os.remove(fp)
                total -= size
                removed += 1
            except Exception:
                pass
        return removed
    except Exception:
        return 0


# 模块加载时执行一次过期清理 + 大小限制
try:
    _cleanup_expired_cache()
    _enforce_cache_size_limit()
except Exception:
    pass


def _disk_cache_key(url: str) -> str:
    """根据 URL 生成磁盘缓存文件名（md5 hash）。"""
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _disk_cache_path(url: str) -> str:
    return os.path.join(_IMG_CACHE_DIR, _disk_cache_key(url))


def _load_disk_cache(url: str) -> str | None:
    """从磁盘缓存加载 base64 data URI。
    返回 None 表示缓存不存在；返回 "" 表示之前下载失败过（缓存失败结果）。
    """
    path = _disk_cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


_last_cache_cleanup = 0.0  # 上次清理时间戳，避免频繁扫描


def _save_disk_cache(url: str, data_uri: str):
    """保存 base64 data URI 到磁盘缓存，并定期检查大小限制。"""
    path = _disk_cache_path(url)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data_uri)
    except Exception:
        return
    # 定期检查缓存大小（避免每次保存都扫描）
    global _last_cache_cleanup
    now = time.time()
    if now - _last_cache_cleanup > _CACHE_CLEANUP_INTERVAL_SEC:
        _last_cache_cleanup = now
        try:
            _enforce_cache_size_limit()
        except Exception:
            pass


def _is_image_bytes(data: bytes) -> bool:
    """通过 magic bytes 判断是否为真实图片（防服务器返回 HTML 错误页）。"""
    if len(data) < 4:
        return False
    # JPEG: FF D8 FF
    if data[:3] == b"\xff\xd8\xff":
        return True
    # PNG: 89 50 4E 47
    if data[:4] == b"\x89PNG":
        return True
    # GIF: 47 49 46 38
    if data[:4] == b"GIF8":
        return True
    # WebP: 52 49 46 46 .. .. .. .. 57 45 42 50
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def _img_to_data_uri(url: str) -> str:
    """服务端下载图片转 base64 data URI。

    绕过 https 页面加载 http 图片的浏览器混合内容拦截，
    同时处理中国石油报需 Referer、中国化工报证书异常等情况。
    带内存缓存，避免重复下载。
    """
    if not url:
        return ""
    # 1. 内存缓存（最快）
    if url in _IMG_CACHE:
        return _IMG_CACHE[url]
    # 2. 磁盘缓存（重启后仍有效，避免重复下载）
    disk_val = _load_disk_cache(url)
    if disk_val is not None:
        # None=缓存不存在；""=之前下载失败过；非空=成功缓存
        _IMG_CACHE[url] = disk_val
        return disk_val
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        # 中国石油报需带 Referer 才能访问图片
        if "cnpc.com.cn" in url:
            parsed = urlparse(url)
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        # 中国化工报 https 证书异常，统一走 http 并跳过证书校验
        req_url = url
        if "ccin.com.cn" in req_url:
            req_url = req_url.replace("https://", "http://")
            verify = False
        else:
            verify = True
        resp = requests.get(req_url, headers=headers, timeout=15, verify=verify, stream=True)
        if resp.status_code != 200:
            _IMG_CACHE[url] = ""
            _save_disk_cache(url, "")
            config.logger.info("img_fail status=%s url=%s", resp.status_code, url[:120])
            return ""
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
        # 部分报纸图片服务器返回 application/octet-stream 而非 image/jpeg，
        # 需通过扩展名/magic bytes 判断真实类型
        if not content_type.startswith("image/"):
            # 尝试从 URL 扩展名推断
            ext_match = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:\.\d+)?$", req_url, re.IGNORECASE)
            ext_to_ct = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "gif": "image/gif", "webp": "image/webp",
            }
            inferred = ext_to_ct.get(ext_match.group(1).lower()) if ext_match else None
            if not inferred:
                _IMG_CACHE[url] = ""
                _save_disk_cache(url, "")
                config.logger.info("img_fail bad_ct=%s url=%s", content_type, url[:120])
                return ""
            content_type = inferred
        # 限制大小（resp.content 会自动解压 gzip，避免 raw 读取压缩字节导致 magic bytes 误判）
        content = resp.content
        if len(content) > _MAX_IMG_BYTES:
            _IMG_CACHE[url] = ""
            _save_disk_cache(url, "")
            config.logger.info("img_fail too_large=%s url=%s", len(content), url[:120])
            return ""
        # magic bytes 校验：确保下载的真的是图片（防 HTML 错误页）
        if not _is_image_bytes(content):
            _IMG_CACHE[url] = ""
            _save_disk_cache(url, "")
            config.logger.info("img_fail not_image_bytes url=%s", url[:120])
            return ""
        b64 = base64.b64encode(content).decode()
        data_uri = f"data:{content_type};base64,{b64}"
        _IMG_CACHE[url] = data_uri
        _save_disk_cache(url, data_uri)
        return data_uri
    except Exception as e:
        _IMG_CACHE[url] = ""
        _save_disk_cache(url, "")
        config.logger.info("img_fail except=%s url=%s", type(e).__name__, url[:120])
        return ""


def _render_image(url: str):
    """渲染单张远程图片：服务端下载转 base64，绕过 https 混合内容拦截。"""
    u = _normalize_display_url(url)
    data_uri = _img_to_data_uri(u)
    if data_uri:
        st.image(data_uri, width="stretch")
    else:
        st.caption("🖼️ 图片暂不可用（图源限制或已过期）")


def _preload_images(urls: list[str], max_workers: int = 8) -> None:
    """并发预加载图片到缓存（内存+磁盘）。

    在渲染稿件列表前调用，让所有图片同时下载而非串行，
    大幅减少页面首屏等待时间。已缓存的 URL 会被跳过。
    """
    need = []
    for u in urls:
        if not u:
            continue
        nu = _normalize_display_url(u)
        if nu in _IMG_CACHE:
            continue
        if _load_disk_cache(nu) is not None:
            # 磁盘有缓存（含失败标记），加载到内存即可
            _IMG_CACHE[nu] = _load_disk_cache(nu) or ""
            continue
        need.append(nu)
    if not need:
        return
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_img_to_data_uri, u): u for u in need}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass


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


@st.cache_data(ttl=10)
def _fetch_unreviewed_cached(limit=30):
    return db.fetch_unreviewed(limit=limit)


@st.cache_data(ttl=10)
def _fetch_reviewed_cached(limit=200):
    return db.fetch_reviewed(limit=limit)


@st.cache_data(ttl=10)
def _fetch_image_articles_cached(limit=200):
    return db.fetch_image_articles(limit)


def _invalidate_caches():
    """审核/删除/投稿等写操作后调用，清空所有数据缓存，确保列表立即刷新。"""
    _stats_overview_cached.clear()
    _fetch_unreviewed_cached.clear()
    _fetch_reviewed_cached.clear()
    _fetch_image_articles_cached.clear()


st.title("🧭 行者")

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
    st.error(f"DB 初始化失败（{type(_db_err).__name__}: {_db_err}）。点击下方按钮重试。")
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
        _last_crawl = db.get_setting("last_crawl_date")
    except Exception:
        _last_crawl = None
    if _last_crawl != _today_str:
        def _bg_crawl():
            try:
                crawler.crawl_all()
                db.set_setting("last_crawl_date", _today_str)
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
                _invalidate_caches()
                st.success(f"爬取完成：新增 {stats.get('added', 0)} 条，"
                           f"跳过 {stats.get('skipped', 0)} 条，"
                           f"无图过滤 {stats.get('skipped_no_image', 0)} 条")
            except Exception as e:
                st.error(f"爬取失败：{e}")
    st.caption("补爬多日：选择起止日期，重复稿件自动跳过")
    last_crawl = db.get_setting("last_crawl_date")
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
tab_review, tab_history, tab_calendar, tab_material, tab_writing, tab_help = st.tabs(
    ["✅ 今日审核", "🗂 历史已审", "📅 常规日历", "📚 素材对标", "✍️ 撰稿中心", "❓ 使用说明"]
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


# ----- Tab 3: 常规日历 + 投稿记录 -----
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
with tab_writing:
    sub_writer, sub_check = st.tabs(["✍️ 行者撰稿", "📝 成稿体检"])
    with sub_writer:
        st.subheader("✍️ 行者撰稿")
        st.caption("AI 辅助生成新闻稿初稿（智谱 GLM-4.7-Flash，失败回退腾讯云 deepseek）。")
        with st.form("writer_form"):
            topic = st.text_input("选题关键词*", placeholder="如：春检、安全月、冬季保供")
            col1, col2 = st.columns(2)
            with col1:
                angle = st.text_input("写作角度（可选）", placeholder="如：人物故事、数据对比")
                target = st.selectbox("目标媒体", ["中国石油报", "辽宁日报", "企业内网"])
            with col2:
                word_count = st.slider("目标字数", 300, 2000, 800, 100)
            facts = st.text_area("已知事实/数据（可选）", placeholder="如：处理量同比+15%，创历史新高")
            submitted = st.form_submit_button("生成初稿", type="primary")
        if submitted:
            if not topic.strip():
                st.warning("请输入选题关键词")
            else:
                with st.spinner("AI 正在撰写..."):
                    r = ai_writer.write_article(topic, angle, word_count, target, facts)
                if r["ok"]:
                    st.session_state["draft_title"] = r["title"] or ""
                    st.session_state["draft_body"] = r["body"]
                    st.toast("初稿已生成，可在下方继续修改", icon="✍️")
                else:
                    st.error(f"生成失败：{r['error']}")

        # 初稿展示 + AI 修改（在表单外，支持反复修改）
        if st.session_state.get("draft_body"):
            st.divider()
            st.markdown("#### 📄 当前稿件")
            cur_title = st.text_input("标题", value=st.session_state["draft_title"], key="draft_title_in")
            cur_body = st.text_area("正文", value=st.session_state["draft_body"], height=400, key="draft_body_in")
            # 同步编辑后的值回 session_state，供 AI 修改读取
            st.session_state["draft_title"] = cur_title
            st.session_state["draft_body"] = cur_body

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
                            st.session_state["draft_title"] = rr["title"]
                            st.session_state["draft_body"] = rr["body"]
                            st.toast("修改完成", icon="✅")
                            st.rerun()
                        else:
                            st.error(f"修改失败：{rr['error']}")
            with rc2:
                if st.button("🗑 清空稿件"):
                    st.session_state.pop("draft_title", None)
                    st.session_state.pop("draft_body", None)
                    st.rerun()
            st.caption("提示：可直接在正文框手动编辑，再提修改要求让 AI 改；修改会覆盖当前稿件。")
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
