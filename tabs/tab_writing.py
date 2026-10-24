# -*- coding: utf-8 -*-
"""标签页：撰稿中心（行者撰稿 + 成稿体检）。

2026-10-24 从 app.py 拆出（434 行）。这是整个应用体量最大、交互最密的一页：
- 行者撰稿：AI 生成初稿、云端草稿箱（改名字/删除/载入）、自动保存
- 成稿体检：写完后检查字数、敏感表述等

两个专用辅助函数一并搬来：
- _set_draft_content  带 key 的输入框必须靠这个统一改，只改 session_state 会显示旧值
- _draft_save_status  草稿保存状态文案
"""
from __future__ import annotations

import streamlit as st

import ai_writer
import db
import draft_checker
from db_cache import _invalidate_draft_cache, _list_drafts_cached
from text_utils import _bj_time, _md_escape


def _set_draft_content(title: str, body: str) -> None:
    """统一入口：改写当前稿件内容时，必须连同"带 key 的输入框"一起更新。

    这是 Streamlit 最容易中招的坑：控件一旦带了 key，就会记住自己上一次的值，
    之后 `value=` 参数不再生效。只改 session_state 里的源变量（draft_title/body）
    没用——界面继续显示旧内容，下一轮还会把旧内容反向写回 session_state。
    典型症状：点开另一条草稿却还是上一篇、改过的名字被改回去。
    """
    st.session_state["draft_title"] = title
    st.session_state["draft_body"] = body
    st.session_state["draft_title_in"] = title
    st.session_state["draft_body_in"] = body


def _draft_save_status(dirty: bool) -> str:
    """草稿保存状态文案。dirty=True 表示内容与库里不一致（有未入库的改动）。"""
    if dirty:
        return "⚠️ 有未保存的修改（点「保存草稿」立即入库）"
    saved_at = st.session_state.get("draft_saved_at")
    if saved_at:
        return f"✅ 已保存 · {_bj_time(saved_at)}（北京时间）· 存于云端草稿箱"
    return "尚未保存"

