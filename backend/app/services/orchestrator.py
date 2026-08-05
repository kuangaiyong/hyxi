"""任务编排引擎 - LLM 意图解析 + 逐步执行（含持久化）"""

import json
import os
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
from app.services.post_tree import build_tree, post_key
from app.services.llm_service import LLMService
from app.services.collector_runner import CollectorRunner, ManualAuthRequired
from app.services.translator_service import TranslatorService
from app.services.excel_service import ExcelService
from app.services.progress_manager import progress_manager
from app.services.storage import (
    init_db, migrate_from_json, save_task, load_all_tasks,
    delete_task as db_delete_task,
    legacy_sentiment_task_ids, migrate_sentiment_blob, drop_legacy_sentiment_table,
    discard_legacy_sentiment,
)

logger = get_logger(__name__)


class TaskOrchestrator:
    """编排任务执行：解析意图 → 逐步执行 → 汇总结果"""

    def __init__(self):
        self.tasks: Dict[str, dict] = {}
        self._running_tasks: set = set()
        self._sentiment_running: set = set()
        self._task_queue: asyncio.Queue = asyncio.Queue()  # 任务等待队列
        init_db()
        migrate_from_json()
        self._load_tasks()
        self._migrate_sentiment()

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
                        source["params"].setdefault("incremental", True)
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
                        posts = collector.normalize(result)
                        for p in posts:
                            p["source"] = source["id"]
                        context.setdefault("posts", []).extend(posts)
                        context.setdefault("sources", {})[source["id"]] = {
                            "name": source["name"],
                            "collector_id": source["collector_id"],
                            "output_path": collector.output_path(source),
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

                        # 增量：过滤已翻译的帖子
                        already = [p for p in posts if p.get("_processed", {}).get("translated") and p.get("translation")]
                        pending = [p for p in posts if not p.get("_processed", {}).get("translated") or not p.get("translation")]
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
                        self._save_translated_json(context.get("sources", {}), context["posts"])

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
                # 记下 output_path：用户之后在数据源页删掉这个来源，历史任务的结果
                # 也还能照原路读回来，而不是静默变成「没有数据」
                "sources": [
                    {
                        "id": sid,
                        "name": m["name"],
                        "collector_id": m["collector_id"],
                        "output_path": m["output_path"],
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

    def _save_translated_json(self, sources_meta: dict, posts: list):
        """把翻译结果按来源拆回各自的落盘文件。

        多来源之后 posts 是跨源拼起来的，整锅写回任何一个文件都会污染别的来源。
        """
        for source_id, meta in sources_meta.items():
            json_path = meta.get("output_path")
            if not json_path or not os.path.exists(json_path):
                continue
            mine = [p for p in posts if p.get("source", source_id) == source_id]
            if not mine:
                continue
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["posts"] = mine
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

    def run_sentiment_async(self, task_id: str, all_posts: list, pending_posts: list, existing_results: list = None):
        """后台启动舆情分析（增量：仅分析 pending_posts，合并已有结果）"""
        from app.services.sentiment_service import SentimentService
        self._sentiment_running.add(task_id)

        async def _run():
            try:
                # 构建 source:fingerprint → 绝对索引映射（用于合并）。
                # 键必须带来源，只用 fingerprint 会跨来源碰撞
                fp_to_idx = {}
                for i, p in enumerate(all_posts):
                    if p.get("fingerprint"):
                        fp_to_idx[post_key(p)] = i

                # 来源名与父贴映射：让 prompt 能标注来源、给评论带上父贴上下文
                source_names = {s["id"]: s["name"] for s in source_service.list_sources()}
                by_key = {post_key(p): p for p in all_posts}
                _roots, children = build_tree(all_posts)
                parent_by_key = {}
                for parent_k, kids in children.items():
                    for child in kids:
                        parent_by_key[post_key(child)] = by_key[parent_k]

                await SentimentService.analyze(
                    task_id, pending_posts, progress_manager, existing_results, fp_to_idx,
                    source_names, parent_by_key, all_posts,
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
            finally:
                self._sentiment_running.discard(task_id)

        asyncio.create_task(_run())



def _load_posts_from_sources(sources: List[dict]):
    """从各来源已落盘的 JSON 兜底加载，返回 (posts, sources_meta)。

    支持「只翻译已有数据」这类不带 collect 步骤的任务。每条帖子补上 source 标记，
    否则跨源合并后分不清谁是谁。
    """
    posts: List[dict] = []
    meta: Dict[str, dict] = {}
    for source in sources:
        collector = get_collector(source["collector_id"])
        try:
            path = collector.output_path(source)
        except ValueError:
            continue
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        mine = loaded.get("posts", [])
        for p in mine:
            p.setdefault("source", source["id"])
        posts.extend(mine)
        meta[source["id"]] = {
            "name": source["name"],
            "collector_id": source["collector_id"],
            "output_path": path,
            "total_pages": loaded.get("total_pages", 0),
            "post_count": len(mine),
        }
    return posts, meta


def load_task_posts(task: dict) -> List[dict]:
    """一个任务的全量扁平帖子，顺序即「舆情下标 / index 语义」依赖的那个顺序。

    任务里记了采过哪些源就只读那几个，没记（比如还没跑完）就退回全部已注册的源。
    """
    recorded = (task.get("result") or {}).get("sources", [])
    if not recorded:
        posts, _meta = _load_posts_from_sources(source_service.list_sources())
        return posts

    registered = {s["id"]: s for s in source_service.list_sources()}
    posts: List[dict] = []
    for entry in recorded:
        sid = entry.get("id")
        source = registered.get(sid)
        if source:
            mine, _meta = _load_posts_from_sources([source])
            posts.extend(mine)
            continue
        # 来源已被用户删除。任务结果里记了当时的落盘路径，照原路读回来 ——
        # 否则一次「删数据源」会把所有引用它的历史任务结果一起变成空白
        path = entry.get("output_path")
        if not path or not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        for p in loaded.get("posts", []):
            p.setdefault("source", sid)
        posts.extend(loaded.get("posts", []))
    return posts


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
