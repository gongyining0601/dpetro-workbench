"""PostgreSQL 建表 + 数据访问层（DAO）。

2026-09-29 改造（方案 A 上云版）：
- 从 SQLite 迁到 Supabase PostgreSQL（数据持久化到云端，多端访问）
- 表名 "column" 改名为 media_column（避免 PG 里 column 保留字反复加引号）
- 占位符 ? → %s（psycopg2 风格）
- INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING（PG 风格）
- AUTOINCREMENT → BIGSERIAL（PG 自增）
- 行对象用 RealDictCursor（行为兼容旧 sqlite3.Row 的 dict-like 访问）

所有 SQL 都用参数化绑定，杜绝注入；时间一律存 ISO 字符串。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

# 连接池：Supabase 免费版连接上限约 60，maxconn 留余量
_POOL_MAXCONN = 10
_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    """懒初始化线程安全连接池。"""
    global _pool
    if _pool is None:
        if not config.DB_DSN:
            raise RuntimeError(
                "DATABASE_URL 未设置。请在 .env / Streamlit Cloud secrets / GitHub "
                "Actions secrets 里配置 Supabase connection string。"
            )
        _pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=_POOL_MAXCONN,
            dsn=config.DB_DSN,
        )
    return _pool

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_source (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    home        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_column (
    id          BIGSERIAL PRIMARY KEY,
    source_id   BIGINT NOT NULL,
    name        TEXT NOT NULL,
    url         TEXT,
    priority    TEXT,
    UNIQUE(source_id, name),
    FOREIGN KEY (source_id) REFERENCES media_source(id)
);

CREATE TABLE IF NOT EXISTS article (
    id            BIGSERIAL PRIMARY KEY,
    column_id     BIGINT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    author        TEXT,
    publish_date  TEXT,
    summary       TEXT,
    body_text     TEXT,
    content_hash  TEXT,
    crawled_at    TEXT NOT NULL,
    FOREIGN KEY (column_id) REFERENCES media_column(id)
);
CREATE INDEX IF NOT EXISTS idx_article_crawled ON article(crawled_at);

CREATE TABLE IF NOT EXISTS review_record (
    id          BIGSERIAL PRIMARY KEY,
    article_id  BIGINT NOT NULL,
    decision    TEXT NOT NULL,   -- 相关 / 无关 / 借鉴
    note        TEXT,
    reviewed_at TEXT NOT NULL,
    UNIQUE(article_id),          -- 每篇只保留最新一次审核结论
    FOREIGN KEY (article_id) REFERENCES article(id)
);

CREATE TABLE IF NOT EXISTS routine_calendar (
    id                  BIGSERIAL PRIMARY KEY,
    topic_name          TEXT NOT NULL,
    start_month         INTEGER,
    start_day           INTEGER,
    end_month           INTEGER,
    end_day             INTEGER,
    recommended_column  TEXT,
    lead_days           INTEGER,
    note                TEXT
);

CREATE TABLE IF NOT EXISTS submission (
    id              BIGSERIAL PRIMARY KEY,
    topic           TEXT NOT NULL,
    target_media    TEXT NOT NULL,
    target_column   TEXT,
    submitted_at    TEXT NOT NULL,
    result          TEXT,    -- 录用 / 退稿 / 待审
    published_date  TEXT,
    published_url   TEXT,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS draft_check (
    id                       BIGSERIAL PRIMARY KEY,
    draft_title              TEXT,
    draft_text               TEXT,
    issues_json              TEXT,
    version_adaptions_json   TEXT,
    checked_at               TEXT NOT NULL
);

-- P3 语义向量索引表（替代旧的 data/article_vectors.npz 落盘文件）
-- embedding 存 JSONB（向量列表），不引入 pgvector 扩展以省事
-- 查询时一次性 SELECT 全表到内存，numpy 暴力 cosine（数据量小，千篇毫秒级）
CREATE TABLE IF NOT EXISTS article_embedding (
    article_id  BIGINT PRIMARY KEY,
    embedding   JSONB NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (article_id) REFERENCES article(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_article_embedding ON article_embedding(article_id);
"""


