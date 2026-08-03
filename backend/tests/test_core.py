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

    def test_null_items_are_not_counted_as_neutral(self):
        from app.services.sentiment_service import SentimentService
        results = [None, {"sentiment": "positive", "intensity": 2, "reason_cn": "", "dimensions": []}]
        s = SentimentService._build_summary(results)
        assert s["sentiment_distribution"]["neutral"] == 0
        assert s["not_analyzed"] == 1
        assert s["analyzed"] == 1
        # 分母是实际分析成功的条数，未分析的不该稀释占比
        assert s["sentiment_percentages"]["positive"] == 100.0

    def test_parse_failure_placeholder_is_not_counted_as_neutral(self):
        from app.services.sentiment_service import SentimentService
        # LLM 解析失败时写入的兜底条目，不能冒充成一条真实的中性评价
        results = [
            {"sentiment": None, "intensity": 0, "reason_cn": "解析失败", "dimensions": []},
            {"sentiment": "negative", "intensity": 4, "reason_cn": "", "dimensions": []},
        ]
        s = SentimentService._build_summary(results)
        assert s["sentiment_distribution"]["neutral"] == 0
        assert s["not_analyzed"] == 1
        assert s["sentiment_percentages"]["negative"] == 100.0
        # 失败条目的 intensity 不该拉低均值
        assert s["avg_intensity"] == 4

    def test_unknown_sentiment_value_goes_to_not_analyzed(self):
        from app.services.sentiment_service import SentimentService
        results = [
            {"sentiment": "很正面", "intensity": 4, "reason_cn": "", "dimensions": []},
            {"sentiment": "positive", "intensity": 4, "reason_cn": "", "dimensions": []},
        ]
        s = SentimentService._build_summary(results)
        assert s["sentiment_distribution"] == {"positive": 1, "negative": 0, "neutral": 0}
        assert s["not_analyzed"] == 1

    def test_empty_results(self):
        from app.services.sentiment_service import SentimentService
        s = SentimentService._build_summary([])
        assert s["avg_intensity"] == 0
        assert s["sentiment_percentages"]["positive"] == 0


class TestDimensionNormalizationEndToEnd:
    """维度必须保持封闭集合，否则 top_dimensions 会碎成近义标签、跨来源对比失效"""

    def _norm(self, dims):
        from app.services.sentiment_service import SentimentService
        return SentimentService._normalize_dimensions(dims)

    def test_shorthand_maps_to_canonical_label(self):
        """实测 LLM 会把带括号的标签简写，于是同一维度在报表里占两行"""
        assert self._norm(["认证/合规"]) == ["认证/合规(如Synergrid)"]
        assert self._norm(["与其他品牌对比"]) == ["与其他品牌对比(如AEG/Marstek)"]

    def test_exact_labels_pass_through(self):
        assert self._norm(["价格/性价比", "安全性"]) == ["价格/性价比", "安全性"]

    def test_unknown_label_is_dropped(self):
        """宁可少一个标签，也不能让维度表不再封闭"""
        assert self._norm(["用户自创的新维度"]) == []

    def test_duplicates_after_normalization_are_collapsed(self):
        assert self._norm(["认证/合规", "认证/合规(如Synergrid)"]) == ["认证/合规(如Synergrid)"]

    def test_parse_sentiment_normalizes_inline(self):
        from app.services.sentiment_service import SentimentService
        parsed = SentimentService._parse_sentiment(
            '{"sentiment":"positive","intensity":3,"reason_cn":"x","dimensions":["认证/合规"]}'
        )
        assert parsed["dimensions"] == ["认证/合规(如Synergrid)"]

    def test_non_dict_and_bad_json_are_unchanged(self):
        from app.services.sentiment_service import SentimentService
        assert SentimentService._parse_sentiment("不是 JSON") is None


class TestBySourceSummaryEndToEnd:
    """跨来源对比：分组走纯 Python，不让 LLM 感知来源，免得它带着平台先验去评分"""

    def _fixture(self):
        results = [
            {"sentiment": "positive", "intensity": 4, "dimensions": ["价格/性价比"]},
            {"sentiment": "negative", "intensity": 2, "dimensions": ["固件更新"]},
            {"sentiment": "negative", "intensity": 5, "dimensions": ["价格/性价比", "安全性"]},
        ]
        posts = [
            {"source": "src_forum", "fingerprint": "a"},
            {"source": "src_forum", "fingerprint": "b"},
            {"source": "src_social", "fingerprint": "c"},
        ]
        return results, posts, {"src_forum": "论坛", "src_social": "社媒"}

    def test_no_posts_means_no_by_source(self):
        """既有调用方一行不改：不传 posts 就是原来的行为"""
        from app.services.sentiment_service import SentimentService
        results, _, _ = self._fixture()
        assert "by_source" not in SentimentService._build_summary(results)

    def test_by_source_splits_the_same_batch(self):
        from app.services.sentiment_service import SentimentService
        results, posts, names = self._fixture()
        s = SentimentService._build_summary(results, posts, names)

        assert set(s["by_source"]) == {"src_forum", "src_social"}
        assert s["by_source"]["src_forum"]["name"] == "论坛"
        assert s["by_source"]["src_forum"]["distribution"] == {"positive": 1, "negative": 1, "neutral": 0}
        assert s["by_source"]["src_forum"]["avg_intensity"] == 3.0
        assert s["by_source"]["src_social"]["distribution"]["negative"] == 1
        # 分组统计不能改变全局统计
        assert s["sentiment_distribution"] == {"positive": 1, "negative": 2, "neutral": 0}

    def test_cross_source_lines_up_dimensions(self):
        from app.services.sentiment_service import SentimentService
        results, posts, names = self._fixture()
        s = SentimentService._build_summary(results, posts, names)
        assert s["cross_source"]["价格/性价比"] == {"src_forum": 1, "src_social": 1}
        assert s["cross_source"]["安全性"] == {"src_social": 1}

    def test_legacy_posts_without_source_are_grouped_as_tweakers(self):
        from app.services.sentiment_service import SentimentService
        s = SentimentService._build_summary(
            [{"sentiment": "positive", "intensity": 3, "dimensions": []}],
            [{"fingerprint": "old"}],
            {},
        )
        assert list(s["by_source"]) == ["tweakers"]


class TestResolveSourcesEndToEnd:
    """LLM 提议、后端保证：来源一多模型很容易只挑一个，那是静默漏采"""

    def _sources(self):
        return [
            {"id": "src_a", "name": "论坛A"},
            {"id": "src_b", "name": "社媒B"},
        ]

    def _resolve(self, desc, plan_actions):
        from app.models import PlanStep
        from app.services.orchestrator import _resolve_sources
        plan = [PlanStep(action=a, params=p) for a, p in plan_actions]
        return _resolve_sources({"description": desc}, plan, self._sources())

    def test_all_sources_wording_expands_regardless_of_llm(self):
        plan, warns = self._resolve(
            "采集所有来源，翻译，导出Excel",
            [("collect", {"source_id": "src_a"}), ("translate", {}), ("generate_excel", {})],
        )
        assert [s.params["source_id"] for s in plan if s.action == "collect"] == ["src_a", "src_b"]
        assert any("漏掉" in w for w in warns)
        # 非 collect 步骤保持原有顺序，跟在 collect 后面
        assert [s.action for s in plan] == ["collect", "collect", "translate", "generate_excel"]

    def test_named_single_source_is_respected(self):
        plan, warns = self._resolve("只采集论坛A", [("collect", {"source_id": "src_a"})])
        assert [s.params["source_id"] for s in plan] == ["src_a"]
        assert warns == []

    def test_hallucinated_source_id_is_ignored_with_warning(self):
        plan, warns = self._resolve("采集数据", [("collect", {"source_id": "src_不存在"})])
        assert any("编造" in w for w in warns)
        # 一个有效来源都没给出时全采，比静默不采安全
        assert len(plan) == 2

    def test_override_fails_loudly_instead_of_substituting(self):
        """用户临时贴的链接不能拿已注册来源顶替 —— 那是答非所问，报告里还看不出来"""
        import pytest
        with pytest.raises(Exception) as e:
            self._resolve("采集 https://example.com/thread/1", [("collect", {"override": "https://example.com/thread/1"})])
        assert "数据源" in str(e.value)

    def test_plan_without_collect_is_untouched(self):
        plan, warns = self._resolve("翻译已有数据", [("translate", {}), ("generate_excel", {})])
        assert [s.action for s in plan] == ["translate", "generate_excel"]
        assert warns == []


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
        assert [ws.cell(1, c).value for c in range(1, 9)] == [
            "序号", "来源", "层级", "用户名", "发布时间", "原文", "中文翻译", "页码",
        ]
        assert ws.cell(2, 1).value == 1
        assert ws.cell(2, 4).value == "TestUser"
        assert ws.cell(2, 7).value == "Test translation"

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


