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
# 后端 — 使用系统 Python 3.9 的 pip
/usr/bin/python3 -m pip install fastapi uvicorn pydantic pydantic-settings openpyxl httpx python-multipart apscheduler

# 前端
cd frontend && npm install
```

## Python 3.9 约束

系统 Python 是 3.9.6。**禁止使用 Python 3.10+ 语法**：`dict | None`、`list[str]`、`str | None` 均不支持。必须使用 `typing` 模块的 `Optional[dict]`、`List[str]` 等。`__import__('os')` 这种动态导入在 services 中不能用于顶层，但函数内部可用（会触发 linter 警告，但功能正常）。

## 核心架构

```
浏览器 (Vue 3 + Pinia + Vite)
    │  :5173  Vite 代理 /api → localhost:8000
    ▼
FastAPI (backend/main.py)
    │
    ├── routers/config.py      LLM 配置 CRUD
    ├── routers/tasks.py       任务提交/列表/取消 + SSE 进度流
    ├── routers/results.py     帖子查询/统计/Excel下载 + 舆情触发
    ├── routers/schedules.py   定时任务 CRUD
    │
    ├── services/orchestrator.py   任务编排引擎（核心）
    ├── services/llm_service.py    DeepSeek API 客户端
    ├── services/scraper_service.py 调用 Node Playwright 脚本
    ├── services/translator_service.py LLM 翻译
    ├── services/sentiment_service.py LLM 舆情分析
    ├── services/excel_service.py    Excel 生成
    ├── services/progress_manager.py SSE 事件广播
    └── services/scheduler_service.py APScheduler 定时任务
```

## 持久化文件

| 文件 | 用途 |
|------|------|
| `backend/data/config.json` | LLM API 配置（含明文 API Key） |
| `backend/data/tasks.json` | 所有任务记录 + 状态 |
| `backend/data/scheduler.db` | APScheduler 定时任务 SQLite |
| `backend/data/scheduled_tasks.json` | 定时任务配置 |
| `backend/data/sentiment_{task_id}.json` | 舆情分析结果 |
| `backend/data/exports/*.xlsx` | 生成的 Excel |
| `tweakers_thread_{id}.json` | 项目根目录，抓取的原始帖子数据 |

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

## 任务流水线

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行：

1. **scrape** → 调用 `tweakers_scraper_playwright.js`（Node 子进程），默认 `--incremental`
2. **translate** → LLM 批量翻译，跳过 `_processed.translated == true` 的帖子
3. **generate_excel** → openpyxl 生成双语 Excel
4. 舆情分析不在流水线中，通过结果页按钮触发，独立于任务

## 仓库状态

- 后端 (`backend/`) 有独立的 `.git`，前端和根目录文件未纳入版本控制
- 无 `.gitignore`、无 Docker、无测试框架
- 根目录下的 `build_excel.py`、`translate_posts.py`、`scrape_page4.js` 等是早期一次性脚本，已被后端 services 取代，不要修改它们
- `frontend/dist/` 未 gitignore，生产构建产物会出现在仓库中

## 关键设计决策

- **增量机制**：每个帖子有 fingerprint，各步骤执行前检查 `_processed` 标记跳过已处理帖子
- **SSE 进度推送**：`GET /api/v1/tasks/:id/events` 实时推送 step_start/progress/complete/log
- **舆情按 task_id 命名但按线程复用**：`_find_sentiment_file()` 在多个 sentiment JSON 中找匹配的
- **翻译用 LLM 而非 Google Translate**：批量 5 条/次，失败自动重试2轮
- **cwd 依赖**：后端必须从 `backend/` 目录启动，前端从 `frontend/` 启动

## 常见陷阱

- `task.get("result", {}).get("posts")` 在 result 为 JSON null 时返回 None（不是 {}），导致 AttributeError。必须写成 `(task.get("result") or {}).get("posts")`
- 爬虫脚本 `tweakers_scraper_playwright.js` 必须通过 `node` 子进程调用，不能 import
- `Bash` 工具不保持 CWD，每次需要 `cd` 到正确目录
- linter 报告 `__import__` 和 `_p` 未使用变量可以忽略，是 Python 3.9 兼容导致的
- Excel 列名 `chr(64 + col)` 仅支持 26 列以内
- config.json 包含真实 API Key，不应提交到版本控制

## 测试规则

**必须 100% 通过才能声称完成。** 修改任何核心逻辑后，必须运行：

```bash
cd backend && python3 -m pytest tests/ -v
```

核心功能测试位于 `backend/tests/test_core.py`（端到端真实测试，不 mock）和 `backend/tests/test_api.py`（FastAPI TestClient 集成测试）。全部 39 个测试必须 PASSED。每次代码变更后必须验证测试通过。
