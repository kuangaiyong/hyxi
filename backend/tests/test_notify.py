"""邮件通知 —— 配置、选新回复、补译、真 SMTP 发信、每日定时。规格见 docs/features/email-notify.md

真 SQLite（每条用例一个临时库）、真 HTTP 模型替身（llm_site）、真 SMTP（smtp_site：标准库写的本地邮件服务器，
TLS 证书现生成）、真路由函数与后台作业。帖子时间按「现在往前几小时」现算，因为邮件的基准就是当前时间。
"""
import asyncio
import ipaddress
import os
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
        R3 是新主贴但没有回复 —— 它本身不是「新回复」，哪个窗口都不该出现。F1 是 50 天前的楼层
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
        assert site.messages == []

    def test_an_unreachable_server_says_where_it_tried(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]    # 拿一个此刻没人监听的端口
        body = self.client.post("/api/v1/notify/test", json=self._payload(smtp_port=port)).json()
        assert body["success"] is False and f"127.0.0.1:{port}" in body["message"], body


class TestNotifyDigestEndToEnd(_NotifyBase):
    """「立即发送」整条链路：选新回复（基准 = 当前时间）→ 组 HTML → 真 SMTP 收到"""

    def _send(self, site, **over):
        self._save(self._payload(site, **over))
        status = self._run(self._send_and_wait())
        return status

    def test_the_default_window_is_one_day_back_from_now(self):
        import smtp_site
        self._seed_standard()
        with smtp_site.SmtpSite() as site:
            status = self._send(site)
        assert status["last"]["outcome"] == "sent", status
        assert status["last"]["trigger"] == "manual" and status["last"]["reply_count"] == 4, status
        html = self._html(site.message())
        for word in ("C1-orig", "C2-orig", "C5-orig", "F2-orig"):
            assert word in html, word
        for word in ("C3-orig", "C4-orig", "F1-orig", "F3-orig", "R3-orig"):
            assert word not in html, f"{word} 不在近 1 天里"
        # 主贴是上下文：原文、译文都在，但不算新回复
        for word in ("R1-orig", "R1-zh", "R2-orig", "T0-orig"):
            assert word in html, word

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
        assert "F1-orig" not in html and "R3-orig" not in html

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
        assert subject == "【HYXi 新回复】Facebook 公开小组「HYXi 用户群」等 2 个数据源 · 近 1 天 · 共 5 条", subject
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
        assert msg["Subject"] == "【HYXi 新回复】Facebook 公开小组「HYXi 用户群」 · 近 1 天 · 共 1 条", msg["Subject"]
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
