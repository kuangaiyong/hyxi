"""邮件通知 —— 配置、选新回复、补译、真 SMTP 发信、每日定时。规格见 docs/features/email-notify.md

真 SQLite（每条用例一个临时库）、真 HTTP 模型替身（llm_site）、真 SMTP（smtp_site：标准库写的本地邮件服务器，
TLS 证书现生成）、真路由函数与后台作业。帖子时间按「现在往前几小时」现算，因为邮件的基准就是当前时间。
"""
import ast
import asyncio
import base64
import inspect
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

FB = "src_nf_fb"
TW = "src_nf_tw"
PASSWORD = "auth-code-1"


def ts(hours_ago: float) -> str:
    """落盘格式的时间（dd-mm-yyyy HH:MM），从现在往前推"""
    return (datetime.now() - timedelta(hours=hours_ago)).strftime("%d-%m-%Y %H:%M")


def post(fp, user, hours_ago, content, translation="", parent=None, level=0, src=FB):
    return {
        "username": user, "timestamp": ts(hours_ago), "content": content, "translation": translation,
        "page_number": 1, "fingerprint": fp, "source": src, "message_id": f"m_{fp}",
        "parent_fingerprint": parent, "reply_level": level,
        **({"_processed": {"translated": True}} if translation else {}),
    }


def lan_ip() -> str:
    """本机一个非回环的 IPv4（用来模拟「远端」SMTP 服务器）。没有就返回空串"""
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return ""
    return next((ip for ip in ips if not ipaddress.ip_address(ip).is_loopback), "")


class _NotifyBase:
    def setup_method(self):
        import app.config as cfg
        from cryptography.fernet import Fernet
        from app.services import storage
        self.cfg = cfg
        self.storage = storage
        self.tmpdir = tempfile.mkdtemp()
        self._old = (cfg.settings.api_key, cfg.settings.secret_key, cfg.settings.data_dir, storage.DB_PATH)
        cfg.settings.api_key = ""
        cfg.settings.secret_key = Fernet.generate_key().decode()
        cfg.settings.data_dir = self.tmpdir
        storage.DB_PATH = os.path.join(self.tmpdir, "hyxi.db")
        storage.init_db()

        from main import app
        from app.services import notify_service
        from app.services.orchestrator import orchestrator
        self.client = TestClient(app)
        self.notify = notify_service
        self.orchestrator = orchestrator
        storage.save_source({"id": FB, "name": "HYXi 用户群", "collector_id": "facebook_group",
                             "params": {"group_id": "123"}, "enabled": True,
                             "created_at": "2026-10-01T09:00:00"})
        storage.save_source({"id": TW, "name": "Tweakers HYXi 主题", "collector_id": "tweakers",
                             "params": {"thread_id": "2336074"}, "enabled": True,
                             "created_at": "2026-10-01T09:00:01"})

    def teardown_method(self):
        from app.services.scheduler_service import scheduler_service
        # 用例失败时作业可能没走到收尾；进程里的单例不清掉会连累同一 worker 的下一条
        self.notify._state.update(running=None, progress="")
        for sid in (FB, TW):
            self.orchestrator._translation_claims.pop(sid, None)
        self.notify._remove_job(scheduler_service.scheduler)
        (self.cfg.settings.api_key, self.cfg.settings.secret_key,
         self.cfg.settings.data_dir, self.storage.DB_PATH) = self._old
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ===== 工具 =====

    def _payload(self, site=None, **over):
        p = {
            "recipients": ["ops@example.test", "boss@example.test"],
            "source_ids": [FB, TW],
            "days": None,
            "schedule_enabled": False,
            "schedule_time": "09:00",
            "smtp_host": "127.0.0.1",
            "smtp_port": site.port if site else 25,
            "smtp_ssl_port": site.port if site else 465,
            "use_ssl": bool(site and site.tls == "ssl"),
            "sender": "hyxi@example.test",
            "username": "hyxi@example.test",
            "password": PASSWORD,
        }
        if site and site.tls != "none":
            p["smtp_host"] = "localhost"
        p.update(over)
        return p

    def _seed(self, posts):
        by_src = {}
        for p in posts:
            by_src.setdefault(p["source"], []).append(p)
        for sid, rows in by_src.items():
            self.storage.upsert_posts(sid, rows)

    def _seed_standard(self):
        """两个来源：Facebook 串结构 + Tweakers 平铺楼层。全都带译文，不需要模型。

        近 1 天：C1、C2（回复的回复）、C5（新主贴上的新回复）、F2；近 3 天再加 C3、F3；近 14 天再加 C4。
        新主贴：R2（带新回复）、R3（没有回复）—— 新主贴也进邮件（2026-10-17 用户让我定的：没人回复的新主贴
        以前哪封都不会出现，舆情工具不能有这个盲区）。R1 是 40 天前的主贴，只作上下文。F1 是 50 天前的楼层
        """
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1),
            post("c2", "u_c2", 1, "C2-orig", "C2-zh", parent="c1", level=2),
            post("c3", "u_c3", 30, "C3-orig", "C3-zh", parent="r1", level=1),
            post("c4", "u_c4", 10 * 24, "C4-orig", "C4-zh", parent="r1", level=1),
            post("r2", "u_r2", 3, "R2-orig", "R2-zh"),
            post("c5", "u_c5", 1, "C5-orig", "C5-zh", parent="r2", level=1),
            post("r3", "u_r3", 5, "R3-orig", "R3-zh"),
            post("t0", "u_t0", 60 * 24, "T0-orig", "T0-zh", src=TW),
            post("f1", "u_f1", 50 * 24, "F1-orig", "F1-zh", src=TW),
            post("f2", "u_f2", 20, "F2-orig", "F2-zh", src=TW),
            post("f3", "u_f3", 2 * 24, "F3-orig", "F3-zh", src=TW),
        ])

    def _configure_llm(self, url):
        self.storage.set_app_config("llm", {"api_key": "sk-test", "base_url": url, "model_name": "test-model"})

    def _llm_site(self):
        import llm_site
        return llm_site

    def _save(self, payload):
        resp = self.client.put("/api/v1/notify/config", json=payload)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def _send(self, site, **over):
        """保存配置（收件端指向 site）并立即发送一轮，等它结束，返回发送状态"""
        self._save(self._payload(site, **over))
        return self._run(self._send_and_wait())

    async def _send_and_wait(self, timeout=120):
        from app.routers import notify as notify_router
        await notify_router.send_now()
        return await self._wait_done(timeout)

    async def _wait_done(self, timeout=120):
        for _ in range(int(timeout / 0.05)):
            st = self.notify.status()
            if not st["running"]:
                return st
            await asyncio.sleep(0.05)
        raise AssertionError(f"发送一直没结束：{self.notify.status()}")

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    @staticmethod
    def _html(msg):
        return msg.get_body(preferencelist=("html",)).get_content()

    @staticmethod
    def _attachments(msg):
        return [(a.get_filename(), a.get_content()) for a in msg.iter_attachments()]


