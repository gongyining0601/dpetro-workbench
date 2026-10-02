"""Streamlit 审核台主入口。

启动：
    streamlit run app.py

5 个 Tab：
1. 今日审核   —— 5 分钟审核新稿（相关 / 无关 / 借鉴）
2. 历史已审   —— 看过去审核结果，复盘
3. 常规日历   —— 未来两周常规选题预警 + 命中率
4. 选题对标   —— 输入关键词，对标相似已发稿 + 角度建议
5. 成稿体检   —— 草稿质量检查 + 三版适配

投稿记录功能在第 3 个 tab 同屏管理（与命中率一起看）。
"""
from __future__ import annotations

import json

import re

import streamlit as st

import config
import db
import calendar_engine
import topic_matcher
import draft_checker
import ai_writer

st.set_page_config(page_title="行者", layout="wide")


def _md_escape(s: str) -> str:
    """转义 markdown 特殊字符，防止第三方内容（标题/URL）注入。"""
    if not s:
        return ""
    return re.sub(r"([\\*_{}\[\]()#+.\!|>])", r"\\\1", str(s))


def _safe_anchor(label: str, url: str) -> str:
    """生成安全的 markdown 链接文本，仅允许 http/https 协议。"""
    safe_url = (url or "").strip()
    if not safe_url.startswith(("http://", "https://")):
        return f"{_md_escape(label)}：{_md_escape(safe_url)}"
    return f"[{_md_escape(label)}]({safe_url})"

st.title("🧭 行者")

# 启动时初始化数据库（带重试：连接池失效时用户可点击重试恢复）
_db_ok = False
for _attempt in range(2):  # 自动重试1次（连接池可能刚重建）
    try:
        db.init_db()
        _db_ok = True
        break
    except Exception as e:
        _db_err = e
        db._reset_pool()  # 连接可能失效，重建池后重试
if not _db_ok:
    st.error(f"DB 连接失败（{_db_err}）。点击下方按钮重试。")
    if st.button("🔄 重试连接", type="primary"):
        st.rerun()
    st.stop()

# 向量索引同步：用 session_state 缓存，避免每次 rerun 都调 embedding API
# 审核操作后会置 _force_vec_sync=True 强制同步；否则每 VEC_SYNC_INTERVAL 秒同步一次
import time as _time
_VEC_SYNC_INTERVAL = 60  # 秒
_force = st.session_state.get("_force_vec_sync", False)
_last = st.session_state.get("_vec_sync_time", 0)
if _force or (_time.time() - _last) > _VEC_SYNC_INTERVAL:
    try:
        _chroma_stats = topic_matcher.ensure_index_synced()
        st.session_state["_vec_sync_time"] = _time.time()
        st.session_state["_force_vec_sync"] = False
    except Exception as e:
        _chroma_stats = {"error": str(e), "fallback": True}
else:
    _chroma_stats = st.session_state.get("_vec_sync_stats", {"fallback": False, "in_index": 0})
st.session_state["_vec_sync_stats"] = _chroma_stats


# ---------------- 侧边状态 ----------------
with st.sidebar:
    st.subheader("📊 库存概览")
    s = db.stats_overview()
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
        st.caption("嵌入：BAAI/bge-large-zh-v1.5（Silicon Flow API）")
        if _chroma_stats.get("error"):
            st.caption(f"⚠️ 同步警告：{_chroma_stats['error']}")


# ---------------- Tabs ----------------
tab_review, tab_history, tab_calendar, tab_match, tab_check, tab_image, tab_writer, tab_help = st.tabs(
    ["✅ 今日审核", "🗂 历史已审", "📅 常规日历", "🎯 选题对标", "📝 成稿体检", "📷 图文素材", "✍️ 行者撰稿", "❓ 使用说明"]
)


