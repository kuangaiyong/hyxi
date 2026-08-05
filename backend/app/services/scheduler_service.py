"""定时任务调度服务 - 基于 APScheduler + SQLite"""

import uuid
import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from app.config import settings
from app.services import storage
from app.services.orchestrator import orchestrator
from app.services.progress_manager import progress_manager

logger = logging.getLogger("hyxi.scheduler")


# 预设调度选项
PRESETS = {
    "hourly": {"label": "每小时", "trigger": "interval", "hours": 1},
    "6h":     {"label": "每6小时", "trigger": "interval", "hours": 6},
    "12h":    {"label": "每12小时", "trigger": "interval", "hours": 12},
    "daily":  {"label": "每天", "trigger": "cron", "cron_expr": None},
}


async def _job_callback(scheduled_id: str):
    """APScheduler 回调（模块级函数，避免序列化 scheduler 实例）"""
    task_id = str(uuid.uuid4())
    svc = scheduler_service
    callback_ok = False
    try:
        cfg = svc.get_one(scheduled_id)
        if not cfg:
            logger.warning("定时任务 %s 配置不存在，跳过执行", scheduled_id)
            return
        # 创建任务时打上 scheduled_by 标记
        desc = cfg["description"]
        orchestrator.create_task(task_id, desc)
        task = orchestrator.get_task(task_id)
        if task:
            task["scheduled_by"] = scheduled_id
            orchestrator._persist()
        orchestrator.run_task_async(task_id)
        logger.info("定时任务 %s 触发，创建任务 %s", scheduled_id, task_id)
        callback_ok = True
    except Exception as e:
        logger.error("定时任务 %s 回调失败: %s", scheduled_id, str(e), exc_info=True)

    # 更新执行历史（即使创建失败也记录）
    try:
        configs = svc._load_configs()
        for c in configs:
            if c["id"] == scheduled_id:
                c["last_run"] = datetime.now().isoformat()
                c.setdefault("history", []).append({
                    "task_id": task_id,
                    "time": datetime.now().isoformat(),
                    "status": "started" if callback_ok else "failed",
                })
                # 只保留最近 20 条
                c["history"] = c["history"][-20:]
                break
        svc._save_configs(configs)
    except Exception as e:
        logger.error("更新定时任务 %s 执行历史失败: %s", scheduled_id, str(e))


