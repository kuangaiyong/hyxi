---
name: hyxi-test-infra
description: hyxi 测试基础设施的实现细节：选测脚本 scripts/test_impact.py 的映射怎么建、何时判全量、为什么按进程而不按线程归属覆盖率；conftest.py 与 pytest.ini 的会话级护栏（测试默认用临时数据目录、browser 标记与 xdist 分组、钉死 rootdir）。改这三个文件、加新的测试文件，或看不懂选测结论（为什么判全量、为什么没选中某条）时读这份。
---

# hyxi 测试基础设施

什么时候跑什么（改一轮跑受影响的、交付前 `build` 跑全量并刷新映射）是常驻规则，在项目 CLAUDE.md 的「测试」一节。
这里是那几样工具**为什么这么写**，改它们之前必看。

## 按改动精准选测（`scripts/test_impact.py`）

`select` 只打印选中了哪些、为什么；`run` 选完直接并行跑；`--paths 文件…` 不看 git 直接指定。**宁多勿少**：

- 查的是 `backend/tests/impact_map.json`（提交进仓库）：哪条用例执行过哪个文件。**改了映射里没有的源文件、测试地基
  （`pytest.ini`、`conftest.py`、依赖清单、`config.py` / `paths.py`、脚本本身）→ 全量**；映射建好之后新增的用例一律跑；
  改测试文件按改动行选（落在类的辅助方法上跑整类，落在模块顶层跑整个文件）；前端 / 打包只提示该跑哪条 E2E、验包
- **重建映射**：`python scripts\test_impact.py build`（本机约 12 分钟，本身就是一次全量，全过才写映射，写完自动跑健康检查）。
  **交付前那次全量就用它跑**，映射和代码一起提交 —— 映射里记着建它那一刻每个源文件、测试文件的内容哈希，
  之后只要有文件改过却不在当次改动里（改了调用关系又提交了、没重建），选测就判映射过期、直接全量：
  否则再改被新调用的那个文件时，按旧映射会静默漏跑，结论照样显示「选中 N 条」
- **映射按进程归属，不按线程上下文记**：接口用例经 TestClient 调路由，路由跑在它自己开的线程里，pytest-cov 按用例切
  覆盖率上下文只管当前线程 —— 实测那样建出来 93 条接口用例只有 8 条关联得上文件，改路由会漏跑全部接口用例。
  现在按「有 setup_class 的类整类一个进程、其余每 5 条一个进程」跑；采集脚本在 node 子进程里，按进程设 `NODE_V8_COVERAGE`；
  被当数据读的（fixture 的 HTML / JSON、塞给浏览器的采集脚本源码）按文本引用补
- 回归见 `backend/tests/test_impact.py`：选测规则逐条钉住 + 两道护栏（没有源文件会被当成文档跳过；映射还认得
  接口路由、采集脚本、fixture 数据这些关键依赖）。v1.12.0 那 8 处反向验证、35 条新增用例回放全部选中

## 会话级护栏（`backend/tests/conftest.py`、仓库根 `pytest.ini`）

- **测试会话默认指向临时数据目录**（`TWEAKERS_DATA_DIR` / `TASKS_DIR` / `EXPORTS_DIR`，在任何 app 模块导入之前设好）。
  以前「测试不写真实库」靠的是 test_api.py 恰好排在最前面、`setup_class` 先改路径再导入 orchestrator，单跑某个类、
  并行打乱顺序时就没了（`TestSuiteDataIsolation` 守）
- `browser` 标记给起真 Chrome 的 7 个类（快慢分道用，漏标只影响快道快不快）；有 `setup_class` 的类打 `xdist_group`，
  并行时整类留在一个进程。**标记钩子必须 `tryfirst`**：xdist 在 worker 里读分组的钩子按注册顺序会先跑，
  那时标记还没打上，分组整个不生效（实测 `TestAPIEndpointsEndToEnd` 被拆到两个进程）
- `pytest.ini` 只为钉住 rootdir：命令行里混进一个仓库外的路径（比如 `--cov-config 临时文件` 分开写），rootdir 会
  漂到盘符根，nodeid 变成 `code/hyxi/backend/…`，`--deselect` 和选测映射全对不上（实测）
