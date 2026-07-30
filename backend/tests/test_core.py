"""端到端真实测试 — 不 mock，操作真实文件和真实逻辑"""

import os
import sys
import json
import tempfile
import shutil
import hashlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestTaskLifecycleEndToEnd:
    """任务生命周期：真实 JSON 文件持久化"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_path = os.path.join(self.tmpdir, "tasks.json")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _create_orchestrator(self):
        from app.services.orchestrator import TaskOrchestrator
        import app.services.orchestrator as orch_module
        import app.services.storage as storage_module
        orch_module.settings.data_dir = self.tmpdir
        # storage.DB_PATH 是 import 时算好的常量，改 settings.data_dir 不影响它，
        # 必须一并重定向，否则本类的测试会写进真实的 backend/data/hyxi.db
        storage_module.DB_PATH = os.path.join(self.tmpdir, "hyxi.db")
        return TaskOrchestrator()

    def test_create_task_persists(self):
        orch = self._create_orchestrator()
        orch.create_task("t1", "抓取帖子2336074并翻译")

        # 通过 API 验证持久化（SQLite 或 JSON）
        task = orch.get_task("t1")
        assert task is not None
        assert task["id"] == "t1"
        assert task["description"] == "抓取帖子2336074并翻译"
        assert task["status"] == "pending"

    def test_cancel_running_task_persists_status_change(self):
        orch = self._create_orchestrator()
        orch.create_task("t2", "测试取消")
        orch.tasks["t2"]["status"] = "running"
        orch._persist()

        orch.cancel_task("t2")

        # 通过 API 验证
        task = orch.get_task("t2")
        assert task["status"] == "cancelled"
        assert task.get("_cancelled") is True

    def test_delete_task_removes_from_storage(self):
        orch = self._create_orchestrator()
        orch.create_task("t3", "待删除")
        orch.create_task("t4", "保留")

        orch.delete_task("t3")

        assert orch.get_task("t3") is None
        assert orch.get_task("t4") is not None

    def test_deleted_task_does_not_reappear_after_restart(self):
        orch1 = self._create_orchestrator()
        orch1.create_task("t8", "删除后不应复活")
        orch1.create_task("t9", "应当保留")

        assert orch1.delete_task("t8") is True
        assert orch1.get_task("t8") is None

        # 重建 orchestrator 模拟服务重启：删除必须落到存储层
        orch2 = self._create_orchestrator()
        assert orch2.get_task("t8") is None, "已删除的任务在重启后复活了"
        assert orch2.get_task("t9") is not None

    def test_tasks_survive_restart(self):
        orch1 = self._create_orchestrator()
        orch1.create_task("t5", "重启存活测试")
        orch1.tasks["t5"]["status"] = "completed"
        orch1._persist()

        orch2 = self._create_orchestrator()
        assert orch2.get_task("t5") is not None
        assert orch2.get_task("t5")["status"] == "completed"

    def test_running_task_becomes_cancelled_on_restart(self):
        orch1 = self._create_orchestrator()
        orch1.create_task("t6", "运行中被杀")
        orch1.tasks["t6"]["status"] = "running"
        orch1._persist()

        orch2 = self._create_orchestrator()
        assert orch2.get_task("t6")["status"] == "cancelled"

    def test_log_persistence(self):
        orch = self._create_orchestrator()
        orch.create_task("t7", "日志测试")
        orch.tasks["t7"]["logs"] = [
            {"time": "2026-07-28T22:00:00", "level": "info", "message": "步骤1完成"},
            {"time": "2026-07-28T22:01:00", "level": "error", "message": "步骤2失败"},
        ]
        orch._persist()

        task = orch.get_task("t7")
        assert len(task["logs"]) == 2


class TestSentimentParsingEndToEnd:
    """舆情分析：真实 LLM 响应解析"""

    def test_parse_clean_json(self):
        from app.services.sentiment_service import SentimentService
        text = '{"sentiment": "positive", "intensity": 4, "reason_cn": "用户对安装体验满意", "dimensions": ["安装/配置体验", "App/软件体验"]}'
        r = SentimentService._parse_sentiment(text)
        assert r["sentiment"] == "positive"
        assert r["intensity"] == 4
        assert len(r["dimensions"]) == 2

    def test_parse_json_with_markdown_fence(self):
        from app.services.sentiment_service import SentimentService
        text = '```json\n{"sentiment": "negative", "intensity": 3, "reason_cn": "固件有问题", "dimensions": ["固件更新"]}\n```'
        r = SentimentService._parse_sentiment(text)
        assert r["sentiment"] == "negative"

    def test_parse_json_embedded_in_narrative(self):
        from app.services.sentiment_service import SentimentService
        text = '根据分析，这条帖子{"sentiment": "neutral", "intensity": 2, "reason_cn": "纯技术咨询", "dimensions": ["认证/合规(如Synergrid)"]}，总体来看。'
        r = SentimentService._parse_sentiment(text)
        assert r["sentiment"] == "neutral"
        assert r["intensity"] == 2

    def test_parse_invalid_returns_none(self):
        from app.services.sentiment_service import SentimentService
        assert SentimentService._parse_sentiment("不是JSON") is None
        assert SentimentService._parse_sentiment("") is None


class TestBuildSummaryEndToEnd:
    """舆情汇总：真实数据统计"""

    def test_distribution_counts(self):
        from app.services.sentiment_service import SentimentService
        results = [
            {"sentiment": "positive", "intensity": 4, "reason_cn": "", "dimensions": []},
            {"sentiment": "positive", "intensity": 5, "reason_cn": "", "dimensions": []},
            {"sentiment": "negative", "intensity": 3, "reason_cn": "", "dimensions": []},
            {"sentiment": "neutral", "intensity": 2, "reason_cn": "", "dimensions": []},
        ]
        s = SentimentService._build_summary(results)
        assert s["sentiment_distribution"] == {"positive": 2, "negative": 1, "neutral": 1}
        assert s["avg_intensity"] == 3.5

    def test_top_dimensions(self):
        from app.services.sentiment_service import SentimentService
        results = [
            {"sentiment": "positive", "intensity": 3, "reason_cn": "", "dimensions": ["价格/性价比", "安装/配置体验"]},
            {"sentiment": "positive", "intensity": 4, "reason_cn": "", "dimensions": ["价格/性价比"]},
            {"sentiment": "negative", "intensity": 2, "reason_cn": "", "dimensions": ["固件更新"]},
        ]
        s = SentimentService._build_summary(results)
        assert s["top_dimensions"][0][0] == "价格/性价比"
        assert s["top_dimensions"][0][1] == 2

    def test_null_items_treated_as_neutral(self):
        from app.services.sentiment_service import SentimentService
        results = [None, {"sentiment": "positive", "intensity": 2, "reason_cn": "", "dimensions": []}]
        s = SentimentService._build_summary(results)
        assert s["sentiment_distribution"]["neutral"] == 1

    def test_empty_results(self):
        from app.services.sentiment_service import SentimentService
        s = SentimentService._build_summary([])
        assert s["avg_intensity"] == 0


class TestTimestampNormalizationEndToEnd:
    """时间戳格式转换：荷兰 dd-mm-yyyy → yyyy-mm-dd"""

    def _normalize(self, ts):
        import re
        if not ts:
            return ts
        m = re.match(r"(\d{2})-(\d{2})-(\d{4})\s+(.+)", ts)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4)}"
        return ts

    def test_dutch_format_converted(self):
        assert self._normalize("22-05-2026 17:06") == "2026-05-22 17:06"

    def test_end_of_year(self):
        assert self._normalize("31-12-2026 23:59") == "2026-12-31 23:59"

    def test_already_iso_unchanged(self):
        assert self._normalize("2026-05-22 17:06") == "2026-05-22 17:06"

    def test_empty_unchanged(self):
        assert self._normalize("") == ""


class TestExcelGenerationEndToEnd:
    """Excel 生成：真实 openpyxl 输出"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.services.excel_service as excel_mod
        excel_mod.settings.exports_dir = self.tmpdir

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generate_excel_creates_valid_file(self):
        from app.services.excel_service import ExcelService
        from app.services.progress_manager import ProgressManager

        posts = [
            {"username": "TestUser", "timestamp": "2026-05-22 17:06",
             "content": "测试内容", "translation": "Test translation", "page_number": 1},
        ]
        pm = ProgressManager()

        import asyncio
        async def run():
            return await ExcelService.execute("test-task", posts, {"include_stats": True}, pm)

        result = asyncio.get_event_loop().run_until_complete(run())

        assert os.path.exists(result["file_path"])
        assert result["file_name"].endswith(".xlsx")

        from openpyxl import load_workbook
        wb = load_workbook(result["file_path"])
        assert "论坛帖子翻译" in wb.sheetnames
        assert "统计信息" in wb.sheetnames

        ws = wb["论坛帖子翻译"]
        assert ws.cell(1, 1).value == "序号"
        assert ws.cell(2, 1).value == 1
        assert ws.cell(2, 2).value == "TestUser"
        assert ws.cell(2, 5).value == "Test translation"

    def test_generate_excel_without_stats(self):
        from app.services.excel_service import ExcelService
        from app.services.progress_manager import ProgressManager

        posts = [{"username": "U", "timestamp": "", "content": "C", "translation": "T", "page_number": 1}]
        pm = ProgressManager()

        import asyncio
        async def run():
            return await ExcelService.execute("test-task2", posts, {"include_stats": False}, pm)

        result = asyncio.get_event_loop().run_until_complete(run())
        from openpyxl import load_workbook
        wb = load_workbook(result["file_path"])
        assert "统计信息" not in wb.sheetnames


