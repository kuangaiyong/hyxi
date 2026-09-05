---
name: hyxi-gotchas
description: hyxi 的 26 条已知陷阱，逐条绑定具体文件与函数：result 可能为 None、INSERT OR REPLACE 触发级联删除、422 回显明文密码、storage.DB_PATH 是 import 时常量、爬虫退出码契约、增量抓取起点、舆情结果对齐等。改后端 API / 存储层 / 采集脚本 / 前端视图之前扫一遍。
---

## 常见陷阱

- **`task["result"]` 可能是 `None`**：`create_task()` 明确写入 `"result": None`，所以 `task.get("result", {}).get(...)` 在未成功完成的任务上会 500。`results.py` 现已统一用 `(task.get("result") or {}).get(...)`，新增读取处照此写
- **改数据源的 SQL 不能用 `INSERT OR REPLACE`**：SQLite 的 REPLACE 是「删旧行再插新行」，在 `foreign_keys=ON` 下会触发 `credentials` 的 `ON DELETE CASCADE` —— 界面上改个名字或点一下停用，凭据就被静默删光。`save_source()` 用的是 `ON CONFLICT(id) DO UPDATE`，别改回去
- **422 报文默认会回显提交值**：FastAPI 把 pydantic 的 `input` 字段序列化进 `detail`，密码超长这一种情况就够让明文进响应体、再被前端拦截器打进浏览器控制台。`main.py` 注册了 `RequestValidationError` handler 统一剔掉 `input`，新增敏感字段不用额外处理
- **`_persist()` 永远不会删行**：`_save_tasks()` 在 SQLite 分支只对 `self.tasks` 里**剩余**的任务逐条 upsert，从不发 DELETE。所以任何"从 `self.tasks` 移除条目"的新逻辑都必须自己显式调 `db_delete_task()`，否则残留行会在下次启动被 `_load_tasks()` 读回来（`delete_task()` 曾因此让删除的任务重启后复活，已修）。JSON 回退分支是全量覆写，无此问题
- **`storage.DB_PATH` 是 import 时算好的常量**：测试里只改 `settings.data_dir` **不会**改变 DB 位置，必须一并重定向 `storage.DB_PATH`，否则测试会写进真实的 `backend/data/hyxi.db`。`tests/test_core.py` 的 `_create_orchestrator()` 已这么做；`tests/test_api.py` 则是靠在 import 前改 `data_dir` 生效 —— 新增测试文件时注意这个先后顺序
- **`settings.exports_dir` / `tasks_dir` 同理**：它们在 `config.py` 里是**类定义时**用默认 `data_dir` 算好的字段，不是属性。测试里改了 `settings.data_dir` 它们纹丝不动，跑 `generate_excel` 会把报告写进真实的 `backend/data/exports/`（实测踩过，堆了 20 个文件才发现，而且测试单独跑时是绿的 —— 真目录存在所以写得进去）。要改就三个一起改
- **采集脚本的 stdout 只走 SSE，不落进 `task["logs"]`**：`CollectorRunner` 用 `progress.emit()` 转发，而 `task["logs"]` 只由 orchestrator 的 `_task_log()` 写。所以「复用会话，跳过登录」这类脚本自报的行只有在页面盯着 SSE 时看得到，任务跑完再翻记录是找不到的；要断言它就得在流上收
- **退出码 2 的数据必须先入库再抛异常**：`CollectorRunner` 里「读交接文件 → upsert」这一段**排在退出码判断之前**。反过来写的话，脚本已经抓到并写出的那批帖子会连同临时交接文件一起被 `finally` 删掉 —— 160 页的长帖抓到第 100 页撞限流，那 100 页就白抓了，`/retry` 只能从第 1 页重来。改造前脚本写的是长期文件，Python 抛异常也不影响数据留在盘上；换成临时交接文件后这条不再自动成立。回归测试见 `TestScraperPartialExitEndToEnd::test_partial_results_are_persisted_so_retry_can_resume`
- **爬虫退出码是契约**：`0` 完整 / `1` 硬失败（无可用数据）/ `2` 部分完成（数据已落盘，可增量续抓）/ `3` 需要人工授权（见「登录与会话」一节）。残缺时 `total_pages` 保留站点声明的真值**不再被截断点覆盖**，输出 JSON 带 `complete: false` + `stop_reason`，原因同时写 stderr 并被 `CollectorRunner` 拼进任务的 `error_message`。退出码非 0 一律让抓取步骤失败——残缺数据继续跑翻译→Excel→舆情，产出的是一份看起来完整、实际有偏的报告，比任务失败糟得多；续抓走 `POST /api/v1/tasks/{id}/retry`
- **「第一页零帖子」也算硬失败**：全量抓取时第一页一条都提不出来（且没有历史数据可依），脚本直接抛错而不是写一份 `complete: true` 的空结果。这不是假想场景 —— IP 被封时页面返回 200 外壳、DOM 里没有任何 `.message`，改造前正是靠这条路径写出「0 条帖子、抓取成功」，下游照常翻译 0 条并导出空 Excel。站点改类名导致提取器失效时也是同一条路径
- **爬虫 URL 页码有偏移**：`/list_messages/{id}/0` 是第 1 页，`/1` 是第 2 页（`displayToUrl` / `urlToDisplay`）；`group_feed` 的 `/batch/{n}` 同理差一位
- **本地 fixture 站点是唯一能跑通的验证手段**：Tweakers 出口 IP 被封，`backend/tests/fixtures/fixture_site.py` 同时挂论坛（`/forum/list_messages/...`）和小组（`/groups/{id}/batch/{n}`）两个站点。跑的仍是真 Chrome、真 HTTP、真子进程、真 DOM 提取，只是被抓的站点换成本地的。要把数据源指过去就在数据源页填 `base_url`
- **增量抓取从 `maxPage + 1` 开始**（Tweakers）：已抓过的最后一页后来新增的回帖会被永久漏掉，fingerprint 去重救不了（那一页不会再访问）。要补全就把 job 里的 `incremental` 置 false 跑全量
- **采集脚本必须「读旧 + 合并」再落盘，绝不能只写这一轮抓到的**：落盘文件同时承载 `translation` 和 `_processed` 标记，整体覆盖等于把已翻译的帖子重新变成新帖，下一轮再付一次翻译钱、舆情也重算一遍。`group_feed.js` 曾漏掉这段（信息流没有页码可续，很容易写成「全量重扫 + 覆盖」），已修并有回归测试 `TestGroupFeedCollectorEndToEnd::test_incremental_rerun_keeps_translations`
- **正文图只存相对路径，`<img>` 靠 `?api_key=` 过鉴权**：`/api/v1/media/{path}` 不能用 `StaticFiles` 挂载 —— `<img>` 没法自定义请求头，只能复用 `require_api_key` 为 SSE 开的 query 参数口子。**路径穿越必须挡**：media 目录之外就是 `hyxi.db` 和 `.env` 里的明文加密密钥。裸的 `../` 通常在客户端就被规范化掉，但 `%2e%2e%2f` 会被框架解码后原样送进 `rel_path` —— 实测确认过，拿裸 `../` 当测试用例等于什么都没测（把校验整段禁用，那版照样绿）
- **存储顺序不是时间顺序，求时间区间必须排序取极值**：信息流按时间倒序渲染，增量又往后追加，落盘数组的首尾和最早/最晚毫无关系。`/stats` 曾直接取 `timestamps[0]` / `[-1]`，实测显示成「开始 2026-07-28、结束 2026-07-10」——开始比结束晚 18 天，还把跨 5～8 月的数据缩成 7 月里的 18 天。排序也**必须先 `_normalize_timestamp()` 转 ISO**：落盘的 `dd-mm-yyyy` 按字符串排是按「日」排先，`01-07` 会排到 `28-06` 前面
- **`/posts` 的响应里评论挂在主贴的 `replies` 下，按 index 建索引时必须递归展开**：`SentimentView.loadPosts()` 曾只遍历顶层 `posts`，把全部评论漏在外面 —— 实测 88 条里 42 条是评论，情感趋势图只画了 35 条（该 73 条），详情弹窗点评论行也取不到帖子。舆情结果的下标来自扁平数组，评论一样占位
- **舆情结果贴回帖子必须「先按 key 建映射、再排序」**：`sentiment_*.json` 的 `results[i]` 对齐的是**扁平数组**第 i 条（下标来自 `enumerate(all_posts)`），而导出明细按 `order_by_thread()` 排成「主贴 → 它的评论 → 下一主贴」。排完再按行号取，每条帖子都会配上别人的情感结论 —— 实测 88 条里 72 条位置会变，而导出的表**表面上完全看不出异常**。回归测试见 `TestExportEndpointEndToEnd::test_sentiment_follows_the_post_not_the_row_number`（把实现改成按行号取，它会立刻报出「评论A1 拿到了主贴B 的结论」）
- **主贴时间倒序的排序键必须先转 ISO**，理由同上一条；**且只排主贴**，评论跟着自己的主贴走。没解析出时间的帖子沉到最后而不是当成最早 —— 早期采集读不到 tooltip 绝对时间时是**故意留空**的（写相对时间会污染指纹），这批帖子实际很新，排到最前面会把整页占满（实测那个任务里正好 12 条，用户看到的首屏一个时间都没有）。改排序时**页面和导出两条路都要顾**，回归测试分别在 `TestNestedPostsApiEndToEnd`、`TestPostTreeEndToEnd`、`TestExportEndpointEndToEnd::test_rows_are_ordered_newest_first`
- **`/posts` 的 `index` 是扁平存储数组里的绝对位置，不是页内序号**：`SentimentView` 用 `index - 1` 反查帖子，而舆情结果数组的下标来自 `enumerate(all_posts)`。一页只保证 `page_size` 个**主贴**，带上评论后条目数会超出，按页内计数编号会让相邻两页的 index 区间重叠、详情弹窗显示错帖子
- **删数据源不能让历史任务结果变空白**：`task["result"]["sources"]` 里存了当时的 `output_path`，来源从注册表消失后 `results.py` 照原路读文件兜底
- **`error_message` 取 stderr 的第一行，不是最后一行**：脚本先写 `stopReason`，而 Playwright 的报错是多行的 —— 第一行才是原因（`page.goto: Timeout 30000ms exceeded.`），后面跟着一整段 `Call log:` 明细。取末行拿到的是 `  - navigating to "..."`，真正的原因整个丢掉，还把 ANSI 转义码带进界面：用户实测看到的就是 `采集脚本异常退出 (code=1): [2m - navigating to "…"[22m`，完全看不出是超时还是被拒。ANSI 在 `CollectorRunner` 读完 stderr 时**一次剥干净**（SSE 日志、人工授权原因、`error_message` 三处都拿它，各剥各的迟早漏一处）；`_stderr_reason()` 另外跳过 Node 告警 —— 它**占两行**（实测 node v24.14.1：`(node:11032) ExperimentalWarning: x` 后面还跟一行 ``(Use `node --trace-warnings ...` to show where the warning was created)``），只跳第一行的话返回的是第二行，原因照样丢、只是往后挪了一格。
- **测「网络失败」不能按请求次数掐连接**：Chrome 对 `ERR_EMPTY_RESPONSE` 会自己重发（实测一次 `page.goto` 打了 4 个请求），掐一次会被它内部重试救回来，`gotoPage` 压根不抛异常 —— 那条用例于是在修复前后都是绿的。`fixture_site.py` 的 `drop_seconds` 因此按**时间窗**掐，且窗口从第一个页面请求起算而不是服务器启动起算（浏览器冷启动要一两秒）。同理，重试了几次只能数脚本自己打的日志，服务端收到的请求数不是那个数。
- **爬虫必须通过 `node` 子进程调用**，不能 import
- **日志有两套命名空间**：`logging_config.get_logger()` 用全局 `_logger` 缓存，**第一个调用者的 name 定死了整个 logger**（实际是 orchestrator 的 `app.services.orchestrator`）；其余 service 用 `logging.getLogger("hyxi.xxx")`，拿不到那些 handler。加日志时注意实际输出去向
- **`TaskInputView.vue` 是死代码**（未注册路由，全项目零引用，功能已并入 `TaskManagementView`）
- **`Bash` 工具不保持 CWD**，每条命令都要自己 `cd` 到正确目录
