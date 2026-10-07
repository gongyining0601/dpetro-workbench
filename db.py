"""PostgreSQL 建表 + 数据访问层（DAO）。

2026-09-29 改造（方案 A 上云版）：
- 从 SQLite 迁到 Supabase PostgreSQL（数据持久化到云端，多端访问）
- 表名 "column" 改名为 media_column（避免 PG 里 column 保留字反复加引号）
- 占位符 ? → %s（psycopg2 风格）
- INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING（PG 风格）
- AUTOINCREMENT → BIGSERIAL（PG 自增）
- 行对象用 RealDictCursor（行为兼容旧 sqlite3.Row 的 dict-like 访问）

2026-10-03 新增：app_setting 表（key-value），用于存储访问密码哈希等应用级配置。

所有 SQL 都用参数化绑定，杜绝注入；时间一律存 ISO 字符串。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

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
    decision    TEXT NOT NULL,   -- 保存 / 删除
    note        TEXT,
    reviewed_at TEXT NOT NULL,
    UNIQUE(article_id),          -- 每篇只保留最新一次审核结论
    FOREIGN KEY (article_id) REFERENCES article(id) ON DELETE CASCADE
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

-- 撰稿中心草稿库（2026-10-07 新增）
-- 此前稿件只存在浏览器内存（st.session_state），刷新/关页即丢且无历史。
-- 建表语句必须放在这里，保证新环境（新库/新容器）首次启动即自动建表；
-- 否则 app.py 调用 db.list_drafts 会因缺表直接报错。
CREATE TABLE IF NOT EXISTS draft (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT,
    body            TEXT,
    topic           TEXT,
    angle           TEXT,
    target_media    TEXT,
    status          TEXT DEFAULT 'draft',
    created_at      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_updated_at ON draft(updated_at DESC);

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

-- 应用级配置（key-value）：目前用于存储访问密码哈希
-- 单条记录保证"唯一性"（只有一个 access_password）
CREATE TABLE IF NOT EXISTS app_setting (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """返回 UTC 时间的 ISO 字符串，确保跨环境（本地/Streamlit/GitHub Actions）一致。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reset_pool():
    """销毁并重建连接池。用于所有连接被服务器断开后恢复。"""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
    _pool = None


@contextmanager
def get_conn():
    """从连接池获取连接；自动提交/回滚，用完归还池。

    健壮性：Supabase 空闲超时会关闭连接，ThreadedConnectionPool 不会自动重建。
    因此检测到连接断开时，不仅移除该连接，还会重建整个池。
    """
    pool = _get_pool()
    conn = pool.getconn()
    # 健康检查：连接断开则丢弃
    if conn.closed:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        # 重新获取；若仍断开，说明池已整体失效，重建池
        conn = pool.getconn()
        if conn.closed:
            _reset_pool()
            pool = _get_pool()
            conn = pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        if not conn.closed:
            conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # 连接在使用中被断开：重建池后重抛，让上层重试
        _reset_pool()
        raise
    except Exception:
        if not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn.closed:
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
        else:
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
        # 增量字段迁移（已有表）
        cur.execute("ALTER TABLE article ADD COLUMN IF NOT EXISTS has_image BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE article ADD COLUMN IF NOT EXISTS image_urls TEXT")
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
                   body_text: str | None, content_hash: str | None,
                   has_image: bool = False, image_urls: str | None = None,
                   ai_pending: bool = False) -> bool:
    """插入新文章；URL 唯一约束命中则跳过。返回是否新增。

    ai_pending=True 表示 AI 过滤失败、未能判定分类，稿件仍入库并标记待人工确认，
    避免因 API 故障导致稿件永久丢失（第四次测评 P0 修复）。
    """
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "INSERT INTO article"
            "(column_id, title, url, author, publish_date, summary, body_text,"
            " content_hash, crawled_at, has_image, image_urls, ai_pending)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (url) DO NOTHING",
            (column_id, title, url, author, publish_date, summary,
             body_text, content_hash, now_iso(), has_image, image_urls, ai_pending),
        )
        return cur.rowcount > 0


def fetch_unreviewed(limit: int = 50):
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.author, a.publish_date, a.summary, "
            "LEFT(a.body_text, 500) AS body_text, a.crawled_at, a.has_image, a.image_urls, "
            "a.ai_pending, "
            "c.name AS column_name, s.name AS source_name "
            "FROM article a "
            "LEFT JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE r.id IS NULL ORDER BY a.crawled_at DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()



