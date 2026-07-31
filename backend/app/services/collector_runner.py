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
from app.config import settings
from app.services.progress_manager import ProgressManager

logger = logging.getLogger("hyxi.collector")

# 子进程超时（秒）
SUBPROCESS_TIMEOUT = getattr(settings, "task_timeout_minutes", 30) * 60

# StreamReader 默认上限 64KiB，超长行会抛 LimitOverrunError 打断正常抓取
STREAM_LIMIT = 4 * 1024 * 1024


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
        output_path = collector.output_path(source)
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

        jobs_dir = os.path.join(settings.data_dir, "jobs")
        os.makedirs(jobs_dir, exist_ok=True)
        job_file = os.path.join(jobs_dir, f"{task_id}_{uuid.uuid4().hex[:8]}.json")
        with open(job_file, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)

        try:
            return await CollectorRunner._run(
                task_id, collector, source, job_file, output_path, progress, step_index
            )
        finally:
            # job 文件将来会携带凭据引用，不留在磁盘上
            try:
                os.remove(job_file)
            except OSError:
                pass

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

        proc = await asyncio.create_subprocess_exec(
            "node", collector.script_path(), f"--job={job_file}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=settings.project_root,
            limit=STREAM_LIMIT,
        )

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

            if proc.returncode != 0:
                # 脚本用 stderr 说明中断原因（限流 / 重定向 / 页面异常），不带上就只剩一个退出码
                lines = stderr_text.strip().splitlines()
                detail = f": {lines[-1][:200]}" if lines else ""
                raise Exception(f"采集脚本异常退出 (code={proc.returncode}){detail}")

            if not os.path.exists(output_path):
                raise Exception(f"输出文件未生成: {output_path}")

            with open(output_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            await progress.emit(task_id, "step_progress", {
                "step": step_index,
                "progress": 1.0,
                "message": f"采集完成: {data.get('total_posts', 0)} 条帖子",
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
