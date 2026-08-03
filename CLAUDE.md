# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HYXi 舆情分析平台 — 对荷兰 Tweakers.net 论坛的 HYXi Halo 家用储能电池相关帖子进行抓取、LLM 翻译、舆情分析，输出 Excel 报告。前后端分离 + 一个独立的 Node/Playwright 爬虫脚本。

## 环境与启动命令（Windows / Python 3.12）

开发机是 **Windows**，Python 版本与 `C:\code\video_evaluation_new` 项目保持一致：**3.12**，依赖装在 `backend\.venv` 内，一律通过 venv 里的解释器调用。

环境搭建 —— **`requirements.txt` 不完整，必须额外补装 `apscheduler`、`sqlalchemy`、`pytest`**：

```powershell
py -3.12 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt apscheduler sqlalchemy pytest
npm ci
cd frontend; npm install
```

- `apscheduler` — `scheduler_service.py` 直接 import，缺失则应用无法启动
- `sqlalchemy` — APScheduler 的 `SQLAlchemyJobStore` 需要，缺失则启动时抛 `ImportError: SQLAlchemyJobStore requires SQLAlchemy installed`
- `deep-translator` 列在 requirements.txt 里但**全项目零引用**（翻译早已改用 LLM），是残留

启动后端（`main:app` 的 import 依赖 cwd，必须先进 `backend`）：

