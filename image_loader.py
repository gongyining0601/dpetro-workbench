# -*- coding: utf-8 -*-
"""图片加载与缓存：把远程图片变成浏览器能显示的 base64 数据流。

2026-10-08 从 app.py（2010 行）中拆出，是「拆分巨型 app.py」的第一步。

为什么要拆：原来这一坨 363 行的图片处理逻辑混在 UI 主流程里，
改一行要翻半天，也不好单独测试。独立成模块之后，这里只管「怎么把图弄到面前」，
页面怎么排是 app.py 的事。

对外提供三类能力：
1. 单图转换：`_img_to_data_uri(url)` → base64 data URI，拿不到时返回空串
2. 失败归因：`_IMG_FAIL_REASON[url]` 记录失败人话原因（报社下架/防盗链/网络/过大）
3. 并发预加载：`_preload_images(urls)` → 渲染列表前一次性并发把图抓齐，首屏不等串行下载

缓存是两级：内存 dict（`_IMG_CACHE`）+ 磁盘目录（`.img_cache/`），
磁盘层保证 Streamlit 重启后不用重新下载，并有过期天数与总大小上限自动清理。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import streamlit as st

import config
import db

def _normalize_display_url(url: str) -> str:
    """渲染前规范化 URL：ccin.com.cn 的 https 统一转 http，避免浏览器 SSL 警告。"""
    if not url:
        return url
    if url.startswith("https://") and "ccin.com.cn" in url:
        return "http://" + url[len("https://"):]
    return url


_IMG_CACHE: dict[str, str] = {}
# 记录每张图失败的原因，用于给使用者看人话提示（而不是一句笼统的"暂不可用"）
_IMG_FAIL_REASON: dict[str, str] = {}
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

    返回值约定：
    - "data:..." 开头 = 成功缓存，直接用
    - "" = 最近刚失败过（24 小时内，别再浪费时间重打）
    - None = 无缓存 / 失败已超过 24 小时（应该重试）
    """
    path = _disk_cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None
    if content.startswith("data:"):
        return content  # 成功缓存
    if content.startswith("{"):
        # 新版失败标记：{"fail": <时间戳>}，24 小时后自动重试
        try:
            ts = float(json.loads(content).get("fail", 0))
            if (time.time() - ts) < _FAIL_RETRY_SECONDS:
                return ""
        except Exception:
            pass
        return None
    # 兼容旧版：空字符串文件是失败标记但没有时间戳，视为已过期，重试一次
    return None


# 图片下载失败后隔多久重试一次（此前失败也缓存 30 天，报社临时抽风会让图"死"一个月）
_FAIL_RETRY_SECONDS = 24 * 3600


_last_cache_cleanup = 0.0  # 上次清理时间戳，避免频繁扫描


def _save_disk_cache(url: str, data_uri: str):
    """保存 base64 data URI 到磁盘缓存，并定期检查大小限制。

    data_uri 为空 = 失败标记，写成带时间戳的 JSON（24 小时后自动重试），
    避免报社网站临时抽风导致图片被"判死"30 天。
    """
    path = _disk_cache_path(url)
    try:
        with open(path, "w", encoding="utf-8") as f:
            if data_uri:
                f.write(data_uri)
            else:
                f.write(json.dumps({"fail": time.time()}))
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


def _archived_data_uri(url: str) -> str:
    """从数据库归档里取图（抓取当时就压缩存下来的），取不到返回空串。

    这是「中国石油报图片全失效」的根治办法：图已经在自己库里，
    报社网站改版、把原图下架，这边照旧能显示。
    """
    if not getattr(config, "IMAGE_ARCHIVE_ENABLED", True):
        return ""
    try:
        row = db.get_image_asset(url)
    except Exception:
        return ""     # 归档表还没建/查不动，静默回落到源站下载
    if not row or not row.get("data"):
        return ""
    try:
        raw = row["data"]
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        b64 = base64.b64encode(bytes(raw)).decode()
        return f"data:{row.get('mime') or 'image/jpeg'};base64,{b64}"
    except Exception:
        return ""