class TestNotifyConfigEndToEnd(_NotifyBase):
    """配置的存取：密码加密落库、只进不出；留空沿用只限同一台服务器；坏输入当场说清楚"""

    def test_password_is_stored_encrypted_and_never_comes_back(self):
        from app.services import crypto
        body = self._save(self._payload())
        assert body["has_password"] is True and body["is_configured"] is True, body
        assert "password" not in body, body
        got = self.client.get("/api/v1/notify/config")
        assert got.status_code == 200 and PASSWORD not in got.text, got.text
        assert got.json()["recipients"] == ["ops@example.test", "boss@example.test"]
        assert got.json()["source_ids"] == [FB, TW]
        stored = self.storage.get_app_config("notify")
        assert all(PASSWORD not in v for v in stored.values()), "明文密码落库了"
        assert crypto.decrypt(stored["password_enc"]) == PASSWORD

    def test_blank_password_keeps_the_saved_one_only_for_the_same_server_and_user(self):
        """留空 = 不修改。但服务器或用户名换了还沿用旧密码，等于把它发给一台新的服务器（谁都能借这个口子套出来）"""
        from app.services import crypto
        self._save(self._payload())
        body = self._save(self._payload(password="", recipients=["new@example.test"]))
        assert body["has_password"] is True and body["recipients"] == ["new@example.test"], body
        assert crypto.decrypt(self.storage.get_app_config("notify")["password_enc"]) == PASSWORD

        for change in ({"smtp_host": "smtp.evil.test"}, {"username": "other@example.test"}):
            resp = self.client.put("/api/v1/notify/config", json=self._payload(password="", **change))
            assert resp.status_code == 400 and "重新填写密码" in resp.json()["detail"], (change, resp.text)
        assert self.client.get("/api/v1/notify/config").json()["smtp_host"] == "127.0.0.1", "被拒的保存改动了配置"

    def test_bad_input_is_refused_with_the_reason(self):
        cases = [
            ({"recipients": ["not-an-email"]}, "收件人"),
            ({"recipients": []}, "收件人"),
            ({"source_ids": ["src_nope"]}, "数据源"),
            ({"source_ids": []}, "数据源"),
            ({"days": 5}, "近"),
            ({"schedule_time": "9:00"}, "HH:MM"),
            ({"sender": "nobody"}, "发件人"),
            # 评审发现：正则放行、发信时 email.headerregistry.Address 却不认 —— 存得下，每次发送都失败
            ({"sender": "a[b]@c.d"}, "发件人"),
            ({"recipients": ["x(y)@z.w"]}, "收件人"),
            ({"smtp_host": "  "}, "SMTP 服务器"),
            ({"username": ""}, "用户名"),
            ({"password": ""}, "密码"),
        ]
        for change, word in cases:
            resp = self.client.put("/api/v1/notify/config", json=self._payload(**change))
            assert resp.status_code == 400 and word in resp.json()["detail"], (change, resp.text)
        assert self.client.get("/api/v1/notify/config").json()["is_configured"] is False

    def test_no_message_names_a_mail_provider(self):
        """用户要求「QQ 字样都要去掉」（2026-10-19）：报错和提示里不点名哪家邮箱。
        坏输入的报错在这里真触发一遍；发信时才有的几句（发件人被拒、服务器不支持加密）本机替身触发不了，
        就把模块里所有给人看的字符串过一遍 —— 函数说明和注释记的是 QQ 的真实表现，不算"""
        for change in ({"smtp_host": "  "}, {"username": ""}, {"password": ""}):
            detail = self.client.put("/api/v1/notify/config", json=self._payload(**change)).json()["detail"]
            assert not re.search(r"qq", detail, re.I), (change, detail)
        tree = ast.parse(inspect.getsource(self.notify))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        named = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docstrings and re.search(r"qq", n.value, re.I)]
        assert named == [], named

    def test_without_secret_key_the_password_is_not_saved(self):
        self.cfg.settings.secret_key = ""
        resp = self.client.put("/api/v1/notify/config", json=self._payload())
        assert resp.status_code == 400 and "TWEAKERS_SECRET_KEY" in resp.json()["detail"], resp.text
        assert self.storage.get_app_config("notify") == {}

    def test_delete_clears_the_config_and_the_daily_job(self):
        from app.services.scheduler_service import scheduler_service
        self._save(self._payload(schedule_enabled=True))
        assert scheduler_service.scheduler.get_job(self.notify.JOB_ID) is not None
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh")])
        self._run(self._send_and_wait())                     # 留下一条发送记录（没有新回复）
        assert self.notify.status()["last"] is not None
        resp = self.client.delete("/api/v1/notify/config")
        assert resp.status_code == 200 and resp.json()["is_configured"] is False, resp.text
        assert self.storage.get_app_config("notify") == {}
        assert scheduler_service.scheduler.get_job(self.notify.JOB_ID) is None
        # 旧配置的发送记录留着只会误导（「已发送给 2 个收件人」说的是已经删掉的那份配置）
        assert self.notify.status()["last"] is None

    def test_send_now_without_a_saved_config_is_refused(self):
        resp = self.client.post("/api/v1/notify/send")
        assert resp.status_code == 400 and "保存" in resp.json()["detail"], resp.text


class TestNotifyScheduleEndToEnd(_NotifyBase):
    """每天定时发送：job 跟着配置挂上 / 摘掉，调度器启动时按配置恢复"""

    def test_the_daily_job_follows_the_config(self):
        from app.services.scheduler_service import scheduler_service
        self._save(self._payload(schedule_enabled=True, schedule_time="08:30"))
        job = scheduler_service.scheduler.get_job(self.notify.JOB_ID)
        assert job is not None and str(job.trigger) == "cron[hour='8', minute='30']", job and job.trigger
        self._save(self._payload(schedule_enabled=True, schedule_time="21:05", password=""))
        job = scheduler_service.scheduler.get_job(self.notify.JOB_ID)
        assert str(job.trigger) == "cron[hour='21', minute='5']", job.trigger
        self._save(self._payload(schedule_enabled=False, password=""))
        assert scheduler_service.scheduler.get_job(self.notify.JOB_ID) is None, "关掉定时后 job 还在"

    def _start_fresh_scheduler(self):
        """新起一个调度器实例（job store 在本用例的临时库里），照服务启动那样 start()"""
        import app.services.scheduler_service as sched_mod
        svc = sched_mod.SchedulerService()

        async def run():
            svc.start()
            try:
                job = svc.scheduler.get_job(self.notify.JOB_ID)
                return (str(job.trigger) if job else None), [j.id for j in svc.scheduler.get_jobs()]
            finally:
                svc.shutdown()

        try:
            return asyncio.run(run())
        finally:
            # asyncio.run 结束时会把当前事件循环置空，其余用 get_event_loop 的测试会跟着挂
            asyncio.set_event_loop(asyncio.new_event_loop())

    def test_the_daily_job_is_restored_when_the_scheduler_starts(self):
        """job 存在 apscheduler_jobs 里，但以配置为准：重启后照配置重新挂一遍"""
        self._save(self._payload(schedule_enabled=True, schedule_time="07:45"))
        trigger, _ = self._start_fresh_scheduler()
        assert trigger == "cron[hour='7', minute='45']", trigger

    def test_a_broken_notify_config_does_not_stop_the_scheduler(self):
        """同 TestSchedulerStartupResilienceEndToEnd：附属能力的坏配置只跳过自己"""
        import app.services.scheduler_service as sched_mod
        sched_mod.SchedulerService()._save_configs([
            {"id": "good", "description": "正常配置", "interval": "hourly",
             "enabled": True, "created_at": "2026-10-01T00:00:00"},
        ])
        self.storage.set_app_config("notify", {"settings": "{这不是 JSON", "password_enc": "x"})
        trigger, job_ids = self._start_fresh_scheduler()
        assert trigger is None and "good" in job_ids, job_ids


