"""FastAPI 端点集成测试 — 使用 TestClient 真实请求"""

import os
import re
import sys
import json
import asyncio
import tempfile
import threading
import shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient


class TestAPIEndpointsEndToEnd:
    """API 端点端到端测试"""

    @classmethod
    def setup_class(cls):
        cls.tmpdir = tempfile.mkdtemp()
        # 设置数据目录到临时位置
        import app.config as cfg
        # 本机根目录若有 .env，api_key 会在 import 期被读入，这批用例就会全部 401
        cls._old_key = cfg.settings.api_key
        cfg.settings.api_key = ""
        cfg.settings.data_dir = cls.tmpdir
        cfg.settings.tasks_dir = os.path.join(cls.tmpdir, "tasks")
        cfg.settings.exports_dir = os.path.join(cls.tmpdir, "exports")
        os.makedirs(cfg.settings.tasks_dir, exist_ok=True)
        os.makedirs(cfg.settings.exports_dir, exist_ok=True)

        from main import app
        from app.services import storage
        cls.storage = storage
        # DB_PATH 是 storage 被 import 那一刻算好的常量，只改 data_dir 不生效。
        # 本类原先靠「自己是第一个 import storage 的人」侥幸成立 —— 只要有别的用例
        # 先 import 了它，这里的写入就会全部落到真实的 backend/data/hyxi.db 上
        # （实测因此往生产库里写进过测试任务）。文件里其余每个类都显式重定向，补齐它
        cls._old_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        storage.set_app_config("llm", {
            "api_key": "sk-test", "base_url": "https://api.test.com",
            "model_name": "test-model",
        })
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls):
        import app.config as cfg
        cfg.settings.api_key = cls._old_key
        cls.storage.DB_PATH = cls._old_db_path
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_health_check(self):
        resp = self.client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_version_endpoint(self):
        """服务身份走 /api/version：便携包里 / 是前端首页，不再回 JSON"""
        resp = self.client.get("/api/version")
        assert resp.status_code == 200
        assert resp.json()["service"] == "HYXi 舆情分析 API"

    def test_config_crud(self):
        # 保存配置
        resp = self.client.post("/api/v1/config", json={
            "api_key": "sk-test-key",
            "base_url": "https://api.test.com",
            "model_name": "test-model",
        })
        assert resp.status_code == 200
        assert resp.json()["is_configured"] is True

        # 获取配置(不包含 api_key)
        resp = self.client.get("/api/v1/config")
        assert resp.status_code == 200
        assert "api_key" not in resp.json()
        assert resp.json()["base_url"] == "https://api.test.com"

        # 重置配置
        resp = self.client.delete("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["is_configured"] is False

    def test_vision_config_crud_is_isolated_from_the_llm_config(self):
        """多模态配置走 app_config 的 vision 前缀，与 llm 前缀互不影响。

        两组配置混在一起的话，改一个模型会把另一个也改掉 —— 翻译和图片理解用的
        通常不是同一个供应商。
        """
        self.client.post("/api/v1/config", json={
            "api_key": "sk-text", "base_url": "https://text.test", "model_name": "text-model",
        })
        resp = self.client.post("/api/v1/config/vision", json={
            "api_key": "sk-vision", "base_url": "https://vision.test", "model_name": "vl-model",
        })
        assert resp.status_code == 200
        assert resp.json()["is_configured"] is True

        vision = self.client.get("/api/v1/config/vision").json()
        assert vision["model_name"] == "vl-model"
        assert "api_key" not in vision
        # 文本模型没被带跑
        assert self.client.get("/api/v1/config").json()["model_name"] == "text-model"

        # 清掉视觉配置不影响文本配置
        assert self.client.delete("/api/v1/config/vision").json()["is_configured"] is False
        assert self.client.get("/api/v1/config").json()["is_configured"] is True

    def test_vision_config_defaults_when_unset(self):
        """没配置时回默认值且 is_configured=false —— 前端据此显示「纯文本分析」"""
        self.client.delete("/api/v1/config/vision")
        body = self.client.get("/api/v1/config/vision").json()
        assert body["is_configured"] is False
        assert body["base_url"] == "https://api.kimi.com/coding/v1"
        assert body["model_name"] == "kimi-for-coding"

    def test_task_list_empty_initially(self):
        resp = self.client.get("/api/v1/tasks")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_create_task_and_fetch(self):
        resp = self.client.post("/api/v1/tasks", json={
            "description": "端到端测试任务"
        })
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        assert resp.json()["status"] in ("pending", "parsing")  # 取决于执行速度

        # 获取任务详情
        resp = self.client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["description"] == "端到端测试任务"

        # 列表包含该任务
        resp = self.client.get("/api/v1/tasks")
        assert resp.json()["total"] >= 1

    def test_task_list_includes_all(self):
        resp = self.client.get("/api/v1/tasks")
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        assert len(tasks) >= 1

    def test_cancel_task(self):
        # 创建一个任务然后取消
        resp = self.client.post("/api/v1/tasks", json={
            "description": "待取消任务"
        })
        task_id = resp.json()["id"]

        resp = self.client.delete(f"/api/v1/tasks/{task_id}")
        assert resp.status_code in (200, 400)  # 可能已经执行完毕

    def test_schedule_presets(self):
        resp = self.client.get("/api/v1/schedules/presets")
        assert resp.status_code == 200
        presets = resp.json()["presets"]
        assert "hourly" in presets
        assert "daily" in presets

    def test_schedule_crud(self):
        # 创建
        resp = self.client.post("/api/v1/schedules", json={
            "description": "定时测试任务",
            "interval": "daily",
            "time": "09:00",
        })
        assert resp.status_code == 201
        sched_id = resp.json()["id"]
        assert resp.json()["enabled"] is True

        # 列表（检查不报错即可，next_run_time 格式因 APScheduler 版本而异）
        resp = self.client.get("/api/v1/schedules")
        assert resp.status_code == 200

        # 切换
        resp = self.client.post(f"/api/v1/schedules/{sched_id}/toggle")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        # 删除
        resp = self.client.delete(f"/api/v1/schedules/{sched_id}")
        assert resp.status_code == 200

    def _schedules_on_disk(self):
        import app.config as cfg
        path = os.path.join(cfg.settings.data_dir, "scheduled_tasks.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_malformed_time_rejected_and_not_persisted(self):
        before = len(self._schedules_on_disk())
        for bad_time in ("9", "25:00", "09:70", "abc", ""):
            resp = self.client.post("/api/v1/schedules", json={
                "description": "畸形时间",
                "interval": "daily",
                "time": bad_time,
            })
            assert resp.status_code == 422, f"{bad_time!r} 被接受了"
        # 脏配置不得落盘，否则下次启动加载时会砖化调度器
        assert len(self._schedules_on_disk()) == before

    def test_patch_rejects_unknown_interval(self):
        resp = self.client.post("/api/v1/schedules", json={
            "description": "待更新任务", "interval": "daily", "time": "09:00",
        })
        sched_id = resp.json()["id"]

        resp = self.client.patch(f"/api/v1/schedules/{sched_id}", json={"interval": "weekly"})
        assert resp.status_code == 400

        resp = self.client.patch(f"/api/v1/schedules/{sched_id}", json={"time": "99:99"})
        assert resp.status_code == 422

        # 合法更新仍然可用
        resp = self.client.patch(f"/api/v1/schedules/{sched_id}", json={"interval": "hourly"})
        assert resp.status_code == 200
        self.client.delete(f"/api/v1/schedules/{sched_id}")

    def test_patch_paused_schedule_does_not_500(self):
        resp = self.client.post("/api/v1/schedules", json={
            "description": "暂停后再编辑", "interval": "daily", "time": "09:00",
        })
        sched_id = resp.json()["id"]
        assert self.client.post(f"/api/v1/schedules/{sched_id}/toggle").json()["enabled"] is False

        # 暂停中的任务没有注册 job，remove_job 会抛 JobLookupError
        resp = self.client.patch(f"/api/v1/schedules/{sched_id}", json={"description": "改个描述"})
        assert resp.status_code == 200
        assert resp.json()["description"] == "改个描述"
        self.client.delete(f"/api/v1/schedules/{sched_id}")

    def test_404_for_nonexistent_task(self):
        resp = self.client.get("/api/v1/tasks/nonexistent-id")
        assert resp.status_code == 404

    def test_result_endpoints_do_not_500_when_result_is_none(self):
        """未成功完成的任务 result 为 None，结果类端点不得抛 AttributeError"""
        resp = self.client.post("/api/v1/tasks", json={"description": "result 为 None 的任务"})
        task_id = resp.json()["id"]
        assert self.client.get(f"/api/v1/tasks/{task_id}").json()["result"] is None

        for path, allowed in (
            ("posts", (200,)),
            ("stats", (200,)),
            ("export?format=xlsx", (404,)),
            ("export?format=csv", (404,)),
        ):
            r = self.client.get(f"/api/v1/tasks/{task_id}/{path}")
            assert r.status_code in allowed, f"{path} 返回 {r.status_code}，期望 {allowed}"

    def test_sentiment_endpoints_reject_unknown_task(self):
        """task_id 会被拼进文件路径，不存在的任务必须 404 而不是去读文件"""
        for path in ("sentiment", "export?format=xlsx"):
            r = self.client.get(f"/api/v1/tasks/no-such-task/{path}")
            assert r.status_code == 404, f"{path} 返回 {r.status_code}，期望 404"

    def test_sentiment_path_traversal_blocked(self):
        """%5C 在 Windows 上是路径分隔符，不得借此读到 data 目录之外的文件"""
        traversal = "..%5C..%5C..%5C..%5Cfrontend%5Cpackage"
        r = self.client.get(f"/api/v1/tasks/{traversal}/sentiment")
        assert r.status_code == 404
        assert "tweakers-scraper-frontend" not in r.text


class TestSourcesAPIEndToEnd:
    """数据源 CRUD 与凭据加密 — 真实 HTTP 请求 + 真实 SQLite 落盘"""

    PASSWORD = "sup3r-s3cret-pw"

    @classmethod
    def setup_class(cls):
        import app.config as cfg
        from cryptography.fernet import Fernet

        cls.cfg = cfg
        cls._old_api_key = cfg.settings.api_key
        cls._old_secret = cfg.settings.secret_key
        cfg.settings.api_key = ""
        cfg.settings.secret_key = Fernet.generate_key().decode()

        from main import app
        from app.services import storage

        # DB_PATH 是 import 时算好的常量，只改 data_dir 不生效；不重定向就会写进真实的
        # backend/data/hyxi.db —— 这个类单独跑时尤其致命
        cls.tmpdir = tempfile.mkdtemp()
        cls.storage = storage
        cls._old_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()

        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_api_key
        cls.cfg.settings.secret_key = cls._old_secret
        cls.storage.DB_PATH = cls._old_db_path
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _create(self, thread_id="2336074", name="测试源"):
        return self.client.post("/api/v1/sources", json={
            "collector_id": "tweakers",
            "name": name,
            "params": {"thread_id": thread_id},
        })

    def test_collector_catalog_drives_form_rendering(self):
        resp = self.client.get("/api/v1/collectors")
        assert resp.status_code == 200
        tweakers = next(c for c in resp.json() if c["id"] == "tweakers")
        assert tweakers["needs_credentials"] is False
        assert [f["name"] for f in tweakers["param_fields"]] == ["thread_id", "base_url"]
        assert [f["required"] for f in tweakers["param_fields"]] == [True, False]
        group = next(c for c in resp.json() if c["id"] == "facebook_group")
        assert group["incremental_strategy"] == "watermark"

    def test_internal_collector_hidden_from_catalog(self):
        """group_feed 是给 fixture 站点用的通用采集器（base_url 必填、没有真实站点），
        真实版本是 facebook_group。它不该出现在「新增数据源」的下拉框里，但仍要能被
        get_collector 解析 —— 既有数据源和增量回归测试都还在用它"""
        from app.collectors import get_collector

        ids = [c["id"] for c in self.client.get("/api/v1/collectors").json()]
        assert "group_feed" not in ids
        assert ids == ["tweakers", "facebook_group"]
        assert get_collector("group_feed").id == "group_feed"

    def test_source_crud(self):
        resp = self._create(name="Tweakers 主帖")
        assert resp.status_code == 201
        source = resp.json()
        assert source["collector_name"] == "Tweakers.net 论坛"
        assert source["params"]["thread_id"] == "2336074"
        assert source["has_credential"] is False

        sid = source["id"]
        assert self.client.get(f"/api/v1/sources/{sid}").json()["name"] == "Tweakers 主帖"

        resp = self.client.patch(f"/api/v1/sources/{sid}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        assert self.client.delete(f"/api/v1/sources/{sid}").status_code == 200
        assert self.client.get(f"/api/v1/sources/{sid}").status_code == 404

    def test_missing_required_param_rejected(self):
        resp = self.client.post("/api/v1/sources", json={
            "collector_id": "tweakers", "name": "缺参数", "params": {},
        })
        assert resp.status_code == 400
        assert "帖子 ID" in resp.json()["detail"]

    def test_unknown_collector_rejected(self):
        resp = self.client.post("/api/v1/sources", json={
            "collector_id": "myspace", "name": "不存在的采集器", "params": {},
        })
        assert resp.status_code == 400

    def test_undeclared_params_are_dropped(self):
        """params 会原样进 job 文件，未声明的键不能借这条路塞进采集脚本"""
        resp = self.client.post("/api/v1/sources", json={
            "collector_id": "tweakers", "name": "夹带私货",
            "params": {"thread_id": "2336074", "pacing": {"delay_min": 0}, "headless": False},
        })
        assert resp.status_code == 201
        assert set(resp.json()["params"]) == {"thread_id"}
        self.client.delete(f"/api/v1/sources/{resp.json()['id']}")

    def test_credential_never_leaves_backend_and_is_encrypted_at_rest(self):
        import sqlite3
        from app.services import storage

        sid = self._create(name="带凭据的源").json()["id"]
        try:
            resp = self.client.put(f"/api/v1/sources/{sid}/credential", json={
                "username": "tester@example.com", "password": self.PASSWORD,
            })
            assert resp.status_code == 200
            assert resp.json()["has_credential"] is True
            assert resp.json()["credential_username"] == "tester@example.com"

            # 任何出口都不得回显密码
            for path in ("/api/v1/sources", f"/api/v1/sources/{sid}"):
                body = self.client.get(path).text
                assert self.PASSWORD not in body, f"{path} 泄漏了密码明文"
                assert "secret_enc" not in body
                assert "password" not in body

            # 落盘的必须是密文
            conn = sqlite3.connect(storage.DB_PATH)
            try:
                row = conn.execute(
                    "SELECT secret_enc FROM credentials WHERE source_id = ?", (sid,)
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            assert self.PASSWORD not in row[0]

            # 但采集器启动路径能解回明文，否则登录根本没法用
            from app.services import source_service
            assert source_service.get_credential_secret(sid) == self.PASSWORD

            assert self.client.delete(f"/api/v1/sources/{sid}/credential").json()["has_credential"] is False
        finally:
            self.client.delete(f"/api/v1/sources/{sid}")

    def test_deleting_source_cascades_credential(self):
        sid = self._create(name="级联删除").json()["id"]
        self.client.put(f"/api/v1/sources/{sid}/credential", json={
            "username": "u", "password": self.PASSWORD,
        })
        self.client.delete(f"/api/v1/sources/{sid}")

        from app.services import storage
        assert storage.get_credential(sid) is None

    def test_editing_source_keeps_credential(self):
        """改名/停用不能把凭据带走。

        INSERT OR REPLACE 是「删旧行再插新行」，在 foreign_keys=ON 下会触发 credentials 的
        ON DELETE CASCADE —— 界面上点一次「保存」凭据就没了，且毫无提示。
        """
        from app.services import storage

        sid = self._create(name="改名前").json()["id"]
        try:
            self.client.put(f"/api/v1/sources/{sid}/credential", json={
                "username": "keep@example.com", "password": self.PASSWORD,
            })
            assert storage.get_credential(sid) is not None

            resp = self.client.patch(f"/api/v1/sources/{sid}", json={"name": "改名后"})
            assert resp.status_code == 200
            assert resp.json()["name"] == "改名后"
            assert resp.json()["has_credential"] is True

            resp = self.client.patch(f"/api/v1/sources/{sid}", json={"enabled": False})
            assert resp.json()["has_credential"] is True

            cred = storage.get_credential(sid)
            assert cred is not None
            assert cred["username"] == "keep@example.com"
            from app.services import source_service
            assert source_service.get_credential_secret(sid) == self.PASSWORD
        finally:
            self.client.delete(f"/api/v1/sources/{sid}")

    def test_validation_error_does_not_echo_password(self):
        """422 也不能把密码吐回来。

        FastAPI 默认把 pydantic 的 input（提交的原始值）序列化进 detail，
        一次超长校验失败就足以让明文密码进响应体、再被前端拦截器打进控制台。
        """
        sid = self._create(name="超长密码").json()["id"]
        try:
            canary = "LEAK-CANARY-" + "x" * 600
            resp = self.client.put(f"/api/v1/sources/{sid}/credential", json={
                "username": "u", "password": canary,
            })
            assert resp.status_code == 422
            assert "LEAK-CANARY" not in resp.text
            assert '"input"' not in resp.text
            # 报文仍要说清楚哪个字段错在哪，否则没法排查
            assert "password" in resp.text
        finally:
            self.client.delete(f"/api/v1/sources/{sid}")

    def test_credential_refused_without_secret_key(self):
        """没配密钥时必须报错，绝不能静默降级成明文落库"""
        sid = self._create(name="无密钥").json()["id"]
        old = self.cfg.settings.secret_key
        self.cfg.settings.secret_key = ""
        try:
            resp = self.client.put(f"/api/v1/sources/{sid}/credential", json={
                "username": "u", "password": self.PASSWORD,
            })
            assert resp.status_code == 400
            assert "TWEAKERS_SECRET_KEY" in resp.json()["detail"]

            from app.services import storage
            assert storage.get_credential(sid) is None
        finally:
            self.cfg.settings.secret_key = old
            self.client.delete(f"/api/v1/sources/{sid}")

    def test_seed_is_idempotent(self):
        from app.services import source_service

        for src in self.client.get("/api/v1/sources").json():
            self.client.delete(f"/api/v1/sources/{src['id']}")

        source_service.seed_default_sources()
        after_first = self.client.get("/api/v1/sources").json()
        assert len(after_first) == 1
        assert after_first[0]["collector_id"] == "tweakers"

        source_service.seed_default_sources()
        assert len(self.client.get("/api/v1/sources").json()) == 1

    # ===== 人工授权端点 =====
    # 这几条都不触发浏览器：真打 HTTP、真读库，只走「不该启动子进程」的那些分支。
    # 真正开浏览器的成功路径由 test_core.py 的 TestFacebookLoginEndToEnd 覆盖。

    def test_authorize_rejects_collector_that_needs_no_login(self):
        sid = self._create(name="不需要登录").json()["id"]
        try:
            resp = self.client.post(f"/api/v1/sources/{sid}/authorize")
            assert resp.status_code == 400
            assert "不需要登录" in resp.json()["detail"]
        finally:
            self.client.delete(f"/api/v1/sources/{sid}")

    def test_authorize_endpoints_404_on_unknown_source(self):
        assert self.client.post("/api/v1/sources/src_nope/authorize").status_code == 404
        assert self.client.get("/api/v1/sources/src_nope/authorize/events").status_code == 404

    def test_authorize_is_rejected_while_one_is_running(self):
        """同一个数据源不能同时开两个授权窗口"""
        from app.services import source_service

        resp = self.client.post("/api/v1/sources", json={
            "collector_id": "facebook_group", "name": "并发授权",
            "params": {"group_id": "123"},
        })
        sid = resp.json()["id"]
        # 直接把它标成「授权中」，等价于前一个授权还没结束
        source_service._authorizing.add(sid)
        try:
            resp = self.client.post(f"/api/v1/sources/{sid}/authorize")
            assert resp.status_code == 409
            assert "授权中" in resp.json()["detail"]
        finally:
            source_service._authorizing.discard(sid)
            self.client.delete(f"/api/v1/sources/{sid}")

    def test_authorization_result_is_visible_on_the_source(self):
        """授权成功后界面靠 last_auth_at 把徽标从「需重新授权」翻成「会话正常」"""
        from app.services import source_service

        resp = self.client.post("/api/v1/sources", json={
            "collector_id": "facebook_group", "name": "授权徽标",
            "params": {"group_id": "123"},
        })
        sid = resp.json()["id"]
        try:
            assert resp.json()["needs_credentials"] is True
            assert resp.json()["last_auth_at"] is None

            source_service.mark_authorized(sid)
            after = self.client.get(f"/api/v1/sources/{sid}").json()
            assert after["last_auth_at"], "授权成功没有反映到出口模型上"
            # 授权不该动凭据
            assert after["has_credential"] is False
        finally:
            self.client.delete(f"/api/v1/sources/{sid}")


class TestNestedPostsApiEndToEnd:
    """嵌套评论的出口：真实 HTTP 请求打到真实落盘的采集结果上"""

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls._old_api_key = cfg.settings.api_key
        cfg.settings.api_key = ""

        from main import app
        from app.services import storage

        cls.tmpdir = tempfile.mkdtemp()
        cls.storage = storage
        cls._old_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        # 两个来源各自一批真实结构的数据：论坛（无评论）+ 小组（主贴带评论）
        forum = [
            {"username": f"用户{i}", "timestamp": "22-05-2026 17:0{}".format(i),
             "content": f"论坛帖子{i}", "translation": "", "page_number": 1,
             "fingerprint": f"f{i}", "source": "src_forum",
             "parent_fingerprint": None, "reply_level": 0}
            for i in range(3)
        ]
        group = []
        for i in range(2):
            root_fp = f"g{i}"
            group.append({
                "username": f"楼主{i}", "timestamp": "02-06-2026 09:1{}".format(i),
                "content": f"小组主贴{i}", "translation": "", "page_number": 1,
                "fingerprint": root_fp, "source": "src_group",
                "parent_fingerprint": None, "reply_level": 0,
            })
            for j in range(2):
                group.append({
                    "username": f"回复者{i}{j}", "timestamp": "02-06-2026 10:0{}".format(j),
                    "content": f"评论{i}{j} 独特词{i}{j}", "translation": "", "page_number": 1,
                    "fingerprint": f"g{i}c{j}", "source": "src_group",
                    "parent_fingerprint": root_fp, "reply_level": 1,
                })
        storage.upsert_posts("src_forum", forum)
        storage.upsert_posts("src_group", group)

        # 任务里记下来源 id，之后即使把数据源删掉也还能按 id 读回来
        cls.task_id = "nested-e2e"
        from app.services.orchestrator import orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "嵌套出口",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {
                "total_posts": len(forum) + len(group),
                "sources": [
                    {"id": "src_forum", "name": "论坛来源", "collector_id": "tweakers",
                     "post_count": len(forum)},
                    {"id": "src_group", "name": "小组来源", "collector_id": "group_feed",
                     "post_count": len(group)},
                ],
            },
        }
        cls.orchestrator = orchestrator

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_api_key
        cls.storage.DB_PATH = cls._old_db_path
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _posts(self, **params):
        return self.client.get(f"/api/v1/tasks/{self.task_id}/posts", params=params).json()

    def test_pagination_counts_root_posts_only(self):
        """分页粒度是主贴，评论跟着父贴走，不会被分页边界切断"""
        data = self._posts(page_size=200)
        assert data["total"] == 5          # 论坛 3 + 小组 2 个主贴
        assert len(data["posts"]) == 5
        with_replies = [p for p in data["posts"] if p["replies"]]
        assert len(with_replies) == 2
        assert all(len(p["replies"]) == 2 for p in with_replies)
        assert all(r["reply_level"] == 1 for p in with_replies for r in p["replies"])

    def test_index_is_absolute_and_never_overlaps_across_pages(self):
        """index 是扁平存储数组里的真实位置，舆情详情靠它做绝对对齐。

        一页只保证 page_size 个主贴，带上评论后条目数会超出 —— 按页内计数编号
        会让相邻两页的 index 区间重叠，详情弹窗于是显示错帖子。
        """
        page1 = self._posts(page=1, page_size=4)
        page2 = self._posts(page=2, page_size=4)

        def all_indices(payload):
            out = []
            def walk(p):
                out.append(p["index"])
                for c in p["replies"]:
                    walk(c)
            for p in payload["posts"]:
                walk(p)
            return out

        i1, i2 = all_indices(page1), all_indices(page2)
        # 主贴按时间倒序，第一页 4 个主贴是「小组主贴1、小组主贴0、论坛帖子2、论坛帖子1」，
        # 前两个各带 2 条评论
        assert len(i1) == 8, i1
        assert not (set(i1) & set(i2)), f"两页的 index 重叠了: {i1} vs {i2}"
        assert sorted(i1 + i2) == list(range(1, 10))

        # index 必须能直接反查到同一条帖子
        detail = self.client.get(
            f"/api/v1/tasks/{self.task_id}/posts/{i1[-1] - 1}"
        ).json()
        flat = [p for p in page1["posts"] for p in [p] + p["replies"]]
        assert detail["content"] == flat[-1]["content"]

    def test_roots_are_ordered_newest_first(self):
        """主贴按发表时间从新到旧。存储顺序是采集顺序，跟时间毫无关系"""
        data = self._posts(page_size=200)
        got = [p["content"] for p in data["posts"]]
        assert got == ["小组主贴1", "小组主贴0", "论坛帖子2", "论坛帖子1", "论坛帖子0"], got

        times = [p["timestamp"] for p in data["posts"]]
        assert times == sorted(times, reverse=True), times

        # 存储顺序（index）必须原封不动 —— 它是舆情结论的对齐锚点
        assert [p["index"] for p in data["posts"]] == [7, 4, 3, 2, 1]

    def test_sorting_uses_iso_not_raw_dutch_string(self):
        """落盘是 dd-mm-yyyy，直接按字符串排会变成「按日排先」

        07-01（1月7日）会排到 28-06（6月28日）前面，整个顺序错乱且看着还挺像回事。
        """
        self.storage.upsert_posts("src_forum", [{
            "username": "跨年用户", "timestamp": "07-01-2027 08:00",
            "content": "元月的帖子", "translation": "", "page_number": 1,
            "fingerprint": "jan", "source": "src_forum",
            "parent_fingerprint": None, "reply_level": 0,
        }])
        try:
            got = [p["content"] for p in self._posts(page_size=200)["posts"]]
            assert got[0] == "元月的帖子", got
        finally:
            conn = self.storage._get_conn()
            conn.execute("DELETE FROM posts WHERE fingerprint = 'jan'")
            conn.commit()
            conn.close()

    def test_posts_without_a_timestamp_sink_to_the_bottom(self):
        """早期采集读不到 tooltip 绝对时间，落盘留空。这些帖子不该霸占最前面"""
        self.storage.upsert_posts("src_forum", [{
            "username": "无时间用户", "timestamp": "",
            "content": "没有时间的帖子", "translation": "", "page_number": 1,
            "fingerprint": "notime", "source": "src_forum",
            "parent_fingerprint": None, "reply_level": 0,
        }])
        try:
            got = [p["content"] for p in self._posts(page_size=200)["posts"]]
            assert got[-1] == "没有时间的帖子", got
        finally:
            conn = self.storage._get_conn()
            conn.execute("DELETE FROM posts WHERE fingerprint = 'notime'")
            conn.commit()
            conn.close()

    def test_search_hit_on_comment_brings_back_its_root(self):
        data = self._posts(search="独特词10")
        assert data["total"] == 1
        root = data["posts"][0]
        assert root["matched"] is False, "父贴本身没命中，不该标 matched"
        assert root["content"] == "小组主贴1"
        hits = [r for r in root["replies"] if r["matched"]]
        assert [h["content"] for h in hits] == ["评论10 独特词10"]
        # 兄弟评论也一并返回，保住上下文
        assert len(root["replies"]) == 2

    def test_results_survive_source_deletion(self):
        """用户在数据源页删掉来源后，历史任务的结果不能静默变成空白"""
        # 这两个来源本来就没注册过（DB 是空的），等价于「已被删除」
        assert self.client.get("/api/v1/sources").json() == []
        data = self._posts(page_size=200)
        assert data["total"] == 5
        assert {p["source_name"] for p in data["posts"]} == {"论坛来源", "小组来源"}

        stats = self.client.get(f"/api/v1/tasks/{self.task_id}/stats").json()
        assert stats["total_posts"] == 9


class TestOnlyFreshFilterEndToEnd:
    """结果页的「只看新回复」筛选：真实 HTTP 请求打到真实入库的帖子上。

    **必须在服务端过滤**：一页只有 50 个主贴，而「老帖新回复」恰恰因为主贴按时间
    倒序排而被压在后面几页 —— 那正是这个功能要解决的问题。在前端筛只能筛出当前
    页里的，等于什么都没做。
    """

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls._old_api_key = cfg.settings.api_key
        cfg.settings.api_key = ""

        from main import app
        from app.services import storage

        cls.tmpdir = tempfile.mkdtemp()
        cls.storage = storage
        cls._old_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        # 基准是数据集里最新的帖子时间（20-08），不是 datetime.now() ——
        # 所以这份数据永远自洽，不会跑着跑着就过期
        def post(fp, when, text, parent=None):
            return {"username": "u_" + fp, "timestamp": when, "content": text,
                    "translation": "", "page_number": 1, "fingerprint": fp,
                    "source": "src_f", "parent_fingerprint": parent,
                    "reply_level": 1 if parent else 0}

        posts = [
            # A：老主贴 + 很新的回复 —— 3 天和 7 天窗口都该命中
            post("ra", "01-06-2026 10:00", "老主贴A"),
            post("ra1", "19-08-2026 10:00", "新回复A 独特词甲", "ra"),
            # B：整串都旧 —— 任何窗口都不该命中
            post("rb", "05-06-2026 10:00", "老主贴B"),
            post("rb1", "06-06-2026 10:00", "旧回复B 独特词乙", "rb"),
            # C：主贴本身就在窗口内 —— 整串都新，本来就排在最前面，不算「被埋」
            post("rc", "18-08-2026 10:00", "新主贴C"),
            post("rc1", "20-08-2026 10:00", "新回复C", "rc"),
            # D：老主贴 + 5 天前的回复 —— 7 天窗口命中、3 天窗口不该命中
            post("rd", "01-05-2026 10:00", "老主贴D"),
            post("rd1", "15-08-2026 10:00", "新回复D 独特词丁", "rd"),
        ]
        storage.upsert_posts("src_f", posts)

        cls.task_id = "only-fresh-e2e"
        from app.services.orchestrator import orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "只看新回复",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {"total_posts": len(posts), "sources": [
                {"id": "src_f", "name": "论坛", "collector_id": "tweakers",
                 "post_count": len(posts)},
            ]},
        }
        cls.orchestrator = orchestrator

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_api_key
        cls.storage.DB_PATH = cls._old_db_path
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _posts(self, **params):
        return self.client.get(f"/api/v1/tasks/{self.task_id}/posts", params=params).json()

    def _roots(self, data):
        return sorted(p["content"] for p in data["posts"])

    def test_only_fresh_keeps_just_the_threads_with_new_replies(self):
        """不开筛选是 4 串，开了只剩「老主贴 + 窗口内新回复」那两串"""
        assert self._posts(page_size=200)["total"] == 4

        data = self._posts(page_size=200, only_fresh=True, fresh_days=7)
        assert data["total"] == 2, self._roots(data)
        assert self._roots(data) == ["老主贴A", "老主贴D"]
        # total 必须跟着过滤走，否则分页器会显示出翻不到的页码
        assert len(data["posts"]) == 2

    def test_the_window_actually_narrows_the_result(self):
        """3 天窗口把 5 天前那条回复排除掉。

        窗口只是个装饰的话，这条和上面那条会一模一样地绿。
        """
        data = self._posts(page_size=200, only_fresh=True, fresh_days=3)
        assert self._roots(data) == ["老主贴A"], self._roots(data)

    def test_only_fresh_and_search_are_combined_with_and(self):
        """两个条件同时给就都生效 —— 搜到的那串不新，结果必须是空的"""
        both = self._posts(page_size=200, only_fresh=True, fresh_days=7, search="独特词乙")
        assert both["total"] == 0, self._roots(both)
        # 对照：只搜不筛能搜到，证明上面那 0 不是因为搜索本身没命中
        assert self._posts(page_size=200, search="独特词乙")["total"] == 1

    def test_a_bad_window_is_still_rejected(self):
        """新参数不能给非法窗口开后门 —— 静默回落会让用户以为自己换了窗口"""
        resp = self.client.get(
            f"/api/v1/tasks/{self.task_id}/posts",
            params={"only_fresh": True, "fresh_days": 5},
        )
        assert resp.status_code == 400
        assert "3/7/14" in resp.json()["detail"]


class TestExportEndpointEndToEnd:
    """全站唯一的导出口 —— 真实 HTTP 打到真实落盘数据上"""

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls.tmpdir = tempfile.mkdtemp()
        cls._old_key = cfg.settings.api_key
        cls._old_dir = cfg.settings.data_dir
        cfg.settings.api_key = ""
        cfg.settings.data_dir = cls.tmpdir

        from main import app
        from app.services import storage
        cls.storage = storage
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        # 一个主贴带两条评论，再一个主贴 —— 存储顺序刻意与线程顺序不同，
        # order_by_thread 必然重排，对齐一旦写错就会被这批数据抓住
        cls.posts = [
            {"username": "楼主A", "timestamp": "02-06-2026 09:00", "content": "主贴A",
             "translation": "译A", "page_number": 1, "fingerprint": "a",
             "source": "src_x", "parent_fingerprint": None, "reply_level": 0},
            {"username": "楼主B", "timestamp": "03-06-2026 09:00", "content": "主贴B",
             "translation": "译B", "page_number": 1, "fingerprint": "b",
             "source": "src_x", "parent_fingerprint": None, "reply_level": 0},
            {"username": "回复1", "timestamp": "02-06-2026 10:00", "content": "评论A1",
             "translation": "译A1", "page_number": 1, "fingerprint": "a1",
             "source": "src_x", "parent_fingerprint": "a", "reply_level": 1},
            {"username": "回复2", "timestamp": "02-06-2026 11:00", "content": "评论A2",
             "translation": "译A2", "page_number": 1, "fingerprint": "a2",
             "source": "src_x", "parent_fingerprint": "a", "reply_level": 1},
        ]
        storage.upsert_posts("src_x", cls.posts)

        cls.task_id = "export-e2e"
        from app.services.orchestrator import orchestrator
        cls.orchestrator = orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "导出用任务",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {"total_posts": 4, "sources": [
                {"id": "src_x", "name": "小组来源:测试", "collector_id": "group_feed",
                 "post_count": 4},
            ]},
        }

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_key
        cls.cfg.settings.data_dir = cls._old_dir
        cls.storage.DB_PATH = cls._old_db
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _write_sentiment(self, results):
        """入库时 results 仍按**扁平数组**下标给（下标来自 enumerate(all_posts)），
        存储层负责换成 (source_id, fingerprint)"""
        self.storage.save_sentiment(self.task_id, {
            "analyzed_at": "2026-08-05T10:00:00",
            "summary": {"top_dimensions": []},
            "results": results,
        }, self.posts)

    def _drop_sentiment(self):
        conn = self.storage._get_conn()
        try:
            conn.execute("DELETE FROM sentiment_runs WHERE task_id = ?", (self.task_id,))
            conn.execute("DELETE FROM sentiment_results WHERE task_id = ?", (self.task_id,))
            conn.commit()
        finally:
            conn.close()

    def _csv_rows(self):
        import csv as _csv
        from io import StringIO
        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export", params={"format": "csv"})
        assert resp.status_code == 200
        text = resp.content.decode("utf-8-sig")
        return resp, list(_csv.DictReader(StringIO(text)))

    def _filename(self, resp):
        from urllib.parse import unquote
        disposition = resp.headers["content-disposition"]
        return unquote(re.search(r"filename\*=utf-8''([^;]+)", disposition, re.I).group(1))

    def test_sentiment_follows_the_post_not_the_row_number(self):
        """**最容易写错的一处**：舆情下标对齐的是扁平数组，而明细按线程重排。

        先按下标建映射再排序才对。反过来（排完按行号取）每条帖子都会配上别人的结论，
        而表面上完全看不出异常 —— 所以这里用「每条帖子的结论里写着自己的名字」来钉死。
        """
        self._write_sentiment([
            {"sentiment": "positive", "intensity": 5, "reason_cn": "属于:主贴A", "dimensions": ["价格/性价比"]},
            {"sentiment": "negative", "intensity": 4, "reason_cn": "属于:主贴B", "dimensions": []},
            {"sentiment": "neutral", "intensity": 3, "reason_cn": "属于:评论A1", "dimensions": []},
            {"sentiment": "neutral", "intensity": 2, "reason_cn": "属于:评论A2", "dimensions": []},
        ])
        _resp, rows = self._csv_rows()

        # 排序确实发生了：存储顺序是 A,B,A1,A2，导出顺序是「主贴按时间倒序 + 评论跟着走」
        assert [r["原文"] for r in rows] == ["主贴B", "主贴A", "评论A1", "评论A2"]
        for row in rows:
            assert row["分析理由"] == f"属于:{row['原文']}", f"第 {row['序号']} 行串了：{row}"
        assert [r["情感"] for r in rows] == ["负面", "正面", "中立", "中立"]
        assert [r["层级"] for r in rows] == ["0", "0", "1", "1"]

    def test_rows_are_ordered_newest_first(self):
        """导出与页面用同一条排序规则：主贴按发表时间从新到旧，评论跟着自己的主贴走。

        主贴B(03-06) 比主贴A(02-06) 新，所以排在前面 —— 哪怕 A 的评论(10:00/11:00)
        比 B 的发表时间还晚。评论不参与主贴之间的排序。
        """
        self._drop_sentiment()
        _resp, rows = self._csv_rows()
        assert [r["原文"] for r in rows] == ["主贴B", "主贴A", "评论A1", "评论A2"]
        roots = [r["发布时间"] for r in rows if r["层级"] == "0"]
        assert roots == sorted(roots, reverse=True), roots

    def test_force_reanalyzes_posts_that_were_already_done(self):
        """增量按帖子身份跳过已分析的，改了分析口径后老帖子会永远停在旧结论上。

        实测库里 124/125 条都已分析、带图的 23 条**全部**已分析 —— 没有这个入口，
        接上图片理解之后在现有数据上一点变化都看不到。
        """
        self.storage.mark_sentiment_analyzed([
            dict(p, _processed={"sentiment_at": "2026-08-05T10:00:00"}) for p in self.posts
        ])
        try:
            # 不带 force：全都跳过
            body = self.client.post(f"/api/v1/tasks/{self.task_id}/sentiment").json()
            assert body["status"] == "completed", body
            assert "已完成舆情分析" in body["message"]

            # 带 force：4 条全部重新进入待分析
            body = self.client.post(
                f"/api/v1/tasks/{self.task_id}/sentiment?force=true"
            ).json()
            assert body["status"] == "started", body
            assert body["pending_count"] == 4, body
            assert "强制重新分析" in body["message"]
        finally:
            conn = self.storage._get_conn()
            try:
                conn.execute("UPDATE posts SET sentiment_at = NULL WHERE source_id = 'src_x'")
                conn.commit()
            finally:
                conn.close()
            self.orchestrator._sentiment_running.discard(self.task_id)

    def test_unanalyzed_posts_still_export(self):
        """只翻译没跑舆情的任务也得导得出来，否则唯一的导出口对它永远是死的"""
        self._drop_sentiment()
        resp, rows = self._csv_rows()
        assert resp.status_code == 200
        assert len(rows) == 4
        assert {r["情感"] for r in rows} == {"未分析"}
        assert {r["强度"] for r in rows} == {""}
        # 原文译文照常给全
        assert [r["中文翻译"] for r in rows] == ["译B", "译A", "译A1", "译A2"]

    def test_partial_sentiment_does_not_overrun(self):
        """增量分析进行到一半时 results 比 posts 短，越界的按未分析处理"""
        self._write_sentiment([
            {"sentiment": "positive", "intensity": 5, "reason_cn": "属于:主贴A", "dimensions": []},
            {"sentiment": "negative", "intensity": 4, "reason_cn": "属于:主贴B", "dimensions": []},
        ])
        _resp, rows = self._csv_rows()
        by_content = {r["原文"]: r for r in rows}
        assert by_content["主贴A"]["情感"] == "正面"
        assert by_content["主贴B"]["情感"] == "负面"
        assert by_content["评论A1"]["情感"] == "未分析"
        assert by_content["评论A2"]["情感"] == "未分析"

    def test_filename_carries_source_and_export_time(self):
        """文件名要能自证是哪个来源、哪次任务、什么时候导的"""
        from datetime import datetime

        for fmt in ("xlsx", "csv"):
            resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export", params={"format": fmt})
            assert resp.status_code == 200
            name = self._filename(resp)
            assert name.startswith("HYXi舆情_分析报告_"), name
            assert name.endswith(f".{fmt}"), name
            # 来源名里的冒号是 Windows 非法字符，必须被剔掉
            assert "小组来源测试" in name, name
            assert ":" not in name, name
            assert self.task_id[:8] in name, name
            assert f"{datetime.now():%Y%m%d}" in name, name

    def test_multiple_sources_collapse_to_a_count(self):
        """来源一多就不逐个列，否则文件名能长到没法看"""
        task = self.orchestrator.tasks[self.task_id]
        original = task["result"]["sources"]
        task["result"]["sources"] = original + [
            {"id": "src_y", "name": "论坛来源", "collector_id": "tweakers",
             "post_count": 4},
        ]
        try:
            resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export", params={"format": "csv"})
            assert "2个来源" in self._filename(resp)
        finally:
            task["result"]["sources"] = original

    def test_xlsx_is_a_real_workbook_with_both_sheets(self):
        from io import BytesIO
        from openpyxl import load_workbook

        self._write_sentiment([
            {"sentiment": "positive", "intensity": 5, "reason_cn": "好", "dimensions": ["价格/性价比"]},
        ])
        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export", params={"format": "xlsx"})
        assert resp.status_code == 200
        wb = load_workbook(BytesIO(resp.content))
        # 这批帖子都没有配图，所以不该多出一张「配图」表
        assert wb.sheetnames == ["概览", "帖子明细"]
        ws = wb["帖子明细"]
        assert ws.max_row == 5                       # 表头 + 4 条
        # 按列名定位，别写死列号 —— 加一列就得改一遍测试，改漏了还查不出来
        col = {c.value: c.column for c in ws[1]}
        assert {"原文", "中文翻译", "图片描述", "配图", "情感"} <= set(col)
        # 结论写给扁平数组第 0 条（主贴A），导出按时间倒序后它落在第 3 行
        assert ws.cell(3, col["原文"]).value == "主贴A"
        assert ws.cell(3, col["情感"]).value == "正面"

    def test_format_must_be_one_of_the_two(self):
        for bad in ("json", "pdf", "XLS", ""):
            resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export", params={"format": bad})
            assert resp.status_code == 400, f"format={bad!r} 返回 {resp.status_code}"

    def test_default_format_is_xlsx(self):
        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export")
        assert resp.status_code == 200
        assert self._filename(resp).endswith(".xlsx")

    def test_old_download_endpoints_are_gone(self):
        """四个旧下载口已合并，留着等于让用户拿到不含舆情的半份数据"""
        for path in ("download", "export/csv", "export/json", "sentiment/download"):
            resp = self.client.get(f"/api/v1/tasks/{self.task_id}/{path}")
            assert resp.status_code == 404, f"{path} 仍然可用（{resp.status_code}）"


class TestQuoteApiEndToEnd:
    """引用在出口的形状：真 HTTP → 真 SQLite → 真导出。

    采集侧只给一份快照（引用块的原文 + 被引用楼层的 id）。出口要拿这个 id 去本任务的
    帖子里找那条楼层，**用那条楼层自己的正文、译文和配图** —— 只有这条路拿得到全文
    （原站长引用被服务端截断成 `[...]`，点不开），也只有这条路能保证引用图不缺不错：
    那些图本来就是那条楼层自己的。找不到才退回快照，并标 `resolved=false`。
    """

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls.tmpdir = tempfile.mkdtemp()
        cls._old_key = cfg.settings.api_key
        cls._old_dir = cfg.settings.data_dir
        cfg.settings.api_key = ""
        cfg.settings.data_dir = cls.tmpdir

        from main import app
        from app.services import storage
        cls.storage = storage
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        # 一源一串（Tweakers）：一条主题 + 三条回复，三种引用形态各一条
        cls.forum = [
            {"username": "楼主", "timestamp": "22-05-2026 17:06", "content": "主题正文",
             "translation": "主题译文", "page_number": 1, "message_id": "1001",
             "fingerprint": "t1", "source": "src_thread",
             "parent_fingerprint": None, "reply_level": 0},
            {"username": "回复甲", "timestamp": "22-05-2026 18:41", "content": "我同意 @楼主",
             "translation": "我同意译文", "page_number": 1, "message_id": "1002",
             "fingerprint": "r1", "source": "src_thread",
             "parent_fingerprint": "t1", "reply_level": 1,
             # 引用了主题，带的图是引用块里那些 —— 出口要用主题自己那份。
             # **两条引用**：真站实测一条楼层可以引多人，只留第一条就是静默丢内容
             "quotes": [
                 {"message_id": "1001", "cite": "楼主 schreef op vrijdag 22 mei 2026 @ 17:06",
                  "username": "楼主", "content": "被截断的主题正文 [...]",
                  "truncated": True, "images": ["src_thread/r1_q0.png"]},
                 {"message_id": "8888", "cite": "", "username": "",
                  "content": "第二段被引用的内容，没有链接", "truncated": False, "images": []},
             ]},
            {"username": "回复乙", "timestamp": "23-05-2026 09:12", "content": "补充一句",
             "translation": "补充译文", "page_number": 1, "message_id": "1003",
             "fingerprint": "r2", "source": "src_thread",
             "parent_fingerprint": "t1", "reply_level": 1,
             # 引用了一条**没采到**的楼层（或者已被删）
             "quotes": [{"message_id": "9999", "cite": "老张 schreef op vrijdag 1 mei 2026 @ 10:00",
                         "username": "老张", "content": "那段没采到的正文",
                         "truncated": False, "images": ["src_thread/r2_q0.png"]}]},
            {"username": "回复丙", "timestamp": "24-05-2026 09:12", "content": "没有引用",
             "translation": "", "page_number": 1, "message_id": "1004",
             "fingerprint": "r3", "source": "src_thread",
             "parent_fingerprint": "t1", "reply_level": 1},
        ]
        # 一源多主贴（Facebook）：一条引用都没有
        cls.group = [
            {"username": "楼主G", "timestamp": "02-06-2026 09:00", "content": "小组主贴",
             "translation": "小组译文", "page_number": 1, "message_id": "9001",
             "fingerprint": "g1", "source": "src_feed",
             "parent_fingerprint": None, "reply_level": 0},
        ]
        storage.upsert_posts("src_thread", cls.forum)
        storage.upsert_posts("src_feed", cls.group)

        cls.task_id = "quote-e2e"
        from app.services.orchestrator import orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "引用出口",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {
                "total_posts": len(cls.forum) + len(cls.group),
                "sources": [
                    {"id": "src_thread", "name": "论坛串", "collector_id": "tweakers"},
                    {"id": "src_feed", "name": "小组", "collector_id": "facebook_group"},
                ],
            },
        }
        cls.orchestrator = orchestrator

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_key
        cls.cfg.settings.data_dir = cls._old_dir
        cls.storage.DB_PATH = cls._old_db
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _flat(self):
        """{index: PostData}，把 replies 展开 —— 回复挂在主贴的 replies 下。

        用 index 不用 message_id：出口一直没暴露 message_id（那是采集期的字段），
        而 index 是扁平数组里的绝对位置，正好用来对「被引用的是第几条」。
        """
        data = self.client.get(
            f"/api/v1/tasks/{self.task_id}/posts", params={"page_size": 200}).json()
        out = {}

        def walk(p):
            out[p["index"]] = p
            for r in p.get("replies") or []:
                walk(r)

        for p in data["posts"]:
            walk(p)
        return out

    def test_a_quote_survives_the_round_trip(self):
        """quote_json 落库再读回来逐字段相等，且没有引用的帖子连键都没有"""
        posts = self.storage.load_posts(["src_thread"])
        by_fp = {p["fingerprint"]: p for p in posts}
        quote = by_fp["r1"]["quotes"][0]
        assert quote["message_id"] == "1001"
        assert quote["cite"].startswith("楼主 schreef op")
        assert quote["images"] == ["src_thread/r1_q0.png"]
        assert quote["truncated"] is True
        assert "quotes" not in by_fp["r3"], "没有引用的帖子凭空多了一个空数组"

    def test_a_resolved_quote_uses_the_quoted_floors_own_content(self):
        """被引用楼层在库里 → 用**它自己**的正文、译文与配图，全文不缺、图不错配"""
        quoted = self._flat()[2]["quotes"][0]
        assert quoted["resolved"] is True
        assert quoted["message_id"] == "1001"
        assert quoted["username"] == "楼主"
        assert quoted["content"] == "主题正文", "用了快照里那段被截断的正文"
        assert quoted["translation"] == "主题译文", "只看译文时引用框里会冒出一段荷兰语"
        assert quoted["index"] == 1
        # 截断是原站那一段快照的属性，全文在手就跟我们无关了
        assert quoted["truncated"] is False

    def test_an_unresolved_quote_falls_back_to_the_snapshot(self):
        """被引用楼层没采到 → 快照兜底，并**明说**它可能只有一截"""
        quoted = self._flat()[3]["quotes"][0]
        assert quoted["resolved"] is False
        assert quoted["message_id"] == "9999"
        assert quoted["username"] == "老张"
        assert quoted["content"] == "那段没采到的正文"
        assert quoted["index"] is None
        # 快照自己带的图也得留着 —— 那是这条引用里唯一的图
        assert quoted["images"] == ["src_thread/r2_q0.png"]

    def test_a_post_without_a_quote_reports_null(self):
        assert self._flat()[4]["quotes"] == []

    def test_several_quotes_on_one_floor_are_all_rendered(self):
        """**一条楼层引多人**：真站实测支持（串 2336074 第 1 页有一处）。

        只渲染 / 只解析第一条是静默丢内容 —— 用户在页面上完全看不出少了一段被引用的东西。
        """
        quoted = self._flat()[2]["quotes"]
        assert len(quoted) == 2, f"第二条引用被丢了: {quoted}"
        assert quoted[0]["username"] == "楼主" and quoted[0]["resolved"] is True
        assert quoted[0]["content"] == "主题正文", "第一条没走「用被引用楼层自己那份」"
        assert quoted[1]["message_id"] == "8888"
        assert quoted[1]["resolved"] is False
        assert quoted[1]["content"] == "第二段被引用的内容，没有链接"

    def test_the_export_lists_every_quote_of_a_floor(self):
        from app.routers.results import _export_rows

        rows = _export_rows({"description": "论坛串"}, self.forum, [], 7)
        by_index = {r["index"]: r for r in rows}
        cell = by_index[2]["quote"]
        assert cell.count("引用 @") == 2, f"导出里只留了一条引用: {cell}"
        assert "｜" in cell

    def test_thread_kind_comes_from_the_collector_declaration(self):
        """一源一串 / 一源多主贴由采集器声明给出，前端不认 collector_id"""
        flat = self._flat()
        assert flat[1]["thread_kind"] == "thread"
        assert flat[5]["thread_kind"] == "feed"

    def test_search_reaches_the_quoted_text(self):
        """搜一个说法时，「正在回它的人」应该和被引用的人一起出现"""
        data = self.client.get(
            f"/api/v1/tasks/{self.task_id}/posts",
            params={"search": "老张", "page_size": 200}).json()
        hit = [p for p in data["posts"] if p["index"] == 1]
        assert hit, "引用里出现的名字搜不到"
        assert any(r["matched"] for r in hit[0]["replies"]), "命中项没标出来"

    def test_the_export_gains_a_quote_column_only_when_there_are_quotes(self):
        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export", params={"format": "csv"})
        assert resp.status_code == 200
        header = resp.content.decode("utf-8-sig").splitlines()[0]
        assert "引用" in header, f"报告里看不出在回复谁: {header}"

    def test_a_report_without_quotes_has_exactly_the_old_columns(self):
        """**红线**：Facebook 那种一条引用都没有的报告，列集合与改动前完全一致。

        导出是两种格式共用的，凭空多一列全空会让既有报告的下游解析全变 ——
        「不影响 Facebook 数据源的正常使用」这条要求在这里落地。
        """
        from app.routers.results import _export_rows
        from app.services.excel_service import EXPORT_COLUMNS

        rows = _export_rows({"description": "只有小组"}, self.group, [], 7)
        from app.services.excel_service import export_columns

        assert [k for k, _ in export_columns(rows)] == [k for k, _ in EXPORT_COLUMNS]

    def test_the_quote_column_carries_who_and_what(self):
        from app.routers.results import _export_rows

        rows = _export_rows({"description": "论坛串"}, self.forum, [], 7)
        by_index = {r["index"]: r for r in rows}
        # 明细表按「主题 → 它的回复」排，主题是第 1 行
        assert by_index[1]["quote"] == ""
        resolved = by_index[2]["quote"]
        assert resolved.startswith("引用 @楼主（#1）：主题正文"), resolved
        fallback = by_index[3]["quote"]
        assert "原楼未采集" in fallback and "那段没采到的正文" in fallback, fallback