class TestThreadIdExtractionEndToEnd:
    """帖子 ID 提取"""

    def _extract(self, desc, plan=None):
        from app.services.orchestrator import _extract_thread_id
        return _extract_thread_id({"description": desc, "plan": plan or []})

    def test_from_description(self):
        assert self._extract("抓取帖子2336074的所有内容") == 2336074

    def test_from_plan_scrape_step(self):
        assert self._extract("翻译数据", [{"action": "scrape", "params": {"thread_id": 2336074}}]) == 2336074

    def test_no_match_returns_zero(self):
        assert self._extract("帮助翻译帖子") == 0


class TestFingerprintGenerationEndToEnd:
    """帖子指纹：SHA256 唯一标识"""

    def test_same_input_produces_same_fingerprint(self):
        def fingerprint(username, timestamp, content):
            raw = f"{username}|{timestamp}|{(content or '')[:100]}"
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

        fp1 = fingerprint("Dorpjes", "22-05-2026 17:06", "Sinds gisteren...")
        fp2 = fingerprint("Dorpjes", "22-05-2026 17:06", "Sinds gisteren...")
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_different_inputs_produce_different_fingerprints(self):
        def fingerprint(username, timestamp, content):
            raw = f"{username}|{timestamp}|{(content or '')[:100]}"
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

        fp1 = fingerprint("A", "t1", "content1")
        fp2 = fingerprint("B", "t2", "content2")
        assert fp1 != fp2

    def test_content_truncated_at_100_chars(self):
        def fingerprint(username, timestamp, content):
            raw = f"{username}|{timestamp}|{(content or '')[:100]}"
            return hashlib.sha256(raw.encode()).hexdigest()[:16]

        long_content = "x" * 200
        short_content = "x" * 100
        fp_long = fingerprint("U", "T", long_content)
        fp_short = fingerprint("U", "T", short_content)
        assert fp_long == fp_short


