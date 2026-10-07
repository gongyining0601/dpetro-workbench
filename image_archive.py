"""图片归档：抓取当时就把图下下来、压缩后存进数据库。

为什么要有这个模块（2026-10-07）：
中国石油报网站改版，历史图片 URL 全部失效——服务器返回一个 93 字节的网页脚本，
原图文件已经被源站下架，再也找不回来。页面上一片"图片暂不可用"。

对策：在爬虫抓到稿件的那一刻（图还活着），就把图下载下来压一道存进数据库。
以后页面优先读库里的归档，源站再怎么改版/删图都不受影响。

只对 config.IMAGE_ARCHIVE_SOURCES 白名单里的来源生效（默认只有中国石油报），
其他来源保持原样（直接读源站），不额外占空间、不改行为。

压缩参数：长边 ≤ IMAGE_ARCHIVE_MAX_EDGE（默认 1200px）、JPEG 质量 80。
实测 160KB 的报纸图 → 约 38KB（原体积的 27%），手机/电脑看都够清晰。
"""
from __future__ import annotations

import io
import logging

import requests

import config
import db

try:
    from PIL import Image
    from PIL import ImageOps

    _PIL_OK = True
except ImportError:  # pragma: no cover - 环境缺 Pillow 时优雅降级
    _PIL_OK = False

logger = logging.getLogger(__name__)

# 魔数校验：确保下载到的真是图片，不是"200 但给你一个网页"
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _sniff_mime(data: bytes) -> str:
    for sig, mime in _MAGIC:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def should_archive(source_name: str) -> bool:
    """该来源的图片是否需要归档（白名单 + 总开关）。"""
    return bool(getattr(config, "IMAGE_ARCHIVE_ENABLED", True)) and \
        source_name in getattr(config, "IMAGE_ARCHIVE_SOURCES", set())


def _download(url: str, timeout: int = 20) -> bytes:
    """下载原图字节。失败抛异常，由调用方兜住。"""
    headers = {"User-Agent": "Mozilla/5.0"}
    if "cnpc.com.cn" in url:
        # 中国石油报图片服务器校验 Referer，不带就给网页不给图
        from urllib.parse import urlparse

        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"
    resp = requests.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    data = resp.content
    if len(data) > config.IMAGE_ARCHIVE_MAX_BYTES:
        raise RuntimeError(f"原图过大 {len(data)}B")
    if not _sniff_mime(data):
        raise RuntimeError("下载到的不是图片（源站可能已下架）")
    return data


# ---------------- 中国石油报兜底：从版面整版图裁出文章区域 ----------------
# 实测（2026-10-07）：中国石油报把文章配图文件（res/<uuid>_middle.jpg）从服务器
# 下架了，但「版面整版图」(res/zgsybYYYYMMDD0X.jpg) 还活着，且 epaperObject 里
# 每篇文章都带 coord（版面上的百分比坐标）。文章原图 404 时，
# 就下载整版图按坐标把「文章那一块」（照片+图注）裁出来当图用。

_board_cache: dict[str, "Image.Image"] = {}   # 版面图 URL → PIL Image（本次进程内）


def crop_board_region(board_url: str, coord: str) -> bytes:
    """从版面整版图按百分比坐标裁出文章区域，返回 JPEG 字节。

    coord 形如 "x1,y1,x2,y2,..."（多边形顶点的百分比坐标；通常是 4 个角 8 个值，
    不规则版式的文章会有更多顶点，取外接矩形即可）。
    失败抛异常，由调用方兜住。
    """
    if not _PIL_OK:
        raise RuntimeError("未安装 Pillow，无法裁图")
    img = _board_cache.get(board_url)
    if img is None:
        raw = _download(board_url, timeout=45)
        img = Image.open(io.BytesIO(raw))
        img.load()
        _board_cache[board_url] = img
    vals = [float(v) for v in (coord or "").split(",") if v.strip()]
    if len(vals) < 6 or len(vals) % 2 != 0:
        raise RuntimeError(f"coord 格式不对：{coord[:40]}")
    W, H = img.size
    xs = vals[0::2]
    ys = vals[1::2]
    left, top = int(min(xs) / 100 * W), int(min(ys) / 100 * H)
    right, bottom = int(max(xs) / 100 * W), int(max(ys) / 100 * H)
    # 往里收 1 个像素，避免把邻篇文章的白边裁进来
    left, top = max(0, left + 1), max(0, top + 1)
    right, bottom = min(W, right - 1), min(H, bottom - 1)
    if right - left < 60 or bottom - top < 40:
        raise RuntimeError(f"裁剪区域过小 {right - left}x{bottom - top}")
    crop = img.crop((left, top, right, bottom))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def clear_board_cache() -> None:
    """清空版面图内存缓存（一篇报纸约 2.6MB×4 版，用完及时释放）。"""
    _board_cache.clear()


def compress(data: bytes) -> tuple[bytes, str, int, int]:
    """压缩：长边缩到 ≤ MAX_EDGE，转 JPEG q80。返回 (字节, mime, 宽, 高)。"""
    if not _PIL_OK:
        raise RuntimeError("未安装 Pillow，无法压缩")
    img = Image.open(io.BytesIO(data))
    # 手机/相机拍的图带 EXIF 旋转，不修正会出现"躺倒"的照片
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.mode in ("RGBA", "LA", "P"):
        # PNG 透明通道转 JPEG 会变黑，先铺白底
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    max_edge = int(getattr(config, "IMAGE_ARCHIVE_MAX_EDGE", 1200))
    if max(w, h) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)
        w, h = img.size
    buf = io.BytesIO()
    quality = int(getattr(config, "IMAGE_ARCHIVE_QUALITY", 80))
    img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue(), "image/jpeg", w, h


def archive_one(url: str, source_name: str, fallback_fn=None) -> bool:
    """下载 + 压缩 + 存库单张图。任何失败都返回 False，绝不抛异常打断爬虫。

    fallback_fn：原图下载失败时的兜底函数，返回图片字节（如从版面整版图裁剪）。
    """
    if not url:
        return False
    try:
        if db.get_image_asset(url) is not None:
            return False  # 已归档过，跳过（省一次下载 + 不重复占空间）
        try:
            raw = _download(url)
            how = "原图"
        except Exception as download_err:
            if fallback_fn is None:
                raise
            raw = fallback_fn()
            if not raw:
                raise download_err
            how = "版面裁图"
        packed, mime, w, h = compress(raw)
        ok = db.save_image_asset(
            orig_url=url, source_name=source_name, mime=mime,
            data=packed, width=w, height=h, orig_bytes=len(raw),
        )
        if ok:
            logger.info(
                "  归档图片[%s] %.0fKB → %.0fKB (%s)",
                how, len(raw) / 1024, len(packed) / 1024, url[:80],
            )
        return ok
    except Exception as e:
        logger.warning("图片归档失败（%s）：%s", str(e)[:60], url[:100])
        return False


def archive_urls(urls, source_name: str, fallback_fn=None) -> int:
    """批量归档。urls 可以是 list 或 JSON 字符串。返回成功张数。"""
    if not should_archive(source_name) or not urls:
        return 0
    if isinstance(urls, str):
        import json

        try:
            urls = json.loads(urls)
        except (json.JSONDecodeError, TypeError):
            return 0
    ok = 0
    for u in urls[:10]:  # 单篇最多归档 10 张，防异常页面塞几百张
        if archive_one(u, source_name, fallback_fn=fallback_fn):
            ok += 1
    if _board_cache:
        clear_board_cache()
    return ok
