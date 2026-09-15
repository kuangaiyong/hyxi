"""按改动精准选测 —— 规则与用法见 CLAUDE.md「测试」一节。

    python scripts/test_impact.py select [--base HEAD] [--paths 文件 ...]   打印选中了哪些、为什么
    python scripts/test_impact.py run    [--base HEAD] [--paths 文件 ...] [-n 3]   选完直接跑
    python scripts/test_impact.py build  [-n 3]                            重建映射（一次带覆盖率的全量）

**宁多勿少，拿不准就全量**：
- 映射（backend/tests/impact_map.json）记的是「哪条用例执行过哪个文件」，由 build 从真实运行里记下来，不手写。
  **按进程归属，不按线程上下文**：接口用例经 TestClient 调路由，路由跑在它自己开的线程里，coverage 按用例切上下文
  只管当前线程 —— 实测那样建出来的映射里 93 条接口用例只有 8 条关联得上文件，改路由会漏跑全部接口用例。
  所以 build 把用例分成小进程跑（有 setup_class 的类整类一个进程，其余每 5 条一个），进程里任何线程执行过的文件
  都归给这几条用例；采集脚本跑在 node 子进程里，按进程设 NODE_V8_COVERAGE 记下加载过的 collectors/*.js；
  被当数据读的（fixture 的 HTML / JSON、塞给浏览器的采集脚本源码）按文本引用补上
- 改了映射里没有的源文件（新文件、没有任何用例执行到的文件）、测试地基（pytest.ini、conftest、依赖清单、
  config / paths、本脚本）→ 全量
- 映射建好之后新增的用例一律跑（映射里还没有它们执行过什么）
- 改了测试文件 → 跑改动落在其中的用例；改在类的辅助方法上跑整个类，改在模块顶层跑整个文件
- 前端、打包、文档不进 pytest：前端改动提示跑哪几条 E2E，打包改动提示验包
"""

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "backend", ".venv", "Scripts", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
MAP_PATH = os.path.join(ROOT, "backend", "tests", "impact_map.json")
TESTS_DIR = "backend/tests"
MAP_CHECK = "backend/tests/test_impact.py::TestImpactGuards::test_the_committed_map_still_knows_the_critical_edges"
GROUP_SIZE = 5

# 改了就全量：所有用例都踩在它们上面，或者它们决定了用例怎么跑
FULL_FILES = {
    "pytest.ini",
    "backend/tests/conftest.py",
    "backend/requirements.txt",
    "package.json",
    "package-lock.json",
    "backend/app/config.py",
    "backend/app/paths.py",
    "scripts/test_impact.py",
}
IGNORE_PATTERNS = ["docs/*", ".claude/*", "*.md", ".gitignore", ".codegraph/*", "backend/tests/impact_map.json"]
FRONTEND_E2E = {
    "frontend/e2e/results_filters.js": "e2e",
    "frontend/e2e/sentiment_time_column.js": "e2e:sentiment",
    "frontend/e2e/results_source_link.js": "e2e:link",
    "frontend/e2e/stale_bundle_navigation.js": "e2e:stale",
    "frontend/e2e/access_key_flow.js": "e2e:key",
    "frontend/e2e/results_thread_structure.js": "e2e:thread",
}
# 按映射查的：会被用例执行或读到的东西
MAPPED_PATTERNS = ["backend/*.py", "backend/app/*", "collectors/*", "backend/tests/fixtures/*"]


def _git(*args):
    out = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{out.stderr.strip()}")
    return out.stdout


def changed_paths(base):
    """相对 base 改过的文件（工作区 + 暂存区 + 未跟踪），重命名两头都算"""
    paths = set()
    for line in _git("diff", "--name-status", base).splitlines():
        parts = line.split("\t")
        paths.update(p for p in parts[1:] if p)
    paths.update(p for p in _git("ls-files", "--others", "--exclude-standard").splitlines() if p)
    return sorted(paths)


