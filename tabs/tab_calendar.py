# -*- coding: utf-8 -*-
"""标签页：常规日历 + 投稿记录。

2026-10-08 从 app.py 拆出。
"""
from __future__ import annotations

import streamlit as st

from db_cache import _invalidate_caches
import calendar_engine
import config
import db

def render_calendar() -> None:
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
