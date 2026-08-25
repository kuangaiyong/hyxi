"""本地 OpenAI 兼容 API，用来验证「批量解析失败 → 单条重试」和流水线里的舆情步骤。

真 HTTP、真 httpx 客户端、真解析逻辑 —— 换掉的只是模型本身，因为真模型没法
稳定复现「这一批里第三条吐了非 JSON」。它按请求体自己判断走哪条分支：
  意图解析请求（system prompt 里是那个调度器）→ 按描述里要没要舆情回不同的计划
  批量舆情请求（user_message 里有多条「帖子N」）→ 故意少给一段分隔符
  单条重试请求（只有一条）                      → 返回合法 JSON

意图识别本身由真模型负责（prompt 里已有 sentiment 动作），这里只是个可控的替身：
测的是「计划里有 sentiment 就执行、没有就不执行」，不是模型理解得准不准。
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD = ('{"sentiment": "negative", "intensity": 4, '
        '"reason_cn": "单条重试成功", "dimensions": ["固件更新"]}')
SEPARATOR = "---SENTIMENT_SEPARATOR---"
PLAN = '{"plan": [{"action": "generate_excel", "params": {"include_stats": true}}]}'
PLAN_WITH_TRANSLATE = ('{"plan": [{"action": "translate", "params": {}}, '
                       '{"action": "generate_excel", "params": {"include_stats": true}}, '
                       '{"action": "sentiment", "params": {}}]}')
PLAN_WITH_SENTIMENT = ('{"plan": [{"action": "generate_excel", "params": {"include_stats": true}}, '
                       '{"action": "sentiment", "params": {}}]}')


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
        self.server.prompts.append(system_msg)
        self.server.user_prompts.append(user_msg)

        if "智能调度器" in system_msg:
            # 替身模型：描述里要了舆情就给带 sentiment 的计划。真模型靠 prompt
            # 里那几条规则自己判断，这里只需要可控
            # 触发词要窄：既有用例的描述里就有「翻译已有数据」，用「翻译」当判据
            # 会把它们的计划一起改掉
            if "重新翻译" in user_msg:
                content = PLAN_WITH_TRANSLATE
            else:
                content = PLAN_WITH_SENTIMENT if "舆情" in user_msg else PLAN
        elif "翻译成中文" in user_msg:
            # 翻译批量请求：按 [1] [2] ... 的条数回同样多段，用译者那边的分隔符拼起来。
            # 内容是什么不重要 —— 这个分支存在的意义是「翻译这一步到底有没有真的发出去」
            n = len(re.findall(r"^\[\d+\]$", user_msg, re.M))
            self.server.translated.append(n)
            joiner = chr(10) + "---POST_SEPARATOR---" + chr(10)
            content = joiner.join(f"译文{i + 1}" for i in range(max(n, 1)))
        elif n_posts > 1:
            if self.server.drop_separator:
                # 模型压根没照分隔符输出，整批 JSON 连成一段 —— parts 只有 1 段，
                # 第 2 条往后落进「parts 不够」那条路。真实模型偶尔就是这样。
                content = "\n".join([GOOD] * n_posts)
            else:
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
    prompts 记下每次请求的 system prompt，用来断言模型确实被告知了某个动作。
    user_prompts 记下每次请求的 user message，用来断言帖子块里带上了讨论串上下文
    和图片描述。
    """

    def __init__(self, port: int = 0, drop_separator: bool = False):
        self._port = port
        # True 时批量结果整段返回、不带分隔符，走「parts 不够」那条路
        self.drop_separator = drop_separator
        self._server = None
        self._thread = None
        self.seen = []
        self.prompts = []
        self.user_prompts = []
        # 每次翻译请求里有几条 —— 「增量跳过」和「全量重译」的唯一分辨点。
        # 必须是 list：int 是不可变的，赋到 server 上就成了两份各自增长的计数
        self.translated = []

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._server.seen = self.seen
        self._server.prompts = self.prompts
        self._server.user_prompts = self.user_prompts
        self._server.translated = self.translated
        self._server.drop_separator = self.drop_separator
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:{}".format(self._server.server_address[1])

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False