def classify(path):
    """返回 (类别, 说明)。类别：ignore / full / frontend / package / manual / tests_file / mapped"""
    if path in FULL_FILES:
        return "full", "测试地基"
    if any(fnmatch.fnmatch(path, p) for p in IGNORE_PATTERNS):
        return "ignore", "文档 / 说明"
    if path in FRONTEND_E2E:
        return "frontend", f"npm run {FRONTEND_E2E[path]}"
    if path.startswith("frontend/"):
        return "frontend", "npm run build（类型检查）+ 六条前端 E2E"
    if path.startswith("build/"):
        return "package", ".\\build\\build.ps1 + .\\build\\verify_package.ps1"
    if path == "start.ps1":
        return "manual", "跑一遍 .\\start.ps1 看自检与三段链路"
    if re.fullmatch(r"backend/tests/test_[^/]+\.py", path):
        return "tests_file", "改动落在哪些用例上"
    if any(fnmatch.fnmatch(path, p) for p in MAPPED_PATTERNS):
        return "mapped", "查映射"
    return "full", "不认识的文件，拿不准就全量"


def test_ranges(source):
    """测试文件里每个 Test 类与 test 方法的行号范围：{类名: (起, 止, {方法名: (起, 止)})}"""
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            methods = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    start = min([item.lineno] + [d.lineno for d in item.decorator_list])
                    methods[item.name] = (start, item.end_lineno)
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            out[node.name] = (start, node.end_lineno, methods)
    return out


def tests_for_changed_lines(rel_path, source, lines):
    """改动行号 → 选中的 nodeid 前缀。lines 为 None 表示整个文件都算改了（新文件）"""
    ranges = test_ranges(source)
    if lines is None:
        return {rel_path}
    selected = set()
    for ln in lines:
        owner = next(((name, r) for name, r in ranges.items() if r[0] <= ln <= r[1]), None)
        if owner is None:
            return {rel_path}   # 改在模块顶层（import、共用辅助函数）：整个文件
        name, (_, _, methods) = owner
        hit = next((m for m, (s, e) in methods.items() if s <= ln <= e), None)
        selected.add(f"{rel_path}::{name}::{hit}" if hit else f"{rel_path}::{name}")
    return selected


def changed_lines(rel_path, base):
    """相对 base 改动的新文件行号；文件是新加的（未跟踪 / base 里没有）返回 None"""
    if rel_path not in set(_git("ls-files").splitlines()) or not _git("ls-tree", "--name-only", base, rel_path).strip():
        return None
    lines = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", _git("diff", "-U0", base, "--", rel_path), re.M):
        start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        # 纯删除（count=0）落在 start 那一行之后，算它所在的位置
        lines.update(range(start, start + count) if count else [max(start, 1)])
    return lines


