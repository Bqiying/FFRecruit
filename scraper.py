"""
FFXIV 国服招募板爬虫
直接调用 xivpf.littlenightmare.top API 获取数据
- 默认每 90 秒爬一次（含每轮 1-3 秒随机延迟）
- 失败自动重试 + 智能退避（连续失败时逐级放大等待，避免上游故障时被误判）
- 复用连接减少 TLS 握手，降低被风控注意的概率
- 自动获取全部分页
- 存入 SQLite 数据库
- 控制台实时显示新招募

用法:
  python scraper.py              # 持续运行
  python scraper.py --once       # 只爬一次
  python scraper.py --pages 3    # 只爬前 3 页
"""

import os
import sys
import json
import time
import random
import sqlite3
import argparse
from datetime import datetime
from typing import Optional

# 自动安装依赖
def ensure_deps():
    try:
        import httpx
    except ImportError:
        print("正在安装依赖: httpx...")
        os.system(f'"{sys.executable}" -m pip install httpx -q')

ensure_deps()
import httpx

# ─────────────── 配置加载 ───────────────
# 优先从 config.json 读取，其次从 config.example.json 读取默认值

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_config() -> dict:
    """加载配置文件：优先 config.json，其次 config.example.json"""
    config_path = os.path.join(BASE_DIR, "config.json")
    example_path = os.path.join(BASE_DIR, "config.example.json")

    cfg = {}
    used_file = None
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            used_file = "config.json"
        except Exception as e:
            print(f"[WARN] 读取 config.json 失败，回退到示例配置: {e}")

    if not cfg and os.path.isfile(example_path):
        try:
            with open(example_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            used_file = "config.example.json (默认)"
        except Exception as e:
            print(f"[WARN] 读取示例配置也失败: {e}")

    return cfg

CONFIG = _load_config()
_API_CFG = CONFIG.get("api", {}) if isinstance(CONFIG, dict) else {}
_SCR_CFG = CONFIG.get("scraper", {}) if isinstance(CONFIG, dict) else {}

# 解析并给出默认值
UA_PROJECT = _API_CFG.get("user_agent_project") or "FFXIV-PF-History-Bot"
UA_EMAIL = _API_CFG.get("contact_email") or ""
REFERER = _API_CFG.get("referer") or "https://xivpf.ff14.xin/"
API_URL = _API_CFG.get("api_url") or "https://xivpf.littlenightmare.top/api/listings"
DEFAULT_INTERVAL = int(_SCR_CFG.get("interval_seconds") or 90)
# 每轮轮询之间的随机抖动延迟（秒），避免固定节奏被上游识别
JITTER_MIN = 1
JITTER_MAX = 3
# 失败退避：连续失败时等待时间逐级放大（秒），避免上游故障期间高频轰炸被误判为攻击
# 成功一轮后自动重置为正常间隔；封顶 1 小时
BACKOFF_STEPS = [300, 900, 1800, 3600]  # 5分钟 → 15分钟 → 30分钟 → 1小时
# 单页请求失败后的重试次数（间隔递增）
PAGE_RETRIES = 2
PER_PAGE = int(_SCR_CFG.get("per_page") or 100)

# 拼接 User-Agent（符合上游要求：项目名 + (contact: 邮箱)）
if UA_EMAIL and not UA_EMAIL.lower().startswith("your-email@") and "@" in UA_EMAIL:
    _UA_STRING = f"{UA_PROJECT} (contact: {UA_EMAIL})"
else:
    # 邮箱未正确配置：打印醒目警告
    _UA_STRING = UA_PROJECT
    print("\n" + "=" * 78)
    print("  ⚠  【重要】尚未正确配置 User-Agent 联系方式！")
    print("     上游作者要求：调用 API 必须提供可联系的邮箱。")
    print("     请按以下步骤操作：")
    print("       1. 复制 config.example.json 并重命名为 config.json")
    print(f"       2. 修改 contact_email 为您的真实邮箱（当前值: '{UA_EMAIL or '空'}'）")
    print("       3. 保存后重新运行")
    print("     本次启动仍将继续，但可能被上游拒绝请求。")
    print("=" * 78 + "\n")
    time.sleep(2)

HEADERS = {
    "User-Agent": _UA_STRING,
    "Accept": "application/json",
    "Referer": REFERER,
}

DATABASE = os.path.join(BASE_DIR, "ffrecruit.db")

# ─────────────── 数据库 ───────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
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
    # 兼容旧数据库，缺失字段时 ALTER TABLE
    try:
        conn.execute("ALTER TABLE pf_history ADD COLUMN home_world TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE pf_history ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_datacenter ON pf_history (datacenter)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_closed ON pf_history (is_closed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lastseen ON pf_history (last_seen_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON pf_history (category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_home_world ON pf_history (home_world)")

    # 迁移（v1.1 → v1.2）：
    # 之前错误地把繁中服「陸行鳥」全部规范化为简体「陆行鸟」，导致繁中服务器伊弗利特/利維坦
    # 等和国服陆行鸟区混在一起。这里修正：只要 home_world 或 world 是 TW_WORLD_NAMES
    # 中的一个，就把它的 datacenter 改回繁体区分开
    tw_places = ",".join("?" * len(TW_WORLD_NAMES))
    cur = conn.cursor()
    # 步骤 1：取消之前"全部把陸行鳥→陆行鸟"的迁移（对 TW 服务器而言）
    try:
        cur.execute(f"""
            UPDATE pf_history SET datacenter = '陸行鳥'
            WHERE (datacenter = '陆行鸟' OR datacenter = '陸行鳥')
              AND (home_world IN ({tw_places}) OR world IN ({tw_places}))
        """, list(TW_WORLD_NAMES) + list(TW_WORLD_NAMES))
        cur.execute(f"""
            UPDATE pf_history SET datacenter = '貓小胖'
            WHERE datacenter = '猫小胖'
              AND (home_world IN ({tw_places}) OR world IN ({tw_places}))
        """, list(TW_WORLD_NAMES) + list(TW_WORLD_NAMES))
    except Exception as e:
        print(f"[WARN] TW 数据修正迁移失败: {e}")
    finally:
        cur.close()

    conn.commit()
    return conn


def upsert_listing(conn: sqlite3.Connection, item: dict) -> str:
    """插入或更新招募，返回 'new' 或 'updated'"""
    now = datetime.now().isoformat()
    lid = item["listing_id"]
    
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pf_history WHERE listing_id = ?", (lid,))
    exists = cur.fetchone()
    
    slots_json = json.dumps(item.get("slots", []), ensure_ascii=False)
    
    if exists:
        cur.execute("""
            UPDATE pf_history 
            SET last_seen_at = ?, slots = ?, is_closed = 0, category = ?, home_world = ?
            WHERE listing_id = ?
        """, (now, slots_json, item.get("category", ""), item.get("home_world", ""), lid))
        conn.commit()
        return "updated"
    else:
        cur.execute("""
            INSERT INTO pf_history (
                listing_id, datacenter, world, creator_name, home_world, category,
                duty_id, duty_name, description, min_item_level,
                has_password, slots, first_seen_at, last_seen_at, is_closed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            lid,
            item.get("datacenter", ""),
            item.get("world", ""),
            item.get("creator_name", ""),
            item.get("home_world", ""),
            item.get("category", ""),
            item.get("duty_id", 0),
            item.get("duty_name", ""),
            item.get("description", ""),
            item.get("min_item_level", 0),
            1 if item.get("has_password") else 0,
            slots_json,
            now,
            now,
        ))
        conn.commit()
        return "new"


def close_stale_listings(conn: sqlite3.Connection, active_ids: set):
    """
    将不在当前列表中的活跃招募标记为已关闭
    分批处理（每批 500 个），避免 SQLite NOT IN 参数上限（默认 999 个）导致更新失败
    """
    cur = conn.cursor()
    id_list = list(active_ids)

    if not id_list:
        # 本轮完全没抓到数据 -> 所有活跃的全关
        cur.execute("UPDATE pf_history SET is_closed = 1 WHERE is_closed = 0")
        conn.commit()
        return

    BATCH_SIZE = 500
    # 为避免分批 NOT IN 时上一批已经关闭的 listing 被下一批"复活"
    # 做法：先把所有活跃 listing_id 写入临时表，再用 NOT EXISTS 关联更新
    tmp_table = "_tmp_active_ids_" + str(int(time.time() * 1000))
    cur.execute(f"CREATE TEMP TABLE {tmp_table} (listing_id TEXT PRIMARY KEY)")
    # 分批 INSERT 到临时表（也避免 INSERT 参数上限）
    for i in range(0, len(id_list), BATCH_SIZE):
        batch = id_list[i:i + BATCH_SIZE]
        placeholders = ",".join("?" * len(batch))
        cur.execute(
            f"INSERT OR IGNORE INTO {tmp_table} (listing_id) VALUES {','.join(['(?)' for _ in batch])}",
            batch,
        )
    cur.execute(f"""
        UPDATE pf_history SET is_closed = 1
        WHERE is_closed = 0
          AND NOT EXISTS (
              SELECT 1 FROM {tmp_table} t
              WHERE t.listing_id = pf_history.listing_id
          )
    """)
    cur.execute(f"DROP TABLE IF EXISTS {tmp_table}")
    conn.commit()


def enforce_one_active_per_creator(conn: sqlite3.Connection):
    """
    业务规则兜底：同一个角色（creator_name + home_world）只能有 1 个招募进行中。
    每个角色只保留 last_seen_at 最新的那一条为 is_closed=0，
    其他所有同角色的 is_closed=0 招募全部强制关闭。
    """
    cur = conn.cursor()
    cur.execute("""
        UPDATE pf_history
        SET is_closed = 1
        WHERE is_closed = 0
          AND rowid NOT IN (
              -- 每个角色（creator_name + home_world）只留 last_seen_at 最大的那一条
              SELECT keeper_rowid FROM (
                  SELECT MAX(rowid) AS keeper_rowid
                  FROM pf_history
                  WHERE is_closed = 0
                  GROUP BY
                      creator_name,
                      home_world,
                      -- last_seen_at 最新的那条作为 keeper
                      (SELECT MAX(last_seen_at)
                       FROM pf_history h2
                       WHERE h2.is_closed = 0
                         AND h2.creator_name = pf_history.creator_name
                         AND h2.home_world  = pf_history.home_world)
              )
          )
    """)
    updated = cur.rowcount
    conn.commit()
    return updated


# ─────────────── API 调用 ───────────────

def fetch_page(client: httpx.Client, page: int, per_page: int = PER_PAGE) -> Optional[dict]:
    """获取单页数据，失败自动重试（间隔递增），避免单次抖动直接放弃整轮"""
    for attempt in range(PAGE_RETRIES + 1):
        try:
            resp = client.get(
                API_URL,
                params={"page": page, "per_page": per_page},
                headers=HEADERS,
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < PAGE_RETRIES:
                wait = 2 * (attempt + 1)
                print(f"    ⚠ 第 {page} 页请求失败(第{attempt + 1}次重试): {e}，{wait} 秒后重试")
                time.sleep(wait)
            else:
                print(f"    ⚠ 第 {page} 页请求失败(已重试 {PAGE_RETRIES} 次): {e}")
                return None
    return None


def compute_wait(interval: int, consecutive_failures: int) -> int:
    """计算下一轮等待秒数：连续失败时按 BACKOFF_STEPS 逐级退避；成功时用正常间隔+随机抖动"""
    if consecutive_failures <= 0:
        return interval + random.randint(JITTER_MIN, JITTER_MAX)
    step = min(consecutive_failures - 1, len(BACKOFF_STEPS) - 1)
    return BACKOFF_STEPS[step] + random.randint(JITTER_MIN, JITTER_MAX)


def fetch_all_pages(client: httpx.Client, max_pages: Optional[int] = None) -> list:
    """获取所有页面数据"""
    # 先获取第一页，得到总页数
    first_page = fetch_page(client, 1)
    if not first_page:
        return []
    
    pagination = first_page.get("pagination", {})
    total_pages = max_pages or pagination.get("total_pages", 1)
    total = pagination.get("total", 0)
    
    print(f"  总招募数: {total}, 总页数: {total_pages}")
    
    all_items = []
    items = first_page.get("data", [])
    all_items.extend(items)
    print(f"    第 1/{total_pages} 页: {len(items)} 条")
    
    for page in range(2, total_pages + 1):
        data = fetch_page(client, page)
        if data:
            items = data.get("data", [])
            all_items.extend(items)
            print(f"    第 {page}/{total_pages} 页: {len(items)} 条")
        
        # 礼貌延迟
        time.sleep(0.2)
    
    return all_items


# 任务类型中英对照
CATEGORY_MAP = {
    "None": "其他招募",
    "DutyRoulette": "随机任务",
    "Dungeons": "迷宫挑战",
    "Guildhests": "行会令",
    "Trials": "讨伐歼灭战",
    "HighEndDuty": "高难度任务",
    "Raids": "大型任务",
    "Pvp": "玩家对战",
    "Fates": "危命任务",
    "TreasureHunt": "寻宝",
    "Hunt": "怪物狩猎",
    "Gathering": "采集活动",
    "DeepDungeon": "深层迷宫",
    "AdventuringForays": "特殊场景探索",
    "VariantAndCriterion": "特殊迷宫探索",
    "GoldSaucer": "金碟游乐场",
}
# 大区繁体→简体 规范化
DATACENTER_NORMALIZE = {
    # 只规范化"写法不标准但本质是国服大区"的名字
    # 繁中服「陸行鳥」保留原写法，以便 exclude_tw 过滤区分
    "陸行鸟": "陆行鸟",    # 混写 → 简体国服
    "貓小胖": "猫小胖",
}

# 国服 4 大区简体名（用于 TW 服务器归属判断时的反向修正）
CN_DATACENTERS = {"陆行鸟", "莫古力", "猫小胖", "豆豆柴"}
# 繁中服务器名（完全匹配）
TW_WORLD_NAMES = {"伊弗利特", "迦樓羅", "利維坦", "鳳凰", "奧汀", "奥汀", "巴哈姆特", "泰坦"}


def normalize_datacenter(dc: str) -> str:
    if not dc:
        return ""
    return DATACENTER_NORMALIZE.get(dc, dc)


def convert_api_item(api_item: dict) -> dict:
    """将 API 返回的数据转换为内部格式"""
    # slots_filled = 已入队人数, slots_available = 队伍总人数（上限）
    slots_filled = api_item.get("slots_filled", 0)
    total_slots = api_item.get("slots_available", 0)

    # 根据人数填充 slots
    slots = []
    if total_slots > 0:
        roles_pool = ["T", "H", "DPS", "DPS", "DPS", "DPS", "T", "H"]
        for i in range(total_slots):
            slots.append({
                "role": roles_pool[i % len(roles_pool)],
                "status": "filled" if i < slots_filled else "empty"
            })

    duty_name = api_item.get("duty", "")
    if duty_name == "无" or duty_name == "未指定":
        duty_name = ""

    cat_en = api_item.get("category", "") or ""
    cat_cn = CATEGORY_MAP.get(cat_en, cat_en if cat_en else "其他招募")

    home_world = api_item.get("home_world", "") or ""
    created_world = api_item.get("created_world", "") or ""

    # 关键判断：如果服务器属于繁中服（伊弗利特等），就算 datacenter 被上游 API 写成简体也要保留区分
    raw_dc = normalize_datacenter(api_item.get("datacenter", ""))
    server_any = home_world or created_world
    is_tw_world = server_any in TW_WORLD_NAMES
    # 繁中服服务器的 datacenter 强制恢复为繁体写法（或保留），避免和国服陆行鸟混淆
    if is_tw_world and raw_dc in CN_DATACENTERS:
        final_dc = {
            "陆行鸟": "陸行鳥",
            "莫古力": "莫古力",
            "猫小胖": "貓小胖",
            "豆豆柴": "豆豆柴",
        }.get(raw_dc, raw_dc)
    else:
        final_dc = raw_dc

    return {
        "listing_id": str(api_item.get("id", "")),
        "creator_name": api_item.get("name", ""),
        "datacenter": final_dc,
        "world": created_world or home_world,    # 招募发布所在服务器
        "home_world": home_world,                  # 队长所属服务器
        "category": cat_cn,
        "duty_id": api_item.get("duty_id", 0) or 0,
        "duty_name": duty_name,
        "description": api_item.get("description", ""),
        "slots": slots,
        "has_password": False,
        "min_item_level": api_item.get("min_item_level", 0) or 0,
    }


def print_item(item: dict, is_new: bool = False):
    """在控制台打印一条招募"""
    tag = "\033[93m✨ NEW\033[0m " if is_new else "      "
    
    dc = item.get("datacenter", "?")
    world = item.get("world", "?")
    creator = item.get("creator_name", "?")
    home = item.get("home_world", "") or world
    duty = (item.get("duty_name", "") or "未指定")[:12]
    desc = item.get("description", "")[:35]
    slots = item.get("slots", [])
    filled = sum(1 for s in slots if s.get("status") == "filled")
    total = len(slots)
    slots_str = f"[{filled}/{total}]" if total > 0 else ""
    
    line = f"{tag}\033[96m{dc:6s}\033[0m | \033[97m{world:10s}\033[0m | \033[92m{creator:14s}\033[33m@{home:10s}\033[0m | \033[95m{duty:12s}\033[0m | \033[94m{slots_str:8s}\033[0m | {desc}"
    print(line)


def main():
    parser = argparse.ArgumentParser(description="FFXIV 国服招募板爬虫")
    parser.add_argument("--once", action="store_true", help="只爬一次然后退出")
    parser.add_argument("--pages", type=int, help="限制爬取页数")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="轮询间隔（秒）")
    args = parser.parse_args()
    
    print("\033[96m" + "=" * 110 + "\033[0m")
    print("\033[1;96m  FFXIV 国服招募板爬虫 (xivpf.littlenightmare.top API)\033[0m")
    print("\033[96m" + "=" * 110 + "\033[0m")
    print(f"  API:         {API_URL}")
    print(f"  数据库:      {DATABASE}")
    print(f"  轮询间隔:    {args.interval} 秒 (+ 随机 {JITTER_MIN}-{JITTER_MAX} 秒延迟)")
    print(f"  失败退避:    连续失败等待 {BACKOFF_STEPS[0]}s → {BACKOFF_STEPS[-1]}s 逐级放大，成功自动复位")
    print(f"  每页数量:    {PER_PAGE}")
    print(f"  限制页数:    {args.pages or '自动（全量）'}")
    print(f"  User-Agent:  {_UA_STRING}")
    print()
    
    conn = get_db()
    # 复用连接（keep-alive），避免每轮重新 TLS 握手，降低被上游风控注意的概率
    client = httpx.Client(timeout=60)
    consecutive_failures = 0  # 连续失败次数，用于智能退避
    
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n\033[96m[{now}] ═══ 开始新一轮爬取 ═══\033[0m")
        
        # 每轮重置：只保留「本轮真正获取到的」活跃 ID
        seen_ids = set()
        
        try:
            raw_items = fetch_all_pages(client, max_pages=args.pages)
        except Exception as e:
            import traceback
            print(f"\033[91m  爬取异常: {e}\033[0m")
            traceback.print_exc()
            consecutive_failures += 1
            wait = compute_wait(args.interval, consecutive_failures)
            print(f"  \033[93m连续失败 {consecutive_failures} 次，退避 {wait} 秒后重试\033[0m")
            time.sleep(wait)
            continue
        
        if not raw_items:
            print(f"  \033[93m未获取到任何数据\033[0m")
            if args.once:
                break
            consecutive_failures += 1
            wait = compute_wait(args.interval, consecutive_failures)
            print(f"  \033[93m连续失败 {consecutive_failures} 次，退避 {wait} 秒后重试\033[0m")
            time.sleep(wait)
            continue
        
        # 本轮成功，重置失败计数
        consecutive_failures = 0
        
        # 转换数据并写入数据库
        new_items = []
        updated_items = []
        for raw in raw_items:
            item = convert_api_item(raw)
            # 先记录本轮看到的活跃 ID（即使后续 upsert 失败，也认为它在上游是活跃的）
            seen_ids.add(item["listing_id"])
            try:
                status = upsert_listing(conn, item)
                if status == "new":
                    new_items.append(item)
                elif status == "updated":
                    updated_items.append(item)
            except Exception as e:
                print(f"    \033[91m写入失败: {item.get('listing_id')} - {e}\033[0m")
        
        # ① 关闭不在当前列表中的招募（本轮没看到的 = 已经给上游下架了）
        close_stale_listings(conn, seen_ids)
        
        # ② 业务规则兜底：同一个角色（creator_name + home_world）只能有 1招募进行中
        #    防止上游 API 短暂延迟/重复数据导致的同角色多招募同时在线
        closed_extra = enforce_one_active_per_creator(conn)
        if closed_extra > 0:
            print(f"  \033[93m[规则兜底] 强制关闭 {closed_extra} 条同角色重复招募\033[0m")
        
        # 统计
        total_active = conn.execute("SELECT COUNT(*) FROM pf_history WHERE is_closed = 0").fetchone()[0]
        total_all = conn.execute("SELECT COUNT(*) FROM pf_history").fetchone()[0]
        
        print(f"\n  \033[96m[{now}] 爬取完成\033[0m | 获取 \033[97m{len(raw_items)}\033[0m 条 | \033[93m新增 {len(new_items)}\033[0m | 更新 {len(updated_items)} | 库中 \033[92m{total_all}\033[0m 条 (活跃 {total_active})")
        
        if new_items:
            print(f"\n  \033[93m✨ 新增招募 ({len(new_items)} 条):\033[0m")
            print(f"  {'大区':<6} | {'服务器':<10} | {'队长':<14} | {'副本':<12} | {'队伍':<8} | 描述")
            print(f"  {'-'*96}")
            for item in new_items[:30]:
                print("  ", end="")
                print_item(item, is_new=True)
            if len(new_items) > 30:
                print(f"  ... 还有 {len(new_items) - 30} 条")
        
        if args.once:
            break
        
        wait = compute_wait(args.interval, consecutive_failures)
        print(f"\n  ⏳ 等待 {wait} 秒后继续... 按 Ctrl+C 停止")
        time.sleep(wait)
    
    client.close()
    conn.close()
    print("\n已退出")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已停止")
        sys.exit(0)