class TestPostSourceUrlEndToEnd:
    """主贴要带原帖固定链接 —— 真 HTTP → 真 SQLite → 真数据源记录

    用户要的是「在结果页点一下就跳到 Facebook 原贴」。链接**现算**：group_id 在
    sources.params_json 里、message_id 在 posts 表里，两样都已经有了，存一份 url 列
    是双写（CLAUDE.md 存储红线第四条），而且历史数据全都没有那一列 —— 现算则连三个月
    前采的帖子一起有链接。

    站点 URL 形态是**站点知识**，落在采集器声明里而不是出口路由里，否则
    「新增一个来源 = 一个 Node 脚本加十几行声明」这条前提就破了。
    """

    GROUP_ID = "2407063016436085"
    # 用户报的那条主贴，permalink 里的数字就是库里的 message_id（实测核对过 seq=26）
    MESSAGE_ID = "2494381381037581"

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls.tmpdir = tempfile.mkdtemp()
        cls._old_key = cfg.settings.api_key
        cls._old_dir = cfg.settings.data_dir
        cfg.settings.api_key = ""
        cfg.settings.data_dir = cls.tmpdir

        from main import app
        from app.services import storage, source_service
        cls.storage = storage
        cls.source_service = source_service
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        # 真的注册一个数据源：链接要靠它的 params 里的 group_id 才拼得出来
        cls.fb = source_service.create_source(
            collector_id="facebook_group", name="Facebook 小组",
            params={"group_id": cls.GROUP_ID},
        )
        cls.tw = source_service.create_source(
            collector_id="tweakers", name="Tweakers", params={"thread_id": "2336074"},
        )

        storage.upsert_posts(cls.fb["id"], [
            {"username": "Dries Boink", "timestamp": "31-07-2026 17:47",
             "content": "Iemand enig idee hoe ik een account aanmaak?",
             "page_number": 2, "fingerprint": "fbroot", "message_id": cls.MESSAGE_ID,
             "source": cls.fb["id"], "parent_fingerprint": None, "reply_level": 0},
            {"username": "Peter Quekel", "timestamp": "13-08-2026 05:03",
             "content": "Klopt, via de api krijg je alleen gegevens.",
             "page_number": 2, "fingerprint": "fbreply", "message_id": "2506705343138518",
             "source": cls.fb["id"], "parent_fingerprint": "fbroot", "reply_level": 1},
        ])
        storage.upsert_posts(cls.tw["id"], [
            {"username": "Dorpjes", "timestamp": "22-05-2026 17:06", "content": "Halo test",
             "page_number": 1, "fingerprint": "twroot", "message_id": "77123456",
             "source": cls.tw["id"], "parent_fingerprint": None, "reply_level": 0},
        ])

        cls.task_id = "source-url-e2e"
        from app.services.orchestrator import orchestrator
        cls.orchestrator = orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "带链接的任务",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {"total_posts": 3, "sources": [
                {"id": cls.fb["id"], "name": "Facebook 小组",
                 "collector_id": "facebook_group", "post_count": 2},
                {"id": cls.tw["id"], "name": "Tweakers",
                 "collector_id": "tweakers", "post_count": 1},
            ]},
        }

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_key
        cls.cfg.settings.data_dir = cls._old_dir
        cls.storage.DB_PATH = cls._old_db
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _posts(self):
        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/posts")
        assert resp.status_code == 200, resp.text
        return {p["source"]: p for p in resp.json()["posts"]}, resp.json()["posts"]

    def test_facebook_root_post_carries_its_permalink(self):
        """主贴的 source_url 必须是 Facebook 自己的 permalink 形态，能直接打开"""
        by_source, _ = self._posts()
        root = by_source[self.fb["id"]]
        assert root["source_url"] == (
            f"https://www.facebook.com/groups/{self.GROUP_ID}/permalink/{self.MESSAGE_ID}/"
        ), "链接形态和用户手里那条对不上"

    def test_replies_get_no_url(self):
        """回复贴不给链接 —— 用户明确只要主贴，而评论锚点是 ?comment_id= 另一种形态"""
        _, posts = self._posts()
        replies = [r for p in posts for r in p["replies"]]
        assert replies, "前置条件：这个任务里得有回复贴"
        assert all(r["source_url"] == "" for r in replies), (
            f"回复贴也挂上了链接: {[r['source_url'] for r in replies]}"
        )

    def test_tweakers_root_floor_carries_its_permalink(self):
        """Tweakers 的**主题**（树根那一条）也要有「🔗 原帖」。

        形态用的是站点自己的那个：楼层头部那个日期就指向
        `/forum/list_message/<id>#<id>`（2026 真站实测）。改造前 Tweakers 没覆写
        `post_url()`，一条链接都给不出来 —— 结果页上 Tweakers 的主题卡是唯一
        没有原帖入口的卡片。
        """
        by_source, _ = self._posts()
        root = by_source[self.tw["id"]]
        mid = "77123456"
        assert root["source_url"] == (
            f"https://gathering.tweakers.net/forum/list_message/{mid}#{mid}"
        ), "链接形态和站点自己用的那个对不上"

    def test_a_source_without_a_url_shape_stays_empty(self):
        """没有覆写 post_url() 的来源仍然是空串 —— 不是拼一个错的，也不许 500。

        `group_feed` 就是这种（只服务本地 fixture，没有真实的 URL 形态）。
        """
        from app.collectors import get_collector

        assert get_collector("group_feed").post_url({"params": {}}, "123") is None

    def test_tweakers_also_refuses_a_non_web_base_url(self):
        """新增的 post_url() 必须和 Facebook 那条一样守住 `http(s)` —— 它会原样进 `<a :href>`"""
        original = dict(self.tw)
        try:
            for bad in ("javascript:alert(document.domain)//x", "//evil.example", 5, True):
                src = dict(self.tw)
                src["params"] = {**self.tw["params"], "base_url": bad}
                self.storage.save_source(src)
                by_source, _ = self._posts()
                assert by_source[self.tw["id"]]["source_url"] == "", f"{bad!r} 进了链接"
                assert by_source[self.tw["id"]]["content"], "帖子本身不该受影响"
        finally:
            self.storage.save_source(original)

    def test_url_survives_deleting_the_source(self):
        """数据源删掉后历史任务照旧能看，只是没链接 —— 不许 500、不许整页空白"""
        self.source_service.delete_source(self.fb["id"])
        try:
            by_source, _ = self._posts()
            assert by_source[self.fb["id"]]["source_url"] == ""
            assert by_source[self.fb["id"]]["content"], "帖子本身不该跟着消失"
        finally:
            # 后面的用例还要用它，按原记录写回去（create_source 会换一个新 id）
            self.storage.save_source(self.fb)

    def _with_params(self, **params):
        src = dict(self.fb)
        src["params"] = {**self.fb["params"], **params}
        self.storage.save_source(src)

    def test_a_non_web_base_url_yields_no_link(self):
        """base_url 是用户在数据源页填的，原样进 `<a :href>` —— 不是 http(s) 就不给链接。

        「javascript:alert(document.domain)//x」拼出来是一段合法脚本（后面全是注释），
        每条主贴的「🔗 原帖」都会变成它；而且链接是逐条现算的，改一次参数所有历史
        任务的链接一起被换掉。（发版前评审实测 post_url 原样返回了这串）
        """
        try:
            for bad in ("javascript:alert(document.domain)//x", "data:text/html,x",
                        "JAVASCRIPT:alert(1)", "//evil.example", "ftp://x.example"):
                self._with_params(base_url=bad)
                by_source, _ = self._posts()
                assert by_source[self.fb["id"]]["source_url"] == "", f"{bad!r} 进了链接"
        finally:
            self.storage.save_source(self.fb)

    def test_a_non_string_base_url_does_not_break_the_whole_list(self):
        """表单总是提交字符串，但手工构造的 API 请求能把 base_url 存成数字 ——
        链接逐条现算，一条抛异常整页帖子列表就 500，所有引用这个来源的历史结果页全挂"""
        try:
            for bad in (5, True, ["x"]):
                self._with_params(base_url=bad)
                by_source, _ = self._posts()        # 内含 status_code == 200 的断言
                assert by_source[self.fb["id"]]["source_url"] == ""
                assert by_source[self.fb["id"]]["content"], "帖子本身不该受影响"
        finally:
            self.storage.save_source(self.fb)

    def test_group_id_cannot_break_out_of_its_path_segment(self):
        """group_id 同样是用户填的：带 / ? # 的值要被编码在它自己那一段里"""
        try:
            self._with_params(group_id="abc/../x?y#z")
            by_source, _ = self._posts()
            url = by_source[self.fb["id"]]["source_url"]
            assert url.startswith("https://www.facebook.com/groups/"), url
            tail = url[len("https://www.facebook.com/groups/"):]
            assert "?" not in url and "#" not in url and tail.count("/") == 3, (
                f"group_id 冲出了自己那一段路径：{url}"
            )
        finally:
            self.storage.save_source(self.fb)

    def test_an_orphaned_reply_shown_at_root_gets_no_link(self):
        """丢了父贴的回复会显示在树根，但**不能**给它「🔗 原帖」链接。

        存储层的两处「提升」（`merge_duplicate_posts()` 的悬空 parent、
        `drop_empty_posts()` 丢掉空父贴时）只清父指针，`message_id` 原样留着 ——
        而那是它的 **comment_id**。出口若只凭「排在树根」就当它是主贴，拼出来的
        `/permalink/<comment_id>/` 点开既不是这条回复、也不是它的主贴。
        评审在开发库上实测：65 条带链接的主贴里有 2 条是这样。

        判据是 `reply_level`：它显示在哪儿由父指针决定，它**是什么**由层级决定。
        """
        self.storage.upsert_posts(self.fb["id"], [
            {"username": "Jean Charbon", "timestamp": "02-08-2026 23:29",
             "content": "Foutclearing eens uitvoeren? Misschien helpt dat.",
             "page_number": 2, "fingerprint": "orphanreply",
             "message_id": "2496723817470004",          # 这是评论 id，不是帖子 id
             "source": self.fb["id"], "parent_fingerprint": None, "reply_level": 1},
        ])
        try:
            resp = self.client.get(f"/api/v1/tasks/{self.task_id}/posts")
            assert resp.status_code == 200, resp.text
            roots = resp.json()["posts"]
            orphan = [p for p in roots if p["username"] == "Jean Charbon"]
            assert orphan, "父指针为空的回复应当出现在树根（build_tree 只看父指针）"
            assert orphan[0]["reply_level"] == 1, "它本身仍是回复，层级不该被抹平"
            assert orphan[0]["source_url"] == "", (
                f"给一条丢了父贴的回复拼出了主贴链接：{orphan[0]['source_url']}"
            )
        finally:
            conn = self.storage._get_conn()
            conn.execute("DELETE FROM posts WHERE source_id=? AND fingerprint=?",
                         (self.fb["id"], "orphanreply"))
            conn.commit()
            conn.close()

    def test_root_carries_the_site_comment_count_and_replies_do_not(self):
        """原帖评论数只挂主贴：结果页拿它和子树条数比，对不上标「已采 X · 原帖 Y」"""
        self.storage.upsert_posts(self.fb["id"], [
            {"username": "Dries Boink", "timestamp": "31-07-2026 17:47",
             "content": "Iemand enig idee hoe ik een account aanmaak?",
             "page_number": 2, "fingerprint": "fbroot", "message_id": self.MESSAGE_ID,
             "source": self.fb["id"], "parent_fingerprint": None, "reply_level": 0,
             "site_comment_count": 6},
        ])
        by_source, posts = self._posts()
        assert by_source[self.fb["id"]]["site_comment_count"] == 6
        replies = [r for p in posts for r in p["replies"]]
        assert replies and all(r["site_comment_count"] is None for r in replies), replies
        assert by_source[self.tw["id"]]["site_comment_count"] is None, "没读到数的主贴该是 null"

    def test_post_url_is_not_stored_in_the_posts_table(self):
        """链接现算，不落库 —— 存一份就是双写，历史数据也不会凭空长出这一列"""
        conn = self.storage._get_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
        conn.close()
        assert "url" not in cols and "source_url" not in cols, \
            f"posts 表里多了一列存链接的字段: {cols}"