def collected_tests():
    out = subprocess.run([PY, "-m", "pytest", TESTS_DIR, "--collect-only", "-q", "-p", "no:cacheprovider"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    ids = [l.strip() for l in out.stdout.splitlines() if "::" in l]
    if out.returncode != 0 or not ids:
        raise RuntimeError("收集用例失败：\n" + (out.stdout + out.stderr)[-2000:])
    return ids


def load_map():
    if not os.path.exists(MAP_PATH):
        return None
    with open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def file_hash(path):
    """内容哈希，行尾先统一成 LF：仓库开着 autocrlf，git 检出时会把 LF 换成 CRLF，只换了行尾不该判成映射过期"""
    with open(path, "rb") as f:
        return hashlib.sha1(f.read().replace(b"\r\n", b"\n")).hexdigest()


def hashed_files():
    """决定映射里那些依赖的文件：源文件、fixture、测试文件（含未跟踪的新文件）"""
    return sorted(p for p in _git("ls-files", "-co", "--exclude-standard").splitlines()
                  if classify(p)[0] in ("mapped", "tests_file"))


def stale_files(impact_map, changed, root=ROOT, current=None):
    """映射建好之后内容变过、却不在这次改动里的文件。有的话映射里的依赖可能已经不对了（比如路由新调用了
    post_tree.py 且已提交）—— 再按它选测会静默漏跑，结论照样显示「选中 N 条」"""
    hashes = (impact_map or {}).get("hashes")
    if hashes is None:
        return ["（映射里没有文件哈希，是旧格式）"]
    current = hashed_files() if current is None else current
    out = []
    for p in sorted(set(current) | set(hashes)):
        if p in changed:
            continue
        full = os.path.join(root, p)
        if not os.path.exists(full):
            if p in hashes:
                out.append(p)   # 建映射之后删掉的
        elif hashes.get(p) != file_hash(full):
            out.append(p)       # 改过的，或建映射之后才加的
    return out


def select(paths, base, impact_map, all_tests, read_source=None, stale=(), collect_error=None):
    """核心选择逻辑（纯函数，便于单测）。返回 dict：full（原因列表）、tests（nodeid 集合）、notes（非 pytest 的提示）。
    base 可以是提交名，也可以是「路径 → 改动行号」的函数（单测用）；stale 见 stale_files；collect_error 是收集用例失败的原因"""
    read_source = read_source or (lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read())
    result = {"full": [], "tests": set(), "notes": [], "by_path": {}}
    files = (impact_map or {}).get("files", {})
    mapped_tests = (impact_map or {}).get("tests", [])
    existing = set(all_tests) if all_tests is not None else None
    if any(classify(p)[0] in ("mapped", "tests_file") for p in paths):
        # 只有要跑 pytest 的改动才受这两条影响：只改文档时映射过期了也不必全量
        if collect_error:
            result["full"].append(f"收集用例失败（{collect_error}），拿不准就全量")
        if stale:
            result["full"].append(f"映射建好之后这些文件改过、依赖可能变了：{'、'.join(stale[:5])}"
                                  f"{' 等 %d 个' % len(stale) if len(stale) > 5 else ''} —— 先重建映射（build）")
    for path in paths:
        kind, why = classify(path)
        result["by_path"][path] = (kind, why)
        if kind == "full":
            result["full"].append(f"{path}：{why}")
        elif kind in ("frontend", "package", "manual"):
            result["notes"].append(f"{path} → {why}")
        elif kind == "tests_file":
            if not os.path.exists(os.path.join(ROOT, path)):
                continue   # 删掉的测试文件：用例没了，不必跑
            try:
                lines = base(path) if callable(base) else changed_lines(path, base)
                result["tests"].update(tests_for_changed_lines(path, read_source(path), lines))
            except SyntaxError as e:
                result["full"].append(f"{path}：解析不了（{e}），拿不准就全量")
        elif kind == "mapped":
            if impact_map is None:
                result["full"].append(f"{path}：还没有映射（先跑 build）")
            elif path not in files:
                result["full"].append(f"{path}：映射里没有它（新文件，或没有任何用例执行到它）")
            else:
                ids = [mapped_tests[i] for i in files[path]]
                if existing is not None and not any(t in existing for t in ids):
                    result["full"].append(f"{path}：映射里关联的用例都不存在了")
                else:
                    result["tests"].update(ids)
    if impact_map is not None and all_tests is not None:
        known = set(mapped_tests)
        fresh = [t for t in all_tests if t not in known]
        if fresh and (result["tests"] or result["full"]):
            result["tests"].update(fresh)
            result["notes"].append(f"映射建好之后新增的用例 {len(fresh)} 条，一律跑（映射里还没有它们执行过什么）")
    if all_tests is not None and result["tests"]:
        # 展开成当前真实存在的用例：前缀可能是文件 / 类 / 方法；映射里的旧 nodeid 已经不存在的丢掉
        expanded = {t for t in all_tests if any(t == s or t.startswith(s + "::") or t.startswith(s + "[")
                                                 for s in result["tests"])}
        result["tests"] = expanded
    return result


def compact(ids, all_tests):
    """整个类都选中的收成类级 nodeid，命令行短一截"""
    by_class = {}
    for t in all_tests:
        by_class.setdefault("::".join(t.split("::")[:2]), set()).add(t)
    chosen, out = set(ids), []
    for cls, members in sorted(by_class.items()):
        picked = members & chosen
        if not picked:
            continue
        out.extend([cls] if picked == members else sorted(picked))
    return out


def run_pytest(args):
    """经 pytest.main 跑，参数走文件：几百个 nodeid 拼进命令行会撞 Windows 3.2 万字符的上限"""
    fd, argfile = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(args, f, ensure_ascii=False)
    try:
        code = "import json,sys,pytest; sys.exit(pytest.main(json.load(open(sys.argv[1], encoding='utf-8'))))"
        return subprocess.run([PY, "-c", code, argfile], cwd=ROOT,
                              env=dict(os.environ, PYTHONIOENCODING="utf-8")).returncode
    finally:
        os.remove(argfile)


def parallel_args(workers):
    return ["-n", str(workers), "--dist", "loadgroup"] if workers and workers > 1 else []


def cmd_select(args, run=False):
    paths = args.paths if args.paths else changed_paths(args.base)
    impact_map = load_map()
    try:
        all_tests, collect_error = collected_tests(), None
    except RuntimeError as e:
        all_tests, collect_error = None, str(e).strip().splitlines()[0]
    stale = stale_files(impact_map, set(paths)) if impact_map is not None else []
    res = select(paths, args.base, impact_map, all_tests, stale=stale, collect_error=collect_error)
    print(f"改动文件 {len(paths)} 个（相对 {args.base}{'，--paths 指定' if args.paths else ''}）")
    for p in paths:
        kind, why = res["by_path"][p]
        print(f"  [{kind}] {p}  —— {why}")
    for n in res["notes"]:
        print(f"  提示：{n}")
    if res["full"]:
        print("\n结论：全量（" + "；".join(res["full"]) + "）")
        targets = [TESTS_DIR]
    elif res["tests"]:
        targets = compact(res["tests"], all_tests or [])
        print(f"\n结论：选中 {len(res['tests'])} / {len(all_tests)} 条（{len(targets)} 个目标）")
        for t in targets:
            print(f"  {t}")
    else:
        print("\n结论：没有要跑的 pytest 用例")
        targets = []
    if not run or not targets:
        return 0
    return run_pytest(targets + parallel_args(args.workers) + ["-q", "-p", "no:cacheprovider"])


# ---------------------------------------------------------------- build

def _whole_class_owners(all_tests):
    """定义了 setup_class / teardown_class 的类：整类放进一个进程，类级状态和类内顺序照串行时的样子"""
    owners = set()
    for test_file in sorted({t.split("::")[0] for t in all_tests}):
        tree = ast.parse(open(os.path.join(ROOT, test_file), encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(i, (ast.FunctionDef, ast.AsyncFunctionDef)) and i.name in ("setup_class", "teardown_class")
                    for i in node.body):
                owners.add(f"{test_file}::{node.name}")
    return owners


def plan_groups(all_tests, size=GROUP_SIZE):
    """[(命令行目标, 这个进程跑的用例)]：有类级状态的类整类一组，其余按采集顺序每 size 条一组"""
    whole = _whole_class_owners(all_tests)
    by_owner = {}
    for t in all_tests:
        parts = t.split("::")
        by_owner.setdefault("::".join(parts[:2]) if len(parts) > 2 else parts[0], []).append(t)
    groups = []
    for owner, members in by_owner.items():
        if owner in whole:
            groups.append(([owner], members))
        else:
            groups.extend((members[i:i + size], members[i:i + size]) for i in range(0, len(members), size))
    return groups


def _run_group(index, targets, work):
    rc = os.path.join(work, f"rc{index}")
    data_file = os.path.join(work, "cov", f"{index}.coverage")
    node_dir = os.path.join(work, "node", str(index))
    os.makedirs(node_dir, exist_ok=True)
    # source 与 include 同时写时 coverage 只认 source：source 给整个 backend，再把虚拟环境和测试文件本身排除
    # （测试文件的改动按行号选，不查映射）
    with open(rc, "w", encoding="utf-8") as f:
        f.write(f"[run]\ndata_file = {data_file}\nsource =\n    {os.path.join(ROOT, 'backend')}\n"
                "omit =\n    */.venv/*\n    */backend/tests/test_*.py\n    */backend/tests/conftest.py\n")
    # --cov-config 必须用 = 连写：分成两个参数时 pytest 在插件注册选项之前的早期解析里把那个路径当成位置参数，
    # rootdir 被算到盘符根，nodeid 全变成 code/hyxi/backend/…（实测；pytest.ini 现在也钉住了 rootdir）
    proc = subprocess.run([PY, "-m", "pytest", *targets, "--cov", f"--cov-config={rc}", "--cov-report=",
                           "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8", NODE_V8_COVERAGE=node_dir))
    return proc.returncode, (proc.stdout + proc.stderr)[-1500:], data_file, node_dir


def _covered_backend_files(data_file):
    from coverage import CoverageData

    if not os.path.exists(data_file):
        return set()
    data = CoverageData(basename=data_file)
    data.read()
    out = set()
    for measured in data.measured_files():
        rel = os.path.relpath(measured, ROOT).replace("\\", "/")
        if not rel.startswith("..") and data.lines(measured):
            out.add(rel)
    return out


def _loaded_collector_scripts(node_dir):
    out = set()
    for name in os.listdir(node_dir):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(node_dir, name), encoding="utf-8") as f:
            scripts = json.load(f).get("result", [])
        for script in scripts:
            url = script.get("url", "")
            if url.startswith("file:"):
                rel = os.path.relpath(os.path.normpath(unquote(urlparse(url).path).lstrip("/")), ROOT).replace("\\", "/")
                if rel.startswith("collectors/"):
                    out.add(rel)
    return out


def _module_assignments(source):
    """模块顶层的赋值：[(常量名集合, 那条语句的源码)]。GOLDEN_FILE = os.path.join(..., "golden_tweakers.json")
    这种，类里只出现常量名，常量名也得算作对那个文件的引用"""
    tree, lines, out = ast.parse(source), source.splitlines(), []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names:
                out.append((names, "\n".join(lines[node.lineno - 1:node.end_lineno])))
    return out


def _text_references(tracked, all_tests, files_to_tests):
    """按文本引用补映射：测试类 / fixture 模块的源码里出现了某个文件名（或指向它的模块级常量名），就关联上。
    覆盖率只记「执行」，被当数据读的（fixture 的 HTML / JSON、塞给浏览器的采集脚本源码）它看不见"""
    classes, assignments = [], {}   # classes: (类源码, 所在测试文件, 用例集合)
    for test_file in sorted({t.split("::")[0] for t in all_tests}):
        source = open(os.path.join(ROOT, test_file), encoding="utf-8").read()
        lines = source.splitlines()
        assignments[test_file] = _module_assignments(source)
        for cls, (start, end, _) in test_ranges(source).items():
            members = {t for t in all_tests if t.startswith(f"{test_file}::{cls}::")}
            if members:
                classes.append(("\n".join(lines[start - 1:end]), test_file, members))
    fixtures = {p: open(os.path.join(ROOT, p), encoding="utf-8").read()
                for p in tracked if p.startswith("backend/tests/fixtures/") and p.endswith(".py")}
    added = 0
    for path in tracked:
        parent = os.path.basename(os.path.dirname(path))
        needles = {os.path.basename(path)}
        if path.startswith("backend/tests/fixtures/") and parent != "fixtures":
            needles.add(parent)
        aliases = {tf: {n for names, stmt in stmts if any(nd in stmt for nd in needles) for n in names}
                   for tf, stmts in assignments.items()}
        hit = files_to_tests.setdefault(path, set())
        before = len(hit)
        for cls_src, test_file, members in classes:
            if any(n in cls_src for n in needles | aliases[test_file]):
                hit.update(members)
        for fx_path, fx_src in fixtures.items():
            if fx_path != path and any(n in fx_src for n in needles):
                hit.update(files_to_tests.get(fx_path, set()))
        added += len(hit) - before
        if not hit:
            del files_to_tests[path]
    return added


def cmd_build(args):
    work = tempfile.mkdtemp(prefix="hyxi-impact-")
    try:
        return _build(args, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)   # 没全过提前返回时也要清（实测留下过两个覆盖率工作目录）


def _build(args, work):
    started = time.time()
    all_tests = [t for t in collected_tests() if t != MAP_CHECK]   # 健康检查查的正是这次要写出来的映射
    groups = plan_groups(all_tests)
    os.makedirs(os.path.join(work, "cov"), exist_ok=True)
    print(f"建映射：{len(all_tests)} 条用例分成 {len(groups)} 个进程、{args.workers} 个并发（工作目录 {work}）", flush=True)
    files_to_tests, failed, done = {}, [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(_run_group, i, targets, work): members for i, (targets, members) in enumerate(groups)}
        for fut in as_completed(futures):
            members = futures[fut]
            code, tail, data_file, node_dir = fut.result()
            done += 1
            if code != 0:
                failed.append((members, tail))
                print(f"  [{done}/{len(groups)}] 没过：{members[0]} 等 {len(members)} 条", flush=True)
                continue
            for rel in _covered_backend_files(data_file) | _loaded_collector_scripts(node_dir):
                files_to_tests.setdefault(rel, set()).update(members)
            if done % 10 == 0 or done == len(groups):
                print(f"  [{done}/{len(groups)}] 已用 {int(time.time() - started)} 秒", flush=True)
    if failed:
        print(f"\n{len(failed)} 个进程没全过，不写映射（失败用例的覆盖是残缺的，会漏掉依赖）：")
        for members, tail in failed:
            print(f"--- {members[0]} 等 {len(members)} 条\n{tail}")
        return 1

    tracked = [p for p in _git("ls-files").splitlines() if classify(p)[0] == "mapped"]
    text_edges = _text_references(tracked, all_tests, files_to_tests)
    tests_index = sorted(set(all_tests) | {MAP_CHECK})
    pos = {t: i for i, t in enumerate(tests_index)}
    impact_map = {
        "version": 2,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "git_head": _git("rev-parse", "HEAD").strip(),
        "tests": tests_index,
        "files": {f: sorted(pos[t] for t in ts if t in pos) for f, ts in sorted(files_to_tests.items())
                  if classify(f)[0] == "mapped"},
        # 建映射那一刻各文件的内容：之后有文件变了又不在当次改动里，select 就判映射过期、走全量
        "hashes": {p: file_hash(os.path.join(ROOT, p)) for p in hashed_files()},
    }
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(impact_map, f, ensure_ascii=False, indent=0)
        f.write("\n")
    unmapped = [p for p in tracked if p not in impact_map["files"]]
    print(f"\n映射已写入 {os.path.relpath(MAP_PATH, ROOT)}：{len(impact_map['files'])} 个文件、{len(tests_index)} 条用例，"
          f"文本引用补了 {text_edges} 条关联，用时 {int(time.time() - started)} 秒")
    if unmapped:
        print(f"没有任何用例执行到、改了会触发全量的 {len(unmapped)} 个：")
        for p in unmapped:
            print(f"  {p}")
    check = run_pytest([MAP_CHECK, "-q", "-p", "no:cacheprovider"])
    print("映射健康检查：" + ("通过" if check == 0 else "没过 —— 映射缺了关键依赖，别提交它"))
    return check


def main(argv=None):
    ap = argparse.ArgumentParser(description="按改动精准选测")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("select", "run"):
        p = sub.add_parser(name)
        p.add_argument("--base", default="HEAD", help="和哪个提交比（默认 HEAD：只看未提交的改动）")
        p.add_argument("--paths", nargs="*", help="直接指定改动文件，不看 git")
        p.add_argument("-n", "--workers", type=int, default=3)
    b = sub.add_parser("build")
    b.add_argument("-n", "--workers", type=int, default=3)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        return cmd_build(args)
    return cmd_select(args, run=args.cmd == "run")


if __name__ == "__main__":
    sys.exit(main())
