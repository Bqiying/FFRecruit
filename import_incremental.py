"""
FFRecruit 增量导入脚本（服务器端专用）
读取电脑端 scraper.py --once --export 导出的增量 JSON，导入服务器本地数据库。

用法:
  python3 import_incremental.py sync.json

说明:
  - 服务器只跑 main.py（网站 + 查询），不跑爬虫
  - 电脑端抓取后导出增量文件，scp 上传到服务器后运行本脚本导入
  - items       : 新增/更新的招募完整行（INSERT OR REPLACE）
  - closed_ids  : 本轮被关闭的招募 ID（标记 is_closed = 1）
"""

import os
import sys
import json
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "ffrecruit.db")

COLS = [
    "listing_id", "datacenter", "world", "creator_name", "home_world", "category",
    "duty_id", "duty_name", "description", "min_item_level",
    "has_password", "slots", "first_seen_at", "last_seen_at", "is_closed",
]


def ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pf_history (
            listing_id      TEXT PRIMARY KEY,
            datacenter      TEXT NOT NULL,
            world           TEXT NOT NULL,
            creator_name    TEXT NOT NULL,
            home_world      TEXT NOT NULL DEFAULT '',
            category        TEXT NOT NULL DEFAULT '',
            duty_id         INTEGER NOT NULL DEFAULT 0,
            duty_name       TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            min_item_level  INTEGER NOT NULL DEFAULT 0,
            has_password    INTEGER NOT NULL DEFAULT 0,
            slots           TEXT NOT NULL DEFAULT '[]',
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            is_closed       INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def main():
    if len(sys.argv) < 2:
        print("用法: python3 import_incremental.py <增量文件.json>")
        sys.exit(1)

    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"[ERROR] 文件不存在: {path}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    closed_ids = data.get("closed_ids", [])

    conn = sqlite3.connect(DATABASE)
    ensure_table(conn)
    cur = conn.cursor()

    placeholders = ", ".join("?" * len(COLS))
    upsert_sql = f"INSERT OR REPLACE INTO pf_history ({', '.join(COLS)}) VALUES ({placeholders})"

    n_new = 0
    n_upd = 0
    for it in items:
        row = [it.get(c) for c in COLS]
        if row[0] is None:
            continue
        existed = cur.execute("SELECT 1 FROM pf_history WHERE listing_id = ?", (row[0],)).fetchone()
        cur.execute(upsert_sql, row)
        if existed:
            n_upd += 1
        else:
            n_new += 1

    n_closed = 0
    for lid in closed_ids:
        cur.execute("UPDATE pf_history SET is_closed = 1 WHERE listing_id = ? AND is_closed = 0", (lid,))
        n_closed += cur.rowcount

    conn.commit()
    cur.close()
    conn.close()

    print(f"[OK] 导入完成: 新增 {n_new} | 更新 {n_upd} | 关闭 {n_closed}")
    print(f"     数据源: {path}")


if __name__ == "__main__":
    main()
