"""历史重放：用「新规则」把库里现存的老稿件重跑一遍，看会拦掉哪些。

用途（2026-10-07）：
题材黑名单 + 人民日报视觉版不再整版直通，这两条规则是新加的。
上线前先拿库里的真实稿件"彩排"一遍：
  - 有多少条会被新规则拦掉（= 以前漏进来的漏网之鱼）
  - 拦掉的理由是什么（旅游/体育/漫画… 还是误杀的工业稿）
看完再决定要不要批量清理，避免规则一上就把好稿子误删了。

用法：  python replay_filter.py            # 只看统计与前 40 条
        python replay_filter.py --all     # 列出全部
        python replay_filter.py --apply   # 真删（会先打印清单，需二次确认）
"""
from __future__ import annotations

import sys

import config
import crawler
import db

# 重放时不真的调 AI（省时间省钱）：AI 兜底一律按"放行"算，
# 这样统计出来的"会被拦掉"是保守下限——只拦规则能明确判定的。
_AI_SKIP = "--ai" not in sys.argv


def _load_articles(limit: int = 2000):
    sql = """
        SELECT a.id, a.title, a.url, a.body_text, a.publish_date, a.image_urls,
               c.name AS column_name, s.name AS source_name
        FROM article a
        LEFT JOIN media_column c ON c.id = a.column_id
        LEFT JOIN media_source s ON s.id = c.source_id
        ORDER BY a.crawled_at DESC LIMIT %s
    """
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute(sql, (limit,))
        return cur.fetchall()


def _judge(title: str, body_text: str, source_name: str, column_name: str) -> tuple[str, str]:
    """对新规则下的单篇稿件下判定。返回 (结论, 说明)。

    结论：黑名单排除 / 关键词保留 / 栏目直通保留 / 无从判定(需AI)
    """
    hit = crawler.match_topic_blacklist(title, body_text or "")
    if hit:
        return "黑名单排除", f"{hit[0]}题材（命中「{hit[1]}」）"
    sc = f"{source_name}/{column_name}"
    blob = f"{title} {(body_text or '')[:100]}"
    if sc not in getattr(config, "PHOTO_COLUMN_KEYWORDS", {}):
        for cat, sc_set in config.CATEGORY_MAP.items():
            if sc in sc_set:
                return "栏目直通保留", cat
    for cat, kws in getattr(config, "PHOTO_COLUMN_KEYWORDS", {}).get(sc, {}).items():
        for kw in kws:
            if kw in blob:
                return "图片版关键词保留", f"{cat}（命中「{kw}」）"
    for cat, kws in config.CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in blob:
                return "关键词保留", f"{cat}（命中「{kw}」）"
    return "无从判定（需AI）", ""


def main() -> int:
    rows = _load_articles()
    if not rows:
        print("库里没有稿件，先跑 python crawler.py 抓一批。")
        return 0
    tally: dict[str, int] = {}
    excluded = []
    for r in rows:
        verdict, why = _judge(r["title"] or "", r["body_text"] or "",
                              r["source_name"] or "", r["column_name"] or "")
        tally[verdict] = tally.get(verdict, 0) + 1
        if verdict == "黑名单排除":
            excluded.append((r, why))

    print(f"\n库内稿件共 {len(rows)} 条 —— 新规则重放结果：")
    for k in ("黑名单排除", "无从判定（需AI）", "图片版关键词保留", "关键词保留", "栏目直通保留"):
        if k in tally:
            print(f"  {k:<16} {tally[k]:>4} 条")
    print(f"\n其中「黑名单排除」= 以前漏进来的：{len(excluded)} 条\n")

    show_all = "--all" in sys.argv
    n = len(excluded) if show_all else min(40, len(excluded))
    for r, why in excluded[:n]:
        print(f"  [{r['source_name']}/{r['column_name']}] {r['title'][:40]}")
        print(f"      → {why}   ({r['publish_date'] or '无日期'})")
    if len(excluded) > n:
        print(f"  …… 还有 {len(excluded) - n} 条，加 --all 看全部")

    if "--apply" in sys.argv and excluded:
        print("\n⚠️ 即将从 article 表删除以上稿件（不可恢复）。")
        ans = input("确认请输入 yes：").strip().lower()
        if ans != "yes":
            print("已取消。")
            return 0
        with db.get_conn() as c:
            cur = db.conn_cursor(c)
            for r, _why in excluded:
                cur.execute("DELETE FROM article WHERE id=%s", (r["id"],))
        print(f"已删除 {len(excluded)} 条。")
    elif excluded:
        print("\n（只统计不改动。确认无误后加 --apply 才会真的删。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
