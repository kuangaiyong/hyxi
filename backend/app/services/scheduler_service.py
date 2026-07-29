"""定时任务调度服务 - 基于 APScheduler + SQLite"""

import os
import json
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
        db_path = os.path.join(settings.data_dir, "scheduler.db")
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")}

        self.scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            timezone="Asia/Shanghai",
        )
        self._config_path = os.path.join(settings.data_dir, "scheduled_tasks.json")
        self._running_jobs: dict[str, str] = {}  # scheduled_task_id -> apscheduler_job_id

    def start(self):
        """启动调度器并加载持久化任务"""
        self.scheduler.start()

        # 恢复自定义任务配置
        configs = self._load_configs()
        for cfg in configs:
            if cfg.get("enabled", True):
                self._add_job(cfg)

    def shutdown(self):
        self.scheduler.shutdown(wait=False)

    # ===== 任务配置持久化 =====

    def _load_configs(self) -> list:
        if os.path.exists(self._config_path):
            with open(self._config_path, "r") as f:
                return json.load(f)
        return []

    def _save_configs(self, configs: list):
        os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)

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
        configs = self._load_configs()
        configs.append(cfg)
        self._save_configs(configs)

        self._add_job(cfg)
        return cfg

    def update(self, scheduled_id: str, updates: dict) -> Optional[dict]:
        configs = self._load_configs()
        for cfg in configs:
            if cfg["id"] == scheduled_id:
                cfg.update(updates)
                self._save_configs(configs)
                # 重新注册 job
                self.scheduler.remove_job(scheduled_id)
                if cfg.get("enabled", True):
                    self._add_job(cfg)
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