def _img_to_data_uri(url: str) -> str:
    """服务端下载图片转 base64 data URI。

    绕过 https 页面加载 http 图片的浏览器混合内容拦截，
    同时处理中国石油报需 Referer、中国化工报证书异常等情况。
    带内存缓存，避免重复下载。

    取图顺序：内存缓存 → 数据库归档 → 磁盘缓存 → 源站下载。
    归档要排在磁盘缓存前面：磁盘里可能存着"以前下载失败"的空标记，
    而现在库里有归档图，应该优先用归档的。
    """
    if not url:
        return ""
    # 1. 内存缓存（最快）
    if url in _IMG_CACHE:
        return _IMG_CACHE[url]
    # 2. 数据库归档（源站删图也不受影响）
    _archived = _archived_data_uri(url)
    if _archived:
        _IMG_CACHE[url] = _archived
        return _archived
    # 3. 磁盘缓存（重启后仍有效，避免重复下载）
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
            _IMG_FAIL_REASON[url] = "removed" if resp.status_code in (404, 410) else "blocked"
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
                _IMG_FAIL_REASON[url] = "removed"  # 200 但返回的是网页 = 原图已被源站下架
                _save_disk_cache(url, "")
                config.logger.info("img_fail bad_ct=%s url=%s", content_type, url[:120])
                return ""
            content_type = inferred
        # 限制大小（resp.content 会自动解压 gzip，避免 raw 读取压缩字节导致 magic bytes 误判）
        content = resp.content
        if len(content) > _MAX_IMG_BYTES:
            _IMG_CACHE[url] = ""
            _IMG_FAIL_REASON[url] = "toolarge"
            _save_disk_cache(url, "")
            config.logger.info("img_fail too_large=%s url=%s", len(content), url[:120])
            return ""
        # magic bytes 校验：确保下载的真的是图片（防 HTML 错误页）
        if not _is_image_bytes(content):
            _IMG_CACHE[url] = ""
            _IMG_FAIL_REASON[url] = "removed"
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
        _IMG_FAIL_REASON[url] = "unreachable"
        _save_disk_cache(url, "")
        config.logger.info("img_fail except=%s url=%s", type(e).__name__, url[:120])
        return ""


def _render_image(url: str):
    """渲染单张远程图片：服务端下载转 base64，绕过 https 混合内容拦截。

    2026-10-08 修正显示方式（解决"中国石油报图片糊/变形"）：
    原来用 st.image(width="stretch")，会把每张图硬拉到容器宽度（约 650px）。
    低分辨率的图被放大后惨不忍睹——实测中国石油报的版面裁图最小只有 248×109，
    拉满容器等于放大 2.6 倍，看着就像"压缩过度"。
    现在改成按原始尺寸居中显示，只有宽过容器时才等比缩小：
    小图不放大所以清晰，大图不溢出，宽高比全程保持，不存在变形。
    """
    u = _normalize_display_url(url)
    data_uri = _img_to_data_uri(u)
    if data_uri:
        # height:auto          等比缩放，宽高比绝不失真
        # max-width:100%       大图收进容器内，不撑破布局
        # display+margin       宽度不足容器的小图居中摆放，不贴左边
        st.markdown(
            f'<img src="{data_uri}" alt="" '
            f'style="max-width:100%;height:auto;display:block;margin:0 auto;" />',
            unsafe_allow_html=True,
        )
        return
    reason = _IMG_FAIL_REASON.get(u, "")
    _REASON_TEXT = {
        # 200 但拿回来的是网页：报社网站改版/换系统，原图文件已被下架
        "removed": "🖼️ 图片已失效 —— 报社网站改版，原图已从源站下架，无法找回",
        "blocked": "🖼️ 图片被图源限制访问（防盗链），暂无法显示",
        "unreachable": "🖼️ 网络原因暂时取不到图，系统会在 24 小时内自动重试",
        "toolarge": "🖼️ 图片过大，无法显示",
    }
    st.caption(_REASON_TEXT.get(reason, "🖼️ 图片暂不可用（图源限制或已过期）"))


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
        # 优先查库内归档：中国石油报原图已被源站下架，磁盘里只有"失败标记"，
        # 若直接加载失败标记，归档图就永远没机会出场了
        _archived = _archived_data_uri(nu)
        if _archived:
            _IMG_CACHE[nu] = _archived
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
