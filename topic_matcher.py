"""选题对标器：输入素材关键词，返回相似已发稿 + 角度建议 + 避坑提示。

2026-09-29 改造（方案 A 上云版）：
- 嵌入从本地 bge（sentence-transformers + torch）改为云端 API
  - 2026-10-04 从 SiliconFlow 切换到智谱 embedding-3（SiliconFlow 需余额 402）
  - 模型 embedding-3，维度 1024
  - 不占 1GB RAM（torch/sentence-transformers 已从 requirements 去掉）
  - 失败时自动回退关键词字符串包含打分（原 MVP 逻辑）
- 向量存储: vector_store.NumpyVectorStore（PG 表 article_embedding 读写 + 内存 numpy cosine）
- 索引范围: 只对 review_record decision='保存' 的稿件建向量
- 同步: ensure_index_synced() 增量 upsert；decision 变'无关'自动从索引删
- 兜底: Silicon Flow API 调用失败 / 索引空 → 回退关键词字符串包含打分

运行环境：Python 3.12+（不再依赖 torch，Streamlit Cloud / 本地均可）
"""
from __future__ import annotations

import os
import re

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
    """智谱 embedding-3 批量嵌入。返回归一化向量列表。

    单次最多传 32 条文本；超过自动分批。
    智谱 embedding-3 支持 dimensions 参数（256-2048）。
    """
    if not texts:
        return []
    if not config.EMBED_API_KEY:
        raise RuntimeError("ZHIPU_API_KEY 未设置（embedding 复用智谱 key）")

    headers = {"Authorization": f"Bearer {config.EMBED_API_KEY}"}
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
                "dimensions": config.SF_EMBED_DIM,
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
    """拉 PG 中 decision='保存' 的稿件（建索引候选集）。"""
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.body_text, a.url, a.publish_date, "
            "c.name AS column_name, s.name AS source_name, r.decision "
            "FROM article a "
            "JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE r.decision='保存'"
        )
        return cur.fetchall()


def ensure_index_synced() -> dict:
    """同步 PG 已审稿到向量索引。返回统计。幂等，可重复跑。"""
    stats = {"in_db": 0, "in_index": 0, "added": 0,
             "updated": 0, "removed": 0, "fallback": False, "error": None}
    store = _init()
    if store is None:
        stats["fallback"] = True
        return stats

    try:
        rows = _fetch_reviewed_articles()
        stats["in_db"] = len(rows)
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


# 锦州石化领域同义词表（小词表，手工维护）——提升向量与关键词检索召回
_SYNONYMS = {
    "检修": ["大修", "消缺", "春检", "秋检", "停工检修"],
    "大修": ["检修", "消缺", "停工检修"],
    "催化": ["催化裂化", "催化重整", "催化主操"],
    "加氢": ["加氢裂化", "加氢精制", "渣油加氢"],
    "环保": ["VOCs", "LDAR", "超低排放", "绿色低碳", "双碳"],
    "VOCs": ["环保", "LDAR", "挥发性有机物"],
    "党建": ["党员", "党支部", "主题党日", "红色"],
    "保供": ["储气", "调峰", "冬保"],
    "节能": ["降本", "能效", "减排", "能耗"],
    "技改": ["技术改造", "小改小革", "技措", "改造"],
    "安全": ["HSE", "隐患", "应急", "演练", "事故"],
    "数字化": ["智能", "智慧", "信息化", "AI", "智能炼化"],
    "人才": ["技能", "工匠", "师带徒", "竞赛", "培训"],
    "创新": ["研发", "专利", "攻关", "QC"],
    "质量": ["质检", "计量", "化验", "标准"],
}


def _expand_query(keywords: list[str]) -> list[str]:
    """扩展 query：原词 + 领域同义词（提升向量与关键词检索召回）。"""
    expanded = list(keywords)
    kw_str = " ".join(keywords)
    for kw in keywords:
        for syn in _SYNONYMS.get(kw, []):
            if syn not in expanded:
                expanded.append(syn)
    # 整串匹配：如"春检"含"检修"语义，补上相关词
    for root, syns in _SYNONYMS.items():
        if root in kw_str and root not in expanded:
            expanded.append(root)
        for syn in syns:
            if syn in kw_str and syn not in expanded:
                expanded.append(syn)
    return expanded


def _calibrate_score(cos_score: float, title_hits: int) -> int:
    """把 cosine*100（典型 40-85）线性校准到 55-95，加标题命中加权，上限 99。"""
    calibrated = 55 + (cos_score - 40) * (95 - 55) / max(1, (85 - 40))
    calibrated = max(0, min(99, calibrated))
    return min(99, round(calibrated) + title_hits * 4)


