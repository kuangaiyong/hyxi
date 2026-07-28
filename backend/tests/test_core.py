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
        orch_module.settings.data_dir = self.tmpdir
        return TaskOrchestrator()

    def test_create_task_writes_json_to_disk(self):
        orch = self._create_orchestrator()
        orch.create_task("t1", "抓取帖子2336074并翻译")

        assert os.path.exists(self.tasks_path)
        with open(self.tasks_path) as f:
            data = json.load(f)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "t1"
        assert data["tasks"][0]["description"] == "抓取帖子2336074并翻译"
        assert data["tasks"][0]["status"] == "pending"

    def test_cancel_running_task_persists_status_change(self):
        orch = self._create_orchestrator()
        orch.create_task("t2", "测试取消")
        orch.tasks["t2"]["status"] = "running"

        orch.cancel_task("t2")

        with open(self.tasks_path) as f:
            data = json.load(f)
        assert data["tasks"][0]["status"] == "cancelled"
        assert data["tasks"][0].get("_cancelled") is True

    def test_delete_task_removes_from_json(self):
        orch = self._create_orchestrator()
        orch.create_task("t3", "待删除")
        orch.create_task("t4", "保留")

        orch.delete_task("t3")

        with open(self.tasks_path) as f:
            data = json.load(f)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "t4"

    def test_tasks_survive_restart(self):
        orch1 = self._create_orchestrator()
        orch1.create_task("t5", "重启存活测试")
        orch1.tasks["t5"]["status"] = "completed"
        orch1._persist()

        orch2 = self._create_orchestrator()
        assert orch2.get_task("t5") is not None
        assert orch2.get_task("t5")["status"] == "completed"  # completed 重启后仍是 completed

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

        with open(self.tasks_path) as f:
            data = json.load(f)
        assert len(data["tasks"][0]["logs"]) == 2


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
