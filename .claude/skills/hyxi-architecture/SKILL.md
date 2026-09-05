---
name: hyxi-architecture
description: hyxi 的 API 端点全表 + 设计论证与架构细节：存储为什么不留双写、舆情结论为何按帖子身份而非下标存、任务流水线四个动作的展开约定（LLM 能决定什么、不能决定什么）、LLM 重试的两层语义、反爬虫姿态的逐条实测结论。改 orchestrator / storage / results.py / 采集节奏，或想知道某处「为什么这么设计」时读这份。
---

## 持久化的设计论证

**为什么不留双写**：同一份数据存两处、两个读者各读一份，必然长出「改了一边、另一边还是旧的」的 bug。实测踩过：修完 `sentiment_*.json` 后 `/sentiment` 仍返回旧数据，因为它读 SQLite 而 `/export` 读 JSON。

**舆情结论按帖子身份存，不按下标**。下标只在写入现场有意义 —— 离开那里就没人能保证它对得上哪条帖子（已因此出过一次事故，见「常见陷阱」）。`storage.save_sentiment()` 收到的仍是下标数组，落库时立刻换成 `(source_id, fingerprint)`；`get_sentiment(task_id, posts)` 再按当前帖子顺序还原成前端要的下标数组，**API 响应形状不变**。副作用是 `total` / `failed` 现在按当前帖子实算，而不是分析那一刻冻结的数字 —— `/sentiment` 因此与 `/posts`、`/stats`、`/export` 报同一个数。

**结论也不按 task_id 存**。它是对帖子下的，哪个任务触发的分析不改变它对哪条帖子成立。`posts.sentiment_at` 本来就跨任务共享（同一条帖子不重复花钱分析），结论必须同一个粒度 —— 按任务过滤的话，第二个任务跑同一批数据，页面上 94 条里 90 条显示「未分析」，而它们早分析过了（用户实测报过）。这与「绝不跨任务顶替」不矛盾：当初出事的是**按下标**取别的任务那份按别人帖子列表编号的整数组；现在按 `(source_id, fingerprint)` 取，取到的就是这条帖子自己的结论，取不到就是真没有。`sentiment_runs` 因此只剩「本任务最近一次触发的时间」这一个用途，summary 全在读取时现算。

**「分析中」必须在取结论之前判**（`results.py` 的 `GET /sentiment`）。结论跨任务共享后，只要这批帖子里有一条被别的任务分析过，`_task_sentiment()` 就返回真值；放在后面判会让前端认为分析已结束，既不连 SSE 也不再轮询。

`intensity` 列必须是 `NUMERIC` 不能是 `REAL`：REAL 亲和性会把整数 3 存成 3.0，导出的强度列跟着变成「3.0」。

**`posts.seq` 是全链路的顺序锚点**。改造前「扁平数组的下标即顺序」这件事有 8 处依赖（增量过滤、指纹合并、翻译下标对应、舆情绝对索引、Excel、`/posts` 切片、`index` 语义、Node 端合并），入库后由它完整承担：按 source 单调递增、**已有帖子的 seq 永不变**、新帖追加在后。读取一律 `ORDER BY seq`，跨来源拼接顺序由调用方给的 `source_ids` 决定。它一洗牌，所有历史舆情结论就会错位到别的帖子上。

**`posts` 故意不挂 `sources` 外键**：`ON DELETE CASCADE` 会让「删数据源」把历史任务的结果一起清空，而那正是要避免的（见「常见陷阱」）。代价是删源后帖子会留下来，与改造前「删源后落盘 JSON 仍在」一致。

**采集脚本不再读旧数据**。它只输出本轮新抓到的，合并由 `storage.upsert_posts()` 做：已存在的帖子**只更新采集字段**，绝不覆盖 `translation` / `translated` / `sentiment_at`。增量所需的 `known_fingerprints` 与续抓页码由 Python 从库里算好，通过 job 文件下发。

