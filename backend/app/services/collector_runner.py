"""采集器 runner —— 用 job.json + stdout NDJSON 协议驱动 Node 采集脚本。

这里没有任何站点分支：入参由 Collector.build_job() 生成，进度由脚本自报，输出位置由
job 指定。超时控制、进程树清理、退出码契约沿用已验证的实现，不要改。
"""

import os
import json
import uuid
import asyncio
import logging
from typing import Any, Dict

from app.collectors.base import Collector
from app.config import resolve_node_executable, settings
from app.services import storage
from app.services.progress_manager import ProgressManager

logger = logging.getLogger("hyxi.collector")

# 子进程超时（秒）
SUBPROCESS_TIMEOUT = getattr(settings, "task_timeout_minutes", 30) * 60

# StreamReader 默认上限 64KiB，超长行会抛 LimitOverrunError 打断正常抓取
STREAM_LIMIT = 4 * 1024 * 1024

# 退出码契约：0 完整 / 1 硬失败 / 2 部分完成 / 3 需要人工授权
EXIT_NEEDS_MANUAL_AUTH = 3


class ManualAuthRequired(Exception):
    """脚本报告需要人工完成登录（两步验证、安全检查、密码失效）。

    单独一个异常类型，是为了让 orchestrator 能给出「去数据源页点人工登录」这句人话，
    而不是把用户丢给一个退出码。
    """

    def __init__(self, source_id: str, source_name: str, reason: str):
        self.source_id = source_id
        self.source_name = source_name
        self.reason = reason
        super().__init__(
            f"数据源「{source_name}」需要人工重新授权：{reason}。"
            "请到「数据源」页点「人工登录」完成验证后重试。"
        )


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """终止子进程及其后代。

    Windows 上 terminate() 只杀 node 本身，Playwright 拉起的 Chromium 不在同一 job object
    内，会变成孤儿进程一直驻留；必须按进程树清理。
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except Exception:
        pass


class CollectorRunner:
    """执行一个采集器脚本并返回它写出的原始 JSON"""

    @staticmethod
    async def execute(
        task_id: str,
        collector: Collector,
        source: Dict[str, Any],
        progress: ProgressManager,
        step_index: int = 0,
    ) -> dict:
        jobs_dir = os.path.join(settings.data_dir, "jobs")
        os.makedirs(jobs_dir, exist_ok=True)
        run_id = f"{task_id}_{uuid.uuid4().hex[:8]}"
        # 脚本的产出是一个**用完即删的交接文件**，不再是长期落盘的数据文件：
        # 帖子的家在 posts 表里。JS 侧的契约没变，还是「写 job.output_path」
        output_path = os.path.join(jobs_dir, f"{run_id}_out.json")

        source_id = source.get("id") or collector.id
        if source.get("mode") != "login_only":
            # 增量所需的信息由 Python 从库里算好下发。脚本以前是自己读旧落盘文件的，
            # 那份文件现在不存在了。
            #
            # **关掉增量时必须一起清空**：facebook_group.js 的 `seen` 集合是无条件
            # 用 known_fingerprints 建的（只有水位线提前退出那句看 incremental），
            # 照旧下发的话每条帖子都会被判成「见过」→ 不进 fresh → 图片也不会重下，
            # 于是 incremental=False 对它完全无效。tweakers 那边的续抓页码同理。
            source = dict(source)
            incremental = (source.get("params") or {}).get("incremental", True)
            source["known_fingerprints"] = (
                storage.known_fingerprints(source_id) if incremental else []
            )
            source["max_page_number"] = (
                storage.max_page_number(source_id) if incremental else 0
            )

        job = collector.build_job(source, output_path)

        script_path = collector.script_path()
        if not os.path.exists(script_path):
            raise ValueError(f"采集脚本不存在: {script_path}")

        # 依赖缺失在 Python 侧就拦下：这条异常会进 task["error_message"] 并在前端展示，
        # 用户看到的是「怎么修」而不是一段 Node 的 MODULE_NOT_FOUND 堆栈
        if not os.path.exists(
            os.path.join(settings.project_root, "node_modules", "playwright", "package.json")
        ):
            raise ValueError("缺少 Node 依赖 playwright。请在项目根目录执行 npm ci 后重试。")

        job_file = os.path.join(jobs_dir, f"{run_id}.json")
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)

        try:
            return await CollectorRunner._run(
                task_id, collector, source, job_file, output_path, progress, step_index
            )
        finally:
            # 两个都是进程间通信的临时文件，用完即删：job 携带凭据引用，
            # 交接文件里是已经入库的帖子
            for path in (job_file, output_path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    @staticmethod
    def _child_env(collector: Collector, source: Dict[str, Any]) -> Dict[str, str]:
        """凭据只走子进程环境变量。

        **绝不能进 argv**（会出现在进程列表和任何回显命令行的日志里），
        **也不能进 job 文件**（要落磁盘）。环境变量随进程结束一起消失。
        """
        env = dict(os.environ)
        if not collector.needs_credentials:
            return env
        source_id = source.get("id")
        if not source_id:
            return env

        from app.services import source_service

        # 取不到凭据不在这里抛：脚本会走「会话失效且没凭据」那条路，
        # 以退出码 3 给出「去点人工登录」的人话。在这里抛只会变成一个看不懂的
        # OperationalError/InvalidToken，把真正可操作的提示盖掉
        try:
            info = source_service.credential_info(source_id)
            if not info["has_credential"]:
                return env
            secret = source_service.get_credential_secret(source_id)
        except Exception as e:
            logger.warning("读取数据源 %s 的凭据失败，将按无凭据处理: %s", source_id, e)
            return env
        if secret is None:
            return env
        env["HYXI_CRED_USERNAME"] = info["credential_username"]
        env["HYXI_CRED_PASSWORD"] = secret
        return env

    @staticmethod
    async def _run(
        task_id: str,
        collector: Collector,
        source: Dict[str, Any],
        job_file: str,
        output_path: str,
        progress: ProgressManager,
        step_index: int,
    ) -> dict:
        source_id = source.get("id") or collector.id

        await progress.emit(task_id, "step_progress", {
            "step": step_index,
            "progress": 0.05,
            "message": f"正在启动 {collector.display_name} 采集...",
        })
        # 只记采集器和来源，不回显命令行：job 里将来带凭据引用，命令行会进任务日志和 SSE
        await progress.emit(task_id, "log", {
            "level": "info",
            "message": f"启动采集器 {collector.id}（来源 {source_id}）",
        })

        node_exe = resolve_node_executable()
        logger.info("采集子进程使用 node: %s", node_exe)
        proc = await asyncio.create_subprocess_exec(
            node_exe, collector.script_path(), f"--job={job_file}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=settings.project_root,
            limit=STREAM_LIMIT,
            env=CollectorRunner._child_env(collector, source),
        )

        auth_reason = [""]

        # 并发读取 stdout 和 stderr，避免管道死锁
        async def read_stderr():
            stderr_lines = []
            try:
                async for line in proc.stderr:
                    stderr_lines.append(line.decode("utf-8", errors="replace"))
            except Exception:
                pass
            return "".join(stderr_lines)

        async def pump_stdout_and_wait():
            async for line in proc.stdout:
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                event = None
                if line_str.startswith("{"):
                    try:
                        event = json.loads(line_str)
                    except ValueError:
                        event = None

                if isinstance(event, dict) and event.get("evt") == "need_manual_auth":
                    # 记下原因，退出码 3 落地时拼进给用户的那句人话
                    auth_reason[0] = event.get("reason") or "需要人工完成登录验证"
                    await progress.emit(task_id, "log", {
                        "level": "warning",
                        "message": f"需要人工授权: {auth_reason[0]}",
                    })
                    continue

                if isinstance(event, dict) and event.get("evt") == "progress":
                    total = event.get("total") or 0
                    current = event.get("current") or 0
                    pct = 0.05 + (current / total) * 0.75 if total > 0 else 0.05
                    await progress.emit(task_id, "step_progress", {
                        "step": step_index,
                        "progress": min(pct, 0.8),
                        "message": event.get("msg") or f"已采集 {current}/{total}",
                    })
                    continue

                # 解析不出结构化事件的行原样当日志转发
                await progress.emit(task_id, "log", {
                    "level": "info",
                    "message": line_str,
                })

            await proc.wait()

        stderr_task = asyncio.create_task(read_stderr())

        try:
            # 超时必须罩住「读流 + 等待」整体：目标站 stall 住连接时读流循环永远不结束，
            # 只给 proc.wait() 设超时的话根本执行不到那一行，协程会永久占住并发槽
            try:
                await asyncio.wait_for(pump_stdout_and_wait(), timeout=SUBPROCESS_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error("子进程超时 (%ds)，正在终止... task=%s", SUBPROCESS_TIMEOUT, task_id)
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                await _kill_process_tree(proc)
                raise Exception(f"采集任务超时（超过 {settings.task_timeout_minutes} 分钟），已终止子进程")

            stderr_text = await stderr_task
            if stderr_text.strip():
                await progress.emit(task_id, "log", {
                    "level": "warning",
                    "message": f"stderr: {stderr_text[:500]}",
                })

            if proc.returncode == EXIT_NEEDS_MANUAL_AUTH:
                lines = stderr_text.strip().splitlines()
                reason = auth_reason[0] or (lines[-1][:200] if lines else "需要人工完成登录验证")
                # 退出码 3 就是脚本对「这个会话不管用了」的权威判定，拿它把授权时间抹掉，
                # 界面上的「会话正常」才不会在采集正因会话失效而失败时继续显示。
                # 清在这里而不是各个调用方：采集和人工授权超时两条路都要翻徽标。
                # 抹不掉也不能盖住下面这句可操作的提示——那是用户唯一能照着做的东西
                try:
                    from app.services import source_service

                    source_service.clear_authorization(source_id)
                except Exception as e:
                    logger.warning("清除数据源 %s 的授权状态失败: %s", source_id, e)
                raise ManualAuthRequired(
                    source_id, source.get("name") or source_id, reason
                )

            # **入库必须在退出码判断之前**。退出码 2 是「部分完成，数据已落盘」——
            # 脚本在退出前已经把本轮抓到的写进交接文件了。先判退出码就 raise 的话，
            # 这批数据会连同交接文件一起被 finally 删掉，/retry 只能从第 1 页重抓一遍
            # （160 页的长帖抓到第 100 页限流，那 100 页就白抓了）
            data = None
            if source.get("mode") != "login_only" and os.path.exists(output_path):
                with open(output_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # **已存在的帖子只更新采集字段**，绝不覆盖 translation 和 _processed
                # 标记 —— 整体覆盖等于把已翻译的帖子重新变成新帖，下一轮再付一次翻译钱
                posts = collector.normalize(data)
                for p in posts:
                    p["source"] = source_id
                added = storage.upsert_posts(source_id, posts)
                data["posts"] = storage.load_posts([source_id])
                data["total_posts"] = len(data["posts"])

            if proc.returncode != 0:
                # 脚本用 stderr 说明中断原因（限流 / 重定向 / 页面异常），不带上就只剩一个退出码
                lines = stderr_text.strip().splitlines()
                detail = f": {lines[-1][:200]}" if lines else ""
                raise Exception(f"采集脚本异常退出 (code={proc.returncode}){detail}")

            # 人工登录模式只落会话文件，不产出帖子数据
            if source.get("mode") == "login_only":
                await progress.emit(task_id, "step_progress", {
                    "step": step_index, "progress": 1.0, "message": "授权完成，会话已保存",
                })
                return {"mode": "login_only", "authorized": True}

            if data is None:
                raise Exception(f"输出文件未生成: {output_path}")

            await progress.emit(task_id, "step_progress", {
                "step": step_index,
                "progress": 1.0,
                "message": f"采集完成: 本轮新增 {added} 条，共 {data['total_posts']} 条",
            })

            return data

        except asyncio.CancelledError:
            logger.info("采集任务被取消，正在终止子进程... task=%s", task_id)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            await _kill_process_tree(proc)
            raise

        except Exception:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            await _kill_process_tree(proc)
            raise
