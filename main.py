"""
FFXIV Party Finder History Search API
基于 FastAPI 提供历史招募板搜索接口
支持 PostgreSQL（生产，pg_trgm 模糊搜索）和 SQLite（本地开发预览）
"""

import os
import json
import time as _time
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ─────────────────────────────── 配置 ───────────────────────────────

DB_TYPE = os.environ.get("DB_TYPE", "postgres")

DB_HOST = os.environ.get("DB_HOST", "postgres")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "ffrecruit")
DB_USER = os.environ.get("DB_USER", "ffrecruit")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "ffrecruit")
DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX", "10"))

SQLITE_PATH = os.environ.get("SQLITE_PATH", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ffrecruit.db"
))

CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

IS_SQLITE = DB_TYPE == "sqlite"

# ─────────────────────────────── 繁中服识别 ───────────────────────────────
# 传统中国字服务器（繁中服）默认不在网页内展示，提供按钮切换
TW_WORLDS = {"伊弗利特", "迦樓羅", "利維坦", "鳳凰", "奧汀", "奥汀", "巴哈姆特", "泰坦"}
# 繁中服常见大区（非国服简体大区）。依据名称包含繁體字、或明确属其他地区者
TW_DATACENTER_PATTERNS = ("陸行鳥", "Mana", "Gaia", "Elemental", "Aether", "Primal", "Crystal", "Chaos", "Light", "Materia")

# ─────────────────────────────── 数据库连接 ───────────────────────────────

db_pool = None
_sqlite_conn = None