**迁移**：启动时 `migrate_from_json()` 幂等执行，源文件移进 `data/_migrated_backup/` 而不是删掉。舆情从旧的整块 JSON 列搬到键控表时，靠一条**可检验的不变量**兜底 —— 凡是 `results[i]` 有结论，第 i 条帖子必须已带 `sentiment_at`；对不上就整份跳过并保留原数据。**结果条数与帖子条数不等也整份跳过**，不许「只迁对得上的前缀」—— 那假设了缺文件的来源排在末尾，而实测踩过反例（tweakers 排在前、文件被删，幸存的 8 条 group_feed 帖子套上了 tweakers 的结论，5 条错位，且上面那条不变量拦不住）。


## 任务流水线的展开约定

1. **collect** → 每个数据源一个步骤，串行执行；`node collectors/<script>.js --job=<path>`
   - **LLM 只输出 `source_id`**，采集参数（帖子 ID、站点地址、起始页、节奏）全部来自数据源自己，模型碰不到任何一个平台参数
   - **入参只有一个 job 文件**，argv 里没有站点参数。job 由 `Collector.build_job()` 生成
   - **`pacing` 完全不可配**——请求节奏是反爬纪律，谁都不能改；`base_url` 是用户可在数据源页填的（自建镜像 / 本地验证），留空即官方站点
   - **进度靠脚本自报**：stdout 上的 NDJSON 行 `{"evt":"progress","current":N,"total":M}`，解析不出 JSON 的行原样当日志转发。不再有 `第 X/Y 页` 文本正则
   - **输出位置由 job 指定**（`Collector.output_path()` 是全项目唯一的文件名来源）
   - `start_page` 是**显示页码**（1-based），不是 URL 里那个页码；两者差一位，见下方「常见陷阱」
   - **起始页不由 LLM 决定**：`orchestrator._resolve_start_page()` 只认用户描述里的显式指令（`从第 N 页开始`、`start_page: N`），其余一律 1。抓取循环只前进不回补，起始页给大了就是永久丢数据
   - **要采哪些来源也不由 LLM 最终决定**：`_resolve_sources()` 在描述里出现「所有来源 / 全部平台 / 各渠道」时**无条件展开为全部已启用来源**，不看模型给了什么；模型编造的 `source_id` 被忽略并打 warning；一个有效来源都没给出时全采。来源一多模型很容易只挑一个，那是静默漏采——报告看起来完整、实际缺半个平台的声音，比任务失败糟得多。用户临时贴的新链接（`params.override`）**直接失败并提示去数据源页注册**，绝不拿已注册的源顶替
2. **translate** → LLM 批量翻译（5 条/次），跳过 `_processed.translated == true` 且已有 `translation` 的帖子；完成后按 `post_key` 合并回原始顺序，并**按来源拆回各自的落盘文件**（整锅写回任何一个文件都会污染别的来源）
3. **generate_excel** → openpyxl 生成 Excel：DFS 顺序（主贴 → 其评论 → 下一主贴）、「来源」「层级」两列、评论行加 `└─ ` 前缀与浅底
4. **sentiment** → 舆情分析，**只在用户描述里要了才有这一步**，见下

translate、generate_excel 和 sentiment 在 context 里没有 posts 时会**从各数据源已落盘的 JSON 兜底加载**（`_load_posts_from_sources()`），所以可以只提交「翻译已有数据」这类任务。不再有 `_extract_thread_id()` 那套「从描述里抠 5 位数字」的猜测，也不再 glob `tweakers_thread_*.json`。

**要不要 sentiment 步骤由 LLM 判断，没有关键词表**：`sentiment` 是 `parse_intent` prompt 里的第 4 个动作，模型按用户描述自己决定加不加。**别再退回关键词匹配**——曾经用过 `("舆情分析","分析舆情","情感分析",…)` 这种子串表，「分析一下舆情」「帮我分析下舆情」中间插了词就全认不出，而这正是用户的自然写法。

