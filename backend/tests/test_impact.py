"""按改动精准选测（scripts/test_impact.py）的规则。

选测漏掉一条，就是把没验过的改动当成验过了 —— 这里钉的是「宁多勿少」的每一条，外加两道护栏：
没有哪个源文件会被当成文档静默跳过；提交进仓库的映射还认得这次采集修复里最要紧的那些依赖。
"""

import importlib.util
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("test_impact_tool", os.path.join(ROOT, "scripts", "test_impact.py"))
impact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(impact)

FB = "backend/tests/test_core.py::TestFacebookLoginEndToEnd"
FAKE_MAP = {
    "tests": [f"{FB}::test_a", f"{FB}::test_b", "backend/tests/test_api.py::TestApi::test_c"],
    "files": {"collectors/facebook_group.js": [0, 1], "backend/app/services/storage.py": [0, 1, 2]},
}
ALL = list(FAKE_MAP["tests"])

TEST_SOURCE = '''import os


def helper():
    return 1


class TestOne:
    def _setup(self):
        return 2

    def test_first(self):
        assert helper()

    @pytest.mark.skip
    def test_second(self):
        assert True
'''
REL = "backend/tests/test_sample.py"


def _select(paths, lines=None, all_tests=ALL, impact_map=FAKE_MAP):
    return impact.select(paths, (lambda p: lines), impact_map, all_tests, read_source=lambda p: TEST_SOURCE)