def _match_vector(keywords: list[str], top_k: int, store: NumpyVectorStore) -> list[dict]:
    """Silicon Flow 嵌入 + numpy 向量检索 + 重排（标题命中加权 + 分数校准）。"""
    expanded = _expand_query(keywords)
    query_text = " ".join(expanded)
    try:
        emb = _embed(query_text)
        # 多取候选便于重排
        hits = store.query(emb, top_k=max(top_k * 3, top_k))
    except Exception as e:
        print(f"[topic_matcher] 向量查询失败，回退关键词：{e}")
        return _match_keywords(keywords, top_k)

    metas = _fetch_meta_by_ids([aid for aid, _ in hits])
    kw_lower = [k.lower() for k in keywords]
    out: list[dict] = []
    for aid, cos_score in hits:
        m = metas.get(aid)
        if not m:
            continue
        title_l = (m["title"] or "").lower()
        title_hits = sum(1 for kw in kw_lower if kw and kw in title_l)
        final = _calibrate_score(cos_score, title_hits)
        out.append({**m, "score": final})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_k]


def _match_keywords(keywords: list[str], top_k: int) -> list[dict]:
    """关键词字符串包含打分（兜底）。一条 JOIN 查出 body_text，避免 N+1 查询。"""
    kw_lower = [k.lower() for k in _expand_query(keywords)]
    with db.get_conn() as c:
        cur = db.conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.publish_date, a.body_text, "
            "r.decision, c.name AS column_name, s.name AS source_name "
            "FROM article a "
            "JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE r.decision='保存' "
            "ORDER BY r.reviewed_at DESC LIMIT 1000"
        )
        rows = cur.fetchall()
    scored: list[dict] = []
    for r in rows:
        title = r["title"] or ""
        body = r["body_text"] or ""
        title_l = title.lower()
        body_l = body.lower()
        score = 0
        body_hits = 0
        for kw in kw_lower:
            if kw in title_l:
                score += 5
            # 正文命中最多算 3 个关键词，避免长文因含多个词而虚高
            if kw in body_l and body_hits < 3:
                score += 1
                body_hits += 1
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


