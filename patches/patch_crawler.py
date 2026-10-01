"""一次性补丁：把 crawler.py 里 sqlite3 风格的 c.execute 改成 psycopg2 风格。

跑法（在 D:\\DPetroWorkbench 下）：
    python patches\\patch_crawler.py
"""
from pathlib import Path

TARGET = Path(r"D:\DPetroWorkbench\crawler.py")
OLD = (
    '    with db.get_conn() as c:\n'
    '        n = c.execute("SELECT COUNT(*) n FROM article").fetchone()["n"]\n'
)
NEW = (
    '    with db.get_conn() as c:\n'
    '        cur = db.conn_cursor(c)\n'
    '        n = cur.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"]\n'
)


def main():
    text = TARGET.read_text(encoding="utf-8")
    if OLD not in text:
        # 退化匹配：去掉 AS 的版本
        OLD2 = (
            '    with db.get_conn() as c:\n'
            '        n = c.execute("SELECT COUNT(*) AS n FROM article").fetchone()["n"]\n'
        )
        if OLD2 in text:
            print("已经打过补丁或语法稍有差异，跳过。")
            return
        print("未找到目标块，请手动检查 crawler.py 的 main() 函数。")
        return
    new_text = text.replace(OLD, NEW, 1)
    TARGET.write_text(new_text, encoding="utf-8")
    print(f"已写入：{TARGET}")


if __name__ == "__main__":
    main()