class TestImpactSelection:
    def test_docs_and_skills_run_nothing(self):
        res = _select(["docs/fixes/x.md", ".claude/skills/hyxi-gotchas/SKILL.md", "CLAUDE.md"])
        assert not res["full"] and not res["tests"]

    def test_a_mapped_file_selects_exactly_the_tests_that_executed_it(self):
        res = _select(["collectors/facebook_group.js"])
        assert not res["full"]
        assert res["tests"] == {f"{FB}::test_a", f"{FB}::test_b"}

    def test_a_source_file_the_map_does_not_know_forces_a_full_run(self):
        res = _select(["backend/app/services/brand_new.py"])
        assert res["full"], "映射里没有的源文件没触发全量：新文件上的改动没有任何用例验过"

    def test_without_a_map_every_source_change_is_a_full_run(self):
        res = _select(["collectors/facebook_group.js"], impact_map=None)
        assert res["full"]

    @pytest.mark.parametrize("path", ["backend/tests/conftest.py", "backend/app/config.py", "backend/app/paths.py",
                                      "backend/requirements.txt", "package.json", "package-lock.json",
                                      "scripts/test_impact.py", "some/unknown/file.txt"])
    def test_foundation_and_unknown_files_force_a_full_run(self, path):
        # 映射里给它也放上条目：否则「映射里没有 → 全量」那条会替地基规则兜住，删掉地基规则也看不出来
        mapped_too = {"tests": FAKE_MAP["tests"], "files": {**FAKE_MAP["files"], path: [0]}}
        assert _select([path], impact_map=mapped_too)["full"], f"{path} 没触发全量"

    def test_tests_added_after_the_map_was_built_always_run(self):
        fresh = f"{FB}::test_added_later"
        res = _select(["collectors/facebook_group.js"], all_tests=ALL + [fresh])
        assert fresh in res["tests"], "映射建好之后新增的用例没被选上：映射里还没有它执行过什么"

    def test_change_inside_a_test_selects_that_test_only(self):
        res = impact.tests_for_changed_lines(REL, TEST_SOURCE, {13})
        assert res == {f"{REL}::TestOne::test_first"}

    def test_change_on_a_decorator_belongs_to_the_decorated_test(self):
        res = impact.tests_for_changed_lines(REL, TEST_SOURCE, {15})
        assert res == {f"{REL}::TestOne::test_second"}

    def test_change_in_a_class_helper_selects_the_whole_class(self):
        assert impact.tests_for_changed_lines(REL, TEST_SOURCE, {10}) == {f"{REL}::TestOne"}

    def test_change_at_module_level_selects_the_whole_file(self):
        assert impact.tests_for_changed_lines(REL, TEST_SOURCE, {5}) == {REL}

    def test_new_test_file_selects_the_whole_file(self):
        assert impact.tests_for_changed_lines(REL, TEST_SOURCE, None) == {REL}

    def test_a_map_older_than_other_source_changes_forces_a_full_run(self):
        """映射建好之后别处的源码改过、已经提交了（比如路由新调用了 post_tree.py），映射里没有这条新依赖 ——
        再改 post_tree.py 时按映射选就会漏掉那些接口用例，而且结论照样显示「选中 N 条」，看不出少了"""
        res = impact.select(["collectors/facebook_group.js"], (lambda p: None), FAKE_MAP, ALL,
                            stale=["backend/app/routers/results.py"])
        assert res["full"], "映射过期了还在按它选测"

    def test_a_stale_map_does_not_turn_a_docs_only_change_into_a_full_run(self):
        res = impact.select(["docs/fixes/x.md"], (lambda p: None), FAKE_MAP, ALL, stale=["backend/app/routers/results.py"])
        assert not res["full"] and not res["tests"]

    def test_stale_files_are_the_ones_changed_since_the_build_outside_this_change(self, tmp_path):
        for name in ("a.py", "b.py", "gone.py"):
            (tmp_path / name).write_text(f"{name} = 1", encoding="utf-8")
        hashes = {name: impact.file_hash(str(tmp_path / name)) for name in ("a.py", "b.py", "gone.py")}
        (tmp_path / "a.py").write_text("a = 2", encoding="utf-8")      # 建映射之后改过、已提交
        (tmp_path / "b.py").write_text("b = 2", encoding="utf-8")      # 这次正在改的
        (tmp_path / "gone.py").unlink()                                # 建映射之后删掉的
        (tmp_path / "new.py").write_text("n = 1", encoding="utf-8")    # 建映射之后加的
        stale = impact.stale_files({"hashes": hashes}, changed={"b.py"}, root=str(tmp_path),
                                   current=["a.py", "b.py", "new.py"])
        assert stale == ["a.py", "gone.py", "new.py"]

    def test_a_line_ending_only_change_does_not_make_the_map_stale(self, tmp_path):
        """仓库开着 autocrlf：git 检出会把 LF 换成 CRLF，内容没变 —— 判成过期的话每次选测都白白全量"""
        (tmp_path / "a.py").write_bytes(b"x = 1\ny = 2\n")
        hashes = {"a.py": impact.file_hash(str(tmp_path / "a.py"))}
        (tmp_path / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")
        assert impact.stale_files({"hashes": hashes}, changed=set(), root=str(tmp_path), current=["a.py"]) == []

    def test_a_mapped_file_whose_tests_are_all_gone_forces_a_full_run(self):
        gone = {"tests": ["backend/tests/test_core.py::TestRemoved::test_x"], "files": {"collectors/facebook_group.js": [0]}}
        res = _select(["collectors/facebook_group.js"], impact_map=gone)
        assert res["full"], "映射里关联的用例都删光了，却给出「没有要跑的用例」"

    def test_tests_that_cannot_be_collected_force_a_full_run(self):
        """正在改的测试文件有语法错误时收集会失败 —— 那正是拿不准的时候"""
        res = impact.select(["backend/tests/test_core.py"], (lambda p: None), FAKE_MAP, None,
                            read_source=lambda p: TEST_SOURCE, collect_error="收集用例失败")
        assert res["full"]

    def test_frontend_and_build_changes_point_to_their_own_checks(self):
        res = _select(["frontend/src/views/ResultsView.vue", "frontend/e2e/results_thread_structure.js",
                       "build/build.ps1"])
        assert not res["full"] and not res["tests"]
        notes = "\n".join(res["notes"])
        assert "e2e:thread" in notes and "verify_package" in notes


class TestImpactGuards:
    def test_no_tracked_source_file_is_silently_ignored(self):
        """后来有人往忽略名单里加了个太宽的模式，源文件就会被当成文档跳过 —— 改了它一条用例都不跑"""
        tracked = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, encoding="utf-8").stdout.splitlines()
        sources = [p for p in tracked if p.startswith(("backend/", "collectors/")) and not p.endswith(".md")
                   and p != "backend/tests/impact_map.json"]   # 映射本身是生成物
        assert len(sources) > 30, "前置条件：没拿到受跟踪的源文件，这一条等于没测"
        leaked = [(p, impact.classify(p)) for p in sources if impact.classify(p)[0] not in ("mapped", "full", "tests_file")]
        assert leaked == [], f"这些源文件被当成不必跑测试：{leaked}"

    def test_the_committed_map_still_knows_the_critical_edges(self):
        """映射是一次真实全量里记下来的。重建时哪一路记录坏了（node 覆盖率没开、文本引用没补），
        这里的依赖就会少掉 —— 那之后按映射选测会漏跑这些用例。每条都是这次采集修复里真出过问题的地方"""
        impact_map = impact.load_map()
        assert impact_map is not None, "backend/tests/impact_map.json 不存在：先跑 python scripts/test_impact.py build"
        tests = impact_map["tests"]

        def mapped(path):
            return {tests[i] for i in impact_map["files"].get(path, [])}

        def has(path, name):
            return any(t.endswith("::" + name) or ("::" + name + "::") in t for t in mapped(path))

        fb_tests = [t for t in tests if t.startswith(FB + "::")]
        assert fb_tests, "前置条件：映射里没有 Facebook 端到端用例"
        missing = [t for t in fb_tests if t not in mapped("collectors/facebook_group.js")]
        assert missing == [], f"facebook_group.js 没关联上这些用例（node 加载记录坏了？）：{missing}"
        edges = [
            ("collectors/facebook_group.js", "TestThreadDialogExtractionEndToEnd"),        # 当文本读进来塞给浏览器
            ("collectors/lib/http.js", "test_rate_limit_backoff_that_would_run_past_the_deadline_stops_right_away"),
            ("collectors/lib/http.js", "test_a_throttled_site_is_not_hammered_by_the_network_retry"),
            ("collectors/tweakers.js", "test_extraction_matches_golden_baseline"),
            ("collectors/group_feed.js", "test_nested_comments_are_extracted_with_parent_links"),
            ("collectors/lib/auth.js", "test_browser_requests_chinese_ui"),
            ("backend/tests/fixtures/login_site.py", "test_dialog_whose_address_has_no_trailing_slash_is_harvested"),
            ("backend/app/services/storage.py", "test_thread_counts_refresh_a_root_the_collector_does_not_send_again"),
            ("backend/app/services/storage.py", "test_a_leftover_dialog_root_is_merged_into_its_real_post"),
            ("backend/app/services/collector_runner.py", "test_job_carries_a_deadline_ahead_of_the_kill"),
            # 接口用例经 TestClient 调路由，路由跑在它自己开的线程里：按线程切覆盖率上下文会整批记不上（实测 93 条只剩 8 条）
            ("backend/app/routers/results.py", "test_root_carries_the_site_comment_count_and_replies_do_not"),
            ("backend/app/routers/sources.py", "TestSourcesAPIEndToEnd"),
            # 被当数据读的：一个经模块级常量 GOLDEN_FILE 引用，一个由 fixture 站点按目录读
            ("backend/tests/fixtures/golden_tweakers.json", "test_extraction_matches_golden_baseline"),
            ("backend/tests/fixtures/group_site/batch_0.html", "test_nested_comments_are_extracted_with_parent_links"),
        ]
        lost = [(p, n) for p, n in edges if not has(p, n)]
        assert lost == [], f"映射丢了这些依赖：{lost}"
