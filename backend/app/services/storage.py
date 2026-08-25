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

# 单独拎出来：重建这张表时要在一个显式事务里执行它。
# **不能走 executescript** —— 实测它会打断外层事务，回滚后会留下「空的新表 +
# 孤立的旧表」，而那正是要防的静默丢数据
SENTIMENT_RESULTS_DDL = """
-- 结论**按帖子身份存**，不按下标，也**不按任务**。
--
-- 下标一旦离开写入现场就没人能保证它还对得上（已因此出过一次事故：批次内下标被当成
-- 全量位置存盘，结论整体挂到别人身上）。
--
-- 主键里不含 task_id：结论是对**帖子**下的，任务只是「什么时候、由谁触发的」。
-- `_processed.sentiment_at` 本来就按帖子存、跨任务共享（所以同一条帖子不会被重复
-- 花钱分析），结论必须同一个粒度 —— 否则第二个任务跑同一批数据，页面上 94 条里
-- 会有 90 条显示「未分析」，而它们其实早就分析过了。
CREATE TABLE IF NOT EXISTS sentiment_results (
    source_id       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    sentiment       TEXT,
    -- NUMERIC 而非 REAL：REAL 亲和性会把整数 3 存成 3.0，导出的强度列于是从
    -- 「3」变成「3.0」。NUMERIC 保留原样，整数还是整数
    intensity       NUMERIC,
    reason_cn       TEXT NOT NULL DEFAULT '',
    dimensions_json TEXT NOT NULL DEFAULT '[]',
    task_id         TEXT NOT NULL DEFAULT '',   -- 出处，最后写它的那个任务
    analyzed_at     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (source_id, fingerprint)
)
"""

