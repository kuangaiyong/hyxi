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

# 注册页：真站登录表单下方那个绿色大按钮指向 /reg/?entry_point=login&next=...，
# 人工授权时误点一下就落到这里。页面上没有 email/pass 输入框，也没有 feed，
# 于是轮询循环两头都不认，会一声不吭地空转到超时 —— 这份 fixture 就是复现那一幕。
_REG_PAGE = """<html><head><meta charset="utf-8"><title>注册</title></head><body>
<form method="POST" action="/reg/">
  <input type="text" name="reg_email__" />
</form>
<a href="/login/">登录</a>
</body></html>"""

# 小组页结构对齐 2026-08-04 探测到的真实（已登录）小组页：
#   - 没有 abbr[data-utime]，也没有 data-post-id / data-comment-id，
#     帖子和评论的 id 只能从固定链接的 URL 里取
#   - 没有 h3 / strong，作者只剩 a[href*="/user/"]，而同一个人会连出几个这样的链接，
#     排在前面的是头像、文本为空 —— 直接 querySelector 取到的是空的那个
#   - 评论正文是第一个 div[dir=auto]，没有专用容器
#   - 主贴时间链接的 aria-label 是**相对时间**，评论的是绝对时间但走 Facebook 账号
#     自己的时区（实测比本地早 15 小时）—— 两个坑都复刻在这里，绝对的本地时间
#     只有 hover 出来的 tooltip 有
#   - 长正文只渲染前几行，末尾挂一个 role=button 的「展开」。不点它，textContent
#     拿到的是残缺正文 + 「展开」两个字（实测有一条整条正文只剩 16 个字符）；
#     点开后按钮文字变成「收起」，同样会被 textContent 吃进正文。第二条主贴复刻这一幕。
_FEED_PAGE = """<html><head><meta charset="utf-8"><title>小组</title></head><body>
<div role="feed">
  <div role="article">
    <a href="/groups/2407063016436085/user/11/" aria-label="Marieke_V"></a>
    <a href="/groups/2407063016436085/user/11/">Marieke_V</a>
    <a href="/groups/2407063016436085/posts/9001/" aria-label="6天"
       data-tip="2026年5月28日周三17:54"><span>6天</span></a>
    <div data-ad-comet-preview="message">Na drie maanden met de HYXi Halo ben ik echt tevreden.</div>
    <div role="article">
      <a href="/groups/2407063016436085/user/22/" aria-label="Joost1988"></a>
      <a href="/groups/2407063016436085/user/22/">Joost1988</a>
      <div dir="auto">Zelfde ervaring hier, +1</div>
      <a href="/groups/2407063016436085/posts/9001/?comment_id=5501"
         aria-label="2026年5月28日凌晨3:42" data-tip="2026年5月28日周三18:42">6天</a>
    </div>
  </div>
  <div role="article">
    <a href="/groups/2407063016436085/user/33/" aria-label="TechNerd_NL"></a>
    <a href="/groups/2407063016436085/user/33/">TechNerd_NL</a>
    <a href="/groups/2407063016436085/posts/9002/" aria-label="4天"
       data-tip="2026年5月30日周五05:27"><span>4天</span></a>
    <div data-ad-comet-preview="message" id="folded"><span>Firmware 2.4.1 heeft…
      <div role="button" tabindex="0" onclick="unfold()">展开</div></span></div>
  </div>
</div>
<script>
function unfold() {
  document.getElementById('folded').innerHTML =
    '<span>Firmware 2.4.1 heeft bij mij de WiFi-verbinding gesloopt. '
    + 'Na een downgrade werkt alles weer. '
    + '<div role="button" tabindex="0">收起</div></span>';
}
</script>
<script>
document.addEventListener('mouseover', function (e) {
  var a = e.target.closest && e.target.closest('a[data-tip]');
  if (!a || document.getElementById('tip')) return;
  var tip = document.createElement('div');
  tip.id = 'tip';
  tip.setAttribute('role', 'tooltip');
  tip.textContent = a.getAttribute('data-tip');
  document.body.appendChild(tip);
});
document.addEventListener('mouseout', function () {
  var t = document.getElementById('tip');
  if (t) t.remove();
});
</script>
</body></html>"""

# 一刻不停在导航的页面。人在窗口里输账号、提交、过验证，每一步都是一次导航，
# 而轮询每 2 秒查一次 loggedIn —— 两者撞上时 Playwright 会抛「Execution context was
# destroyed」。这页把那个窗口放到最大，用来钉死「轮询不能因为导航而把脚本搞挂」。
# 导航挂在 load 上而不是解析期，否则 gotoPage 自己就会被打断，测的就不是轮询了。
_CHURN_PAGE = """<html><head><meta charset="utf-8"><title>跳转中</title></head><body>
<script>window.addEventListener('load', function () {
  setTimeout(function () { location.replace('/churn?n=' + Math.random()); }, 0);
});</script>
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

    def _record_lang(self):
        self.server.request_languages.append(self.headers.get("Accept-Language") or "")

    def do_GET(self):
        self._record_lang()
        if self.path.startswith("/login"):
            self._send(_LOGIN_PAGE.format(error=""))
            return
        if self.path.startswith("/reg"):
            self._send(_REG_PAGE)
            return
        if self.path.startswith("/churn"):
            self._send(_CHURN_PAGE)
            return
        if _GROUP_RE.match(self.path):
            # 未登录时 302 到 /login/?next=... —— 2026-08-03 实测真站就是这个行为，
            # 不是给未登录用户看只读预览
            if not self._has_session():
                if self.server.landing == "reg":
                    target = "/reg/?entry_point=login&next=" + quote(self.path, safe="")
                elif self.server.landing == "churn":
                    target = "/churn"
                else:
                    target = "/login/?next=" + quote(self.path, safe="")
                self._send("", status=302, extra_headers=[("Location", target)])
                return
            self._send(_FEED_PAGE)
            return
        self.send_error(404)

    def do_POST(self):
        self._record_lang()
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
    """with LoginSite() as base_url: ...

    landing="reg" 时未登录访问小组页改跳注册页，用来验证脚本会不会提示走错页面；
    landing="churn" 时改跳一个不停自我导航的页面，用来验证轮询撞上导航不会把脚本搞挂。
    request_languages 收下每个请求的 Accept-Language，是「浏览器界面语言」这件事
    唯一不依赖真站的观测点。
    """

    def __init__(self, port: int = 0, landing: str = "login"):
        self._port = port
        self._landing = landing
        self._server = None
        self._thread = None
        self.request_languages = []

    def __enter__(self) -> str:
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
        self._server.landing = self._landing
        self._server.request_languages = self.request_languages
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