class TestNotifySmtpEndToEnd(_NotifyBase):
    """「发送测试邮件」走真 SMTP：SSL 端口、STARTTLS、本机中继三种连法；密码绝不明文发给远端"""

    def _test_mail(self, site, **over):
        resp = self.client.post("/api/v1/notify/test", json=self._payload(site, **over))
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_ssl_port_delivers_over_tls(self):
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password=PASSWORD) as site:
            body = self._test_mail(site)
        assert body["success"] is True, body
        assert site.auths == [{"user": "hyxi@example.test", "password": PASSWORD, "tls": True}], site.auths
        assert len(site.messages) == 1 and site.messages[0]["tls"] is True
        assert sorted(site.messages[0]["rcpt_to"]) == ["TO:<boss@example.test>", "TO:<ops@example.test>"]
        msg = site.message()
        assert "测试" in msg["Subject"] and msg["From"].addresses[0].addr_spec == "hyxi@example.test", msg["Subject"]

    def test_plain_port_upgrades_with_starttls_before_logging_in(self):
        import smtp_site
        with smtp_site.SmtpSite(tls="starttls", password=PASSWORD) as site:
            body = self._test_mail(site)
        assert body["success"] is True, body
        assert [a["tls"] for a in site.auths] == [True], "密码在 STARTTLS 之前就发出去了"

    def test_a_local_relay_without_tls_is_allowed(self):
        """本机（127.0.0.1）上的中继：流量不出本机，不加密也行。E2E 的收件端就是这样"""
        import smtp_site
        with smtp_site.SmtpSite(tls="none", password=PASSWORD) as site:
            body = self._test_mail(site)
        assert body["success"] is True, body
        assert [a["tls"] for a in site.auths] == [False] and len(site.messages) == 1

    def test_a_remote_server_without_tls_never_sees_the_password(self):
        import smtp_site
        ip = lan_ip()
        if not ip:
            pytest.skip("本机没有非回环的 IPv4，模拟不出「远端」服务器")
        with smtp_site.SmtpSite(tls="none", host="0.0.0.0") as site:
            body = self._test_mail(site, smtp_host=ip)
        assert body["success"] is False and "SSL" in body["message"], body
        assert site.auths == [] and site.messages == [], "明文把密码发给远端了"

    def test_a_rejected_login_tells_to_use_the_authorization_code(self):
        """QQ 邮箱填登录密码而不是授权码时就是 535"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password="the-real-code") as site:
            body = self._test_mail(site)
        assert body["success"] is False and "授权码" in body["message"], body
        assert not re.search(r"qq", body["message"], re.I), body    # 用户要求不点名哪家邮箱
        assert site.messages == []

    def test_a_server_that_hangs_up_after_a_rejected_login_still_says_use_the_authorization_code(self):
        """用户在 QQ 邮箱上实测报过「SMTP 发送失败（smtp.qq.com:465）：Connection unexpectedly closed」（2026-10-18），
        照着这句话什么也改不了。smtp.qq.com 通告 LOGIN 和 PLAIN 两种登录方式（本机探测过）；
        报错不带「: timed out」，说明是对面主动断开。smtplib 先试 PLAIN，被拒（535）后连接断了，
        它照样换 LOGIN 再试，撞上已断开的连接，抛出的错就把 535 盖住了。
        断言里要有 535 和服务器原话：只说「授权码」的话，分不出是真收到了 535，还是把断开一律当成密码错"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password="the-real-code", auth_failure="reply_then_hang_up") as site:
            body = self._test_mail(site)
        msg = body["message"]
        assert body["success"] is False and "授权码" in msg and "535" in msg and "authentication failed" in msg, body
        assert len(site.auths) == 1 and site.messages == [], site.auths

    def test_a_server_that_hangs_up_without_a_reply_is_not_called_a_wrong_password(self):
        """没收到 535 就断开（限流、服务器重启、网络断了），说不清原因就照实报断开 —— 说成「密码不对」
        会把用户支去换授权码，换了照样失败"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password="the-real-code", auth_failure="hang_up") as site:
            body = self._test_mail(site)
        msg = body["message"]
        assert body["success"] is False and "Connection unexpectedly closed" in msg, body
        assert "授权码" not in msg and "密码" not in msg, body

    def test_a_rejection_other_than_535_is_not_called_a_wrong_password(self):
        """只有 535 才是「凭据不对」。454（临时失败）、504（不支持这种方式）之类说成密码错，人会去换授权码，换了照样失败"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password="the-real-code", auth_failure="temp_fail_then_hang_up") as site:
            body = self._test_mail(site)
        msg = body["message"]
        assert body["success"] is False and "454" in msg and "Temporary authentication failure" in msg, body
        assert "授权码" not in msg and "密码" not in msg, body

    def test_a_535_is_not_hidden_by_a_later_rejection(self):
        """第一种方式回 535，换第二种时服务器回 421 再挂断：smtplib 抛的是最后那个 421，真正的原因又被盖住"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password="the-real-code", auth_failure="reject_then_421") as site:
            body = self._test_mail(site)
        msg = body["message"]
        assert body["success"] is False and "535" in msg and "authentication failed" in msg, body
        assert "421" not in msg, body

    def test_a_server_echoing_the_credentials_never_puts_the_password_in_the_message(self):
        """提示里附服务器原话。有的服务器拒绝时会回显收到的认证串（base64 里就是密码），
        而这句提示会进发送记录、显示在页面上 —— 密码只进不出"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password="the-real-code", auth_failure="echo_credentials") as site:
            body = self._test_mail(site)
        msg = body["message"]
        plain = base64.b64encode(f"\0hyxi@example.test\0{PASSWORD}".encode()).decode()
        login_form = base64.b64encode(PASSWORD.encode()).decode()
        assert body["success"] is False and "535" in msg and "authentication failed" in msg, body
        assert PASSWORD not in msg and plain not in msg and login_form not in msg, body

    def test_a_server_that_only_accepts_the_second_login_method_still_lets_us_in(self):
        """smtplib 被拒后换下一种方式再试，服务器只认其中一种时就靠它登上 —— 修 535 被盖住时不能把这个回退一起去掉
        （评审时否掉过「只试一种方式」的写法，就是因为它）"""
        import smtp_site
        with smtp_site.SmtpSite(tls="ssl", password=PASSWORD, auth_failure="plain_rejected") as site:
            body = self._test_mail(site)
        assert body["success"] is True, body
        assert [a.get("mech", "PLAIN") for a in site.auths] == ["PLAIN", "LOGIN"], site.auths
        assert len(site.messages) == 1

    def test_an_unreachable_server_says_where_it_tried(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]    # 拿一个此刻没人监听的端口
        body = self.client.post("/api/v1/notify/test", json=self._payload(smtp_port=port)).json()
        assert body["success"] is False and f"127.0.0.1:{port}" in body["message"], body