class TestPostKeyEndToEnd:
    """跨来源索引键：指纹不含来源，键必须带上，否则两个平台的帖子会互相覆盖"""

    def test_same_fingerprint_different_sources_do_not_collide(self):
        from app.services.post_tree import post_key
        a = {"source": "src_aaa", "fingerprint": "deadbeefdeadbeef"}
        b = {"source": "src_bbb", "fingerprint": "deadbeefdeadbeef"}
        assert post_key(a) != post_key(b)

    def test_legacy_posts_without_source_fall_back_to_tweakers(self):
        """历史数据没有 source 字段，缺省填 tweakers 才能和既有落盘数据对上"""
        from app.services.post_tree import post_key
        assert post_key({"fingerprint": "abc123"}) == "tweakers:abc123"
        assert post_key({"source": "", "fingerprint": "abc123"}) == "tweakers:abc123"

    def test_merge_by_key_does_not_leak_across_sources(self):
        from app.services.orchestrator import _merge_by_fingerprint
        source_posts = [
            {"source": "src_a", "fingerprint": "ff", "content": "A 的帖子"},
            {"source": "src_b", "fingerprint": "ff", "content": "B 的帖子"},
        ]
        translated = [{"source": "src_a", "fingerprint": "ff", "content": "A 的帖子", "translation": "只属于A"}]
        merged = _merge_by_fingerprint(source_posts, translated)
        assert merged[0]["translation"] == "只属于A"
        assert "translation" not in merged[1]


class TestPostTreeEndToEnd:
    """扁平数组 → 出口组树。存储层不动，8 处依赖扁平结构的逻辑一行不改"""

    def _posts(self):
        return [
            {"source": "s", "fingerprint": "root1", "content": "主贴1"},
            {"source": "s", "fingerprint": "c1", "parent_fingerprint": "root1", "content": "评论1"},
            {"source": "s", "fingerprint": "c2", "parent_fingerprint": "c1", "content": "评论1的回复"},
            {"source": "s", "fingerprint": "root2", "content": "主贴2"},
        ]

    def test_build_tree_groups_children_under_parents(self):
        from app.services.post_tree import build_tree, post_key
        roots, children = build_tree(self._posts())
        assert [r["fingerprint"] for r in roots] == ["root1", "root2"]
        assert [c["fingerprint"] for c in children[post_key(roots[0])]] == ["c1"]

    def test_orphan_comment_is_kept_as_root(self):
        """父贴不在本批数据里的评论不能被丢掉 —— 增量只抓到评论那一轮就会这样"""
        from app.services.post_tree import build_tree
        roots, _ = build_tree([
            {"source": "s", "fingerprint": "x", "parent_fingerprint": "不存在的父贴"},
        ])
        assert len(roots) == 1

    def test_order_by_thread_is_depth_first_and_fills_level(self):
        from app.services.post_tree import order_by_thread
        ordered = order_by_thread(self._posts())
        assert [p["fingerprint"] for p in ordered] == ["root1", "c1", "c2", "root2"]
        assert [p["reply_level"] for p in ordered] == [0, 1, 2, 0]

    def test_pure_forum_data_is_returned_untouched(self):
        from app.services.post_tree import order_by_thread
        posts = [{"fingerprint": "a"}, {"fingerprint": "b"}]
        assert order_by_thread(posts) is posts


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


class TestEnvFileAnchoringEndToEnd:
    """.env 只认项目根那一份 —— 文档里的启动命令先 cd 进 backend，
    env_file 写相对路径的话根目录配置会被整个跳过，密钥漏配还毫无提示"""

    def test_env_file_is_project_root_not_cwd(self):
        from app.config import Settings

        env_file = Settings.model_config["env_file"]
        assert os.path.isabs(env_file)
        assert env_file == os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            ".env",
        )

    def test_stray_env_in_cwd_is_ignored(self):
        """在别处放一份同名 .env 并切过去，配置不能被它带跑"""
        from app.config import Settings

        tmpdir = tempfile.mkdtemp()
        original_cwd = os.getcwd()
        try:
            with open(os.path.join(tmpdir, ".env"), "w") as f:
                f.write("TWEAKERS_SECRET_KEY=stray-key-from-cwd\n")
            os.chdir(tmpdir)
            assert Settings().secret_key != "stray-key-from-cwd"
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestTranslationNumberingStripEndToEnd:
    """翻译结果的编号前缀剥离：不得吞掉正文里的真实数值"""

    def test_leading_value_preserved(self):
        from app.services.translator_service import _strip_numbering
        assert _strip_numbering("5 kWh 就够了") == "5 kWh 就够了"

    def test_leading_year_preserved(self):
        from app.services.translator_service import _strip_numbering
        assert _strip_numbering("2026 年才会上市") == "2026 年才会上市"

    def test_real_numbering_prefix_removed(self):
        from app.services.translator_service import _strip_numbering
        assert _strip_numbering("1. 这块电池不错") == "这块电池不错"
        assert _strip_numbering("[2]. 这块电池不错") == "这块电池不错"
        assert _strip_numbering("3) 这块电池不错") == "这块电池不错"
        assert _strip_numbering("4、这块电池不错") == "这块电池不错"

    def test_bracketed_number_followed_by_newline_removed(self):
        """请求里的编号就是 [n] 这个形态，模型经常原样抄回来。
        实测在双来源端到端里印进了 Excel 和结果页。"""
        from app.services.translator_service import _strip_numbering
        assert _strip_numbering("[1]\n我使用HYXi Halo已经三个月了") == "我使用HYXi Halo已经三个月了"
        assert _strip_numbering("[12] 逆变器停机了") == "逆变器停机了"

    def test_bracketed_value_in_text_preserved(self):
        from app.services.translator_service import _strip_numbering
        # 方括号里是编号才剥；正文本身以数字开头的照旧保留
        assert _strip_numbering("15 分钟就装好了") == "15 分钟就装好了"


class TestUntranslatedDetectionEndToEnd:
    """批内混语种时 LLM 有时把一种语言原样吐回来，要能被判成漏译进重译队列"""

    def test_identical_non_chinese_is_flagged(self):
        from app.services.translator_service import _looks_untranslated
        assert _looks_untranslated("Firmware update broke my WiFi", "Firmware update broke my WiFi")

    def test_chinese_source_is_not_flagged(self):
        """原文本来就是中文时「译文==原文」是正确结果，不能拉去重译"""
        from app.services.translator_service import _looks_untranslated
        assert not _looks_untranslated("这块电池不错", "这块电池不错")

    def test_real_translation_is_not_flagged(self):
        from app.services.translator_service import _looks_untranslated
        assert not _looks_untranslated("固件更新弄坏了我的 WiFi", "Firmware update broke my WiFi")

    def test_empty_translation_is_not_flagged_here(self):
        """空译文由既有的 [翻译为空] 分支处理，这里不重复判定"""
        from app.services.translator_service import _looks_untranslated
        assert not _looks_untranslated("", "Firmware update")