class TestImagesInExportAndApiEndToEnd:
    """纯图帖走完整条出口：真 HTTP → 真 SQLite → 真图片文件 → 真 xlsx / csv"""

    @classmethod
    def setup_class(cls):
        import app.config as cfg
        from PIL import Image as PILImage

        cls.cfg = cfg
        cls.tmpdir = tempfile.mkdtemp()
        cls._old_key = cfg.settings.api_key
        cls._old_dir = cfg.settings.data_dir
        cfg.settings.api_key = ""
        cfg.settings.data_dir = cls.tmpdir

        from main import app
        from app.services import storage
        cls.storage = storage
        cls._old_db = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        media = os.path.join(cls.tmpdir, "media", "src_p")
        os.makedirs(media, exist_ok=True)
        PILImage.new("RGB", (600, 400), (20, 120, 90)).save(os.path.join(media, "shot_0.png"))

        cls.posts = [
            {"username": "koen", "timestamp": "08-08-2026 12:37", "content": "",
             "translation": "", "page_number": 1, "fingerprint": "pic",
             "source": "src_p", "parent_fingerprint": None, "reply_level": 0,
             "images": ["src_p/shot_0.png"], "image_desc": "安装检查报告，总分 88"},
            {"username": "bob", "timestamp": "08-08-2026 13:00", "content": "Zelfde hier.",
             "translation": "我也一样。", "page_number": 1, "fingerprint": "txt",
             "source": "src_p", "parent_fingerprint": "pic", "reply_level": 1},
        ]
        storage.upsert_posts("src_p", cls.posts)

        cls.task_id = "img-export-e2e"
        from app.services.orchestrator import orchestrator
        cls.orchestrator = orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "带图任务",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {"total_posts": 2, "sources": [
                {"id": "src_p", "name": "Facebook", "collector_id": "facebook_group",
                 "post_count": 2},
            ]},
        }

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_key
        cls.cfg.settings.data_dir = cls._old_dir
        cls.storage.DB_PATH = cls._old_db
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_image_only_post_survives_collection(self):
        """一个字都没有、只有一张图的帖子必须进得了库 —— 曾经在入库口被静默丢掉"""
        stored = self.storage.load_posts(["src_p"])
        assert [p["fingerprint"] for p in stored] == ["pic", "txt"]
        assert stored[0]["images"] == ["src_p/shot_0.png"]

    def test_posts_api_exposes_the_image_description(self):
        """页面靠它解释「这条没有正文的帖子凭什么得出这个结论」"""
        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/posts")
        assert resp.status_code == 200
        root = resp.json()["posts"][0]
        assert root["images"] == ["src_p/shot_0.png"]
        assert root["image_desc"] == "安装检查报告，总分 88"

    def test_xlsx_carries_the_picture(self):
        from io import BytesIO
        from openpyxl import load_workbook
        from app.services.excel_service import IMAGE_SHEET

        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export",
                               params={"format": "xlsx"})
        assert resp.status_code == 200
        wb = load_workbook(BytesIO(resp.content))
        assert wb.sheetnames == ["概览", "帖子明细", IMAGE_SHEET]
        assert len(wb["帖子明细"]._images) == 1, "明细行里没有缩略图"
        assert len(wb[IMAGE_SHEET]._images) == 1, "配图表里没有大图"

        col = {c.value: c.column for c in wb["帖子明细"][1]}
        cell = wb["帖子明细"].cell(2, col["图片描述"])
        assert "安装检查报告" in str(cell.value)
        assert cell.hyperlink and IMAGE_SHEET in cell.hyperlink.location

    def test_csv_lists_the_image_path_as_text(self):
        import csv as _csv
        from io import StringIO

        resp = self.client.get(f"/api/v1/tasks/{self.task_id}/export",
                               params={"format": "csv"})
        assert resp.status_code == 200
        rows = list(_csv.DictReader(StringIO(resp.content.decode("utf-8-sig"))))
        assert rows[0]["配图"] == "src_p/shot_0.png", "CSV 里得留个能找到原图的线索"
        assert rows[0]["图片描述"] == "安装检查报告，总分 88"
        assert rows[1]["配图"] == ""