prompt 里有两条必须留着：一是**正例要列全**（各种语序和口语说法），二是**必须给反例**——「抓取舆情数据」「采集舆情相关帖子」只是把舆情当话题词，不该触发。本项目自己就叫「舆情分析平台」，没有反例的话模型会把话题词也当成要求，而**定时任务没人盯着**，每轮静默逐条调一次 LLM，扣的钱要很久以后才发现。还有一条是 sentiment 必须排在 translate 之后（它读的是译文）。

真实模型实测 9/9（`分析一下舆情`/`帮我分析下舆情`/`看看大家的情感倾向如何`/`出一份舆情报告` 都给出 sentiment，`每天抓取Facebook上的舆情数据`/`采集舆情相关帖子并翻译成中文` 都不给）。自动化测试只能钉住「prompt 里确实声明了这个动作和那条反例」（`TestSentimentActionIsOfferedToTheLLM`）和「计划里有就执行、没有就不执行」（`TestPipelineSentimentStepEndToEnd`）——模型理解得准不准，只能对真模型跑。

新建任务、`POST /tasks/{id}/retry`、定时任务三条入口都走 `run_task_async` → `execute_task`，所以这条规则对三者一致生效。

漏掉这一步的后果特别隐蔽：结论按帖子身份跨任务共享，所以新任务一打开，页面上是**上一个任务留下的**结论，看着像已经分析过了；只有那几条新采到的帖子是空的，得逐条翻才发现。用户实测报过（95 条里只有第 95 条没分析，因为它是当天新增的唯一一条）。


## 关键设计决策

