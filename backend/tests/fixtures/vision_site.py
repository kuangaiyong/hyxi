"""本地多模态模型替身，用来验证「图片先理解、再参与舆情判断」这条链路。

真 HTTP、真 base64、真 httpx 客户端 —— 换掉的只是模型本身。它把收到的每一次请求
都记下来，所以测试能断言：
  - 发过来的确实是 OpenAI 兼容的 content parts，且 image_url 是合法 data URI
  - data URI 解回来的字节与磁盘上那张图**逐字节相同**（证明不是拿假数据糊弄）
  - 系统提示词里带着与翻译同一份角色和术语表

`fail_status` 用来复刻真实踩到的那种情况：Kimi 配额用尽时 chat/completions 返回 403，
而 /models 照样 200 —— 舆情分析必须照常跑完，只是没有图片描述。
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DESCRIPTION = "图中是一台壁挂式家用储能电池，屏幕显示报错码 E03，下方接线未固定。"

# 低于这个输出预算就当作「推理把 token 烧光」，返回空 content。实测真机推理约
# 300~700 token，取 1024 作为替身的判定线
REASONING_TOKEN_FLOOR = 1024


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))

        system_msg = ""
        images = []
        texts = []
        for m in body.get("messages", []):
            content = m.get("content")
            if m.get("role") == "system":
                system_msg = content or ""
                continue
            # 多模态请求的 content 是 parts 数组；纯文本请求是字符串
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "image_url":
                        images.append(part["image_url"]["url"])
                    elif part.get("type") == "text":
                        texts.append(part.get("text") or "")
            elif content:
                texts.append(content)

        max_tokens = body.get("max_tokens")
        self.server.calls.append({
            "system": system_msg,
            "images": images,
            "texts": texts,
            "model": body.get("model"),
            "temperature": body.get("temperature"),
            "max_tokens": max_tokens,
        })

        # 复刻真机行为：kimi-for-coding 是推理模型，reasoning_content 照样计入
        # max_tokens。实测给 512 时全被推理吃掉，HTTP 200、finish_reason=length、
        # content 是空串 —— 「理解成功但没有描述」，界面上完全看不出异常
        if max_tokens is not None and max_tokens < REASONING_TOKEN_FLOOR:
            payload = json.dumps({
                "choices": [{
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "",
                                "reasoning_content": "思考中……" * 20},
                }],
                "usage": {"completion_tokens": max_tokens,
                          "completion_tokens_details": {"reasoning_tokens": max_tokens}},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # 复刻真机行为：kimi-for-coding 只接受 temperature=1，传别的值整个请求 400。
        # 实测踩过 —— 传了 0.2，每次图片理解都被打回并降级成「这条没有描述」，
        # 功能看着在跑，实际一张图都没理解
        if "temperature" in body and body["temperature"] != 1:
            payload = json.dumps({"error": {
                "message": "invalid temperature: only 1 is allowed for this model",
                "type": "invalid_request_error",
            }}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if self.server.fail_status:
            payload = json.dumps({"error": {
                "message": "You've reached your usage limit for this billing cycle.",
                "type": "access_terminated_error",
            }}).encode("utf-8")
            self.send_response(self.server.fail_status)
        else:
            payload = json.dumps({
                "choices": [{"message": {"content": DESCRIPTION}}],
                "usage": {"total_tokens": 1},
            }).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        # test_connection() 先探 /models。真实 Kimi 在配额用尽时这里照样 200 ——
        # 「连接成功」不等于图片理解可用，这个替身把那个陷阱一起复刻了
        payload = json.dumps({"data": [{"id": "fake-vision"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class VisionSite:
    """with VisionSite() as base_url: ...

    calls 里是每次请求的 system / images / texts，供测试断言。
    """

    def __init__(self, port: int = 0, fail_status: int = 0):
        self._port = port
        self._server = None
        self._thread = None
        self.calls = []
        self.fail_status = fail_status

    @property
    def image_count(self) -> int:
        return sum(len(c["images"]) for c in self.calls)

    def decoded_images(self):
        """把收到的 data URI 解回原始字节，用来与磁盘上的图逐字节比对"""
        out = []
        for call in self.calls:
            for uri in call["images"]:
                assert uri.startswith("data:"), uri
                header, _, b64 = uri.partition(",")
                assert ";base64" in header, header
                out.append(base64.b64decode(b64))
        return out

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._server.calls = self.calls
        self._server.fail_status = self.fail_status
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:{}".format(self._server.server_address[1])

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False
