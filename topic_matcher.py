"""选题对标器：输入素材关键词，返回相似已发稿 + 角度建议 + 避坑提示。

2026-09-29 改造（方案 A 上云版）：
- 嵌入从本地 bge（sentence-transformers + torch）改为 Silicon Flow 免费 API
  - 模型 BAAI/bge-large-zh-v1.5（从 base 升级到 large，更准；维度 1024）
  - 不占 1GB RAM（torch/sentence-transformers 已从 requirements 去掉）
  - 失败时自动回退关键词字符串包含打分（原 MVP 逻辑）
- 向量存储: vector_store.NumpyVectorStore（PG 表 article_embedding 读写 + 内存 numpy cosine）
- 索引范围: 只对 review_record decision='相关'或'借鉴' 的稿件建向量
- 同步: ensure_index_synced() 增量 upsert；decision 变'无关'自动从索引删
- 兜底: Silicon Flow API 调用失败 / 索引空 → 回退关键词字符串包含打分

运行环境：Python 3.12+（不再依赖 torch，Streamlit Cloud / 本地均可）
"""
from __future__ import annotations

import os

# Silicon Flow 不需要 HF 镜像设置（旧版 bge 本地加载才需要），保留无副作用
# 兼容性：某些子模块若引入 transformers 仍会读这两个变量，设上不报错
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import requests as _requests
import numpy as np

import config
import db
from vector_store import NumpyVectorStore


_STORE: NumpyVectorStore | None = None
_INDEX_PATH = "data/article_vectors.npz"  # 旧路径占位，新版 NumpyVectorStore 忽略