```powershell
cd backend; .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**未设 `TWEAKERS_API_KEY` 时接口无鉴权**，所以默认只绑本机。确需局域网访问时必须先在项目根 `.env`（已 gitignore）设 `TWEAKERS_API_KEY=<共享密钥>`，再改成 `--host 0.0.0.0`，并同时设 `TWEAKERS_ENABLE_DOCS=false` 关闭 `/docs`、`/redoc`、`/openapi.json`。前端需在「LLM 配置」页填入同一密钥（存 localStorage）。

密钥留空则放行并在启动日志打告警——既有部署不会因为漏配就整个不可用。`/api/health` 与 `/` 始终公开，`/api/v1/*` 全部受保护。浏览器的 `EventSource` 不能自定义请求头，所以两个 SSE 端点额外接受 `?api_key=` 查询参数。

注意 `settings.host` / `settings.port` **是死配置**——项目不调 `uvicorn.run()`，实际监听地址只由命令行 `--host` 决定，改环境变量 `TWEAKERS_HOST` 不会生效。`enable_docs` 则是真实生效的（`main.py` 用它决定 `docs_url`）。

启动前端（localhost:5173，Vite 把 `/api` 代理到 localhost:8000）：

```powershell
cd frontend; npm run dev
```

生产构建见下方「前端」一节（注意不是 `npm run build`）。

**PowerShell 路径注意**：PS 5.1 下调用相对路径的 exe 必须带 `.\` 前缀，写 `backend\.venv\Scripts\python.exe` 会报 `CommandNotFoundException`；且 `&&` 是语法错误，串联用 `;`。

**爬虫依赖必须在项目根目录装**（上面那条 `npm ci`，根 `package.json` 只有 playwright）——漏掉它抓取步骤会以 `Cannot find module 'playwright'` 失败；`frontend/node_modules` 和 `%LOCALAPPDATA%\ms-playwright` 里的浏览器二进制都不顶用。`CollectorRunner` 启动子进程前会自检该依赖并直接提示执行 `npm ci`。脚本用 `channel: 'chrome'` 启动，**要求本机装有真实 Chrome**，Playwright 自带的 chromium 不满足。

抓取节奏是刻意放慢的（见「反爬虫姿态」一节），首次全量抓超长帖建议把超时调大：`TWEAKERS_TASK_TIMEOUT_MINUTES=60`（默认 30 分钟约够 160 页）。日常 `--incremental` 不受影响。

## Python 语法约束

目标运行环境是 **Python 3.12**，3.10+ 语法（PEP 604 的 `str | None` 等）可以安全使用。

但**现有代码全部按 `typing` 模块风格编写**（`Optional[dict]`、`List[str]`，`models.py` 还加了 `from __future__ import annotations`）。改动既有文件时沿用该风格，不要为了"现代化"混入 `X | Y` —— 一个文件里两种写法并存比统一用哪种都糟。

## 核心架构

```
浏览器 (Vue 3 + Pinia + Vite)
    │  :5173  Vite 代理 /api → localhost:8000
    ▼
FastAPI (backend/main.py — lifespan 建目录 + 启停 APScheduler)
    │
    ├── app/routers/config.py      LLM 配置 CRUD + 连接测试 + 重置
    ├── app/routers/tasks.py       任务提交/列表/取消/删除/重试 + SSE 进度流
    ├── app/routers/results.py     帖子查询(含搜索)/统计/CSV+JSON+Excel下载 + 舆情触发
    ├── app/routers/schedules.py   定时任务 CRUD + 预设 + 手动触发
    │
    ├── app/services/orchestrator.py    任务编排引擎（核心，含等待队列，全局单例）
    ├── app/services/llm_service.py     LLM API 客户端（_retry_with_backoff 指数退避）
    ├── app/services/llm_utils.py       共享配置加载 (get_llm_service / load_llm_config)
    ├── app/collectors/                 采集器声明（base + 每个来源一个纯声明类）
    ├── app/services/collector_runner.py 驱动 Node 采集脚本（job.json + NDJSON，超时 + 取消清理）
    ├── app/services/translator_service.py LLM 翻译（5 条/批 + 失败条目单条重译）
    ├── app/services/sentiment_service.py  LLM 舆情分析（3 条/批，双写 JSON + SQLite）
    ├── app/services/excel_service.py   openpyxl 生成双语 Excel + 舆情报告
    ├── app/services/storage.py         SQLite 存储层（tasks + sentiment，JSON→DB 迁移）
    ├── app/services/progress_manager.py SSE 事件广播 (asyncio.Queue pub/sub 单例)
    └── app/services/scheduler_service.py APScheduler（SQLAlchemyJobStore，Asia/Shanghai）
```

三个模块级全局单例：`orchestrator`、`progress_manager`、`scheduler_service`。`orchestrator` 在 **import 时**就执行 `init_db()` + `migrate_from_json()` + 加载历史任务，所以测试里改 `settings.data_dir` 必须在 import orchestrator **之前**完成（`tests/test_api.py` 的 `setup_class` 就是这个顺序）。

配置走 pydantic-settings，环境变量前缀 **`TWEAKERS_`**（如 `TWEAKERS_MAX_CONCURRENT_TASKS=2`）。默认 `max_concurrent_tasks=1`、`task_timeout_minutes=30`。

## 持久化

| 存储 | 用途 |
|------|------|
| `backend/data/hyxi.db` | **主存储** — tasks / sentiment / sources / credentials 四张表（WAL 模式） |
| `backend/data/config.json` | LLM API 配置（含明文 API Key，已 gitignore） |
| `backend/data/tasks.json` | JSON 回退存储（仅 SQLite 不可用时启用） |
| `backend/data/sentiment_{task_id}.json` | 舆情结果（与 DB 双写） |
| `backend/data/scheduler.db` | APScheduler job store |
| `backend/data/scheduled_tasks.json` | 定时任务业务配置（与 job store 分离） |
| `backend/data/exports/*.xlsx` | 生成的 Excel |
| `backend/data/logs/app.log` | 滚动日志（5MB x 3） |
| `tweakers_thread_{id}.json` | **项目根目录**，抓取的原始帖子数据，也是翻译结果的落盘位置 |

**存储策略**：启动时 `init_db()` 建表，然后 `migrate_from_json()` 在 tasks 表为空时把历史 JSON 迁进来。DB 不可用则整体回退 JSON（`self._db_ready` 开关）。每次任务变更走 `_persist()` → 对所有内存任务逐条 upsert。

定时任务是**双份存储**：调度触发器在 `scheduler.db`，业务配置（description / interval / enabled / history）在 `scheduled_tasks.json`。改定时任务逻辑时两边要同步（`SchedulerService.update()` 就是先写 JSON，再 `remove_job` + `_add_job`）。

## 数据源与凭据

`sources` 表存用户在「数据源」页注册的采集实例（`collector_id` + `params_json`），`credentials` 表存对应的登录凭据，`ON DELETE CASCADE` 挂在 `source_id` 上（`_get_conn()` 里已开 `PRAGMA foreign_keys=ON`，删源即删凭据）。首启时 `seed_default_sources()` 只在 sources 表整体为空时补一条 Tweakers 源 —— 用户删掉就是不想要，不该每次启动又长回来。

密码用 **`cryptography.Fernet` 对称加密**后落 `credentials.secret_enc`，密钥取自 `TWEAKERS_SECRET_KEY`：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

生成后写进项目根 `.env`（已 gitignore）。**没配密钥时后端拒绝保存凭据并返回 400，不会静默降级成明文落库** —— 平台账号被盗的后果远大于可随时轮换的 LLM key。密钥换了以后旧密文解不开，`decrypt()` 会明说「与保存时的密钥不一致，请重新录入凭据」。

凭据**只进不出**：`SourcePublic` 只回 `has_credential` 和 `credential_username`，任何端点都不会把密码或密文读回去；`get_credential_secret()` 仅供采集器子进程的启动路径调用。`CollectorRunner` 也**不再把命令行 emit 到 SSE 和任务日志**（旧 `ScraperService` 会），密码既不进 argv 也不进 job 文件。

`source_service._validate_params()` 只保留 `Collector.param_fields` 声明过的键 —— params 会原样进 job 文件，放任未知键通过等于给了一条绕过声明往采集脚本塞参数的路。

## 帖子数据模型

```json
{
  "username": "Dorpjes",
  "timestamp": "22-05-2026 17:06",
  "content": "荷兰语原文...",
  "page_number": 1,
  "message_id": "",
  "fingerprint": "a3f8c2d1...",
  "translation": "中文翻译...",
  "_processed": { "translated": true, "sentiment_at": "2026-07-28T20:27:00" }
}
```

`fingerprint` = `SHA256("username|timestamp|content[:100]")` 取前 16 位十六进制，由 Node 端 `makeFingerprint()` 生成，是增量去重和跨文件结果合并的唯一锚点。`timestamp` 落盘始终是荷兰格式 `dd-mm-yyyy HH:MM`，只在 API 出口由 `_normalize_timestamp()` 转 ISO。

## API 端点

```
POST   /api/v1/tasks                  创建任务（后台异步执行）
GET    /api/v1/tasks                  任务列表
GET    /api/v1/tasks/{id}             任务详情
DELETE /api/v1/tasks/{id}             取消运行中 / force=true 删除已结束
POST   /api/v1/tasks/{id}/retry       重试终态任务（复用原描述创建新任务，返回新 id）
GET    /api/v1/tasks/{id}/events      SSE 实时进度流

GET    /api/v1/tasks/{id}/posts        帖子分页查询 (page, page_size≤200, search)
GET    /api/v1/tasks/{id}/posts/{idx}  单条帖子详情（0-based）
GET    /api/v1/tasks/{id}/stats        任务统计
GET    /api/v1/tasks/{id}/download     Excel 下载
GET    /api/v1/tasks/{id}/export/csv   CSV 下载（utf-8-sig）
GET    /api/v1/tasks/{id}/export/json  JSON 下载

POST   /api/v1/tasks/{id}/sentiment           触发舆情分析（增量）
GET    /api/v1/tasks/{id}/sentiment           获取舆情结果（含跨任务 fallback）
GET    /api/v1/tasks/{id}/sentiment/events    舆情分析 SSE 流
GET    /api/v1/tasks/{id}/sentiment/download  舆情报告 Excel 下载

GET    /api/v1/config                LLM 配置（不含 api_key）
POST   /api/v1/config                保存配置
POST   /api/v1/config/test           测试连接
DELETE /api/v1/config                重置配置

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

GET    /api/health                   健康检查
```

## 任务流水线

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行：

1. **scrape** → `node collectors/tweakers.js --job=<path>`，orchestrator 默认注入 `incremental=True`
   - **入参只有一个 job 文件**，argv 里没有站点参数。job 由 `Collector.build_job()` 生成，字段见 `app/collectors/tweakers.py`；`base_url` / `pacing` 只从 source 取不从 params 取，免得 LLM 能改抓取目标和请求节奏
   - **进度靠脚本自报**：stdout 上的 NDJSON 行 `{"evt":"progress","current":N,"total":M}`，解析不出 JSON 的行原样当日志转发。不再有 `第 X/Y 页` 文本正则
   - **输出位置由 job 指定**（`Collector.output_path()` 是全项目唯一的文件名来源）
   - `start_page` 是**显示页码**（1-based），不是 URL 里那个页码；两者差一位，见下方「常见陷阱」
   - **起始页不由 LLM 决定**：prompt 里已删掉 `start_page` 参数，`orchestrator._resolve_start_page()` 只认用户描述里的显式指令（`从第 N 页开始`、`start_page: N`），其余一律 1，LLM 若仍输出该参数会被忽略并打 warning 日志。这么设计是因为抓取循环只前进不回补，起始页给大了就是永久丢数据，而默认 1 能让「所有页面 / 全部 / 整个帖子」等所有措辞都自动落到正确值
2. **translate** → LLM 批量翻译（5 条/次），跳过 `_processed.translated == true` 且已有 `translation` 的帖子；完成后按 fingerprint 合并回原始顺序并写回根目录 JSON
3. **generate_excel** → openpyxl 生成双语 Excel（sheet「论坛帖子翻译」+ 可选统计表）

translate 和 generate_excel 在 context 里没有 posts 时会**自动从 `tweakers_thread_{id}.json` 兜底加载**，所以可以只提交「翻译已有数据」这类任务。thread_id 推导顺序见 `_extract_thread_id()`：plan 的 scrape 步骤 → 描述里 5 位以上的数字。`results.py:_load_posts_from_json()` 还多一层兜底：扫描根目录最新的 `tweakers_thread_*.json`。

**舆情分析不在流水线内**，由结果页按钮触发，增量粒度是 `_processed.sentiment_at` 为空的帖子。

**并发控制**：`max_concurrent_tasks` 超限时新任务进 `_task_queue` 排队，前一个任务在 `_run_with_queue` 结束后调 `_process_queue()` 自动出队执行，不会直接失败。

## 测试

**99 个测试，必须全部 PASSED**（本机实测 `99 passed in 9.68s`）。修改任何核心逻辑后必须在仓库根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\ -v
```

跑单个测试类：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_core.py::TestSearchFilteringEndToEnd -v
```

测试文件自己做了 `sys.path.insert`，没有 conftest.py / pytest.ini。覆盖：任务生命周期持久化、舆情解析、摘要构建、时间戳归一化、Excel 生成、帖子 ID 提取、指纹去重、增量逻辑、LLM 工具函数、舆情 Excel、搜索过滤、原子写入、日志配置、API 端点集成（TestClient 真实请求）。

## 前端

Vue 3 + `<script setup>` + Pinia + vue-router，路径别名 `@` → `frontend/src`。已注册路由：`/tasks`（默认）、`/sentiment`、`/schedules`、`/sources`、`/config`、`/tasks/:id/progress`、`/tasks/:id/results`、`/tasks/:id/sentiment`。

`useSSE.ts` 是唯一的 SSE 消费点：监听 `step_start` / `step_progress` / `step_complete` / `log` / `error` / `task_complete`，收到 `task_complete` 后主动 `disconnect()`。

`SourcesView.vue` 的参数表单**不写死字段**，按 `GET /api/v1/collectors` 回的 `param_fields` 渲染；必填校验以后端 `_validate_params()` 的 400 为准，前端只负责把报文显示出来。加新采集器时前端零改动。

**生产构建**：`npm run build`（= `vue-tsc -b && vite build`）。

## 关键设计决策

- **增量机制**：每个帖子有 fingerprint，各步骤执行前检查 `_processed` 标记跳过已处理帖子
- **SSE 进度推送**：`progress_manager` 按 task_id 做 pub/sub，30s 无事件发 `: keepalive` 注释帧防代理断连
- **舆情按 task_id 命名但跨任务复用**：`get_sentiment()` 先查本任务，查不到则 fallback 到最新一条舆情数据；`_find_sentiment_file()` 还会用「帖子总数匹配」猜哪个文件对应当前数据
- **舆情双写**：结果同时写 SQLite 和 JSON 文件；但 `/sentiment/download` 只读 JSON 文件，不读 DB
- **LLM 重试**：统一走 `_retry_with_backoff` 指数退避（3 次，1s/2s/4s），429/5xx 自动重试
- **翻译用 LLM 而非 Google Translate**：5 条/批 + `---POST_SEPARATOR---` 切分，解析失败的条目再单条重译
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
- **限流即停**：`gotoPage()` 遇 429/403/503 读 `Retry-After` 退让一次（无则 60s，上限 300s），仍失败即抛错终止。`handleConsent()` 里跳转 DPG 回调那一跳**也必须走 `gotoPage`** —— 被拒时正是那个请求返回 403，而它落地后的 URL 仍是正常的 `/forum/...`，只看 URL 发现不了
- **明确不做**：代理池 / IP 轮换、验证码破解、提高并发或速度、无视 robots.txt、stealth 插件
- **升级路径**：若仍被拦，先改有头模式（`headless=False`）观察实际拦截页面，而不是继续叠伪装

> **当前状态（2026-07-31 实测）：本机出口 IP `43.110.141.12` 已被 Tweakers 防火墙整体封禁**，任何请求都会跳 DPG 隐私 gate 后拿到 403，页面原文是「De toegang tot Tweakers vanaf dit IP is geweigerd」，申诉邮箱 `gathering@tweakers.net`（需在邮件里附上该 IP）。这是 IP 级封禁，**不是**指纹或节奏问题 —— 继续调 UA / 延时没有任何意义，换 IP 也在「明确不做」之列。抓取链路本身已验证可用（403 会被正确识别并让任务失败），恢复抓取需要先解封或换一台出网机器。



## 常见陷阱

- **`result` 为 None 时 `.get()` 崩溃（现存 bug，已实测复现）**：`task.get("result", {}).get("posts")` 在 `result` 键存在但值为 `None` 时返回 `None` 而非 `{}`。`create_task()` 明确写入 `"result": None`，所以**任何未成功完成的任务**访问这几个端点都会 500 + `AttributeError`：`results.py:91`（/posts）、`152`（/stats）、`198`（/export/csv）、`230`（/export/json）。正确写法是同文件 `129-130` / `180-181` 用的 `(task.get("result") or {}).get(...)`
- **`_persist()` 永远不会删行**：`_save_tasks()` 在 SQLite 分支只对 `self.tasks` 里**剩余**的任务逐条 upsert，从不发 DELETE。所以任何"从 `self.tasks` 移除条目"的新逻辑都必须自己显式调 `db_delete_task()`，否则残留行会在下次启动被 `_load_tasks()` 读回来（`delete_task()` 曾因此让删除的任务重启后复活，已修）。JSON 回退分支是全量覆写，无此问题
- **`storage.DB_PATH` 是 import 时算好的常量**：测试里只改 `settings.data_dir` **不会**改变 DB 位置，必须一并重定向 `storage.DB_PATH`，否则测试会写进真实的 `backend/data/hyxi.db`。`tests/test_core.py` 的 `_create_orchestrator()` 已这么做；`tests/test_api.py` 则是靠在 import 前改 `data_dir` 生效 —— 新增测试文件时注意这个先后顺序
- **爬虫退出码是契约**：`0` 完整 / `1` 硬失败（无可用数据）/ `2` 部分完成（数据已落盘，可增量续抓）。残缺时 `total_pages` 保留站点声明的真值**不再被截断点覆盖**，输出 JSON 带 `complete: false` + `stop_reason`，原因同时写 stderr 并被 `CollectorRunner` 拼进任务的 `error_message`。退出码非 0 一律让抓取步骤失败——残缺数据继续跑翻译→Excel→舆情，产出的是一份看起来完整、实际有偏的报告，比任务失败糟得多；续抓走 `POST /api/v1/tasks/{id}/retry`
- **「第一页零帖子」也算硬失败**：全量抓取时第一页一条都提不出来（且没有历史数据可依），脚本直接抛错而不是写一份 `complete: true` 的空结果。这不是假想场景 —— IP 被封时页面返回 200 外壳、DOM 里没有任何 `.message`，改造前正是靠这条路径写出「0 条帖子、抓取成功」，下游照常翻译 0 条并导出空 Excel。站点改类名导致提取器失效时也是同一条路径
- **爬虫 URL 页码有偏移**：`/list_messages/{id}/0` 是第 1 页，`/1` 是第 2 页（`displayToUrl` / `urlToDisplay`）
- **增量抓取从 `maxPage + 1` 开始**：已抓过的最后一页后来新增的回帖会被永久漏掉，fingerprint 去重救不了（那一页不会再访问）。要补全就把 job 里的 `incremental` 置 false 跑全量
- **爬虫必须通过 `node` 子进程调用**，不能 import
- **日志有两套命名空间**：`logging_config.get_logger()` 用全局 `_logger` 缓存，**第一个调用者的 name 定死了整个 logger**（实际是 orchestrator 的 `app.services.orchestrator`）；其余 service 用 `logging.getLogger("hyxi.xxx")`，拿不到那些 handler。加日志时注意实际输出去向
- **Excel 列名 `chr(64 + col)` 仅支持 26 列以内**
- **`config.json` 含真实 API Key**，已 gitignore，不要提交
- **`TaskInputView.vue` 是死代码**（未注册路由，全项目零引用，功能已并入 `TaskManagementView`）
- **`frontend/tsconfig.tsbuildinfo` 未被 gitignore**，`npm run build` 跑过之后会出现在工作树里
- **`Bash` 工具不保持 CWD**，每条命令都要自己 `cd` 到正确目录
