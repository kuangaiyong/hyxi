"""SQLite 存储层 — 替代 JSON 文件实现持久化"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List
from app.config import settings

logger = logging.getLogger("hyxi.storage")

DB_PATH = os.path.join(settings.data_dir, "hyxi.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    description TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '[]',
    progress REAL NOT NULL DEFAULT 0.0,
    current_step TEXT,
    result_json TEXT,
    error_message TEXT,
    logs_json TEXT NOT NULL DEFAULT '[]',
    scheduled_by TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS sentiment (
    task_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    collector_id TEXT NOT NULL,
    name TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_auth_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    source_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'password',
    username TEXT NOT NULL DEFAULT '',
    secret_enc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sentiment_created ON sentiment(created_at DESC);
"""


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库（创建表结构）"""
    try:
        conn = _get_conn()
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        logger.info("SQLite 数据库初始化完成: %s", DB_PATH)
    except Exception as e:
        logger.error("数据库初始化失败: %s", str(e))


def migrate_from_json():
    """从 JSON 文件迁移数据到 SQLite（如果 DB 为空且有 JSON 文件）"""
    tasks_path = os.path.join(settings.data_dir, "tasks.json")
    if not os.path.exists(tasks_path):
        return

    conn = _get_conn()
    try:
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        if count > 0:
            return  # 已有数据，跳过迁移

        with open(tasks_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        migrated = 0
        for task in data.get("tasks", []):
            conn.execute(
                """INSERT OR REPLACE INTO tasks
                   (id, status, description, plan_json, progress, current_step,
                    result_json, error_message, logs_json, scheduled_by,
                    created_at, started_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task.get("id", ""),
                    task.get("status", "pending"),
                    task.get("description", ""),
                    json.dumps(task.get("plan", []), ensure_ascii=False),
                    task.get("progress", 0.0),
                    task.get("current_step"),
                    json.dumps(task.get("result"), ensure_ascii=False) if task.get("result") else None,
                    task.get("error_message"),
                    json.dumps(task.get("logs", []), ensure_ascii=False),
                    task.get("scheduled_by"),
                    _to_iso(task.get("created_at")),
                    _to_iso(task.get("started_at")),
                    _to_iso(task.get("completed_at")),
                ),
            )
            migrated += 1

        conn.commit()
        logger.info("从 %s 迁移 %d 条任务到 SQLite", tasks_path, migrated)

        # 迁移 sentiment 文件
        import glob as _g
        sentiment_dir = settings.data_dir
        for sf in _g.glob(os.path.join(sentiment_dir, "sentiment_*.json")):
            try:
                with open(sf, "r", encoding="utf-8") as f:
                    sdata = json.load(f)
                tid = sdata.get("task_id", os.path.basename(sf))
                conn.execute(
                    "INSERT OR REPLACE INTO sentiment (task_id, data_json, created_at) VALUES (?,?,?)",
                    (tid, json.dumps(sdata, ensure_ascii=False), sdata.get("analyzed_at", datetime.now().isoformat())),
                )
            except Exception:
                pass
        conn.commit()
        logger.info("Sentiment 数据迁移完成")
    except Exception as e:
        logger.error("数据迁移失败: %s", str(e))
    finally:
        conn.close()


# ===== Task CRUD =====

def save_task(task: dict):
    """保存/更新单个任务"""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO tasks
               (id, status, description, plan_json, progress, current_step,
                result_json, error_message, logs_json, scheduled_by,
                created_at, started_at, completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task.get("id", ""),
                task.get("status", "pending"),
                task.get("description", ""),
                json.dumps(task.get("plan", []), ensure_ascii=False),
                task.get("progress", 0.0),
                task.get("current_step"),
                json.dumps(task.get("result"), ensure_ascii=False) if task.get("result") else None,
                task.get("error_message"),
                json.dumps(task.get("logs", []), ensure_ascii=False),
                task.get("scheduled_by"),
                _to_iso(task.get("created_at")),
                _to_iso(task.get("started_at")),
                _to_iso(task.get("completed_at")),
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error("保存任务失败: %s", str(e))
    finally:
        conn.close()


def load_all_tasks() -> list:
    """加载所有任务"""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [_row_to_task(r) for r in rows]
    except Exception as e:
        logger.error("加载任务失败: %s", str(e))
        return []
    finally:
        conn.close()


def get_task(task_id: str) -> Optional[dict]:
    """获取单个任务"""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None
    except Exception as e:
        logger.error("获取任务 %s 失败: %s", task_id, str(e))
        return None
    finally:
        conn.close()


def delete_task(task_id: str) -> bool:
    """删除任务"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error("删除任务 %s 失败: %s", task_id, str(e))
        return False
    finally:
        conn.close()


# ===== Sentiment CRUD =====

def save_sentiment(task_id: str, data: dict):
    """保存舆情分析结果"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sentiment (task_id, data_json, created_at) VALUES (?,?,?)",
            (task_id, json.dumps(data, ensure_ascii=False), data.get("analyzed_at", datetime.now().isoformat())),
        )
        conn.commit()
    except Exception as e:
        logger.error("保存舆情结果失败: %s", str(e))
    finally:
        conn.close()