class SchedulerService:
    """定时任务调度器"""

    def __init__(self):
        # 触发器与业务配置同库：全部持久化只有 hyxi.db 一个文件，备份和搬迁只用拿一份。
        # APScheduler 自己建 apscheduler_jobs 表，与业务表互不干涉
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{storage.DB_PATH}")}

        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            timezone="Asia/Shanghai",
        )
        self._running_jobs: dict[str, str] = {}  # scheduled_task_id -> apscheduler_job_id

    def start(self):
        """启动调度器并加载持久化任务"""
        self.scheduler.start()

        # 恢复自定义任务配置。单条坏配置只跳过自己——否则一个畸形的 time
        # 就能让整个调度器加载中断，服务再也起不来。
        for cfg in self._load_configs():
            if not cfg.get("enabled", True):
                continue
            try:
                self._add_job(cfg)
            except Exception as e:
                logger.error("定时任务 %s 配置无效，已跳过: %s", cfg.get("id"), e)

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception as e:
            # start() 失败时调度器从未运行，shutdown 会抛 SchedulerNotRunningError
            logger.warning("调度器关闭异常: %s", e)

    # ===== 任务配置持久化 =====

    def _load_configs(self) -> list:
        return storage.load_schedules()

    def _save_configs(self, configs: list):
        """整表覆写。调用方一贯是「读全量 → 改一条 → 写回」，逐条 upsert
        补不上「某条被删掉了」这种情况，所以先算差集再删。"""
        keep = {c["id"] for c in configs}
        for existing in storage.load_schedules():
            if existing["id"] not in keep:
                storage.delete_schedule(existing["id"])
        for cfg in configs:
            storage.save_schedule(cfg)

    def _add_job(self, cfg: dict):
        """向 APScheduler 添加一个 job"""
        preset = PRESETS.get(cfg["interval"])
        if not preset:
            return

        if cfg["interval"] == "daily":
            time_str = cfg.get("time", "09:00")
            hour, minute = map(int, time_str.split(":"))
            trigger = CronTrigger(hour=hour, minute=minute)
        else:
            hours = preset["hours"]
            trigger = IntervalTrigger(hours=hours)

        job_id = self.scheduler.add_job(
            _job_callback,
            trigger=trigger,
            id=cfg["id"],
            kwargs={"scheduled_id": cfg["id"]},
            replace_existing=True,
        )
        self._running_jobs[cfg["id"]] = job_id

    # ===== CRUD =====

    def get_all(self) -> list:
        configs = self._load_configs()
        for cfg in configs:
            job = self.scheduler.get_job(cfg["id"])
            if job:
                try:
                    cfg["next_run"] = job.next_run_time.isoformat() if job.next_run_time else None
                except AttributeError:
                    cfg["next_run"] = None
            else:
                cfg["next_run"] = None
        return configs

    def get_one(self, scheduled_id: str) -> Optional[dict]:
        for cfg in self._load_configs():
            if cfg["id"] == scheduled_id:
                return cfg
        return None

    def create(self, description: str, interval: str, time_str: str = "09:00") -> dict:
        cfg = {
            "id": str(uuid.uuid4()),
            "description": description,
            "interval": interval,
            "time": time_str,
            "enabled": True,
            "created_at": datetime.now().isoformat(),
        }
        # 先建 job 再落盘：反过来的话，一条建不起 job 的脏配置会被持久化，
        # 下次启动读回来又炸一次
        self._add_job(cfg)

        configs = self._load_configs()
        configs.append(cfg)
        self._save_configs(configs)
        return cfg

    def update(self, scheduled_id: str, updates: dict) -> Optional[dict]:
        configs = self._load_configs()
        for cfg in configs:
            if cfg["id"] == scheduled_id:
                cfg.update(updates)
                # 重新注册 job。暂停中的任务本就没有 job，remove 会抛 JobLookupError
                try:
                    self.scheduler.remove_job(scheduled_id)
                except Exception:
                    pass
                if cfg.get("enabled", True):
                    # 与 create() 同理，建不起 job 就不落盘
                    self._add_job(cfg)
                self._save_configs(configs)
                return cfg
        return None

    def delete(self, scheduled_id: str) -> bool:
        configs = self._load_configs()
        new_configs = [c for c in configs if c["id"] != scheduled_id]
        if len(new_configs) < len(configs):
            self._save_configs(new_configs)
            try:
                self.scheduler.remove_job(scheduled_id)
            except Exception:
                pass
            return True
        return False

    def toggle(self, scheduled_id: str) -> Optional[dict]:
        configs = self._load_configs()
        for cfg in configs:
            if cfg["id"] == scheduled_id:
                cfg["enabled"] = not cfg.get("enabled", True)
                self._save_configs(configs)
                if cfg["enabled"]:
                    self._add_job(cfg)
                else:
                    try:
                        self.scheduler.remove_job(scheduled_id)
                    except Exception:
                        pass
                return cfg
        return None

    async def run_now(self, scheduled_id: str) -> str:
        """立即手动触发一次"""
        cfg = self.get_one(scheduled_id)
        if not cfg:
            raise ValueError("定时任务不存在")
        await _job_callback(scheduled_id)
        # 返回最近创建的任务ID
        tasks = orchestrator.get_all_tasks()
        return tasks[0]["id"] if tasks else ""

# 全局单例（必须在 _job_callback 之前创建）
scheduler_service = SchedulerService()
