"""常规选题日历：基于时间规则 + 历史用稿，提前预警未来两周的常规选题。

MVP 实现：用 routine_calendar 表里的时间窗 + lead_days，
计算「今天起算未来 14 天」内的常规选题，并对照 submission 表的历史
中稿率给出推荐版面与命中率提示。

2026-09-29 改造（方案 A 上云版）：
- c.execute(...) 改为 db.conn_cursor(c).execute(...)（psycopg2 连接无 dict-like execute 快捷方式）
"""
from __future__ import annotations

from datetime import date, timedelta

import db


def _to_date(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def _in_window(today: date, start_m: int, start_d: int,
               end_m: int, end_d: int) -> bool:
    """判断 today 是否落在（跨年也支持）时间窗内。"""
    y = today.year
    start = _to_date(y, start_m, start_d)
    end = _to_date(y, end_m, end_d)
    if end < start:  # 跨年，例如冬季保供 10/15-3/15
        return today >= start or today <= end
    return start <= today <= end


def upcoming_topics(today: date | None = None, horizon_days: int = 14) -> list[dict]:
    """返回未来 horizon_days 天内将进入时间窗的选题。

    触发逻辑：今天 + lead_days 落在选题时间窗内，或今天在时间窗内。
    """
    today = today or date.today()
    horizon = today + timedelta(days=horizon_days)
    out: list[dict] = []
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        rows = cur.execute(
            "SELECT topic_name, start_month, start_day, end_month, end_day,"
            " recommended_column, lead_days, note FROM routine_calendar"
        ).fetchall()
        for r in rows:
            try:
                # 用 horizon 日期试一下是否在窗内（按 lead_days 提前感知）
                trigger_day = today + timedelta(days=max(0, r["lead_days"] or 0))
                in_window_soon = _in_window(trigger_day, r["start_month"],
                                             r["start_day"],
                                             r["end_month"], r["end_day"])
                already_in = _in_window(today, r["start_month"], r["start_day"],
                                        r["end_month"], r["end_day"])
                if in_window_soon or already_in:
                    out.append({
                        "topic": r["topic_name"],
                        "recommended_column": r["recommended_column"],
                        "lead_days": r["lead_days"],
                        "note": r["note"],
                        "status": "进行中" if already_in else "临近",
                    })
            except ValueError:
                continue
    return out


def hit_rate_by_column() -> list[dict]:
    """从 submission 表统计各目标版面的命中率（用稿规律学习）。"""
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        rows = cur.execute(
            "SELECT target_column, "
            "COUNT(*) AS total, "
            "SUM(CASE WHEN result='录用' THEN 1 ELSE 0 END) AS hits "
            "FROM submission WHERE target_column IS NOT NULL "
            "GROUP BY target_column ORDER BY total DESC"
        ).fetchall()
    return [{
        "版面": r["target_column"],
        "投稿": r["total"],
        "录用": r["hits"] or 0,
        "命中率": f"{(r['hits'] or 0)/r['total']*100:.0f}%" if r["total"] else "—",
    } for r in rows]


if __name__ == "__main__":
    for t in upcoming_topics():
        print(t)