# ----- Tab 1: 今日审核 -----
with tab_review:
    st.subheader("今日新稿（5 分钟审核法）")
    st.caption("点击「相关 / 借鉴 / 无关」即记录一次审核；只看摘要点开详情。")
    rows = db.fetch_unreviewed(limit=30)
    if not rows:
        st.info("暂无待审稿件。运行 `python crawler.py` 拉取，或确认 config.py 已填栏目 URL。")
    for r in rows:
        with st.container(border=True):
            cols = st.columns([5, 2, 2, 2])
            with cols[0]:
                st.markdown(f"**{_md_escape(r['title'])}**")
                st.caption(
                    f"{r['source_name']} · {r['column_name']} · "
                    f"{r['publish_date'] or '日期不详'} · 作者: {r['author'] or '不详'}"
                )
                with st.expander("摘要 / 详情"):
                    st.write(r["summary"] or "（无摘要）")
                    if r["body_text"]:
                        st.text_area("正文预览（前 500 字）",
                                     r["body_text"][:500], height=160,
                                     disabled=True, key=f"body_{r['id']}")
                    st.markdown(_safe_anchor("原文链接", r['url']))
            def _do_review(article_id, decision):
                """审核操作：连接断开时自动重建池并重试一次。"""
                import psycopg2 as _pg
                try:
                    db.set_review(article_id, decision)
                    return True
                except (_pg.OperationalError, _pg.InterfaceError):
                    db._reset_pool()
                    try:
                        db.set_review(article_id, decision)
                        return True
                    except Exception:
                        return False
                except Exception:
                    return False

            with cols[1]:
                if st.button("相关", key=f"rel_{r['id']}", type="primary"):
                    if _do_review(r["id"], "相关"):
                        st.session_state["_force_vec_sync"] = True
                        st.toast("已标「相关」", icon="✅")
                    else:
                        st.error("连接失败，请重试")
                    st.rerun()
            with cols[2]:
                if st.button("借鉴", key=f"bor_{r['id']}"):
                    if _do_review(r["id"], "借鉴"):
                        st.session_state["_force_vec_sync"] = True
                        st.toast("已标「借鉴」", icon="💡")
                    else:
                        st.error("连接失败，请重试")
                    st.rerun()
            with cols[3]:
                if st.button("无关", key=f"irr_{r['id']}"):
                    if _do_review(r["id"], "无关"):
                        st.session_state["_force_vec_sync"] = True
                        st.toast("已标「无关」", icon="🚫")
                    else:
                        st.error("连接失败，请重试")
                    st.rerun()


# ----- Tab 2: 历史已审 -----
with tab_history:
    st.subheader("已审稿件")
    rows = db.fetch_reviewed(limit=200)
    if not rows:
        st.info("还没有审核记录。去「今日审核」审几篇试试。")
    else:
        for r in rows:
            tag = {"相关": "🟢 相关", "借鉴": "💡 借鉴"}.get(r["decision"], r["decision"])
            with st.expander(f"{tag} | {r['title']} | {r['source_name']}/{r['column_name']}"):
                if r.get("publish_date"):
                    st.caption(f"发布日期：{r['publish_date']}  |  审核时间：{r['reviewed_at']}")
                st.markdown(_safe_anchor("原文链接", r['url']))
                if r.get("body_text"):
                    st.markdown(_md_escape(r["body_text"]))
                else:
                    st.info("（无正文内容）")


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
                st.rerun()
            except Exception as e:
                st.error(f"保存失败：{e}")


# ----- Tab 4: 选题对标 -----
with tab_match:
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
        for a in topic_matcher.angle_advice(kws):
            st.markdown(f"- {a}")


# ----- Tab 5: 成稿体检 -----
with tab_check:
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
            for i in ai_r.get("ai_issues", []):
                st.markdown(f"- {_md_escape(i)}")
        st.markdown("### 🧭 三版适配")
        for ver, advice in result["versions"].items():
            with st.expander(ver, expanded=True):
                for a in advice:
                    st.markdown(f"- {_md_escape(a)}")
    elif not (title or text):
        st.info("先填标题或正文，再点体检。")

