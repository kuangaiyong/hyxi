"""任务编排引擎 - LLM 意图解析 + 逐步执行（含持久化）"""

import json
import asyncio
import traceback
import logging
from typing import Dict, List, Optional
from datetime import datetime
from app.models import LLMConfig, TaskStatus, PlanStep
from app.config import settings
from app.logging_config import get_logger
from app.collectors import get_collector
from app.services import source_service
from app.services.post_tree import post_key, thread_of
from app.services.llm_service import LLMService
from app.services.collector_runner import CollectorRunner, ManualAuthRequired
from app.services.translator_service import TranslatorService, needs_translation
from app.services.excel_service import ExcelService
from app.services.progress_manager import progress_manager
from app.services.storage import (
    init_db, migrate_from_json, save_task, load_all_tasks,
    delete_task as db_delete_task,
    legacy_sentiment_task_ids, migrate_sentiment_blob, drop_legacy_sentiment_table,
    discard_legacy_sentiment, migrate_posts_file, retire_file,
    purge_fake_parse_failures, merge_duplicate_posts,
)
from app.services import storage

logger = get_logger(__name__)

# 结果页补译每翻完这么多条就落一次库：几百条要翻十几分钟，整批翻完才写的话，
# 中途关掉应用这一整批的钱就白花了（2026-09-15 便携包中途被关过一次）
BACKFILL_CHUNK_SIZE = 50


def translation_channel(task_id: str) -> str:
    """补译进度走的频道。**不能直接用 task_id**：任务进度流和舆情流共用那个频道，
    同一任务的舆情页正在分析时，翻译进度会串到舆情进度条上"""
    return f"{task_id}:translate"


class _BackfillProgress:
    """把译者「这一块翻到几成」（step_progress）换算成补译的「总共翻了几条」。

    补译频道上只发 translation_progress / translation_complete 两种事件，译者的 log 不转发；
    只把 warning 级的记进 warnings —— 译者把批量调用的异常（API Key 失效、模型名写错）吃掉、
    只留一条 warning，一条都没翻成时要拿它当失败原因告诉用户
    """

    def __init__(self, channel: str, done_before: int, chunk_size: int, total: int, warnings: list):
        self.channel = channel
        self.done_before = done_before
        self.chunk_size = chunk_size
        self.total = total
        self.warnings = warnings

    async def emit(self, _channel: str, event_type: str, data: dict):
        if event_type == "log" and data.get("level") == "warning":
            self.warnings.append(data.get("message") or "")
            return
        if event_type != "step_progress":
            return
        fraction = min(max(float(data.get("progress") or 0), 0.0), 1.0)
        await progress_manager.emit(self.channel, "translation_progress", {
            "done": self.done_before + int(self.chunk_size * fraction), "total": self.total,
        })