SCHEMA = SENTIMENT_RESULTS_DDL + ";\n" + """
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
    -- 「这次是全量重跑」：collect / translate / sentiment 三处的增量标记一并忽略。
    -- 必须是真列 —— tasks 是固定列表，任务字典里的自定义键根本不会被持久化，
    -- 服务重启或从库里读回时就丢了
    force_full INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

-- 旧的 sentiment 表（整份结果连同按下标排列的 results 数组塞在一个 JSON 列里）
-- 刻意不在这里建：它只应该由老库遗留下来，迁完就 DROP。写进 SCHEMA 会让它
-- 每次启动又长回来，于是「读哪一份」永远是个选择题

-- 「这个任务在什么时候跑过一次舆情分析」，仅此而已。
-- summary（情感分布 / TOP 维度 / 平均强度）**不存**：它是纯派生的，一存就会和
-- 实际展示的结论对不上 —— 结论按帖子共享之后，任务能看到的结论会比它自己分析的多
CREATE TABLE IF NOT EXISTS sentiment_runs (
    task_id TEXT PRIMARY KEY,
    analyzed_at TEXT NOT NULL
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
    -- 多模态模型对本帖配图的中文描述。落库是为了不重复付费：主贴的图会被它下面
    -- 每一条回复的整串上下文引用，不存的话增量分析每轮都要把同一张图重新描述一遍
    image_desc         TEXT NOT NULL DEFAULT '',
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
        _rekey_sentiment_results(conn)
        _drop_stored_summary(conn)
        _ensure_posts_image_desc(conn)
        _ensure_tasks_force_full(conn)
        conn.close()
        logger.info("SQLite 数据库初始化完成: %s", DB_PATH)
    except Exception as e:
        logger.error("数据库初始化失败: %s", str(e))


def _ensure_posts_image_desc(conn) -> None:
    """给既有库补上 posts.image_desc 列。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，所以新加的列必须自己 ALTER
    上去，否则老库读写都会报 no such column。幂等，做法同 _drop_stored_summary。
    """
    cols = [d[1] for d in conn.execute("PRAGMA table_info(posts)")]
    if "image_desc" in cols:
        return
    try:
        conn.execute("ALTER TABLE posts ADD COLUMN image_desc TEXT NOT NULL DEFAULT ''")
        conn.commit()
        logger.info("posts.image_desc 已补齐")
    except Exception as e:
        logger.error("补 posts.image_desc 列失败: %s", e)


def _ensure_tasks_force_full(conn) -> None:
    """给既有库补上 tasks.force_full 列。理由同 _ensure_posts_image_desc。"""
    cols = [d[1] for d in conn.execute("PRAGMA table_info(tasks)")]
    if "force_full" in cols:
        return
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN force_full INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        logger.info("tasks.force_full 已补齐")
    except Exception as e:
        logger.error("补 tasks.force_full 列失败: %s", e)


def _drop_stored_summary(conn) -> None:
    """扔掉 sentiment_runs 里那列存好的 summary。

    它现在是纯派生的（读的时候按 results 现算），留着就是一份会过期的副本 ——
    结论按帖子共享之后，任务能看到的结论比它自己分析的多，存下来的那份必然对不上。
    """
    cols = [d[1] for d in conn.execute("PRAGMA table_info(sentiment_runs)")]
    if "summary_json" not in cols:
        return
    try:
        conn.execute("ALTER TABLE sentiment_runs DROP COLUMN summary_json")
        conn.commit()
        logger.info("sentiment_runs.summary_json 已移除（改为读时现算）")
    except Exception as e:
        logger.warning("移除 summary_json 列失败（不影响功能，它已不再被读取）: %s", e)


def purge_fake_parse_failures() -> None:
    """清掉「解析失败」那种冒充结论的行，并把对应帖子的 sentiment_at 一并清空。

    模型整批没照分隔符输出时，切不出来的那几条曾被记成
    {"sentiment": "neutral", "intensity": 1, "reason_cn": "解析失败"}。那不是结论，
    是兜底占位 —— 它带着 sentiment 值绕过单条重试，又被写上 sentiment_at 永久定死，
    于是报告和情感分布里凭空多出几条「中性」。实测三个库共 10 条，其中一条正文是
    「Deze update werkt niet na de update...」，明确在抱怨固件却算成了中性。

    产生它的分支已改回记 None。这里清掉既有的：**两处都要清**，只删结论行的话
    sentiment_at 还在，增量分析永远不会再碰它们，用户只能走一次全量重跑
    （为 3 条帖子重算 258 条，还要重新付钱）。清完下一轮增量自然把它们捡回去。

    **必须排在旧 JSON 舆情 blob 的迁移之后**，所以它不在 init_db() 的补丁链里，
    而由 orchestrator 在 _migrate_sentiment() 之后调用。反过来的话，老库升上来的
    第一次启动恰好是空转的：清理先跑完，migrate_sentiment_blob() 才把 blob 里那批
    假 neutral 重新写进 sentiment_results —— 而这一次正是它最该生效的那一次，用户
    得再重启一遍才看得到效果。

    **`sentiment IS NOT NULL` 不能省**：新代码写下的占位是 sentiment 为空的诚实
    记录，它带的「解析失败」是给用户看的原因，且本来就没有 sentiment_at、下一轮
    增量自然会重算。连它一起删的话这个一次性迁移永远变不成 no-op —— 每次启动都在
    删一条马上又要写回来的行，页面上那条「未分析」还白白丢掉了原因。

    匹配的字符串是**旧版本写下的原文**，故意写死不引用现在的常量 —— 迁移认的是
    历史数据长什么样，跟着当前代码走反而会在改文案那天静默失配。
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT source_id, fingerprint FROM sentiment_results "
            "WHERE reason_cn=? AND sentiment IS NOT NULL",
            ("解析失败",),
        ).fetchall()
        if not rows:
            return
        conn.execute(
            "DELETE FROM sentiment_results WHERE reason_cn=? AND sentiment IS NOT NULL",
            ("解析失败",),
        )
        conn.executemany(
            "UPDATE posts SET sentiment_at=NULL WHERE source_id=? AND fingerprint=?",
            [tuple(r) for r in rows],
        )
        conn.commit()
        logger.info("清掉 %d 条「解析失败」占位结论，相关帖子下轮分析会重新算", len(rows))
    except Exception as e:
        # 清不掉只是那几条继续显示成「中性/解析失败」，不该拦住启动
        logger.warning("清理「解析失败」占位结论失败: %s", e)
    finally:
        conn.close()


