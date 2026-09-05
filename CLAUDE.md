# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HYXi 舆情分析平台 — 对荷兰 Tweakers.net 论坛的 HYXi Halo 家用储能电池相关帖子进行抓取、LLM 翻译、舆情分析，输出 Excel 报告。前后端分离 + 一个独立的 Node/Playwright 爬虫脚本。

## 环境与启动命令（Windows / Python 3.12）

开发机是 **Windows**，Python 版本与 `C:\code\video_evaluation_new` 项目保持一致：**3.12**，依赖装在 `backend\.venv` 内，一律通过 venv 里的解释器调用。

环境搭建 —— **`pytest` 不在 `requirements.txt` 里，跑测试必须单独装**：

```powershell
py -3.12 -m venv backend\.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt pytest
npm ci
cd frontend; npm install
```

`requirements.txt` 里的 `APScheduler` 与 `SQLAlchemy` 都是运行时硬依赖，不是可选项：前者被 `scheduler_service.py` 直接 import，后者是 APScheduler 的 `SQLAlchemyJobStore` 所需，缺任何一个应用都起不来。

启动后端（`main:app` 的 import 依赖 cwd，必须先进 `backend`）：

```powershell
cd backend; .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**未设 `TWEAKERS_API_KEY` 时接口无鉴权**，所以默认只绑本机。确需局域网访问时必须先在项目根 `.env`（已 gitignore）设 `TWEAKERS_API_KEY=<共享密钥>`，再改成 `--host 0.0.0.0`，并同时设 `TWEAKERS_ENABLE_DOCS=false` 关闭 `/docs`、`/redoc`、`/openapi.json`。前端需在「LLM 配置」页填入同一密钥（存 localStorage）。

密钥留空则放行并在启动日志打告警——既有部署不会因为漏配就整个不可用。`/api/health` 与 `/` 始终公开，`/api/v1/*` 全部受保护。浏览器的 `EventSource` 不能自定义请求头，所以两个 SSE 端点额外接受 `?api_key=` 查询参数。

注意 `settings.host` / `settings.port` 在**源码开发态是死配置**——那条路由命令行 `--host` 决定，改 `TWEAKERS_HOST` 不生效。但**便携包里它们是真配置**：`run_server.py` 把它们传给 `uvicorn.run()`，所以给使用者的《使用说明》里「改 .env 开局域网访问」那段是成立的。`enable_docs` 两边都真实生效（`main.py` 用它决定 `docs_url`）。

启动前端（localhost:5173，Vite 把 `/api` 代理到 localhost:8000）：

```powershell
cd frontend; npm run dev
```

**日常起服务用根目录的 `start.ps1`**：它依次自检三处依赖、拉起两个进程，再**实际验证**后端 `/api/health`、前端首页、以及前端→后端的代理链路，失败会指出是哪一环。手动起两个进程只在需要看实时控制台输出时才用。面向使用者（而非改代码者）的完整安装 / 配置 / 排障说明在 `README.md`，改动启动方式或环境变量时两份都要跟。

```powershell
.\start.ps1
```

`start.ps1` 存的是 **UTF-8 带 BOM**：PS 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，中文输出全是乱码。用别的工具改写它时注意别把 BOM 弄掉。

生产构建是 `frontend` 目录下的 `npm run build`（= `vue-tsc -b && vite build`）。**必须在 `frontend` 里跑**——根 `package.json` 只有 playwright 依赖、`scripts` 是空的，在仓库根目录执行会直接报「Missing script: build」。

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
    ├── app/routers/results.py     帖子查询(含搜索)/统计/唯一导出口 + 舆情触发与查询
    ├── app/routers/schedules.py   定时任务 CRUD + 预设 + 手动触发
    ├── app/routers/sources.py     数据源 CRUD + 采集器清单 + 凭据录入/清除
    │
    ├── app/services/orchestrator.py    任务编排引擎（核心，含等待队列，全局单例）
    ├── app/services/llm_service.py     LLM API 客户端（_retry_with_backoff 指数退避）
    ├── app/services/llm_utils.py       共享配置加载 (get_llm_service / load_llm_config)
    ├── app/collectors/                 采集器声明（base + 每个来源一个纯声明类）
    ├── app/services/source_service.py  数据源注册 + 参数校验 + 凭据加解密
    ├── app/services/crypto.py          Fernet 加解密（密钥没配就拒绝保存，不降级明文）
    ├── app/services/collector_runner.py 驱动 Node 采集脚本（job.json + NDJSON，超时 + 取消清理）
    ├── app/services/post_tree.py       跨来源索引键 + 出口组树（存储层始终扁平）
    ├── app/services/translator_service.py LLM 翻译（5 条/批 + 失败条目单条重译）
    ├── app/services/vision_service.py     多模态图片理解（只服务于舆情，翻译不用）
    ├── app/services/sentiment_service.py  LLM 舆情分析（3 条/批，结论按帖子身份落库）
    ├── app/services/excel_service.py   openpyxl 生成双语 Excel + 舆情报告
    ├── app/services/storage.py         SQLite 存储层（全部持久化，见「持久化」一节）
    ├── app/services/progress_manager.py SSE 事件广播 (asyncio.Queue pub/sub 单例)
    └── app/services/scheduler_service.py APScheduler（SQLAlchemyJobStore，Asia/Shanghai）
```

三个模块级全局单例：`orchestrator`、`progress_manager`、`scheduler_service`。`orchestrator` 在 **import 时**就执行 `init_db()` + `migrate_from_json()` + 加载历史任务 + 舆情迁移，所以测试里改 `settings.data_dir` 与 `storage.DB_PATH` 必须在 import orchestrator **之前**完成（`tests/test_api.py` 的 `setup_class` 就是这个顺序）。

`orchestrator = TaskOrchestrator()` **必须留在 orchestrator.py 末尾**：`__init__` 里的舆情迁移要调用同模块的 `load_task_posts()`，在它定义之前实例化会得到一串 `NameError`（实测踩过，靠 `except` 兜住才没炸出来）。

配置走 pydantic-settings，环境变量前缀 **`TWEAKERS_`**（如 `TWEAKERS_MAX_CONCURRENT_TASKS=2`）。默认 `max_concurrent_tasks=1`、`task_timeout_minutes=30`。

## 持久化

**业务数据一律进 `backend/data/hyxi.db`（WAL 模式），不再有 JSON 存储。**

| 表 | 用途 |
|------|------|
| `posts` | 帖子。`seq` 承担改造前「数组下标即顺序」的全部语义 |
| `tasks` | 任务记录（plan / result / logs 是形状可变的不透明块，留在 JSON 列里） |
| `sentiment_runs` | 某个任务最近一次触发分析的时间，只此一列 |
| `sentiment_results` | **舆情结论，按 `(source_id, fingerprint)` 存** |
| `sources` / `credentials` | 数据源与加密凭据，`ON DELETE CASCADE` |
| `app_config` | LLM 配置，按 `llm.api_key` / `llm.base_url` / `llm.model_name` 分列；多模态模型同形，换 `vision.` 前缀 |
| `schedules` | 定时任务业务配置 |
| `apscheduler_jobs` | APScheduler 自建，调度触发器 |

| 非数据库文件 | 说明 |
|------|------|
| `backend/data/media/{source_id}/*` | 采集下载的正文图（二进制，已 gitignore） |
| `backend/data/logs/app.log` | 滚动日志（5MB x 3） |
| `backend/data/jobs/{run}.json` | 交给采集子进程的入参，**用完即删的 IPC，不是持久化** |
| `backend/data/jobs/{run}_out.json` | 采集脚本的产出，**读完入库即删的交接文件** |
| `backend/data/sessions/{source_id}.json` | Playwright `storageState`。它只认文件路径，且是可重建的运行时缓存，丢了只是重新登录一次 |

**四条存储红线**（静默错误，改存储层前必看）：

- `intensity` 列必须是 `NUMERIC` 不能是 `REAL` —— REAL 亲和性把整数 3 存成 3.0，导出跟着变「3.0」
- **舆情结论按 `(source_id, fingerprint)` 存，不按下标、也不按 task_id**。下标只在写入现场有意义
- **`posts` 故意不挂 `sources` 外键** —— `ON DELETE CASCADE` 会让「删数据源」清空历史任务结果
- **不留双写**：同一份数据存两处必然长出「改了一边另一边还是旧的」的 bug（已实测踩过）

设计论证（为什么这么定）见 `Skill(hyxi-architecture)`。

## 数据源与凭据

`sources` 表存用户在「数据源」页注册的采集实例（`collector_id` + `params_json`），`credentials` 表存对应的登录凭据，`ON DELETE CASCADE` 挂在 `source_id` 上（`_get_conn()` 里已开 `PRAGMA foreign_keys=ON`，删源即删凭据）。首启时 `seed_default_sources()` 只在 sources 表整体为空时补一条 Tweakers 源 —— 用户删掉就是不想要，不该每次启动又长回来。

密码用 **`cryptography.Fernet` 对称加密**后落 `credentials.secret_enc`，密钥取自 `TWEAKERS_SECRET_KEY`：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

生成后写进项目根 `.env`（已 gitignore）。**没配密钥时后端拒绝保存凭据并返回 400，不会静默降级成明文落库** —— 平台账号被盗的后果远大于可随时轮换的 LLM key。密钥换了以后旧密文解不开，`decrypt()` 会明说「与保存时的密钥不一致，请重新录入凭据」。

凭据**只进不出**：`SourcePublic` 只回 `has_credential` 和 `credential_username`，任何端点都不会把密码或密文读回去；`get_credential_secret()` 仅供采集器子进程的启动路径调用。`CollectorRunner` 也**不再把命令行 emit 到 SSE 和任务日志**（旧 `ScraperService` 会），密码既不进 argv 也不进 job 文件。

`source_service._validate_params()` 只保留 `Collector.param_fields` 声明过的键 —— params 会原样进 job 文件，放任未知键通过等于给了一条绕过声明往采集脚本塞参数的路。

**明文密码只走子进程环境变量**（`CollectorRunner._child_env()` 设 `HYXI_CRED_USERNAME` / `HYXI_CRED_PASSWORD`）：不进 argv（会出现在进程列表和任何回显命令行的地方），不进 job 文件（要落磁盘），随进程结束一起消失。取凭据失败**不在这里抛**——脚本会走「会话失效且没凭据」那条路给出可操作提示，在这里抛只会变成一个看不懂的 `OperationalError` 把它盖掉。

## 登录与会话（needs_credentials 的采集器）

`collectors/lib/auth.js` 是唯一登录入口，**会话优先**：先用 `storageState` 打开目标页，
已登录就直接返回，只有会话失效才动用密码。会话按 **source 而非 collector** 隔离，
落 `backend/data/sessions/{source_id}.json`（已 gitignore）。
撞上验证就 `emit('need_manual_auth')` + 退出码 3 交给人，**不硬闯**。

> ⚠️ **Facebook 服务条款禁止自动化登录与抓取，账号可能被封。用专用小号，不要复用任何有价值的账号。**

**改 `auth.js`、`facebook_group.js`、人工授权接口或任何 Playwright 选择器之前，
必须先 `Skill(hyxi-auth-session)`** —— 那里有真站实测结论（表单选择器、为什么不能点
submit 按钮、Arkose 人机验证）、小组页 DOM 实测结论，以及已排除、不要再试的三条路。
照着猜再叠选择器会白费很长时间。

## 帖子数据模型

```json
{
  "username": "Dorpjes",
  "timestamp": "22-05-2026 17:06",
  "content": "荷兰语原文...",
  "page_number": 1,
  "message_id": "",
  "fingerprint": "a3f8c2d1...",
  "source": "src_09ee083e",
  "parent_fingerprint": null,
  "reply_level": 0,
  "translation": "中文翻译...",
  "_processed": { "translated": true, "sentiment_at": "2026-07-28T20:27:00" }
}
```

`fingerprint` = `SHA256("username|timestamp|content[:100]")` 取前 16 位十六进制，由 Node 端 `makeFingerprint()` 生成，是增量去重和跨文件结果合并的唯一锚点。`timestamp` 落盘始终是荷兰格式 `dd-mm-yyyy HH:MM`，只在 API 出口由 `_normalize_timestamp()` 转 ISO。

**指纹不含来源，Python 侧一律用 `post_tree.post_key()` = `source:fingerprint` 做索引键。** 只用 fingerprint 会让两个平台的同名空内容帖互相覆盖 —— 翻译结果串源、舆情结论盖到别人头上。指纹算法本身**一个字符都不能动**：改了历史数据全部失配，已翻译的帖子会被判成新帖重新付费翻译。历史数据没有 `source` 字段，`post_key()` 缺省填 `tweakers`，完全兼容。

**评论嵌套只在出口组装，存储层永远是扁平数组。** 整条处理链有 8 处假设 posts 是扁平的（增量过滤、`_merge_by_fingerprint`、翻译的下标一一对应、舆情的绝对索引、Excel、`results.py` 切片、Node 端合并）。物理嵌套要给每一处写一对展平/回填函数，且会破坏「顺序以源 JSON 为准」的保证。`post_tree.build_tree()` / `order_by_thread()` 是唯一的组树入口，父贴不在本批数据里的评论按主贴处理，不会被丢掉。

**出口一律「主贴按发表时间从新到旧、评论跟着自己的主贴走」**，页面（`/posts`）和导出（`/export`）用同一条规则，所以两边看到的顺序逐条一致。规则落在 `post_tree.order_by_thread()` 里，没有开关 —— 两个出口各排各的，迟早分家。`/posts` 因为直接用 `build_tree()` 分页，自己调一次 `sort_time()`，**必须排在切片之前**：一页只有 50 个主贴，只排页内的话后面更新的帖子永远出不了第二页。排序键先经 `normalize_timestamp()` 转 ISO（理由见「常见陷阱」）；解析不出时间的主贴沉到最后，靠稳定排序保持采集顺序。**存储层的 `seq` 和响应里的 `index` 都不动**，舆情结论按它们对齐。

`page_number` 对信息流类来源没有页的含义，`group_feed` 填的是**滚动批次序号**，保证字段非空；它的增量走时间水位线（`incremental_strategy = "watermark"`）而不是页码。

## API 端点

全表（含参数与语义注解）见 `Skill(hyxi-architecture)`。分组概览：

- `/api/v1/tasks*` —— 创建 / 列表 / 详情 / 取消删除 / retry 重跑 / `events` SSE 进度流
- `/api/v1/tasks/{id}/posts*` —— 帖子查询（**按主贴分页**，评论在 `replies` 下）、单条详情、`stats`
- `/api/v1/tasks/{id}/sentiment`、`/export` —— 舆情结论与导出（导出只有这一个口）
- `/api/v1/sources*` —— 数据源增删改查、`authorize` 人工登录、凭据（写入即加密，只出不进）
- `/api/v1/schedules*`、`/api/v1/config*`、`/api/v1/media/{path}` —— 定时任务、LLM 配置、正文图

## 任务流水线

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行：

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行四个动作：

1. **collect** → 每个数据源一个步骤，串行；`node collectors/<script>.js --job=<path>`
2. **translate** → LLM 批量翻译（5 条/次），跳过已翻译的；完成后**按来源拆回各自的落盘文件**
   （整锅写回任何一个文件都会污染别的来源）
3. **generate_excel** → openpyxl，DFS 顺序（主贴 → 其评论 → 下一主贴）
4. **sentiment** → 舆情分析，**只在用户描述里要了才有这一步**

**三条红线**：

- **`pacing` 完全不可配** —— 请求节奏是反爬纪律，谁都不能改（`base_url` 才是用户可填的）
- **LLM 只输出 `source_id`**，采集参数全部来自数据源自己，模型碰不到任何一个平台参数；
  起始页与「要采哪些来源」同样不由 LLM 最终决定
- 新建任务、`POST /tasks/{id}/retry`、定时任务三条入口都走 `run_task_async` → `execute_task`，
  规则对三者一致生效

展开约定（进度自报格式、prompt 的正反例、sentiment 判定实测）见 `Skill(hyxi-architecture)`。

## 采集语义 / 图片理解 / 便携包（按需加载）

这三块都是「一旦开始做就知道需要」的知识，已搬进 skill，不再常驻：

- **改采集脚本的增量 / 去重 / 翻页逻辑** → `Skill(hyxi-collector-semantics)`
  （正文图抓取、`force_full` 全量重跑、老帖新回复如何避免被时间倒序埋掉）
- **动图片理解、舆情配图、Kimi 调用** → `Skill(hyxi-image-pipeline)`
  （Kimi 真机实测结论、纯图帖、导出报告里的配图）
- **打便携包、改打包脚本或数据目录布局** → `Skill(hyxi-portable-package)`
  （数据目录为何在包的同级、采集脚本只能压缩不能编字节码）

## 测试

**373 个测试，必须全部 PASSED**（本机实测 `373 passed`）。修改任何核心逻辑后必须在仓库根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\ -v
```

**前端没有单元测试框架**（package.json 里无 vitest / jest / @vue/test-utils）。
结果页筛选条件那条回归靠真浏览器守：`frontend/e2e/results_filters.js`
（`cd frontend; npm run e2e`）—— 真 Chrome、真前后端、无 mock，**要求两个服务都起着**
（先跑 `.\start.ps1`），所以它进不了 pytest。它自己从 `/tasks` 里挑任务、探出一个
筛得空和一个筛得出的窗口，不写死任何 ID。密钥从项目根 `.env` 读。

跑单个测试类：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_core.py::TestSearchFilteringEndToEnd -v
```

测试文件自己做了 `sys.path.insert`，没有 conftest.py / pytest.ini。覆盖：任务生命周期持久化、舆情解析、摘要构建、时间戳归一化、Excel 生成、帖子 ID 提取、指纹去重、增量逻辑、LLM 工具函数、舆情 Excel、搜索过滤、原子写入、日志配置、API 端点集成（TestClient 真实请求）。

## 前端

Vue 3 + `<script setup>` + Pinia + vue-router，路径别名 `@` → `frontend/src`。已注册路由：`/tasks`（默认）、`/sentiment`、`/schedules`、`/sources`、`/config`、`/tasks/:id/progress`、`/tasks/:id/results`、`/tasks/:id/sentiment`。

`useSSE.ts` 是唯一的 SSE 消费点：监听 `step_start` / `step_progress` / `step_complete` / `log` / `error` / `task_complete`，收到 `task_complete` 后主动 `disconnect()`。

`SourcesView.vue` 的参数表单**不写死字段**，按 `GET /api/v1/collectors` 回的 `param_fields` 渲染；必填校验以后端 `_validate_params()` 的 400 为准，前端只负责把报文显示出来。加新采集器时前端零改动。

**新采集器没出现在下拉框里，先看 `Collector.internal`**：该端点会过滤掉 `internal = True` 的采集器（`collector_catalog()`）。目前只有 `group_feed`「公开小组信息流」是这样 —— 它是多来源那一版为本地 fixture 站点写的通用采集器，`base_url` 必填且没有默认站点，真实场景下用户填不出可用地址，真实版本是 `facebook_group`。**它只是不进界面，`get_collector()` 照常解析**，已注册的数据源、任务编排和 `TestGroupFeedCollectorEndToEnd` 那条增量回归都还在用它。

**生产构建**：`npm run build`（= `vue-tsc -b && vite build`）。

## 关键设计决策 / 反爬虫姿态

完整论证见 `Skill(hyxi-architecture)`。以下几条常驻，因为违反它们的后果是静默的：

- **姿态**：让流量像一个诚实、有礼貌的真实浏览器用户，**不是**「压着人家打还不被发现」
- **明确不做**：代理池 / IP 轮换、验证码破解、提高并发或速度、无视 robots.txt、stealth 插件
- **客户端提示、accept-language、时区一律不手工覆盖**（实测结论，别再「优化」回去）：
  设了 `userAgent` 后 Chrome 会自己同步 `sec-ch-ua`；时区跟随宿主机 `Asia/Shanghai`，
  从中国境内出网却报 `Europe/Amsterdam` 是更硬的矛盾
- **限流即停，但网络失败要重试 —— 两者绝不能混为一谈**：429/403/503 读 `Retry-After` 退让一次后即抛；
  网络类失败才重试
- **绝不按下标跨任务顶替舆情结果**：查不到就是「未分析」，不许 fallback 到最新一条
- **LLM 重试分两层**：`_retry_with_backoff` 是传输层退避（只管 429/5xx）；解析失败是另一回事

> **当前状态（2026-07-31 实测）：本机出口 IP 已被 Tweakers 防火墙整体封禁**，
> 任何请求都会跳 DPG 隐私 gate 后拿到 403。本地 fixture 站点是唯一能跑通的验证手段。

## 常见陷阱

26 条已知陷阱（逐条绑定具体文件与函数）已搬进 `Skill(hyxi-gotchas)`。
**改后端 API / 存储层 / 采集脚本 / 前端视图之前扫一遍**，其中几条是静默错误：
`INSERT OR REPLACE` 会触发级联删除、422 报文默认回显明文密码、
`storage.DB_PATH` 是 import 时算好的常量（测试会写进真实库）。

**爬虫退出码是跨模块契约**（这条常驻）：`0` 完整 / `1` 硬失败（无可用数据）/
`2` 部分完成（数据已落盘，可增量续抓）/ `3` 需要人工授权。
退出码 2 的数据**必须先入库再抛异常**。