def fetch_image_articles(limit: int = 200):
    """图文素材库（有图片的稿件，不限行业）。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.author, a.publish_date, a.summary, "
            "LEFT(a.body_text, 500) AS body_text, a.crawled_at, a.image_urls, "
            "c.name AS column_name, s.name AS source_name "
            "FROM article a "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE a.has_image = TRUE ORDER BY a.crawled_at DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()
def fetch_reviewed(limit: int = 100):
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT a.id, a.title, a.url, a.publish_date, a.body_text, a.image_urls, r.decision, r.note, "
            "r.reviewed_at, c.name AS column_name, s.name AS source_name "
            "FROM article a "
            "JOIN review_record r ON r.article_id = a.id "
            "LEFT JOIN media_column c ON c.id = a.column_id "
            "LEFT JOIN media_source s ON s.id = c.source_id "
            "WHERE r.decision='保存' "
            "ORDER BY r.reviewed_at DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()


def set_review(article_id: int, decision: str, note: str = "") -> None:
    """审核决策二元化：保存 / 删除。

    保存：写入 review_record(decision='保存')，article 行保留，进入语义索引供选题对标。
    删除：硬删 article 行（CASCADE 自动删 review_record + article_embedding），不可恢复。
    「今日审核」用 LEFT JOIN review_record WHERE r.id IS NULL 找未审核，
    保存/删除后 article 不再无 review_record，自动从待审列表消失。
    """
    with get_conn() as c:
        cur = conn_cursor(c)
        if decision == "保存":
            cur.execute(
                "INSERT INTO review_record(article_id, decision, note, reviewed_at) "
                "VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (article_id) DO UPDATE SET "
                "decision=EXCLUDED.decision, note=EXCLUDED.note, "
                "reviewed_at=EXCLUDED.reviewed_at",
                (article_id, decision, note, now_iso()),
            )
        elif decision == "删除":
            # 硬删 article；review_record + article_embedding 由外键 CASCADE 自动清理
            cur.execute("DELETE FROM article WHERE id=%s", (article_id,))


def set_review_batch(article_ids: list[int], decision: str, note: str = "") -> int:
    """批量审核：一次 SQL 处理多条，避免单条往返。

    保存：UNNEST 构造 VALUES 多行 + ON CONFLICT DO UPDATE
    删除：DELETE WHERE id = ANY(...)，外键 CASCADE 自动清 review_record + embedding
    返回影响行数。空列表直接返回 0。
    """
    if not article_ids:
        return 0
    with get_conn() as c:
        cur = conn_cursor(c)
        if decision == "保存":
            # UNNEST 把数组展开成多行，一次 INSERT ... ON CONFLICT 完成
            cur.execute(
                "INSERT INTO review_record(article_id, decision, note, reviewed_at) "
                "SELECT id, %s, %s, %s FROM UNNEST(%s::bigint[]) AS t(id) "
                "ON CONFLICT (article_id) DO UPDATE SET "
                "decision=EXCLUDED.decision, note=EXCLUDED.note, "
                "reviewed_at=EXCLUDED.reviewed_at",
                (decision, note, now_iso(), list(article_ids)),
            )
        elif decision == "删除":
            cur.execute(
                "DELETE FROM article WHERE id = ANY(%s::bigint[])",
                (list(article_ids),),
            )
        return cur.rowcount


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
    """合并为单条 SQL：6 个子查询一次网络往返返回全部指标。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM article) AS total_articles, "
            "(SELECT COUNT(*) FROM article a "
            " LEFT JOIN review_record r ON r.article_id = a.id "
            " WHERE r.id IS NULL) AS unreviewed, "
            "(SELECT COUNT(*) FROM review_record WHERE decision='保存') AS saved, "
            "(SELECT COUNT(*) FROM submission) AS submissions, "
            "(SELECT COUNT(*) FROM submission WHERE result='录用') AS published"
        )
        row = cur.fetchone()
    return {
        "总稿件": row["total_articles"],
        "待审": row["unreviewed"],
        "保存": row["saved"],
        "投稿次数": row["submissions"],
        "录用次数": row["published"],
    }


# ---------- DAO: app_setting (key-value) ----------

def get_setting(key: str) -> str | None:
    """读取应用配置值；不存在返回 None。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute("SELECT value FROM app_setting WHERE key=%s", (key,))
        row = cur.fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    """写入应用配置（UPSERT）。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "INSERT INTO app_setting(key, value, updated_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
            (key, value, now_iso()),
        )


if __name__ == "__main__":
    init_db()
    print("DB 初始化完成（Supabase PostgreSQL）")


