"""Playwright 抓取服务 - 调用 Node.js 脚本"""

import os
import re
import json
import asyncio
from app.config import settings
from app.services.progress_manager import ProgressManager


class ScraperService:
    """调用 tweakers_scraper_playwright.js 抓取帖子"""

    @staticmethod
    async def execute(
        task_id: str,
        params: dict,
        progress: ProgressManager,
    ) -> dict:
        """执行抓取"""
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
        )

        # 并发读取 stdout 和 stderr，避免管道死锁
        async def read_stderr():
            stderr_lines = []
            async for line in proc.stderr:
                stderr_lines.append(line.decode("utf-8", errors="replace"))
            return "".join(stderr_lines)

        stderr_task = asyncio.ensure_future(read_stderr())

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