class TaskOrchestrator:
    """编排任务执行：解析意图 → 逐步执行 → 汇总结果"""

    def __init__(self):
        self.tasks: Dict[str, dict] = {}
        self._running_tasks: set = set()
        self._sentiment_running: set = set()
        # 来源 → 正在翻译它的占用者（`backfill:<任务>` 补译作业 / `task:<任务>` 任务里的翻译步骤）。
        # 翻译状态按帖子跨任务共享，同一批帖子翻两遍就是付两遍钱。**用普通字典不用 asyncio.Lock**：
        # 本对象是模块级单例，测试每次换一个事件循环，锁一旦在某个循环里争用过就绑死在那个循环上
        self._translation_claims: Dict[str, str] = {}
        # 补译作业一跑十几分钟。asyncio 只弱引用任务，不留强引用的话可能在跑到一半时被回收
        self._backfill_jobs: set = set()
        self._task_queue: asyncio.Queue = asyncio.Queue()  # 任务等待队列
        init_db()
        migrate_from_json()
        self._load_tasks()
        self._migrate_posts()
        self._migrate_sentiment()
        # **必须排在 _migrate_sentiment 之后**：旧 blob 里那批假 neutral 正是这一步
        # 才写进 sentiment_results 的，放在 init_db() 里等于让升级那一次空转
        purge_fake_parse_failures()
        # 同理必须排在迁移之后：旧 JSON 里那批帖子正是这一步才进的 posts 表，
        # 放在前面等于对着一个还没有数据的库做合并
        merge_duplicate_posts()

    def _migrate_posts(self):
        """把各来源的落盘 JSON 搬进 posts 表。

        必须在 _load_tasks 之后（要读历史任务记下的路径）、_migrate_sentiment
        之前（后者要按帖子顺序把旧结论换成身份）。
        """
        # 已注册的来源：文件名从 legacy_output_path() 取，不 glob 猜
        for source in source_service.list_sources():
            try:
                path = get_collector(source["collector_id"]).legacy_output_path(source)
            except (ValueError, KeyError):
                continue
            self._migrate_one_source(source["id"], path)

        # 已被删除、但仍被历史任务引用的来源：任务结果里记着当时的落盘路径。
        # 漏掉这一步，一次「删数据源」就会让所有引用它的历史任务结果变成空白
        for task in self.tasks.values():
            for entry in (task.get("result") or {}).get("sources", []):
                self._migrate_one_source(entry.get("id"), entry.get("output_path"))

    def _migrate_one_source(self, source_id: Optional[str], path: Optional[str]):
        if not source_id or not path:
            return
        try:
            if migrate_posts_file(source_id, path):
                retire_file(path)
        except Exception as e:
            logger.error("迁移来源 %s 的帖子失败: %s", source_id, e)

    def _migrate_sentiment(self):
        """把旧的「整份 JSON 一个列」舆情结果换成按帖子身份存。

        放在 _load_tasks 之后：映射需要每个任务当时那批帖子，而那要先有任务记录。
        """
        pending = legacy_sentiment_task_ids()
        if not pending:
            return
        for task_id in pending:
            task = self.tasks.get(task_id)
            if not task:
                # 任务已被删除，这份结论从任何接口都够不着了。留着它只会挡住
                # 旧表的 DROP，让「整份 JSON 一个列」那套永远清不干净
                logger.warning("舆情 %s 对应的任务已不存在，丢弃这份孤儿结果", task_id)
                discard_legacy_sentiment(task_id)
                continue
            try:
                migrate_sentiment_blob(task_id, load_task_posts(task))
            except Exception as e:
                logger.error("迁移舆情 %s 失败: %s", task_id, e)
        drop_legacy_sentiment_table()

    # ===== 持久化 =====

    def _load_tasks(self):
        """从 SQLite 加载历史任务，并清理异常终止的任务"""
        task_list = load_all_tasks()
        for task_data in task_list:
            if task_data["status"] in ("pending", "parsing", "running"):
                task_data["status"] = TaskStatus.CANCELLED
                task_data["completed_at"] = datetime.now()
                task_data.setdefault("logs", []).append({
                    "time": datetime.now().isoformat(),
                    "level": "warning",
                    "message": "服务重启，未完成的任务已自动取消。",
                })
            self.tasks[task_data["id"]] = task_data
        if any(t["status"] == TaskStatus.CANCELLED for t in task_list):
            self._persist()

    def _save_tasks(self):
        """保存所有任务。

        注意这里**从不发 DELETE**：只对 self.tasks 里剩余的任务逐条 upsert。
        任何「从 self.tasks 移除条目」的逻辑都必须自己显式调 db_delete_task()，
        否则残留行会在下次启动被 _load_tasks() 读回来。
        """
        for t in self.tasks.values():
            save_task(t)

    def _persist(self):
        """保存（同步到磁盘）"""
        try:
            self._save_tasks()
        except Exception as e:
            logger.error("持久化任务列表失败: %s", str(e))

    def create_task(self, task_id: str, description: str, force_full: bool = False):
        """注册新任务。

        force_full=True 时这一轮忽略全部增量标记：重新采集、重新翻译、重新分析舆情，
        配图也重新下载。只有「全量重跑」那个按钮会给 True —— 新建任务和定时任务
        一律走增量，否则每一轮定时都在重复付翻译和舆情的钱。
        """
        self.tasks[task_id] = {
            "id": task_id,
            "status": TaskStatus.PENDING,
            "description": description,
            "force_full": bool(force_full),
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

        # 检查并发限制 — 超出限制时加入队列等待
        if len(self._running_tasks) >= settings.max_concurrent_tasks:
            task["status"] = TaskStatus.PENDING
            task["current_step"] = "排队等待中..."
            self._persist()
            await progress_manager.emit(task_id, "log", {
                "level": "info",
                "message": "已有任务正在执行，当前任务已加入等待队列...",
            })
            await self._task_queue.put(task_id)
            # 等待队列被消费（run_task_async 在 _process_queue 中重新调用 execute_task）
            return

        self._running_tasks.add(task_id)
        task["status"] = TaskStatus.PARSING
        task["started_at"] = datetime.now()
        self._persist()

        # 「全量重跑」：下面 collect / translate / sentiment 三处的增量判据一并放开
        force_full = bool(task.get("force_full"))
        if force_full:
            await self._task_log(
                task_id, "warning",
                "全量重跑：忽略全部增量标记，将重新采集、重新翻译、重新下载配图并重新分析舆情",
            )

        llm_service = None
        try:
            # 加载 LLM 配置（使用统一工具函数）
            from app.services.llm_utils import get_llm_service
            llm_service = get_llm_service()
            if not llm_service:
                raise Exception("请先配置 LLM API")

            # ===== Step 0: 意图解析 =====
            task["current_step"] = "解析任务意图"
            self._persist()
            await progress_manager.emit(task_id, "step_start", {
                "step": -1,
                "action": "parse_intent",
                "message": "正在调用 LLM 解析任务意图...",
            })

            enabled_sources = source_service.list_sources(enabled_only=True)
            for s in enabled_sources:
                s["collector_name"] = get_collector(s["collector_id"]).display_name

            plan_data = await llm_service.parse_intent(task["description"], enabled_sources)

            plan = [PlanStep(**s) for s in plan_data.get("plan", [])]
            plan, source_warnings = _resolve_sources(task, plan, enabled_sources)
            for w in source_warnings:
                await self._task_log(task_id, "warning", w)
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
                    if step.action == "collect":
                        source = source_service.get_source(step.params["source_id"])
                        if not source:
                            raise Exception(f"数据源已被删除: {step.params['source_id']}")
                        collector = get_collector(source["collector_id"])
                        source["params"] = dict(source.get("params") or {})
                        # 全量重跑：连同续抓页码和已知指纹一起作废（见 CollectorRunner），
                        # 否则 Facebook 侧的 seen 集合会把每条帖子都判成「见过」，
                        # 图片也就不会重下
                        source["params"]["incremental"] = not force_full
                        ignored_start = _resolve_start_page(task, source["params"])
                        if ignored_start is not None:
                            await self._task_log(
                                task_id, "warning",
                                f"忽略 LLM 给出的 start_page={ignored_start}，"
                                f"改为从第 {source['params']['start_page']} 页开始",
                            )
                        try:
                            result = await CollectorRunner.execute(
                                task_id, collector, source, progress_manager, idx,
                            )
                        except ManualAuthRequired as e:
                            # 这条异常的消息本身就是给用户看的人话（含「去哪点哪个按钮」），
                            # 不要再包一层技术描述把它埋掉
                            await self._task_log(task_id, "error", str(e))
                            raise
                        # CollectorRunner 已把本轮结果并进 posts 表并回读了全量，
                        # 顺序就是表里的 seq —— 增量抓取时这里拿到的是「历史 + 新增」
                        posts = result.get("posts") or []
                        context.setdefault("posts", []).extend(posts)
                        context.setdefault("sources", {})[source["id"]] = {
                            "name": source["name"],
                            "collector_id": source["collector_id"],
                            "total_pages": result.get("total_pages", 0),
                            "post_count": len(posts),
                        }
                        context["total_pages"] = (
                            context.get("total_pages", 0) + result.get("total_pages", 0)
                        )
                        await self._task_log(
                            task_id, "info",
                            f"来源「{source['name']}」采集到 {len(posts)} 条帖子",
                        )

                    elif step.action == "translate":
                        posts = context.get("posts", [])
                        # 没有 collect 步骤时，从各来源已落盘的 JSON 兜底加载
                        if not posts:
                            posts, loaded_meta = _load_posts_from_sources(enabled_sources)
                            if not posts:
                                raise Exception(
                                    "没有可翻译的帖子数据，请先执行采集步骤"
                                    "（各来源的落盘文件都不存在或为空）"
                                )
                            await self._task_log(
                                task_id, "info",
                                f"从已有文件加载 {len(posts)} 条帖子（{len(loaded_meta)} 个来源）",
                            )
                            context["posts"] = posts
                            context["sources"] = loaded_meta
                            context["total_pages"] = sum(
                                m["total_pages"] for m in loaded_meta.values()
                            )

                        # 同一来源正被结果页补译（或别的任务在翻）时先等它结束：翻译状态按帖子跨任务共享，
                        # 两边各翻一遍就是付两遍钱。翻译期间自己也占着来源，补译那边见了就不另起
                        source_ids = sorted({p.get("source") or "tweakers" for p in posts})
                        owner = f"task:{task_id}"
                        claimed = await self._wait_and_claim_translation(task, source_ids, owner)
                        if not claimed:
                            await self._task_log(task_id, "info", "任务已取消，跳过翻译")
                        else:
                            try:
                                # 占到来源后**无条件**回库刷一次译文：补译刚翻好的帖子在内存里还是空译文，
                                # 不刷新就会再翻一遍（付两遍钱），重译失败还会把好译文冲成失败标记。
                                # 不能只在「等过」时刷 —— 帖子往往是前面的步骤（采集、舆情）读进内存的，
                                # 补译在那期间就翻完、放掉了来源，这一步一次占到、压根没等过
                                posts = _refresh_translations(posts)
                                context["posts"] = posts

                                # 增量：过滤已翻译的帖子。全量重跑时全部重译
                                if force_full:
                                    already, pending = [], list(posts)
                                    await self._task_log(task_id, "info", f"全量重跑: {len(pending)} 条全部重新翻译")
                                else:
                                    # 「还缺译文」只认 needs_translation 一处：失败标记也算，
                                    # 以前它被当成已翻译、任务怎么跑都不重试，与结果页的补译口径两样
                                    already = [p for p in posts if p.get("_processed", {}).get("translated") and not needs_translation(p)]
                                    pending = [p for p in posts if not p.get("_processed", {}).get("translated") or needs_translation(p)]
                                if already:
                                    await self._task_log(task_id, "info", f"增量翻译: {len(already)} 条已翻译跳过, {len(pending)} 条待翻译")
                                if pending:
                                    result = await TranslatorService.execute(
                                        task_id, pending, step.params, progress_manager, idx
                                    )
                                    for p in result.get("posts", []):
                                        p.setdefault("_processed", {})["translated"] = True
                                    context["posts"] = _merge_by_fingerprint(posts, result.get("posts", []))
                                else:
                                    context["posts"] = already
                                    await self._task_log(task_id, "info", "所有帖子已翻译，跳过")
                                self._save_translations(context["posts"])
                            finally:
                                self.release_translation(source_ids, owner)

                    elif step.action == "generate_excel":
                        posts = context.get("posts", [])
                        if not posts:
                            posts, loaded_meta = _load_posts_from_sources(enabled_sources)
                            context["sources"] = loaded_meta
                            context["total_pages"] = sum(
                                m["total_pages"] for m in loaded_meta.values()
                            )
                        if not posts:
                            raise Exception("没有帖子数据可生成 Excel")
                        result = await ExcelService.execute(
                            task_id, posts, step.params, progress_manager, idx,
                            context.get("sources", {}),
                        )
                        context["excel_path"] = result.get("file_path", "")
                        context["excel_name"] = result.get("file_name", "")

                    elif step.action == "sentiment":
                        posts = context.get("posts", [])
                        if not posts:
                            posts, loaded_meta = _load_posts_from_sources(enabled_sources)
                            context["sources"] = loaded_meta
                            context["total_pages"] = sum(
                                m["total_pages"] for m in loaded_meta.values()
                            )
                        if not posts:
                            raise Exception("没有可分析的帖子数据")
                        context["posts"] = posts

                        # 增量粒度是 _processed.sentiment_at，跨任务共享 —— 同一条帖子
                        # 不会因为换个任务重跑就再花一次钱。既没正文又没配图的才是真的
                        # 分析不了，analyze() 内部还会再滤一次，这里只用来说人话
                        from app.services.sentiment_service import is_analyzable
                        if force_full:
                            # image_desc 与 translation / sentiment_at 同属「花钱换来的
                            # 结果」，平时绝不重算。但全量重跑正是为「换了模型、改了口径，
                            # 按当前设置重来一遍」而存在的 —— 不清掉它，
                            # sentiment_service 会跳过全部已有描述的帖子，确认弹窗里
                            # 承诺的「带图的还会再调一次多模态模型」就是假话。
                            # 只清内存这一份：save_image_descs() 不写空串，所以多模态
                            # 没配 / 这一张没描述出来时，库里那份旧描述仍在，下一轮照常读回
                            for p in posts:
                                p["image_desc"] = ""
                        pending = list(posts) if force_full else [
                            p for p in posts if not p.get("_processed", {}).get("sentiment_at")
                        ]
                        with_content = [p for p in pending if is_analyzable(p)]
                        if not with_content:
                            await self._task_log(task_id, "info", "所有可分析的帖子都已完成舆情分析，跳过")
                        else:
                            await self._task_log(
                                task_id, "info",
                                f"全量重跑: {len(with_content)} 条全部重新分析"
                                if force_full else
                                f"增量舆情分析: {len(posts) - len(pending)} 条已跳过, "
                                f"{len(with_content)} 条待分析",
                            )
                            # 这批帖子已知的结论（含别的任务分析出来的）要作为 existing
                            # 一起传下去，否则本轮写回的全量会把它们抹掉
                            existing = (storage.get_sentiment(task_id, posts) or {}).get("results") or []
                            await self.run_sentiment(task_id, posts, pending, existing, idx)

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
            sources_meta = context.get("sources", {})
            task["result"] = {
                "total_posts": len(context.get("posts", [])),
                "total_pages": context.get("total_pages", 0),
                "excel_name": context.get("excel_name", ""),
                "excel_path": context.get("excel_path", ""),
                # 记下 source_id：用户之后在数据源页删掉这个来源，历史任务的结果
                # 也还能按 id 从 posts 表读回来，而不是静默变成「没有数据」
                # （posts 表刻意没挂 sources 外键，就是为了这个）
                "sources": [
                    {
                        "id": sid,
                        "name": m["name"],
                        "collector_id": m["collector_id"],
                        "post_count": m["post_count"],
                    }
                    for sid, m in sources_meta.items()
                ],
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

    def _save_translations(self, posts: list):
        """把翻译结果写回 posts 表。

        按 (source, fingerprint) 定位逐条更新，不碰其它字段 —— 改造前是整锅写回
        落盘文件，多来源之后那样做会污染别的来源。
        """
        storage.save_translations(posts)

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
        asyncio.create_task(self._run_with_queue(task_id))

    async def _run_with_queue(self, task_id: str):
        """运行任务并在完成后处理队列"""
        await self.execute_task(task_id)
        # 任务完成后，检查队列
        await self._process_queue()

    async def _process_queue(self):
        """处理等待队列中的下一个任务"""
        while not self._task_queue.empty():
            if len(self._running_tasks) >= settings.max_concurrent_tasks:
                break
            try:
                next_id = self._task_queue.get_nowait()
                task = self.tasks.get(next_id)
                if task and task["status"] == TaskStatus.PENDING and not task.get("_cancelled"):
                    logger.info("从队列中启动任务 %s", next_id)
                    await progress_manager.emit(next_id, "log", {
                        "level": "info",
                        "message": "等待结束，开始执行...",
                    })
                    asyncio.create_task(self._run_with_queue(next_id))
                else:
                    # 任务已被取消，跳过
                    logger.info("跳过已取消的队列任务 %s", next_id)
            except asyncio.QueueEmpty:
                break

    # ===== 清理 =====

    def delete_task(self, task_id: str) -> bool:
        """删除任务记录"""
        if task_id in self.tasks:
            del self.tasks[task_id]
            # _persist() 只 upsert 剩余任务、从不删行，必须显式 DELETE，
            # 否则删掉的任务会在下次启动被读回来（曾因此「复活」过）
            db_delete_task(task_id)
            self._persist()
            return True
        return False

    # ===== 舆情分析 =====

    def is_sentiment_running(self, task_id: str) -> bool:
        return task_id in self._sentiment_running

    # ===== 翻译占用（补译作业与任务里的翻译步骤互斥） =====

    def translation_owner(self, source_ids: List[str]) -> Optional[str]:
        """这几个来源里任何一个正被翻译占着，就返回占用者；都空闲返回 None"""
        for sid in source_ids:
            owner = self._translation_claims.get(sid)
            if owner:
                return owner
        return None

    def claim_translation(self, source_ids: List[str], owner: str) -> Optional[str]:
        """占住这几个来源去翻译。成功返回 None；有一个被别人占着就一个都不占，返回那个占用者"""
        for sid in source_ids:
            holder = self._translation_claims.get(sid)
            if holder and holder != owner:
                return holder
        for sid in source_ids:
            self._translation_claims[sid] = owner
        return None

    def release_translation(self, source_ids: List[str], owner: str) -> None:
        """只放自己占的：占用者对不上说明来源已经归别人了，不能替人放"""
        for sid in source_ids:
            if self._translation_claims.get(sid) == owner:
                del self._translation_claims[sid]

    async def _wait_and_claim_translation(self, task: dict, source_ids: List[str], owner: str) -> bool:
        """任务里的翻译步骤占来源：被补译或别的任务占着就每秒重试，第一次被挡时记一条日志。

        占到返回 True；等的过程中任务被取消返回 False，调用方跳过这一步。
        翻译本身跑起来也停不下（取消只在步骤之间检查），所以这里只管「别在被取消之后还接着等」
        """
        waited = False
        while True:
            holder = self.claim_translation(source_ids, owner)
            if not holder:
                return True
            if task.get("_cancelled"):
                return False
            if not waited:
                what = "正在补译" if holder.startswith("backfill:") else "正在被另一个任务翻译"
                await self._task_log(task["id"], "info", f"同一来源{what}，等它结束再翻译")
                waited = True
            await asyncio.sleep(1)

    def start_backfill_translation(self, task_id: str, source_ids: List[str],
                                   pending: List[dict]) -> Optional[str]:
        """结果页补译：占住来源、起后台作业，立刻返回 None；来源被占着就返回占用者、不起作业。

        占用必须在这里**同步**做（同 run_sentiment_async）：等协程被调度起来再占的话，
        连点两下会在第一轮占上之前起出第二轮
        """
        owner = f"backfill:{task_id}"
        holder = self.claim_translation(source_ids, owner)
        if holder:
            return holder
        job = asyncio.create_task(self._backfill_translation(task_id, source_ids, pending, owner))
        self._backfill_jobs.add(job)
        job.add_done_callback(self._backfill_jobs.discard)
        return None

    async def _backfill_translation(self, task_id: str, source_ids: List[str],
                                    pending: List[dict], owner: str):
        """分块翻、每块落库。后台协程的异常没人接，一律变成 translation_complete{failed}。

        translated 报的是**真翻成了几条**（按 needs_translation 判），不是处理了几条：译者把失败写成
        标记、不抛，只数处理条数的话一条没翻成也是「完成」。一条都没翻成就报失败并带上原因
        """
        channel = translation_channel(task_id)
        total, done, ok, warnings = len(pending), 0, 0, []
        outcome = {"status": "completed"}
        try:
            await progress_manager.emit(channel, "translation_progress", {"done": 0, "total": total})
            for start in range(0, total, BACKFILL_CHUNK_SIZE):
                chunk = pending[start:start + BACKFILL_CHUNK_SIZE]
                result = await TranslatorService.execute(
                    channel, chunk, {}, _BackfillProgress(channel, done, len(chunk), total, warnings), 0,
                )
                translated = result.get("posts", [])
                for p in translated:
                    p.setdefault("_processed", {})["translated"] = True
                self._save_translations(translated)
                done += len(chunk)
                ok += sum(1 for p in translated if not needs_translation(p))
                await progress_manager.emit(channel, "translation_progress", {"done": done, "total": total})
            if total and not ok:
                reason = warnings[0] if warnings else "模型返回的都是空内容"
                outcome = {"status": "failed",
                           "error": f"一条都没有翻成（{reason}）。到「LLM 配置」页点「测试连接」检查 API Key 和模型名后再试"}
        except Exception as e:
            logger.exception("补译失败 task=%s", task_id)
            outcome = {"status": "failed", "error": str(e)}
        finally:
            # 先放来源再发完成：页面收到完成就去拉 /stats，那时 translating 必须已经是 false
            self.release_translation(source_ids, owner)
        await progress_manager.emit(channel, "translation_complete", {**outcome, "translated": ok, "total": total})

    async def run_sentiment(self, task_id: str, all_posts: list, pending_posts: list,
                            existing_results: list = None, step_index: int = 0):
        """跑一轮舆情分析并等它结束（增量：仅分析 pending_posts，合并已有结果）。

        失败会抛出来 —— 流水线的 sentiment 步骤要靠它决定这一步成没成。
        页面按钮那条路不关心，见 run_sentiment_async。
        """
        from app.services.sentiment_service import SentimentService
        self._sentiment_running.add(task_id)
        try:
            # 构建 source:fingerprint → 绝对索引映射（用于合并）。
            # 键必须带来源，只用 fingerprint 会跨来源碰撞
            fp_to_idx = {}
            for i, p in enumerate(all_posts):
                if p.get("fingerprint"):
                    fp_to_idx[post_key(p)] = i

            # 来源名让 prompt 能标注来源；讨论串映射让每条帖子都带上「主贴 + 全部回复」
            # 的上下文（**必须基于 all_posts 而不是 pending_posts** —— 增量时待分析的
            # 往往只是一条新回复，只拿它自己组串就等于没有上下文）
            source_names = {s["id"]: s["name"] for s in source_service.list_sources()}
            thread_by_key = thread_of(all_posts)

            await SentimentService.analyze(
                task_id, pending_posts, progress_manager, existing_results, fp_to_idx,
                source_names, thread_by_key, all_posts, step_index,
            )
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
            raise
        finally:
            self._sentiment_running.discard(task_id)

    def run_sentiment_async(self, task_id: str, all_posts: list, pending_posts: list, existing_results: list = None):
        """舆情页按钮那条路：立刻返回，分析在后台跑。

        _sentiment_running 必须在这里**同步**置上：等协程被调度起来再置的话，
        POST 刚返回那一瞬间前端来问 GET /sentiment 会得到「没在跑」。
        """
        self._sentiment_running.add(task_id)

        async def _run():
            try:
                await self.run_sentiment(task_id, all_posts, pending_posts, existing_results)
            except Exception:
                pass  # 失败已经通过 sentiment_complete 事件报给前端了

        asyncio.create_task(_run())



def _load_posts_from_sources(sources: List[dict]):
    """从 posts 表加载，返回 (posts, sources_meta)。

    支持「只翻译已有数据」这类不带 collect 步骤的任务。
    """
    posts: List[dict] = []
    meta: Dict[str, dict] = {}
    for source in sources:
        mine = storage.load_posts([source["id"]])
        if not mine:
            continue
        posts.extend(mine)
        meta[source["id"]] = {
            "name": source["name"],
            "collector_id": source["collector_id"],
            "total_pages": max((p.get("page_number") or 1) for p in mine),
            "post_count": len(mine),
        }
    return posts, meta


def load_task_posts(task: dict) -> List[dict]:
    """一个任务的全量扁平帖子，顺序即「舆情下标 / index 语义」依赖的那个顺序。

    任务里记了采过哪些源就只读那几个，没记（比如还没跑完）就退回全部已注册的源。
    **按 source_id 直接查表**，来源有没有从注册表里删掉都读得到 —— 否则一次
    「删数据源」会把所有引用它的历史任务结果一起变成空白。
    """
    recorded = (task.get("result") or {}).get("sources", [])
    if not recorded:
        return storage.load_posts([s["id"] for s in source_service.list_sources()])
    return storage.load_posts([e["id"] for e in recorded if e.get("id")])


def _refresh_translations(posts: list) -> list:
    """把这批帖子的译文与「已翻译」标记换成库里的最新值（按 source:fingerprint 对齐，顺序不动）。

    任务里的翻译步骤占到来源之后用：它手里那份是更早的步骤读进来的，补译刚翻好的帖子在内存里还是空译文
    """
    sources = sorted({p.get("source") or "tweakers" for p in posts})
    fresh = {post_key(p): p for p in storage.load_posts(sources)}
    out = []
    for p in posts:
        latest = fresh.get(post_key(p))
        if latest is not None:
            p = dict(p, translation=latest.get("translation", ""))
            if (latest.get("_processed") or {}).get("translated"):
                p["_processed"] = {**(p.get("_processed") or {}), "translated": True}
        out.append(p)
    return out


def _merge_by_fingerprint(source_posts: list, translated: list) -> list:
    """把翻译结果按 source:fingerprint 合回源列表。

    顺序必须以源 JSON 为准：合并结果会被写回源文件，一旦按「已翻译+待翻译」的
    分区序落盘，原始楼层顺序就再也还原不回来了。
    键带来源是因为 fingerprint 不含来源，跨源合并时会碰撞（空内容帖尤其危险）。
    """
    by_key = {post_key(p): p for p in translated if p.get("fingerprint")}
    return [by_key.get(post_key(p), p) for p in source_posts]


_ALL_SOURCES_PATTERNS = (
    "所有来源", "全部来源", "各来源", "各个来源", "所有数据源", "全部数据源",
    "所有平台", "全部平台", "各平台", "各个平台", "所有渠道", "全部渠道", "各渠道",
)


def _resolve_sources(task: dict, plan: List[PlanStep], sources: List[dict]):
    """确定性地敲定要采哪些来源，返回 (修正后的 plan, 告警列表)。

    照搬 _resolve_start_page 的路子：LLM 负责理解，后端负责保证。来源一多模型很容易
    只挑一个，那是静默漏采 —— 报告看起来完整，实际缺了半个平台的声音，比任务失败糟得多。
    所以用户说「所有来源」时无条件展开为全部已启用来源，不看模型给了什么。
    """
    warnings: List[str] = []
    valid_ids = {s["id"] for s in sources}
    collect_idx = [i for i, s in enumerate(plan) if s.action == "collect"]
    if not collect_idx:
        return plan, warnings

    desc = task.get("description", "")
    wants_all = any(p in desc for p in _ALL_SOURCES_PATTERNS)

    picked = []
    for i in collect_idx:
        sid = plan[i].params.get("source_id")
        if sid in valid_ids:
            if sid not in picked:
                picked.append(sid)
        elif plan[i].params.get("override"):
            # 用户临时贴了个新链接。不能拿已注册的来源顶替 —— 那是答非所问，
            # 而报告里看不出来。宁可失败并告诉他去哪补。
            raise Exception(
                f"「{plan[i].params['override']}」还不是已注册的数据源。"
                "请先到「数据源」页把它加进来，再重新提交任务。"
            )
        elif sid is not None:
            warnings.append(f"忽略 LLM 编造的数据源 source_id={sid}（不在已启用清单里）")

    if wants_all:
        missing = [s["id"] for s in sources if s["id"] not in picked]
        if missing:
            names = "、".join(s["name"] for s in sources if s["id"] in missing)
            warnings.append(f"用户要求全部来源，补上被 LLM 漏掉的：{names}")
        picked = [s["id"] for s in sources]
    elif not picked:
        # 模型一个有效来源都没给出，又不是「所有来源」—— 全采比静默不采安全
        picked = [s["id"] for s in sources]
        if picked:
            warnings.append("LLM 未指定有效数据源，按全部已启用来源执行")

    if not picked:
        raise Exception("没有已启用的数据源，请先到「数据源」页注册至少一个来源")

    name_of = {s["id"]: s["name"] for s in sources}
    rebuilt = [PlanStep(action="collect", params={"source_id": sid, "source_name": name_of[sid]})
               for sid in picked]
    rebuilt += [s for s in plan if s.action != "collect"]
    return rebuilt, warnings


def _resolve_start_page(task: dict, params: dict) -> Optional[int]:
    """确定性派生抓取起始页，写回 params，并返回被忽略的 LLM 取值（无则 None）。

    LLM 常把帖子 URL 末尾的页码误当成起始页，而抓取循环只前进不回补，前面几页会被
    永久跳过。所以起始页只认用户描述里的显式指令，其余情况一律从第 1 页开始。
    """
    import re

    ignored = params.get("start_page")
    resolved = 1
    desc = task.get("description", "")
    for pattern in (r"从第\s*(\d+)\s*页\s*(?:开始|起)", r"start_page\s*[:=]\s*(\d+)"):
        m = re.search(pattern, desc)
        if m:
            resolved = max(int(m.group(1)), 1)
            break

    params["start_page"] = resolved
    return ignored if ignored is not None and ignored != resolved else None


# 全局单例。**必须放在文件末尾**：__init__ 里的舆情迁移要调用下面那些模块级函数，
# 在它们定义之前实例化会得到一串 NameError（实测就是这样，靠 except 兜住才没炸）
orchestrator = TaskOrchestrator()
