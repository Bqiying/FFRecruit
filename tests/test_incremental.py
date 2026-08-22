"""端到端测试：export_incremental 导出 → import_incremental 导入"""
import os
import sys
import json
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

tmp = tempfile.mkdtemp()
db_src = os.path.join(tmp, "src.db")
db_dst = os.path.join(tmp, "dst.db")

# ── 源库（电脑端）：先放一条旧数据 ──
src = sqlite3.connect(db_src)
src.execute("""CREATE TABLE pf_history (
    listing_id TEXT PRIMARY KEY, datacenter TEXT, world TEXT, creator_name TEXT,
    home_world TEXT, category TEXT, duty_id INTEGER, duty_name TEXT, description TEXT,
    min_item_level INTEGER, has_password INTEGER, slots TEXT,
    first_seen_at TEXT, last_seen_at TEXT, is_closed INTEGER)""")
src.execute("INSERT INTO pf_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("1", "陆行鸟", "红玉海", "甲", "红玉海", "大型任务", 1, "副本A", "", 0, 0, "[]",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00", 1))
src.execute("INSERT INTO pf_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2", "陆行鸟", "神意之地", "乙", "神意之地", "高难", 2, "副本B", "绝龙诗", 0, 0, "[]",
             "2026-08-22T10:00:00", "2026-08-22T10:00:00", 0))
src.execute("INSERT INTO pf_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("3", "陆行鸟", "拉诺西亚", "丙", "拉诺西亚", "大型任务", 3, "副本C", "", 0, 0, "[]",
             "2026-08-22T09:00:00", "2026-08-22T09:00:00", 0))
# 模拟本轮：1、2 更新过；3 本轮没看到 → 关闭
src.execute("UPDATE pf_history SET last_seen_at='2026-08-22T10:05:00', is_closed=0 WHERE listing_id IN ('1','2')")
src.execute("UPDATE pf_history SET is_closed=1 WHERE listing_id='3'")
src.commit()

# ── 电脑端导出 ──
from scraper import export_incremental
export_path = os.path.join(tmp, "sync.json")
n = export_incremental(src, ["1", "2"], ["3"], export_path)
print("导出条数:", n)

# ── 服务器端导入（模拟「首次全量同步」后已有历史数据）──
import import_incremental as imp
imp.DATABASE = db_dst
import sys
dst0 = sqlite3.connect(db_dst)
imp.ensure_table(dst0)
# 首次全量：3 号曾在服务器库且是活跃的
dst0.execute("INSERT INTO pf_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("3", "陆行鸟", "拉诺西亚", "丙", "拉诺西亚", "大型任务", 3, "副本C", "", 0, 0, "[]",
              "2026-08-22T09:00:00", "2026-08-22T09:00:00", 0))
dst0.commit()
dst0.close()

sys.argv = ["import_incremental.py", export_path]
imp.main()

dst = sqlite3.connect(db_dst)
rows = dst.execute("SELECT listing_id, is_closed, last_seen_at FROM pf_history ORDER BY listing_id").fetchall()
print("服务器库结果:", rows)
expected = [("1", 0, "2026-08-22T10:05:00"), ("2", 0, "2026-08-22T10:05:00"), ("3", 1, "2026-08-22T09:00:00")]
print("导入验证:", "PASS" if rows == expected else "FAIL")
