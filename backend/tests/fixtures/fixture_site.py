"""本地 fixture 站点：把 /forum/list_messages/{id}/{n} 映射到 tweakers_site/page_{n}.html。

出口 IP 已被 Tweakers 防火墙封禁，真实抓取跑不通；用它验证时跑的仍然是真 Chrome、
真 HTTP、真子进程、真 DOM 提取，只是被抓的站点换成本地的，不涉及任何 mock。
"""

import base64
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(FIXTURE_DIR, "tweakers_site")
GROUP_SITE_DIR = os.path.join(FIXTURE_DIR, "group_site")

_PATH_RE = re.compile(r"^/forum/list_messages/\d+/(\d+)/?$")
# 第二个站点：结构与论坛完全不同（主贴 + 嵌套评论，按批次翻页）
_GROUP_RE = re.compile(r"^/groups/\d+/batch/(\d+)/?$")

# 1x1 PNG。正文图必须真的返回 —— 404 的话浏览器按 alt 文本渲染，
# 尺寸过滤和「图落到盘上了没有」就都测不成了
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)


class _Handler(BaseHTTPRequestHandler):
    def _throttle(self) -> bool:
        """前 throttle_first 次页面请求回 503 + Retry-After: 1。

        这是「拿到了响应、对方明确说停」那条路，与 _drop() 的「一次响应都没拿到」
        完全不同 —— gotoPage 对两者的处置也必须不同，两条路各有各的用例。
        """
        limit = getattr(self.server, "throttle_first", 0)
        if not limit:
            return False
        with self.server.drop_lock:
            if self.server.throttled >= limit:
                return False
            self.server.throttled += 1
        # **必须带真实 body**：空 body 的 5xx 会被 Chrome 直接抛成
        # net::ERR_HTTP_RESPONSE_CODE_FAILURE 而不是返回一个 Response，
        # 于是走的是网络失败那条路，限流路径根本测不到（实测踩过）。
        # 真站被拒时也是整页 HTML（Tweakers 那句荷兰语提示），这样才逼真
        body = b""
        if getattr(self.server, "throttle_body", True):
            body = "<html><body>Even geduld, te veel verzoeken.</body></html>".encode("utf-8")
        self.send_response(503)
        self.send_header("Retry-After", "1")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)
        return True

    def _drop(self) -> bool:
        """开头 drop_seconds 秒内的页面请求一律掐断连接，一个字节都不回。

        复现的是「一次响应都没拿到」——真站超时、线路抖动、连接被重置都落在这条路上，
        page.goto 会抛 net::ERR_EMPTY_RESPONSE。用掐连接而不是 sleep 到超时，是因为
        真等 30s 会让这条用例慢得没法进套件，而走的代码路径完全一样（goto 抛异常）。
        只掐页面请求，图片不算——它们不经过 gotoPage。

        **按时间窗掐，不能按次数**：Chrome 对 ERR_EMPTY_RESPONSE 会自己重发好几次
        （实测一次 page.goto 打了 4 个请求），按次数掐的话第一次导航被 Chrome 内部
        重试救回来，gotoPage 压根不会抛异常 —— 那条用例于是在修复前后都是绿的，
        等于什么都没测（实测踩过）。窗口从**第一个页面请求**起算，而不是从服务器
        启动起算：浏览器冷启动要一两秒，按启动起算窗口可能早就过完了。
        """
        window = getattr(self.server, "drop_seconds", 0)
        if not window:
            return False
        with self.server.drop_lock:
            if self.server.drop_t0 is None:
                self.server.drop_t0 = time.monotonic()
            if time.monotonic() - self.server.drop_t0 >= window:
                return False
            self.server.dropped += 1
        self.close_connection = True
        return True

    def do_GET(self):
        if self.path.startswith("/i/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
            return
        match = _PATH_RE.match(self.path)
        group_match = _GROUP_RE.match(self.path)
        if match:
            page_file = os.path.join(SITE_DIR, "page_{}.html".format(match.group(1)))
        elif group_match:
            page_file = os.path.join(GROUP_SITE_DIR, "batch_{}.html".format(group_match.group(1)))
        else:
            self.send_error(404)
            return
        if self._throttle():
            return
        if self._drop():
            return
        if not os.path.exists(page_file):
            self.send_error(404)
            return
        with open(page_file, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FixtureSite:
    """with FixtureSite() as base_url: ..."""

    def __init__(self, port: int = 0, drop_seconds: float = 0, throttle_first: int = 0,
                 throttle_body: bool = True):
        self._port = port
        # 限流响应带不带正文。不带的话 Chrome 压根不返回 Response，直接抛
        # ERR_HTTP_RESPONSE_CODE_FAILURE —— 那条路也不许被当成网络失败重试
        self.throttle_body = throttle_body
        # 前 N 次页面请求回 503（限流路径）。与 drop_seconds 组合就能造出
        # 「先说别打了、随后整个连不上」这条真实场景
        self.throttle_first = throttle_first
        # 第一个页面请求之后的 N 秒内一律掐断连接。0 = 正常站点
        #（既有全部用例的行为一个字都没变）
        self.drop_seconds = drop_seconds
        self._server = None
        self._thread = None

    @property
    def dropped(self) -> int:
        """实际掐掉了几个请求 —— 用来断言这条用例真的制造过网络失败。

        它不等于 gotoPage 的尝试次数（Chrome 会自己重发），要数重试次数得看
        脚本自己打的日志。
        """
        return getattr(self._server, "dropped", 0)

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._server.drop_seconds = self.drop_seconds
        self._server.throttle_first = self.throttle_first
        self._server.throttle_body = self.throttle_body
        self._server.throttled = 0
        self._server.dropped = 0
        self._server.drop_t0 = None
        self._server.drop_lock = threading.Lock()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:{}".format(self._server.server_address[1])

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    with FixtureSite(port) as base:
        print("fixture site: {}/forum/list_messages/9990001/0".format(base), flush=True)
        threading.Event().wait()
