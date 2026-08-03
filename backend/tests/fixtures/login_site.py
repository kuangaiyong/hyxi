"""带登录门的本地 fixture 站点。

用来验证 facebook_group.js 的登录 / 会话复用 / 两步验证退出路径。真 HTTP、真表单、
真 Set-Cookie、真重定向，选择器与 facebook_group.js 里那一套逐字一致 —— 换句话说
它验的是脚本真实会用的选择器，不是另写一套好过的。

三个账号对应三条分支：
  ok@example.com  / 正确密码 → 登录成功
  2fa@example.com / 任意密码 → 两步验证页（脚本应以退出码 3 交回给人）
  其余                        → 登录失败（#error_box）
"""

import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote

GOOD_USER = "ok@example.com"
GOOD_PASSWORD = "correct-horse-battery"
TWO_FACTOR_USER = "2fa@example.com"
SESSION_COOKIE = "fixture_session"

# 表单结构对齐 2026-08-03 探测到的真实 facebook.com：
#   - 输入框 id 是随机的，只能按 name 选
#   - input[type=submit] 是 0×0 不可见的
#   - 表单里 DOM 顺序第一个 [role=button] 是「显示密码」图标，点它只会把密码显示出来
#   - 页面上没有 [data-testid]，也没有 [name="login"]
# 所以采集器靠在密码框按回车提交；这份 fixture 复刻同样的陷阱，保证测的是真实路径。
_LOGIN_PAGE = """<html><head><meta charset="utf-8"><title>登录</title></head><body>
<form method="POST" action="/login" id="login_form">
  <div role="button" style="width:24px;height:24px" onclick="document.getElementById('_R_1hmkqsqppb6amH1_').type='text'"></div>
  <input type="text" name="email" id="_R_1h6kqsqppb6amH1_" />
  <input type="password" name="pass" id="_R_1hmkqsqppb6amH1_" />
  <input type="submit" value="登录" style="width:0;height:0;position:absolute;left:-9999px" />
</form>
{error}
</body></html>"""

_ERROR_BOX = '<div id="error_box">账号或密码不正确</div>'

_TWO_FACTOR_PAGE = """<html><head><meta charset="utf-8"><title>两步验证</title></head><body>
<form method="POST" action="/checkpoint/2fa">
  <input type="text" name="approvals_code" id="approvals_code" />
  <button type="submit">提交</button>
</form>
</body></html>"""

_FEED_PAGE = """<html><head><meta charset="utf-8"><title>小组</title></head><body>
<div role="feed">
  <div role="article" data-post-id="p1">
    <h3><a href="/u/1">Marieke_V</a></h3>
    <abbr data-utime="1780391640"></abbr>
    <div data-ad-comet-preview="message">Na drie maanden met de HYXi Halo ben ik echt tevreden.</div>
    <div role="article" data-comment-id="c1">
      <h3><a href="/u/2">Joost1988</a></h3>
      <abbr data-utime="1780394520"></abbr>
      <div data-ad-comet-preview="message">Zelfde ervaring hier, +1</div>
    </div>
  </div>
  <div role="article" data-post-id="p2">
    <h3><a href="/u/3">TechNerd_NL</a></h3>
    <abbr data-utime="1780519620"></abbr>
    <div data-ad-comet-preview="message">Firmware 2.4.1 heeft bij mij de WiFi-verbinding gesloopt.</div>
  </div>
</div>
</body></html>"""

_GROUP_RE = re.compile(r"^/groups/\d+/?$")


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, extra_headers=None):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _has_session(self) -> bool:
        return SESSION_COOKIE in (self.headers.get("Cookie") or "")

    def do_GET(self):
        if self.path.startswith("/login"):
            self._send(_LOGIN_PAGE.format(error=""))
            return
        if _GROUP_RE.match(self.path):
            # 未登录时 302 到 /login/?next=... —— 2026-08-03 实测真站就是这个行为，
            # 不是给未登录用户看只读预览
            if not self._has_session():
                target = "/login/?next=" + quote(self.path, safe="")
                self._send("", status=302, extra_headers=[("Location", target)])
                return
            self._send(_FEED_PAGE)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.path.startswith("/login"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        email = (form.get("email") or [""])[0]
        password = (form.get("pass") or [""])[0]

        if email == TWO_FACTOR_USER:
            self._send(_TWO_FACTOR_PAGE)
            return
        if email == GOOD_USER and password == GOOD_PASSWORD:
            self._send(
                _FEED_PAGE,
                extra_headers=[("Set-Cookie", f"{SESSION_COOKIE}=1; Path=/")],
            )
            return
        self._send(_LOGIN_PAGE.format(error=_ERROR_BOX))

    def log_message(self, *args):
        pass


class LoginSite:
    """with LoginSite() as base_url: ..."""

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

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    with LoginSite(port) as base:
        print("login fixture: {}/groups/2407063016436085".format(base), flush=True)
        threading.Event().wait()