def _angle_advice_llm(keywords: list[str], matched: list[dict]) -> list[str] | None:
    """LLM 生成量身角度建议（智谱→腾讯云回退）。失败返回 None（调用方回退启发式）。"""
    zp_key = config.ZHIPU_API_KEY
    tc_key = config.TENCENTCLOUD_API_KEY
    if not (zp_key or tc_key):
        return None

    topic = " ".join(keywords)
    refs = "\n".join(
        f"- {m.get('title', '')}（{m.get('source', '')}/{m.get('column', '')}，{m.get('publish_date') or ''}）"
        for m in (matched or [])[:5]
    )

    sys_prompt = (
        "你是锦州石化公司的资深新闻编辑，熟悉中国石油报、辽宁日报、企业内网的用稿口味。"
        "用户给出选题关键词和已发相似稿，请给出 3-5 条具体可落地的写作角度建议。"
        "每条要求：① 切入点具体（装置/人物/节点/数据），② 避免空泛（如'全厂综述'），"
        "③ 一句话表达。直接列点，不要寒暄、不要编号前缀。"
    )
    user_msg = (
        f"选题：{topic}\n\n"
        f"相似已发稿参考：\n{refs or '（暂无相似稿，按锦州石化通用思路给）'}\n\n"
        f"请给 3-5 条写作角度建议。"
    )

    def _call(base: str, api_key: str, model: str):
        try:
            resp = _requests.post(
                base,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1024,
                    "thinking": {"type": "disabled"},
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _parse(data: dict) -> list[str] | None:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return None
        lines = []
        for l in content.split("\n"):
            l = l.strip()
            if not l:
                continue
            l = re.sub(r"^[-*•]\s*", "", l)
            l = re.sub(r"^\d+[.、)）]\s*", "", l)
            if l:
                lines.append(l)
        return lines[:6] if lines else None

    # 1) 智谱
    if zp_key:
        data = _call(config.ZHIPU_CHAT_URL, zp_key, config.ZHIPU_CHAT_MODEL)
        if data:
            parsed = _parse(data)
            if parsed:
                return parsed
    # 2) 腾讯云 deepseek
    if tc_key:
        data = _call(
            config.TENCENTCLOUD_CHAT_URL,
            tc_key,
            config.TENCENTCLOUD_CHAT_MODEL,
        )
        if data:
            parsed = _parse(data)
            if parsed:
                return parsed
    return None


def _angle_advice_heuristic(keywords: list[str]) -> list[str]:
    """扩充版启发式角度建议（覆盖 ~15 类锦州石化常见选题）。"""
    advice: list[str] = []
    kw = " ".join(keywords)
    rules = [
        (("检修", "春检", "秋检", "大修", "消缺"), "选一个关键节点（催化剂装填、压缩机对中、塔盘更换）做特写，配'小改小革'人物故事。"),
        (("保供", "储气", "调峰", "冬保"), "从'区域调峰+极端天气应对'切入，引用注采量数据。"),
        (("党建", "党员", "党支部", "主题党日"), "避开'会议记录体'，选一个具体岗位（如催化主操）展开。"),
        (("VOCs", "环保", "LDAR", "超低排放"), "用一组检测数字（LDAR点位整改率、超低排放比例）做骨架。"),
        (("安全", "HSE", "隐患", "应急", "演练"), "挑一次具体隐患整改或应急演练，写'发现-处置-复盘'三段式。"),
        (("技改", "技术改造", "小改小革", "技措"), "算一笔账：改造投入 vs 节省/增收，用数字说话。"),
        (("节能", "降本", "能效", "减排", "能耗"), "对比改造前后能耗数据（蒸汽/电/水单耗），写'能效账本'。"),
        (("数字化", "智能", "智慧", "信息化", "AI"), "聚焦一个智能应用场景（智能巡检、APC先进控制），写前后对比。"),
        (("人才", "技能", "工匠", "师带徒", "竞赛", "培训"), "选一名技师/工匠，写'岗位成长史'，避免罗列培训人次。"),
        (("创新", "研发", "专利", "攻关", "QC"), "讲一个攻关小组故事：问题-尝试-突破，配技术指标提升数据。"),
        (("质量", "质检", "计量", "化验", "标准"), "用一次质量攻关或计量比对做主线，写'精度背后的故事'。"),
        (("双碳", "绿色", "低碳", "碳"), "用碳减排数据做骨架，写'一吨碳的旅程'或具体降碳项目。"),
        (("设备", "机泵", "换热器", "压缩机", "阀门"), "选一台关键设备，写它的'健康档案'和守护它的班组。"),
        (("廉政", "作风", "纪检", "监督"), "用一次具体制度落地或案例，写'制度如何管住风险'。"),
        (("文化", "宣传", "品牌", "故事"), "找一个老物件/老照片/老传统，写'石化记忆'人文稿。"),
    ]
    for triggers, tip in rules:
        if any(t in kw for t in triggers):
            advice.append(f"角度建议：{tip}")
    if not advice:
        advice.append("角度建议：先定一个具体装置/具体人，再倒推选题——避免'全厂综述'式空泛。")
    advice.append("避坑提示：辽报忌'企业内部口径'（如'装置一次开车成功'需补背景解释），中石油报可保留行业术语。")
    return advice


def angle_advice(keywords: list[str], matched: list[dict] | None = None) -> list[str]:
    """生成写作角度建议。优先 LLM（智谱→腾讯），失败回退扩充后的启发式。"""
    matched = matched or []
    try:
        llm = _angle_advice_llm(keywords, matched)
        if llm:
            return llm
    except Exception as e:
        print(f"[topic_matcher] LLM 角度建议失败，回退启发式：{e}")
    return _angle_advice_heuristic(keywords)


if __name__ == "__main__":
    """CI 入口：同步 PG 已审稿到向量索引。

    GitHub Actions workflow 在跑完 crawler.py 后调用本入口，
    把新增的"相关/借鉴"稿件 embedding 起来写入 article_embedding 表。
    本地也可手动跑：python -B topic_matcher.py
    """
    stats = ensure_index_synced()
    print(f"[topic_matcher] 同步完成："
          f"in_db={stats.get('in_db', 0)}, "
          f"in_index={stats.get('in_index', 0)}, "
          f"added={stats.get('added', 0)}, "
          f"removed={stats.get('removed', 0)}, "
          f"fallback={stats.get('fallback', False)}")
    if stats.get("error"):
        print(f"[topic_matcher] 同步警告：{stats['error']}")
    elif stats.get("fallback"):
        print("[topic_matcher] 已回退关键词检索（Silicon Flow API 或向量库初始化失败）")
