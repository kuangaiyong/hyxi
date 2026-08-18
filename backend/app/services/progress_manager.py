"""进度管理器 - SSE 事件广播"""

import json
import asyncio
from typing import Dict, List
from collections import defaultdict


# 单个订阅者最多积压的事件数
QUEUE_MAXSIZE = 1000


class ProgressManager:
    """管理任务进度事件的发布/订阅"""

    def __init__(self):
        self.subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        """订阅任务事件，返回一个异步队列"""
        queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.subscribers[task_id].append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: asyncio.Queue):
        """取消订阅"""
        if task_id in self.subscribers:
            try:
                self.subscribers[task_id].remove(queue)
            except ValueError:
                pass
            # 不删空 key 的话 subscribers 会随任务数单调增长，永不回收
            if not self.subscribers[task_id]:
                del self.subscribers[task_id]

    async def emit(self, task_id: str, event_type: str, data: dict):
        """向所有订阅者广播事件"""
        message = {"event": event_type, "data": data}
        dead_queues = []
        for queue in self.subscribers.get(task_id, []):
            try:
                # 慢消费者不能拖住抓取/翻译主流程，队列满了就丢最旧的事件
                while True:
                    try:
                        queue.put_nowait(message)
                        break
                    except asyncio.QueueFull:
                        queue.get_nowait()
            except Exception:
                dead_queues.append(queue)

        for q in dead_queues:
            self.unsubscribe(task_id, q)

    async def event_generator(self, task_id: str, terminal_event: str):
        """SSE 事件生成器 (async generator)。收到 `terminal_event` 后这条流自行结束。

        **结束条件必须由端点各自给**：任务进度流和舆情流跑在同一个频道上，但一条等的是
        `task_complete`、另一条等的是 `sentiment_complete`。共用一份「终止事件表」会让
        流水线里的 sentiment 步骤一发完 `sentiment_complete` 就把任务进度流掐断 ——
        紧随其后的 `task_complete` 没人收得到，而前端只有在收到它时才会刷新任务状态，
        于是进度页永远停在 running，既不跳转也不出现「查看结果」（用户实测报过）。
        """
        queue = self.subscribe(task_id)
        try:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    event_type = message["event"]
                    data_str = json.dumps(message["data"], ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data_str}\n\n"

                    if event_type == terminal_event:
                        break
                except asyncio.TimeoutError:
                    # 发送心跳（SSE 注释，防止代理断开连接）
                    yield f": keepalive\n\n"

        finally:
            self.unsubscribe(task_id, queue)


# 全局单例
progress_manager = ProgressManager()