- **增量机制**：每个帖子有 fingerprint，各步骤执行前检查 `_processed` 标记跳过已处理帖子
- **SSE 进度推送**：`progress_manager` 按 task_id 做 pub/sub，30s 无事件发 `: keepalive` 注释帧防代理断连。**每条流的结束事件由端点自己给**（`event_generator(channel, terminal_event)`）：任务进度流等 `task_complete`、舆情流等 `sentiment_complete`、人工授权流等 `task_complete`。三者跑在同一套频道机制上，共用一份「终止事件表」会让流水线的 sentiment 步骤一发完 `sentiment_complete` 就把任务进度流掐断 —— 紧随其后的 `step_complete` 和 `task_complete` 全没人收得到，而前端只在收到 `task_complete` 时才 `fetchTask()`，于是带舆情的任务跑完后进度页永远停在 running，不跳转也不出现「查看结果」，最后一行是「连接中断」（用户实测报过）。回归测试见 `TestPipelineSentimentStepEndToEnd::test_progress_stream_survives_the_sentiment_step_and_delivers_task_complete`
- **绝不按下标跨任务顶替舆情结果**：曾经查不到就 fallback 到最新一条，而那份结果是按别的任务的帖子列表编号的，取来与当前帖子完全对不上；更糟的是增量分析会把它当作 `existing_results` 合并后持久化，直接污染目标任务。**按帖子身份取则相反 —— 必须跨任务共享**，见「持久化」一节
- **导出只有一个口**：`GET /export?format=xlsx|csv` 出一份含原文 + 译文 + 舆情结论的文件，界面入口只在舆情页。**报告每次下载现算、不落盘**（`ExcelService.build_export` 返回字节流）——落盘既会在 `exports/` 堆垃圾，两个人同时下载还会撞成一个在写另一个在读。流水线的 `generate_excel` 步骤照旧生成它自己那份，但那份不再被任何人下载
- **导出与页面读同一份结论**（`results.py::_task_sentiment()`）：按帖子身份取，取到什么就写什么，所以报告里的「未分析」条数与舆情页显示的完全一致。它不再有「本任务 / 别的任务」之分 —— 那个区分只在按下标取整数组的年代才有意义
- **LLM 重试分两层，别混为一谈**：`_retry_with_backoff` 是**传输层**指数退避（3 次，1s/2s/4s），只管 429/5xx；**解析失败是另一回事**——批量输出靠分隔符切分，LLM 偶尔在某一段吐出非 JSON，那一条会被记成 `{"sentiment": null, "reason_cn": "解析失败"}`。翻译和舆情都在批量之后补一轮**单条重试**（单条不必切分隔符，解析可靠得多），实测真实任务里 88 条中的 2 条因此救回。单条重试必须复用批量那份 prompt 片段（`_post_block`），来源标签和父贴上文少给一样就成了另一道题。**兜底占位一律记 `sentiment: null`，绝不能填一个具体情感值** —— 「模型整批没给分隔符」那条分支曾记成 `neutral`，于是它绕过了上面这轮单条重试（判据就是 sentiment 为不为空）、还被写上 `sentiment_at` 永久定死，最后以「中性 + 解析失败 + 空维度」进报告和情感分布。真实库里捞出 10 条，其中一条正文是明确抱怨固件的「Deze update werkt niet...」却算成中性。`storage.purge_fake_parse_failures()` 清存量（结论行 + `sentiment_at` **两处都要清**，只清一处等于把那几条永久钉在「已分析」上）。它**不在 `init_db()` 的补丁链里**，而由 `TaskOrchestrator.__init__` 在 `_migrate_sentiment()` **之后**调用 —— 旧 JSON blob 里那批假 neutral 正是那一步才写进 `sentiment_results` 的，放进 `init_db()` 会让老库升上来的第一次启动恰好空转，而那正是它唯一该生效的一次。判据里的 `sentiment IS NOT NULL` 同样不能省：新代码写下的占位 sentiment 为空、`reason_cn` 一样是「解析失败」，那是给用户看的原因且本来就没有 `sentiment_at`，连它一起删会让这个一次性迁移永远变不成 no-op。回归测试见 `TestSentimentRetryEndToEnd::test_missing_segments_are_retried_not_faked_as_neutral` 与 `TestFakeNeutralPurgeEndToEnd`
- **翻译用 LLM 而非 Google Translate**：5 条/批 + `---POST_SEPARATOR---` 切分，解析失败的条目再单条重译。源文本可能是荷兰语或英语且批内混杂，**「译文与原文一字不差且原文非中文」判为漏译**，走同一条单条重译队列
- **舆情维度是封闭集合**：`DEFAULT_DIMENSIONS` 那 14 个。`_normalize_dimensions()` 把 LLM 返回的标签对齐回去（实测它会把 `认证/合规(如Synergrid)` 简写成 `认证/合规`，于是同一维度在 `top_dimensions` 和 `cross_source` 里各占一行），对不上的直接丢弃。维度表的全部价值就在于它封闭，一碎成近义标签跨来源对比就废了
- **原子写入**：JSON 先写 `.tmp` 再 `os.replace()`（仅 tasks.json 回退路径有此保护，其他 JSON 是直接覆写）
- **深色模式**：CSS 变量 + `[data-theme="dark"]`，首次跟随系统偏好，之后 localStorage 记忆
- **响应式**：≤768px 侧边栏收缩为图标模式，≤480px 进一步精简

## 反爬虫姿态

目标是「让流量像一个诚实、有礼貌的真实浏览器用户」，**不是**「压着人家打还不被发现」。当前实现对目标站的压力严格低于改造前：请求速率降到约 1/4，并发保持 1，收到限流主动停止。

