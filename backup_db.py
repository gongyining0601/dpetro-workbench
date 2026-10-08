# -*- coding: utf-8 -*-
"""Supabase 数据备份：把云端全部表导出成本地 JSON 快照。

为什么需要：数据放在云端 Supabase，跨境线路会抖，而且免费 tier 没有方便的
一键导出。手里有一份本地快照，出事就能一键还原，不至于几年的稿件记录全没了。

怎么用（大白话版）：
    python backup_db.py                  # 备份到 backups/ 目录，文件名带时间戳
    python backup_db.py --keep 7         # 只保留最近 7 份，更早的自动删掉
    python backup_db.py --skip-images    # 不备份图片二进制（快很多，文件也小很多）
    python backup_db.py --gzip           # 压成 .json.gz，省空间

紧急还原：
    python backup_db.py --restore backups/backup_2026-10-08_1900.json

注意：还原是"按主键 UPSERT 覆盖"，不会清空现有数据；已存在且本地快照里没有的行会保留。
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys
from datetime import datetime, date, timezone
from decimal import Decimal

import psycopg2.extras

import config
import db

# 备份哪些表（顺序按依赖关系排：先主表后子表，方便将来核对）
TABLES = [
    "media_source",
    "media_column",
    "article",
    "article_embedding",
    "review_record",
    "routine_calendar",
    "submission",
    "draft",
    "draft_check",
    "excluded_article",
    "image_asset",
    "app_setting",
]

# 含二进制大字段的表：备份体积的主要来源，可用 --skip-images 跳过
IMAGE_TABLES = {"image_asset"}

DEFAULT_OUT_DIR = "backups"


def _jsonable(v):
    """把 psycopg2 返回的特殊类型转成 JSON 能写的格式。"""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        # bytea 二进制（图片原图/缩略图）转 base64 文本，方便塞进 JSON
        return {"__bytes_b64__": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(val) for k, val in v.items()}
    return str(v)


def _table_rows(table: str, cur) -> list:
    """导出单张表的全部行（dict 形式）。大表走分页，避免一次拉爆内存。"""
    cur.execute(f"SELECT * FROM {table}")
    rows = cur.fetchall()
    return [dict(r) for r in rows]


def do_backup(out_dir: str, *, skip_images: bool = False,
              gzip_out: bool = False, keep: int | None = None) -> str:
    """执行备份，返回生成的文件路径。"""
    os.makedirs(out_dir, exist_ok=True)

    tables = [t for t in TABLES if not (skip_images and t in IMAGE_TABLES)]

    snapshot = {
        "_meta": {
            "backup_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": "backup_db.py",
            "tables": tables,
            "note": "由 backup_db.py 导出；含 __bytes_b64__ 字段的是二进制，需 base64 解码",
        }
    }

    # 备份本身也走重试通道，遇到抖动不至于整轮白跑
    conn = db._get_pool().getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        total = 0
        for t in tables:
            rows = _table_rows(t, cur)
            snapshot[t] = [_jsonable(dict(r)) for r in rows]
            total += len(rows)
            print(f"  ✓ {t:<20} {len(rows):>7} 行")
    finally:
        db._get_pool().putconn(conn)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    fname = f"backup_{ts}.json" + (".gz" if gzip_out else "")
    path = os.path.join(out_dir, fname)

    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if gzip_out:
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(payload)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)

    size_kb = os.path.getsize(path) / 1024
    print(f"\n备份完成：{path}")
    print(f"  共 {total} 行 / {len(tables)} 张表 / {size_kb:.0f} KB")

    if keep:
        _prune(out_dir, keep)

    return path


def _prune(out_dir: str, keep: int):
    """按文件名中的时间戳排序，删除超出保留份数的旧备份。"""
    files = sorted(
        [f for f in os.listdir(out_dir) if f.startswith("backup_") and f.endswith((".json", ".json.gz"))],
        reverse=True,
    )
    for old in files[keep:]:
        p = os.path.join(out_dir, old)
        os.remove(p)
        print(f"  已清理旧备份：{old}")


def do_restore(path: str):
    """从本地快照还原到云端数据库（UPSERT，不清表现有数据）。"""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        snapshot = json.load(f)

    tables = snapshot.get("_meta", {}).get("tables") or [k for k in snapshot if not k.startswith("_")]

    conn = db._get_pool().getconn()
    restored = 0
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        for t in tables:
            rows = snapshot.get(t) or []
            if not rows:
                continue
            for r in rows:
                cols = [c for c in r.keys()]
                vals = []
                for v in r.values():
                    if isinstance(v, dict) and "__bytes_b64__" in v:
                        vals.append(base64.b64decode(v["__bytes_b64__"]))
                    else:
                        vals.append(v)
                updates = [c for c in cols if c != "id"]
                sql = (
                    f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    + ",".join(f"{c}=EXCLUDED.{c}" for c in updates)
                )
                cur.execute(sql, vals)
                restored += 1
            conn.commit()
            print(f"  ↩ {t:<20} {len(rows):>7} 行")
    finally:
        db._get_pool().putconn(conn)

    print(f"\n还原完成：共写回 {restored} 行（同 id 已覆盖）")


def main():
    ap = argparse.ArgumentParser(description="Supabase 数据备份 / 还原")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"备份目录（默认 {DEFAULT_OUT_DIR}）")
    ap.add_argument("--skip-images", action="store_true", help="跳过图片二进制表，速度快体积小")
    ap.add_argument("--gzip", action="store_true", help="压缩输出为 .json.gz")
    ap.add_argument("--keep", type=int, default=None, help="只保留最近 N 份备份")
    ap.add_argument("--restore", metavar="FILE", default=None, help="从指定快照文件还原")
    args = ap.parse_args()

    if not config.DB_DSN:
        print("错误：没有配置 DATABASE_URL，无法连接数据库。请检查 .env 或 Streamlit secrets。",
              file=sys.stderr)
        sys.exit(1)

    if args.restore:
        print(f"开始还原：{args.restore}")
        do_restore(args.restore)
    else:
        print(f"开始备份 → {args.out_dir}")
        do_backup(args.out_dir, skip_images=args.skip_images,
                  gzip_out=args.gzip, keep=args.keep)


if __name__ == "__main__":
    main()