class TestStatsTimeRangeEndToEnd:
    """统计里的时间区间必须真的是最早和最晚"""

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls._old_api_key = cfg.settings.api_key
        cfg.settings.api_key = ""

        from main import app
        from app.services import storage

        cls.tmpdir = tempfile.mkdtemp()
        cls.storage = storage
        cls._old_db_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage.init_db()
        cls.client = TestClient(app)

        # 存储顺序刻意不等于时间顺序 —— 信息流按时间倒序渲染，增量又往后追加，
        # 真实落盘文件本来就是乱的。最早的那条排在最后，最晚的排在中间。
        # 日期还跨了月：01-07 和 28-06 按落盘的 dd-mm-yyyy 字符串比大小会得出
        # 「7 月 1 日早于 6 月 28 日」，所以必须先归一化成 ISO 再比。
        posts = [
            {"username": "中", "timestamp": "30-06-2026 12:00", "content": "中间",
             "fingerprint": "t2", "source": "src_x", "page_number": 1,
             "parent_fingerprint": None, "reply_level": 0},
            {"username": "晚", "timestamp": "01-07-2026 08:00", "content": "最晚",
             "fingerprint": "t3", "source": "src_x", "page_number": 1,
             "parent_fingerprint": None, "reply_level": 0},
            {"username": "无时间", "timestamp": "", "content": "时间提取失败",
             "fingerprint": "t4", "source": "src_x", "page_number": 1,
             "parent_fingerprint": None, "reply_level": 0},
            {"username": "早", "timestamp": "28-06-2026 09:00", "content": "最早",
             "fingerprint": "t1", "source": "src_x", "page_number": 1,
             "parent_fingerprint": None, "reply_level": 0},
        ]
        # seq 按插入顺序给，读回来仍是这个乱序 —— 排序必须由 /stats 自己做
        storage.upsert_posts("src_x", posts)

        cls.task_id = "range-e2e"
        from app.services.orchestrator import orchestrator
        orchestrator.tasks[cls.task_id] = {
            "id": cls.task_id, "status": "completed", "description": "时间区间",
            "plan": [], "logs": [], "progress": 1.0, "current_step": None,
            "result": {
                "total_posts": len(posts),
                "sources": [{"id": "src_x", "name": "乱序来源", "collector_id": "group_feed",
                             "post_count": len(posts)}],
            },
        }
        cls.orchestrator = orchestrator

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_api_key
        cls.storage.DB_PATH = cls._old_db_path
        cls.orchestrator.tasks.pop(cls.task_id, None)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_time_range_is_min_and_max_not_first_and_last(self):
        """取的是最早/最晚，不是数组的首尾。

        实测线上出现过 time_range_start=2026-07-28、time_range_end=2026-07-10，
        开始比结束晚了 18 天 —— 因为直接取了 timestamps[0] 和 timestamps[-1]，
        而存储顺序是抓取顺序，与时间早晚无关。空时间戳不参与（真实数据里有一批
        旧版提取器留下的空时间，让它当上「最早」会把整个区间拉垮）。
        """
        stats = self.client.get(f"/api/v1/tasks/{self.task_id}/stats").json()

        assert stats["time_range_start"] == "2026-06-28 09:00"
        assert stats["time_range_end"] == "2026-07-01 08:00"
        assert stats["time_range_start"] <= stats["time_range_end"], "开始晚于结束"