class TestNotifyDigestEndToEnd(_NotifyBase):
    """「立即发送」整条链路：选新回复（基准 = 当前时间）→ 组 HTML → 真 SMTP 收到"""

    def test_the_default_window_is_one_day_back_from_now(self):
        import smtp_site
        self._seed_standard()
        with smtp_site.SmtpSite() as site:
            status = self._send(site)
        assert status["last"]["outcome"] == "sent", status
        assert status["last"]["trigger"] == "manual" and status["last"]["reply_count"] == 4, status
        assert status["last"]["root_count"] == 2, status
        html = self._html(site.message())
        for word in ("C1-orig", "C2-orig", "C5-orig", "F2-orig"):
            assert word in html, word
        for word in ("C3-orig", "C4-orig", "F1-orig", "F3-orig"):
            assert word not in html, f"{word} 不在近 1 天里"
        # 老主贴是上下文：原文、译文都在，但不算新内容
        for word in ("R1-orig", "R1-zh", "T0-orig"):
            assert word in html, word
        # 新主贴（有没有回复都算）单独标出来
        assert "R2-orig" in html and "R3-orig" in html
        assert html.count("🆕 新主贴") == 2, html.count("🆕 新主贴")

    def test_wider_windows_add_older_replies_and_never_drop_any(self):
        import smtp_site
        self._seed_standard()
        seen = {}
        for days, extra in ((3, ("C3-orig", "F3-orig")), (14, ("C4-orig",))):
            with smtp_site.SmtpSite() as site:
                status = self._send(site, days=days, password="" if seen else PASSWORD)
            html = self._html(site.message())
            for word in ("C1-orig", "C2-orig", "C5-orig", "F2-orig") + extra:
                assert word in html, (days, word)
            for prev in seen.values():
                assert all(w in html for w in prev), f"窗口放宽到 {days} 天反而丢了回复"
            seen[days] = extra
            assert f"近 {days} 天" in site.message()["Subject"], site.message()["Subject"]
        assert status["last"]["reply_count"] == 7, status
        assert "F1-orig" not in html and "R3-orig" in html

    def test_the_mail_shows_source_original_and_translation_and_nothing_else(self):
        """标明来源；原文 + 译文；不要舆情、不要原文链接；帖子内容一律转义"""
        import smtp_site
        self._seed_standard()
        self._seed([post("c6", "u_c6", 0.5, '<script>alert(1)</script> & <a href="http://evil.test">klik</a>',
                         "带标签的回复", parent="r1", level=1)])
        # 帖子已经有舆情结论时，邮件里照样不能出现（用户明确不要）
        posts = self.storage.load_posts([FB])
        self.storage.save_sentiment("nf-task", {"results": [
            {"sentiment": "negative", "intensity": 4, "reason_cn": "舆情结论不该进邮件", "dimensions": ["固件更新"]}
            for _ in posts]}, posts)
        with smtp_site.SmtpSite() as site:
            self._send(site)
        msg = site.message()
        html = self._html(msg)
        assert "来自：Facebook 公开小组「HYXi 用户群」" in html, html[:2000]
        assert "来自：Tweakers.net 论坛「Tweakers HYXi 主题」" in html
        assert "C1-orig" in html and "C1-zh" in html
        assert "回复 u_c1" in html, "回复的回复要写明回的是谁"
        assert "&lt;script&gt;" in html and "<script" not in html, "帖子内容没有转义"
        # 转义后的帖子正文里还留着 href 这几个字母，要钉的是「没有真正的链接标签」
        assert "<a " not in html and 'href="' not in html, "邮件里不放链接"
        for word in ("舆情结论不该进邮件", "固件更新", "负面", "📊"):
            assert word not in html, f"邮件里出现了舆情内容：{word}"
        assert html.index("R2-orig") < html.index("R1-orig"), "主贴按发表时间从新到旧（同结果页）"
        assert html.index("C1-orig") < html.index("C2-orig"), "串里按发表先后"
        subject = msg["Subject"]
        assert subject == ("【HYXi】Facebook 公开小组「HYXi 用户群」等 2 个数据源 · 近 1 天 · "
                           "新回复 5 条、新主贴 2 条"), subject
        assert self._attachments(msg) == []

    def test_the_subject_names_only_sources_that_have_new_replies(self):
        """真实数据里实测过：Tweakers 那个来源一条帖子都没有，标题却以它打头 ——「Tweakers…等 2 个数据源」。
        标题点名的必须是新回复真正来自的来源；没有新回复的来源只在正文里说一声"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1)])
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[TW, FB])
        msg = site.message()
        assert msg["Subject"] == "【HYXi】Facebook 公开小组「HYXi 用户群」 · 近 1 天 · 新回复 1 条", msg["Subject"]
        html = self._html(msg)
        assert "来自：Tweakers.net 论坛「Tweakers HYXi 主题」" in html and "近 1 天没有新回复" in html

    def test_tweakers_floor_times_are_dutch_local_time(self):
        """Tweakers 的 data-datetime 是荷兰本地时间（不带时区），宿主机是上海时间。评审发现：直接拿上海的「现在」去比，
        窗口整体偏 6~7 小时 —— 20 小时前发的楼层看起来是 26 小时前，当天那封里没有，第二天那封更没有。
        （宿主机本身就在荷兰时区时这条不区分新旧代码；本机是上海时间）"""
        from zoneinfo import ZoneInfo
        import smtp_site
        ams = datetime.now(ZoneInfo("Europe/Amsterdam")).replace(tzinfo=None)
        floors = [("t0", 60 * 24, "T0-orig"), ("f2", 20, "F2-orig"), ("f9", 30, "F9-orig")]
        self._seed([dict(post(fp, f"u_{fp}", 0, content, content + "-zh", src=TW),
                         timestamp=(ams - timedelta(hours=h)).strftime("%d-%m-%Y %H:%M"))
                    for fp, h, content in floors])
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[TW])
        assert status["last"]["outcome"] == "sent", status
        html = self._html(site.message())
        assert "F2-orig" in html, "20 小时前（荷兰时间）发的楼层不在近 1 天里"
        assert "F9-orig" not in html

    def test_a_new_main_post_without_replies_is_in_the_mail_in_full(self):
        """没人回复的新主贴以前哪封邮件都不会出现 —— 一条新发的负面帖没人接话，就永远通知不到（用户让我定：要进邮件）。
        它是新内容本身而不是上下文：全文照登，不按老主贴的 600 字截断。丢了父贴的回复（reply_level > 0）不是主贴，不算"""
        import smtp_site
        long_text = "NEWPOST " * 120 + "THE-END"
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),                         # 老主贴，没有新回复
            post("n1", "u_n1", 5, long_text, "新主贴的译文"),
            post("o1", "u_o1", 2, "ORPHAN-orig", "ORPHAN-zh", parent=None, level=1),  # 父贴没采到的回复
        ])
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[FB])
        assert status["last"]["outcome"] == "sent", status
        assert status["last"]["root_count"] == 1 and status["last"]["reply_count"] == 0, status
        msg = site.message()
        assert msg["Subject"] == "【HYXi】Facebook 公开小组「HYXi 用户群」 · 近 1 天 · 新主贴 1 条", msg["Subject"]
        html = self._html(msg)
        assert "🆕 新主贴" in html and "THE-END" in html, "新主贴要全文照登"
        assert "R1-orig" not in html and "ORPHAN-orig" not in html

    def test_a_new_main_post_cut_from_the_body_is_only_context_there(self):
        """新内容超过 20 条、新主贴本身没排进正文的最新 20 条、只是它下面的回复排进去了：正文里它只作上下文（截断），
        全文在附件里 —— 不然一条很长的老新主贴会把正文撑长，截断提示的条数也对不上（评审发现）"""
        import smtp_site
        long_text = "NEWPOST " * 120 + "THE-END"
        self._seed(
            [post("old", "u_old", 30 * 24, "OLD-orig", "OLD-zh"),
             post("n1", "u_n1", 20, long_text, "新主贴的译文"),
             post("n1r", "u_n1r", 1 / 60, "N1R-orig", "N1R-zh", parent="n1", level=1)]
            + [post(f"x{i:02d}", f"u_x{i:02d}", (i + 1) / 60, f"X{i:02d}-orig", f"X{i:02d}-zh", parent="old", level=1)
               for i in range(1, 26)])
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB])
        msg = site.message()
        body = self._html(msg)
        assert "N1R-orig" in body and "🆕 新主贴" in body, "回复排进了正文，它的新主贴要作上下文出现"
        assert "THE-END" not in body, "没排进最新 20 条的新主贴不该在正文里全文照登"
        (_, full), = self._attachments(msg)
        assert "THE-END" in full

    def test_a_tweakers_topic_is_never_a_new_main_post(self):
        """Tweakers 一个来源就是用户挑好的一条主题，而「主题」只是采到的最早一楼（没采第 1 页时并不是真主题）——
        不能当新主贴报。它下面的新楼层照常算新回复"""
        from zoneinfo import ZoneInfo
        import smtp_site
        ams = datetime.now(ZoneInfo("Europe/Amsterdam")).replace(tzinfo=None)
        self._seed([dict(post(fp, f"u_{fp}", 0, content, content + "-zh", src=TW),
                         timestamp=(ams - timedelta(hours=h)).strftime("%d-%m-%Y %H:%M"))
                    for fp, h, content in (("t0", 10, "T0-orig"), ("f1", 5, "F1-orig"))])
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[TW])
        assert status["last"]["reply_count"] == 1 and status["last"]["root_count"] == 0, status
        assert "🆕 新主贴" not in self._html(site.message())

    def _seed_many(self, n):
        self._seed([post("root", "u_root", 30 * 24, "ROOT-orig", "ROOT-zh")] + [
            post(f"n{i:02d}", f"u_n{i:02d}", i / 60, f"N{i:02d}-orig", f"N{i:02d}-zh", parent="root", level=1)
            for i in range(1, n + 1)
        ])

    def test_more_than_20_new_replies_go_into_an_html_attachment(self):
        import smtp_site
        self._seed_many(25)
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[FB])
        assert status["last"]["attachment"] is True and status["last"]["reply_count"] == 25, status
        msg = site.message()
        html = self._html(msg)
        assert html.count('class="reply"') == 20, html.count('class="reply"')
        assert "N01-orig" in html and "N25-orig" not in html, "正文要列最新的 20 条"
        assert "附件" in html
        (name, full), = self._attachments(msg)
        assert name.endswith(".html") and full.count('class="reply"') == 25, name
        assert "N25-orig" in full and "N25-zh" in full and "<meta charset=\"utf-8\">" in full

    def test_exactly_20_stay_in_the_body(self):
        import smtp_site
        self._seed_many(20)
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB])
        msg = site.message()
        assert self._html(msg).count('class="reply"') == 20 and self._attachments(msg) == []

    def test_nothing_new_sends_nothing(self):
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 30, "C1-orig", "C1-zh", parent="r1", level=1)])
        with smtp_site.SmtpSite() as site:
            status = self._send(site)
        assert status["last"]["outcome"] == "skipped", status
        assert "近 1 天没有新回复" in status["last"]["message"], status
        assert site.messages == []

    def test_the_scheduled_send_takes_the_same_path(self):
        import smtp_site
        self._seed_standard()
        with smtp_site.SmtpSite() as site:
            self._save(self._payload(site))
            self._run(self.notify.scheduled_send())
        status = self.notify.status()
        assert status["last"]["trigger"] == "schedule" and status["last"]["outcome"] == "sent", status
        assert len(site.messages) == 1


class TestNotifyTranslationEndToEnd(_NotifyBase):
    """缺译文先补译：失败重试最多 3 次，仍失败就不发、说清楚；同一来源别人正在翻就等它"""

    def _seed_untranslated(self):
        """一条新回复缺译文，其余都有"""
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("c1", "u_c1", 2, "Nog niet vertaald", "", parent="r1", level=1),
        ])

    def test_missing_translations_are_filled_before_sending(self):
        import smtp_site
        self._seed_untranslated()
        server = self._llm_site().LLMSite()
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))
            status = self._run(self._send_and_wait())
        assert status["last"]["outcome"] == "sent", status
        assert server.translated == [1], f"应该只补译缺译文的那 1 条：{server.translated}"
        stored = {p["fingerprint"]: p for p in self.storage.load_posts([FB])}
        assert stored["c1"]["translation"] == "译文1", stored["c1"]
        assert "译文1" in self._html(site.message())

    def test_translation_that_keeps_failing_is_retried_3_times_and_nothing_is_sent(self):
        import smtp_site
        self._seed_untranslated()
        # 批量回空内容、单条重译也回空：译者每一轮都留失败标记
        server = self._llm_site().LLMSite(batch_empty=True, retry_reply="")
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))
            status = self._run(self._send_and_wait())
        rounds = [n for n in server.translated if n]
        assert len(rounds) == 1 + 3, f"补译应当是 1 次 + 重试 3 次：{server.translated}"
        assert status["last"]["outcome"] == "failed", status
        assert "翻译失败" in status["last"]["message"] and "没有发送" in status["last"]["message"], status
        assert site.messages == [], "翻译失败还是把邮件发出去了"

    def test_a_root_that_cannot_be_translated_does_not_hold_back_the_mail(self):
        """主贴只是上下文。评审发现：它翻不成也算「翻译失败」，一条总被拒译的老主贴能让每天的邮件都发不出去 ——
        页面上写的却是「新回复缺译文的会先补译」。新回复都翻好了就发，主贴只放原文、并在发送记录里说一声"""
        import smtp_site
        self._seed([
            post("r1", "u_r1", 40 * 24, "UNTRANSLATABLE root text", ""),
            post("c1", "u_c1", 2, "Nog niet vertaald", "", parent="r1", level=1),
        ])
        server = self._llm_site().LLMSite(fail_texts=("UNTRANSLATABLE",))
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))
            status = self._run(self._send_and_wait())
        assert status["last"]["outcome"] == "sent", status
        assert "1 条主贴没翻成" in status["last"]["message"], status
        html = self._html(site.message())
        assert "UNTRANSLATABLE root text" in html and "（尚未翻译）" in html
        assert "[翻译失败" not in html, "失败标记被当成译文放进了邮件"
        stored = {p["fingerprint"]: p for p in self.storage.load_posts([FB])}
        assert stored["c1"]["translation"] == "译文1", stored["c1"]

    def test_a_new_main_post_that_cannot_be_translated_holds_back_the_mail(self):
        """新主贴是新内容本身（老主贴才只是上下文），翻不成就和新回复一样：不发、说清楚"""
        import smtp_site
        self._seed([post("n1", "u_n1", 5, "UNTRANSLATABLE new post", "")])
        server = self._llm_site().LLMSite(fail_texts=("UNTRANSLATABLE",))
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))
            status = self._run(self._send_and_wait())
        assert status["last"]["outcome"] == "failed", status
        assert "翻译失败" in status["last"]["message"] and "没有发送" in status["last"]["message"], status
        assert site.messages == []

    def test_clearing_the_config_while_a_send_is_running_is_refused(self):
        """评审发现：发送跑到一半点「清除」，这一轮照样用刚清掉的配置（连同密码）把信发出去，结束时还把发送记录写回来"""
        import smtp_site
        self._seed_untranslated()
        gate = threading.Event()
        server = self._llm_site().LLMSite(translate_gate=gate)
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))

            async def main():
                from app.routers import notify as notify_router
                await notify_router.send_now()
                for _ in range(200):
                    if server.blocked:
                        break
                    await asyncio.sleep(0.05)
                try:
                    await notify_router.delete_config()
                    refused = None
                except HTTPException as e:
                    refused = e.status_code
                finally:
                    gate.set()
                return refused, await self._wait_done()

            refused, status = self._run(main())
        assert refused == 409, refused
        assert self.notify.is_configured(), "被拒的清除还是把配置删了"
        assert status["last"]["outcome"] == "sent", status

    def test_it_waits_while_the_same_source_is_being_translated(self):
        """翻译状态按帖子跨任务共享：同一来源正在被补译 / 任务翻译时另起一轮就是付两遍钱"""
        import smtp_site
        self._seed_untranslated()
        server = self._llm_site().LLMSite()
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))
            assert self.orchestrator.claim_translation([FB], "backfill:other") is None

            async def main():
                from app.routers import notify as notify_router
                await notify_router.send_now()
                for _ in range(200):
                    if "正在翻译" in self.notify.status()["progress"]:
                        break
                    await asyncio.sleep(0.05)
                waiting = self.notify.status()["progress"]
                sent_while_waiting = list(server.translated)
                self.orchestrator.release_translation([FB], "backfill:other")
                return waiting, sent_while_waiting, await self._wait_done()

            waiting, sent_while_waiting, status = self._run(main())
        assert "正在翻译" in waiting, waiting
        assert sent_while_waiting == [], f"别人还占着来源就发了翻译请求：{sent_while_waiting}"
        assert status["last"]["outcome"] == "sent" and server.translated == [1], (status, server.translated)
        assert self.orchestrator.translation_owner([FB]) is None, "发完没放掉来源"

    def test_a_second_send_while_one_is_running_is_refused(self):
        import smtp_site
        self._seed_untranslated()
        gate = threading.Event()
        server = self._llm_site().LLMSite(translate_gate=gate)
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            self._save(self._payload(site, source_ids=[FB]))

            async def main():
                from app.routers import notify as notify_router
                await notify_router.send_now()
                for _ in range(200):
                    if server.blocked:
                        break
                    await asyncio.sleep(0.05)
                try:
                    await notify_router.send_now()
                    second = None
                except HTTPException as e:
                    second = e.status_code
                finally:
                    gate.set()
                return second, await self._wait_done()

            second, status = self._run(main())
        assert second == 409, second
        assert status["last"]["outcome"] == "sent" and len(site.messages) == 1, status


class TestNotifyCatchUpEndToEnd(_NotifyBase):
    """补发：发表在这个来源的补发起点之后、还没随哪封发送成功的邮件发出去过的，这一封补上。
    起点 = 这个来源第一次进邮件那一轮的窗口起点，之后不动。光按「近 N 天」时，采集晚了、某一轮发送失败、
    采集停了几天，都会有一段内容哪封都进不去（三轮评审发现，2026-10-17 用户让我定：要修）。
    判据是「发没发过」（posts.notified_at，只在发送成功后写），不拿时间反推"""

    def _notified(self, fp):
        conn = self.storage._get_conn()
        try:
            row = conn.execute("SELECT notified_at FROM posts WHERE source_id=? AND fingerprint=?",
                               (FB, fp)).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _mark_sent(self, *fps):
        """这几条已经随某封邮件发出去过"""
        self.storage.mark_notified([{"source": FB, "fingerprint": fp} for fp in fps],
                                   (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"))

    def _anchors(self):
        return json.loads(self.storage.get_app_config("notify_marks").get("anchors") or "{}")

    def _set_anchor(self, at, sid=FB):
        """把这个来源的补发起点拨到 at（相当于它第一次进邮件是在那之前）"""
        anchors = self._anchors()
        anchors[sid] = at.isoformat(timespec="seconds")
        self.storage.set_app_config("notify_marks", {"anchors": json.dumps(anchors)})

    def test_only_a_mail_that_was_sent_marks_its_posts(self):
        """新回复、新主贴随发送成功的邮件发出才记「发过」；作上下文的老主贴不记；没发出去的一轮什么都不记。
        起点在第一轮就定下（哪怕那轮失败了），之后不动"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1),
                    post("n1", "u_n1", 3, "N1-orig", "N1-zh")])
        with smtp_site.SmtpSite(password="the-real-code") as site:     # 密码不对：这一轮发不出去
            status = self._send(site, source_ids=[FB])
        assert status["last"]["outcome"] == "failed", status
        assert self._notified("c1") is None, "没发出去也记成发过了"
        anchor = self._anchors()[FB]
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[FB], password=PASSWORD)
        assert status["last"]["outcome"] == "sent", status
        assert self._notified("c1") and self._notified("n1"), "发出去的新回复、新主贴要记下"
        assert self._notified("r1") is None, "老主贴只是上下文，没「发过」"
        assert self._anchors()[FB] == anchor, "起点不该跟着每一轮挪"

    def test_an_old_database_gets_the_column_and_old_rows_stay_unsent(self):
        import sqlite3
        old_db = os.path.join(self.tmpdir, "old.db")
        conn = sqlite3.connect(old_db)
        conn.execute("""CREATE TABLE posts (source_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            seq INTEGER NOT NULL, username TEXT NOT NULL DEFAULT '', timestamp TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', translation TEXT NOT NULL DEFAULT '',
            page_number INTEGER NOT NULL DEFAULT 1, message_id TEXT NOT NULL DEFAULT '',
            parent_fingerprint TEXT, reply_level INTEGER NOT NULL DEFAULT 0,
            images_json TEXT NOT NULL DEFAULT '[]', translated INTEGER NOT NULL DEFAULT 0, sentiment_at TEXT,
            PRIMARY KEY (source_id, fingerprint))""")
        conn.execute("INSERT INTO posts (source_id, fingerprint, seq, content) VALUES (?,?,?,?)",
                     (FB, "legacy", 0, "旧帖"))
        conn.commit()
        conn.close()
        self.storage.DB_PATH = old_db
        self.storage.init_db()
        conn = sqlite3.connect(old_db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(posts)")]
            value = conn.execute("SELECT notified_at FROM posts WHERE fingerprint='legacy'").fetchone()[0]
        finally:
            conn.close()
        assert "notified_at" in cols and value is None, (cols, value)

    def test_merging_duplicate_rows_keeps_the_sent_mark(self):
        """评审发现：重复行合并时「发过」只留在被删的那一行上的话，规范行还是空的，下一封会把它当「之前没发出」再补一遍"""
        self.storage.upsert_posts(FB, [dict(post("a1", "u_dup", 2, "Zelfde bericht", "同一条"), message_id="m_dup")],
                                  merge_by_message_id=False)
        self.storage.upsert_posts(FB, [dict(post("a2", "u_dup", 2, "Zelfde bericht!", "同一条"), message_id="m_dup")],
                                  merge_by_message_id=False)
        self._mark_sent("a2")
        self.storage.merge_duplicate_posts()
        rows = {p["fingerprint"] for p in self.storage.load_posts([FB])}
        assert rows == {"a1"}, rows
        assert self._notified("a1"), "合并后规范行丢了「发过」标记"

    def test_a_reply_collected_after_the_last_mail_is_caught_up(self):
        """照真实时序摆数据：这个来源两天前就开始进邮件了。两条都是 25 小时前发的回复 —— OLD25 随上一封发出去了，
        LATE 上一封时还没采到。光按「近 1 天」两条都超出窗口，LATE 却哪封都没进过，必须补上；OLD25 不能再发"""
        import smtp_site
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1),
            post("old25", "u_old", 25, "OLD25-orig", "OLD25-zh", parent="r1", level=1),
            post("late", "u_late", 25, "LATE-orig", "LATE-zh", parent="r1", level=1),
        ])
        self._mark_sent("old25")
        self._set_anchor(datetime.now() - timedelta(hours=48))
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[FB])
        html = self._html(site.message())
        assert "LATE-orig" in html and "补发" in html, "采集晚了的回复被漏掉了"
        assert "OLD25-orig" not in html, "随上一封发过的不该再发一遍"
        assert status["last"]["reply_count"] == 2, status       # C1（近 1 天里）+ LATE（补发）
        assert self._notified("late"), "补发出去的也要记下，下一封不再补"

    def test_what_a_failed_round_missed_comes_in_the_next_mail(self):
        """评审发现：前一天那轮失败了（SMTP 临时不可用 / 服务停了一天），那一天窗口里的内容既不在今天的窗口里、
        也早于上一封 —— 拿时间反推的话永远进不去。认「发没发过」就没有这道缝"""
        import smtp_site
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("missed", "u_m", 36, "MISSED-orig", "MISSED-zh", parent="r1", level=1),  # 失败那轮窗口里的
            post("sent36", "u_s", 36, "SENT36-orig", "SENT36-zh", parent="r1", level=1),
        ])
        self._mark_sent("sent36")
        self._set_anchor(datetime.now() - timedelta(hours=72))
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB])
        html = self._html(site.message())
        assert "MISSED-orig" in html and "SENT36-orig" not in html, html[:1500]

    def test_posts_collected_after_quiet_days_are_not_lost(self):
        """评审发现：采集停了几天，这几天每轮都「没有新内容」；用会移动的水位线当下界的话，采集恢复后补进来的
        已经落在下界之前。起点不动，就不会丢 —— 起点之前发表的不算（那时还没开始发这个来源）"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh")])
        self._set_anchor(datetime.now() - timedelta(hours=90))   # 不取整天：起点前不到一天的那段要靠精确比较挡住
        with smtp_site.SmtpSite() as site:
            quiet = self._send(site, source_ids=[FB])                      # 采集停着：这一轮什么都没有
        assert quiet["last"]["outcome"] == "skipped", quiet
        self._seed([post("back", "u_b", 60, "BACK-orig", "BACK-zh", parent="r1", level=1),     # 恢复后才采到
                    post("before", "u_x", 93, "BEFORE-orig", "BEFORE-zh", parent="r1", level=1)])
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB], password="")
        html = self._html(site.message())
        assert "BACK-orig" in html and "BEFORE-orig" not in html, html[:1500]

    def test_changing_the_window_does_not_lose_posts(self):
        """评审发现：上一封按近 7 天发、之后改成近 1 天 —— 起点照旧，之前窗口里没发出去的照样补"""
        import smtp_site
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("wide", "u_w", 24 * 4, "WIDE-orig", "WIDE-zh", parent="r1", level=1),
        ])
        self._set_anchor(datetime.now() - timedelta(days=8))
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB], days=None)
        assert "WIDE-orig" in self._html(site.message())

    def test_a_late_reply_that_cannot_be_translated_does_not_block_forever(self):
        """补发的翻不成只放原文、照发 —— 它要是拦邮件，就会每轮都被挑出来、每轮都失败（评审发现）。
        照发之后它记成「发过」，下一封不再补（起点不动，不记的话它会被一直补下去）"""
        import smtp_site
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("late", "u_late", 25, "UNTRANSLATABLE late reply", "", parent="r1", level=1),
        ])
        self._set_anchor(datetime.now() - timedelta(hours=48))
        server = self._llm_site().LLMSite(fail_texts=("UNTRANSLATABLE",))
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            status = self._send(site, source_ids=[FB])
        assert status["last"]["outcome"] == "sent", status
        assert "补发的没翻成" in status["last"]["message"], status
        html = self._html(site.message())
        assert "UNTRANSLATABLE late reply" in html and "（尚未翻译）" in html and "补发" in html
        with smtp_site.SmtpSite() as site:
            again = self._send(site, source_ids=[FB], password="")
        assert again["last"]["outcome"] == "skipped", "发过的补发帖子又被挑了出来"

    def test_the_anchor_is_kept_per_source(self):
        """起点按来源记：新加进来的来源从这一轮才开始，它首次采到的老帖子不是补发（评审发现）"""
        from zoneinfo import ZoneInfo
        import smtp_site
        ams = datetime.now(ZoneInfo("Europe/Amsterdam")).replace(tzinfo=None)
        self._seed([
            post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
            post("late", "u_late", 25, "LATE-orig", "LATE-zh", parent="r1", level=1),
        ] + [dict(post(fp, f"u_{fp}", 0, content, content + "-zh", src=TW),
                  timestamp=(ams - timedelta(hours=h)).strftime("%d-%m-%Y %H:%M"))
             for fp, h, content in (("t0", 60 * 24, "T0-orig"), ("f1", 25, "TWLATE-orig"))])
        self._set_anchor(datetime.now() - timedelta(hours=48), sid=FB)     # 之前只发过 FB
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB, TW])
        html = self._html(site.message())
        assert "LATE-orig" in html, "FB 采集晚了的回复要补上"
        assert "TWLATE-orig" not in html, "TW 这一轮才开始发，它的老楼层不是「补发」"

    def test_deselecting_a_source_forgets_its_anchor(self):
        """评审发现：取消勾选一个来源，过一阵再勾回来 —— 留着旧起点的话，这段时间的帖子会被当「补发」一股脑塞进来"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("mid", "u_m", 10 * 24, "MID-orig", "MID-zh", parent="r1", level=1),
                    post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1)])
        self._set_anchor(datetime.now() - timedelta(days=30))
        self._save(self._payload(source_ids=[TW]))                       # 取消勾选 FB
        assert FB not in self._anchors()
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB, TW], password="")           # 又勾回来
        html = self._html(site.message())
        assert "C1-orig" in html and "MID-orig" not in html, html[:1500]

    def test_a_post_whose_time_is_read_later_is_caught_up(self):
        """入库那轮没读到发表时间（相对时间读不到时故意留空），上一封里自然没有它；之后重采补上了时间 ——
        它没发过、发表在起点之后，补上（评审发现：拿入库时间反推时它哪封都进不去）"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    dict(post("p1", "u_p1", 0, "Tijd onbekend", "时间未知", parent="r1", level=1), timestamp="")])
        self._set_anchor(datetime.now() - timedelta(hours=48))
        # 同一条（message_id 认得出来），这轮读到了时间：指纹跟着变，按 message_id 归并回原来那一行
        self._seed([dict(post("p1-dated", "u_p1", 30, "Tijd onbekend", "时间未知", parent="r1", level=1),
                         message_id="m_p1")])
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB])
        assert "Tijd onbekend" in self._html(site.message())

    def test_a_post_sent_before_is_tagged_when_the_window_repeats_it(self):
        """窗口比发送间隔长时，同一条会连着出现在好几封里 —— 照用户定的窗口照列，但标「已发过」，读的人好跳过"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 30, "C1-orig", "C1-zh", parent="r1", level=1),
                    post("c2", "u_c2", 2, "C2-orig", "C2-zh", parent="r1", level=1)])
        self._mark_sent("c1")
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB], days=3)
        html = self._html(site.message())
        assert "C1-orig" in html and "C2-orig" in html
        tag = ">已发过</span>"                  # 只数标签：标题和抬头里还有一句「含已发过 1 条」
        assert html.count(tag) == 1 and html.index(tag) < html.index("C1-orig"), html[:1500]

    def test_a_window_full_of_posts_already_sent_sends_nothing(self):
        """评审发现：近 7 天窗口、每天发一封，采集停了的那几天窗口里全是前几封发过的 —— 照样每天发一封全是「已发过」的信。
        页面上写的是「没有新内容的那天不发」：只有首次出现的才算新内容"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 30, "C1-orig", "C1-zh", parent="r1", level=1)])
        self._mark_sent("c1")
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[FB], days=7)
        assert status["last"]["outcome"] == "skipped" and site.messages == [], status

    def test_a_post_already_sent_that_cannot_be_translated_does_not_block(self):
        """评审发现：一条早先作为补发、只带原文发出去的回复，改宽窗口后重新落进窗口；它翻不成也拦邮件的话，
        接下来每一轮都失败 —— 而这条收件人早就收到过。已发过的和补发一样翻不成只放原文"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 30, "UNTRANSLATABLE old reply", "", parent="r1", level=1),
                    post("c2", "u_c2", 2, "C2-orig", "C2-zh", parent="r1", level=1)])
        self._mark_sent("c1")
        server = self._llm_site().LLMSite(fail_texts=("UNTRANSLATABLE",))
        with server as url, smtp_site.SmtpSite() as site:
            self._configure_llm(url)
            status = self._send(site, source_ids=[FB], days=3)
        assert status["last"]["outcome"] == "sent", status
        assert "C2-orig" in self._html(site.message())

    def test_clearing_the_config_forgets_what_was_sent(self):
        """评审发现：「清除」不清「发过」标记 —— 换一批收件人重新配，他们一次都没收到过的帖子会被当成已发过（E2E 在开发库上跑完也会留痕）"""
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1)])
        self._save(self._payload())
        self._mark_sent("c1")
        assert self.client.delete("/api/v1/notify/config").status_code == 200
        assert self._notified("c1") is None, "清除配置后「发过」标记还在"

    def test_the_subject_and_header_say_what_is_outside_the_window(self):
        """评审发现：标题和抬头写着「近 1 天」，条数里却混着窗口外的补发 —— 读的人会以为窗口算错了。要写明含几条补发、几条已发过"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1),
                    post("c0", "u_c0", 3, "C0-orig", "C0-zh", parent="r1", level=1),
                    post("late", "u_late", 25, "LATE-orig", "LATE-zh", parent="r1", level=1)])
        self._mark_sent("c0")
        self._set_anchor(datetime.now() - timedelta(hours=48))
        with smtp_site.SmtpSite() as site:
            self._send(site, source_ids=[FB])
        msg = site.message()
        assert "含补发 1 条、已发过 1 条" in msg["Subject"], msg["Subject"]
        assert "含补发 1 条、已发过 1 条" in self._html(msg)

    def test_a_broken_anchor_record_does_not_break_sending(self):
        """评审发现：记录一坏，每轮都在读它时失败，连发送记录都写不进去。坏了只当没有 —— 这一封不补发而已"""
        import smtp_site
        self._seed([post("r1", "u_r1", 40 * 24, "R1-orig", "R1-zh"),
                    post("c1", "u_c1", 2, "C1-orig", "C1-zh", parent="r1", level=1)])
        self.storage.set_app_config("notify_marks", {"anchors": "{这不是 JSON"})
        with smtp_site.SmtpSite() as site:
            status = self._send(site, source_ids=[FB])
        assert status["last"]["outcome"] == "sent" and len(site.messages) == 1, status
        assert FB in self._anchors(), "坏记录要换成好的"
