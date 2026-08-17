# HYXi 舆情分析平台

对荷兰 Tweakers.net 论坛、Facebook 公开小组中与 HYXi Halo 家用储能电池相关的帖子进行
**采集 → LLM 翻译 → 舆情分析 → 导出报告**的一体化平台。

前后端分离，采集由独立的 Node / Playwright 脚本完成。

---

## 目录

- [技术栈](#技术栈)
- [环境要求](#环境要求)
- [首次安装](#首次安装)
- [配置](#配置)
- [启动服务](#启动服务)
- [局域网访问](#局域网访问)
- [生产构建](#生产构建)
- [运行测试](#运行测试)
- [目录结构](#目录结构)
- [数据存放](#数据存放)
- [常见问题](#常见问题)

---

## 技术栈

| 层 | 技术 |
|---|---|
| 前端 | Vue 3 (`<script setup>`) + Pinia + vue-router + Vite + TypeScript + ECharts |
| 后端 | FastAPI + uvicorn + APScheduler，Python 3.12 |
| 存储 | SQLite（WAL 模式），全部业务数据落 `backend/data/hyxi.db` |
| 采集 | Node.js + Playwright，驱动**真实 Chrome** |
| 翻译 / 舆情 | 任意 OpenAI 兼容的 LLM 接口 |

---

## 环境要求

| 组件 | 版本要求 | 说明 |
|---|---|---|
| Python | **3.12** | 依赖装在 `backend\.venv` 内，一律通过 venv 里的解释器调用 |
| Node.js | 18 以上 | 采集脚本与前端构建都要用 |
| Google Chrome | 任意较新版本 | 采集脚本用 `channel: 'chrome'` 启动，**要求本机装有真实 Chrome**，Playwright 自带的 chromium 不满足 |

> 开发机为 Windows，下文命令均为 **PowerShell** 语法。macOS / Linux 的差异见文末[常见问题](#常见问题)。

---

## 首次安装

依赖分**三处**，缺一不可，不能互相顶替。

### 1. 后端依赖

```powershell
py -3.12 -m venv backend\.venv
```

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt pytest
```

`requirements.txt` 已包含 APScheduler 与 SQLAlchemy（APScheduler 的 `SQLAlchemyJobStore` 需要
后者，缺失则应用启动即报错）。`pytest` 不在清单里，只有要跑测试时才需要，所以单独附在命令末尾。

### 2. 采集器依赖 —— **必须在项目根目录安装**

```powershell
npm ci
```

根 `package.json` 只有 `playwright` 一个依赖。**漏掉这一步，抓取步骤会以
`Cannot find module 'playwright'` 失败**；`frontend/node_modules` 里的那份顶不上。
`CollectorRunner` 在启动子进程前会自检该依赖并直接提示执行 `npm ci`。

### 3. 前端依赖

```powershell
cd frontend; npm install
```

---

## 配置

配置文件为项目根目录下的 `.env`（**已 gitignore，不要提交**）。没有该文件时服务仍能启动，
但会退化到「无鉴权 + 无法保存数据源凭据」的状态。

```ini
TWEAKERS_API_KEY=<接口共享密钥>
TWEAKERS_SECRET_KEY=<Fernet 加密密钥>
```

### TWEAKERS_API_KEY —— 接口鉴权

`/api/v1/*` 全部受它保护，`/api/health` 与 `/` 始终公开。

**留空则完全放行**，并在启动日志打一条告警。所以留空时后端只应绑 `127.0.0.1`。
前端需要在页面的「LLM 配置」中填入同一个值（保存在浏览器 localStorage）。

> 浏览器的 `EventSource` 不能自定义请求头，因此 SSE 端点额外接受 `?api_key=` 查询参数。

### TWEAKERS_SECRET_KEY —— 凭据加密

数据源的登录密码用 `cryptography.Fernet` 加密后落库。生成一个密钥：

```powershell
.\backend\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**没配置该密钥时后端会拒绝保存凭据并返回 400，不会静默降级成明文落库。**
密钥更换后旧密文将无法解开，需要重新录入凭据。

### LLM 的 API Key 不放在这里

它在页面的「LLM 配置」中填写，存进数据库的 `app_config` 表。

### 多模态模型（图片理解，可选）

同样在「LLM 配置」页填写，是页面上第二张卡片。配置后，**做舆情分析时**会先把帖子配图
交给多模态模型理解成中文描述，再连同正文与整串讨论上下文一起交给大模型判断情感倾向。

- **只有舆情分析用图片，翻译不用**
- 留空则舆情分析按纯文本进行，不影响任何既有功能
- 必须填一个**支持图片输入**的模型；填了不支持的不会报错，只会每次拿不到描述、退回纯文本
- 「测试连接」只验证密钥与地址是否有效（它探的是 `/models`），**不代表额度充足或该模型能读图**
- **一个字都没有、只有一张图的帖子也会被分析** —— 判断全靠这段图片描述。
  没配多模态时这类帖子会诚实地显示「未分析」，而不是凭空给一个结论

### 导出的报告里有配图

「导出」出来的 xlsx 里，带图的帖子那一行会有一张缩略图；点该行**「图片描述」列里的
🔍**，跳到「配图」工作表看大图和完整描述，那边有「← 返回帖子明细」跳回来。
（Excel 没有「点图片就放大」的原生行为，所以入口做在文字格上。）
CSV 放不下图，「配图」列给的是相对路径，照着能在 `backend/data/media/` 里找到原图。

### 其他可选项（环境变量前缀统一为 `TWEAKERS_`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TWEAKERS_MAX_CONCURRENT_TASKS` | `1` | 并发任务上限，超限的任务自动排队 |
| `TWEAKERS_TASK_TIMEOUT_MINUTES` | `30` | 单任务超时。首次全量抓超长帖建议调到 `60` |
| `TWEAKERS_ENABLE_DOCS` | `true` | 设 `false` 关闭 `/docs`、`/redoc`、`/openapi.json` |

> `TWEAKERS_HOST` / `TWEAKERS_PORT` 是**死配置**：项目不调用 `uvicorn.run()`，
> 实际监听地址只由启动命令的 `--host` / `--port` 决定，改这两个环境变量不会生效。

---

## 启动服务

### 一键启动（推荐）

```powershell
.\start.ps1
```

脚本会依次完成：检查三处依赖是否齐全 → 拉起后端与前端 → **轮询验证两个服务是否真的能应答**
（并额外验证前端到后端的 `/api` 代理链路是否通）→ 打印结果与停止命令。

全部就绪时退出码为 `0`；任何一个服务没起来则退出码为 `1`，并打印该服务日志的末尾若干行。
端口已被占用时会跳过启动直接验证，所以重复执行不会拉起重复进程。

停止全部服务：

```powershell
Get-NetTCPConnection -LocalPort 8000,5173 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

> 需要单独调试某一个服务、或想看实时控制台输出时，用下面的手动方式。

### 手动启动

共两个服务，各开一个终端窗口。

### 1. 后端（端口 8000）

```powershell
cd backend; .\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

`main:app` 的 import 依赖当前工作目录，**必须先 `cd backend`**。

若不想切目录，可在仓库根目录用 `--app-dir` 等价启动：

```powershell
.\backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend main:app --host 127.0.0.1 --port 8000
```

验证后端已就绪：

```powershell
(Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/health" -UseBasicParsing).Content
```

应输出 `{"status":"ok"}`。

### 2. 前端（端口 5173）

```powershell
cd frontend; npm run dev
```

Vite 会把 `/api` 代理到 `http://localhost:8000`，所以两个服务都要开着。

### 3. 打开页面

浏览器访问 **http://localhost:5173**

首次使用建议按这个顺序走：

1. **LLM 配置** —— 填 Base URL、模型名、API Key（以及后端的 `TWEAKERS_API_KEY`），点「测试连接」
2. **数据源** —— 注册一个采集来源（如 Tweakers 的帖子 ID）
3. **任务** —— 用自然语言描述要做的事，例如「采集所有来源的数据，翻译成中文并分析舆情」

---

## 局域网访问

默认只绑 `127.0.0.1`，因为未配置 `TWEAKERS_API_KEY` 时接口是没有鉴权的。
确需让局域网内其他机器访问时，**四步都要做**：

1. 在 `.env` 中设置 `TWEAKERS_API_KEY=<共享密钥>`
2. 在 `.env` 中设置 `TWEAKERS_ENABLE_DOCS=false`，关闭接口文档
3. 后端启动参数改为 `--host 0.0.0.0`
4. 在前端「LLM 配置」页填入同一个密钥

---

## 生产构建

```powershell
cd frontend; npm run build
```

该命令等价于 `vue-tsc -b && vite build`，产物在 `frontend/dist`。

> **注意**：后端目前**不托管**这份静态产物（`main.py` 只提供 API）。
> 生产部署需要另外配置 web 服务器（如 nginx）来发布 `dist`，并把 `/api` 反向代理到 8000 端口。

---

## 运行测试

在**仓库根目录**执行：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\ -v
```

当前共 **261 个用例**，本机约 340 秒跑完，必须全部通过。

只跑某一个测试类：

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend\tests\test_core.py::TestSearchFilteringEndToEnd -v
```

测试遵循「真实验证」原则：起真实的本地站点、跑真实的 Chrome 子进程、读写真实的 SQLite，
不使用 mock 绕过。本地 fixture 站点位于 `backend/tests/fixtures/`。

---

## 目录结构

```
hyxi/
├── backend/
│   ├── main.py                  FastAPI 入口（lifespan 建目录 + 启停调度器）
│   ├── requirements.txt
│   ├── app/
│   │   ├── routers/             tasks / results / config / schedules / sources
│   │   ├── services/            orchestrator、llm_service、translator、sentiment、
│   │   │                        excel、storage、scheduler、collector_runner 等
│   │   └── collectors/          各采集器的声明类（纯声明，不含抓取逻辑）
│   ├── tests/                   pytest 用例 + 本地 fixture 站点
│   └── data/                    运行时数据（见下节，已 gitignore）
├── collectors/                  Node / Playwright 采集脚本
│   ├── tweakers.js              Tweakers.net 论坛
│   ├── facebook_group.js        Facebook 小组（需登录）
│   ├── group_feed.js            通用信息流采集器（供测试使用）
│   └── lib/                     browser / auth / job 等公共模块
├── frontend/
│   └── src/                     views / api / stores / components
├── start.ps1                    一键启动脚本（含就绪验证）
├── package.json                 仅 playwright，供采集脚本使用
├── .env                         密钥配置（不提交）
└── CLAUDE.md                    面向 AI 助手的项目工作说明
```

---

## 数据存放

**业务数据一律进 SQLite，没有 JSON 存储。**

| 路径 | 内容 |
|---|---|
| `backend/data/hyxi.db` | 帖子、任务、舆情结论、数据源、凭据、LLM 配置、定时任务、调度器作业 |
| `backend/data/media/` | 采集下载的正文图片 |
| `backend/data/logs/app.log` | 滚动日志（5MB × 3） |
| `backend/data/sessions/` | Playwright 登录会话缓存，丢失只需重新授权一次 |
| `backend/data/jobs/` | 交给采集子进程的入参与产出，**用完即删**，不是持久化 |

以上目录全部已 gitignore。备份时只需要拷 `hyxi.db`（连同 `-wal` / `-shm`）与 `media/`。

---

## 常见问题

**`CommandNotFoundException`：无法识别 `backend\.venv\Scripts\python.exe`**
PowerShell 5.1 下调用相对路径的可执行文件**必须带 `.\` 前缀**。另外 `&&` 在 PS 5.1 中是语法错误，
串联多条命令请用 `;`。

**抓取步骤报 `Cannot find module 'playwright'`**
采集依赖没装在项目根目录。回到仓库根目录执行 `npm ci`，`frontend/node_modules` 里的那份不顶用。

**采集启动时报找不到 Chrome**
脚本用 `channel: 'chrome'` 启动，需要本机安装**真实的 Google Chrome**，
Playwright 自带的 chromium 或 `%LOCALAPPDATA%\ms-playwright` 里的浏览器都不满足。

**Tweakers 采集持续返回 403**
这是出口 IP 被目标站防火墙整体封禁，页面原文为
「De toegang tot Tweakers vanaf dit IP is geweigerd」。调整 UA、延时都没有意义，
需要解封或更换出网机器。

**端口被占用**
```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess
```
拿到 PID 后用 `Stop-Process -Id <PID>` 结束，或改用其他端口启动。

**修改后端代码后不生效**
启动命令没有加 `--reload`，改完代码需要重启后端进程。

**macOS / Linux 下怎么跑**
把 `.\backend\.venv\Scripts\python.exe` 换成 `backend/.venv/bin/python`，
命令分隔符 `;` 换成 `&&`，其余步骤完全相同。
（该路径未在本机验证过，目前的开发与测试环境均为 Windows。）