class TestMediaEndpointEndToEnd:
    """正文图回读端点 —— 鉴权 + 路径穿越"""

    @classmethod
    def setup_class(cls):
        import app.config as cfg

        cls.cfg = cfg
        cls.tmpdir = tempfile.mkdtemp()
        cls._old_dir = cfg.settings.data_dir
        cls._old_key = cfg.settings.api_key
        cfg.settings.data_dir = cls.tmpdir
        cfg.settings.api_key = ""

        os.makedirs(os.path.join(cls.tmpdir, "media", "src_a"), exist_ok=True)
        with open(os.path.join(cls.tmpdir, "media", "src_a", "pic.png"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        # 采集器落图的目录旁边就是明文存着 LLA API Key 的 config.json
        with open(os.path.join(cls.tmpdir, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"api_key": "sk-must-not-leak"}, f)

        from main import app
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.data_dir = cls._old_dir
        cls.cfg.settings.api_key = cls._old_key
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_serves_existing_image(self):
        resp = self.client.get("/api/v1/media/src_a/pic.png")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")

    def test_missing_file_is_404(self):
        assert self.client.get("/api/v1/media/src_a/nope.png").status_code == 404

    def test_path_traversal_is_blocked(self):
        """`../../config.json` 里是明文 LLM API Key —— 穿越出去就等于把它送人。

        不回 403 而回 404：403 等于承认那个位置有东西。

        **必须用 URL 编码形式**：裸的 `../` 会被 httpx 在客户端就规范化掉，根本到不了
        端点，拿它当用例等于什么都没测（实测把校验整段禁用，裸 `../` 那版照样绿）。
        `%2e%2e%2f` 才会被框架解码后原样交到 rel_path 手里。
        """
        for evil in (
            "%2e%2e%2fconfig.json",
            "..%2fconfig.json",
            "src_a%2f..%2f..%2fconfig.json",
            "src_a/..%2f..%2fconfig.json",
        ):
            resp = self.client.get(f"/api/v1/media/{evil}")
            assert resp.status_code == 404, evil
            assert b"sk-must-not-leak" not in resp.content, evil

    def test_requires_api_key_when_configured(self):
        """<img> 带不了请求头，所以必须支持 ?api_key=（与 SSE 同一个口子）"""
        self.cfg.settings.api_key = "s3cr3t"
        try:
            assert self.client.get("/api/v1/media/src_a/pic.png").status_code == 401
            ok = self.client.get("/api/v1/media/src_a/pic.png?api_key=s3cr3t")
            assert ok.status_code == 200
        finally:
            self.cfg.settings.api_key = ""


class TestApiKeyAuthEndToEnd:
    """共享密钥认证 — 未配置时放行，配置后拦截"""

    @classmethod
    def setup_class(cls):
        cls.tmpdir = tempfile.mkdtemp()
        import app.config as cfg
        cls.cfg = cfg
        cls._old_key = cfg.settings.api_key

        from main import app
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls):
        cls.cfg.settings.api_key = cls._old_key
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_open_when_key_not_configured(self):
        """漏配环境变量不该让既有部署整个不可用"""
        self.cfg.settings.api_key = ""
        assert self.client.get("/api/v1/tasks").status_code == 200

    def test_rejected_without_key(self):
        self.cfg.settings.api_key = "s3cr3t"
        try:
            resp = self.client.get("/api/v1/tasks")
            assert resp.status_code == 401
            resp = self.client.get("/api/v1/tasks", headers={"X-API-Key": "wrong"})
            assert resp.status_code == 401
        finally:
            self.cfg.settings.api_key = ""

    def test_accepted_with_key(self):
        self.cfg.settings.api_key = "s3cr3t"
        try:
            resp = self.client.get("/api/v1/tasks", headers={"X-API-Key": "s3cr3t"})
            assert resp.status_code == 200
            # 浏览器 EventSource 无法自定义请求头，只能走 query
            resp = self.client.get("/api/v1/tasks?api_key=s3cr3t")
            assert resp.status_code == 200
        finally:
            self.cfg.settings.api_key = ""

    def test_health_and_root_stay_public(self):
        """健康检查被监控系统调用，不能要求密钥"""
        self.cfg.settings.api_key = "s3cr3t"
        try:
            assert self.client.get("/api/health").status_code == 200
            assert self.client.get("/").status_code == 200
        finally:
            self.cfg.settings.api_key = ""

    def test_write_endpoints_also_protected(self):
        """覆写 LLM 密钥、植入定时任务这类写操作必须一并挡住"""
        self.cfg.settings.api_key = "s3cr3t"
        try:
            resp = self.client.post("/api/v1/config", json={
                "api_key": "sk-hijack", "base_url": "https://evil.test", "model_name": "x",
            })
            assert resp.status_code == 401
            resp = self.client.post("/api/v1/schedules", json={
                "description": "植入的定时任务", "interval": "daily", "time": "09:00",
            })
            assert resp.status_code == 401
        finally:
            self.cfg.settings.api_key = ""


class TestFrontendHostingEndToEnd:
    """便携包里后端自己发布前端：单端口同时供 /api/* 和页面，不引入 nginx。

    真 TestClient 请求、真文件落盘 —— 这条路只在打包态生效，源码态 web/ 不存在。
    """

    def setup_method(self):
        from fastapi import FastAPI

        self.tmpdir = tempfile.mkdtemp()

        # **必须在 import main 之前重定向**。main 会拉起 orchestrator，而它在 import 期
        # 就 init_db() + 加载历史任务；storage.DB_PATH 又是 import 时算好的常量。
        # 本类若碰巧是进程里第一个 import main 的，这两样就绑到真实的
        # backend/data/hyxi.db 上了 —— 实测因此往生产库里写进过 3 条测试任务
        import app.config as cfg
        import app.services.storage as storage_module
        self.cfg, self.storage = cfg, storage_module
        self._old = (cfg.settings.data_dir, cfg.settings.tasks_dir,
                     cfg.settings.exports_dir, storage_module.DB_PATH)
        cfg.settings.data_dir = self.tmpdir
        cfg.settings.tasks_dir = os.path.join(self.tmpdir, "tasks")
        cfg.settings.exports_dir = os.path.join(self.tmpdir, "exports")
        storage_module.DB_PATH = os.path.join(self.tmpdir, "hyxi.db")

        self.web = os.path.join(self.tmpdir, "web")
        os.makedirs(os.path.join(self.web, "assets"))
        with open(os.path.join(self.web, "index.html"), "w", encoding="utf-8") as f:
            f.write("<!doctype html><title>HYXi</title><div id=app></div>")
        with open(os.path.join(self.web, "assets", "index-abc123.js"), "w", encoding="utf-8") as f:
            f.write("console.log('bundle')")
        with open(os.path.join(self.web, "favicon.ico"), "wb") as f:
            f.write(bytes([0, 0, 1, 0]))
        # 包外的东西：路径穿越拿到它就等于泄露数据库和明文密钥
        with open(os.path.join(self.tmpdir, "secret.txt"), "w", encoding="utf-8") as f:
            f.write("TWEAKERS_SECRET_KEY=leaked")

        from main import mount_frontend

        self.app = FastAPI()

        @self.app.get("/api/health")
        async def _health():
            return {"status": "ok"}

        assert mount_frontend(self.app, self.web) is True
        self.client = TestClient(self.app)

    def teardown_method(self):
        (self.cfg.settings.data_dir, self.cfg.settings.tasks_dir,
         self.cfg.settings.exports_dir, self.storage.DB_PATH) = self._old
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_index_is_served_at_root(self):
        resp = self.client.get("/")
        assert resp.status_code == 200
        assert "<div id=app>" in resp.text

    def test_deep_link_falls_back_to_index(self):
        """路由是 createWebHistory()，/tasks/{id}/progress 直接刷新必须落到 SPA 上，
        否则用户刷新一次就是白屏"""
        resp = self.client.get("/tasks/abc-123/progress")
        assert resp.status_code == 200
        assert "<div id=app>" in resp.text

    def test_unknown_api_path_stays_a_json_404(self):
        """catch-all 谁都接得住，但打错的接口地址必须还是 404 JSON。

        回一张 HTML 页面会让排查彻底走偏：调用方拿到 200 + <!doctype html>，
        报出来的是 JSON 解析失败，跟真实原因毫无关系。
        """
        resp = self.client.get("/api/v1/definitely-not-a-route")
        assert resp.status_code == 404
        assert "html" not in resp.headers.get("content-type", "")

    def test_existing_api_route_is_not_shadowed(self):
        resp = self.client.get("/api/health")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}

    def test_real_file_beats_the_fallback(self):
        resp = self.client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.content == bytes([0, 0, 1, 0]), "favicon 被 index.html 顶掉了"

    def test_bundle_is_served_from_assets(self):
        resp = self.client.get("/assets/index-abc123.js")
        assert resp.status_code == 200 and "bundle" in resp.text

    def test_path_traversal_cannot_escape_the_web_dir(self):
        """web 目录之外就是 data/ 和 .env —— 裸的 ../ 通常在客户端就被规范化掉，
        %2e%2e%2f 会被框架解码后原样送进来"""
        for attack in ("../secret.txt", "%2e%2e%2fsecret.txt", "..%2Fsecret.txt"):
            resp = self.client.get(f"/{attack}")
            assert "leaked" not in resp.text, f"{attack} 读到了 web 目录外的文件"

    def test_without_web_dir_it_stays_out_of_the_way(self):
        """源码开发态没有 web/，整段跳过 —— start.ps1 那条路径不受影响"""
        from fastapi import FastAPI
        from main import mount_frontend

        assert mount_frontend(FastAPI(), os.path.join(self.tmpdir, "nope")) is False


