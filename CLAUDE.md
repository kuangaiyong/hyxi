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

爬虫依赖装在项目根目录（根 `package.json` 只有 playwright）。脚本用 `channel: 'chrome'` 启动，**要求本机装有真实 Chrome**，Playwright 自带的 chromium 不满足。

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
    ├── app/services/scraper_service.py 调用 Node Playwright 子进程（超时 + 取消清理）
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
| `backend/data/hyxi.db` | **主存储** — tasks + sentiment 两张表（WAL 模式） |
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

GET    /api/health                   健康检查
```

## 任务流水线

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行：

1. **scrape** → `node tweakers_scraper_playwright.js --thread=ID --start=N [--headless] [--incremental]`，orchestrator 默认注入 `incremental=True`
2. **translate** → LLM 批量翻译（5 条/次），跳过 `_processed.translated == true` 且已有 `translation` 的帖子；完成后按 fingerprint 合并回原始顺序并写回根目录 JSON
3. **generate_excel** → openpyxl 生成双语 Excel（sheet「论坛帖子翻译」+ 可选统计表）

translate 和 generate_excel 在 context 里没有 posts 时会**自动从 `tweakers_thread_{id}.json` 兜底加载**，所以可以只提交「翻译已有数据」这类任务。thread_id 推导顺序见 `_extract_thread_id()`：plan 的 scrape 步骤 → 描述里 5 位以上的数字。`results.py:_load_posts_from_json()` 还多一层兜底：扫描根目录最新的 `tweakers_thread_*.json`。

**舆情分析不在流水线内**，由结果页按钮触发，增量粒度是 `_processed.sentiment_at` 为空的帖子。

**并发控制**：`max_concurrent_tasks` 超限时新任务进 `_task_queue` 排队，前一个任务在 `_run_with_queue` 结束后调 `_process_queue()` 自动出队执行，不会直接失败。

## 测试

**56 个测试，必须全部 PASSED**（本机实测 `56 passed in 3.35s`）。修改任何核心逻辑后必须在仓库根目录运行：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\ -v
```

跑单个测试类：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_core.py::TestSearchFilteringEndToEnd -v
```

测试文件自己做了 `sys.path.insert`，没有 conftest.py / pytest.ini。覆盖：任务生命周期持久化、舆情解析、摘要构建、时间戳归一化、Excel 生成、帖子 ID 提取、指纹去重、增量逻辑、LLM 工具函数、舆情 Excel、搜索过滤、原子写入、日志配置、API 端点集成（TestClient 真实请求）。

## 前端

Vue 3 + `<script setup>` + Pinia + vue-router，路径别名 `@` → `frontend/src`。已注册路由：`/tasks`（默认）、`/sentiment`、`/schedules`、`/config`、`/tasks/:id/progress`、`/tasks/:id/results`、`/tasks/:id/sentiment`。

`useSSE.ts` 是唯一的 SSE 消费点：监听 `step_start` / `step_progress` / `step_complete` / `log` / `error` / `task_complete`，收到 `task_complete` 后主动 `disconnect()`。

**生产构建**：`npm run build`（= `vue-tsc -b && vite build`）**当前会失败** —— `SentimentView.vue` 有 6 个真实类型错误（`pending_count` 不在返回类型上、多处 possibly-undefined 索引）。构建请用：

```bash
cd frontend && npx vite build
```

要根治得同时补 `types/sentiment.ts` 的返回类型和那几处空值判断。

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

## 常见陷阱

- **`result` 为 None 时 `.get()` 崩溃（现存 bug，已实测复现）**：`task.get("result", {}).get("posts")` 在 `result` 键存在但值为 `None` 时返回 `None` 而非 `{}`。`create_task()` 明确写入 `"result": None`，所以**任何未成功完成的任务**访问这几个端点都会 500 + `AttributeError`：`results.py:91`（/posts）、`152`（/stats）、`198`（/export/csv）、`230`（/export/json）。正确写法是同文件 `129-130` / `180-181` 用的 `(task.get("result") or {}).get(...)`
- **`_persist()` 永远不会删行**：`_save_tasks()` 在 SQLite 分支只对 `self.tasks` 里**剩余**的任务逐条 upsert，从不发 DELETE。所以任何"从 `self.tasks` 移除条目"的新逻辑都必须自己显式调 `db_delete_task()`，否则残留行会在下次启动被 `_load_tasks()` 读回来（`delete_task()` 曾因此让删除的任务重启后复活，已修）。JSON 回退分支是全量覆写，无此问题
- **`storage.DB_PATH` 是 import 时算好的常量**：测试里只改 `settings.data_dir` **不会**改变 DB 位置，必须一并重定向 `storage.DB_PATH`，否则测试会写进真实的 `backend/data/hyxi.db`。`tests/test_core.py` 的 `_create_orchestrator()` 已这么做；`tests/test_api.py` 则是靠在 import 前改 `data_dir` 生效 —— 新增测试文件时注意这个先后顺序
- **爬虫脚本失败也返回 exit 0**：`main()` 内部 `try/catch` 吞掉异常后照样写文件，所以 `ScraperService` 里的 `proc.returncode != 0` 检查基本不会触发；判断抓取是否真的成功要看输出 JSON 的内容
- **爬虫 URL 页码有偏移**：`/list_messages/{id}/0` 是第 1 页，`/1` 是第 2 页（`displayToUrl` / `urlToDisplay`）
- **增量抓取从 `maxPage + 1` 开始**：已抓过的最后一页后来新增的回帖会被永久漏掉，fingerprint 去重救不了（那一页不会再访问）。要补全就去掉 `--incremental` 跑全量
- **爬虫必须通过 `node` 子进程调用**，不能 import
- **日志有两套命名空间**：`logging_config.get_logger()` 用全局 `_logger` 缓存，**第一个调用者的 name 定死了整个 logger**（实际是 orchestrator 的 `app.services.orchestrator`）；其余 service 用 `logging.getLogger("hyxi.xxx")`，拿不到那些 handler。加日志时注意实际输出去向
- **Excel 列名 `chr(64 + col)` 仅支持 26 列以内**
- **`config.json` 含真实 API Key**，已 gitignore，不要提交
- **`TaskInputView.vue` 是死代码**（未注册路由，全项目零引用，功能已并入 `TaskManagementView`）
- **`frontend/tsconfig.tsbuildinfo` 未被 gitignore**，`npm run build` 跑过之后会出现在工作树里
- **`Bash` 工具不保持 CWD**，每条命令都要自己 `cd` 到正确目录
