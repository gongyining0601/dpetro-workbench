# -*- coding: utf-8 -*-
"""数据库查询缓存层：给跨境查询套上 Streamlit 缓存，减少重复往返。

2026-10-08 从 app.py 拆出（原「性能优化：缓存包装」段）。

为什么需要这一层：
Supabase 在境外，一次查询往返约 0.4~0.5 秒。而 Streamlit 每次点击都会重跑脚本，
于是"随便点一下就查一次库"，页面上全是等待。这些数据（稿件列表、草稿、
配置项）大多一天才变一次，没必要每次都回源——所以套 TTL 缓存，
写操作完成后由 _invalidate_xxx() 主动清空，保证"改完立刻能看到"。

三类失效入口：
- _invalidate_caches()         稿件审核/删除/投稿后，清空所有列表缓存
- _invalidate_draft_cache()    草稿增删改后，列表缓存失效
- _invalidate_setting_cache()  配置项（如 last_crawl_date）被改写后
"""
from __future__ import annotations

import streamlit as st

import db

# ==================== 性能优化：缓存包装 ====================
# 2026-10-03 新增：用 Streamlit 缓存减少重复数据库查询
# - init_db: 整个会话只执行一次（建表是幂等的）
# - 读查询: ttl 缓存，写操作后手动清空
@st.cache_resource
def _init_db_cached():
    db.init_db()


@st.cache_data(ttl=30)
def _stats_overview_cached():
    return db.stats_overview()


# 配置项（如"上次爬取日期"）原来每次交互都要查两次库，跨境往返各约 0.4 秒，
# 而它一天才变一次 —— 缓存住，写的时候清。
@st.cache_data(ttl=300)
def _get_setting_cached(key: str, _v: int = 0):
    return db.get_setting(key)


# 草稿列表：原来每次点击都会查一次库（即使抽屉没打开，Streamlit 也会渲染里面的内容）。
# 缓存 + 写操作后清空，既省掉往返，又保证"存完立刻能看到"。
@st.cache_data(ttl=120)
def _list_drafts_cached(limit: int = 30, _v: int = 0):
    return [dict(d) for d in db.list_drafts(limit=limit)]


# 这些列表一天才被爬虫更新一次；每次交互都回源查库（跨境约 0.5 秒/次）纯属浪费。
# 放长缓存，写操作后由 _invalidate_caches() 主动清空，保证改完立刻能看到。
@st.cache_data(ttl=30)
def _fetch_unreviewed_cached(limit=30):
    return db.fetch_unreviewed(limit=limit)


@st.cache_data(ttl=60)
def _fetch_reviewed_cached(limit=200):
    return db.fetch_reviewed(limit=limit)


@st.cache_data(ttl=60)
def _fetch_image_articles_cached(limit=200):
    return db.fetch_image_articles(limit)


# 已排除列表：爬虫写、页面读，一天才变一次，同样走缓存 + 写后失效
@st.cache_data(ttl=60)
def _list_excluded_cached(limit: int = 60, _v: int = 0):
    return [dict(r) for r in db.list_excluded(limit=limit)]


def _invalidate_caches():
    """审核/删除/投稿等写操作后调用，清空所有数据缓存，确保列表立即刷新。"""
    _stats_overview_cached.clear()
    _fetch_unreviewed_cached.clear()
    _fetch_reviewed_cached.clear()
    _fetch_image_articles_cached.clear()
    _list_excluded_cached.clear()


def _invalidate_draft_cache():
    """草稿发生增删改后调用：列表缓存失效，下一轮重新读库（保证存完立刻可见）。"""
    _list_drafts_cached.clear()


def _invalidate_setting_cache():
    """配置项（如 last_crawl_date）被改写后调用。"""
    _get_setting_cached.clear()