class TestDevModePointsToTheFrontendEndToEnd:
    """源码开发态的 8000 只有 API，有人拿浏览器打开页面路径时必须告诉他页面在哪。

    用户实测：打开 `http://127.0.0.1:8000/tasks`，开发态回的是 `404 {"detail":"Not Found"}`，
    没有任何一个字说「页面在 5173」。同一个端口号在便携包里是同时供页面和 API 的，
    用户去访问 8000 是完全合理的行为。

    打到**真实的 `main.app`**（uvicorn 起的就是它），不另造一个应用：要钉住的正是
    开发态启动路径上那一段 `if not mount_frontend(...)`。
    """

    @classmethod
    def setup_class(cls):
        import pytest
        import app.config as cfg
        import app.services.storage as storage_module

        if os.path.isfile(os.path.join(cfg.settings.project_root, "web", "index.html")):
            pytest.skip("项目根有 web/，main.app 走的是便携包那种单端口形态，不是开发态")

        # 同 TestFrontendHostingEndToEnd：import main 之前先把库重定向走
        cls.tmpdir = tempfile.mkdtemp()
        cls.cfg, cls.storage = cfg, storage_module
        cls._old = (cfg.settings.api_key, cfg.settings.data_dir, storage_module.DB_PATH)
        cfg.settings.api_key = ""
        cfg.settings.data_dir = cls.tmpdir
        storage_module.DB_PATH = os.path.join(cls.tmpdir, "hyxi.db")
        storage_module.init_db()

        from main import app
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls):
        if hasattr(cls, "_old"):
            (cls.cfg.settings.api_key, cls.cfg.settings.data_dir, cls.storage.DB_PATH) = cls._old
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_page_path_says_where_the_pages_are(self):
        resp = self.client.get("/tasks")
        assert resp.status_code == 404, "状态码仍然该是 404 —— 这里确实没有这个页面"
        assert "5173" in resp.json()["detail"], (
            f"开发态打开页面路径只回了一句 {resp.json()!r}，没告诉用户页面在 5173"
        )

    def test_deep_link_says_it_too(self):
        """结果页那种深链是最常被人直接粘贴过来的"""
        resp = self.client.get("/tasks/abc-123/results")
        assert resp.status_code == 404 and "5173" in resp.json()["detail"]

    def test_unknown_api_path_is_still_a_plain_404(self):
        """接口调用方要的是干净的 404，指路的话对它是噪音 —— 只对页面路径说"""
        resp = self.client.get("/api/v1/definitely-not-a-route")
        assert resp.status_code == 404
        assert "5173" not in resp.json()["detail"], "打错的接口地址也被指去了前端"

    def test_real_api_404_keeps_its_own_detail(self):
        """接口自己抛的 404 带着业务含义（「任务不存在」），不能被统一文案盖掉"""
        resp = self.client.get("/api/v1/tasks/definitely-not-a-task")
        assert resp.status_code == 404
        assert "5173" not in resp.json()["detail"]

    def test_root_and_docs_are_untouched(self):
        assert self.client.get("/").json()["service"] == "HYXi 舆情分析 API"
        assert self.client.get("/api/health").status_code == 200


