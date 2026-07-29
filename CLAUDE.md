# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

HYXi 舆情分析平台 — 对荷兰 Tweakers.net 论坛的 HYXi Halo 家用储能电池相关帖子进行抓取、LLM 翻译、舆情分析，输出 Excel 报告。前后端分离架构。

## 启动命令

```bash
# 后端 (Python 3.9, macOS 系统自带)
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port 8000

# 前端 (Vue 3 + Vite)
cd frontend && npm run dev        # 开发模式 localhost:5173
cd frontend && npx vite build     # 生产构建
```

安装依赖：
```bash
/usr/bin/python3 -m pip install fastapi uvicorn pydantic pydantic-settings openpyxl httpx python-multipart apscheduler

cd frontend && npm install
```

## Python 3.9 约束

系统 Python 是 3.9.6。**禁止使用 Python 3.10+ 语法**：`dict | None`、`list[str]`、`str | None` 均不支持。必须使用 `typing` 模块的 `Optional[dict]`、`List[str]` 等。

## 核心架构

```
浏览器 (Vue 3 + Pinia + Vite)
    │  :5173  Vite 代理 /api → localhost:8000
    ▼
FastAPI (backend/main.py)
    │
    ├── routers/config.py      LLM 配置 CRUD
    ├── routers/tasks.py       任务提交/列表/取消/重试 + SSE 进度流
    ├── routers/results.py     帖子查询(含搜索)/统计/CSV+JSON+Excel下载 + 舆情触发
    ├── routers/schedules.py   定时任务 CRUD
    │
    ├── services/orchestrator.py   任务编排引擎（核心，含任务队列）
    ├── services/llm_service.py    LLM API 客户端（含指数退避重试 + chat_with_retry）
    ├── services/llm_utils.py      共享 LLM 配置加载 (get_llm_service / load_llm_config)
    ├── services/scraper_service.py 调用 Node Playwright 脚本（含超时 + 取消清理）
    ├── services/translator_service.py LLM 翻译（批量 5 条/次，含应用层重试）
    ├── services/sentiment_service.py LLM 舆情分析（双写：JSON + SQLite）
    ├── services/excel_service.py    Excel 生成（含舆情报告 generate_sentiment_report）
    ├── services/storage.py        SQLite 存储层（tasks + sentiment 表，含 JSON→DB 迁移）
    ├── services/progress_manager.py SSE 事件广播 (asyncio.Queue pub/sub)
    └── services/scheduler_service.py APScheduler 定时任务 (SQLite job store)
```

## 持久化

| 存储 | 用途 |
|------|------|
| `backend/data/hyxi.db` | **主存储** — tasks + sentiment 的 SQLite 数据库（WAL 模式） |
| `backend/data/config.json` | LLM API 配置（含明文 API Key，已 gitignore） |
| `backend/data/tasks.json` | JSON 回退存储（DB 不可用时启用） |
| `backend/data/sentiment_{task_id}.json` | 舆情结果（双写：同时写入 DB 和 JSON） |
| `backend/data/scheduler.db` | APScheduler 定时任务 SQLite |
| `backend/data/scheduled_tasks.json` | 定时任务配置 |
| `backend/data/exports/*.xlsx` | 生成的 Excel |
| `backend/data/logs/app.log` | 滚动日志（5MB x 3） |
| `tweakers_thread_{id}.json` | 项目根目录，抓取的原始帖子数据 |

**存储策略**：启动时自动调用 `init_db()` 创建表结构，然后 `migrate_from_json()` 将历史 JSON 数据迁移到 SQLite。DB 不可用时自动回退 JSON。每次任务变更调用 `save_task()` 逐条 upsert。

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
  "_processed": {
    "translated": true,
    "sentiment_at": "2026-07-28T20:27:00"
  }
}
```

`fingerprint` = SHA256(username + timestamp + content[:100])[:16]，用于增量去重。

## API 端点

```
POST   /api/v1/tasks                  创建任务（后台异步执行）
GET    /api/v1/tasks                  任务列表
GET    /api/v1/tasks/{id}             任务详情
DELETE /api/v1/tasks/{id}             取消运行中 / force=true 删除已结束
POST   /api/v1/tasks/{id}/retry       重试失败/取消的任务（复用原描述）

GET    /api/v1/tasks/{id}/events      SSE 实时进度流