def get_sentiment(task_id: str) -> Optional[dict]:
    """获取舆情分析结果

    只按 task_id 精确匹配。曾经在查不到时回退返回最新一条，但结果里的 index 是按各自
    任务的帖子列表编号的，跨任务取来会与当前帖子完全对不上；更糟的是增量分析会把它
    当作 existing_results 合并后持久化，直接污染目标任务。
    """
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM sentiment WHERE task_id = ?", (task_id,)).fetchone()
        if row:
            return json.loads(row["data_json"])
        return None
    except Exception as e:
        logger.error("获取舆情结果失败: %s", str(e))
        return None
    finally:
        conn.close()


# ===== Source / Credential CRUD =====

def save_source(source: dict):
    """保存/更新数据源。

    必须用 ON CONFLICT DO UPDATE 而不是 INSERT OR REPLACE：REPLACE 是「删旧行再插新行」，
    在 foreign_keys=ON 下会触发 credentials 的 ON DELETE CASCADE —— 改个名字就把凭据清空了。
    """
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO sources
               (id, collector_id, name, params_json, enabled, last_auth_at, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   collector_id = excluded.collector_id,
                   name = excluded.name,
                   params_json = excluded.params_json,
                   enabled = excluded.enabled,
                   last_auth_at = excluded.last_auth_at""",
            (
                source["id"],
                source["collector_id"],
                source.get("name", ""),
                json.dumps(source.get("params", {}), ensure_ascii=False),
                1 if source.get("enabled", True) else 0,
                _to_iso(source.get("last_auth_at")),
                _to_iso(source.get("created_at")) or datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_sources(enabled_only: bool = False) -> List[dict]:
    """加载数据源列表"""
    conn = _get_conn()
    try:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at"
        return [_row_to_source(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_source(source_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return _row_to_source(row) if row else None
    finally:
        conn.close()


def delete_source(source_id: str) -> bool:
    """删除数据源。凭据靠外键 ON DELETE CASCADE 一并清掉"""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def save_credential(source_id: str, kind: str, username: str, secret_enc: str):
    """保存凭据。secret_enc 必须是已加密的密文，本层不做加密"""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO credentials
               (source_id, kind, username, secret_enc, created_at) VALUES (?,?,?,?,?)""",
            (source_id, kind, username, secret_enc, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_credential(source_id: str) -> Optional[dict]:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM credentials WHERE source_id = ?", (source_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "source_id": row["source_id"],
            "kind": row["kind"],
            "username": row["username"],
            "secret_enc": row["secret_enc"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def delete_credential(source_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM credentials WHERE source_id = ?", (source_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ===== Helpers =====

def _row_to_source(row) -> dict:
    return {
        "id": row["id"],
        "collector_id": row["collector_id"],
        "name": row["name"],
        "params": json.loads(row["params_json"] or "{}"),
        "enabled": bool(row["enabled"]),
        "last_auth_at": row["last_auth_at"],
        "created_at": row["created_at"],
    }


def _row_to_task(row) -> dict:
    """将 SQLite row 转为任务 dict"""
    if row is None:
        return None
    return {
        "id": row["id"],
        "status": row["status"],
        "description": row["description"],
        "plan": json.loads(row["plan_json"] or "[]"),
        "progress": row["progress"] or 0.0,
        "current_step": row["current_step"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error_message": row["error_message"],
        "logs": json.loads(row["logs_json"] or "[]"),
        "scheduled_by": row["scheduled_by"],
        "created_at": _from_iso(row["created_at"]),
        "started_at": _from_iso(row["started_at"]) if row["started_at"] else None,
        "completed_at": _from_iso(row["completed_at"]) if row["completed_at"] else None,
    }


def _to_iso(val) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _from_iso(val: Optional[str]) -> Optional[datetime]:
    if val is None:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None