def _init():
    """懒加载向量库（不再加载本地模型）。失败返回 None（调用方回退关键词检索）。"""
    global _STORE
    if _STORE is not None:
        return _STORE
    try:
        config.ensure_dirs()
        _STORE = NumpyVectorStore(_INDEX_PATH)
        return _STORE
    except Exception as e:
        print(f"[topic_matcher] 向量库初始化失败，回退关键词检索：{e}")
        _STORE = None
        return None


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Silicon Flow API 批量嵌入。返回归一化向量列表。

    单次最多传 32 条文本（API 限频策略保守值）；超过自动分批。
    """
    if not texts:
        return []
    if not config.SF_API_KEY:
        raise RuntimeError("SILICONFLOW_API_KEY 未设置")

    headers = {"Authorization": f"Bearer {config.SF_API_KEY}"}
    out: list[list[float]] = []
    BATCH = 32
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        r = _requests.post(
            config.SF_EMBED_URL,
            headers=headers,
            json={
                "model": config.SF_EMBED_MODEL,
                "input": batch,
                "encoding_format": "float",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()["data"]
        # 按 index 排序保证顺序对齐
        data.sort(key=lambda x: x["index"])
        for d in data:
            vec = np.array(d["embedding"], dtype="float32")
            n = np.linalg.norm(vec)
            out.append((vec / n if n > 0 else vec).tolist())
    return out


def _embed(text: str) -> list[float]:
    """单条文本嵌入（封装 _embed_batch）。"""
    return _embed_batch([text])[0]


def _doc_text(title: str, body: str) -> str:
    """构造用于嵌入的文档文本：标题 + 正文前 500 字。"""
    return f"{title}\n{(body or '')[:500]}"


def _fetch_reviewed_articles():
    """拉 PG 中 decision='相关'/'借鉴' 的稿件（建索引候选集）。"""
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.body_text, a.url, a.publish_date, "
            "c.name AS column_name, s.name AS source_name, r.decision "
            "FROM article a "
            "JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE r.decision IN ('相关','借鉴')"
        )
        return cur.fetchall()


def ensure_index_synced() -> dict:
    """同步 PG 已审稿到向量索引。返回统计。幂等，可重复跑。"""
    stats = {"in_sqlite": 0, "in_index": 0, "added": 0,
             "updated": 0, "removed": 0, "fallback": False, "error": None}
    store = _init()
    if store is None:
        stats["fallback"] = True
        return stats

    try:
        rows = _fetch_reviewed_articles()
        stats["in_sqlite"] = len(rows)
        stats["in_index"] = store.count()

        pg_ids = {str(r["id"]) for r in rows}
        index_ids = set(store.ids)

        # 新增：PG 有、索引没有
        to_add = [r for r in rows if str(r["id"]) not in index_ids]
        if to_add:
            texts = [_doc_text(r["title"] or "", r["body_text"] or "") for r in to_add]
            try:
                embs = _embed_batch(texts)
                store.upsert([str(r["id"]) for r in to_add], np.array(embs, dtype="float32"))
                stats["added"] = len(to_add)
            except Exception as e:
                print(f"[topic_matcher] 新增嵌入失败：{e}")
                stats["error"] = str(e)

        # 更新：两边都有的，正文可能被改过——本项目正文爬后不变，这里不重算，
        # 仅在需要时可改为按 content_hash 比对。保持简单：不更新。
        # updated 保留字段，当前恒 0。

        # 删除：索引有、PG 已不在"相关/借鉴"（decision 改成无关或记录被删）
        to_remove = list(index_ids - pg_ids)
        if to_remove:
            stats["removed"] = store.delete(to_remove)

        stats["in_index"] = store.count()
    except Exception as e:
        stats["error"] = str(e)
        print(f"[topic_matcher] 同步失败：{e}")

    return stats


def match(keywords: list[str], top_k: int = 5) -> list[dict]:
    """返回与关键词最相关的已发稿。

    优先向量检索（Silicon Flow 嵌入 + numpy cosine）；API 失败/索引空 → 回退关键词。
    """
    if not keywords:
        return []
    store = _init()
    if store is not None and store.count() > 0:
        return _match_vector(keywords, top_k, store)
    return _match_keywords(keywords, top_k)


def _fetch_meta_by_ids(ids: list[str]) -> dict[str, dict]:
    """按 article_id 批量回查元数据（PG 是元数据唯一来源）。"""
    if not ids:
        return {}
    int_ids = [int(i) for i in ids]
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.publish_date, "
            "c.name AS column_name, s.name AS source_name, r.decision "
            "FROM article a "
            "JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE a.id = ANY(%s)",
            (int_ids,),
        )
        rows = cur.fetchall()
    return {
        str(r["id"]): {
            "title": r["title"], "source": r["source_name"],
            "column": r["column_name"], "decision": r["decision"],
            "publish_date": r["publish_date"], "url": r["url"],
        }
        for r in rows
    }


def _match_vector(keywords: list[str], top_k: int, store: NumpyVectorStore) -> list[dict]:
    """Silicon Flow 嵌入 + numpy 向量检索。"""
    query_text = " ".join(keywords)
    try:
        emb = _embed(query_text)
        hits = store.query(emb, top_k=top_k)  # [(article_id, score)]
    except Exception as e:
        print(f"[topic_matcher] 向量查询失败，回退关键词：{e}")
        return _match_keywords(keywords, top_k)

    metas = _fetch_meta_by_ids([aid for aid, _ in hits])
    out: list[dict] = []
    for aid, score in hits:
        m = metas.get(aid)
        if not m:
            continue
        out.append({**m, "score": score})
    return out


def _match_keywords(keywords: list[str], top_k: int) -> list[dict]:
    """原 MVP 关键词字符串包含打分（兜底）。"""
    kw_lower = [k.lower() for k in keywords]
    rows = db.fetch_reviewed(limit=1000)
    scored: list[dict] = []
    for r in rows:
        title = (r["title"] or "")
        with db.get_conn() as c:
            cur = db.conn_cursor(c)
            cur.execute("SELECT body_text FROM article WHERE id=%s", (r["id"],))
            art = cur.fetchone()
        body = (art["body_text"] if art else "") or ""
        title_l = title.lower()
        body_l = body.lower()
        score = 0
        for kw in kw_lower:
            if kw in title_l:
                score += 5
            if kw in body_l:
                score += 1
        if score > 0:
            scored.append({
                "title": title,
                "source": r["source_name"],
                "column": r["column_name"],
                "decision": r["decision"],
                "publish_date": r["publish_date"],
                "url": r["url"],
                "score": score,
            })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def angle_advice(keywords: list[str]) -> list[str]:
    """基于锦州石化常见角度的启发式建议。"""
    advice: list[str] = []
    kw = " ".join(keywords)
    if "检修" in kw or "春检" in kw:
        advice.append("角度建议：选一个关键节点（如催化剂装填、压缩机对中）做特写，配'小改小革'人物故事。")
    if "保供" in kw or "储气" in kw:
        advice.append("角度建议：从'区域调峰+极端天气应对'切入，引用注采量数据。")
    if "党建" in kw or "党员" in kw:
        advice.append("角度建议：避开'会议记录体'，选一个具体岗位（如催化主操）展开。")
    if "VOCs" in kw or "环保" in kw:
        advice.append("角度建议：用一组检测数字（LDAR点位整改率、超低排放比例）做骨架。")
    if not advice:
        advice.append("角度建议：先定一个具体装置/具体人，再倒推选题——避免'全厂综述'式空泛。")
    advice.append("避坑提示：辽报忌'企业内部口径'（如'装置一次开车成功'需补背景解释），中石油报可保留行业术语。")
    return advice


if __name__ == "__main__":
    print("同步向量索引：", ensure_index_synced())
    print("检索：", [(m["title"][:20], m["score"]) for m in match(["春检", "催化"])])