class TestIncrementalMergeOrderEndToEnd:
    """增量翻译合并：顺序必须与源 JSON 一致"""

    def _post(self, fp, translated=False):
        p = {"fingerprint": fp, "username": "u", "content": f"c{fp}", "page_number": 1}
        if translated:
            p["translation"] = f"t{fp}"
            p["_processed"] = {"translated": True}
        return p

    def test_merge_keeps_source_order(self):
        from app.services.orchestrator import _merge_by_fingerprint
        # 源顺序 a,b,c,d，其中 b、d 已翻译，a、c 本轮才翻译
        source = [self._post("a"), self._post("b", True), self._post("c"), self._post("d", True)]
        pending = [source[0], source[2]]
        for p in pending:
            p["translation"] = f"new-{p['fingerprint']}"

        merged = _merge_by_fingerprint(source, pending)
        assert [p["fingerprint"] for p in merged] == ["a", "b", "c", "d"]
        assert merged[0]["translation"] == "new-a"
        assert merged[1]["translation"] == "tb"

    def test_posts_without_fingerprint_survive(self):
        from app.services.orchestrator import _merge_by_fingerprint
        source = [{"content": "无指纹"}, self._post("z")]
        merged = _merge_by_fingerprint(source, [{"content": "无指纹", "translation": "x"}])
        assert len(merged) == 2
        assert merged[1]["fingerprint"] == "z"


class TestSchedulerStartupResilienceEndToEnd:
    """调度器启动：单条坏配置不得砖化整个服务"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.services.scheduler_service as sched_mod
        self._orig_data_dir = sched_mod.settings.data_dir
        sched_mod.settings.data_dir = self.tmpdir

    def teardown_method(self):
        import asyncio
        import app.services.scheduler_service as sched_mod
        sched_mod.settings.data_dir = self._orig_data_dir
        # asyncio.run 结束时会把当前事件循环置空，其余用 get_event_loop 的测试会跟着挂
        asyncio.set_event_loop(asyncio.new_event_loop())
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_malformed_config_is_skipped_not_fatal(self):
        import asyncio
        import app.services.scheduler_service as sched_mod

        svc = sched_mod.SchedulerService()
        # 坏配置排在前面：修复前它会中断整个加载循环，后面的好配置永远注册不上
        with open(svc._config_path, "w", encoding="utf-8") as f:
            json.dump([
                {"id": "bad", "description": "畸形时间", "interval": "daily",
                 "time": "9", "enabled": True},
                {"id": "good", "description": "正常配置", "interval": "hourly",
                 "enabled": True},
            ], f, ensure_ascii=False)

        async def run():
            svc.start()
            try:
                return svc.scheduler.get_job("bad"), svc.scheduler.get_job("good")
            finally:
                svc.shutdown()

        bad, good = asyncio.run(run())
        assert bad is None, "畸形配置被注册成了 job"
        assert good is not None, "坏配置把后面的好配置一起带崩了"

    def test_create_does_not_persist_unbuildable_config(self):
        import asyncio
        import app.services.scheduler_service as sched_mod

        svc = sched_mod.SchedulerService()

        async def run():
            svc.start()
            try:
                svc.create("时间畸形", "daily", "9")
            except Exception:
                pass
            finally:
                svc.shutdown()

        asyncio.run(run())
        assert svc._load_configs() == [], "建不起 job 的脏配置被落盘了"

    def test_update_does_not_persist_unbuildable_config(self):
        import asyncio
        import app.services.scheduler_service as sched_mod

        svc = sched_mod.SchedulerService()
        # 模拟旧版本遗留的脏数据：time 畸形，但 hourly 不读 time 所以 job 建得起来
        with open(svc._config_path, "w", encoding="utf-8") as f:
            json.dump([{"id": "legacy", "description": "遗留脏配置", "interval": "hourly",
                        "time": "9", "enabled": True}], f, ensure_ascii=False)

        async def run():
            svc.start()
            try:
                svc.update("legacy", {"interval": "daily"})
            except Exception:
                pass
            finally:
                svc.shutdown()

        asyncio.run(run())
        assert svc._load_configs()[0]["interval"] == "hourly", "改不成的 interval 被落盘了"


class TestProcessedFlagSyncEndToEnd:
    """舆情分析写回 _processed：必须合并，不能覆盖掉 translated 标记"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.json_path = os.path.join(self.tmpdir, "tweakers_thread_123.json")
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump({"posts": [
                {"fingerprint": "aaa", "content": "c1", "translation": "t1",
                 "_processed": {"translated": True}},
                {"fingerprint": "bbb", "content": "c2", "translation": "t2",
                 "_processed": {"translated": True}},
            ]}, f, ensure_ascii=False)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sentiment_flag_does_not_wipe_translated(self):
        from app.services.sentiment_service import _sync_processed_flags

        analyzed = [{"fingerprint": "aaa", "_processed": {"sentiment_at": "2026-07-30T12:00:00"}}]
        assert _sync_processed_flags(self.json_path, analyzed) == 1

        with open(self.json_path, encoding="utf-8") as f:
            posts = json.load(f)["posts"]
        assert posts[0]["_processed"]["translated"] is True, "translated 标记被舆情写回抹掉了"
        assert posts[0]["_processed"]["sentiment_at"] == "2026-07-30T12:00:00"
        assert posts[1]["_processed"] == {"translated": True}

    def test_no_match_leaves_file_untouched(self):
        from app.services.sentiment_service import _sync_processed_flags
        before = open(self.json_path, encoding="utf-8").read()
        assert _sync_processed_flags(self.json_path, [{"fingerprint": "zzz", "_processed": {}}]) == 0
        assert open(self.json_path, encoding="utf-8").read() == before