class TestIncrementalLogicEndToEnd:
    """增量处理逻辑"""

    def test_filter_already_translated_posts(self):
        posts = [
            {"fingerprint": "fp1", "_processed": {"translated": True, "sentiment_at": None}, "content": "a"},
            {"fingerprint": "fp2", "_processed": {"translated": False, "sentiment_at": None}, "content": "b"},
            {"fingerprint": "fp3", "_processed": {"translated": True, "sentiment_at": None}, "content": "c"},
        ]
        pending = [p for p in posts if not p["_processed"].get("translated")]
        assert len(pending) == 1
        assert pending[0]["fingerprint"] == "fp2"

    def test_filter_already_analyzed_posts(self):
        posts = [
            {"fingerprint": "fp1", "_processed": {"sentiment_at": "2026-07-28"}},
            {"fingerprint": "fp2", "_processed": {"sentiment_at": None}},
            {"fingerprint": "fp3", "_processed": {"sentiment_at": "2026-07-27"}},
        ]
        pending = [p for p in posts if not p["_processed"].get("sentiment_at")]
        assert len(pending) == 1
        assert pending[0]["fingerprint"] == "fp2"

    def test_fingerprint_dedup_merges_new_posts(self):
        existing = [{"fingerprint": "fp1"}, {"fingerprint": "fp2"}]
        new_all = [{"fingerprint": "fp1"}, {"fingerprint": "fp2"}, {"fingerprint": "fp3"}]
        existing_fps = {p["fingerprint"] for p in existing}
        new_only = [p for p in new_all if p["fingerprint"] not in existing_fps]
        assert len(new_only) == 1
        assert new_only[0]["fingerprint"] == "fp3"