def _ensure_sqlite_schema():
    """确保 SQLite 表结构完整，缺少字段则自动补上"""
    global _sqlite_conn
    try:
        cur = _sqlite_conn.cursor()
        cur.execute("PRAGMA table_info(pf_history)")
        cols = {r[1] for r in cur.fetchall()}
        for col, defval in [
            ("home_world", "TEXT NOT NULL DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in cols:
                try:
                    cur.execute(f"ALTER TABLE pf_history ADD COLUMN {col} {defval}")
                    _sqlite_conn.commit()
                    print(f"[INFO] 已新增列: {col}")
                except sqlite3.OperationalError as e:
                    print(f"[WARN] 新增列 {col} 失败: {e}")
        # ── 性能优化：复合索引（覆盖高频查询组合） ──
        # 1) 大区+时间：按大区筛选并按时间倒序（最高频查询）
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dc_firstseen ON pf_history (datacenter, first_seen_at DESC)")
        # 2) 类型+时间：按任务类型筛选
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cat_firstseen ON pf_history (category, first_seen_at DESC)")
        # 3) 状态+时间：按进行中/已关闭筛选
        cur.execute("CREATE INDEX IF NOT EXISTS idx_closed_firstseen ON pf_history (is_closed, first_seen_at DESC)")
        # 4) 服务器+时间
        cur.execute("CREATE INDEX IF NOT EXISTS idx_home_firstseen ON pf_history (home_world, first_seen_at DESC)")
        # 5) 队长名+时间
        cur.execute("CREATE INDEX IF NOT EXISTS idx_creator_firstseen ON pf_history (creator_name, first_seen_at DESC)")
        # 6) 副本名+时间
        cur.execute("CREATE INDEX IF NOT EXISTS idx_duty_firstseen ON pf_history (duty_name, first_seen_at DESC)")
        # 7) 单列索引（辅助）
        cur.execute("CREATE INDEX IF NOT EXISTS idx_category ON pf_history (category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_home_world ON pf_history (home_world)")
        # 8) 覆盖索引：is_closed + datacenter + home_world + world（用于 exclude_tw 的精确判断）
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tw_check ON pf_history (datacenter, home_world, world)")
        _sqlite_conn.commit()
        cur.close()
    except Exception as e:
        print(f"[WARN] schema 检查失败: {e}")


def _init_sqlite():
    global _sqlite_conn
    _sqlite_conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    _sqlite_conn.row_factory = sqlite3.Row
    _sqlite_conn.execute("PRAGMA journal_mode=WAL")
    _sqlite_conn.execute("PRAGMA synchronous=NORMAL")     # 减少fsync，大幅提升写性能
    _sqlite_conn.execute("PRAGMA cache_size=-8000")       # 8MB 缓存（默认2MB）
    _sqlite_conn.execute("PRAGMA temp_store=MEMORY")      # 临时表/排序在内存
    _sqlite_conn.execute("PRAGMA mmap_size=268435456")    # 256MB mmap
    _sqlite_conn.execute("PRAGMA query_only=0")
    _ensure_sqlite_schema()
    print(f"[INFO] SQLite 连接成功: {SQLITE_PATH}")


def _init_postgres():
    global db_pool
    import psycopg2.pool
    db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=DB_POOL_MIN, maxconn=DB_POOL_MAX,
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )
    conn = db_pool.getconn()
    db_pool.putconn(conn)
    print(f"[INFO] PostgreSQL 连接池初始化成功 ({DB_POOL_MIN}-{DB_POOL_MAX})")


def get_conn():
    if IS_SQLITE:
        return _sqlite_conn
    return db_pool.getconn()


def put_conn(conn):
    if not IS_SQLITE and db_pool is not None:
        db_pool.putconn(conn)


def db_cursor(conn):
    if IS_SQLITE:
        return conn.cursor()
    import psycopg2.extras
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def fetchall(cur):
    if IS_SQLITE:
        return [dict(row) for row in cur.fetchall()]
    return cur.fetchall()


def fetchone(cur):
    if IS_SQLITE:
        row = cur.fetchone()
        return dict(row) if row else None
    return cur.fetchone()


# ─────────────────────────────── SQL 方言适配 ───────────────────────────────

def ph() -> str:
    return "?" if IS_SQLITE else "%s"

def sql_like(column: str) -> str:
    p = ph()
    return f"{column} LIKE {p}" if IS_SQLITE else f"{column} ILIKE {p}"

def sql_false() -> str:
    return "0" if IS_SQLITE else "FALSE"

def is_tw_datacenter(name: str) -> bool:
    if not name:
        return False
    for pat in TW_DATACENTER_PATTERNS:
        if pat in name:
            return True
    return False

def is_tw_world(name: str) -> bool:
    return bool(name) and name in TW_WORLDS

def tw_exclude_condition(P: str) -> tuple[str, list]:
    """
    返回 (SQL 片段, params 列表)
    使用精确匹配 IN/!= 而非 LIKE，以便走索引
    """
    conds: list[str] = []
    params: list = []
    # 精确匹配繁中大区名（不走 LIKE，能命中索引）
    tw_dc_exact = [p for p in TW_DATACENTER_PATTERNS if not any(c in p for c in "%_")]
    if tw_dc_exact:
        places = ",".join([P] * len(tw_dc_exact))
        conds.append(f"datacenter IN ({places})")
        params.extend(tw_dc_exact)
    # 繁中服务器精确 IN
    if TW_WORLDS:
        places = ",".join([P] * len(TW_WORLDS))
        conds.append(f"home_world IN ({places})")
        params.extend(TW_WORLDS)
        conds.append(f"world IN ({places})")
        params.extend(TW_WORLDS)
    return f"NOT ({' OR '.join(conds)})", params


# ─────────────────────────────── 生命周期 ───────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        if IS_SQLITE:
            _init_sqlite()
        else:
            _init_postgres()
    except Exception as e:
        print(f"[ERROR] 数据库连接失败: {e}")
        raise
    yield
    if not IS_SQLITE and db_pool is not None:
        db_pool.closeall()
        print("[INFO] PostgreSQL 连接池已关闭")
    elif IS_SQLITE and _sqlite_conn is not None:
        _sqlite_conn.close()
        print("[INFO] SQLite 连接已关闭")


app = FastAPI(
    title="FFXIV Party Finder History API",
    description="《最终幻想14》历史招募板搜索系统 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────── 请求日志中间件 ───────────────────────────────
# 在控制台实时显示每个 API 请求的方法、路径、状态码、耗时
@app.middleware("http")
async def request_logger(request: Request, call_next):
    # 跳过静态文件和健康检查的日志（太频繁）
    path = request.url.path
    if path.startswith("/static") or path == "/health" or path == "/favicon.ico":
        return await call_next(request)

    start = _time.time()
    method = request.method
    qs = f"?{request.url.query}" if request.url.query else ""

    try:
        response = await call_next(request)
    except Exception as e:
        elapsed_ms = (_time.time() - start) * 1000
        print(f"[API] [ERR] {method} {path}{qs}  500  {elapsed_ms:.0f}ms  ERROR: {e}", flush=True)
        raise

    elapsed_ms = (_time.time() - start) * 1000
    status = response.status_code
    # 慢请求标记（纯文本，兼容 Windows GBK 控制台）
    if elapsed_ms > 500:
        indicator = "[SLOW!]"
    elif elapsed_ms > 200:
        indicator = "[slow ]"
    else:
        indicator = "[ok]"
    print(f"[API] {indicator} {method} {path}{qs}  {status}  {elapsed_ms:.0f}ms", flush=True)
    return response


# ─────────────────────────────── 数据模型 ───────────────────────────────

class HistoryItem(BaseModel):
    listing_id: str
    datacenter: str
    world: str
    creator_name: str
    home_world: str = ""
    category: str = ""
    duty_id: int
    duty_name: str
    description: str
    min_item_level: int
    has_password: bool
    slots: list[dict[str, Any]] = Field(default_factory=list)
    first_seen_at: str
    last_seen_at: str
    is_closed: bool
    duration_seconds: Optional[int] = None


class HistoryResponse(BaseModel):
    items: list[HistoryItem]
    total_count: int
    page: int
    page_size: int
    total_pages: int


# ─────────────────────────────── 工具函数 ───────────────────────────────

def _parse_time(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                     "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(val.split("+")[0].strip(), fmt)
            except ValueError:
                continue
    return None


def _parse_slots(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    return bool(val)


def _row_to_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    first_seen = _parse_time(item.get("first_seen_at"))
    last_seen = _parse_time(item.get("last_seen_at"))
    duration = None
    if first_seen and last_seen:
        duration = int((last_seen - first_seen).total_seconds())
    item["duration_seconds"] = duration
    item["first_seen_at"] = first_seen.isoformat() if first_seen else ""
    item["last_seen_at"] = last_seen.isoformat() if last_seen else ""
    item["slots"] = _parse_slots(item.get("slots"))
    item["has_password"] = _parse_bool(item.get("has_password"))
    item["is_closed"] = _parse_bool(item.get("is_closed"))
    item.setdefault("home_world", "")
    item.setdefault("category", "")
    return item


# ─────────────────────────────── COUNT 缓存 ───────────────────────────────
# 避免 5000+ 行时每次搜索都全表 COUNT(*)，缓存 30 秒
_count_cache: dict[str, tuple[int, float]] = {}
_COUNT_CACHE_TTL = 30  # 秒

def _cached_count(cache_key: str, count_sql: str, params: list, conn) -> int:
    """带缓存的 COUNT 查询，相同 cache_key 30 秒内不重复查"""
    now = _time.time()
    cached = _count_cache.get(cache_key)
    if cached and (now - cached[1]) < _COUNT_CACHE_TTL:
        return cached[0]
    cur = conn.cursor()
    cur.execute(count_sql, params)
    total = cur.fetchone()[0]
    cur.close()
    _count_cache[cache_key] = (total, now)
    return total


# ─────────────────────────────── 搜索 API ───────────────────────────────

@app.get("/api/v1/history", response_model=HistoryResponse)
async def search_history(
    creator: Optional[str] = Query(default=None, description="队长名（模糊搜索）"),
    duty_id: Optional[int] = Query(default=None, description="副本 ID"),
    duty_name: Optional[str] = Query(default=None, description="副本名称"),
    datacenter: Optional[str] = Query(default=None, description="大区"),
    world: Optional[str] = Query(default=None, description="队长所属服务器 (home_world)"),
    keyword: Optional[str] = Query(default=None, description="招募文案关键词"),
    is_closed: Optional[bool] = Query(default=None, description="招募状态"),
    category: Optional[str] = Query(default=None, description="任务类型"),
    start_date: Optional[str] = Query(default=None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="结束日期 YYYY-MM-DD"),
    exclude_tw: bool = Query(default=True, description="是否排除繁中服（大区/服务器含繁體字如伊弗利特、利維坦等）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> HistoryResponse:
    conn = get_conn()
    try:
        conditions: list[str] = []
        params: list[Any] = []
        P = ph()

        if exclude_tw:
            cond, ps = tw_exclude_condition(P)
            conditions.append(cond)
            params.extend(ps)

        if creator and creator.strip():
            conditions.append(sql_like("creator_name"))
            params.append(f"%{creator.strip()}%")

        if duty_id is not None:
            conditions.append(f"duty_id = {P}")
            params.append(duty_id)

        if duty_name and duty_name.strip():
            conditions.append(f"duty_name = {P}")
            params.append(duty_name.strip())

        if datacenter and datacenter.strip():
            conditions.append(f"datacenter = {P}")
            params.append(datacenter.strip())

        if world and world.strip():
            conditions.append(f"home_world = {P}")
            params.append(world.strip())

        if keyword and keyword.strip():
            kw = keyword.strip()
            conditions.append(f"description LIKE {P}")
            params.append(f"%{kw}%")

        if is_closed is not None:
            conditions.append(f"is_closed = {P}")
            params.append(1 if is_closed else 0)

        if category and category.strip():
            cat = category.strip()
            conditions.append(f"category = {P}")
            params.append(cat)

        if start_date and start_date.strip():
            conditions.append(f"first_seen_at >= {P}")
            params.append(start_date.strip())

        if end_date and end_date.strip():
            conditions.append(f"first_seen_at <= {P}")
            params.append(end_date.strip() + " 23:59:59")

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

        count_sql = f"SELECT COUNT(*) FROM pf_history {where_clause}"
        # COUNT 缓存：相同查询条件 30 秒内不重复全表扫描
        cache_key = f"count:{hash(tuple(str(p) for p in params))}"
        total_count = _cached_count(cache_key, count_sql, params, conn)

        offset = (page - 1) * page_size
        total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 0

        data_sql = f"""
            SELECT listing_id, datacenter, world, creator_name, home_world, category,
                   duty_id, duty_name, description, min_item_level,
                   has_password, slots, first_seen_at, last_seen_at, is_closed
            FROM pf_history
            {where_clause}
            ORDER BY first_seen_at DESC
            LIMIT {P} OFFSET {P}
        """
        data_params = params + [page_size, offset]

        cur = db_cursor(conn)
        cur.execute(data_sql, data_params)
        rows = fetchall(cur)
        cur.close()

        items = [_row_to_item(row) for row in rows]

        return HistoryResponse(
            items=items, total_count=total_count,
            page=page, page_size=page_size, total_pages=total_pages,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] 搜索失败: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    finally:
        put_conn(conn)


# ─────────────────────────────── 辅助 API ───────────────────────────────

@app.get("/api/v1/stats")
async def get_stats(exclude_tw: bool = Query(default=True, description="是否排除繁中服")) -> dict[str, Any]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        P = ph()

        def _where_and_params(extra: str = "") -> tuple[str, list[Any]]:
            conditions: list[str] = []
            params: list[Any] = []
            if exclude_tw:
                cond, ps = tw_exclude_condition(P)
                conditions.append(cond)
                params.extend(ps)
            if extra:
                conditions.append(extra)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            return where, params

        where, params = _where_and_params()
        cur.execute(f"SELECT COUNT(*) FROM pf_history {where}", params)
        total = cur.fetchone()[0]

        where_a, params_a = _where_and_params(f"is_closed = {sql_false()}")
        cur.execute(f"SELECT COUNT(*) FROM pf_history {where_a}", params_a)
        active = cur.fetchone()[0]

        where_d, params_d = _where_and_params()
        cur.execute(f"""
            SELECT datacenter, COUNT(*) as c
            FROM pf_history {where_d}
            GROUP BY datacenter ORDER BY c DESC LIMIT 10
        """, params_d)
        datacenters = [{"datacenter": r[0], "count": r[1]} for r in cur.fetchall()]
        cur.close()
        return {"total_listings": total, "active_listings": active, "datacenters": datacenters}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"统计失败: {e}")
    finally:
        put_conn(conn)


@app.get("/api/v1/categories")
async def get_categories(exclude_tw: bool = Query(default=True, description="是否排除繁中服")) -> list[dict[str, Any]]:
    """获取所有任务类型及数量"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        P = ph()
        conditions = ["category != ''"]
        params: list[Any] = []
        if exclude_tw:
            cond, ps = tw_exclude_condition(P)
            conditions.append(cond)
            params.extend(ps)
        where = "WHERE " + " AND ".join(conditions)
        cur.execute(f"""
            SELECT category, COUNT(*) as c
            FROM pf_history
            {where}
            GROUP BY category
            ORDER BY c DESC
        """, params)
        rows = cur.fetchall()
        cur.close()
        return [{"category": r[0] or "其他招募", "count": r[1]} for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    finally:
        put_conn(conn)


@app.get("/api/v1/duties")
async def get_duties(
    category: Optional[str] = Query(default=None, description="按任务类型过滤"),
    limit: int = Query(default=500, ge=1, le=2000),
    exclude_tw: bool = Query(default=True, description="是否排除繁中服"),
) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        P = ph()
        conditions = ["duty_name != ''"]
        params: list[Any] = []
        if category and category.strip():
            conditions.append(f"category = {P}")
            params.append(category.strip())
        if exclude_tw:
            cond, ps = tw_exclude_condition(P)
            conditions.append(cond)
            params.extend(ps)
        where = "WHERE " + " AND ".join(conditions)

        cur.execute(f"""
            SELECT duty_id, duty_name, category, COUNT(*) as listing_count
            FROM pf_history
            {where}
            GROUP BY duty_id, duty_name, category
            ORDER BY listing_count DESC
            LIMIT {P}
        """, params + [limit])
        rows = cur.fetchall()
        cur.close()
        return [
            {"duty_id": r[0], "duty_name": r[1], "category": r[2] or "", "listing_count": r[3]}
            for r in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    finally:
        put_conn(conn)


@app.get("/api/v1/datacenters")
async def get_datacenters(exclude_tw: bool = Query(default=True, description="是否排除繁中服")) -> list[str]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        P = ph()
        conditions = ["datacenter != ''"]
        params: list[Any] = []
        if exclude_tw:
            cond, ps = tw_exclude_condition(P)
            conditions.append(cond)
            params.extend(ps)
        where = "WHERE " + " AND ".join(conditions)
        cur.execute(f"SELECT DISTINCT datacenter FROM pf_history {where} ORDER BY datacenter", params)
        rows = cur.fetchall()
        cur.close()
        return [r[0] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    finally:
        put_conn(conn)


@app.get("/api/v1/worlds")
async def get_worlds(
    datacenter: Optional[str] = Query(default=None, description="按大区过滤"),
    exclude_tw: bool = Query(default=True, description="是否排除繁中服"),
) -> list[str]:
    """获取所有服务器（队长所属 home_world），可按大区过滤"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        P = ph()
        conditions = ["home_world != ''"]
        params: list[Any] = []
        if datacenter and datacenter.strip():
            conditions.append(f"datacenter = {P}")
            params.append(datacenter.strip())
        if exclude_tw:
            cond, ps = tw_exclude_condition(P)
            conditions.append(cond)
            params.extend(ps)
        where = "WHERE " + " AND ".join(conditions)
        cur.execute(f"""
            SELECT DISTINCT home_world
            FROM pf_history
            {where}
            ORDER BY home_world
        """, params)
        rows = cur.fetchall()
        cur.close()
        return [r[0] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询失败: {e}")
    finally:
        put_conn(conn)


@app.get("/health")
async def health_check() -> dict[str, str]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"db_error: {e}")
    finally:
        put_conn(conn)


# ─────────────────────────────── 前端页面 ───────────────────────────────

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
