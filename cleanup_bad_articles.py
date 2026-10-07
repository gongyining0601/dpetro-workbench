"""清理误入库的农业/社会类文章。

删除 article + article_embedding 中标题匹配的记录。
由 GitHub Actions 临时 workflow 调用，自动使用 DATABASE_URL secret。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db

# 误入库的文章标题（上一轮硬规则误召回）
BAD_TITLES = [
    "青果压枝头 村民有奔头",
    "辽宁省暨沈阳市烈士纪念日向英雄烈士敬献花篮仪式举行",
    "海外名校求学十一载，他回鲅鱼圈卖葡萄",
    "广州归来，他在辽东山村种出一片药香",
]


def main():
    db.init_db()
    with db.get_conn() as c:
        cur = c.cursor()
        total_emb = 0
        total_art = 0
        for title in BAD_TITLES:
            # 先删 embedding（外键依赖）
            cur.execute(
                "DELETE FROM article_embedding WHERE article_id IN "
                "(SELECT id FROM article WHERE title = %s) RETURNING article_id",
                (title,),
            )
            emb_deleted = cur.rowcount
            # 再删 article
            cur.execute("DELETE FROM article WHERE title = %s", (title,))
            art_deleted = cur.rowcount
            total_emb += emb_deleted
            total_art += art_deleted
            status = "已删除" if art_deleted else "未找到"
            print(f"  [{status}] {title!r} (article={art_deleted}, embedding={emb_deleted})")
        c.commit()
    print(f"\n清理完成：删除 article={total_art}, article_embedding={total_emb}")


if __name__ == "__main__":
    main()