GET    /api/v1/tasks/{id}/posts       帖子分页查询 (page, page_size, search)
GET    /api/v1/tasks/{id}/stats       任务统计
GET    /api/v1/tasks/{id}/download    Excel 下载
GET    /api/v1/tasks/{id}/export/csv  CSV 下载
GET    /api/v1/tasks/{id}/export/json JSON 下载

POST   /api/v1/tasks/{id}/sentiment           触发舆情分析
GET    /api/v1/tasks/{id}/sentiment           获取舆情结果（含跨任务 fallback）
GET    /api/v1/tasks/{id}/sentiment/events    舆情分析 SSE 流
GET    /api/v1/tasks/{id}/sentiment/download  舆情报告 Excel 下载

GET    /api/v1/config                LLM 配置（不含 api_key）
POST   /api/v1/config                保存配置
POST   /api/v1/config/test           测试连接

GET    /api/v1/schedules             定时任务列表
POST   /api/v1/schedules             创建定时任务
PATCH  /api/v1/schedules/{id}        更新
DELETE /api/v1/schedules/{id}        删除
POST   /api/v1/schedules/{id}/toggle 启用/暂停
POST   /api/v1/schedules/{id}/run    手动触发
```

## 任务流水线

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行：

1. **scrape** → 调用 `tweakers_scraper_playwright.js`（Node 子进程，`waitForSelector` + 超时控制，默认 `--incremental`）
2. **translate** → LLM 批量翻译（`chat_with_retry`，5 条/次），跳过 `_processed.translated == true` 的帖子
3. **generate_excel** → openpyxl 生成双语 Excel
4. 舆情分析独立于流水线，通过结果页按钮触发，增量：仅分析 `_processed.sentiment_at` 为空的帖子

**并发控制**：`max_concurrent_tasks` 超限时新任务进入等待队列，前面完成后再自动出队执行，不再直接失败。

## 测试

**56 个测试，必须全部 PASSED。** 修改任何核心逻辑后必须运行：

```bash
cd backend && python3 -m pytest tests/ -v
# 运行单个测试
cd backend && python3 -m pytest tests/test_core.py::TestSearchFilteringEndToEnd -v
```

测试覆盖：任务生命周期持久化、舆情解析、摘要构建、时间戳归一化、Excel 生成、帖子 ID 提取、指纹去重、增量逻辑、LLM 工具函数、舆情 Excel、搜索过滤、原子写入、日志配置、API 端点集成。

## 关键设计决策

- **增量机制**：每个帖子有 fingerprint，各步骤执行前检查 `_processed` 标记跳过已处理帖子
- **SSE 进度推送**：`GET /api/v1/tasks/:id/events` 实时推送 step_start/progress/complete/log/task_complete
- **舆情按 task_id 命名但跨任务复用**：`get_sentiment()` 先从 SQLite 查本任务，fallback 到最新舆情数据
- **舆情双写**：结果同时写入 SQLite 和 JSON 文件
- **LLM 重试**：统一通过 `_retry_with_backoff` 指数退避（3 次），429/5xx 自动重试
- **翻译用 LLM 而非 Google Translate**：批量 5 条/次，应用层单条重试
- **原子写入**：JSON 先写 `.tmp` 再 `os.replace()` 重命名
- **深色模式**：CSS 变量 + `[data-theme="dark"]` 选择器，跟随系统偏好，localStorage 记忆
- **响应式**：≤768px 侧边栏收缩为图标模式，≤480px 进一步精简

## 常见陷阱

- `task.get("result", {}).get("posts")` 在 result 为 JSON null 时返回 None（不是 {}），导致 AttributeError。必须写成 `(task.get("result") or {}).get("posts")`
- 爬虫脚本 `tweakers_scraper_playwright.js` 必须通过 `node` 子进程调用，不能 import
- `Bash` 工具不保持 CWD，每次需要 `cd` 到正确目录
- Excel 列名 `chr(64 + col)` 仅支持 26 列以内
- `config.json` 包含真实 API Key，不应提交到版本控制
- `TaskInputView.vue` 未在路由中注册，是死代码（功能已合并到 TaskManagementView）
- `apscheduler` 运行时依赖但未列入 requirements.txt，需单独安装
- 前端 TypeScript 存在少量 `any` 类型警告（SentimentView.vue），不影响运行