def now_iso() -> str:
    """返回 UTC 时间的 ISO 字符串，确保跨环境（本地/Streamlit/GitHub Actions）一致。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn():
    """从连接池获取连接；自动提交/回滚，用完归还池。返回 RealDictCursor（dict-like 行）。"""
    pool = _get_pool()
    conn = pool.getconn()
    # 健康检查：连接断开则丢弃重建
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _exec(c, sql, params=None):
    """执行并返回 cursor；统一用 RealDictCursor 走 dict-like 行。"""
    cur = conn_cursor(c)
    cur.execute(sql, params or ())
    return cur


def conn_cursor(conn):
    """从 conn 拿 RealDictCursor（行为兼容旧 sqlite3.Row 的 dict-like 访问）。"""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def init_db() -> None:
    """首次启动建表 + 灌入媒体源/栏目/常规选题种子。幂等可重复跑。"""
    config.ensure_dirs()
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(SCHEMA)
        # 灌媒体源 + 栏目
        for src in config.MEDIA_SOURCES:
            cur.execute(
                "INSERT INTO media_source(name, home, created_at) "
                "VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING",
                (src["name"], src["home"], now_iso()),
            )
            cur.execute(
                "SELECT id FROM media_source WHERE name=%s", (src["name"],)
            )
            row = cur.fetchone()
            src_id = row["id"]
            for col in src["columns"]:
                # UPSERT：同名栏目已存在时也同步 url/priority（config 是 source of truth）
                cur.execute(
                    "INSERT INTO media_column(source_id, name, url, priority) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (source_id, name) DO UPDATE SET "
                    "url=EXCLUDED.url, priority=EXCLUDED.priority",
                    (src_id, col["name"], col["url"], col["priority"]),
                )
        # 灌常规选题种子（首次初始化才插入，避免重复）
        cur.execute("SELECT COUNT(*) AS n FROM routine_calendar")
        n = cur.fetchone()["n"]
        if n == 0:
            for t in config.ROUTINE_TOPICS_SEED:
                cur.execute(
                    "INSERT INTO routine_calendar"
                    "(topic_name, start_month, start_day, end_month, end_day,"
                    " recommended_column, lead_days, note) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (t["name"], t["start_month"], t["start_day"],
                     t["end_month"], t["end_day"],
                     t["recommended_column"], t["lead_days"], t["note"]),
                )


# ---------- DAO: article ----------

def upsert_article(column_id: int, *, title: str, url: str, author: str | None,
                   publish_date: str | None, summary: str | None,
                   body_text: str | None, content_hash: str | None) -> bool:
    """插入新文章；URL 唯一约束命中则跳过。返回是否新增。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "INSERT INTO article"
            "(column_id, title, url, author, publish_date, summary, body_text,"
            " content_hash, crawled_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (url) DO NOTHING",
            (column_id, title, url, author, publish_date, summary,
             body_text, content_hash, now_iso()),
        )
        return cur.rowcount > 0


def fetch_unreviewed(limit: int = 50):
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.author, a.publish_date, a.summary, "
            "a.body_text, a.crawled_at, c.name AS column_name, s.name AS source_name "
            "FROM article a "
            "LEFT JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE r.id IS NULL ORDER BY a.crawled_at DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()


def fetch_reviewed(limit: int = 100):
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.publish_date, r.decision, r.note, "
            "r.reviewed_at, c.name AS column_name, s.name AS source_name "
            "FROM article a "
            "JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "ORDER BY r.reviewed_at DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()


def set_review(article_id: int, decision: str, note: str = "") -> None:
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "INSERT INTO review_record(article_id, decision, note, reviewed_at) "
            "VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (article_id) DO UPDATE SET "
            "decision=EXCLUDED.decision, note=EXCLUDED.note, "
            "reviewed_at=EXCLUDED.reviewed_at",
            (article_id, decision, note, now_iso()),
        )


# ---------- DAO: column / source ----------

def get_column_id(source_name: str, column_name: str) -> int | None:
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT col.id FROM media_column col "
            "JOIN media_source s ON s.id = col.source_id "
            "WHERE s.name=%s AND col.name=%s",
            (source_name, column_name),
        )
        row = cur.fetchone()
        return row["id"] if row else None


def list_columns_with_urls():
    """返回已配置 URL 的栏目（待爬）。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT col.id, col.name AS col_name, col.url, col.priority, "
            "s.name AS source_name, s.home "
            "FROM media_column col "
            "JOIN media_source s ON s.id = col.source_id "
            "WHERE col.url IS NOT NULL AND col.url <> '' "
            "ORDER BY col.priority DESC"
        )
        return cur.fetchall()


def stats_overview():
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute("SELECT COUNT(*) AS n FROM article")
        n_articles = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM article a LEFT JOIN review_record r "
            "ON r.article_id = a.id WHERE r.id IS NULL"
        )
        n_unreviewed = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM review_record WHERE decision='相关'"
        )
        n_relevant = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM review_record WHERE decision='借鉴'"
        )
        n_borrow = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM submission")
        n_submissions = cur.fetchone()["n"]
        cur.execute(
            "SELECT COUNT(*) AS n FROM submission WHERE result='录用'"
        )
        n_published = cur.fetchone()["n"]
    return {
        "总稿件": n_articles,
        "待审": n_unreviewed,
        "相关": n_relevant,
        "借鉴": n_borrow,
        "投稿次数": n_submissions,
        "录用次数": n_published,
    }


if __name__ == "__main__":
    init_db()
    print("DB 初始化完成（Supabase PostgreSQL）")
