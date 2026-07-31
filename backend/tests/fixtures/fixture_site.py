"""本地 fixture 站点：把 /forum/list_messages/{id}/{n} 映射到 tweakers_site/page_{n}.html。

出口 IP 已被 Tweakers 防火墙封禁，真实抓取跑不通；用它验证时跑的仍然是真 Chrome、
真 HTTP、真子进程、真 DOM 提取，只是被抓的站点换成本地的，不涉及任何 mock。
"""

import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tweakers_site")

_PATH_RE = re.compile(r"^/forum/list_messages/\d+/(\d+)/?$")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        match = _PATH_RE.match(self.path)
        if not match:
            self.send_error(404)
            return
        page_file = os.path.join(SITE_DIR, "page_{}.html".format(match.group(1)))
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

    def __init__(self, port: int = 0):
        self._port = port
        self._server = None
        self._thread = None

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
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