- **身份自洽优先于伪装**：`resolveUserAgent()` 运行期从浏览器自己读 UA，只把 `HeadlessChrome` 换成 `Chrome`。不硬编码 UA —— Chrome 升级后硬编码的指纹会和客户端提示失配，那是定时炸弹。自相矛盾的指纹（UA 说 mac、`navigator.platform` 说 Windows）恰恰是最容易被判定的一类
- **客户端提示和 accept-language 一律不手工覆盖**（实测结论，别再"优化"回去）：设了 `userAgent` 之后 Chrome 会自己把 `sec-ch-ua` 同步成不含 HeadlessChrome 的值，且与页面侧 `navigator.userAgentData.brands` 逐字一致；手写一份反而在 GREASE 品牌上对不上（`Not=A?Brand;v=24` vs 真实 `Not;A=Brand;v=8`）。`accept-language` 由 `locale` 决定：塞进 `extraHTTPHeaders` 会被 locale 覆盖，把 q 值列表塞进 `locale` 会产出畸形的 `nl;q=0.9;q=0.9` 并污染 `navigator.languages`，去掉 `locale` 则 `navigator.language` 掉回 `zh-CN` 与请求头分家
- **时区故意不设**：跟随宿主机 `Asia/Shanghai`。检测方比的是时区与出口 IP 的地理位置，从中国境内出网却报 `Europe/Amsterdam` 是更硬的矛盾。将来改从欧洲机器出网时要一并调整
- **节奏**：翻页间隔随机 4-11s，页内 `humanRead()` 随机滚动，每 8~15 页休息 25~60s（日志里的 ☕）
- **会话复用**：`.scraper_state.json`（已 gitignore，含会话 cookie）保存 cookie/localStorage，不必每轮重走 DPG 隐私 gate。用 `storageState` 而非 `launchPersistentContext` —— 后者的用户数据目录不能被两个进程同时占用，`max_concurrent_tasks > 1` 时会启动失败
- **限流即停，但网络失败要重试 —— 两者绝不能混为一谈**：`gotoPage()` 遇 429/403/503 读 `Retry-After` 退让一次（无则 60s，上限 300s），仍失败即抛错终止；而 `page.goto` **自己抛出来**的（导航超时 / DNS 解析不了 / 连接被重置）是**一次响应都没拿到**，站点可能根本没事，只是路上抖了一下 —— 这类退避 5s、10s 重试 2 次再放弃。混着处理两头都错：不重试的话跨境线路抖一下就让整轮采集失败（用户实测报过：首个页面 `goto` 超时 30s → 退出码 1 → 整个任务失败）；反过来对着一个已经说「别打了」的站点重试，正是「明确不做」的那一类事。**网络重试必须包在限流循环的里面**（`gotoTolerant()` 在 `gotoPage()` 内层）——反过来的话，退让后的那一次要是抛了异常，整个限流循环会从头再走一遍：一个已经说「别打了」的站点被连打 3 轮、每轮还睡满一次 `Retry-After`（最坏 6 个请求 + 15 分钟）。且**站点一旦说过一次停，退让后的那一次就不再容忍网络抖动**（`retries` 传 0），于是限流路径上最多 4 个请求、真正拿到响应的仍然只有 2 个。
  还有一条实测出来的边界：**没有正文的 4xx/5xx 会被 Chrome 直接抛成 `net::ERR_HTTP_RESPONSE_CODE_FAILURE`，而不是返回一个 Response**（503 + `Content-Length: 0` 实测如此）。那是「服务器答复了一个错误状态」不是网络失败，`RESPONDED_WITH_ERROR` 把它挡在重试之外 —— 漏掉它的话，一个回空正文 429 的限流器会被当成线路抖动连打 3 次。fixture 的限流响应因此**必须带真实正文**（`throttle_body`），否则测的根本不是限流那条路。`handleConsent()` 里跳转 DPG 回调那一跳**也必须走 `gotoPage`** —— 被拒时正是那个请求返回 403，而它落地后的 URL 仍是正常的 `/forum/...`，只看 URL 发现不了
- **明确不做**：代理池 / IP 轮换、验证码破解、提高并发或速度、无视 robots.txt、stealth 插件
- **升级路径**：若仍被拦，先改有头模式（`headless=False`）观察实际拦截页面，而不是继续叠伪装