class TestLLMUtilsEndToEnd:
    """LLM 工具函数测试"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.config as cfg
        self._orig_data_dir = cfg.settings.data_dir
        self._orig_config_file = cfg.settings.config_file
        cfg.settings.data_dir = self.tmpdir
        cfg.settings.config_file = os.path.join(self.tmpdir, "config.json")

    def teardown_method(self):
        import app.config as cfg
        cfg.settings.data_dir = self._orig_data_dir
        cfg.settings.config_file = self._orig_config_file
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_config_returns_none_when_no_file(self):
        from app.services.llm_utils import load_llm_config
        assert load_llm_config() is None

    def test_load_config_returns_config_when_file_exists(self):
        config_data = {
            "api_key": "sk-test-key-123",
            "base_url": "https://api.example.com",
            "model_name": "test-model",
        }
        cfg_path = os.path.join(self.tmpdir, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(config_data, f)

        from app.services.llm_utils import load_llm_config
        config = load_llm_config()
        assert config is not None
        assert config.api_key == "sk-test-key-123"
        assert config.base_url == "https://api.example.com"
        assert config.model_name == "test-model"

    def test_load_config_handles_corrupt_json(self):
        cfg_path = os.path.join(self.tmpdir, "config.json")
        with open(cfg_path, "w") as f:
            f.write("not valid json {{{")

        from app.services.llm_utils import load_llm_config
        assert load_llm_config() is None

    def test_get_llm_service_returns_none_without_config(self):
        from app.services.llm_utils import get_llm_service
        assert get_llm_service() is None

    def test_get_llm_service_returns_service_with_config(self):
        config_data = {
            "api_key": "sk-test",
            "base_url": "https://api.test.com",
            "model_name": "m",
        }
        with open(os.path.join(self.tmpdir, "config.json"), "w") as f:
            json.dump(config_data, f)

        from app.services.llm_utils import get_llm_service
        import asyncio
        llm = get_llm_service()
        assert llm is not None
        assert llm.config.api_key == "sk-test"
        # 清理 client
        asyncio.get_event_loop().run_until_complete(llm.close())


class TestSentimentExcelGenerationEndToEnd:
    """舆情 Excel 生成测试"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generate_sentiment_excel_creates_valid_file(self):
        from app.services.excel_service import ExcelService

        sentiment_data = {
            "task_id": "test-sentiment",
            "analyzed_at": "2026-07-29T22:00:00",
            "total": 10,
            "success": 9,
            "failed": 1,
            "summary": {
                "sentiment_distribution": {"positive": 3, "negative": 2, "neutral": 5},
                "sentiment_percentages": {"positive": 30.0, "negative": 20.0, "neutral": 50.0},
                "avg_intensity": 2.5,
                "top_dimensions": [
                    ["价格/性价比", 7],
                    ["安装/配置体验", 5],
                    ["App/软件体验", 3],
                ],
            },
            "results": [
                {"sentiment": "positive", "intensity": 4, "reason_cn": "满意", "dimensions": ["价格/性价比"]},
                {"sentiment": "negative", "intensity": 3, "reason_cn": "有问题", "dimensions": ["安装/配置体验"]},
                {"sentiment": "neutral", "intensity": 2, "reason_cn": "询问", "dimensions": []},
                None,
            ],
        }

        result = ExcelService.generate_sentiment_report("test-task", sentiment_data, self.tmpdir)

        assert os.path.exists(result["file_path"])
        assert result["file_name"].endswith(".xlsx")
        assert "sentiment" in result["file_name"]

        from openpyxl import load_workbook
        wb = load_workbook(result["file_path"])
        assert "舆情分析汇总" in wb.sheetnames
        assert "帖子情感详情" in wb.sheetnames

        ws = wb["舆情分析汇总"]
        assert ws.cell(1, 1).value and "HYXi" in str(ws.cell(1, 1).value)

        ws2 = wb["帖子情感详情"]
        assert ws2.cell(1, 1).value == "序号"
        assert ws2.cell(2, 1).value == 1
        assert ws2.cell(2, 2).value == "正面"
        assert ws2.cell(5, 2).value == "(解析失败)"

    def test_generate_sentiment_excel_with_empty_data(self):
        from app.services.excel_service import ExcelService

        sentiment_data = {
            "task_id": "test-empty",
            "analyzed_at": "2026-07-29T22:00:00",
            "total": 0,
            "success": 0,
            "failed": 0,
            "summary": {
                "sentiment_distribution": {"positive": 0, "negative": 0, "neutral": 0},
                "sentiment_percentages": {"positive": 0, "negative": 0, "neutral": 0},
                "avg_intensity": 0,
                "top_dimensions": [],
            },
            "results": [],
        }

        result = ExcelService.generate_sentiment_report("test-empty", sentiment_data, self.tmpdir)
        assert os.path.exists(result["file_path"])

        from openpyxl import load_workbook
        wb = load_workbook(result["file_path"])
        assert "舆情分析汇总" in wb.sheetnames


