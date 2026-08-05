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

-- 旧的 sentiment 表（整份结果连同按下标排列的 results 数组塞在一个 JSON 列里）
-- 刻意不在这里建：它只应该由老库遗留下来，迁完就 DROP。写进 SCHEMA 会让它
-- 每次启动又长回来，于是「读哪一份」永远是个选择题

-- 一次舆情分析的元信息。summary 是纯派生的聚合结果，整体读写、不参与查询，
-- 按前面定的边界可以留在 JSON 列里
CREATE TABLE IF NOT EXISTS sentiment_runs (
    task_id TEXT PRIMARY KEY,
    analyzed_at TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

-- 结论**按帖子身份存**，不按下标。下标一旦离开写入现场就没人能保证它还对得上：
-- 已经因此出过一次事故（批次内下标被当成全量位置存盘，结论整体挂到别人身上）
CREATE TABLE IF NOT EXISTS sentiment_results (
    task_id         TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    sentiment       TEXT,
    -- NUMERIC 而非 REAL：REAL 亲和性会把整数 3 存成 3.0，导出的强度列于是从
    -- 「3」变成「3.0」。NUMERIC 保留原样，整数还是整数
    intensity       NUMERIC,
    reason_cn       TEXT NOT NULL DEFAULT '',
    dimensions_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (task_id, source_id, fingerprint)
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

CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    interval TEXT NOT NULL,
    time TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    history_json TEXT NOT NULL DEFAULT '[]'
);

-- 帖子。**故意不挂 sources 外键**：删数据源不能让历史任务结果变空白，
-- 而 ON DELETE CASCADE 正好会干这件事。这与改造前「删源后落盘 JSON 仍在」一致
CREATE TABLE IF NOT EXISTS posts (
    source_id          TEXT NOT NULL,
    fingerprint        TEXT NOT NULL,
    -- 采集顺序。整条处理链有 8 处依赖「扁平数组的下标即顺序」，入库后没有数组了，
    -- 由它完整承担：按 source 单调递增、已有帖子的 seq 永不变、新帖追加在后
    seq                INTEGER NOT NULL,
    username           TEXT NOT NULL DEFAULT '',
    timestamp          TEXT NOT NULL DEFAULT '',
    content            TEXT NOT NULL DEFAULT '',
    translation        TEXT NOT NULL DEFAULT '',
    page_number        INTEGER NOT NULL DEFAULT 1,
    message_id         TEXT NOT NULL DEFAULT '',
    parent_fingerprint TEXT,
    reply_level        INTEGER NOT NULL DEFAULT 0,
    images_json        TEXT NOT NULL DEFAULT '[]',
    translated         INTEGER NOT NULL DEFAULT 0,
    sentiment_at       TEXT,
    PRIMARY KEY (source_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_source_seq ON posts(source_id, seq);
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


MIGRATED_DIR_NAME = "_migrated_backup"


def retire_file(path: str) -> None:
    """迁移成功的源文件移进备份目录，不直接删。

    留一份让人能自己核对；确认无误后由用户清掉。
    """
    try:
        backup_dir = os.path.join(settings.data_dir, MIGRATED_DIR_NAME)
        os.makedirs(backup_dir, exist_ok=True)
        os.replace(path, os.path.join(backup_dir, os.path.basename(path)))
    except Exception as e:
        logger.warning("归档已迁移文件失败 %s: %s", path, e)


def _migrate_app_config() -> None:
    """config.json → app_config 表。含明文 LLM API Key，迁完就不该再留在文件里"""
    path = os.path.join(settings.data_dir, "config.json")
    if not os.path.exists(path):
        return
    if get_app_config("llm"):
        return  # 已迁过
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("api_key"):
            set_app_config("llm", cfg)
            logger.info("LLM 配置已迁入 app_config 表")
        retire_file(path)
    except Exception as e:
        logger.error("迁移 config.json 失败: %s", e)


def _migrate_schedules() -> None:
    """scheduled_tasks.json → schedules 表"""
    path = os.path.join(settings.data_dir, "scheduled_tasks.json")
    if not os.path.exists(path):
        return
    if load_schedules():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            configs = json.load(f)
        for cfg in configs:
            save_schedule(cfg)
        logger.info("已迁移 %d 条定时任务配置到 SQLite", len(configs))
        retire_file(path)
    except Exception as e:
        logger.error("迁移 scheduled_tasks.json 失败: %s", e)


def migrate_posts_file(source_id: str, path: str) -> int:
    """把一个来源的落盘 JSON 搬进 posts 表，seq 取数组下标。

    幂等：该来源已有帖子就跳过 —— 重跑不能把 seq 洗牌，那会让所有历史结论错位。
    """
    if not os.path.exists(path) or count_posts(source_id) > 0:
        return 0
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    posts = loaded.get("posts") or []
    for p in posts:
        p.setdefault("source", source_id)
    added = upsert_posts(source_id, posts)
    logger.info("来源 %s 的 %d 条帖子已迁入 posts 表", source_id, added)
    return added


def migrate_from_json():
    """从 JSON 文件迁移数据到 SQLite（幂等：已迁过就跳过）"""
    _migrate_app_config()
    _migrate_schedules()

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

def _post_identity(post: dict) -> tuple:
    """(source_id, fingerprint)。与 post_tree.post_key() 同一套口径，
    历史数据没有 source 时缺省填 tweakers"""
    return (post.get("source") or "tweakers", post.get("fingerprint") or "")


def save_sentiment(task_id: str, data: dict, posts: List[dict]) -> None:
    """保存舆情分析结果。

    `data["results"]` 按扁平数组下标排列，落库时立刻换成 (source_id, fingerprint)。
    **下标只在写入现场有意义** —— 离开这里就再没人能保证它对得上哪条帖子。
    """
    results = data.get("results") or []
    rows = []
    for i, post in enumerate(posts):
        if i >= len(results):
            break
        r = results[i]
        if not r:
            continue
        source_id, fingerprint = _post_identity(post)
        if not fingerprint:
            continue
        rows.append((
            task_id, source_id, fingerprint, r.get("sentiment"), r.get("intensity"),
            r.get("reason_cn") or "",
            json.dumps(r.get("dimensions") or [], ensure_ascii=False),
        ))

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_runs (task_id, analyzed_at, summary_json) VALUES (?,?,?)",
            (task_id, data.get("analyzed_at") or datetime.now().isoformat(),
             json.dumps(data.get("summary") or {}, ensure_ascii=False)),
        )
        # 整轮覆盖：analyze() 传进来的 results 已经是「已有结果 + 本轮新增」的全量
        conn.execute("DELETE FROM sentiment_results WHERE task_id = ?", (task_id,))
        # OR REPLACE 而非裸 INSERT：同一批 posts 里出现两条 (source_id, fingerprint)
        # 相同的记录并非不可能（指纹只吃 username|timestamp|content[:100]，翻页错位
        # 就能撞上）。裸 INSERT 会抛 IntegrityError 让整个事务回滚 —— 这一轮
        # 花钱算出来的结论一条都存不下，而调用方还在报「分析完成」
        conn.executemany(
            """INSERT OR REPLACE INTO sentiment_results
               (task_id, source_id, fingerprint, sentiment, intensity, reason_cn, dimensions_json)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()
    except Exception as e:
        # 必须往上抛：结论只在内存里，存不下就是真的没了。吞掉异常会让任务
        # 报「完成」而库里空空如也，用户永远不知道该重跑
        logger.error("保存舆情结果失败: %s", str(e))
        raise
    finally:
        conn.close()


def get_sentiment(task_id: str, posts: List[dict]) -> Optional[dict]:
    """按 posts 的顺序重建 results 数组，返回与旧版逐字段一致的结构。

    只按 task_id 精确匹配。曾经在查不到时回退返回最新一条，但那份结果是按别的
    任务的帖子列表编号的，取来与当前帖子完全对不上；更糟的是增量分析会把它当作
    existing_results 合并后持久化，直接污染目标任务。
    """
    conn = _get_conn()
    try:
        run = conn.execute(
            "SELECT * FROM sentiment_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not run:
            return None
        by_identity = {
            (r["source_id"], r["fingerprint"]): {
                "sentiment": r["sentiment"],
                "intensity": r["intensity"],
                "reason_cn": r["reason_cn"],
                "dimensions": json.loads(r["dimensions_json"] or "[]"),
            }
            for r in conn.execute(
                "SELECT * FROM sentiment_results WHERE task_id = ?", (task_id,)
            ).fetchall()
        }
    except Exception as e:
        logger.error("获取舆情结果失败: %s", str(e))
        return None
    finally:
        conn.close()

    results = [by_identity.get(_post_identity(p)) for p in posts]
    return {
        "task_id": task_id,
        "analyzed_at": run["analyzed_at"],
        "total": len(posts),
        "success": sum(1 for r in results if r and r.get("sentiment")),
        "failed": sum(1 for r in results if not r or not r.get("sentiment")),
        "summary": json.loads(run["summary_json"] or "{}"),
        "results": results,
    }


def legacy_sentiment_task_ids() -> List[str]:
    """旧 sentiment 表里还没迁进 sentiment_runs 的 task_id"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT s.task_id FROM sentiment s
            LEFT JOIN sentiment_runs r ON r.task_id = s.task_id
            WHERE r.task_id IS NULL
        """).fetchall()
        return [r["task_id"] for r in rows]
    except Exception:
        return []          # 老库没有 sentiment 表就没什么可迁的
    finally:
        conn.close()


def migrate_sentiment_blob(task_id: str, posts: List[dict]) -> bool:
    """把一份按下标排列的旧结果换成按身份存。posts 必须是当时那个扁平数组。

    对齐用一条可检验的不变量兜底：**凡是 results[i] 有结论，第 i 条帖子必须已带
    sentiment_at**。对不上就整份跳过 —— 宁可留一份没迁进来的，也不能把结论安到
    别人身上（上一个 bug 正是这么产生的）。
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT data_json FROM sentiment WHERE task_id = ?", (task_id,)
        ).fetchone()
    except Exception:
        return False       # 新库压根没有这张表
    finally:
        conn.close()
    if not row:
        return False

    data = json.loads(row["data_json"])
    results = data.get("results") or []

    # 结果比帖子多，通常是某个来源的落盘文件后来被删了（尾部那些结论的归属帖子
    # 已经不存在）。**不能因此整份放弃** —— 前缀仍然对得上就该救回来，
    # 校验交给下面的不变量，而不是靠长度一票否决
    usable = min(len(results), len(posts))
    if len(results) > len(posts):
        logger.warning("舆情 %s 有 %d 条结果但只剩 %d 条帖子，尾部 %d 条的归属帖子已不存在，"
                       "只迁前 %d 条", task_id, len(results), len(posts),
                       len(results) - usable, usable)

    for i in range(usable):
        r = results[i]
        if r and r.get("sentiment") and not (posts[i].get("_processed") or {}).get("sentiment_at"):
            logger.error("舆情 %s 第 %d 条有结论但帖子没有 sentiment_at 标记，"
                         "下标对不上，跳过迁移", task_id, i)
            return False

    save_sentiment(task_id, data, posts)
    logger.info("舆情 %s 已按帖子身份重存 %d 条结论", task_id, usable)
    return True


def discard_legacy_sentiment(task_id: str) -> None:
    """丢弃一份任务已不存在的旧结果。它从任何接口都够不着，只会挡住旧表的 DROP"""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM sentiment WHERE task_id = ?", (task_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def drop_legacy_sentiment_table() -> None:
    """全部迁完后删掉旧表。留着就是给「读哪一份」留了个选择题"""
    conn = _get_conn()
    try:
        remaining = conn.execute("""
            SELECT COUNT(*) FROM sentiment s
            LEFT JOIN sentiment_runs r ON r.task_id = s.task_id
            WHERE r.task_id IS NULL
        """).fetchone()[0]
        if remaining:
            return
        conn.execute("DROP TABLE IF EXISTS sentiment")
        conn.commit()
        logger.info("旧 sentiment 表已删除")
    except Exception as e:
        logger.warning("删除旧 sentiment 表失败: %s", e)
    finally:
        conn.close()


# ===== 帖子（原「项目根目录 {collector}_{id}.json」）=====

_POST_COLS = (
    "username", "timestamp", "content", "translation", "page_number",
    "message_id", "parent_fingerprint", "reply_level",
)


def _row_to_post(row) -> dict:
    post = {
        "source": row["source_id"],
        "fingerprint": row["fingerprint"],
        "username": row["username"],
        "timestamp": row["timestamp"],
        "content": row["content"],
        "translation": row["translation"],
        "page_number": row["page_number"],
        "message_id": row["message_id"],
        "parent_fingerprint": row["parent_fingerprint"],
        "reply_level": row["reply_level"],
    }
    images = json.loads(row["images_json"] or "[]")
    if images:
        post["images"] = images
    # 只放已置位的键：新采到的帖子本来就没有 _processed，凭空补一个空壳会让
    # 「这条处理过没有」多出一种表示形态
    processed = {}
    if row["translated"]:
        processed["translated"] = True
    if row["sentiment_at"]:
        processed["sentiment_at"] = row["sentiment_at"]
    if processed:
        post["_processed"] = processed
    return post


def load_posts(source_ids: List[str]) -> List[dict]:
    """按 source_ids 给定的顺序取帖子，每个来源内部按 seq。

    顺序即「舆情下标 / /posts 的 index 语义」依赖的那个顺序，不能改成按别的排。
    """
    if not source_ids:
        return []
    conn = _get_conn()
    try:
        posts = []
        for sid in source_ids:
            rows = conn.execute(
                "SELECT * FROM posts WHERE source_id = ? ORDER BY seq", (sid,)
            ).fetchall()
            posts.extend(_row_to_post(r) for r in rows)
        return posts
    finally:
        conn.close()


def count_posts(source_id: str) -> int:
    conn = _get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM posts WHERE source_id = ?", (source_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def known_fingerprints(source_id: str) -> List[str]:
    """交给采集脚本做增量去重。以前脚本自己读旧落盘文件，现在由 job 下发"""
    conn = _get_conn()
    try:
        return [r[0] for r in conn.execute(
            "SELECT fingerprint FROM posts WHERE source_id = ?", (source_id,)
        )]
    finally:
        conn.close()


def max_page_number(source_id: str) -> int:
    """page 型增量（Tweakers）的续抓点。信息流类来源没有页的含义，用不到"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(page_number) FROM posts WHERE source_id = ?", (source_id,)
        ).fetchone()
        return row[0] or 0
    finally:
        conn.close()


def upsert_posts(source_id: str, posts: List[dict]) -> int:
    """写入采集结果，返回新增条数。

    **已存在的帖子只更新采集字段，绝不覆盖 translation / translated / sentiment_at**。
    整体覆盖等于把已翻译的帖子重新变成新帖，下一轮再付一次翻译钱、舆情也重算一遍
    （group_feed.js 曾经就是这么错的）。
    """
    if not posts:
        return 0
    conn = _get_conn()
    try:
        existing = {
            r["fingerprint"]: r["seq"] for r in conn.execute(
                "SELECT fingerprint, seq FROM posts WHERE source_id = ?", (source_id,)
            )
        }
        next_seq = (conn.execute(
            "SELECT MAX(seq) FROM posts WHERE source_id = ?", (source_id,)
        ).fetchone()[0] or -1) + 1

        added = 0
        for post in posts:
            fp = post.get("fingerprint")
            if not fp:
                continue
            if fp in existing:
                # seq 保持不变 —— 它是全链路的顺序锚点，动一下所有历史结论就错位了
                conn.execute(
                    """UPDATE posts SET username=?, timestamp=?, content=?, page_number=?,
                       message_id=?, parent_fingerprint=?, reply_level=?, images_json=?
                       WHERE source_id=? AND fingerprint=?""",
                    (
                        post.get("username", ""), post.get("timestamp", ""),
                        post.get("content", ""), int(post.get("page_number", 1) or 1),
                        post.get("message_id", ""), post.get("parent_fingerprint"),
                        int(post.get("reply_level", 0) or 0),
                        json.dumps(post.get("images") or [], ensure_ascii=False),
                        source_id, fp,
                    ),
                )
                continue
            processed = post.get("_processed") or {}
            conn.execute(
                """INSERT INTO posts (source_id, fingerprint, seq, username, timestamp,
                   content, translation, page_number, message_id, parent_fingerprint,
                   reply_level, images_json, translated, sentiment_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, fp, next_seq, post.get("username", ""),
                    post.get("timestamp", ""), post.get("content", ""),
                    post.get("translation", ""), int(post.get("page_number", 1) or 1),
                    post.get("message_id", ""), post.get("parent_fingerprint"),
                    int(post.get("reply_level", 0) or 0),
                    json.dumps(post.get("images") or [], ensure_ascii=False),
                    1 if processed.get("translated") else 0,
                    processed.get("sentiment_at"),
                ),
            )
            existing[fp] = next_seq
            next_seq += 1
            added += 1
        conn.commit()
        return added
    except Exception:
        logger.exception("写入帖子失败 source=%s", source_id)
        raise
    finally:
        conn.close()


def save_translations(posts: List[dict]) -> int:
    """把翻译结果写回。按 (source, fingerprint) 定位，与顺序无关"""
    conn = _get_conn()
    try:
        updated = 0
        for post in posts:
            fp = post.get("fingerprint")
            if not fp:
                continue
            processed = post.get("_processed") or {}
            cur = conn.execute(
                "UPDATE posts SET translation=?, translated=? WHERE source_id=? AND fingerprint=?",
                (
                    post.get("translation", ""),
                    1 if processed.get("translated") else 0,
                    post.get("source") or "tweakers", fp,
                ),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


def mark_sentiment_analyzed(posts: List[dict]) -> int:
    """把 _processed.sentiment_at 落库。

    只写这一个字段：舆情分析不该碰 translation / translated，
    整体覆盖会让几百条已翻译的帖子下次重新走一遍付费翻译。
    """
    conn = _get_conn()
    try:
        updated = 0
        for post in posts:
            at = (post.get("_processed") or {}).get("sentiment_at")
            fp = post.get("fingerprint")
            if not (at and fp):
                continue
            cur = conn.execute(
                "UPDATE posts SET sentiment_at=? WHERE source_id=? AND fingerprint=? AND sentiment_at IS NULL",
                (at, post.get("source") or "tweakers", fp),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


# ===== 应用配置（原 config.json）=====

def get_app_config(prefix: str) -> dict:
    """取出某个前缀下的全部配置项，返回的键已去掉前缀。查不到返回空 dict"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_config WHERE key LIKE ?", (prefix + ".%",)
        ).fetchall()
        return {r["key"][len(prefix) + 1:]: r["value"] for r in rows}
    finally:
        conn.close()


def set_app_config(prefix: str, values: dict) -> None:
    """整组覆盖某个前缀下的配置。先删后插，避免残留上一版多出来的键"""
    conn = _get_conn()
    try:
        now = datetime.now().isoformat()
        conn.execute("DELETE FROM app_config WHERE key LIKE ?", (prefix + ".%",))
        conn.executemany(
            "INSERT INTO app_config (key, value, updated_at) VALUES (?,?,?)",
            [(f"{prefix}.{k}", "" if v is None else str(v), now) for k, v in values.items()],
        )
        conn.commit()
    finally:
        conn.close()


def delete_app_config(prefix: str) -> None:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM app_config WHERE key LIKE ?", (prefix + ".%",))
        conn.commit()
    finally:
        conn.close()


# ===== 定时任务业务配置（原 scheduled_tasks.json）=====

_SCHEDULE_COLS = ("id", "description", "interval", "time", "enabled", "created_at")


def load_schedules() -> List[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM schedules ORDER BY created_at").fetchall()
        return [_row_to_schedule(r) for r in rows]
    finally:
        conn.close()


def save_schedule(cfg: dict) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO schedules (id, description, interval, time, enabled, created_at, history_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 description=excluded.description, interval=excluded.interval,
                 time=excluded.time, enabled=excluded.enabled,
                 history_json=excluded.history_json""",
            (
                cfg["id"], cfg.get("description", ""), cfg.get("interval", ""),
                cfg.get("time", ""), 1 if cfg.get("enabled", True) else 0,
                cfg.get("created_at") or datetime.now().isoformat(),
                json.dumps(cfg.get("history", []), ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_schedule(schedule_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _row_to_schedule(row) -> dict:
    return {
        "id": row["id"],
        "description": row["description"],
        "interval": row["interval"],
        "time": row["time"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
        "history": json.loads(row["history_json"] or "[]"),
    }


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
