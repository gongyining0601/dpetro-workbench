"""云端版向量存储：PG 表 article_embedding + 内存 numpy 暴力 cosine。

2026-09-29 改造（方案 A 上云版）：
- 旧版从本地 .npz 文件读写 → 改为从 PG 表 article_embedding 读写
- 查询时一次性 SELECT 全表到内存（数据量小，千篇 768/1024 维毫秒级），numpy 矩阵乘算 cosine
- 单条 upsert/delete 直接走 SQL，不再批量写文件
- 元数据（标题/媒体/栏目/url/decision 等）不在这里存，仍以 PG 的 article/review_record 表为唯一来源

为什么不用 chromadb / pgvector：
- chromadb 1.5 Rust bindings 在 Win 上崩溃（HANDOFF 记录）
- pgvector 扩展要在 Supabase 启用，多一道依赖；本项目数据量小，numpy 暴力算足够
- 简单：纯 Python + numpy，零额外扩展依赖

存储格式（PG 表 article_embedding）：
- article_id  BIGINT PRIMARY KEY  → 对应 article.id
- embedding   JSONB               → float 列表，已 L2 归一化
- updated_at  TEXT                → ISO 字符串

类签名与旧版保持一致（path 参数保留但忽略），topic_matcher 改一行都不用动。
"""
from __future__ import annotations

import numpy as np
import psycopg2
import psycopg2.extras

import config
from db import get_conn, conn_cursor, now_iso


class NumpyVectorStore:
    def __init__(self, path: str = ""):
        """path 参数保留以兼容旧调用方，云端忽略（数据在 PG 表里）。"""
        self.path = path
        self.ids: list[str] = []
        self.embs: np.ndarray | None = None  # (N, dim) float32，归一化
        self._load()

    # ---------- 持久化（PG 读写） ----------

    def _load(self) -> None:
        """启动时一次性拉全表到内存。库空时 self.embs 留 None。"""
        try:
            with get_conn() as c:
                cur = conn_cursor(c)
                cur.execute(
                    "SELECT article_id, embedding FROM article_embedding "
                    "ORDER BY article_id"
                )
                rows = cur.fetchall()
            if not rows:
                self.ids = []
                self.embs = None
                return
            ids = [str(r["article_id"]) for r in rows]
            embs = np.array(rows[0]["embedding"], dtype="float32").reshape(1, -1)
            for r in rows[1:]:
                embs = np.vstack([embs, np.array(r["embedding"], dtype="float32")])
            self.ids = ids
            self.embs = embs
        except Exception as e:
            print(f"[vector_store] 读取 PG 向量表失败，按空库启动：{e}")
            self.ids = []
            self.embs = None

    # ---------- 基本操作 ----------

    def count(self) -> int:
        return len(self.ids)

    def upsert(self, ids: list[str], embs: np.ndarray) -> None:
        """按 id 插入或替换向量。换模型导致维度变化时整体重建。"""
        if len(ids) == 0:
            return
        embs = np.asarray(embs, dtype="float32")
        if embs.ndim != 2 or embs.shape[0] != len(ids):
            raise ValueError(f"embs 形状 {embs.shape} 与 ids 数 {len(ids)} 不匹配")

        # 维度变化（换了嵌入模型）→ 清空重建（DELETE 可回滚，与 UPSERT 同事务）
        dim_changed = self.embs is not None and embs.shape[1] != self.embs.shape[1]
        if dim_changed:
            print(f"[vector_store] 向量维度变化，重建索引")

        # 写 PG（清空 + UPSERT 在同一事务，失败可整体回滚）
        ts = now_iso()
        with get_conn() as c:
            cur = conn_cursor(c)
            if dim_changed:
                cur.execute("DELETE FROM article_embedding")
            for aid, vec in zip(ids, embs):
                cur.execute(
                    "INSERT INTO article_embedding(article_id, embedding, updated_at) "
                    "VALUES (%s, %s, %s) "
                    "ON CONFLICT (article_id) DO UPDATE SET "
                    "embedding=EXCLUDED.embedding, updated_at=EXCLUDED.updated_at",
                    (int(aid), psycopg2.extras.Json(vec.tolist()), ts),
                )

        if dim_changed:
            self.ids, self.embs = [], None

        # 同步内存索引
        index = {aid: i for i, aid in enumerate(self.ids)}
        new_rows: list[np.ndarray] = []
        new_ids: list[str] = []
        for aid, vec in zip(ids, embs):
            if aid in index:
                self.embs[index[aid]] = vec  # 替换
            else:
                new_ids.append(aid)
                new_rows.append(vec)
        if new_rows:
            self.ids.extend(new_ids)
            stack = np.stack(new_rows)
            self.embs = stack if self.embs is None else np.vstack([self.embs, stack])

    def delete(self, ids: list[str]) -> int:
        """删除指定 id，返回实际删除条数。"""
        if not ids:
            return 0
        int_ids = [int(i) for i in ids]
        with get_conn() as c:
            cur = conn_cursor(c)
            cur.execute(
                "DELETE FROM article_embedding WHERE article_id = ANY(%s)",
                (int_ids,),
            )
            removed = cur.rowcount

        # 同步内存索引
        if self.embs is not None and removed > 0:
            drop = set(str(i) for i in int_ids)
            keep_mask = np.array([aid not in drop for aid in self.ids], dtype=bool)
            self.ids = [aid for aid, k in zip(self.ids, keep_mask) if k]
            self.embs = self.embs[keep_mask] if keep_mask.any() else None
        return removed

    def _clear_pg(self) -> None:
        """清空 PG 向量表（用 DELETE 而非 TRUNCATE，可被事务回滚）。"""
        with get_conn() as c:
            cur = conn_cursor(c)
            cur.execute("DELETE FROM article_embedding")
        self.ids = []
        self.embs = None

    def query(self, emb, top_k: int = 5) -> list[tuple[str, float]]:
        """返回 [(article_id, 0~100 相关度分)]，按相关度降序。

        库存向量已 L2 归一化；入参 emb 也归一化后，cosine = 点积。
        """
        if self.embs is None or len(self.ids) == 0:
            return []
        q = np.asarray(emb, dtype="float32").reshape(-1)
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n
        sims = self.embs @ q  # (N,) cosine
        k = min(top_k, len(self.ids))
        # argpartition 取前 k（比全排序快），再在这 k 个里排序
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        out: list[tuple[str, float]] = []
        for i in idx:
            score = round(float(sims[i]) * 100)  # -100~100
            out.append((self.ids[int(i)], max(0, score)))
        return out