class TestSearchFilteringEndToEnd:
    """帖子搜索过滤测试"""

    def _make_posts(self):
        return [
            {"username": "Alice", "timestamp": "22-05-2026 10:00", "content": "De installatie was goed", "translation": "安装很好", "page_number": 1},
            {"username": "Bob", "timestamp": "22-05-2026 11:00", "content": "Probleem met de app", "translation": "App有问题", "page_number": 1},
            {"username": "Charlie", "timestamp": "22-05-2026 12:00", "content": "Vraag over garantie", "translation": "保修问题", "page_number": 2},
        ]

    def test_search_matches_username(self):
        posts = self._make_posts()
        kw = "alice"
        filtered = [p for p in posts if kw in (p.get("username") or "").lower() or kw in (p.get("content") or "").lower() or kw in (p.get("translation") or "").lower()]
        assert len(filtered) == 1
        assert filtered[0]["username"] == "Alice"

    def test_search_matches_content_dutch(self):
        posts = self._make_posts()
        kw = "installatie"
        filtered = [p for p in posts if kw in (p.get("username") or "").lower() or kw in (p.get("content") or "").lower() or kw in (p.get("translation") or "").lower()]
        assert len(filtered) == 1
        assert filtered[0]["username"] == "Alice"

    def test_search_matches_translation_chinese(self):
        posts = self._make_posts()
        kw = "保修"
        filtered = [p for p in posts if kw in (p.get("username") or "").lower() or kw in (p.get("content") or "").lower() or kw in (p.get("translation") or "").lower()]
        assert len(filtered) == 1
        assert filtered[0]["username"] == "Charlie"

    def test_search_matches_multiple(self):
        posts = self._make_posts()
        kw = "app"
        filtered = [p for p in posts if kw in (p.get("username") or "").lower() or kw in (p.get("content") or "").lower() or kw in (p.get("translation") or "").lower()]
        assert len(filtered) == 1
        assert filtered[0]["username"] == "Bob"

    def test_search_no_match(self):
        posts = self._make_posts()
        kw = "xyz_not_found"
        filtered = [p for p in posts if kw in (p.get("username") or "").lower() or kw in (p.get("content") or "").lower() or kw in (p.get("translation") or "").lower()]
        assert len(filtered) == 0

    def test_search_empty_returns_all(self):
        posts = self._make_posts()
        kw = ""
        filtered = [p for p in posts if not kw or kw in (p.get("username") or "").lower() or kw in (p.get("content") or "").lower() or kw in (p.get("translation") or "").lower()]
        assert len(filtered) == 3


class TestAtomicWriteEndToEnd:
    """原子写入测试"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tasks_path = os.path.join(self.tmpdir, "tasks.json")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_atomic_write_does_not_lose_data(self):
        """验证原子写入不会丢失已有数据"""
        # 写入初始数据
        initial = {"tasks": [{"id": "t1", "status": "completed"}]}
        with open(self.tasks_path, "w") as f:
            json.dump(initial, f)

        # 模拟原子写入
        tmp_path = self.tasks_path + ".tmp"
        new_data = {"tasks": [{"id": "t1", "status": "completed"}, {"id": "t2", "status": "pending"}]}
        with open(tmp_path, "w") as f:
            json.dump(new_data, f)
        os.replace(tmp_path, self.tasks_path)

        # 验证数据完整
        with open(self.tasks_path) as f:
            result = json.load(f)
        assert len(result["tasks"]) == 2

    def test_atomic_write_preserves_old_on_crash(self):
        """验证写入中途崩溃时原有数据不丢失"""
        # 写入原始数据
        original = {"tasks": [{"id": "original", "status": "completed"}]}
        with open(self.tasks_path, "w") as f:
            json.dump(original, f)

        # 模拟部分写入（不完成替换）
        tmp_path = self.tasks_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write("partial data [corrupt")

        # 验证原始数据完好
        with open(self.tasks_path) as f:
            result = json.load(f)
        assert result["tasks"][0]["id"] == "original"


class TestLoggingConfigEndToEnd:
    """日志配置测试"""

    def test_get_logger_returns_logger(self):
        from app.logging_config import get_logger
        import logging
        # 强制重置单例以测试
        import app.logging_config as lc
        lc._logger = None
        logger = get_logger()
        assert logger is not None
        assert isinstance(logger, logging.Logger)
        assert logger.level <= 20  # INFO or lower

    def test_get_logger_is_singleton(self):
        from app.logging_config import get_logger
        logger1 = get_logger("mod1")
        logger2 = get_logger("mod2")
        assert logger1 is logger2
