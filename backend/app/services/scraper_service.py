"""Playwright 抓取服务 - 调用 Node.js 脚本（含超时与取消清理）"""

import os
import re
import json
import asyncio
import logging
from app.config import settings
from app.services.progress_manager import ProgressManager

logger = logging.getLogger("hyxi.scraper")

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


class ScraperService:
    """调用 tweakers_scraper_playwright.js 抓取帖子"""

    @staticmethod
    async def execute(
        task_id: str,
        params: dict,
        progress: ProgressManager,
    ) -> dict:
        """执行抓取（含超时控制，取消时自动清理子进程）"""
        thread_id = params.get("thread_id")
        start_page = params.get("start_page", 1)
        headless = params.get("headless", True)
        incremental = params.get("incremental", True)

        if not thread_id:
            raise ValueError("缺少 thread_id 参数")

        script_path = os.path.join(settings.project_root, "tweakers_scraper_playwright.js")

        cmd = [
            "node", script_path,
            f"--thread={thread_id}",
            f"--start={start_page}",
        ]
        if headless:
            cmd.append("--headless")
        if incremental:
            cmd.append("--incremental")

        await progress.emit(task_id, "step_progress", {
            "step": 0,
            "progress": 0.05,
            "message": f"正在启动浏览器抓取帖子 {thread_id}...",
        })
        await progress.emit(task_id, "log", {
            "level": "info",
            "message": f"执行命令: {' '.join(cmd)}",
        })

        proc = await asyncio.create_subprocess_exec(
            *cmd,
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

                await progress.emit(task_id, "log", {
                    "level": "info",
                    "message": line_str,
                })

                page_match = re.search(r"第\s*(\d+)/(\d+)\s*页", line_str)
                if page_match:
                    current = int(page_match.group(1))
                    total = int(page_match.group(2))
                    pct = 0.05 + (current / total) * 0.75
                    await progress.emit(task_id, "step_progress", {
                        "step": 0,
                        "progress": min(pct, 0.8),
                        "message": f"正在抓取第 {current}/{total} 页...",
                    })

                if "提取完成" in line_str or "总帖子" in line_str:
                    await progress.emit(task_id, "step_progress", {
                        "step": 0,
                        "progress": 0.85,
                        "message": "抓取完成，正在保存数据...",
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
                raise Exception(f"抓取任务超时（超过 {settings.task_timeout_minutes} 分钟），已终止子进程")

            stderr_text = await stderr_task
            if stderr_text.strip():
                await progress.emit(task_id, "log", {
                    "level": "warning",
                    "message": f"stderr: {stderr_text[:500]}",
                })

            if proc.returncode != 0:
                raise Exception(f"抓取脚本异常退出 (code={proc.returncode})")

            # 读取输出 JSON
            output_file = os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")
            if not os.path.exists(output_file):
                raise Exception(f"输出文件未生成: {output_file}")

            with open(output_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            await progress.emit(task_id, "step_progress", {
                "step": 0,
                "progress": 1.0,
                "message": f"抓取完成: {data.get('total_posts', 0)} 条帖子",
            })

            return data

        except asyncio.CancelledError:
            # 任务被取消时清理子进程
            logger.info("抓取任务被取消，正在终止子进程... task=%s", task_id)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            await _kill_process_tree(proc)
            raise

        except Exception:
            # 其他异常时也确保清理子进程
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            await _kill_process_tree(proc)
            raise
