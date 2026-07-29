"""进度管理器 - SSE 事件广播"""

import json
import asyncio
from typing import Dict, List
from collections import defaultdict


class ProgressManager:
    """管理任务进度事件的发布/订阅"""

    def __init__(self):
        self.subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """订阅任务事件，返回一个异步队列"""
        queue = asyncio.Queue()
        self.subscribers[task_id].append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue):
        """取消订阅"""
        if task_id in self.subscribers:
            try:
                self.subscribers[task_id].remove(queue)
            except ValueError:
                pass

    async def emit(self, task_id: str, event_type: str, data: dict):
        """向所有订阅者广播事件"""
        message = {"event": event_type, "data": data}
        dead_queues = []
        for queue in self.subscribers.get(task_id, []):
            try:
                await queue.put(message)
            except Exception:
                dead_queues.append(queue)

        for q in dead_queues:
            self.unsubscribe(task_id, q)

    async def event_generator(self, task_id: str):
        """SSE 事件生成器 (async generator)"""
        queue = self.subscribe(task_id)
        try:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_type = message["event"]
                    data_str = json.dumps(message["data"], ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data_str}\n\n"

                    if event_type == "task_complete":
                        break
                except asyncio.TimeoutError:
                    # 发送心跳（SSE 注释，防止代理断开连接）
                    yield f": keepalive\n\n"

        finally:
            self.unsubscribe(task_id, queue)


# 全局单例
progress_manager = ProgressManager()
