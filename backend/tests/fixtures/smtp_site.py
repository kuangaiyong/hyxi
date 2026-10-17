"""本地 SMTP 服务器，给邮件通知的用例收信 —— 真 TCP、真 SMTP 协议、真 TLS（证书在测试时现生成）。

Python 3.12 已经删掉了 smtpd，aiosmtpd 又不在依赖里，所以用标准库 socketserver 写一个最小实现：
EHLO / STARTTLS / AUTH PLAIN / MAIL / RCPT / DATA / RSET / NOOP / QUIT。换掉的只是「对面那台邮件服务器」，
发信这一侧（smtplib、证书校验、登录、MIME 编码）全是真的。

    with SmtpSite(tls="ssl") as site:        # 465 那种：连上先握手
    with SmtpSite(tls="starttls") as site:   # 587 那种：明文连上，EHLO 里通告 STARTTLS
    with SmtpSite(tls="none") as site:       # 不加密的中继
        site.port / site.messages / site.auths

tls 不是 none 时，进出 with 会设上 / 还原 `SSL_CERT_FILE`，让发信那一侧的 `ssl.create_default_context()`
认这张现生成的证书。生产环境不设它，照样严格校验真证书 —— 所以这里不需要、也不该在被测代码里开任何「不校验」的口子。
**with 块里别发真 HTTPS 请求**：httpx 也认 `SSL_CERT_FILE`，块里就只信这张自签证书了，调真模型会报
CERTIFICATE_VERIFY_FAILED（2026-10-16 用真实数据核实时踩过）。llm_site 是 http，不受影响。
"""

import base64
import datetime
import email
import email.policy
import ipaddress
import os
import shutil
import socketserver
import ssl
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _make_cert(folder: str):
    """自签证书，SAN 同时给 localhost 和 127.0.0.1（连哪个名字都过得了主机名校验）"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cert_path, key_path


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        site = self.server.site
        sock = self.request
        session = {"tls": False, "auth": None, "mail_from": "", "rcpt_to": []}
        if site.tls == "ssl":
            sock = site.server_context.wrap_socket(sock, server_side=True)
            session["tls"] = True
        r, w = sock.makefile("rb"), sock.makefile("wb")

        def send(line):
            w.write(line.encode("utf-8") + b"\r\n")
            w.flush()

        send("220 hyxi-test ESMTP")
        while True:
            raw = r.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            if cmd in ("EHLO", "HELO"):
                lines = ["hyxi-test"]
                if site.tls == "starttls" and not session["tls"]:
                    lines.append("STARTTLS")
                lines.append("AUTH PLAIN")
                for i, item in enumerate(lines):
                    send(("250 " if i == len(lines) - 1 else "250-") + item)
            elif cmd == "STARTTLS" and site.tls == "starttls" and not session["tls"]:
                send("220 2.0.0 ready to start TLS")
                sock = site.server_context.wrap_socket(sock, server_side=True)
                r, w = sock.makefile("rb"), sock.makefile("wb")
                session["tls"] = True
            elif cmd == "AUTH":
                mech, _, initial = arg.partition(" ")
                if mech.upper() != "PLAIN":
                    send("504 5.5.4 unsupported mechanism")
                    continue
                if not initial:
                    send("334 ")
                    initial = r.readline().decode("ascii", "replace").strip()
                _, user, password = base64.b64decode(initial).decode("utf-8").split("\0")
                site.auths.append({"user": user, "password": password, "tls": session["tls"]})
                if site.password is not None and password != site.password:
                    send("535 5.7.8 Error: authentication failed")
                    continue
                session["auth"] = user
                send("235 2.7.0 Authentication successful")
            elif cmd == "MAIL":
                session["mail_from"] = arg
                send("250 2.1.0 ok")
            elif cmd == "RCPT":
                session["rcpt_to"].append(arg)
                send("250 2.1.5 ok")
            elif cmd == "DATA":
                send("354 end data with <CR><LF>.<CR><LF>")
                chunks = []
                while True:
                    chunk = r.readline()
                    if not chunk or chunk == b".\r\n":
                        break
                    chunks.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                site.messages.append({**session, "rcpt_to": list(session["rcpt_to"]), "data": b"".join(chunks)})
                send("250 2.0.0 queued")
            elif cmd == "RSET":
                session.update(mail_from="", rcpt_to=[])
                send("250 2.0.0 ok")
            elif cmd == "NOOP":
                send("250 2.0.0 ok")
            elif cmd == "QUIT":
                send("221 2.0.0 bye")
                return
            else:
                send("502 5.5.2 command not implemented")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # 「明文连到 SSL 端口」这类用例里握手失败是预期内的，不往测试输出里打一整屏 traceback
        import sys
        self.site.errors.append(repr(sys.exc_info()[1]))


class SmtpSite:
    """with SmtpSite(tls="ssl", password="pw") as site: ...

    password 不是 None 时只认这个密码，其余一律 535（复刻「QQ 邮箱填了登录密码而不是授权码」）。
    messages：每封收到的信 {mail_from, rcpt_to, data, tls, auth}；auths：每次 AUTH 收到的用户名、密码、是否已加密。
    host 默认只听本机；要测「非本机地址」时传 "0.0.0.0"，再从局域网地址连进来。
    """

    def __init__(self, tls: str = "none", password: str = None, host: str = "127.0.0.1"):
        assert tls in ("none", "ssl", "starttls"), tls
        self.tls = tls
        self.password = password
        self.host = host
        self.messages = []
        self.auths = []
        self.errors = []
        self.port = 0
        self.cert_path = ""
        self.server_context = None
        self._dir = ""
        self._server = None
        self._thread = None
        self._old_cert_env = None

    def __enter__(self) -> "SmtpSite":
        if self.tls != "none":
            self._dir = tempfile.mkdtemp(prefix="hyxi-smtp-")
            self.cert_path, key_path = _make_cert(self._dir)
            self.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.server_context.load_cert_chain(self.cert_path, key_path)
            self._old_cert_env = os.environ.get("SSL_CERT_FILE")
            os.environ["SSL_CERT_FILE"] = self.cert_path
        self._server = _Server((self.host, 0), _Handler)
        self._server.site = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self.tls != "none":
            if self._old_cert_env is None:
                os.environ.pop("SSL_CERT_FILE", None)
            else:
                os.environ["SSL_CERT_FILE"] = self._old_cert_env
            shutil.rmtree(self._dir, ignore_errors=True)
        return False

    def message(self, i: int = -1):
        """解析第 i 封信（email.message.EmailMessage），用来看标题、正文、附件"""
        return email.message_from_bytes(self.messages[i]["data"], policy=email.policy.default)
