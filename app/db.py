"""SQLite 连接与建表。

四张表:
- scans: 扫码明细 (每次扫码一行, scan_id 唯一; 只保留未封存的)
- flow_index: 流向索引 (同工件/同日期/同流向只保留一行, 用 ref_count 维护引用数;
  封存不删索引, 历史流向原样保留)
- part_state: 封存时每件最后一条历史扫码的工位 (新班次首条扫码的接续前站)
- seal_state: 单行, 当前封存日 (无行表示尚未封存)

写入一律走单连接 + threading.Lock, 配合事务保证
"扫码和流向索引一同生效, 失败保留原状"。
"""
from __future__ import annotations

import os
import sqlite3
import threading

DB_PATH = os.environ.get("SCANFLOW_DB_PATH", "/data/scanflow.db")

_write_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    """返回进程内唯一的 SQLite 连接 (启用 WAL, 外键)。"""
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _conn = conn
    return _conn


def write_lock() -> threading.Lock:
    return _write_lock


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id   TEXT PRIMARY KEY,              -- 每次扫码的唯一编号
    part_id   TEXT NOT NULL,                 -- 工件号
    station   TEXT NOT NULL,                 -- 工位
    ts_utc    TEXT NOT NULL,                 -- UTC 时刻, 固定格式 YYYY-MM-DDTHH:MM:SS.ffffffZ
    day_utc   TEXT NOT NULL                  -- UTC 日期 YYYY-MM-DD (扫描归属日期)
);

-- 同件扫码按 (时刻, 编号) 排序, 该索引同时服务于前后相邻扫码的定位
CREATE INDEX IF NOT EXISTS idx_scans_part_order
    ON scans (part_id, ts_utc, scan_id);

CREATE TABLE IF NOT EXISTS flow_index (
    part_id      TEXT NOT NULL,
    day_utc      TEXT NOT NULL,              -- 归入后一次扫码的 UTC 日期
    from_station TEXT NOT NULL,
    to_station   TEXT NOT NULL,
    ref_count    INTEGER NOT NULL DEFAULT 1, -- 该件当天该流向出现的次数
    PRIMARY KEY (part_id, day_utc, from_station, to_station)
);

CREATE INDEX IF NOT EXISTS idx_flow_day
    ON flow_index (day_utc);

-- 封存: 每件保留最后一条历史扫码的工位, 作为后续首条未封存扫码的接续前站
CREATE TABLE IF NOT EXISTS part_state (
    part_id      TEXT PRIMARY KEY,
    last_station TEXT NOT NULL,   -- 封存时该件最后一条被删扫码的工位 (接续工位)
    sealed_day   TEXT NOT NULL    -- 最近一次改写本记录的封存日
);

-- 单行表: 当前封存日; 删除该日前的逐条扫码, 该日及之后的明细保留
CREATE TABLE IF NOT EXISTS seal_state (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    seal_day TEXT NOT NULL        -- 当前封存日 YYYY-MM-DD
);
"""


def init_db() -> None:
    conn = get_conn()
    with _write_lock:
        conn.executescript(SCHEMA)
        conn.commit()