class TestExcelRobustnessEndToEnd:
    """Excel 渲染对脏数据的容忍度"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.services.excel_service as excel_mod
        excel_mod.settings.exports_dir = self.tmpdir

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_star_level_normalizes_dirty_intensity(self):
        from app.services.excel_service import _star_level
        assert _star_level("很强") == 0
        assert _star_level(None) == 0
        assert _star_level(-3) == 0
        assert _star_level(99) == 5
        assert _star_level(3.4) == 3

    def test_sentiment_excel_survives_dirty_intensity(self):
        from app.services.excel_service import ExcelService
        sentiment_data = {
            "task_id": "dirty", "analyzed_at": "2026-07-30T10:00:00",
            "total": 3, "success": 3, "failed": 0,
            "summary": {
                "sentiment_distribution": {"positive": 3, "negative": 0, "neutral": 0},
                "sentiment_percentages": {"positive": 100.0, "negative": 0, "neutral": 0},
                "avg_intensity": 0, "top_dimensions": [],
            },
            "results": [
                {"sentiment": "positive", "intensity": "高", "reason_cn": "", "dimensions": []},
                {"sentiment": "positive", "intensity": None, "reason_cn": "", "dimensions": []},
                {"sentiment": "positive", "intensity": 12, "reason_cn": "", "dimensions": []},
            ],
        }
        result = ExcelService.generate_sentiment_report("dirty", sentiment_data, self.tmpdir)
        from openpyxl import load_workbook
        ws = load_workbook(result["file_path"])["帖子情感详情"]
        assert ws.cell(2, 3).value.startswith("☆☆☆☆☆")
        assert ws.cell(4, 3).value.startswith("★★★★★")

    def test_time_range_uses_extremes_not_first_last(self):
        from app.services.excel_service import ExcelService
        from app.services.progress_manager import ProgressManager
        import asyncio

        # 乱序 + 跨年，字典序会把 "05-01-2027" 排在 "22-05-2026" 前面
        posts = [
            {"username": "u", "timestamp": "22-05-2026 17:06", "content": "c", "translation": "t", "page_number": 1},
            {"username": "u", "timestamp": "05-01-2027 09:00", "content": "c", "translation": "t", "page_number": 1},
            {"username": "u", "timestamp": "03-03-2026 08:00", "content": "c", "translation": "t", "page_number": 1},
        ]
        pm = ProgressManager()
        result = asyncio.get_event_loop().run_until_complete(
            ExcelService.execute("time-range", posts, {"include_stats": True}, pm)
        )
        from openpyxl import load_workbook
        ws = load_workbook(result["file_path"])["统计信息"]
        rows = {ws.cell(r, 1).value: ws.cell(r, 2).value for r in range(2, 7)}
        assert rows["时间范围开始"] == "03-03-2026 08:00"
        assert rows["时间范围结束"] == "05-01-2027 09:00"

    def test_export_name_is_timestamped_so_reruns_do_not_overwrite(self):
        from app.services.excel_service import ExcelService
        from app.services.progress_manager import ProgressManager
        import asyncio, time

        posts = [{"username": "u", "timestamp": "22-05-2026 17:06", "content": "c",
                  "translation": "t", "page_number": 1}]
        pm = ProgressManager()
        loop = asyncio.get_event_loop()
        first = loop.run_until_complete(ExcelService.execute("dup-task", posts, {}, pm))
        time.sleep(1.1)
        second = loop.run_until_complete(ExcelService.execute("dup-task", posts, {}, pm))

        assert first["file_name"] != second["file_name"], "同一任务重跑会盖掉上一次导出"
        assert os.path.exists(first["file_path"]) and os.path.exists(second["file_path"])


class TestSentimentIsolationEndToEnd:
    """舆情结果不得跨任务串台"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        import app.services.storage as storage_module
        self.storage = storage_module
        self._old_db = storage_module.DB_PATH
        storage_module.DB_PATH = os.path.join(self.tmpdir, "hyxi.db")
        storage_module.init_db()

    def teardown_method(self):
        self.storage.DB_PATH = self._old_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_task_does_not_fall_back_to_another_task(self):
        self.storage.save_sentiment("task-a", {
            "task_id": "task-a", "total": 2, "analyzed_at": "2026-07-30T10:00:00",
            "results": [{"sentiment": "positive"}, {"sentiment": "negative"}],
        })
        assert self.storage.get_sentiment("task-a")["task_id"] == "task-a"
        # 曾经在这里回退返回最新一条，索引与本任务帖子对不上还会被合并写回
        assert self.storage.get_sentiment("task-b") is None


class TestProgressManagerResourceEndToEnd:
    """SSE 订阅队列的资源边界"""

    def test_full_queue_drops_oldest_instead_of_blocking(self):
        import asyncio
        from app.services.progress_manager import ProgressManager, QUEUE_MAXSIZE

        pm = ProgressManager()

        async def run():
            q = pm.subscribe("t1")
            for i in range(QUEUE_MAXSIZE + 5):
                await pm.emit("t1", "log", {"i": i})
            assert q.qsize() == QUEUE_MAXSIZE
            first = await q.get()
            # 最旧的 5 条被丢弃，而不是让 emit 无界堆积或阻塞抓取主流程
            assert first["data"]["i"] == 5

        asyncio.new_event_loop().run_until_complete(run())

    def test_unsubscribe_removes_empty_key(self):
        from app.services.progress_manager import ProgressManager

        pm = ProgressManager()
        q = pm.subscribe("t2")
        assert "t2" in pm.subscribers
        pm.unsubscribe("t2", q)
        assert "t2" not in pm.subscribers, "订阅字典会随任务数单调增长"

    def test_sentiment_complete_terminates_stream(self):
        import asyncio
        from app.services.progress_manager import ProgressManager

        pm = ProgressManager()

        async def run():
            gen = pm.event_generator("t3")
            asyncio.ensure_future(_emit_later(pm))
            frames = [frame async for frame in gen]
            return frames

        async def _emit_later(manager):
            await asyncio.sleep(0.05)
            await manager.emit("t3", "log", {"message": "x"})
            await manager.emit("t3", "sentiment_complete", {"status": "completed"})

        loop = asyncio.new_event_loop()
        frames = loop.run_until_complete(asyncio.wait_for(run(), timeout=10))
        assert any("sentiment_complete" in f for f in frames)
        assert not pm.subscribers


class TestUpstreamErrorRedactionEndToEnd:
    """上游错误体会落库并被展示，不能原样存"""

    def _resp(self, status: int, body: str):
        import httpx
        return httpx.Response(status, text=body, request=httpx.Request("POST", "https://x/y"))

    def test_extracts_message_and_masks_secrets(self):
        from app.services.llm_service import _safe_error_detail
        body = json.dumps({"error": {
            "message": "Invalid key sk-abcdef1234567890 for org-acme-42",
            "type": "authentication_error",
        }})
        detail = _safe_error_detail(self._resp(401, body))
        assert "sk-abcdef1234567890" not in detail
        assert "org-acme-42" not in detail
        assert "Invalid key" in detail

    def test_unparseable_body_is_not_echoed(self):
        from app.services.llm_service import _safe_error_detail
        detail = _safe_error_detail(self._resp(500, "<html>account 12345 quota exhausted</html>"))
        assert "12345" not in detail
        assert "日志" in detail


# 真实拉起 node 子进程，没有 node 就跳过而不是伪造
_HAS_NODE = shutil.which("node") is not None


def _fake_playwright_install(root: str) -> None:
    """让 CollectorRunner 的依赖自检在 tmpdir 里通过（用假脚本的测试并不需要真 playwright）"""
    pkg_dir = os.path.join(root, "node_modules", "playwright")
    os.makedirs(pkg_dir, exist_ok=True)
    with open(os.path.join(pkg_dir, "package.json"), "w", encoding="utf-8") as f:
        f.write('{"name":"playwright","version":"0.0.0-test"}')

def _write_collector_script(root: str, script: str) -> None:
    """假采集器脚本写到 collectors/tweakers.js —— 与真实布局一致"""
    os.makedirs(os.path.join(root, "collectors"), exist_ok=True)
    with open(os.path.join(root, "collectors", "tweakers.js"), "w", encoding="utf-8") as f:
        f.write(script)


# 采集脚本读 job 文件而不是 argv，假脚本也必须走同一条协议
_READ_JOB = """
const fs = require('fs');
const jobArg = process.argv.find(a => a.startsWith('--job='));
const job = JSON.parse(fs.readFileSync(jobArg.slice('--job='.length), 'utf-8'));
"""

STALL_SCRIPT = """
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const beat = path.join(__dirname, 'heartbeat.txt');

if (process.argv.includes('--child')) {
  setInterval(() => fs.writeFileSync(beat, String(Date.now())), 100);
} else {
  console.log('第 1/9 页');
  spawn(process.execPath, [__filename, '--child'], { stdio: 'ignore' });
  setInterval(() => {}, 1000);
}
"""


