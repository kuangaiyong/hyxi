"""FastAPI 端点集成测试 — 使用 TestClient 真实请求"""

import os
import sys
import json
import tempfile
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
        cfg.settings.config_file = os.path.join(cls.tmpdir, "config.json")
        cfg.settings.tasks_dir = os.path.join(cls.tmpdir, "tasks")
        cfg.settings.exports_dir = os.path.join(cls.tmpdir, "exports")
        os.makedirs(cfg.settings.tasks_dir, exist_ok=True)
        os.makedirs(cfg.settings.exports_dir, exist_ok=True)

        # 创建一个空的 config.json 避免 orchestrator 报错
        with open(cfg.settings.config_file, "w") as f:
            json.dump({"api_key": "sk-test", "base_url": "https://api.test.com", "model_name": "test-model"}, f)

        from main import app
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls):
        import app.config as cfg
        cfg.settings.api_key = cls._old_key
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_health_check(self):
        resp = self.client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_root_endpoint(self):
        resp = self.client.get("/")
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
            ("export/csv", (404,)),
            ("export/json", (404,)),
        ):
            r = self.client.get(f"/api/v1/tasks/{task_id}/{path}")
            assert r.status_code in allowed, f"{path} 返回 {r.status_code}，期望 {allowed}"

    def test_sentiment_endpoints_reject_unknown_task(self):
        """task_id 会被拼进文件路径，不存在的任务必须 404 而不是去读文件"""
        for path in ("sentiment", "sentiment/download"):
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
        assert [f["name"] for f in tweakers["param_fields"]] == ["thread_id"]

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
            "params": {"thread_id": "2336074", "base_url": "https://evil.test"},
        })
        assert resp.status_code == 201
        assert "base_url" not in resp.json()["params"]
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