class TestBackfillTranslationEndToEnd:
    """结果页补译：只翻「有正文、没有可用译文」的，分块落库，同一来源不翻两遍。
    真 HTTP 模型替身（llm_site）、真 SQLite、真路由函数与后台作业。
    规格见 docs/features/results-backfill-translation.md"""

    SRC = "src_bt"

    def setup_method(self):
        import app.config as cfg
        from app.services import storage
        self.cfg = cfg
        self.storage = storage
        self.tmpdir = tempfile.mkdtemp()
        self._old = (cfg.settings.api_key, cfg.settings.data_dir, cfg.settings.exports_dir, storage.DB_PATH)
        cfg.settings.api_key = ""
        cfg.settings.data_dir = self.tmpdir
        # exports_dir 是类定义时算好的常量：只改 data_dir 的话流水线的 generate_excel 会写进真实目录
        cfg.settings.exports_dir = os.path.join(self.tmpdir, "exports")
        os.makedirs(cfg.settings.exports_dir, exist_ok=True)
        storage.DB_PATH = os.path.join(self.tmpdir, "hyxi.db")
        storage.init_db()

        from main import app
        from app.services.orchestrator import orchestrator
        self.client = TestClient(app)
        self.orchestrator = orchestrator
        # 流水线的「翻译已有数据」按已启用来源读帖子，来源得注册着
        storage.save_source({
            "id": self.SRC, "name": "补译来源", "collector_id": "group_feed",
            "params": {"group_id": "g1", "base_url": "http://127.0.0.1:1"},
            "enabled": True, "created_at": "2026-09-15T09:00:00",
        })
        self.task_id = "backfill-e2e"
        self._task(self.task_id, "completed")

    def teardown_method(self):
        for tid in [t for t in self.orchestrator.tasks if t.startswith("backfill-")]:
            self.orchestrator.tasks.pop(tid, None)
        # 用例失败时作业可能没走到释放那一步；进程里的单例不清掉会连累同一 worker 的下一条
        self.orchestrator._translation_claims.pop(self.SRC, None)
        (self.cfg.settings.api_key, self.cfg.settings.data_dir,
         self.cfg.settings.exports_dir, self.storage.DB_PATH) = self._old
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _task(self, task_id, status):
        from datetime import datetime
        self.orchestrator.tasks[task_id] = {
            "id": task_id, "status": status, "description": "补译", "plan": [], "logs": [],
            # 缺 created_at 的话流水线每次落库都会记一条「NOT NULL constraint failed」
            "progress": 1.0, "current_step": None, "force_full": False, "created_at": datetime.now(),
            "result": {"total_posts": 0, "sources": [
                {"id": self.SRC, "name": "补译来源", "collector_id": "group_feed", "post_count": 0}]},
        }

    def _seed(self, rows):
        """rows: (fingerprint, content, translation)。都带 message_id —— 否则空正文的会在入库口被丢掉"""
        self.storage.upsert_posts(self.SRC, [
            {"username": f"u_{fp}", "timestamp": "15-09-2026 10:00", "content": content,
             "translation": translation, "page_number": 1, "fingerprint": fp, "source": self.SRC,
             "message_id": f"m_{fp}", "parent_fingerprint": None, "reply_level": 0,
             **({"_processed": {"translated": True}} if translation else {})}
            for fp, content, translation in rows
        ])

    def _by_fp(self):
        return {p["fingerprint"]: p for p in self.storage.load_posts([self.SRC])}

    def _llm_site(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))
        import llm_site
        return llm_site

    def _configure_llm(self, url):
        self.storage.set_app_config("llm", {"api_key": "sk-test", "base_url": url, "model_name": "test-model"})

    async def _wait_released(self, timeout=60):
        for _ in range(int(timeout / 0.05)):
            if self.orchestrator.translation_owner([self.SRC]) is None:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("补译作业没有结束（来源一直被占着）")

    async def _wait_blocked(self, server, n=1, timeout=30):
        for _ in range(int(timeout / 0.05)):
            if len(server.blocked) >= n:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"等不到第 {n} 个被卡住的翻译请求：{server.blocked}")

    # ===== R1 待补译条数 =====

    def test_stats_count_exactly_the_posts_that_have_no_usable_translation(self):
        """提示条上的 N 必须等于页面上「（尚未翻译）」加失败标记的条数 —— 两处各算各的，迟早对不上。
        没正文的翻不了；译文只有空白的算没翻；译文是失败标记的也算（G2：2026-09-15 补译完真实数据里出了 5 条）"""
        self._seed([
            ("a", "Nog niet vertaald", ""),
            ("b", "Ook nog niet", ""),
            ("g", "Vertaling is alleen spaties", "   "),
            ("f", "Mislukt", "[翻译解析失败] Mislukt"),
            ("c", "Al vertaald", "已经翻译"),
            ("d", "", ""),
            ("e", "   ", ""),
        ])
        stats = self.client.get(f"/api/v1/tasks/{self.task_id}/stats").json()
        assert stats["untranslated_count"] == 4, stats
        assert stats["translating"] is False, stats

    # ===== R7 单条重译与失败标记 =====

    def _translate(self, posts):
        from app.services.translator_service import TranslatorService
        from app.services.progress_manager import ProgressManager
        return asyncio.new_event_loop().run_until_complete(
            TranslatorService.execute("backfill-unit", posts, {}, ProgressManager()))["posts"]

    def test_a_two_character_retry_translation_is_accepted(self):
        """「Jup」→「是的」只有 2 个字。单条重译以前要求译文超过 2 个字才算成功，
        于是真实数据里它挂着「[翻译解析失败] Jup」—— 这条回复明明翻出来了"""
        server = self._llm_site().LLMSite(batch_empty=True, retry_reply="是的")
        with server as url:
            self._configure_llm(url)
            out = self._translate([{"source": self.SRC, "fingerprint": "j", "content": "Jup"}])
        assert out[0]["translation"] == "是的", out[0]

    def test_a_long_post_is_translated_on_retry_by_a_reasoning_model(self):
        """2026-09-15 实测 deepseek-flash：单条重译给 2048 token 时 2048 个全是 reasoning、content 为空，
        长帖因此永远挂着失败标记。替身按实测复刻：预算不到 2049 只推理不出字"""
        from app.services.translator_service import is_failed_translation
        server = self._llm_site().LLMSite(batch_empty=True, reasoning_floor=2049)
        with server as url:
            self._configure_llm(url)
            out = self._translate([{"source": self.SRC, "fingerprint": "l", "content": "Lange tekst. " * 200}])
        assert out[0]["translation"] and not is_failed_translation(out[0]["translation"]), out[0]["translation"][:60]

    def test_a_model_that_rejects_the_bigger_retry_budget_still_gets_the_old_one(self):
        """评审发现：预算是为推理模型调大的，而模型由用户自己配。8K 上下文的模型要求「输入 + max_tokens ≤ 上下文」，
        超了整个请求 400 —— 原来 2048 能重译成的帖子会一律挂失败标记。被 400 拒绝就退回旧预算再试一次"""
        from app.services.translator_service import is_failed_translation
        server = self._llm_site().LLMSite(batch_empty=True, max_tokens_limit=4096)
        with server as url:
            self._configure_llm(url)
            out = self._translate([{"source": self.SRC, "fingerprint": "k", "content": "Korte tekst"}])
        assert out[0]["translation"] and not is_failed_translation(out[0]["translation"]), out[0]["translation"]

    def test_a_pipeline_run_retries_failed_translations_and_leaves_good_ones_alone(self):
        """失败标记以前被当成「已翻译」，任务怎么跑都不会重翻。补译按钮和任务的增量判据必须是同一个答案"""
        self._seed([("x", "Mislukt bericht", "[翻译解析失败] Mislukt bericht"), ("c", "Al vertaald", "已经翻译")])
        server = self._llm_site().LLMSite()
        with server as url:
            self._configure_llm(url)
            self.orchestrator.create_task("backfill-rerun", "重新翻译已有数据")
            asyncio.new_event_loop().run_until_complete(self.orchestrator.execute_task("backfill-rerun"))
        task = self.orchestrator.get_task("backfill-rerun")
        assert task["status"] == "completed", task.get("error_message")
        assert server.translated == [1], f"应该只重翻失败的那 1 条：{server.translated}"
        posts = self._by_fp()
        assert posts["x"]["translation"] == "译文1", posts["x"]
        assert posts["c"]["translation"] == "已经翻译"

    # ===== R2–R4 补译接口、后台作业、进度流 =====

    def test_unknown_task_is_404(self):
        assert self.client.post("/api/v1/tasks/backfill-nope/translate").status_code == 404

    def test_a_task_that_is_still_running_is_refused(self):
        """它自己的翻译步骤会处理；而且结果里还没记下来源，按全部已注册来源读会翻到别的来源头上"""
        self._seed([("a", "Nog niet vertaald", "")])
        self._task("backfill-running", "running")
        resp = self.client.post("/api/v1/tasks/backfill-running/translate")
        assert resp.status_code == 409, resp.text
        assert self.orchestrator.translation_owner([self.SRC]) is None, "拒绝了还占着来源"

    def test_a_task_that_did_not_complete_is_refused(self):
        """评审发现：result 只在任务成功时写，失败 / 取消的任务没记下来源，load_task_posts 会退回「全部已注册来源」——
        在它的结果页补译，等于把所有来源的未译帖子一起送去翻（按条付费）。
        前端只给已完成的任务显示提示条，接口必须是同一个口径"""
        self._seed([("a", "Nog niet vertaald", "")])
        server = self._llm_site().LLMSite()
        with server as url:
            self._configure_llm(url)
            for status in ("failed", "cancelled"):
                tid = f"backfill-{status}"
                self._task(tid, status)
                self.orchestrator.tasks[tid]["result"] = None      # 真实的失败 / 取消任务就是这样
                resp = self.client.post(f"/api/v1/tasks/{tid}/translate")
                assert resp.status_code == 409, (status, resp.text)
                assert self.orchestrator.translation_owner([self.SRC]) is None, f"{status}：拒绝了还占着来源"

    def test_nothing_to_translate_completes_without_touching_the_model(self):
        self._seed([("c", "Al vertaald", "已经翻译"), ("d", "", "")])
        body = self.client.post(f"/api/v1/tasks/{self.task_id}/translate").json()
        assert body["status"] == "completed" and body["pending_count"] == 0, body
        assert self.orchestrator.translation_owner([self.SRC]) is None

    def test_without_model_config_it_is_refused_before_anything_starts(self):
        """先回「已开始」再在后台静默失败的话，页面会一直显示「正在补译」"""
        self._seed([("a", "Nog niet vertaald", "")])
        resp = self.client.post(f"/api/v1/tasks/{self.task_id}/translate")
        assert resp.status_code == 400, resp.text
        assert "请先配置 LLM API" in resp.json()["detail"]
        assert self.orchestrator.translation_owner([self.SRC]) is None

    def test_claims_are_all_or_nothing_and_released_only_by_their_owner(self):
        """占用表是「同一批帖子不翻两遍」的闸门。接口那道先查只挡得住按钮，
        直接起作业的入口（以及任务里的翻译步骤）靠的是这里：占不齐就一个都不占，别人放不掉我的"""
        o = self.orchestrator
        try:
            assert o.claim_translation([self.SRC], "backfill:x") is None
            assert o.claim_translation(["src_other", self.SRC], "task:y") == "backfill:x", "被占着还占上了"
            assert o.translation_owner(["src_other"]) is None, "占不上时不许占掉一部分"
            o.release_translation([self.SRC], "task:y")
            assert o.translation_owner([self.SRC]) == "backfill:x", "别人把我的占用放掉了"
            o.release_translation([self.SRC], "backfill:x")
            assert o.translation_owner([self.SRC]) is None
        finally:
            o._translation_claims.pop("src_other", None)

    def test_a_source_that_is_already_being_translated_is_not_translated_again(self):
        """双击、共用这个来源的另一个任务也点了 —— 第二次必须认出来，不另起一轮付费翻译"""
        self._seed([("a", "Nog niet vertaald", "")])
        server = self._llm_site().LLMSite()
        with server as url:
            self._configure_llm(url)
            assert self.orchestrator.claim_translation([self.SRC], "backfill:backfill-other") is None
            try:
                body = self.client.post(f"/api/v1/tasks/{self.task_id}/translate").json()
            finally:
                self.orchestrator.release_translation([self.SRC], "backfill:backfill-other")
        assert body["status"] == "running", body
        assert server.translated == [], f"来源被占着还发了翻译请求：{server.translated}"

    def test_backfill_translates_only_what_is_missing_and_reports_on_its_own_channel(self):
        """整条链路：路由 → 后台作业 → 真 HTTP 模型 → 落库 → 进度流。
        已有译文的一个字都不能动（动了就是重复付费、还冲掉好译文）。
        翻译事件不许出现在 task_id 频道上 —— 那是任务进度流和舆情流共用的，
        同一任务的舆情页正在分析时，翻译进度会串到舆情进度条上"""
        from app.routers.results import trigger_backfill_translation, translation_events
        from app.services.orchestrator import translation_channel
        from app.services.progress_manager import progress_manager
        self._seed([
            ("a", "Nog niet vertaald", ""), ("b", "Ook nog niet", ""), ("g", "Spaties", "   "),
            ("f", "Mislukt", "[翻译解析失败] Mislukt"),
            ("c", "Al vertaald", "已经翻译"), ("d", "", ""),
        ])
        server = self._llm_site().LLMSite()
        events, shared = [], []
        with server as url:
            self._configure_llm(url)

            async def main():
                shared_queue = progress_manager.subscribe(self.task_id)
                resp = await translation_events(self.task_id)

                async def consume():
                    async for chunk in resp.body_iterator:
                        lines = chunk.splitlines()
                        if len(lines) >= 2 and lines[0].startswith("event: "):
                            events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))

                consumer = asyncio.ensure_future(consume())
                # 订阅在生成器第一次被拉动时才建立，抢在作业开跑之前
                for _ in range(200):
                    if progress_manager.subscribers.get(translation_channel(self.task_id)):
                        break
                    await asyncio.sleep(0.01)
                body = await trigger_backfill_translation(self.task_id)
                assert body["status"] == "started" and body["pending_count"] == 4, body
                await asyncio.wait_for(consumer, timeout=30)
                await self._wait_released()
                while not shared_queue.empty():
                    shared.append(shared_queue.get_nowait()["event"])
                progress_manager.unsubscribe(self.task_id, shared_queue)

            asyncio.new_event_loop().run_until_complete(main())

        assert sum(server.translated) == 4, f"发给模型的条数不对：{server.translated}"
        posts = self._by_fp()
        for fp in ("a", "b", "g", "f"):
            assert posts[fp]["translation"].startswith("译文"), posts[fp]
            assert (posts[fp].get("_processed") or {}).get("translated") is True, posts[fp]
        assert posts["c"]["translation"] == "已经翻译", "已有译文被改了"
        assert posts["d"]["translation"] == "", "没正文的不该翻"

        assert [e for e, _ in events][-1] == "translation_complete", events
        done = [d["done"] for e, d in events if e == "translation_progress"]
        assert done and done == sorted(done) and done[-1] == 4, done
        assert events[-1][1]["status"] == "completed" and events[-1][1]["translated"] == 4, events[-1]
        assert shared == [], f"翻译事件串到了任务进度 / 舆情共用的频道：{shared}"

        stats = self.client.get(f"/api/v1/tasks/{self.task_id}/stats").json()
        assert stats["untranslated_count"] == 0 and stats["translating"] is False, stats

    def test_progress_is_saved_chunk_by_chunk(self):
        """几百条要翻十几分钟。整批翻完才写库的话，中途关掉应用这一整批钱就白花了
        （2026-09-15 便携包中途被关过一次）。卡住第二块，第一块必须已经在库里"""
        import app.services.orchestrator as orch_module
        from app.routers.results import trigger_backfill_translation
        self._seed([("a", "Een", ""), ("b", "Twee", ""), ("c", "Drie", "")])
        gate = threading.Event()
        server = self._llm_site().LLMSite(translate_gate=gate, gate_after=1)
        old_chunk, mid_run = orch_module.BACKFILL_CHUNK_SIZE, {}
        orch_module.BACKFILL_CHUNK_SIZE = 2
        try:
            with server as url:
                self._configure_llm(url)

                async def main():
                    body = await trigger_backfill_translation(self.task_id)
                    assert body["status"] == "started", body
                    await self._wait_blocked(server)          # 第二块的请求卡在替身模型里
                    mid_run.update({fp: p["translation"] for fp, p in self._by_fp().items()})
                    gate.set()
                    await self._wait_released()

                asyncio.new_event_loop().run_until_complete(main())
        finally:
            gate.set()
            orch_module.BACKFILL_CHUNK_SIZE = old_chunk

        assert mid_run["a"] and mid_run["b"], f"第一块翻完没落库：{mid_run}"
        assert mid_run["c"] == "", f"第二块还卡着就有译文了：{mid_run}"
        assert all(p["translation"] for p in self._by_fp().values()), "放行之后没翻完"

    def test_a_failed_run_says_so_and_frees_the_source(self):
        """作业失败时页面必须收到明确的失败，来源也得放开 —— 否则按钮永远停在「正在补译」"""
        from app.services.orchestrator import translation_channel
        from app.services.progress_manager import progress_manager
        self._seed([("a", "Nog niet vertaald", "")])
        # 不配模型、绕过接口那道 400 直接起作业：译者在作业里才发现没配置，走作业自己的失败路径
        pending = [p for p in self.storage.load_posts([self.SRC]) if p["fingerprint"] == "a"]
        got = []

        async def main():
            channel = translation_channel(self.task_id)
            queue = progress_manager.subscribe(channel)
            try:
                assert self.orchestrator.start_backfill_translation(self.task_id, [self.SRC], pending) is None
                await self._wait_released()
                while not queue.empty():
                    got.append(queue.get_nowait())
            finally:
                progress_manager.unsubscribe(channel, queue)

        asyncio.new_event_loop().run_until_complete(main())
        complete = [m["data"] for m in got if m["event"] == "translation_complete"]
        assert complete and complete[-1]["status"] == "failed", got
        assert "请先配置 LLM API" in complete[-1]["error"], complete[-1]

    def test_a_run_that_translates_nothing_fails_and_says_why(self):
        """评审发现：模型每次都报错（API Key 失效、模型名写错）时，译者把异常吃掉、写成「[翻译失败]」标记，
        不抛 —— 作业照样报 completed，页面上 N 纹丝不动、没有任何提示，用户只会一遍遍重点。
        替身让每个请求都回 400：批量、单条重译、退回旧预算那一次，全部失败"""
        from app.routers.results import trigger_backfill_translation
        from app.services.orchestrator import translation_channel
        from app.services.progress_manager import progress_manager
        self._seed([("a", "Nog niet vertaald", "")])
        server = self._llm_site().LLMSite(max_tokens_limit=1)
        got = []
        with server as url:
            self._configure_llm(url)

            async def main():
                channel = translation_channel(self.task_id)
                queue = progress_manager.subscribe(channel)
                try:
                    body = await trigger_backfill_translation(self.task_id)
                    assert body["status"] == "started", body
                    await self._wait_released()
                    while not queue.empty():
                        got.append(queue.get_nowait())
                finally:
                    progress_manager.unsubscribe(channel, queue)

            asyncio.new_event_loop().run_until_complete(main())
        complete = [m["data"] for m in got if m["event"] == "translation_complete"]
        assert complete and complete[-1]["status"] == "failed", complete
        assert complete[-1]["translated"] == 0, complete[-1]
        # 原因要带上模型给的那句，不然用户不知道该去改哪
        assert "max_tokens exceeds the limit of 1" in complete[-1]["error"], complete[-1]

    # ===== R5 与任务里的翻译步骤互斥 =====

    def test_a_pipeline_translate_step_waits_for_the_backfill_and_pays_once(self):
        """补译在跑时，一个带翻译步骤的任务（比如定时任务）开始翻同一来源：
        它得等补译结束、回库看过译文再算待翻译。否则两边各翻一遍，同一批帖子付两次钱"""
        from app.routers.results import trigger_backfill_translation
        self._seed([("a", "Een", ""), ("b", "Twee", ""), ("c", "Al vertaald", "已经翻译")])
        gate = threading.Event()
        server = self._llm_site().LLMSite(translate_gate=gate)
        pipeline_id = "backfill-pipeline"
        try:
            with server as url:
                self._configure_llm(url)

                async def main():
                    body = await trigger_backfill_translation(self.task_id)
                    assert body["status"] == "started", body
                    await self._wait_blocked(server)           # 补译的请求卡住，来源占着
                    self.orchestrator.create_task(pipeline_id, "重新翻译已有数据")
                    run = asyncio.ensure_future(self.orchestrator.execute_task(pipeline_id))
                    # 等到任务要么在等补译、要么自己也发出了翻译请求（后者就是要挡的故障）
                    for _ in range(600):
                        logs = " ".join(l["message"] for l in self.orchestrator.get_task(pipeline_id)["logs"])
                        if "正在补译" in logs or len(server.blocked) >= 2:
                            break
                        await asyncio.sleep(0.05)
                    gate.set()
                    await asyncio.wait_for(run, timeout=60)
                    await self._wait_released()

                asyncio.new_event_loop().run_until_complete(main())
        finally:
            gate.set()

        task = self.orchestrator.get_task(pipeline_id)
        assert task["status"] == "completed", task.get("error_message")
        assert sum(server.translated) == 2, f"同一批帖子被翻了不止一遍：{server.translated}"
        assert all(p["translation"] for p in self._by_fp().values() if p["content"])

    def test_a_pipeline_that_read_its_posts_before_a_backfill_finished_does_not_pay_again(self):
        """评审发现：任务在前面的步骤（采集、舆情）就把帖子读进了内存，等它走到翻译时别人的补译已经翻完、放掉了来源 ——
        它一次就占到来源、没等过。只在「等过」时回库刷新的话，手里那份还是空译文：再翻一遍付两次钱，
        重译失败还会把补译落库的好译文冲成失败标记"""
        from app.routers.results import trigger_backfill_translation
        self._seed([("a", "Een", ""), ("b", "Twee", "")])
        gate = threading.Event()
        server = self._llm_site().LLMSite(sentiment_gate=gate)
        pipeline_id = "backfill-stale"
        try:
            with server as url:
                self._configure_llm(url)

                async def main():
                    self.orchestrator.create_task(pipeline_id, "先分析舆情再翻译已有数据")
                    run = asyncio.ensure_future(self.orchestrator.execute_task(pipeline_id))
                    # 任务卡在舆情步骤里：a、b 已经以「没译文」的样子读进内存，翻译步骤还没开始
                    for _ in range(600):
                        if server.sentiment_blocked:
                            break
                        await asyncio.sleep(0.05)
                    assert server.sentiment_blocked, "任务的舆情请求一直没发出来"
                    body = await trigger_backfill_translation(self.task_id)
                    assert body["status"] == "started", body
                    await self._wait_released()                # 补译翻完、放掉了来源
                    gate.set()
                    await asyncio.wait_for(run, timeout=60)

                asyncio.new_event_loop().run_until_complete(main())
        finally:
            gate.set()

        task = self.orchestrator.get_task(pipeline_id)
        assert task["status"] == "completed", task.get("error_message")
        assert sum(server.translated) == 2, f"补译翻过的帖子被任务又翻了一遍：{server.translated}"

    def test_backfill_is_not_started_while_a_pipeline_is_translating_the_same_source(self):
        from app.routers.results import trigger_backfill_translation
        self._seed([("a", "Een", ""), ("b", "Twee", "")])
        gate = threading.Event()
        server = self._llm_site().LLMSite(translate_gate=gate)
        pipeline_id = "backfill-pipeline"
        try:
            with server as url:
                self._configure_llm(url)

                async def main():
                    self.orchestrator.create_task(pipeline_id, "重新翻译已有数据")
                    run = asyncio.ensure_future(self.orchestrator.execute_task(pipeline_id))
                    await self._wait_blocked(server)           # 任务的翻译请求卡住，来源占着
                    body = await trigger_backfill_translation(self.task_id)
                    gate.set()
                    await asyncio.wait_for(run, timeout=60)
                    await self._wait_released()
                    return body

                body = asyncio.new_event_loop().run_until_complete(main())
        finally:
            gate.set()
        assert body["status"] == "running", body
        assert sum(server.translated) == 2, f"任务在翻的时候补译又翻了一遍：{server.translated}"
