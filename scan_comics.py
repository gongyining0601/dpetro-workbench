"""扫描库内全部稿件图片，找出"疑似漫画"的，供人工核对阈值。

用法：  python scan_comics.py            # 扫描并打印嫌疑名单（不改动数据）
        python scan_comics.py --apply    # 把确认是漫画的登记到已排除列表并删稿

这是阈值校准工具：新阈值上线前先跑一遍，看有没有把真照片误判成漫画。
"""
from __future__ import annotations

import json
import sys

import image_archive
import db
import image_guard


def _iter_images():
    sql = """
        SELECT a.id, a.title, a.url, a.image_urls, a.publish_date,
               c.name AS column_name, s.name AS source_name
        FROM article a
        LEFT JOIN media_column c ON c.id = a.column_id
        LEFT JOIN media_source s ON s.id = c.source_id
        WHERE a.has_image = TRUE
        ORDER BY a.crawled_at DESC LIMIT 500
    """
    import psycopg2 as _pg

    for attempt in (1, 2, 3):
        try:
            with db.get_conn() as c:
                cur = db.conn_cursor(c)
                cur.execute(sql)
                return cur.fetchall()
        except (_pg.OperationalError, _pg.InterfaceError):
            db._reset_pool()
            if attempt == 3:
                raise
    return []


def main() -> int:
    rows = _iter_images()
    print(f"库内有图稿件 {len(rows)} 篇，逐张下载判别（较慢，仅用于校准）…\n")
    hits = []
    seen_urls: set[str] = set()
    for r in rows:
        try:
            urls = json.loads(r["image_urls"]) if r["image_urls"] else []
        except (json.JSONDecodeError, TypeError):
            urls = []
        for u in urls[:3]:
            if u in seen_urls:
                continue
            seen_urls.add(u)
            try:
                raw = image_archive._download(u, timeout=15)
            except Exception:
                continue
            is_comic, why = image_guard.looks_like_comic(raw)
            if is_comic:
                hits.append((r, u, why))
                print(f"🚨 [{r['source_name']}/{r['column_name']}] {r['title'][:36]}")
                print(f"     {why}")
                print(f"     {u[:90]}")
    print(f"\n扫描完成：{len(seen_urls)} 张图，疑似漫画 {len(hits)} 张。")
    if hits and "--apply" in sys.argv:
        for r, u, why in hits:
            db.save_excluded(
                url=r["url"], title=r["title"], source_name=r["source_name"],
                column_name=r["column_name"], reason_code="comic", reason=why,
                publish_date=r["publish_date"], body_snippet="",
                image_urls=r["image_urls"],
            )
        print(f"已把 {len(hits)} 条登记进「已排除」列表（原稿未删，可去页面复核后处理）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