def render_writing() -> None:
    sub_writer, sub_check = st.tabs(["✍️ 行者撰稿", "📝 成稿体检"])
    with sub_writer:
        st.subheader("✍️ 行者撰稿")
        st.caption("AI 辅助生成新闻稿初稿（智谱 GLM-4.7-Flash，失败回退腾讯云 deepseek）。"
                   "稿件存入云端草稿库，换电脑、关页面都能找回。")

        # 跨 rerun 的一次性提示（保存成功等）在这里落地，避免刷新后提示消失
        _flash = st.session_state.pop("_flash_msg", None)
        if _flash:
            st.success(_flash)

        # ---------- 我的草稿（云端持久化，避免刷新/关闭即丢失） ----------
        _pending_del = st.session_state.get("_pending_del_draft")
        with st.expander("📂 我的草稿（点名字打开 · ✏️改名 · 🗑删除）", expanded=False):
            # 容错：草稿库读取失败不应连带整页报错（旧容器/网络抖动时曾出现）
            # 走缓存：每次点击都回源查库太亏；增删改后会主动清缓存，立刻可见。
            try:
                _drafts = _list_drafts_cached(30)
                _draft_err = None
            except Exception as _e:  # noqa: BLE001
                _drafts, _draft_err = None, str(_e)[:200]
            if _draft_err:
                st.warning("⚠️ 草稿库暂时读不到（不影响其它功能）。稍后刷新重试即可。")
                st.caption(f"技术细节：{_draft_err}")
            elif not _drafts:
                st.caption("暂无草稿。生成或修改稿件后会自动保存到这里。")
            else:
                # ---------- 改名（草稿列表里直接改名字，不必先载入） ----------
                _renaming = st.session_state.get("_renaming_draft")
                _rtarget = next((x for x in _drafts if x["id"] == _renaming), None) if _renaming else None
                if _renaming and _rtarget is None:
                    st.session_state.pop("_renaming_draft", None)
                if _rtarget is not None:
                    with st.container(border=True):
                        st.markdown("**✏️ 修改草稿名字**")
                        _new_name = st.text_input(
                            "新名字", value=_rtarget["title"] or "",
                            key="draft_rename_in", max_chars=60,
                            placeholder="给它起个好找的名字，如：冬供稿_辽报版_v3",
                        )
                        _rn1, _rn2 = st.columns(2)
                        with _rn1:
                            if st.button("✅ 保存名字", key="btn_rename_ok", type="primary"):
                                _nn = (_new_name or "").strip()
                                if not _nn:
                                    st.warning("名字不能为空")
                                else:
                                    # 只改名字，正文/选题等原样带回，避免误伤内容
                                    _rid = db.save_draft(
                                        title=_nn,
                                        body=_rtarget["body"] or "",
                                        topic=_rtarget["topic"] or "",
                                        angle=_rtarget["angle"] or "",
                                        target_media=_rtarget["target_media"] or "",
                                        draft_id=_rtarget["id"],
                                    )
                                    if _rid:
                                        _invalidate_draft_cache()
                                        # 改的若是正在编辑那条，连显示、输入框和比对基准一起同步，
                                        # 否则编辑框会拿旧名字把它盖回去
                                        if st.session_state.get("current_draft_id") == _rtarget["id"]:
                                            st.session_state["draft_title"] = _nn
                                            st.session_state["draft_title_in"] = _nn
                                            st.session_state["_saved_title"] = _nn
                                            st.session_state["draft_saved_at"] = db.now_iso()
                                        st.session_state["_flash_msg"] = f"✅ 草稿名字已改为「{_nn[:24]}」。"
                                        st.session_state.pop("_renaming_draft", None)
                                        st.rerun()
                                    else:
                                        st.error("改名失败，请稍后重试（稿件内容未动）。")
                        with _rn2:
                            if st.button("取消", key="btn_rename_cancel"):
                                st.session_state.pop("_renaming_draft", None)
                                st.rerun()

                for _d in _drafts:
                    _is_cur = st.session_state.get("current_draft_id") == _d["id"]
                    _c1, _c2, _c3 = st.columns([6.2, 0.9, 0.9])
                    with _c1:
                        _label = ("▶ " if _is_cur else "") + (_d["title"] or "（无标题）")[:32]
                        if st.button(_label, key=f"draft_load_{_d['id']}", width="stretch"):
                            # 关键：连 widget key 一起刷新，否则编辑框会停在上一篇
                            _set_draft_content(_d["title"] or "", _d["body"] or "")
                            st.session_state["draft_topic"] = _d["topic"] or ""
                            st.session_state["draft_angle"] = _d["angle"] or ""
                            st.session_state["draft_target"] = _d["target_media"] or ""
                            st.session_state["current_draft_id"] = _d["id"]
                            st.session_state["_saved_title"] = _d["title"] or ""
                            st.session_state["_saved_body"] = _d["body"] or ""
                            # 载入的草稿本来就是库里已有的，带上入库时间，
                            # 否则状态栏会误显示"尚未保存"（曾把使用者绕晕）
                            st.session_state["draft_saved_at"] = _d["updated_at"]
                            st.session_state["_flash_msg"] = (
                                f"已载入草稿「{(_d['title'] or '无标题')[:24]}」，"
                                "可直接编辑或让 AI 修改。"
                            )
                            st.rerun()
                        st.caption(
                            f"{_bj_time(_d['updated_at'])} · "
                            f"{_d['target_media'] or '未指定媒体'}"
                            + ("　·　当前打开" if _is_cur else "")
                        )
                    with _c2:
                        if st.button("✏️", key=f"draft_rename_{_d['id']}", help="修改这个草稿的名字"):
                            st.session_state["_renaming_draft"] = _d["id"]
                            st.rerun()
                    with _c3:
                        if st.button("🗑", key=f"draft_del_{_d['id']}", help="删除这个草稿"):
                            st.session_state["_pending_del_draft"] = _d["id"]
                            st.rerun()
                if _pending_del:
                    st.warning(f"⚠️ 确认删除草稿 #{_pending_del}？删除后不可恢复。")
                    _dc1, _dc2 = st.columns(2)
                    with _dc1:
                        if st.button("✅ 确认删除", key="btn_draft_del_ok", type="primary"):
                            try:
                                _del_ok = bool(db.delete_draft(_pending_del))
                                _del_err = None
                            except Exception as _e:  # noqa: BLE001
                                _del_ok, _del_err = False, str(_e)[:150]
                            if _del_ok:
                                _invalidate_draft_cache()
                                st.session_state.pop("_pending_del_draft", None)
                                if st.session_state.get("current_draft_id") == _pending_del:
                                    st.session_state["current_draft_id"] = None
                                st.rerun()
                            else:
                                st.error(f"删除失败（草稿仍在）：{_del_err or '未找到该草稿'}")
                                st.session_state.pop("_pending_del_draft", None)
                    with _dc2:
                        if st.button("取消", key="btn_draft_del_cancel"):
                            st.session_state.pop("_pending_del_draft", None)
                            st.rerun()

        with st.form("writer_form"):
            topic = st.text_input("选题关键词*", placeholder="如：春检、安全月、冬季保供")
            col1, col2 = st.columns(2)
            with col1:
                angle = st.text_input("写作角度（可选）", placeholder="如：人物故事、数据对比")
                target = st.selectbox("目标媒体", ["中国石油报", "辽宁日报", "企业内网"])
            with col2:
                # 2026-10-07 调整：短消息稿也要能写（最低 100 字），
                # 上限收到 1000（原来 2000 太长，报社用稿多在 300~800 字）。
                # 想改上限就动这里的 1000 一个数字。
                word_count = st.slider("目标字数", 100, 1000, 800, 50)
            facts = st.text_area("已知事实/数据（可选）", placeholder="如：处理量同比+15%，创历史新高")
            submitted = st.form_submit_button("生成初稿", type="primary")
        if submitted:
            if not topic.strip():
                st.warning("请输入选题关键词")
            else:
                with st.spinner("AI 正在撰写..."):
                    r = ai_writer.write_article(topic, angle, word_count, target, facts)
                if r["ok"]:
                    _set_draft_content(r["title"] or "", r["body"])
                    st.session_state["draft_topic"] = topic
                    st.session_state["draft_angle"] = angle
                    st.session_state["draft_target"] = target
                    # 生成即入库：避免还没来得及点保存就丢了
                    _new_id = db.save_draft(
                        title=r["title"] or "", body=r["body"],
                        topic=topic, angle=angle, target_media=target,
                    )
                    if _new_id:
                        _invalidate_draft_cache()
                        st.session_state["current_draft_id"] = _new_id
                        st.session_state["_saved_title"] = r["title"] or ""
                        st.session_state["_saved_body"] = r["body"]
                        st.session_state["draft_saved_at"] = db.now_iso()
                        st.session_state["_flash_msg"] = (
                            "✍️ 初稿已生成并入库（先存一份保底，不会丢）。"
                            "如需再改，可在下方编辑后点「保存草稿」。"
                        )
                        # 重跑一次，让上方「我的草稿」立刻能看到这条新稿
                        st.rerun()
                    else:
                        st.warning("初稿已生成，但入库失败。请点下方「💾 保存草稿」重试，"
                                   "或先复制正文保底。")
                else:
                    st.error(f"生成失败：{r['error']}")

        # 初稿展示 + AI 修改（在表单外，支持反复修改）
        if st.session_state.get("draft_body"):
            st.divider()
            st.markdown("#### 📄 当前稿件")
            # 这两个输入框只由 key 管值（见 _set_draft_content），不再同时传 value=，
            # 否则 Streamlit 每轮都会刷一条"Session State 与默认值冲突"的告警。
            # setdefault 是兜底：万一外部没同步 key，也能显示正确内容而不是空白。
            st.session_state.setdefault("draft_title_in", st.session_state.get("draft_title", ""))
            st.session_state.setdefault("draft_body_in", st.session_state.get("draft_body", ""))
            cur_title = st.text_input("标题", key="draft_title_in")
            cur_body = st.text_area("正文", height=400, key="draft_body_in")
            # 同步编辑后的值回 session_state，供 AI 修改读取
            st.session_state["draft_title"] = cur_title
            st.session_state["draft_body"] = cur_body

            # ---------- 保存控制条：使用者自己决定什么时候存 ----------
            _dirty = (cur_title, cur_body) != (
                st.session_state.get("_saved_title"), st.session_state.get("_saved_body")
            )
            _sc1, _sc2, _sc3 = st.columns([1.1, 1.2, 2.0])
            with _sc1:
                _click_save = st.button(
                    "💾 保存草稿", type="primary", key="btn_save_draft",
                    help="把当前标题和正文存入云端草稿箱，存完立刻刷新下方/上方草稿列表",
                )
            with _sc2:
                _click_save_as = st.button(
                    "📄 另存为新草稿", key="btn_saveas_draft",
                    help="保留当前这条不动，另存一份新草稿（相当于留一个版本快照）",
                )
            with _sc3:
                _autosave = st.toggle(
                    "自动保存（改动即入库）", value=True, key="draft_autosave",
                    help="开启：正文一改就自动入库，最保险；关闭：只有点「保存草稿」才入库，"
                         "完全由你决定，但关页面前请记得点保存。",
                )

            # 谁触发保存：手动 > 另存 > 自动兜底
            _save_mode = None
            if _click_save:
                _save_mode = "manual"
            elif _click_save_as:
                _save_mode = "copy"
            elif _dirty and _autosave:
                _save_mode = "auto"

            if _save_mode:
                _did = db.save_draft(
                    title=cur_title, body=cur_body,
                    topic=st.session_state.get("draft_topic", ""),
                    angle=st.session_state.get("draft_angle", ""),
                    target_media=st.session_state.get("draft_target", ""),
                    # 另存为：不带 draft_id → 库里新增一条，原稿不动
                    draft_id=None if _save_mode == "copy" else st.session_state.get("current_draft_id"),
                )
                if _did:
                    _invalidate_draft_cache()
                    st.session_state["current_draft_id"] = _did
                    st.session_state["_saved_title"] = cur_title
                    st.session_state["_saved_body"] = cur_body
                    st.session_state["draft_saved_at"] = db.now_iso()
                    if _save_mode == "manual":
                        st.session_state["_flash_msg"] = "✅ 已保存到云端草稿箱。"
                    elif _save_mode == "copy":
                        st.session_state["_flash_msg"] = (
                            "✅ 已另存为新草稿（原稿保持不变）。可在「我的草稿」中切换。"
                        )
                    # 关键：保存后立刻重跑一次，让草稿列表立即出现这一条，
                    # 不用再手动刷新页面（此前列表渲染在上方，本轮拿不到新数据）
                    st.rerun()
                else:
                    st.error("保存失败（稿件未入库）。请检查网络后重试，或先手动复制一份正文保底。")

            st.caption(_draft_save_status(_dirty))

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
                            _set_draft_content(rr["title"], rr["body"])
                            # 关键：先落库、再给提示、最后只 rerun 一次，
                            # 这样提示不会因为中途重跑而被丢掉
                            if st.session_state.get("draft_autosave", True):
                                _rid = db.save_draft(
                                    title=rr["title"], body=rr["body"],
                                    topic=st.session_state.get("draft_topic", ""),
                                    angle=st.session_state.get("draft_angle", ""),
                                    target_media=st.session_state.get("draft_target", ""),
                                    draft_id=st.session_state.get("current_draft_id"),
                                )
                                if _rid:
                                    _invalidate_draft_cache()
                                    st.session_state["current_draft_id"] = _rid
                                    st.session_state["_saved_title"] = rr["title"]
                                    st.session_state["_saved_body"] = rr["body"]
                                    st.session_state["draft_saved_at"] = db.now_iso()
                                    st.session_state["_flash_msg"] = "✅ AI 修改完成，已自动保存到草稿箱。"
                                else:
                                    st.session_state["_flash_msg"] = (
                                        "✅ AI 修改完成，但自动保存失败。请点「💾 保存草稿」。"
                                    )
                            else:
                                st.session_state["_flash_msg"] = (
                                    "✅ AI 修改完成。自动保存已关闭，请点「💾 保存草稿」入库。"
                                )
                            st.rerun()
                        else:
                            st.error(f"修改失败：{rr['error']}")
            with rc2:
                if st.button("🗑 清空稿件"):
                    # 二次确认：原来一点就清，手滑即丢稿
                    st.session_state["_confirm_clear_draft"] = True
                    st.rerun()
            if st.session_state.get("_confirm_clear_draft"):
                st.warning(
                    "⚠️ 确认清空当前稿件？云端草稿库里的历史版本不受影响，"
                    "可在上方「我的草稿」中找回。"
                )
                cc1, cc2 = st.columns(2)
                with cc1:
                    if st.button("✅ 确认清空", key="btn_clear_draft_ok", type="primary"):
                        st.session_state.pop("draft_title", None)
                        st.session_state.pop("draft_body", None)
                        st.session_state["current_draft_id"] = None
                        # 连同输入框自己的 key 一起清，否则下次写新稿还会冒出旧内容
                        st.session_state.pop("draft_title_in", None)
                        st.session_state.pop("draft_body_in", None)
                        st.session_state.pop("_saved_title", None)
                        st.session_state.pop("_saved_body", None)
                        st.session_state.pop("draft_saved_at", None)
                        st.session_state.pop("_confirm_clear_draft", None)
                        st.session_state["_flash_msg"] = (
                            "已清空当前编辑区。云端草稿箱里的稿件没有删除，"
                            "可在上方「我的草稿」随时载入。"
                        )
                        st.rerun()
                with cc2:
                    if st.button("取消", key="btn_clear_draft_cancel"):
                        st.session_state.pop("_confirm_clear_draft", None)
                        st.rerun()
            with st.expander("❓ 怎么用（保存规则一看就懂）", expanded=False):
                st.markdown(
                    "- **生成初稿时会先存一份到草稿箱**（保底，不会丢）。\n"
                    "- 之后的改动要不要存，由你决定：\n"
                    "    - **自动保存打开（默认）**：正文一改就自动入库，最省心。\n"
                    "    - **自动保存关闭**：只有点「💾 保存草稿」才入库。"
                    "状态栏会提示「⚠️ 有未保存的修改」，关页面前记得点一下。\n"
                    "- **💾 保存草稿**：把当前标题+正文存进草稿箱，**存完草稿列表立即更新**，不用刷新页面。\n"
                    "- **📄 另存为新草稿**：原稿不动，再存一份新的（相当于留一个版本快照，"
                    "改坏了可以回去拿旧的）。\n"
                    "- **📂 我的草稿**：点名字即可载入继续编辑；带「▶」的是你当前正在编辑的那条。\n"
                    "    - **✏️ 改名**：不用载入，直接在列表里给它换名字（正文内容不受影响）。\n"
                    "    - **🗑 删除**：删掉这条（有二次确认）。\n"
                    "    - 也可以在正文上方直接改「标题」再保存，效果一样。\n"
                    "- 直接改正文框也行，改完可以让 AI 按你的要求再改一遍。"
                )
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

    
