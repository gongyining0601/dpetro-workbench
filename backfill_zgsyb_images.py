"""回填中国石油报历史图片：原图已被源站下架，用「版面整版图 + 文章坐标」裁出来补。

背景（2026-10-07 实测）：
中国石油报把文章配图文件（res/<uuid>_middle.jpg）从服务器下架了，
历史稿件的图片链接全部返回重定向 —— 页面上是一片"图片已失效"。
但「版面整版图」(res/zgsybYYYYMMDD0X.jpg) 还活着，且每篇文章都带
coord（版面上的百分比坐标）。按坐标把文章那一块（照片+图注）裁出来，
就能把图找回来，存进 image_asset 归档表（压缩后约几十 KB/张）。

跑法：  python backfill_zgsyb_images.py
幂等：已归档过的图自动跳过，可反复跑。
"""
from __future__ import annotations

import json
import re

from urllib.parse import urljoin

import config
import crawler
import db
import image_archive

_URL_RE = re.compile(r"/zgsyb/(\d{4}-\d{2}/\d{2})/#con_(\d+)")


def _load_zgsyb_articles() -> list:
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.image_urls, a.publish_date "
            "FROM article a "
            "JOIN media_column c2 ON c2.id = a.column_id "
            "JOIN media_source s ON s.id = c2.source_id "
            "WHERE s.name=%s AND a.image_urls IS NOT NULL "
            "ORDER BY a.publish_date DESC",
            ("中国石油报",),
        )
        return cur.fetchall()


def _load_issue(date_path: str) -> dict | None:
    """抓某期的 SPA 页，返回 {contentid: (board_url, coord)} 映射。"""
    spa_url = f"http://epaper.cnpc.com.cn/zgsyb/{date_path}/"
    html = crawler.fetch(spa_url)
    if not html:
        return None
    obj = crawler.parse_zgsyb_object(html)
    if not obj:
        return None
    base = f"http://epaper.cnpc.com.cn/zgsyb/{date_path}/"
    prefix = obj.get("imgPrefix", "") or ""
    mapping: dict[str, tuple[str, str]] = {}
    for key, page in obj.items():
        if not key.startswith("page_") or not isinstance(page, dict):
            continue
        board_url = urljoin(base, prefix + (page.get("img_url") or ""))
        for a in page.get("data") or []:
            cid = str(a.get("contentid") or "")
            if cid:
                mapping[cid] = (board_url, a.get("coord") or "")
    return mapping


def main() -> int:
    if not image_archive.should_archive("中国石油报"):
        print("IMAGE_ARCHIVE_SOURCES 白名单未包含中国石油报，先在 config.py 里打开。")
        return 1
    rows = _load_zgsyb_articles()
    print(f"中国石油报稿件 {len(rows)} 篇")
    # 按期分组：同期的文章共用一次 SPA 抓取
    by_date: dict[str, list] = {}
    for r in rows:
        m = _URL_RE.search(r["url"] or "")
        if not m:
            print(f"  [跳过] URL 解析不出期号：{r['url']}")
            continue
        by_date.setdefault(m.group(1), []).append((m.group(2), r))
    total = 0
    for date_path, items in sorted(by_date.items(), reverse=True):
        mapping = _load_issue(date_path)
        if not mapping:
            print(f"  [{date_path}] 当期页面抓取/解析失败，跳过")
            continue
        print(f"  [{date_path}] {len(items)} 篇，版面文章 {len(mapping)} 篇")
        for cid, r in items:
            board_url, coord = mapping.get(cid, ("", ""))
            if not board_url or not coord:
                print(f"    [跳过] 当期页面里找不到 contentid={cid}（{r['title'][:20]}）")
                continue
            try:
                urls = json.loads(r["image_urls"]) if r["image_urls"] else []
            except (json.JSONDecodeError, TypeError):
                urls = []

            def _crop(_b=board_url, _c=coord):
                return image_archive.crop_board_region(_b, _c)

            for u in urls[:10]:
                if image_archive.archive_one(u, "中国石油报", fallback_fn=_crop):
                    total += 1
        image_archive.clear_board_cache()
    print(f"\n完成：新归档 {total} 张。可重跑本脚本验证幂等。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
