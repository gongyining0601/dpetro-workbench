# -*- coding: utf-8 -*-
"""访问密码登录门控：首次设置密码 / 登录校验 / 失败限流。

2026-10-08 从 app.py 拆出。原来这是一段裸写在主流程里的 if 代码块，
混在页面渲染中间，现在收敛成一个入口函数 require_auth()。

行为与拆分前完全一致：
- config.AUTH_ENABLED 为假时直接放行（本地调试用）
- 从未设过密码 → 渲染"设置密码"表单，设完自动登录
- 已设密码 → 渲染登录框；连续输错达到上限则锁定一段时间，防暴力破解
- 未通过校验的一律 st.stop()，不会让后面的主界面露出来

唯一入口：
    from auth_ui import require_auth
    require_auth()   # 必须在任何业务内容渲染之前调用
"""
from __future__ import annotations

import time

import streamlit as st

import auth
import config

# 失败限流：连续输错几次就锁多久
MAX_LOGIN_FAILURES = 5
LOCK_SECONDS = 300  # 5 分钟


def require_auth() -> None:
    """登录门控总入口。未通过时函数内部会 st.stop()，不会返回到调用方继续渲染。"""
    if not config.AUTH_ENABLED:
        return  # 未开启密码保护，直接放行
    if st.session_state.get("authenticated", False):
        return  # 本次会话已登录

    if auth.has_access_password():
        _render_login()
    else:
        _render_set_password()


def _render_set_password() -> None:
    """首次使用：让用户设置访问密码，设置成功即自动登录。"""
    st.info("🔐 首次使用，请设置访问密码（整个应用只有一个密码，务必牢记）。")
    with st.form("set_password_form", clear_on_submit=True):
        pw1 = st.text_input("设置访问密码", type="password", placeholder="至少 8 位")
        pw2 = st.text_input("确认密码", type="password")
        submitted = st.form_submit_button("✅ 设置密码", type="primary")

    if not submitted:
        st.stop()

    if not pw1:
        st.error("密码不能为空")
    elif pw1 != pw2:
        st.error("两次输入的密码不一致")
    else:
        try:
            auth.set_access_password(pw1)
        except ValueError as e:
            st.error(str(e))
        else:
            st.session_state["authenticated"] = True
            st.success("密码设置成功，已自动登录")
            st.rerun()
    st.stop()


def _render_login() -> None:
    """已有密码：登录表单 + 失败次数限流。"""
    fail_count = st.session_state.get("_login_fail_count", 0)
    lock_until = st.session_state.get("_login_lock_until", 0)
    now = time.time()

    # 还在锁定期内：连表单都不给看
    if now < lock_until:
        remaining = int(lock_until - now)
        st.error(f"🔒 登录失败次数过多，已锁定 {remaining} 秒后重试")
        st.stop()

    with st.form("login_form", clear_on_submit=True):
        pw = st.text_input("🔐 请输入访问密码", type="password")
        submitted = st.form_submit_button("登录", type="primary")

    if not submitted:
        st.stop()

    if auth.verify_access_password(pw):
        st.session_state["authenticated"] = True
        st.session_state["_login_fail_count"] = 0
        st.session_state["_login_lock_until"] = 0
        st.rerun()
        return

    fail_count += 1
    st.session_state["_login_fail_count"] = fail_count
    if fail_count >= MAX_LOGIN_FAILURES:
        st.session_state["_login_lock_until"] = now + LOCK_SECONDS
        st.error(f"密码错误，已连续失败 {fail_count} 次，锁定 {LOCK_SECONDS // 60} 分钟")
    else:
        st.error(f"密码错误，还剩 {MAX_LOGIN_FAILURES - fail_count} 次尝试机会")

    st.caption("提示：忘记密码需联系管理员重置（清空 app_setting 表中 access_password 记录）。")
    st.stop()