def _rekey_sentiment_results(conn) -> None:
    """把主键含 task_id 的旧 sentiment_results 换成按帖子身份。

    同一条帖子在两个任务下各有一份结论时，**取最近分析的那份**。这在真实数据里
    有意义：fixture 那个来源被两个任务分别真跑过一次，结论确实不同。
    """
    # 上一次重建到一半失败留下的孤儿表：它里面是唯一一份完整数据，必须先接回来。
    # 不处理的话，此时 sentiment_results 已经是「新主键、空表」，下面的检测会判定
    # 「不用迁移」直接跳过 —— 全部历史结论静默永久不可见
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sentiment_results_old'"
    ).fetchone():
        logger.warning("发现上次重建残留的 sentiment_results_old，从它恢复")
        conn.execute("DROP TABLE IF EXISTS sentiment_results")
        conn.execute("ALTER TABLE sentiment_results_old RENAME TO sentiment_results")
        conn.commit()

    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sentiment_results'"
    ).fetchone()
    if not sql or "PRIMARY KEY (task_id" not in (sql["sql"] or ""):
        return
    try:
        # 旧表没有 analyzed_at，出处时间从 sentiment_runs 取。
        # 按 analyzed_at 升序，后写的自然覆盖先写的 = 同一条帖子取最近一次的结论；
        # 时间为空的排在最前，也就是最先被覆盖 —— 有确切时间的那份优先
        rows = conn.execute("""
            SELECT r.source_id, r.fingerprint, r.sentiment, r.intensity, r.reason_cn,
                   r.dimensions_json, r.task_id,
                   COALESCE(n.analyzed_at, '') AS analyzed_at
            FROM sentiment_results r
            LEFT JOIN sentiment_runs n ON n.task_id = r.task_id
            ORDER BY COALESCE(n.analyzed_at, '')
        """).fetchall()
        # **必须显式开事务**：DDL 在这里不会自动回滚，中途抛异常就会留下
        # 「空的新表 + 孤立的旧表」，而外层只打一条 error 日志放过去。
        # 实测过：不加事务时失败后 sentiment_results 是 0 行
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE sentiment_results RENAME TO sentiment_results_old")
        conn.execute(SENTIMENT_RESULTS_DDL)
        conn.executemany(
            """INSERT OR REPLACE INTO sentiment_results
               (source_id, fingerprint, sentiment, intensity, reason_cn,
                dimensions_json, task_id, analyzed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [tuple(r) for r in rows],
        )
        conn.execute("DROP TABLE sentiment_results_old")
        conn.commit()
        kept = conn.execute("SELECT COUNT(*) FROM sentiment_results").fetchone()[0]
        logger.info("舆情结论改按帖子身份存：%d 行合并为 %d 行", len(rows), kept)
    except Exception as e:
        conn.rollback()
        logger.error("重建 sentiment_results 失败，已回滚，数据未动: %s", e)


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

    **空正文帖照搬不误**（`drop_empty=False`）：迁移是照原样重建历史，旧舆情 blob
    的 results[i] 对齐的正是这个数组的第 i 条。少搬一条，条数就对不上，
    migrate_sentiment_blob() 会整份跳过，那个来源的历史结论就再也进不来了。
    新采集到的空正文帖仍然被挡在 upsert_posts() 门口。
    """
    if not os.path.exists(path) or count_posts(source_id) > 0:
        return 0
    with open(path, "r", encoding="utf-8") as f:
        loaded = json.load(f)
    posts = loaded.get("posts") or []
    for p in posts:
        p.setdefault("source", source_id)
    added = upsert_posts(source_id, posts, drop_empty=False)
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
                created_at, started_at, completed_at, force_full)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                1 if task.get("force_full") else 0,
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
    analyzed_at = data.get("analyzed_at") or datetime.now().isoformat()
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
            source_id, fingerprint, r.get("sentiment"), r.get("intensity"),
            r.get("reason_cn") or "",
            json.dumps(r.get("dimensions") or [], ensure_ascii=False),
            task_id, analyzed_at,
        ))

    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_runs (task_id, analyzed_at) VALUES (?,?)",
            (task_id, analyzed_at),
        )
        # **不按 task_id 删**：结论属于帖子，别的任务分析过的那些不该被这一轮抹掉。
        # OR REPLACE 而非裸 INSERT：同一批 posts 里出现两条 (source_id, fingerprint)
        # 相同的记录并非不可能（指纹只吃 username|timestamp|content[:100]，翻页错位
        # 就能撞上）。裸 INSERT 会抛 IntegrityError 让整个事务回滚 —— 这一轮
        # 花钱算出来的结论一条都存不下，而调用方还在报「分析完成」
        conn.executemany(
            """INSERT OR REPLACE INTO sentiment_results
               (source_id, fingerprint, sentiment, intensity, reason_cn, dimensions_json,
                task_id, analyzed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
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
    """取这批帖子已知的舆情结论，按 posts 的顺序摆成下标数组。

    **按帖子身份查，不按 task_id 过滤**：结论是对帖子下的，哪个任务触发的分析不改变
    它对哪条帖子成立。`_processed.sentiment_at` 本来就跨任务共享（同一条帖子不会被
    重复花钱分析），结论必须同一个粒度 —— 按任务过滤的话，第二个任务跑同一批数据，
    页面上 94 条里会有 90 条显示「未分析」，而它们早就分析过了。

    这与「绝不跨任务顶替」并不矛盾：当初出事的是**按下标**取别的任务的整份结果，
    那个数组是按别人的帖子列表编号的。现在按 (source_id, fingerprint) 取，
    取到的就是这条帖子自己的结论，取不到就是真没有。
    """
    if not posts:
        return None
    conn = _get_conn()
    try:
        run = conn.execute(
            "SELECT * FROM sentiment_runs WHERE task_id = ?", (task_id,)
        ).fetchone()
        keys = {_post_identity(p) for p in posts}
        by_identity = {}
        latest = ""
        for r in conn.execute("SELECT * FROM sentiment_results").fetchall():
            key = (r["source_id"], r["fingerprint"])
            if key not in keys:
                continue
            by_identity[key] = {
                "sentiment": r["sentiment"],
                "intensity": r["intensity"],
                "reason_cn": r["reason_cn"],
                "dimensions": json.loads(r["dimensions_json"] or "[]"),
            }
            latest = max(latest, r["analyzed_at"] or "")
    except Exception as e:
        logger.error("获取舆情结果失败: %s", str(e))
        return None
    finally:
        conn.close()

    # 本任务没跑过、这批帖子也一条结论都没有 —— 那就是真的还没分析
    if not run and not by_identity:
        return None

    results = [by_identity.get(_post_identity(p)) for p in posts]
    return {
        "task_id": task_id,
        # 本任务跑过就用它自己的时间，否则用这批结论里最新的那个
        "analyzed_at": run["analyzed_at"] if run else latest,
        "total": len(posts),
        "success": sum(1 for r in results if r and r.get("sentiment")),
        "failed": sum(1 for r in results if not r or not r.get("sentiment")),
        # summary 由调用方按 results 现算 —— 存一份就会和展示的结论对不上
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

    # 条数对不上就整份跳过。曾经想「只迁对得上的前缀」把多出来的尾部丢掉，
    # 那假设了缺文件的来源排在最后 —— 实测踩过：某任务采了 tweakers(9) +
    # group_feed(8)，先跑的 tweakers 落盘文件后来被删，幸存的 8 条 group_feed
    # 帖子于是套上了 results[0:8]，也就是 tweakers 的结论。下面那条不变量拦不住
    # （8 条帖子确实都带着 sentiment_at），错位悄无声息。
    # 少迁一份还能从旧表重来，迁错了没人看得出来。
    if len(results) != len(posts):
        logger.error("舆情 %s 有 %d 条结果但当前是 %d 条帖子，对不上，跳过迁移",
                     task_id, len(results), len(posts))
        return False

    for i in range(len(results)):
        r = results[i]
        if r and r.get("sentiment") and not (posts[i].get("_processed") or {}).get("sentiment_at"):
            logger.error("舆情 %s 第 %d 条有结论但帖子没有 sentiment_at 标记，"
                         "下标对不上，跳过迁移", task_id, i)
            return False

    save_sentiment(task_id, data, posts)
    logger.info("舆情 %s 已按帖子身份重存 %d 条结论", task_id, len(results))
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
    if row["image_desc"]:
        post["image_desc"] = row["image_desc"]
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


def drop_empty_posts(posts: List[dict]) -> List[dict]:
    """丢掉**既没正文也没配图**的帖子，并把它们的评论提成主贴。

    真空帖在报告里是一行什么都没有的记录，还白占一次翻译调用（实测那条已被标成
    「已翻译」，译文是空串），舆情又永远分析不了它 —— 页面上就一直挂着一条「未分析」。

    **有配图就不算空**：图会在舆情阶段被多模态转成中文描述，那正是最该看的一类内容。
    这里只能看 `images` 不能看 `image_desc` —— 入库时图还没被理解过。曾经只看正文，
    实测因此静默丢过一条：media/src_b32bc603/6680d5f13a6b2b4c_0.jpg 还躺在盘上
    （HYXi 安装检查报告，总分 88、发电异常 8/20 标橙），posts 表里一行都没有。
    采集脚本先下图、这个口再丢帖子，图留下、帖子没了，连「未分析」都不显示。

    **有评论就不算空**：没人会去评论一片空白，所以「空正文 + 有人在下面发言」几乎一定
    是**提取失败**而不是真的空帖。这种父贴必须留着 —— 丢了它，评论会被下面那段提成主贴，
    而那个提升是**不可逆的**：下一轮正文提对了，父贴换个指纹重新入库（指纹吃正文），
    评论却还挂在主贴身份上，除非它恰好又被重新提取到。真站核实过两例：
      · 「Mijn HyXi Halo is gekoppeld aan:」那条正文没提出来，两条评论被提成主贴
      · 纯图主贴被丢，回复「Is bij mij ook zo…」被提成主贴，舆情因此判成 neutral，
        而它其实是在附和一条报「发电异常 8/20」的故障帖
    代价是报告里会留一行空白的「未分析」，但那是实话 —— 「这里有条读不出来的帖子，
    以下是它的回复」，比把回复冒充成主贴诚实得多。

    **丢主贴不能连累它的评论**：实测那条空主贴下面挂着 2 条有内容的评论，采集脚本
    的 flatten() 是按「主贴 → 它的评论」嵌套遍历的，在那里过滤会把评论一起带走。
    所以过滤放在这个唯一的入库口。真被丢掉的父贴（整棵子树都没内容）才把孤儿评论
    就地提成主贴，不留悬空 parent。
    """
    empty = {
        p.get("fingerprint") for p in posts
        if p.get("fingerprint")
        and not (p.get("content") or "").strip()
        and not (p.get("images") or [])
    }
    if not empty:
        return posts

    # 有**非空**的帖子认它当父贴，就把它捞回来。用空评论去捞空父贴没有意义：
    # 整棵子树都没内容时，它就是真的什么都没有
    anchored = {
        p.get("parent_fingerprint") for p in posts
        if p.get("parent_fingerprint") in empty and p.get("fingerprint") not in empty
    }
    dropped = empty - anchored
    # 这条日志必须在下面那个提前返回**之前** —— 最常见的情况恰恰是「捞回一条、一条没丢」，
    # 写在后面就等于这件事从来不出现在日志里
    if anchored:
        logger.info("%d 条空正文帖子因为下面有人发言而保留（多半是正文提取失败）", len(anchored))
    if not dropped:
        return posts

    kept = []
    for post in posts:
        if post.get("fingerprint") in dropped:
            continue
        if post.get("parent_fingerprint") in dropped:
            post = dict(post)
            post["parent_fingerprint"] = None
            post["reply_level"] = 0
        kept.append(post)
    logger.info("丢弃 %d 条无正文无配图的帖子，保留 %d 条", len(dropped), len(kept))
    return kept


def upsert_posts(source_id: str, posts: List[dict], drop_empty: bool = True) -> int:
    """写入采集结果，返回新增条数。

    **已存在的帖子只更新采集字段，绝不覆盖 translation / translated / sentiment_at**。
    整体覆盖等于把已翻译的帖子重新变成新帖，下一轮再付一次翻译钱、舆情也重算一遍
    （group_feed.js 曾经就是这么错的）。

    空正文帖在这里就被挡掉（见 drop_empty_posts）—— 这是 posts 表唯一的入口，
    放在这里三个采集器和以后新加的采集器都不用各自记得过滤一遍。

    `drop_empty=False` 只给历史迁移用：迁移是**照原样重建**，不是采集。少搬一条
    就会让入库条数对不上旧舆情 blob 的 results 长度，`migrate_sentiment_blob()`
    的「条数不等整份跳过」会因此把那个来源的历史结论永久挡在门外。
    """
    if drop_empty:
        posts = drop_empty_posts(posts)
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
                # seq 保持不变 —— 它是全链路的顺序锚点，动一下所有历史结论就错位了。
                # translation / translated / sentiment_at / image_desc 也一律不在这条
                # UPDATE 里：它们都是花钱换来的结果，重新采集不得把它们冲回空值
                #
                # **images 同理，但只在本轮真抓到图时才覆盖**。写死成「采集到什么就是
                # 什么」的话，全量重跑撞上一次网络抖动就会把 images_json 写成 []，
                # 而图片文件还好端端躺在 media 目录里 —— 页面上整批消失且毫无提示。
                # 代价是帖子里的图真被作者删掉时库里会留着已失效的路径，渲染侧本来
                # 就要处理取不到图的情况。
                images = post.get("images") or []
                if images:
                    conn.execute(
                        "UPDATE posts SET images_json=? WHERE source_id=? AND fingerprint=?",
                        (json.dumps(images, ensure_ascii=False), source_id, fp),
                    )
                conn.execute(
                    """UPDATE posts SET username=?, timestamp=?, content=?, page_number=?,
                       message_id=?, parent_fingerprint=?, reply_level=?
                       WHERE source_id=? AND fingerprint=?""",
                    (
                        post.get("username", ""), post.get("timestamp", ""),
                        post.get("content", ""), int(post.get("page_number", 1) or 1),
                        post.get("message_id", ""), post.get("parent_fingerprint"),
                        int(post.get("reply_level", 0) or 0),
                        source_id, fp,
                    ),
                )
                continue
            processed = post.get("_processed") or {}
            conn.execute(
                """INSERT INTO posts (source_id, fingerprint, seq, username, timestamp,
                   content, translation, page_number, message_id, parent_fingerprint,
                   reply_level, images_json, image_desc, translated, sentiment_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, fp, next_seq, post.get("username", ""),
                    post.get("timestamp", ""), post.get("content", ""),
                    post.get("translation", ""), int(post.get("page_number", 1) or 1),
                    post.get("message_id", ""), post.get("parent_fingerprint"),
                    int(post.get("reply_level", 0) or 0),
                    json.dumps(post.get("images") or [], ensure_ascii=False),
                    post.get("image_desc", ""),
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


def save_image_descs(posts: List[dict]) -> int:
    """把多模态模型给出的图片描述落库。

    只写 image_desc 这一个字段，理由同 mark_sentiment_analyzed —— 别的列都是别的
    步骤花钱换来的。空描述不写：那通常意味着模型没配、配额用尽或图丢了，
    存一个空串会让下一轮以为「已经理解过、就是没内容」，从此再也不重试。
    """
    conn = _get_conn()
    try:
        updated = 0
        for post in posts:
            desc = (post.get("image_desc") or "").strip()
            fp = post.get("fingerprint")
            if not (desc and fp):
                continue
            cur = conn.execute(
                "UPDATE posts SET image_desc=? WHERE source_id=? AND fingerprint=?",
                (desc, post.get("source") or "tweakers", fp),
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
        "force_full": bool(row["force_full"]),
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
