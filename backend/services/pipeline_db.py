"""AES CUTIN 数据一条龙 — SQLite 数据库层"""
import sqlite3
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

_CST = timezone(timedelta(hours=8))
def _now_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S")
from typing import Optional, List

DB_PATH = Path(__file__).parent.parent / "static" / "pipeline.db"


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT    DEFAULT '',
            date        TEXT    NOT NULL,           -- e.g. '0507'
            name        TEXT    NOT NULL,           -- 批次名称
            type        TEXT    NOT NULL,           -- 'mining' | 'feedback'
            status      TEXT    DEFAULT 'in_progress', -- in_progress | done
            notes       TEXT    DEFAULT '',

            -- 输入数据集
            input_name  TEXT    DEFAULT '',
            input_id    TEXT    DEFAULT '',
            input_count INTEGER DEFAULT 0,

            -- DPI（挖掘线）
            dpi_url     TEXT    DEFAULT '',
            dpi_status  TEXT    DEFAULT 'todo',

            -- ALP Checker（挖掘线）
            alp_task_id INTEGER DEFAULT 0,
            alp_url     TEXT    DEFAULT '',
            alp_status  TEXT    DEFAULT 'todo',
            alp_ok_name TEXT    DEFAULT '',   -- ALP成功后的数据集名
            alp_ok_id   TEXT    DEFAULT '',
            alp_ok_count INTEGER DEFAULT 0,

            -- Sim 仿真（挖掘线）
            sim_url     TEXT    DEFAULT '',
            sim_status  TEXT    DEFAULT 'todo',
            sim_out_name TEXT   DEFAULT '',
            sim_out_id  TEXT    DEFAULT '',
            sim_out_count INTEGER DEFAULT 0,

            -- ETP 标注（两种都有）
            etp_task    TEXT    DEFAULT '',   -- task name
            etp_url     TEXT    DEFAULT '',
            etp_status  TEXT    DEFAULT 'todo',
            etp_out_name TEXT   DEFAULT '',
            etp_out_id  TEXT    DEFAULT '',
            etp_out_count INTEGER DEFAULT 0,

            -- DPI 回灌（挖掘线）
            reinject_url    TEXT DEFAULT '',
            reinject_status TEXT DEFAULT 'todo',

            -- FST 写入（两种都有）
            fst_url         TEXT DEFAULT '',   -- FST 叶子节点 URL
            fst_status      TEXT DEFAULT 'todo',
            fst_before      INTEGER DEFAULT 0,
            fst_after       INTEGER DEFAULT 0,

            -- DPI 自动化扩展
            dpi_exec_id     TEXT DEFAULT '',
            dpi_exec_phase  TEXT DEFAULT '',
            dpi_username    TEXT DEFAULT 'junhao.niu',
            dpi_password    TEXT DEFAULT '',
            sim_task_id     TEXT DEFAULT '',
            sim_out_id      TEXT DEFAULT '',
            etp_batch_name  TEXT DEFAULT '',
            etp_tasktype    TEXT DEFAULT '',
            alp_analysis    TEXT DEFAULT '',
            reinject_set_id TEXT DEFAULT '',
            last_polled_at  TEXT DEFAULT '',
            poll_enabled    INTEGER DEFAULT 0
        )
        """)
        # 为已有数据库补字段（迁移）
        existing = {row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()}
        new_cols = {
            "dpi_exec_id":    "TEXT DEFAULT ''",
            "dpi_exec_phase": "TEXT DEFAULT ''",
            "dpi_username":   "TEXT DEFAULT 'junhao.niu'",
            "dpi_password":   "TEXT DEFAULT ''",
            "sim_task_id":    "TEXT DEFAULT ''",
            "sim_out_id":     "TEXT DEFAULT ''",
            "etp_batch_name": "TEXT DEFAULT ''",
            "etp_tasktype":   "TEXT DEFAULT ''",
            "alp_analysis":   "TEXT DEFAULT ''",
            "reinject_set_id":"TEXT DEFAULT ''",
            "last_polled_at": "TEXT DEFAULT ''",
            "poll_enabled":   "INTEGER DEFAULT 0",
        }
        for col, typedef in new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {col} {typedef}")
        conn.commit()


def _row_to_dict(row) -> dict:
    return dict(row) if row else None


def list_runs() -> List[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pipeline_runs ORDER BY date DESC, id DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_run(run_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pipeline_runs WHERE id=?", (run_id,)
        ).fetchone()
    return _row_to_dict(row)


def create_run(data: dict) -> dict:
    data = dict(data)
    data.setdefault("created_at", _now_cst())
    cols = [c for c in data if c != "id"]
    placeholders = ",".join("?" for _ in cols)
    col_str = ",".join(cols)
    vals = [data[c] for c in cols]
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO pipeline_runs ({col_str}) VALUES ({placeholders})", vals
        )
        conn.commit()
        row_id = cur.lastrowid
    return get_run(row_id)


def update_run(run_id: int, data: dict) -> dict:
    if not data:
        return get_run(run_id)
    sets = ",".join(f"{k}=?" for k in data)
    vals = list(data.values()) + [run_id]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE pipeline_runs SET {sets} WHERE id=?", vals
        )
        conn.commit()
    return get_run(run_id)


def delete_run(run_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM pipeline_runs WHERE id=?", (run_id,))
        conn.commit()


# 初始化
init_db()