# ----- Tab 6: 图文素材 -----
with tab_image:
    st.subheader("📷 图文素材库")
    st.caption("所有带图片的稿件（不限行业），可作图片新闻参考。")
    imgs = db.fetch_image_articles(200)
    st.metric("图文稿件", len(imgs))
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
                            try:
                                st.image(u, use_container_width=True)
                            except Exception:
                                st.markdown(f"![图片]({u})")
                except (json.JSONDecodeError, TypeError):
                    pass
            if a.get("summary"):
                st.markdown(_md_escape(a["summary"]))
            if a.get("body_text"):
                st.markdown(_md_escape(a["body_text"][:1000]))
            if a.get("url"):
                st.markdown(f"[原文链接]({_safe_anchor(a['url'])})")

# ----- Tab 7: 行者撰稿（AI辅助写稿） -----
with tab_writer:
    st.subheader("✍️ 行者撰稿")
    st.caption("AI 辅助生成新闻稿初稿（使用免费模型 GLM-4-9B-Chat）。")
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
                if r["title"]:
                    st.text_input("标题", value=r["title"])
                st.text_area("初稿", value=r["body"], height=400)
                st.success("生成完成，可复制后自行修改。")
            else:
                st.error(f"生成失败：{r['error']}")
# ----- Tab 8: 使用说明 -----
with tab_help:
    st.subheader("🧭 行者 · 使用说明")

    st.markdown("""
    ### 🚀 快速上手

    本软件帮你完成：**找选题 → 写稿 → 体检 → 投稿记录** 的全流程。

    ---

    ### 1️⃣ 今日审核（每天必用）

    浏览 AI 筛选后的**石油石化行业相关稿件**，逐篇判断：

    | 按钮 | 含义 | 操作后 |
    |------|------|--------|
    | 🟢 相关 | 题材可用，作选题参考 | 保留到「历史已审」 |
    | 💡 借鉴 | 写法可学，作范文参考 | 保留到「历史已审」 |
    | 🚫 无关 | 与石化行业无关 | **直接删除** |

    > 点开「摘要 / 详情」可看正文前 500 字。

    ---

    ### 2️⃣ 历史已审（素材库）

    - 所有标过「相关」「借鉴」的稿件都在这里
    - **点击稿件折叠条 → 展开查看全文**
    - 含原文链接，可跳转原网站

    ---

    ### 3️⃣ 常规日历（选题 + 投稿管理）

    - **选题提醒**：未来 30 天需关注的选题（春检、安全月、冬季保供等）
    - **投稿记录**：填写投稿的选题、目标媒体、栏目、结果
    - **命中率统计**：按栏目统计中稿率

    ---

    ### 4️⃣ 选题对标（写稿前找参考）

    1. 输入选题关键词（如"春检""安全""保供"）
    2. 点「开始对标」
    3. 系统推荐最相关的 5 篇历史稿 + 写作角度建议

    ---

    ### 5️⃣ 成稿体检（写完后检查）

    粘贴标题和正文，自动检查：

    | 检查项 | 说明 |
    |--------|------|
    | 模糊时间 | 「最近」「近日」→ 需改具体日期 |
    | 空泛数据 | 「大幅提升」「国内第一」→ 需补数字和来源 |
    | 图片说明 | 检测是否规范 |

    同时生成**三版适配建议**：《辽宁日报》版 / 《中国石油报》版 / 企业内网版

    ---

    ### 📋 日常工作流

    ```
    ① 今日审核 → 标记 相关/借鉴/无关
         ↓
    ② 选题对标 → 搜关键词找参考
         ↓
    ③ 成稿体检 → 查问题，按三版适配调整
         ↓
    ④ 常规日历 → 记录投稿，跟踪命中率
    ```

    ---

    ### ⏰ 数据更新

    - 爬虫**每日 07:00** 自动抓取《中国石油报》《辽宁日报》
    - AI 自动过滤，只保留石化行业相关稿件

    ---

    ### ❓ 常见问题

    **Q：云端应用打不开？**
    A：长时间不访问会休眠，打开后点「Yes, get this app back up!」唤醒，等 1-2 分钟。

    **Q：点「无关」后文章去哪了？**
    A：直接删除，不保留记录。确认无关再点。

    **Q：待审列表都是石化相关的吗？**
    A：是的。AI 过滤只保留石油石化产业链相关的稿件。
    """)
