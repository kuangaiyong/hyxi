"""本地 OpenAI 兼容 API，用来验证「批量解析失败 → 单条重试」和流水线里的舆情步骤。

真 HTTP、真 httpx 客户端、真解析逻辑 —— 换掉的只是模型本身，因为真模型没法
稳定复现「这一批里第三条吐了非 JSON」。它按请求体自己判断走哪条分支：
  意图解析请求（system prompt 里是那个调度器）→ 回一份**不含 sentiment 的**计划
  批量舆情请求（user_message 里有多条「帖子N」）→ 故意少给一段分隔符
  单条重试请求（只有一条）                      → 返回合法 JSON

意图解析那条永远不给 sentiment 步骤 —— 真模型也给不出来（parse_intent 的
prompt 里压根没这个动作），补步骤是 _resolve_sentiment 的活。
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD = ('{"sentiment": "negative", "intensity": 4, '
        '"reason_cn": "单条重试成功", "dimensions": ["固件更新"]}')
SEPARATOR = "---SENTIMENT_SEPARATOR---"
PLAN = '{"plan": [{"action": "generate_excel", "params": {"include_stats": true}}]}'


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        user_msg = system_msg = ""
        for m in body.get("messages", []):
            if m.get("role") == "user":
                user_msg = m.get("content") or ""
            elif m.get("role") == "system":
                system_msg = m.get("content") or ""
        n_posts = len(re.findall(r"^帖子\d+ \[来源:", user_msg, re.M))
        self.server.seen.append("plan" if "智能调度器" in system_msg else n_posts)

        if "智能调度器" in system_msg:
            content = PLAN
        elif n_posts > 1:
            # 批量：前 n-1 条正常，最后一条给一段解析不了的文本。
            # 分隔符数量仍然对得上，所以走的是 _parse_sentiment 失败那条路，
            # 而不是「parts 不够」那条。
            parts = [GOOD] * (n_posts - 1) + ["抱歉，我无法分析这条内容。"]
            content = f"\n{SEPARATOR}\n".join(parts)
        else:
            content = GOOD

        payload = json.dumps({
            "choices": [{"message": {"content": content}}],
            "usage": {"total_tokens": 1},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # LLMService.test_connection() 先探这个
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class LLMSite:
    """with LLMSite() as base_url: ...

    seen 记下每次请求里有几条帖子，用来断言重试确实是「一条一条」发出去的。
    """

    def __init__(self, port: int = 0):
        self._port = port
        self._server = None
        self._thread = None
        self.seen = []

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._server.seen = self.seen
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:{}".format(self._server.server_address[1])

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False
