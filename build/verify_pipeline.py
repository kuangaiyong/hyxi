"""便携包全链路验收：对**已经跑起来的便携包**提交一个真实任务，跑通整条业务链。

真实的部分：打包后的后端进程、包内自带的 node、包内的 playwright、真实 Chrome、
真实 HTTP、真实 SQLite、真实大模型（LLM 配置由调用方通过 --llm-* 传入）。

被采集的站点换成本地 fixture —— 这是项目既定的验证手段（Tweakers 出口 IP 被封、
Facebook 需要人工授权，都无法自动化），跑的仍是真 Chrome、真 DOM 提取。

用法:
    python build/verify_pipeline.py --pkg <便携包目录> [--base http://127.0.0.1:8000]
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "backend", "tests", "fixtures"))

GROUP_ID = "2407063016436085"


def api(base, path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")


def step(text):
    print(f"\n==> {text}", flush=True)


def ok(text):
    print(f"    [OK]   {text}", flush=True)


def fail(text):
    print(f"    [失败] {text}", flush=True)
    raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", required=True, help="便携包目录（含 data\\hyxi.db）")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--llm-key", required=True)
    ap.add_argument("--llm-base", required=True)
    ap.add_argument("--llm-model", required=True)
    args = ap.parse_args()

    from fixture_site import FixtureSite

    step("配置大模型（真实密钥、真实接口）")
    llm = {"api_key": args.llm_key, "base_url": args.llm_base, "model_name": args.llm_model}
    api(args.base, "/api/v1/config", "POST", llm)
    # /test 要带上完整配置，它测的是「这份配置通不通」而不是「已保存的那份通不通」
    test = api(args.base, "/api/v1/config/test", "POST", llm, timeout=120)
    if not test.get("success"):
        fail(f"大模型连不通: {test}")
    ok(f"连接成功：{args.llm_model}")

    # 首启时 seed_default_sources() 会补一条 Tweakers 源，而「采集数据」按既定规则
    # 展开成**全部已启用来源** —— 不摘掉它，任务会先去抓真 tweakers.net，撞上本机
    # 出口 IP 被封的 403。这是系统的正确行为，不是缺陷，验收要绕开它
    step("摘掉默认数据源，只留验收用的那一个")
    assert_target_is_the_package(args.base, db_path(args.pkg))
    for existing in api(args.base, "/api/v1/sources"):
        api(args.base, f"/api/v1/sources/{existing['id']}", "DELETE")
        ok(f"移除 {existing['name']}")

    with FixtureSite() as site_url:
        step(f"注册数据源（指向本地 fixture 站点 {site_url}）")
        src = api(args.base, "/api/v1/sources", "POST", {
            "name": "验收用小组", "collector_id": "group_feed",
            "params": {"group_id": GROUP_ID, "base_url": site_url,
                       "headless": True, "incremental": False},
            "enabled": True,
        })
        ok(f"数据源 {src['id']}")

        step("提交任务：采集 + 翻译 + 分析舆情")
        task = api(args.base, "/api/v1/tasks", "POST",
                   {"description": "采集数据，翻译成中文，分析舆情"}, timeout=120)
        tid = task["id"]
        ok(f"任务 {tid}")

        step("等待任务跑完")
        t0 = time.time()
        last = ""
        while time.time() - t0 < 900:
            cur = api(args.base, f"/api/v1/tasks/{tid}")
            if cur["status"] != last:
                print(f"    {int(time.time() - t0):4d}s  {cur['status']}", flush=True)
                last = cur["status"]
            if cur["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(5)
        else:
            fail("任务 15 分钟没跑完")

        if cur["status"] != "completed":
            fail(f"任务未成功：{cur.get('error_message')}")
        ok(f"任务完成，耗时 {int(time.time() - t0)}s")
        print("    计划:", [s["action"] for s in cur.get("plan") or []], flush=True)

    step("核对数据真的落库了（直接读包内的 SQLite）")
    db = os.path.join(args.pkg, "data", "hyxi.db")
    if not os.path.isfile(db):
        fail(f"找不到 {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    roots = conn.execute("SELECT COUNT(*) FROM posts WHERE parent_fingerprint IS NULL").fetchone()[0]
    translated = conn.execute("SELECT COUNT(*) FROM posts WHERE TRIM(COALESCE(translation,'')) <> ''").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM sentiment_results").fetchone()[0]
    conn.close()

    print(f"    帖子 {total}（主贴 {roots} / 评论 {total - roots}）", flush=True)
    print(f"    已翻译 {translated}，舆情结论 {analyzed}", flush=True)
    if total == 0:
        fail("一条都没采到 —— 采集链路没打通")
    if translated == 0:
        fail("没有任何译文 —— 翻译链路没打通")
    if analyzed == 0:
        fail("没有任何舆情结论 —— 分析链路没打通")
    ok("采集 → 翻译 → 舆情 三段都真实写进了包内的库")

    step("导出报告")
    url = f"{args.base}/api/v1/tasks/{list_first_task_id(args.base)}/export?format=xlsx"
    with urllib.request.urlopen(url, timeout=300) as resp:
        blob = resp.read()
    if len(blob) < 5000 or blob[:2] != b"PK":
        fail(f"导出的不是有效 xlsx（{len(blob)} 字节）")
    ok(f"导出 xlsx {len(blob) // 1024} KB")

    print("\n便携包全链路验收通过。", flush=True)


def db_path(pkg):
    return os.path.join(pkg, "data", "hyxi.db")


def assert_target_is_the_package(base, db):
    """确认 --base 上跑的确实是 --pkg 这个便携包。

    下面那步会**删光**目标实例的全部数据源（含级联删除的凭据），而 --base 默认的
    127.0.0.1:8000 同样是开发实例的地址：开发后端还开着时便携包会因端口被占而拒绝
    启动，此时照跑就会把开发环境的数据源和凭据一起删掉。

    判据是「接口报出来的数据源，在这个包自己的库里也存在」—— 换个实例立刻对不上。
    """
    if not os.path.isfile(db):
        fail(f"找不到 {db}，--pkg 指的不是一个跑过的便携包")
    conn = sqlite3.connect(db)
    try:
        in_db = {r[0] for r in conn.execute("SELECT id FROM sources")}
    finally:
        conn.close()
    from_api = {s["id"] for s in api(base, "/api/v1/sources")}
    if from_api - in_db:
        fail(f"{base} 上跑的不是 --pkg 那个便携包（接口有 {sorted(from_api - in_db)}，"
             f"包内的库里没有）。先把别的实例停掉，别把它的数据源删了")
    ok("确认目标实例就是这个便携包")


def list_first_task_id(base):
    return api(base, "/api/v1/tasks")["tasks"][0]["id"]


if __name__ == "__main__":
    main()
