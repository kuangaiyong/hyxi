"""任务编排引擎 - LLM 意图解析 + 逐步执行（含持久化）"""

import json
import os
import asyncio
import traceback
from typing import Dict, List, Optional
from datetime import datetime
from app.models import LLMConfig, TaskStatus, PlanStep
from app.config import settings
from app.services.llm_service import LLMService
from app.services.scraper_service import ScraperService
from app.services.translator_service import TranslatorService
from app.services.excel_service import ExcelService
from app.services.progress_manager import progress_manager


class TaskOrchestrator:
    """编排任务执行：解析意图 → 逐步执行 → 汇总结果"""

    def __init__(self):
        self.tasks: Dict[str, dict] = {}
        self._running_tasks: set = set()
        self._sentiment_running: set = set()
        self._persist_path = os.path.join(settings.data_dir, "tasks.json")
        self._load_tasks()

    # ===== 持久化 =====

    def _load_tasks(self):
        """从磁盘加载历史任务，并清理异常终止的任务"""
        if os.path.exists(self._persist_path):
            try:
                with open(self._persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for task_data in data.get("tasks", []):
                    tid = task_data["id"]
                    # 反序列化 datetime
                    for field in ("created_at", "started_at", "completed_at"):
                        if task_data.get(field):
                            task_data[field] = datetime.fromisoformat(task_data[field])
                    # 清理异常终止的非终态任务（如进程被kill）
                    if task_data["status"] in ("pending", "parsing", "running"):
                        task_data["status"] = TaskStatus.CANCELLED
                        task_data["completed_at"] = datetime.now()
                        task_data.setdefault("logs", []).append({
                            "time": datetime.now().isoformat(),
                            "level": "warning",
                            "message": "服务重启，未完成的任务已自动取消。",
                        })
                    self.tasks[tid] = task_data
                self._save_tasks()  # 保存清理后的状态
            except Exception:
                pass

    def _save_tasks(self):
        """保存所有任务到磁盘"""
        os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
        data = {"tasks": []}
        for t in self.tasks.values():
            item = dict(t)
            # 序列化 datetime
            for field in ("created_at", "started_at", "completed_at"):
                if item.get(field) and isinstance(item[field], datetime):
                    item[field] = item[field].isoformat()
            data["tasks"].append(item)
        with open(self._persist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _persist(self):
        """保存（同步到磁盘）"""
        try:
            self._save_tasks()
        except Exception:
            pass

    def create_task(self, task_id: str, description: str):
        """注册新任务"""
        self.tasks[task_id] = {
            "id": task_id,
            "status": TaskStatus.PENDING,
            "description": description,
            "plan": [],
            "progress": 0.0,
            "current_step": None,
            "result": None,
            "error_message": None,
            "logs": [],
            "created_at": datetime.now(),
            "started_at": None,
            "completed_at": None,
        }
        self._persist()

    def get_task(self, task_id: str) -> Optional[dict]:
        return self.tasks.get(task_id)

    def get_all_tasks(self) -> List[dict]:
        tasks = list(self.tasks.values())
        tasks.sort(key=lambda t: t.get("created_at", datetime.min), reverse=True)
        return tasks

    def cancel_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task and task["status"] in (TaskStatus.PENDING, TaskStatus.PARSING, TaskStatus.RUNNING):
            task["status"] = TaskStatus.CANCELLED
            task["completed_at"] = datetime.now()
            task["_cancelled"] = True  # 取消令牌，execute_task 循环中检查
            self._running_tasks.discard(task_id)
            self._persist()
            return True
        return False

    async def execute_task(self, task_id: str):
        """执行任务的主入口"""
        task = self.tasks.get(task_id)
        if not task:
            return

        # 检查并发限制
        if len(self._running_tasks) >= settings.max_concurrent_tasks:
            await progress_manager.emit(task_id, "error", {
                "message": "已有任务正在运行，请等待完成后再提交。",
            })
            task["status"] = TaskStatus.FAILED
            task["error_message"] = "并发限制"
            self._persist()
            return

        self._running_tasks.add(task_id)
        task["status"] = TaskStatus.PARSING
        task["started_at"] = datetime.now()
        self._persist()

        llm_service = None
        try:
            # 加载 LLM 配置
            config_path = settings.config_file
            if not os.path.exists(config_path):
                raise Exception("请先配置 LLM API")

            with open(config_path, "r") as f:
                cfg_data = json.load(f)

            llm_config = LLMConfig(**cfg_data)
            llm_service = LLMService(llm_config)

            # ===== Step 0: 意图解析 =====
            task["current_step"] = "解析任务意图"
            self._persist()
            await progress_manager.emit(task_id, "step_start", {
                "step": -1,
                "action": "parse_intent",
                "message": "正在调用 LLM 解析任务意图...",
            })

            plan_data = await llm_service.parse_intent(task["description"])

            plan = [PlanStep(**s) for s in plan_data.get("plan", [])]
            task["plan"] = [s.model_dump() for s in plan]
            self._persist()

            await progress_manager.emit(task_id, "step_complete", {
                "step": -1,
                "action": "parse_intent",
                "plan": [s.model_dump() for s in plan],
                "message": f"解析完成，共 {len(plan)} 个步骤",
            })

            if not plan:
                raise Exception("无法解析任务意图，请更详细地描述您的需求。")

            # ===== 逐步执行 =====
            task["status"] = TaskStatus.RUNNING
            self._persist()
            context: dict = {}

            for idx, step in enumerate(plan):
                if task.get("_cancelled"):
                    await self._task_log(task_id, "info", "任务已取消")
                    break

                step.status = "running"
                task["current_step"] = f"执行: {step.action}"
                task["plan"][idx]["status"] = "running"
                self._persist()

                await progress_manager.emit(task_id, "step_start", {
                    "step": idx,
                    "action": step.action,
                    "message": f"开始执行步骤 {idx + 1}: {step.action}",
                })

                try:
                    if step.action == "scrape":
                        # 增量模式：默认开启
                        step.params.setdefault("incremental", True)
                        result = await ScraperService.execute(
                            task_id, step.params, progress_manager
                        )
                        context["posts"] = result.get("posts", [])
                        context["thread_id"] = step.params.get("thread_id")
                        context["total_pages"] = result.get("total_pages", 0)
                        context["raw_json"] = result

                    elif step.action == "translate":
                        posts = context.get("posts", [])
                        # 如果没有 scrape 步骤，从已有 JSON 加载
                        if not posts:
                            thread_id = step.params.get("thread_id") or _extract_thread_id(task)
                            json_path = os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")
                            if os.path.exists(json_path):
                                await self._task_log(task_id, "info", f"从已有文件加载数据: {json_path}")
                                with open(json_path, "r", encoding="utf-8") as f:
                                    loaded = json.load(f)
                                posts = loaded.get("posts", [])
                                context["thread_id"] = thread_id
                                context["total_pages"] = loaded.get("total_pages", 0)
                            else:
                                raise Exception(f"没有可翻译的帖子数据，请先执行抓取步骤。找不到文件: {json_path}")
                        if not posts:
                            raise Exception("没有可翻译的帖子数据")

                        # 增量：过滤已翻译的帖子
                        already = [p for p in posts if p.get("_processed", {}).get("translated") and p.get("translation")]
                        pending = [p for p in posts if not p.get("_processed", {}).get("translated") or not p.get("translation")]
                        if already:
                            await self._task_log(task_id, "info", f"增量翻译: {len(already)} 条已翻译跳过, {len(pending)} 条待翻译")
                        if pending:
                            result = await TranslatorService.execute(
                                task_id, pending, step.params, progress_manager
                            )
                            for p in result.get("posts", []):
                                p.setdefault("_processed", {})["translated"] = True
                            # 合并并恢复原始顺序：用已翻译帖子的指纹定位
                            fp_to_post = {p.get("fingerprint"): p for p in already}
                            for p in result.get("posts", []):
                                fp_to_post[p.get("fingerprint")] = p
                            all_ordered = [fp_to_post.get(p.get("fingerprint"), p) for p in (already + pending)]
                            context["posts"] = all_ordered
                        else:
                            context["posts"] = already
                            await self._task_log(task_id, "info", "所有帖子已翻译，跳过")
                        # 保存回 JSON
                        tid = context.get("thread_id") or _extract_thread_id(task)
                        if tid:
                            self._save_translated_json(tid, context["posts"])

                    elif step.action == "generate_excel":
                        posts = context.get("posts", [])
                        if not posts:
                            # 同样从已有 JSON 加载
                            thread_id = step.params.get("thread_id") or _extract_thread_id(task)
                            json_path = os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")
                            if os.path.exists(json_path):
                                with open(json_path, "r", encoding="utf-8") as f:
                                    loaded = json.load(f)
                                posts = loaded.get("posts", [])
                                context["thread_id"] = thread_id
                                context["total_pages"] = loaded.get("total_pages", 0)
                        if not posts:
                            raise Exception("没有帖子数据可生成 Excel")
                        result = await ExcelService.execute(
                            task_id, posts, step.params, progress_manager
                        )
                        context["excel_path"] = result.get("file_path", "")
                        context["excel_name"] = result.get("file_name", "")

                    else:
                        await self._task_log(task_id, "warning", f"未知动作: {step.action}，已跳过")

                    step.status = "completed"
                    task["plan"][idx]["status"] = "completed"
                    self._persist()

                    await progress_manager.emit(task_id, "step_complete", {
                        "step": idx,
                        "action": step.action,
                        "message": f"步骤 {idx + 1} 完成: {step.action}",
                    })

                except Exception as e:
                    step.status = "failed"
                    step.error = str(e)
                    task["plan"][idx]["status"] = "failed"
                    task["plan"][idx]["error"] = str(e)
                    self._persist()

                    await progress_manager.emit(task_id, "error", {
                        "step": idx,
                        "action": step.action,
                        "message": f"步骤 {idx + 1} 失败: {str(e)}",
                    })
                    raise

            # ===== 汇总结果 =====
            task["result"] = {
                "thread_id": context.get("thread_id"),
                "total_posts": len(context.get("posts", [])),
                "total_pages": context.get("total_pages", 0),
                "excel_name": context.get("excel_name", ""),
                "excel_path": context.get("excel_path", ""),
            }
            task["status"] = TaskStatus.COMPLETED
            self._persist()

            await progress_manager.emit(task_id, "task_complete", {
                "task_id": task_id,
                "status": "completed",
                "result": task["result"],
            })

        except Exception as e:
            task["status"] = TaskStatus.FAILED
            task["error_message"] = str(e)
            self._persist()
            await progress_manager.emit(task_id, "task_complete", {
                "task_id": task_id,
                "status": "failed",
                "error": str(e),
            })
            await progress_manager.emit(task_id, "error", {
                "message": f"任务失败: {str(e)}",
            })

        finally:
            task["completed_at"] = datetime.now()
            self._running_tasks.discard(task_id)
            self._persist()
            if llm_service:
                await llm_service.close()

    def _save_translated_json(self, thread_id: int, posts: list):
        """保存带翻译的帖子数据到 JSON 文件"""
        json_path = os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["posts"] = posts
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    async def _task_log(self, task_id: str, level: str, message: str):
        """记录日志到任务并发送 SSE"""
        task = self.tasks.get(task_id)
        if task:
            task.setdefault("logs", []).append({
                "time": datetime.now().isoformat(),
                "level": level,
                "message": message,
            })
            self._persist()
        await progress_manager.emit(task_id, "log", {
            "level": level,
            "message": message,
        })

    def run_task_async(self, task_id: str):
        """在后台运行任务"""
        asyncio.create_task(self.execute_task(task_id))

    # ===== 清理 =====

    def delete_task(self, task_id: str) -> bool:
        """删除任务记录"""
        if task_id in self.tasks:
            del self.tasks[task_id]
            self._persist()
            return True
        return False

    # ===== 舆情分析 =====

    def is_sentiment_running(self, task_id: str) -> bool:
        return task_id in self._sentiment_running

    def run_sentiment_async(self, task_id: str, all_posts: list, pending_posts: list, existing_results: list = None):
        """后台启动舆情分析（增量：仅分析 pending_posts，合并已有结果）"""
        from app.services.sentiment_service import SentimentService
        self._sentiment_running.add(task_id)

        async def _run():
            try:
                # 构建指纹→绝对索引映射（用于合并）
                fp_to_idx = {}
                for i, p in enumerate(all_posts):
                    fp = p.get("fingerprint")
                    if fp:
                        fp_to_idx[fp] = i

                await SentimentService.analyze(task_id, pending_posts, progress_manager, existing_results, fp_to_idx)
                await progress_manager.emit(task_id, "sentiment_complete", {
                    "task_id": task_id,
                    "status": "completed",
                })
            except Exception as e:
                await progress_manager.emit(task_id, "sentiment_complete", {
                    "task_id": task_id,
                    "status": "failed",
                    "error": str(e),
                })
            finally:
                self._sentiment_running.discard(task_id)

        asyncio.create_task(_run())


# 全局单例
orchestrator = TaskOrchestrator()


def _extract_thread_id(task: dict) -> int:
    """从任务描述或计划中提取帖子ID"""
    import re
    desc = task.get("description", "")
    plan = task.get("plan", [])

    # 先从 plan 中找
    for step in plan:
        if step.get("action") == "scrape":
            tid = step.get("params", {}).get("thread_id")
            if tid:
                return tid

    # 从描述中提取
    match = re.search(r"(\d{5,})", desc)
    return int(match.group(1)) if match else 0