class TestScraperStallTimeoutEndToEnd:
    """抓取子进程 stall 时必须超时并连同后代一起清理"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        _write_collector_script(self.tmpdir, STALL_SCRIPT)
        _fake_playwright_install(self.tmpdir)
        import app.services.collector_runner as runner_mod
        self.mod = runner_mod
        self._old_root = runner_mod.settings.project_root
        self._old_data = runner_mod.settings.data_dir
        self._old_timeout = runner_mod.SUBPROCESS_TIMEOUT
        runner_mod.settings.project_root = self.tmpdir
        runner_mod.settings.data_dir = self.tmpdir
        runner_mod.SUBPROCESS_TIMEOUT = 3

    def teardown_method(self):
        self.mod.settings.project_root = self._old_root
        self.mod.settings.data_dir = self._old_data
        self.mod.SUBPROCESS_TIMEOUT = self._old_timeout
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_stalled_stdout_times_out_and_kills_whole_tree(self):
        if not _HAS_NODE:
            import pytest
            pytest.skip("未安装 node")

        import asyncio, time
        from app.collectors import get_collector
        from app.services.progress_manager import ProgressManager

        collector = get_collector("tweakers")

        async def run():
            # 外层再套一层超时：回归时读流循环永不结束，测试应快速失败而不是挂死
            with_timeout = asyncio.wait_for(
                self.mod.CollectorRunner.execute(
                    "stall", collector,
                    {"id": collector.id, "params": {"thread_id": 123}},
                    ProgressManager(),
                ),
                timeout=30,
            )
            try:
                await with_timeout
            except Exception as e:
                return str(e)
            return ""

        loop = asyncio.new_event_loop()
        try:
            message = loop.run_until_complete(run())
        finally:
            loop.close()

        assert "超时" in message, f"stall 的子进程没有触发超时: {message!r}"

        # 心跳文件由孙进程写：只 terminate 父进程的话它会继续跳（Playwright 的 Chromium 同理）
        beat = os.path.join(self.tmpdir, "collectors", "heartbeat.txt")
        assert os.path.exists(beat), "孙进程没起来，本用例失去意义"
        before = os.path.getmtime(beat)
        time.sleep(2)
        assert os.path.getmtime(beat) == before, "子进程树没被清理干净，孙进程仍在运行"


class TestStartPageResolutionEndToEnd:
    """起始页由后端确定性派生 —— LLM 把 URL 末尾页码当起始页会永久丢掉前面几页"""

    def _resolve(self, description: str, params: dict):
        from app.services.orchestrator import _resolve_start_page
        ignored = _resolve_start_page({"description": description}, params)
        return params.get("start_page"), ignored

    def test_url_trailing_page_does_not_become_start_page(self):
        # 用户原话回归锁：URL 末尾的 /4 是页码，且「所有页面」必须从第 1 页开始
        start, ignored = self._resolve(
            "抓取https://gathering.tweakers.net/forum/list_messages/2336074/4所有页面，翻译成中文",
            {"thread_id": 2336074, "start_page": 4},
        )
        assert start == 1
        assert ignored == 4, "被忽略的 LLM 取值必须回传给调用方打日志，静默纠正同样难排查"

    def test_no_llm_value_resolves_to_first_page_silently(self):
        start, ignored = self._resolve("抓取帖子 2336074 所有页面", {"thread_id": 2336074})
        assert start == 1
        assert ignored is None

    def test_explicit_instruction_is_honored(self):
        assert self._resolve("抓取帖子 2336074，从第 7 页开始", {})[0] == 7
        assert self._resolve("抓取帖子 2336074，从第7页起", {})[0] == 7
        assert self._resolve("scrape thread 2336074 start_page: 7", {})[0] == 7

    def test_illegal_values_fall_back_to_first_page(self):
        assert self._resolve("抓取帖子 2336074，从第 0 页开始", {"start_page": 0})[0] == 1
        assert self._resolve("抓取帖子 2336074", {"start_page": -3})[0] == 1
        assert self._resolve("抓取帖子 2336074", {"start_page": "第四页"})[0] == 1


OK_SCRIPT = _READ_JOB + """
console.log(JSON.stringify({ evt: 'progress', current: 1, total: 1, msg: '第 1/1 页' }));
fs.writeFileSync(
  job.output_path,
  JSON.stringify({ thread_id: 123, total_pages: 1, total_posts: 0, complete: true, stop_reason: null, posts: [] }),
  'utf-8'
);
"""

JOB_ECHO_SCRIPT = _READ_JOB + """
fs.writeFileSync(
  job.output_path,
  JSON.stringify({ thread_id: 123, total_pages: 1, total_posts: 0, complete: true,
                   stop_reason: null, posts: [], job: job }),
  'utf-8'
);
"""

PARTIAL_SCRIPT = _READ_JOB + """
console.log(JSON.stringify({ evt: 'progress', current: 1, total: 9, msg: '第 1/9 页' }));
fs.writeFileSync(
  job.output_path,
  JSON.stringify({ thread_id: 123, total_pages: 9, total_posts: 1, complete: false,
                   stop_reason: '目标站拒绝访问 (HTTP 429)，已主动停止抓取', posts: [] }),
  'utf-8'
);
console.error('目标站拒绝访问 (HTTP 429)，已主动停止抓取');
process.exitCode = 2;
"""


class _ScraperTmpRoot:
    """把 project_root 指到 tmpdir，脚本内容由子类给出"""

    script = OK_SCRIPT

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        _write_collector_script(self.tmpdir, self.script)
        import app.services.collector_runner as runner_mod
        self.mod = runner_mod
        self._old_root = runner_mod.settings.project_root
        self._old_data = runner_mod.settings.data_dir
        runner_mod.settings.project_root = self.tmpdir
        runner_mod.settings.data_dir = self.tmpdir

    def teardown_method(self):
        self.mod.settings.project_root = self._old_root
        self.mod.settings.data_dir = self._old_data
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _execute(self, params=None):
        import asyncio
        from app.collectors import get_collector
        from app.services.progress_manager import ProgressManager

        collector = get_collector("tweakers")

        async def run():
            return await self.mod.CollectorRunner.execute(
                "t", collector,
                {"id": collector.id, "params": params or {"thread_id": 123}},
                ProgressManager(),
            )

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run())
        finally:
            loop.close()


class TestScraperDependencyGuardEndToEnd(_ScraperTmpRoot):
    """根目录没装 Node 依赖时，用户该看到「怎么修」而不是 MODULE_NOT_FOUND 堆栈"""

    def test_missing_playwright_reports_how_to_fix(self):
        try:
            self._execute()
        except Exception as e:
            assert "npm" in str(e), f"异常没告诉用户怎么修: {e}"
        else:
            assert False, "缺少 playwright 时没有报错"

    def test_guard_passes_once_installed(self):
        if not _HAS_NODE:
            import pytest
            pytest.skip("未安装 node")
        _fake_playwright_install(self.tmpdir)
        data = self._execute()
        assert data["complete"] is True


class TestScraperPartialExitEndToEnd(_ScraperTmpRoot):
    """残缺结果必须让任务失败，且原因要能被用户读懂"""

    script = PARTIAL_SCRIPT

    def setup_method(self):
        super().setup_method()
        _fake_playwright_install(self.tmpdir)

    def test_partial_exit_code_and_reason_reach_the_user(self):
        if not _HAS_NODE:
            import pytest
            pytest.skip("未安装 node")
        try:
            self._execute()
        except Exception as e:
            message = str(e)
        else:
            message = ""
        assert "code=2" in message, f"退出码 2 没有让抓取步骤失败: {message!r}"
        assert "拒绝访问" in message, f"中断原因没带到用户面前: {message!r}"


class TestStartPageReachesCollectorJobEndToEnd(_ScraperTmpRoot):
    """用户原话 → 真实子进程收到的 job：URL 末尾的页码不能变成起始页"""

    script = JOB_ECHO_SCRIPT

    def setup_method(self):
        super().setup_method()
        _fake_playwright_install(self.tmpdir)

    def test_url_trailing_page_does_not_become_start_page(self):
        if not _HAS_NODE:
            import pytest
            pytest.skip("未安装 node")
        from app.services.orchestrator import _resolve_start_page

        task = {"description": "抓取 https://gathering.tweakers.net/forum/list_messages/2336074/4"
                               "所有页面，翻译成中文，导出Excel，分析舆情"}
        params = {"thread_id": 123, "start_page": 4}  # LLM 把 URL 末尾的 4 当成了起始页
        _resolve_start_page(task, params)

        job = self._execute(params)["job"]
        assert job["params"]["start_page"] == 1, f"起始页没有落到第 1 页: {job['params']}"

    def test_pacing_is_not_configurable_at_all(self):
        """请求节奏是反爬纪律，谁都不能改 —— 塞进 params 也得被忽略"""
        if not _HAS_NODE:
            import pytest
            pytest.skip("未安装 node")

        job = self._execute({
            "thread_id": 123,
            "pacing": {"delay_min": 0, "delay_max": 0},
        })["job"]
        assert job["pacing"] == {"delay_min": 4000, "delay_max": 11000}

    def test_llm_cannot_reach_collector_params_at_all(self):
        """collect 步骤只把 source_id 交给编排层，LLM 给的其它参数一律不进 job。

        这比「base_url 只认 source」更强：模型现在连一个字段都递不进来。
        base_url 本身是允许用户在数据源页配的（自建镜像 / 本地验证）。
        """
        from app.models import PlanStep
        from app.services.orchestrator import _resolve_sources

        sources = [{"id": "src_x", "name": "某来源"}]
        plan, _warns = _resolve_sources(
            {"description": "采集"},
            [PlanStep(action="collect", params={
                "source_id": "src_x",
                "base_url": "http://evil.example",
                "thread_id": 999,
                "pacing": {"delay_min": 0},
            })],
            sources,
        )
        assert set(plan[0].params) == {"source_id", "source_name"}, plan[0].params


_FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
GOLDEN_FILE = os.path.join(_FIXTURES_DIR, "golden_tweakers.json")
FIXTURE_THREAD_ID = 9990001


class TestTweakersCollectorGoldenEndToEnd:
    """真 Chrome + 真 HTTP + 真子进程跑本地 fixture 站点，提取结果必须与黄金基线逐字相等。

    出口 IP 已被 Tweakers 防火墙封禁，真站抓不了；换成本地站点后跑的仍是完整链路，
    不涉及任何 mock。基线由重构前的脚本对同一份 HTML 抓出，指纹是增量去重与跨文件
    合并的唯一锚点 —— 它变了，全部历史数据失配、已翻译的帖子会被重新付费翻译。
    """

    def setup_method(self):
        from app.config import settings
        self.tmpdir = tempfile.mkdtemp()
        self.output = os.path.join(
            settings.project_root, f"tweakers_thread_{FIXTURE_THREAD_ID}.json"
        )
        if os.path.exists(self.output):
            os.remove(self.output)

    def teardown_method(self):
        if os.path.exists(self.output):
            os.remove(self.output)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extraction_matches_golden_baseline(self):
        import pytest
        from app.config import settings

        if not _HAS_NODE:
            pytest.skip("未安装 node")
        if not os.path.exists(
            os.path.join(settings.project_root, "node_modules", "playwright", "package.json")
        ):
            pytest.skip("项目根目录未安装 playwright")

        sys.path.insert(0, _FIXTURES_DIR)
        from fixture_site import FixtureSite

        import asyncio
        from app.collectors import get_collector
        from app.services.collector_runner import CollectorRunner
        from app.services.progress_manager import ProgressManager

        collector = get_collector("tweakers")

        with FixtureSite() as base_url:
            source = {
                "id": "fixture_tweakers",
                "params": {
                    "thread_id": FIXTURE_THREAD_ID,
                    "headless": True,
                    "incremental": False,
                },
                "base_url": base_url,
                "state_file": os.path.join(self.tmpdir, "state.json"),
                "pacing": {"delay_min": 200, "delay_max": 400},
            }
            loop = asyncio.new_event_loop()
            try:
                data = loop.run_until_complete(
                    CollectorRunner.execute("golden", collector, source, ProgressManager())
                )
            finally:
                loop.close()

        with open(GOLDEN_FILE, "r", encoding="utf-8") as f:
            golden = json.load(f)

        assert data["complete"] is True, f"抓取未完成: {data.get('stop_reason')}"
        assert data["total_pages"] == golden["total_pages"]
        assert len(data["posts"]) == len(golden["posts"]), "帖子数与基线不一致"

        fields = ("username", "timestamp", "content", "page_number", "message_id", "fingerprint")
        for got, want in zip(data["posts"], golden["posts"]):
            assert {k: got.get(k) for k in fields} == {k: want.get(k) for k in fields}


class TestGroupFeedCollectorEndToEnd:
    """真 Chrome + 真 HTTP + 真子进程跑本地小组 fixture：嵌套评论与增量合并"""

    GROUP_ID = "2407063016436085"

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output = os.path.join(self.tmpdir, "group_feed_out.json")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, incremental: bool):
        import asyncio
        from app.collectors import get_collector
        from app.services.collector_runner import CollectorRunner
        from app.services.progress_manager import ProgressManager

        sys.path.insert(0, _FIXTURES_DIR)
        from fixture_site import FixtureSite

        collector = get_collector("group_feed")
        # 输出改到临时目录，不污染项目根
        collector = type(collector)()
        collector.output_path = lambda source: self.output

        with FixtureSite() as base_url:
            source = {
                "id": "fixture_group",
                "collector_id": "group_feed",
                "params": {
                    "group_id": self.GROUP_ID,
                    "base_url": base_url,
                    "headless": True,
                    "incremental": incremental,
                },
                "state_file": os.path.join(self.tmpdir, "state.json"),
                "pacing": {"delay_min": 200, "delay_max": 400},
            }
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    CollectorRunner.execute("group-e2e", collector, source, ProgressManager())
                )
            finally:
                loop.close()

    def _skip_unless_ready(self):
        import pytest
        from app.config import settings

        if not _HAS_NODE:
            pytest.skip("未安装 node")
        if not os.path.exists(
            os.path.join(settings.project_root, "node_modules", "playwright", "package.json")
        ):
            pytest.skip("项目根目录未安装 playwright")

    def test_nested_comments_are_extracted_with_parent_links(self):
        self._skip_unless_ready()
        data = self._run(incremental=False)

        assert data["complete"] is True, f"采集未完成: {data.get('stop_reason')}"
        posts = data["posts"]
        roots = [p for p in posts if not p["parent_fingerprint"]]
        comments = [p for p in posts if p["parent_fingerprint"]]
        assert len(roots) == 4, [p["username"] for p in roots]
        assert len(comments) == 4

        # 每条评论的 parent_fingerprint 必须真的指向本批数据里的某个主贴
        by_fp = {p["fingerprint"]: p for p in posts}
        for c in comments:
            assert c["parent_fingerprint"] in by_fp
            assert by_fp[c["parent_fingerprint"]]["parent_fingerprint"] is None
            assert c["reply_level"] == 1

        # 空用户名要落到兜底命名，不能是空字符串
        assert all(p["username"] for p in posts)
        # 时间统一成落盘格式 dd-mm-yyyy HH:MM
        assert posts[0]["timestamp"] == "02-06-2026 09:14"

    def test_incremental_rerun_keeps_translations(self):
        """增量重跑绝不能整体覆盖旧文件。

        落盘文件同时承载 translation 和 _processed 标记，覆盖等于把已翻译的帖子
        重新变成新帖 —— 下一轮再付一次翻译钱，舆情也重算一遍。
        """
        self._skip_unless_ready()
        self._run(incremental=False)

        # 模拟翻译步骤写回：给第一条挂上译文和已处理标记
        with open(self.output, "r", encoding="utf-8") as f:
            data = json.load(f)
        first_fp = data["posts"][0]["fingerprint"]
        data["posts"][0]["translation"] = "我已经用了三个月"
        data["posts"][0]["_processed"] = {"translated": True}
        with open(self.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        again = self._run(incremental=True)

        assert len(again["posts"]) == 8, "增量重跑后帖子数变了，说明历史数据被覆盖"
        kept = next(p for p in again["posts"] if p["fingerprint"] == first_fp)
        assert kept["translation"] == "我已经用了三个月", "译文被增量重跑抹掉了"
        assert kept["_processed"]["translated"] is True, "_processed 标记被抹掉了"


class TestFacebookLoginEndToEnd:
    """登录 / 会话复用 / 两步验证退出路径 —— 真 Chrome 打真带登录门的本地站点。

    facebook.com 本身不在这里跑：一次失败的自动登录很可能把真账号推进 checkpoint，
    那是不可逆的对外动作。这里验的是脚本用的**同一套选择器和同一条代码路径**，
    fixture 站点有真表单、真 Set-Cookie、真重定向，不涉及任何 mock。
    """

    GROUP_ID = "2407063016436085"

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output = os.path.join(self.tmpdir, "fb_out.json")
        self.state = os.path.join(self.tmpdir, "session.json")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _skip_unless_ready(self):
        import pytest
        from app.config import settings

        if not _HAS_NODE:
            pytest.skip("未安装 node")
        if not os.path.exists(
            os.path.join(settings.project_root, "node_modules", "playwright", "package.json")
        ):
            pytest.skip("项目根目录未安装 playwright")

    def _run(self, base_url, username, password, incremental=False):
        import asyncio
        from app.collectors import get_collector
        from app.services.collector_runner import CollectorRunner
        from app.services.progress_manager import ProgressManager

        collector = type(get_collector("facebook_group"))()
        collector.output_path = lambda source: self.output
        # 凭据只走子进程环境变量，绝不进 argv 和 job 文件
        os.environ["HYXI_CRED_USERNAME"] = username
        os.environ["HYXI_CRED_PASSWORD"] = password
        try:
            source = {
                "id": "fixture_fb",
                "collector_id": "facebook_group",
                "name": "FB fixture",
                "params": {
                    "group_id": self.GROUP_ID,
                    "base_url": base_url,
                    "headless": True,
                    "incremental": incremental,
                    "max_batches": 1,
                },
                "state_file": self.state,
                "pacing": {"delay_min": 200, "delay_max": 400},
            }
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    CollectorRunner.execute("fb-e2e", collector, source, ProgressManager())
                )
            finally:
                loop.close()
        finally:
            os.environ.pop("HYXI_CRED_USERNAME", None)
            os.environ.pop("HYXI_CRED_PASSWORD", None)

    def _login_site(self):
        sys.path.insert(0, _FIXTURES_DIR)
        import login_site
        return login_site

    def test_credential_login_creates_reusable_session(self):
        """(a) 首次凭据登录成功并落会话文件"""
        self._skip_unless_ready()
        site = self._login_site()

        with site.LoginSite() as base_url:
            data = self._run(base_url, site.GOOD_USER, site.GOOD_PASSWORD)

        assert data["complete"] is True, data.get("stop_reason")
        assert len(data["posts"]) == 3          # 2 主贴 + 1 评论
        assert os.path.exists(self.state), "会话文件没有落盘，下一轮还得再输一次密码"

        roots = [p for p in data["posts"] if not p["parent_fingerprint"]]
        comments = [p for p in data["posts"] if p["parent_fingerprint"]]
        assert len(roots) == 2 and len(comments) == 1
        assert comments[0]["reply_level"] == 1
        # data-utime 是 epoch 秒，要落成 dd-mm-yyyy HH:MM
        import re
        assert re.match(r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}$", roots[0]["timestamp"])

    def test_session_is_reused_without_password(self):
        """(c) 保留会话重跑：密码给成错的也应该照样成功，证明这一轮根本没走登录"""
        self._skip_unless_ready()
        site = self._login_site()

        with site.LoginSite() as base_url:
            self._run(base_url, site.GOOD_USER, site.GOOD_PASSWORD)
            assert os.path.exists(self.state)
            data = self._run(base_url, site.GOOD_USER, "这个密码是错的")

        assert data["complete"] is True, data.get("stop_reason")
        assert len(data["posts"]) == 3

    def test_deleting_session_falls_back_to_credentials(self):
        """(b) 删掉会话重跑仍能成功"""
        self._skip_unless_ready()
        site = self._login_site()

        with site.LoginSite() as base_url:
            self._run(base_url, site.GOOD_USER, site.GOOD_PASSWORD)
            os.remove(self.state)
            data = self._run(base_url, site.GOOD_USER, site.GOOD_PASSWORD)

        assert data["complete"] is True, data.get("stop_reason")
        assert os.path.exists(self.state)

    def test_two_factor_raises_manual_auth_with_actionable_message(self):
        """撞上两步验证要以退出码 3 交回给人，并给出「去哪点哪个按钮」的人话"""
        self._skip_unless_ready()
        import pytest
        from app.services.collector_runner import ManualAuthRequired

        site = self._login_site()
        with site.LoginSite() as base_url:
            with pytest.raises(ManualAuthRequired) as e:
                self._run(base_url, site.TWO_FACTOR_USER, "任意密码")

        assert "两步验证" in e.value.reason
        message = str(e.value)
        assert "FB fixture" in message
        assert "人工登录" in message, f"没告诉用户去哪操作: {message}"
        assert not os.path.exists(self.output), "没登进去却写出了结果文件"

    def test_bad_credentials_raise_manual_auth(self):
        self._skip_unless_ready()
        import pytest
        from app.services.collector_runner import ManualAuthRequired

        site = self._login_site()
        with site.LoginSite() as base_url:
            with pytest.raises(ManualAuthRequired) as e:
                self._run(base_url, "wrong@example.com", "wrong")

        assert "密码" in e.value.reason

    def _run_login_only(self, base_url, timeout_ms):
        import asyncio
        from app.collectors import get_collector
        from app.services.collector_runner import CollectorRunner
        from app.services.progress_manager import ProgressManager

        collector = type(get_collector("facebook_group"))()
        collector.output_path = lambda source: self.output
        source = {
            "id": "fixture_fb",
            "collector_id": "facebook_group",
            "name": "FB fixture",
            "mode": "login_only",
            "params": {"group_id": self.GROUP_ID, "base_url": base_url},
            "state_file": self.state,
            "manual_login_timeout_ms": timeout_ms,
            "pacing": {"delay_min": 200, "delay_max": 400},
        }
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                CollectorRunner.execute("fb-auth", collector, source, ProgressManager())
            )
        finally:
            loop.close()

    def test_manual_auth_saves_session_when_window_is_logged_in(self):
        """人工授权模式：窗口里已是登录态时保存会话并成功退出。

        真实场景里「变成登录态」这一步是人在窗口里过两步验证；这里先用凭据登录一次
        拿到真会话，再跑 login_only —— 走的是同一条 waitForManualLogin 成功分支。
        """
        self._skip_unless_ready()
        site = self._login_site()

        with site.LoginSite() as base_url:
            self._run(base_url, site.GOOD_USER, site.GOOD_PASSWORD)
            assert os.path.exists(self.state)
            os.remove(self.output)
            result = self._run_login_only(base_url, timeout_ms=30000)

        assert result == {"mode": "login_only", "authorized": True}
        assert os.path.exists(self.state), "授权成功却没落会话"
        assert not os.path.exists(self.output), "人工授权模式不该产出帖子数据"

    def test_manual_auth_times_out_with_actionable_message(self):
        """没人去点时必须超时收场，不能永久挂住一个有头浏览器"""
        self._skip_unless_ready()
        import pytest
        from app.services.collector_runner import ManualAuthRequired

        site = self._login_site()
        with site.LoginSite() as base_url:
            with pytest.raises(ManualAuthRequired) as e:
                self._run_login_only(base_url, timeout_ms=6000)

        assert "超时" in e.value.reason
        assert "人工登录" in str(e.value)

    def test_missing_credentials_ask_for_manual_login_instead_of_crashing(self):
        """没配凭据、会话又失效时，要给「去点人工登录」而不是一句技术报错。

        取凭据失败在 CollectorRunner 里是被吞掉的（按无凭据处理），就是为了让脚本走到
        这条路 —— 在那里抛只会变成一个看不懂的 OperationalError 把可操作提示盖掉。
        """
        self._skip_unless_ready()
        import pytest
        from app.services.collector_runner import ManualAuthRequired

        site = self._login_site()
        with site.LoginSite() as base_url:
            with pytest.raises(ManualAuthRequired) as e:
                self._run(base_url, "", "")   # 环境变量为空 = 库里没有凭据

        assert "未配置凭据" in e.value.reason
        # orchestrator 靠这两个字段给出人话并在界面上标出是哪个源要重新授权
        assert e.value.source_id == "fixture_fb"
        assert "人工登录" in str(e.value)

    def test_password_never_reaches_argv_or_job_file(self):
        """凭据只走环境变量：进程命令行和 job 文件里都不能出现"""
        from app.collectors import get_collector

        collector = get_collector("facebook_group")
        source = {
            "id": "fixture_fb", "collector_id": "facebook_group",
            "params": {"group_id": self.GROUP_ID},
        }
        job = collector.build_job(source, self.output)
        assert "HYXI_CRED_PASSWORD" not in json.dumps(job)
        assert "password" not in json.dumps(job).lower()
        # 命令行只有脚本路径和 --job=
        assert collector.script_path().endswith("facebook_group.js")


class TestSessionStateReflectsReality:
    """会话失效后，界面上的「会话正常」徽标必须翻回去。

    last_auth_at 只在授权成功时写入。若没有任何地方清除它，会话过期后界面会一直显示
    「会话正常」—— 而采集恰恰正因为会话失效在失败，用户被指向错误的排查方向。
    退出码 3 是脚本对「这个会话不管用了」的权威判定，直接拿来用，不去猜会话文件的有效期。
    """

    GROUP_ID = "2407063016436085"

    def setup_method(self):
        import app.services.storage as storage_module

        self.tmpdir = tempfile.mkdtemp()
        self.output = os.path.join(self.tmpdir, "fb_out.json")
        self.state = os.path.join(self.tmpdir, "session.json")
        # DB_PATH 是 import 时算好的常量，不重定向就会写进真实的 backend/data/hyxi.db
        self._orig_db = storage_module.DB_PATH
        storage_module.DB_PATH = os.path.join(self.tmpdir, "hyxi.db")
        storage_module.init_db()

    def teardown_method(self):
        import app.services.storage as storage_module

        storage_module.DB_PATH = self._orig_db
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _skip_unless_ready(self):
        import pytest
        from app.config import settings

        if not _HAS_NODE:
            pytest.skip("未安装 node")
        if not os.path.exists(
            os.path.join(settings.project_root, "node_modules", "playwright", "package.json")
        ):
            pytest.skip("项目根目录未安装 playwright")

    def _collect(self, source: dict, base_url: str, username: str, password: str):
        import asyncio
        from app.collectors import get_collector
        from app.services.collector_runner import CollectorRunner
        from app.services.progress_manager import ProgressManager

        collector = type(get_collector("facebook_group"))()
        collector.output_path = lambda s: self.output
        job_source = dict(source)
        job_source["params"] = {
            **source["params"],
            "base_url": base_url,
            "headless": True,
            "incremental": False,
        }
        job_source["state_file"] = self.state
        job_source["pacing"] = {"delay_min": 200, "delay_max": 400}

        os.environ["HYXI_CRED_USERNAME"] = username
        os.environ["HYXI_CRED_PASSWORD"] = password
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                CollectorRunner.execute("badge-e2e", collector, job_source, ProgressManager())
            )
        finally:
            loop.close()
            os.environ.pop("HYXI_CRED_USERNAME", None)
            os.environ.pop("HYXI_CRED_PASSWORD", None)

    def _authorized_source(self):
        from app.services import source_service

        source = source_service.create_source(
            collector_id="facebook_group",
            name="FB 会话状态回归",
            params={"group_id": self.GROUP_ID, "max_batches": 1},
        )
        source_service.mark_authorized(source["id"])
        assert source_service.get_source(source["id"])["last_auth_at"], "前置条件：先有一次成功授权"
        return source

    def test_manual_auth_required_clears_last_auth_at(self):
        """撞上两步验证 → 会话状态必须翻回「需重新授权」"""
        self._skip_unless_ready()
        import pytest
        from app.services import source_service
        from app.services.collector_runner import ManualAuthRequired

        sys.path.insert(0, _FIXTURES_DIR)
        import login_site

        source = self._authorized_source()
        with login_site.LoginSite() as base_url:
            with pytest.raises(ManualAuthRequired):
                self._collect(source, base_url, login_site.TWO_FACTOR_USER, "任意密码")

        assert source_service.get_source(source["id"])["last_auth_at"] is None, (
            "会话已经不管用了，界面上却还会显示「会话正常」"
        )

    def _authorize(self, source: dict, base_url: str, timeout_ms: int):
        """跑一轮人工授权（login_only）。超时上限可配就是为了在验证里跑短一点"""
        import asyncio
        from app.collectors import get_collector
        from app.services.collector_runner import CollectorRunner
        from app.services.progress_manager import ProgressManager

        collector = type(get_collector("facebook_group"))()
        collector.output_path = lambda s: self.output
        job_source = dict(source)
        job_source["params"] = {**source["params"], "base_url": base_url}
        job_source["state_file"] = self.state
        job_source["mode"] = "login_only"
        job_source["manual_login_timeout_ms"] = timeout_ms

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                CollectorRunner.execute("auth-e2e", collector, job_source, ProgressManager())
            )
        finally:
            loop.close()

    def test_manual_auth_timeout_reports_configured_duration(self):
        """人工授权等到超时 → 退出码 3，且提示里的时长必须按实际配置算。

        这句话会原样变成界面上的失败提示，而页面同时在跑一个按同一配置走的倒计时。
        写死「5 分钟」会让两者对不上，用户以为超时判定坏了。
        """
        self._skip_unless_ready()
        import pytest
        from app.services import source_service
        from app.services.collector_runner import ManualAuthRequired

        sys.path.insert(0, _FIXTURES_DIR)
        import login_site

        source = self._authorized_source()
        with login_site.LoginSite() as base_url:
            with pytest.raises(ManualAuthRequired) as exc:
                self._authorize(source, base_url, 5000)

        assert "等待人工登录超时" in exc.value.reason
        assert "5 分钟" not in exc.value.reason, (
            f"时长写死了，没跟着配置走: {exc.value.reason}"
        )
        assert source_service.get_source(source["id"])["last_auth_at"] is None, (
            "授权超时也意味着会话没拿到，徽标必须翻回「需重新授权」"
        )

    def test_successful_collect_keeps_last_auth_at(self):
        """反向保证：采集成功时不能把授权时间抹掉，否则徽标会反过来误报失效"""
        self._skip_unless_ready()
        from app.services import source_service

        sys.path.insert(0, _FIXTURES_DIR)
        import login_site

        source = self._authorized_source()
        before = source_service.get_source(source["id"])["last_auth_at"]
        with login_site.LoginSite() as base_url:
            data = self._collect(source, base_url, login_site.GOOD_USER, login_site.GOOD_PASSWORD)

        assert data["complete"] is True, data.get("stop_reason")
        assert source_service.get_source(source["id"])["last_auth_at"] == before
