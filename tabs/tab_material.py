# -*- coding: utf-8 -*-
"""素材对标 Tab：选题对标器（语义检索）+ 图文素材库（上传/浏览/批量删除）。

2026-10-24 从 app.py 拆出（原本内联在 with tab_material: 里 238 行）。
拆的时候把「图文素材上传配置」三个常量一并搬了过来 —— 它们只在这一块里用，
留在 app.py 会形成 app -> tab_material -> app 的循环依赖。
"""
from __future__ import annotations

import json
import os
import re
import time as _time

import streamlit as st

import config
import topic_matcher
from db_cache import _fetch_image_articles_cached
from image_loader import _preload_images, _render_image
from text_utils import _md_escape, _safe_anchor

# ---------------- 图文素材上传配置 ----------------
_UPLOAD_MAX_MB = 10      # 单张上传大小上限
_UPLOAD_MAX_DIM = 1920  # 超过此边长自动缩放
_UPLOAD_PAGE_SIZE = 12  # 每页展示数量


def render_material():
    """渲染素材对标 Tab（选题对标 + 图文素材两个子页）。"""
    sub_match, sub_image = st.tabs(["🎯 选题对标", "📷 图文素材"])
    with sub_match:
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
            with st.spinner("AI 生成角度建议中…（失败会自动回退到规则建议）"):
                advice = topic_matcher.angle_advice(kws, matches)
            for a in advice:
                st.markdown(f"- {a}")


    
    with sub_image:
        st.subheader("📷 图文素材库")
        st.caption("所有带图片的稿件（不限行业），可作图片新闻参考。")

        # ----- 用户上传图片（Pillow 验证 + 压缩）-----
        st.markdown("#### 上传本地图片")
        st.caption(
            f"支持 jpg/jpeg/png/gif，单张 ≤ {_UPLOAD_MAX_MB}MB；"
            f"超过 {_UPLOAD_MAX_DIM}px 自动缩小、质量 85%。"
        )
        uploaded = st.file_uploader(
            "选择图片",
            type=["jpg", "jpeg", "png", "gif"],
            accept_multiple_files=True,
            key="img_uploader",
        )
        if uploaded:
            try:
                from PIL import Image
            except ImportError:
                st.error("缺少 Pillow 依赖，请在虚拟环境执行：.venv\\Scripts\\pip install Pillow")
                st.stop()
            try:
                os.makedirs(config.UPLOAD_DIR, exist_ok=True)
            except (OSError, PermissionError):
                st.error("当前环境文件系统只读，无法保存上传图片。请在本地运行或配置可写目录后使用此功能。")
                st.stop()
            saved, errors = [], []
            for f in uploaded:
                # 大小校验
                if len(f.getbuffer()) > _UPLOAD_MAX_MB * 1024 * 1024:
                    errors.append(f"{f.name}：超过 {_UPLOAD_MAX_MB}MB 限制")
                    continue
                try:
                    f.seek(0)
                    img = Image.open(f)
                    img.verify()  # 验证是真图片
                    f.seek(0)
                    img = Image.open(f)  # verify 后需重开
                    # 缩放
                    if max(img.size) > _UPLOAD_MAX_DIM:
                        img.thumbnail((_UPLOAD_MAX_DIM, _UPLOAD_MAX_DIM))
                    # 模式归一（保证 JPEG 能存）
                    if img.mode in ("RGBA", "P", "LA"):
                        img = img.convert("RGB")
                    # 落盘
                    ts = _time.strftime("%Y%m%d_%H%M%S")
                    safe_name = re.sub(r'[\\/:*?"<>|]', '_', f.name)
                    save_path = os.path.join(config.UPLOAD_DIR, f"{ts}_{safe_name}")
                    ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else "jpg"
                    if ext == "png":
                        img.save(save_path, "PNG", optimize=True)
                    else:
                        # jpg/jpeg/gif 一律存为 JPEG
                        img.save(save_path, "JPEG", quality=85, optimize=True)
                    saved.append(save_path)
                except Exception as e:
                    errors.append(f"{f.name}：无法识别为图片 ({e})")
            if saved:
                st.success(f"已上传 {len(saved)} 张")
                cols = st.columns(min(len(saved), 4))
                for i, p in enumerate(saved):
                    with cols[i % len(cols)]:
                        st.image(p, caption=os.path.basename(p), width="stretch")
            if errors:
                st.error("以下文件上传失败：")
                for e in errors:
                    st.markdown(f"- {_md_escape(e)}")

        # ----- 已上传图片：筛选 + 分页 + 逐张/批量删除 -----
        if os.path.isdir(config.UPLOAD_DIR):
            local_imgs = sorted(
                [os.path.join(config.UPLOAD_DIR, f) for f in os.listdir(config.UPLOAD_DIR)
                 if f.lower().endswith((".jpg", ".jpeg", ".png", ".gif"))],
                key=os.path.getmtime,
                reverse=True,
            )
            search = st.text_input("🔍 按文件名筛选", key="img_search", placeholder="输入关键字过滤")
            if search:
                local_imgs = [p for p in local_imgs if search.lower() in os.path.basename(p).lower()]
            if local_imgs:
                total = len(local_imgs)
                total_pages = max(1, (total + _UPLOAD_PAGE_SIZE - 1) // _UPLOAD_PAGE_SIZE)
                page = min(st.session_state.get("img_page", 0), total_pages - 1)
                start = page * _UPLOAD_PAGE_SIZE
                page_imgs = local_imgs[start:start + _UPLOAD_PAGE_SIZE]
                st.caption(f"共 {total} 张 · 第 {page + 1}/{total_pages} 页")

                # 网格：每行 3 张，每张配元数据 + 逐张删 + 多选框
                # key 统一用文件名 basename（唯一标识），避免删除/翻页后索引错位导致 widget 状态错乱
                sel_keys = []  # [(key, path)] 用于批量删
                for row_start in range(0, len(page_imgs), 3):
                    row = page_imgs[row_start:row_start + 3]
                    cols = st.columns(3)
                    for j, p in enumerate(row):
                        bn = os.path.basename(p)
                        sel_key = f"sel_{bn}"
                        del_key = f"del_{bn}"
                        sel_keys.append((sel_key, p))
                        with cols[j]:
                            st.image(p, width="stretch")
                            st.caption(f"📄 {bn}")
                            st.caption(
                                f"{os.path.getsize(p) // 1024} KB · "
                                f"{_time.strftime('%m-%d %H:%M', _time.localtime(os.path.getmtime(p)))}"
                            )
                            if st.button("🗑 删除", key=del_key):
                                try:
                                    os.remove(p)
                                    # 清理残留的 checkbox 状态，避免下次渲染时 key 仍为 True
                                    st.session_state.pop(sel_key, None)
                                    st.toast(f"已删除 {bn}", icon="🗑")
                                except Exception as e:
                                    st.error(f"删除失败：{e}")
                                st.rerun()
                            st.checkbox("选中", key=sel_key)

                # 批量删除条
                st.divider()
                n_sel = sum(1 for k, _ in sel_keys if st.session_state.get(k, False))
                bc1, bc2 = st.columns(2)
                with bc1:
                    bulk_label = f"🗑 删除选中({n_sel})" if n_sel else "🗑 删除选中"
                    if st.button(bulk_label, key="bulk_del", disabled=(n_sel == 0)):
                        st.session_state["_bulk_confirm"] = True
                        st.rerun()
                with bc2:
                    if st.button("清空本页选择", key="sel_clear"):
                        # 用 pop 而非直接设 False：widget 已渲染时设其 key 会抛 StreamlitAPIException
                        for k, _ in sel_keys:
                            st.session_state.pop(k, None)
                        st.session_state["_bulk_confirm"] = False
                        st.rerun()

                # 批量删除二次确认
                if st.session_state.get("_bulk_confirm", False):
                    st.warning(f"⚠️ 确认删除本页选中的 {n_sel} 张？此操作不可撤销。")
                    kc1, kc2 = st.columns(2)
                    with kc1:
                        if st.button("✅ 确认删除", key="bulk_yes", type="primary"):
                            for k, p in sel_keys:
                                if st.session_state.get(k, False) and os.path.exists(p):
                                    try:
                                        os.remove(p)
                                    except Exception:
                                        pass
                            for k, _ in sel_keys:
                                st.session_state.pop(k, None)
                            st.session_state["_bulk_confirm"] = False
                            st.toast("批量删除完成", icon="🗑")
                            st.rerun()
                    with kc2:
                        if st.button("取消", key="bulk_no"):
                            st.session_state["_bulk_confirm"] = False
                            st.rerun()

                # 分页按钮
                pc1, pc2, pc3 = st.columns([1, 2, 1])
                with pc1:
                    if st.button("⬅ 上一页", key="pg_prev", disabled=(page == 0)):
                        st.session_state["img_page"] = page - 1
                        st.rerun()
                with pc2:
                    st.caption(f"第 {page + 1} / {total_pages} 页")
                with pc3:
                    if st.button("下一页 ➡", key="pg_next", disabled=(page >= total_pages - 1)):
                        st.session_state["img_page"] = page + 1
                        st.rerun()
            else:
                st.info("暂无已上传图片。先在上面上传几张试试。")

        imgs = _fetch_image_articles_cached(200)
        st.metric("图文稿件", len(imgs))
        # 并发预加载所有稿件图片
        all_img_urls = []
        for a in imgs:
            raw = a.get("image_urls")
            if raw:
                try:
                    urls = json.loads(raw) if isinstance(raw, str) else raw
                    all_img_urls.extend(urls[:5])
                except (json.JSONDecodeError, TypeError):
                    pass
        _preload_images(all_img_urls)

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
                                _render_image(u)
                    except (json.JSONDecodeError, TypeError):
                        pass
                # 正文（图片新闻的 summary 是 body_text 的前缀，避免重复只显示 body_text）
                if a.get("body_text"):
                    st.markdown(_md_escape(a["body_text"][:1000]))
                elif a.get("summary"):
                    st.markdown(_md_escape(a["summary"]))
                if a.get("url"):
                    st.markdown(_safe_anchor("原文链接", a["url"]))