def cleanup_old_unreviewed(days: int = 90) -> int:
    """清理超过指定天数的未审核稿件，防止数据库无限增长。

    通过 LEFT JOIN review_record WHERE r.id IS NULL 判定"未审核"，
    publish_date 用 ISO 日期字符串比较（与 now_iso 一致）。
    article_embedding 已有 ON DELETE CASCADE，删 article 时自动连带删。
    失败时抛异常由上层 try/except 兜住，不阻塞 init_db。
    """
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    deleted = 0
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT a.id FROM article a "
            "LEFT JOIN review_record r ON r.article_id = a.id "
            "WHERE r.id IS NULL "
            "AND a.publish_date IS NOT NULL "
            "AND a.publish_date <> '' "
            "AND a.publish_date < %s",
            (cutoff_iso,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        for aid in ids:
            # article_embedding ON DELETE CASCADE 会自动连带删；
            # review_record 对未审核稿件本就不存在，保险起见显式删一次（不报错）
            cur.execute("DELETE FROM review_record WHERE article_id = %s", (aid,))
            cur.execute("DELETE FROM article WHERE id = %s", (aid,))
            deleted += 1
    if deleted:
        config.logger.info(f"清理 {deleted} 条超过 {days} 天的未审核稿件")
    return deleted


def cleanup_non_photo_news() -> int:
    """一次性清理历史"非严格图片新闻"或元数据错误的稿件。

    删除条件（OR）：
    - has_image = FALSE（旧规则遗漏的纯文字稿，保险再清一次）
    - body_text 长度 > 300 字（说明是长正文被错存，新规则只存 ≤200 字图注）
    - author 不含中文字符（如 ccin 的 "TOPQH" 站点 ID 误识为作者）
    - publish_date 为空（旧爬虫未提取到日期的稿件）

    删除后跑 `python crawler.py` 重爬会用新 Fix 4 逻辑拿到正确 author/date。
    返回删除条数。article_embedding + review_record 由外键 CASCADE 自动清。
    """
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "DELETE FROM article "
            "WHERE has_image = FALSE "
            "OR (body_text IS NOT NULL AND LENGTH(body_text) > 300) "
            "OR (author IS NOT NULL AND author !~ '[\\u4e00-\\u9fa5]') "
            "OR (publish_date IS NULL OR publish_date = '')"
        )
        deleted = cur.rowcount
    if deleted:
        config.logger.info(f"清理 {deleted} 条历史问题稿件（非图片新闻/坏元数据）")
    return deleted


# ---------------- 撰稿草稿（draft 表） ----------------
# 背景：撰稿中心此前只把稿件放在 st.session_state（浏览器内存），
# 刷新/关闭/换设备即丢失，且无历史可查。此处落到数据库，支持自动保存与回溯。

def save_draft(*, title: str, body: str, topic: str = "", angle: str = "",
               target_media: str = "", draft_id: int | None = None,
               status: str = "draft") -> int | None:
    """保存草稿。传 draft_id 则更新，否则新建。返回草稿 id，失败返回 None。

    撰稿中心的自动保存与本函数的 update 分支配合，实现"边写边存"。
    """
    ts = now_iso()
    try:
        with get_conn() as c:
            cur = conn_cursor(c)
            if draft_id:
                cur.execute(
                    "UPDATE draft SET title=%s, body=%s, topic=%s, angle=%s,"
                    " target_media=%s, status=%s, updated_at=%s WHERE id=%s",
                    (title, body, topic, angle, target_media, status, ts, draft_id),
                )
                if cur.rowcount:
                    return draft_id
                # id 不存在（可能已被删除）→ 退回新建
            cur.execute(
                "INSERT INTO draft(title, body, topic, angle, target_media,"
                " status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (title, body, topic, angle, target_media, status, ts, ts),
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None
    except Exception as e:
        config.logger.error(f"保存草稿失败：{e}")
        return None


def list_drafts(limit: int = 50) -> list:
    """草稿列表，最近修改的在前。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT id, title, body, topic, angle, target_media, status,"
            " created_at, updated_at FROM draft ORDER BY updated_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def get_draft(draft_id: int):
    """读取单条草稿，不存在返回 None。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute(
            "SELECT id, title, body, topic, angle, target_media, status,"
            " created_at, updated_at FROM draft WHERE id=%s", (draft_id,)
        )
        return cur.fetchone()


def delete_draft(draft_id: int) -> bool:
    """删除草稿。"""
    with get_conn() as c:
        cur = conn_cursor(c)
        cur.execute("DELETE FROM draft WHERE id=%s", (draft_id,))
        return cur.rowcount > 0