> **当前状态（2026-07-31 实测）：本机出口 IP `43.110.141.12` 已被 Tweakers 防火墙整体封禁**，任何请求都会跳 DPG 隐私 gate 后拿到 403，页面原文是「De toegang tot Tweakers vanaf dit IP is geweigerd」，申诉邮箱 `gathering@tweakers.net`（需在邮件里附上该 IP）。这是 IP 级封禁，**不是**指纹或节奏问题 —— 继续调 UA / 延时没有任何意义，换 IP 也在「明确不做」之列。抓取链路本身已验证可用（403 会被正确识别并让任务失败），恢复抓取需要先解封或换一台出网机器。




## API 端点全表


```
POST   /api/v1/tasks                  创建任务（后台异步执行）
GET    /api/v1/tasks                  任务列表
GET    /api/v1/tasks/{id}             任务详情
DELETE /api/v1/tasks/{id}             取消运行中 / force=true 删除已结束
POST   /api/v1/tasks/{id}/retry       重跑终态任务（复用原描述创建新任务，返回新 id）；?full=true 全量重跑
GET    /api/v1/tasks/{id}/events      SSE 实时进度流

GET    /api/v1/tasks/{id}/posts        帖子查询（**按主贴分页**，评论在 replies 里，**主贴按时间倒序**；?fresh_days=3|7|14 标出老帖新回复，?only_fresh=true 只留有新回复的串）
GET    /api/v1/tasks/{id}/posts/{idx}  单条帖子详情（0-based）
GET    /api/v1/tasks/{id}/stats        任务统计
GET    /api/v1/tasks/{id}/export       **唯一的导出口**（?format=xlsx|csv&fresh_days=3|7|14）

POST   /api/v1/tasks/{id}/sentiment           触发舆情分析（增量；?force=true 忽略 sentiment_at 全量重跑）
GET    /api/v1/tasks/{id}/sentiment           获取舆情结果（只读本任务）
GET    /api/v1/tasks/{id}/sentiment/events    舆情分析 SSE 流

GET    /api/v1/config                LLM 配置（不含 api_key）
POST   /api/v1/config                保存配置
POST   /api/v1/config/test           测试连接
DELETE /api/v1/config                重置配置

GET    /api/v1/config/vision         多模态模型配置（可选，同形，不含 api_key）
POST   /api/v1/config/vision         保存
POST   /api/v1/config/vision/test    测试连接（只证明密钥有效，见下）
DELETE /api/v1/config/vision         清除（清除后舆情回到纯文本）

GET    /api/v1/schedules             定时任务列表（附 next_run）
GET    /api/v1/schedules/presets     调度预设 (hourly / 6h / 12h / daily)
POST   /api/v1/schedules             创建
GET    /api/v1/schedules/{id}        详情
PATCH  /api/v1/schedules/{id}        更新
DELETE /api/v1/schedules/{id}        删除
POST   /api/v1/schedules/{id}/toggle 启用/暂停
POST   /api/v1/schedules/{id}/run    手动触发

GET    /api/v1/collectors                    可用采集器清单（param_fields 驱动前端表单）
GET    /api/v1/sources                       数据源列表
POST   /api/v1/sources                       注册数据源
GET    /api/v1/sources/{id}                  详情
PATCH  /api/v1/sources/{id}                  更新（采集器不可换）
DELETE /api/v1/sources/{id}                  删除（凭据级联删除）
PUT    /api/v1/sources/{id}/credential       录入凭据（加密落库，只进不出）
DELETE /api/v1/sources/{id}/credential       清除凭据
POST   /api/v1/sources/{id}/authorize        人工登录（开有头浏览器让人过 2FA）
GET    /api/v1/sources/{id}/authorize/events 人工登录 SSE 进度流

GET    /api/v1/media/{path}          回读采集下来的正文图（受同一套密钥保护）

GET    /api/health                   健康检查
```

