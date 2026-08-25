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

**为什么不留双写**：同一份数据存两处、两个读者各读一份，必然长出「改了一边、另一边还是旧的」的 bug。实测踩过：修完 `sentiment_*.json` 后 `/sentiment` 仍返回旧数据，因为它读 SQLite 而 `/export` 读 JSON。

**舆情结论按帖子身份存，不按下标**。下标只在写入现场有意义 —— 离开那里就没人能保证它对得上哪条帖子（已因此出过一次事故，见「常见陷阱」）。`storage.save_sentiment()` 收到的仍是下标数组，落库时立刻换成 `(source_id, fingerprint)`；`get_sentiment(task_id, posts)` 再按当前帖子顺序还原成前端要的下标数组，**API 响应形状不变**。副作用是 `total` / `failed` 现在按当前帖子实算，而不是分析那一刻冻结的数字 —— `/sentiment` 因此与 `/posts`、`/stats`、`/export` 报同一个数。

**结论也不按 task_id 存**。它是对帖子下的，哪个任务触发的分析不改变它对哪条帖子成立。`posts.sentiment_at` 本来就跨任务共享（同一条帖子不重复花钱分析），结论必须同一个粒度 —— 按任务过滤的话，第二个任务跑同一批数据，页面上 94 条里 90 条显示「未分析」，而它们早分析过了（用户实测报过）。这与「绝不跨任务顶替」不矛盾：当初出事的是**按下标**取别的任务那份按别人帖子列表编号的整数组；现在按 `(source_id, fingerprint)` 取，取到的就是这条帖子自己的结论，取不到就是真没有。`sentiment_runs` 因此只剩「本任务最近一次触发的时间」这一个用途，summary 全在读取时现算。

**「分析中」必须在取结论之前判**（`results.py` 的 `GET /sentiment`）。结论跨任务共享后，只要这批帖子里有一条被别的任务分析过，`_task_sentiment()` 就返回真值；放在后面判会让前端认为分析已结束，既不连 SSE 也不再轮询。

`intensity` 列必须是 `NUMERIC` 不能是 `REAL`：REAL 亲和性会把整数 3 存成 3.0，导出的强度列跟着变成「3.0」。

**`posts.seq` 是全链路的顺序锚点**。改造前「扁平数组的下标即顺序」这件事有 8 处依赖（增量过滤、指纹合并、翻译下标对应、舆情绝对索引、Excel、`/posts` 切片、`index` 语义、Node 端合并），入库后由它完整承担：按 source 单调递增、**已有帖子的 seq 永不变**、新帖追加在后。读取一律 `ORDER BY seq`，跨来源拼接顺序由调用方给的 `source_ids` 决定。它一洗牌，所有历史舆情结论就会错位到别的帖子上。

**`posts` 故意不挂 `sources` 外键**：`ON DELETE CASCADE` 会让「删数据源」把历史任务的结果一起清空，而那正是要避免的（见「常见陷阱」）。代价是删源后帖子会留下来，与改造前「删源后落盘 JSON 仍在」一致。

**采集脚本不再读旧数据**。它只输出本轮新抓到的，合并由 `storage.upsert_posts()` 做：已存在的帖子**只更新采集字段**，绝不覆盖 `translation` / `translated` / `sentiment_at`。增量所需的 `known_fingerprints` 与续抓页码由 Python 从库里算好，通过 job 文件下发。

**迁移**：启动时 `migrate_from_json()` 幂等执行，源文件移进 `data/_migrated_backup/` 而不是删掉。舆情从旧的整块 JSON 列搬到键控表时，靠一条**可检验的不变量**兜底 —— 凡是 `results[i]` 有结论，第 i 条帖子必须已带 `sentiment_at`；对不上就整份跳过并保留原数据。**结果条数与帖子条数不等也整份跳过**，不许「只迁对得上的前缀」—— 那假设了缺文件的来源排在末尾，而实测踩过反例（tweakers 排在前、文件被删，幸存的 8 条 group_feed 帖子套上了 tweakers 的结论，5 条错位，且上面那条不变量拦不住）。

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

`collectors/lib/auth.js` 是唯一的登录入口，`facebook_group.js` 用它。顺序是**会话优先**：先用 `storageState` 打开目标页，选择器判定已登录就直接返回（日志打「复用会话，跳过登录」），只有会话失效才动用密码。这就是「第一次登录后避免每次都用账号密码」的落点。

会话按 **source 而非 collector** 隔离，落 `backend/data/sessions/{source_id}.json`（已 gitignore）—— 同一个采集器可能挂两个账号。

登录后落地页分四种，处置完全不同，混成一个「登录失败」会让用户无从下手：成功 / `checkpoint`（URL 含 `/checkpoint/`）/ `two_factor`（出现验证码输入框）/ `bad_credentials`。**非成功一律 `emit('need_manual_auth')` + 退出码 3**，`CollectorRunner` 转成 `ManualAuthRequired`，orchestrator 把它的消息原样写进 `error_message` —— 那句话本身就是给用户看的人话（「数据源「X」需要人工重新授权：……请到「数据源」页点「人工登录」」），**别再包一层技术描述把它埋掉**。

`POST /api/v1/sources/{id}/authorize` 走 `mode: "login_only"` + `headless: false`：开有头 Chrome 让人自己过两步验证，轮询到登录成功或超时（默认 10 分钟，`manual_login_timeout_ms` 可配），成功即存会话 + `mark_authorized()`。进度走 `progress_manager` 的 `auth_{source_id}` 频道。**这是「撞上验证就交给人，不硬闯」既有姿态的落点，不是绕过验证码。**

**窗口弹在运行后端的那台机器的桌面上**，不在调用方的浏览器里。当前后端与用户同在一个 RDP 会话所以看得见；后端搬去无桌面服务器后这个入口即失效，前端面板第一步就是在说这件事。前端倒计时 `AUTH_TIMEOUT_SECONDS` 与后端 `manual_login_timeout_ms` 是两份独立常量，**改一边必须改另一边**，否则页面会在脚本还在等的时候先归零（或反过来）。超时提示里的分钟数由 `CONFIG.manualLoginTimeout` 算出，别写死。

**人中途关掉那个窗口是正常动作**（面板第四步本来就叫他关），`waitForManualLogin()` 因此返回 `'ok' | 'closed' | 'timeout'` 三态：不接住 `page.$` 会抛 `Target page, context or browser has been closed` 并以退出码 1 把一段 Playwright 堆栈送到界面上。`closed` 走的是和超时一样的退出码 3，给出「浏览器窗口被关闭，授权未完成」。

**`last_auth_at` 必须两头都写**：`mark_authorized()` 在授权成功时写入，`clear_authorization()` 在退出码 3 落地时清空（清在 `CollectorRunner` 里，因为采集和人工授权超时两条路都要翻徽标）。只写不清会让界面在会话过期后一直显示「会话正常」——而采集恰恰正因会话失效在失败，用户被指向错误的排查方向。回归测试见 `TestSessionStateReflectsReality`。

> ⚠️ **Facebook 服务条款禁止自动化登录与抓取，账号可能被封。用专用小号，不要复用任何有价值的账号。**

### 真站实测结论（2026-08-03，对 facebook.com 实跑）

本机能连通 facebook.com（DNS 57.144.220.1，HTTPS 200），不像 Tweakers 那样被封。以下全是实测，**改登录相关代码前先读这一段，别照着猜再叠选择器**：

- **未登录访问 `/groups/{id}` 会 302 到 `/login/?next=...`**，是登录墙不是只读预览。所以 `ensureLogin()` 落地时通常已经在登录页上，不必再跳一次
- **登录表单是 `form#login_form`（`method=post`），输入框 id 是随机的**（形如 `_R_1h6kqsqppb6amH1_`），只能按 `input[name="email"]` / `input[name="pass"]` 选。页面上**没有 `[data-testid]`，也没有 `[name="login"]`**
- **提交必须靠在密码框按回车，不要点按钮**：`input[type="submit"]` 是 0×0 不可见的；表单里 DOM 顺序第一个 `[role="button"]` 是 24×24 的**「显示密码」图标**——按选择器点过去只会把密码显示出来，表单一次都没提交，而页面看上去毫无变化，极难察觉
- **`[role="alert"]` 绝不能当登录错误**：密码框一被填入，页面就冒出一条 aria-live 提示（荷兰语 `Je wachtwoord wordt weergegeven`），当成错误会让**每一次正常登录都被判成密码不对**。`loginError` 只认 `#error_box`
- **提交后不要赌它整页导航**：`waitForLanding()` 轮询到状态确定为止（`classifyLanding()` 对「还停在登录页且没报错」返回 `pending`），导航还是 AJAX 都不影响。当场判死会把提交到落地之间那一段误报成密码错
- **真实拦截点是 Arkose Labs 人机验证**：落地 URL 为 `/two_step_verification/authentication/?...&flow=pre_authentication`，正文提到 “MatchKey van Arkose Labs”。这不是输个短信码就完事的两步验证，**必须人来过** —— `classifyLanding()` 把它归入 `checkpoint`，脚本以退出码 3 交回给人。破解它属于「明确不做」
- **把输入改成逐字键入（`humanType`）后实测无任何变化，照样弹人机验证**。所以别再往「模拟得更像人」这个方向调登录了：触发信号是账号 + 出口 IP 归属地 + 全新浏览器指纹这个组合，不是打字速度。`humanType` 保留（零耗时填完一整个密码本来就不该出现），但它解决不了这件事。**唯一可靠的路径是人工授权一次，之后复用会话**
- **`/login/?next=...` 不会跳注册页**：25 秒内 URL 一次都没变、`input[name="email"]` / `input[name="pass"]` 全程存在、页面上没有任何注册表单。曾有人报「人工登录跳到注册页」，实际是 `locale: 'nl-NL'`（从 `tweakers.js` 抄来的）把界面整页切成荷兰语，而最抢眼的绿色按钮写着 `Nieuw account maken`（指向 `/reg/?entry_point=login&next=...`）。**`facebook_group.js` 的 locale 是 `zh-CN`，别再抄 Tweakers 那份** —— 人工授权窗口是给操作者看的，还顺带与宿主机时区 `Asia/Shanghai` + 中国出口 IP 自洽
- **locale 只管未登录界面**：登录之后 Facebook 按账号自己的语言设置渲染，与 accept-language 无关。所以 `SELECTORS.loggedIn` 里 `[aria-label="创建帖子"]` 和 `[aria-label="Create a post"]` 两个都得留着，真正扛事的是语言无关的 `[role="feed"]` / `[data-pagelet^="GroupsFeed"]`
- **不要用 curl 诊断 Facebook**：同一个 `/login/?next=...`，curl 拿到的是 **400 “Sorry, something went wrong.”**，真 Chrome 拿到的是 **200 完整登录页**。它按 TLS / 请求头指纹区别对待，用 curl 取证会得出完全相反的结论
- **人工授权的轮询必须扛得住导航**：人在窗口里每操作一步都是一次导航，2 秒一次的 `page.$` 撞上去就抛 `Execution context was destroyed`。真实发生过 —— 用户提交密码那一刻脚本以退出码 1 死掉、窗口被连带关闭，刚输的东西全白费。`waitForManualLogin()` 现在把这类导航瞬态当成「这轮没查成」继续轮询，只有 `has been closed` 才判定人放弃了授权

`tests/fixtures/login_site.py` 已按上述结论复刻：302 跳转、随机 id、0×0 的 submit、那个会抢走点击的「显示密码」图标，以及 `landing="reg"`（未登录访问小组页改跳注册页）/ `landing="churn"`（跳一个不停自我导航的页面）两个分支 —— 测的就是真实路径上的坑。它还把每个请求的 `Accept-Language` 记进 `request_languages`，是「窗口对操作者可读」这件事唯一不依赖真站的观测点。

### 小组页 DOM 实测结论（2026-08-04，人工授权拿到真实会话后实跑）

提取器已照真实页面校准，不再是推测。**改它之前先读这一段**：

- **页面上没有 `abbr[data-utime]`，也没有 `data-post-id` / `data-comment-id`**。主贴 id 从头部固定链接的 `/posts/{id}` 取，评论 id 从 `comment_id={id}` 取
- **没有 `h3`，也没有 `strong a`**。作者只剩小组内的个人主页链接 `a[href*="/user/"]`，而同一个人会连出好几个这样的链接，**排在前面的是头像、文本为空** —— `querySelector` 取到的正是空的那个。必须取第一个**有文字**的（`nameOf()`）。踩过：58 条帖子全成了匿名
- **时间只能 hover 读 tooltip**（`[role="tooltip"]`，headless 下照样出，形如 `2026年7月28日周二19:53`）。页面上另外两个时间来源都有毒：主贴头部链接的 `aria-label` 是**相对时间**（「6天」），评论的 `aria-label` 是绝对时间但走 **Facebook 账号自己的时区**（实测比宿主机早 15 小时 = PDT vs `Asia/Shanghai`），主贴和评论根本对不上
- **`timestamp` 进指纹，所以解析不出绝对时间就留空，绝不原样保留**。写个「6天」进去，明天再抓同一条帖子就是「7天」→ 新指纹 → 全部历史数据判成新帖，已翻译的重复付费翻译、舆情重复计数
- **取不到作者时一律填同一个「匿名」，不要按序号编名字**。信息流每一批都会把上一批的帖子重新提取一遍，序号跟着变而 `username` 也进指纹 —— 实测同一条帖子在两个批次里拿到两个指纹，一轮抓下来数据翻倍
- **tooltip 结果按链接缓存**（`timeCache` + DOM 上的 `data-hyxi-t` 标记）：滚动是往下追加，不缓存就要把同一条帖子 hover 十遍
- **长正文必须先点开「展开」再提取**（`expandBodies()`）。Facebook 对长帖只渲染前几行，末尾挂一个 `div[role="button"]`，文字是「展开」。不点它，`textContent` 拿到的是**残缺正文 + 「展开」两个字** —— 实测一条帖子整个正文只剩 16 个字符（`Goedemiddag,… 展开`），点开后 208 个；152 条里 22 条中招。残文比没有更糟：它看起来是完整的一句话，翻译和舆情都照着它算。**按文字认按钮**，登录后的界面语言由账号自己的设置决定、与采集器 locale 无关，所以中英荷三种文案都收
- **展开后按钮文字变成「收起」，同样会被 `textContent` 吃进正文**，和没点开时残留的「… 展开」一样都要剥掉（`bodyTrail`）。它们进的是 `content` 前 100 字 = 指纹，留着等于把界面文案写进去重锚点，且同一条帖子展开前后会算出两个指纹。已展开的按钮文字已经不是「展开」，所以每批重新提取时不会重复点击
- **评论正文是一串并列的 `div[dir="auto"]`，一段一个**（`commentText()`）。`querySelector` 只拿第一段 —— 实测一条 9 段的评论只存下第一段的 71 个字符，原文 811 个，丢了 92%；一轮 42 条评论里 13 条多段，共丢 2792 个字符。**层级限定不能省**：嵌套回复也是 `article`，不加 `closest(sel.post) === root` 就会把子回复的正文并进父评论，父子两条都错。主贴不走这条路（取的是 `[data-ad-comet-preview="message"]` 容器的 textContent，本来就含全部段落），所以主贴段落之间没有换行、评论之间有 —— 这是两条不同的取法，不是 bug
- **「查看N条回复」/「查看更多评论」目前不点，那部分评论根本没进 DOM**（实测一页上有 26 条这样的回复）。要补齐得循环点击并等加载，会显著增加请求量，与反爬虫姿态需要一并权衡
- **信息流里混着不是帖子的 `role="article"`**（广告、推荐小组卡片），既没有固定链接也没有正文容器，`flatten()` 用 `isNotAPost()` 把它们丢掉。存下来就是一条四个字段全空的记录 —— 白占一次翻译调用（清理前的落盘数据里有 55 条这种，全被标成「已翻译」），还会在结果页显示成一条什么都没有的帖子。`isNotAPost()` 的判据是 id 和正文全都没有。**正文为空但有 id 的那一类不在这里拦**（见下条）
- **既没正文、又没配图、下面也没人发言的帖子一条都不入库**，拦在 `storage.drop_empty_posts()`（`upsert_posts()` 开头调用）。它翻译不了（实测那条被标成「已翻译」、译文是空串）、舆情永远分析不了，在报告和舆情页上就一直挂着一行什么都没有的「未分析」。**拦在入库口而不是采集脚本里**，理由有两条：一是 `upsert_posts()` 是 posts 表唯一的门，三个采集器和以后新加的都不用各自记得过滤；二是**在 `flatten()` 里过滤会把评论一起带走** —— 它是「主贴 → 它的评论」嵌套遍历的。真被丢掉的父贴（整棵子树都没内容）才把孤儿评论就地提成主贴（`parent_fingerprint=None`、`reply_level=0`），不留悬空 parent。回归测试见 `TestEmptyPostsNeverStoredEndToEnd`
- **判据必须同时看 `images`，只看正文会静默丢掉纯图帖**（踩过，见下节）。这里只能看 `images` 不能看 `image_desc` —— 入库时图还没被理解过
- **「空正文 + 下面有人发言」一律保留，不许丢**（`anchored` 那段）。没人会去评论一片空白，所以这几乎一定是**正文提取失败**而不是真的空帖。丢了它，评论会被提成主贴，而**那个提升不可逆**：指纹吃正文，下一轮正文提对了父贴就换指纹重新入库，评论却还挂在主贴身份上，除非它恰好又被重新提取到（Facebook 会把老帖的评论折叠起来，而「查看N条回复」目前不点，所以往往就是不会）。真站核实过两例 ——「Mijn HyXi Halo is gekoppeld aan:」那条正文没提出来、以及一条纯图主贴，各自的评论都被提成了主贴，其中「Is bij mij ook zo…」因此按孤立主贴判成 neutral，而它实际在附和一条报「发电异常 8/20」的故障帖。代价是报告里会留一行空白的「未分析」，但那是实话：「这里有条读不出来的帖子，以下是它的回复」，比把回复冒充成主贴诚实得多。用**空评论捞空父贴不算数** —— 整棵子树都没内容时它就是真的什么都没有。真 Chrome 回归测试见 `TestGroupFeedCollectorEndToEnd::test_root_whose_body_failed_to_extract_survives_with_its_comment`
- 保留空父贴**换来的另一个代价**：同一条真实帖子可能在库里留下两行。指纹吃正文，所以「正文提对了」和「正文没提出来」是两个指纹 —— 某一轮提取失败时，那个空指纹会作为新行入库，评论跟着挂过去，原来那行好的反而没了评论。**但它是可恢复的**：下一轮提对了，评论的指纹不变（指纹不含父贴），`upsert_posts()` 会把 `parent_fingerprint` 更新回好的那行。这正是它比「提升成主贴」强的地方 —— 后者不可逆。真要根治得按 `message_id` 去重（它跨提取稳定，指纹不稳定），那是另一件事，现在没做。**同一个缺口还有另一面**：正文为空时指纹只剩 `username|timestamp`，而作者读不出会填「匿名」、时间读不出会留空 —— 一批里同时出现两条三项全失败的帖子就会算出同一个指纹，合成一行，两拨评论并到一个父贴下（舆情因此拿到一串混起来的讨论）。真站实测「时间 / 作者 / `message_id` 无一为空」，所以要三重失败同时发生；补救仍是上面那条 `message_id` 去重，**别去改指纹算法**（历史数据会全部失配，已翻译的帖子要重新付费）
- 这类被留下的父贴在舆情 prompt 里用 `UNREADABLE_PLACEHOLDER`（「本帖正文为空或未能提取」）占位，**不能留空**（一行光秃秃的「主贴 @某人:」看着像渲染坏了，模型会当它什么都没说），也**不能套 `NO_TEXT_PLACEHOLDER`**（「内容全在配图上」是假话，后面没有 `[图片: ...]` 跟着，等于让模型去读一张不存在的图）。系统提示词里另有一条告诉模型别把「没取到」当成「什么都没说」
- **历史迁移不能套用这条规则**：`migrate_posts_file()` 传 `drop_empty=False`。迁移是**照原样重建**，旧舆情 blob 的 `results[i]` 对齐的正是那个数组的第 i 条 —— 少搬一条，条数就对不上，`migrate_sentiment_blob()` 的「条数不等整份跳过」会把那个来源的历史结论**永久**挡在门外，且没有任何报错。回归测试见 `TestPostsStorageEndToEnd::test_migration_keeps_empty_posts_so_sentiment_blob_still_aligns`
- **正文图是 `<img>`，host 在 `scontent-*.xx.fbcdn.net` 上，渲染尺寸几百像素**（实测 367×795 这个量级，`alt` 是 Facebook 自动生成的「可能是包含下列内容的图片：…」）。界面图标是 `data:image/svg+xml`（16~18px）、emoji 在 `static.xx.fbcdn.net`，按 host 一刀就切干净；**头像是 `<svg><image>` 不是 `img`**，压根不会被 `querySelectorAll('img')` 选中。尺寸下限是第二道保险
- **图片不必额外滚动去凑**：实测滚到底后逐个主贴 `scrollIntoView`，原有 15 个主贴的图片数**一张没变**（1→1、0→0），多出来的全是新滚出来的帖子 —— 也就是说「这条没图」是真没图，不是懒加载没触发
- **真实 `message_id` 实测全是 16 位纯数字**。`p1` / `c1` 这种前缀式 id 只有 fixture 站点会生成 —— 落盘数据里出现它就说明某次测试写进了真实文件（发生过 3 条）

实测证据（真 facebook.com 小组 `2407063016436085`）：2 批时 41 条 = 19 主贴 + 22 评论；3 批时 76 条 = 34 主贴 + 42 评论，**时间 / 作者 / `message_id` 无一为空，四字段全空 0 条，含展开/收起标记 0 条，指纹重复 0 组**；**保留结果再跑一次增量：提取 16 条、新增 0 条** —— 展开是确定性的，指纹跨进程稳定。

`tests/fixtures/login_site.py` 的小组页按上述结构复刻，连坑一起：文本为空的头像链接排在作者链接前面、主贴 `aria-label` 给相对时间、评论 `aria-label` 给一个时区错开的绝对时间、tooltip 靠 `mouseover` 现挂、最后一条主贴的正文是折叠的（点「展开」才换成全文并把按钮变成「收起」）、中间夹一条没有链接也没有正文的广告 `article`、第一条评论是 3 段并列的 `div[dir=auto]` 且里面还嵌着一条回复。

### 已排除的三条路（核实过，不要再重复讨论）

「能不能把 Facebook 登录搬进我们自己的页面里，人工输密码，这样就合规了」——方向对，但三种落法全部走不通，逐条核实过：

| 思路 | 判定 | 依据 |
|---|---|---|
| iframe 内嵌 facebook.com 登录页 | **技术上不可能** | 本机实测 `HEAD https://www.facebook.com/login/` → `X-Frame-Options: DENY`。浏览器强制执行，无绕过手段 |
| 自建仿 Facebook 登录表单代收密码 | **可行但绝不做** | 密码会多经过前端 JS、请求体、后端内存、日志、422 报文——本项目已为此踩过坑（FastAPI 默认回显提交值，实测漏过明文密码，靠 `main.py` 的 `RequestValidationError` handler 才堵上）。形态上即钓鱼，且照样过不了 Arkose |
| 官方 Graph API + OAuth | **已不存在** | Graph API v19.0 changelog：`publish_to_groups`、`groups_access_member_info`、Groups API 于 **2024-04-22 移除**。不是「需审核」，是「已删除」 |

于是落点是「本机真实窗口 + 页面内引导」：不搬画面，把「在系统页面里」落在发起、分步引导与状态回显上。**合法性不因「谁输的密码」而改变**——ToS 限制的是自动化采集内容本身；人工授权去掉的是「自动化登录」这一项，属实打实的安全改善（系统彻底不接触密码），后续脚本翻页提取仍在同一片灰色地带，上面那条小号警告继续有效。

把真实浏览器画面用 CDP `Page.startScreencast` 推到页面 canvas（输入经 `Input.dispatch*` 回传）的方案**已评估，暂缓**：唯一不可替代的价值是「后端可部署到无桌面服务器」，而那件事还没发生；代价是 400–500 行跨 Node/Python/前端的新 WebSocket 通道（项目目前零 WS），且在唯一卡点上引入了未验证的不确定性（CDP 转发的输入能否过 Arkose）。**触发条件**：后端真要搬去无桌面机器时再实施。

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

## 任务流水线

LLM 解析用户自然语言 → 生成执行计划 `[{action, params}]` → 逐步执行：

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

## 正文图的抓取（tweakers + facebook_group）

**图片字节取自浏览器自己的响应，不再二次回源。** `collectors/lib/media.js` 是唯一实现，
两个采集器共用；`group_feed.js` 没有图片能力（它只服务本地 fixture）。

- **`context.request` 是 Playwright 在 Node 进程里自带的 HTTP 客户端，不是 Chrome。**
  实测（2026-08-21）它连 `Sec-Fetch-Dest` / `Referer` / `sec-ch-ua` 都不带，更**不读系统
  代理** —— `playwright-core` 整个包里 `HTTPS_PROXY` 出现 **0 次**，`browser.js` 也从没给它
  传过 `proxy`。于是「Chrome 走系统代理把图显示出来了、脚本直连回源却连不上」是一个完全
  可能的状态：**页面抓得到、帖子入库了、图一张都没有**。用户实测报过，而开发机跑的 fixture
  打的是 `127.0.0.1`、代理对 localhost 一律不生效，所以旧实现**在测试里永远是绿的**
- 现在 `attachImageCapture(page)` 挂 `page.on('response')` 把图片响应体留下来，
  `saveImages()` 直接写盘。**必须在任何导航之前挂**，晚一步第一屏的图就漏了。
  顺带把图片请求数从「页面加载一次 + 回源一次」减半，与反爬虫姿态一致
- **缓存读了不删**（同一张图可能配在两条帖子上），内存由 64MB 总量上限 + FIFO 驱逐兜住。
  覆盖同一个键前必须把旧字节数减掉 —— `bytes` 一旦漂过上限，驱逐循环每插一张就把整个缓存
  清空一次，所有图片于是**静默**退回回源那条路，正是要摆脱的那条
- **按重定向链建别名**：图片被 302 时 DOM 上的 `src` 是原始 URL、`response` 报的是最终 URL，
  不建别名命中率直接归零
- 回源仍保留作兜底（懒加载没触发、或直接命中 Chrome 内存缓存因而没有 response 事件），
  **两次回源之间的 300ms 间隔不能去掉** —— 请求节奏是反爬纪律
- **两个阶段都必须出声**。「页面上就没有图」「选择器没选中」「被尺寸门限挡了」「下载失败」
  以前在日志上长得一模一样：什么都不显示，这正是这个问题拖到用户那边才暴露的原因。现在
  提取阶段报候选/命中/排除原因（含被排除图的 host 与渲染尺寸），下载阶段报真实报错原文
  （`ECONNREFUSED` / `ETIMEDOUT` / `407` 指向完全不同的处置），跑完再来一行
  `图片汇总：候选图片地址 N 个 · 通过筛选 N 个 · 落盘 N 张…`
- **汇总里前两个数按 URL 去重、后两个是文件数**，措辞必须区分：信息流每一批都会把上一批的
  帖子重新提取一遍，逐批累加的话 10 批下来会虚报出 82% 的假丢失率 —— 而这行恰恰是远端
  出问题时唯一的诊断依据，它自己说谎就白做了
- **Tweakers 侧的选择器没有对真站核实过**（出口 IP 被封，本机访问不到）。所以那边
  **不设 host 白名单**，只按「在正文容器内」+「非 `data:` URI」+「渲染尺寸 ≥ 80」筛，
  剩下的交给上面那些日志：用户机器上跑一轮就有真实 host 和尺寸可依。
  两个坑已经踩过并钉住了：**尺寸必须在原始元素上量**（剥引用块用的那个 `cloneNode` 游离于
  文档之外，`getBoundingClientRect()` 一律返回 0，照着 clone 取会把每张图都过滤掉）；
  **取图的容器要跟着正文走**（没有 `.messagecontent` 时正文来自 `.post`，盯死前者会让这条
  路上的帖子静默丢图）
- 回归测试：`TestFacebookLoginEndToEnd::test_images_survive_when_only_the_browser_can_fetch_them`
  让 fixture 服务器**按真实请求头**把非浏览器发起的图片请求一律 502，忠实复刻用户那台机器的
  状态（真服务器、真 Chrome、真子进程，无 mock）；
  `TestTweakersCollectorGoldenEndToEnd::test_body_images_are_downloaded_and_quoted_ones_ignored`
  钉住引用块、表情、跨页同 URL 去重。**`golden_tweakers.json` 那条基线同时保证指纹没动** ——
  `<img>` 不贡献 `textContent`，正文一个字符都不能变，否则历史帖子全被判成新帖重新付费翻译

## 图片理解与整串上下文（只作用于舆情）

**舆情分析前，带图帖子的配图先交给多模态模型转成中文描述**，再连同正文一起发给大语言模型。
配置走 `app_config` 的 `vision.` 前缀（`get_app_config` 本来就按前缀分组，**没动任何表结构**），
未配置时 `get_vision_service()` 返回 `None`，整条链路降级为纯文本。

- **只有舆情走这条路，翻译不走**。译文对着的是正文，塞图进去只会污染译文，还要为每条带图
  帖子多付一次多模态调用。回归测试见 `TestImageUnderstandingEndToEnd::test_translation_never_touches_the_vision_model`
- **多模态的系统提示词复用翻译那份角色**（`INDUSTRY_ROLE` + `INDUSTRY_GLOSSARY`，从
  `translator_service` import）。看懂储能设备照片、App 截图、配电箱接线图靠的正是那张术语表。
  抽常量时 `TRANSLATION_SYSTEM_PROMPT` 拼出来**必须逐字节不变**，否则几百条帖子的译文口径跟着变
- **prompt 里只让它描述、不让它下结论**（「不要写「用户对此不满」这类结论」）。混在一起会让
  舆情判定被描述者带跑，而那正是下一步要独立做的事
- **描述落 `posts.image_desc` 且重采集不覆盖**（与 `translation` / `sentiment_at` 同规矩）。
  主贴的图会被它下面**每一条回复**的整串上下文引用，不存的话增量每轮都要把同一张图重新买一次
- **失败一律降级不中断**：模型没配、配额 403、图丢了、路径越界 —— 都只是这条没有描述，
  舆情照常按正文跑完。为一张图让整轮分析失败是不可接受的

**回复贴的判定要看整串，不是父贴前 200 字**。`post_tree.thread_of()` 把每条帖子映射到它所属的
完整讨论串（嵌套回复归到顶层主贴那一串），`_post_block()` 渲染成带 `▶` 标记的上下文块，
注明哪一条才是待分析对象。「+1」「same here」单独看必然是 neutral 噪音 —— 实测 60 条回复里
大量是这种。**`thread_by_key` 必须基于 `all_posts` 而不是 `pending_posts`**：增量时待分析的
往往只是一条新回复，只拿它自己组串等于没有上下文。整串块有 `THREAD_CONTEXT_LIMIT`（3000 字符）
上限，裁剪时**主贴与待分析条目永远保留**。

**`/config/vision/test` 成功不代表图片理解可用**：`test_connection()` 先探 `/models`，而
**配额用尽时那个接口照样返回 200**（实测 Kimi 就是这样：`/models` 200，`chat/completions` 403
`access_terminated_error`）。界面文案已经写明这一点，别把它简化掉。

### Kimi 真机实测结论（2026-08-14，配额恢复后对 api.kimi.com 实跑）

**`kimi-for-coding` 支持图片输入**，实测能准确读出 App 弹窗里的荷兰语文案与设备序列号。
`k3` / `k3-256k` 同样可用；`kimi-for-coding-highspeed` 返回 401（订阅层级不含）。
**改这条链路的参数前先读这一段，别照着「常规做法」猜**：

- **不能传 `temperature`**。它只接受 `1`，传 `0.2` 会被整个请求打回
  `400 invalid temperature: only 1 is allowed for this model`，再被降级逻辑吞成
  「这条没有描述」—— 功能看着在跑，一张图都没理解过。各家视觉模型对这个参数约束不同，
  一律交给服务端默认值
- **`max_tokens` 必须给够，因为它是推理模型**。`reasoning_content` 与正文分开返回，
  但**照样计入 `max_tokens`**。实测给 512 时 512 个全被推理吃掉：HTTP **200**、
  `finish_reason=length`、`content` 是**空串**。这比报错难查得多 —— 没有任何异常信号，
  只是所有图片都「理解成功但没有描述」。实测推理约 300~700 token，`MAX_OUTPUT_TOKENS`
  取 2048。`describe_post_images()` 在拿到空描述时会单独打一条警告点名这个原因
- **预算给够了照样会偶发跑飞，所以拿到空描述必须重打一次**。同一张图，模型偶尔陷进长推理
  把整个预算烧在 reasoning 上。**调大预算解决不了**：实测 4096 也被烧穿过（4096 全进推理），
  只是每次跑飞多浪费一倍 token。实测单次失败率约 12%（84 次调用 10 次空），重打一次后
  23 张图的一轮期望漏网不到 1 张；仍漏的那张只是按纯文本分析，不影响整轮
- 这三条都有回归测试，且 `tests/fixtures/vision_site.py` 把三种真机行为一起复刻了
  （temperature 不为 1 就 400；`max_tokens` 低于门限就返回 200 + 空 content；
  `empty_first=N` 让前 N 次无条件跑飞，用来钉住重试）

真机证据（真实帖子 `src_b32bc603` + 真实 31685 字节 JPEG）：多模态给出
「HyXi Halo App设置界面弹窗提示：当前固件版本不支持并联运行，需先升级至最新固件。
弹窗显示设备序列号SN:34302260600373……」，与主贴正文所述完全对得上；该描述随整串上下文
进入文本模型，回复贴拿到 `neutral / 固件更新 + 扩展/兼容性` 的结论。
库里 23 条带图帖子整批复跑（真实库、真实图、真实模型）：不重试 21/23，加上重试 22/23。

**增量粒度仍是 `sentiment_at`，所以改了分析口径必须走 `?force=true`**。接上图片理解那天，
库里 124/125 条已分析、23 条带图的**全部**已分析 —— 不重跑的话新功能在现有数据上一点变化都
看不到。舆情页的「🔄 强制重新分析」就是这个入口，点击弹确认框（它会重新花钱）。

**舆情分析的另一条路是结果页按钮**（`POST /tasks/{id}/sentiment`，后台跑、立刻返回）。两条路共用 `orchestrator.run_sentiment()`，区别只有：流水线那条 `await` 到底并让异常冒出去（步骤要靠它判成败），按钮那条 `create_task` 后立刻返回、失败只发 `sentiment_complete` 事件。`run_sentiment_async()` 必须**同步**把 task_id 放进 `_sentiment_running`——等协程调度起来再放的话，POST 刚返回那一瞬间前端来问 `GET /sentiment` 会得到「没在跑」。

两条路的增量粒度都是 `_processed.sentiment_at` 为空的帖子。跨来源分组（`by_source` / `cross_source`）走**纯 Python**，不需要 LLM 感知来源；prompt 里只给每条帖子标 `[来源: xxx]` 并说明「按平台内部的相对水平判断」，**绝不描述某个平台的情感先验**——那会污染的正是我们想比较的那个维度。评论会带上父贴前 200 字作 `[回复上文: ...]`，否则「+1」「same here」全被判成 neutral 噪音。

**并发控制**：`max_concurrent_tasks` 超限时新任务进 `_task_queue` 排队，前一个任务在 `_run_with_queue` 结束后调 `_process_queue()` 自动出队执行，不会直接失败。

### 纯图帖（一个字都没有、只有一张图）

**「能不能分析」的判据是 `sentiment_service.is_analyzable()`：有正文 or 有配图。**
入库口、流水线、页面按钮三处全部 import 它，不许各写各的 —— 曾经三处都是
`(p.get("content") or "").strip()`，于是纯图帖在四个环节被一致地当成空气。

代价是真实数据丢过：`media/src_b32bc603/6680d5f13a6b2b4c_0.jpg`（HYXi 安装检查报告，
总分 88、发电异常 8/20 标橙）还躺在盘上，`posts` 表里一行都没有 —— 采集脚本先下图、
`drop_empty_posts()` 再丢帖子。它连「未分析」都不显示，是**直接消失**，比留一行空白难发现得多。

**`analyze()` 里筛选与图片理解的先后顺序是这条链路的命门**：

1. 先按 `is_analyzable()` 圈 `candidates`（**必须排在图片理解之前** —— 用正文筛的话纯图帖
   进不了 `need_desc`，它的图永远不会被理解，于是永远分析不了，形成死循环）
2. `_understand_images()` 按 candidates 所在的**讨论串**收集带图帖
3. **理解完之后**才定 `non_empty`：`正文非空 or image_desc 非空`

第 3 步不能省。多模态没配 / 调用失败 / 图丢了的时候，纯图帖是**真的**没有可判断的内容，
送一个空块给 LLM 只会换回一条编出来的结论 —— 那比诚实地留「未分析」糟得多。

正文位置在 prompt 里**不能留空**，用 `NO_TEXT_PLACEHOLDER` 顶上（`_post_block` 与
`_member_line` 共用同一句）：一片空白看起来像一条被截断的帖子，模型会照着「没说什么」判
neutral，而信息其实全在紧随其后的 `[图片: ...]` 里。系统提示词里因此专门写了一条
「不要因为没有正文就一律判 neutral」。

主贴↔回复的关系不用另做 —— `thread_by_key` 已经让纯图主贴的描述随整串上下文进到它每一条
回复的 prompt 里，反过来纯图主贴也拿得到回复。回归测试见 `TestImageOnlyPostsAreAnalyzedEndToEnd`。

### 导出报告里的配图

`EXPORT_COLUMNS` 里「图片描述」「配图」两列由 Excel 和 CSV 共用：CSV 的「配图」是相对路径
文本（照着能在 media 目录里找到原图），Excel 那一格贴 150px 缩略图。大图另开一张
**「配图」工作表**（`IMAGE_SHEET`），一条帖子一段：帖子头 + 完整描述 + 700px 大图 + 返回链接。

**超链接挂不到图片上，别再试**：openpyxl 3.1.5 的 `Image` 只有 `anchor` / `path`，
`SpreadsheetDrawing._picture_frame()` 里的 `cNvPr` 是现场写死的，没有注入 `hlinkClick` 的口子；
Excel 本身也没有「点图放大」的原生行为。所以跳转入口放在同一行的**「图片描述」格**上 ——
它既是纯图帖最显眼的文字，又不会被图片盖住点不着。内部跳转必须走
`Hyperlink(location=...)`，给 `target` 会被当成外部关系，Excel 打开时报「需要修复」。

缩略图**重新编码**（PIL `thumbnail()` → JPEG），大图**用原始字节只钳显示尺寸**：前者若直接
拿原图按 150px 显示，xlsx 里会存两份原始字节；后者若重新编码，等于把要看清的细节又糊一遍。

**大图不能把整张高度压进一行**：Excel 单行上限 409.5pt（≈546px），而真实 Facebook 截图是
367×795 这个量级，钳到 700px 仍有 525pt —— 设过去会被 Excel 截回来、图盖到下面几行上
（实测导出里出现过 25 行 531pt）。所以配图表**不设行高**，按默认行高（15pt≈20px）留够
`ceil(高/20)` 行让图自己铺开。回归测试见
`TestExportImagesEndToEnd::test_tall_image_gets_enough_rows_instead_of_one_oversized_one`。

**导出的图片总量没有上限**，整份工作簿在内存里拼好再作为一个 Response 返回。当前
138 条帖子 / 31 张图 ≈ 0.93MB，够用；来源长期跑下去图数是线性增长的，哪天导出变慢或吃内存，
先看这里而不是先怀疑 LLM。

**纯图帖拿不到描述时不写 `sentiment_at`**，所以下一轮增量会连图片理解一起重试。这与文本帖
分析失败后重试是同一个规矩，没有单独的失败计数 —— 代价是一张**永远**描述不出来的图每轮多烧
两次多模态调用（真机空描述率约 12%，重试一次后残留很低）。真出现这种图，先查图本身。

`openpyxl` 插图硬依赖 **Pillow**（缺了直接 `ImportError`），已进 `requirements.txt`；
`excel_service` 仍兜了一层 try/except，既有 venv 没更新时报告降级成无图而不是下载 500。
路径解析复用 `vision_service.media_path()`（含 realpath 包含性校验）—— `images` 来自采集脚本，
而 media 目录之外就是数据库和明文密钥。

## 全量重跑（`force_full`）

`POST /tasks/{id}/retry?full=true` 复用原描述建一个新任务，并把 `tasks.force_full`
置位。执行时 `execute_task()` 一次性放开**三处**增量判据：

| 步骤 | 平时的增量判据 | force_full 时 |
|---|---|---|
| collect | `params.incremental`（默认 True） | 置 False |
| translate | `_processed.translated` | 全部当待翻译 |
| sentiment | `_processed.sentiment_at` | 全部当待分析 |
| 图片理解 | `posts.image_desc` 非空即跳过 | 内存里清空，全部重新理解 |

- **`force_full` 必须是 tasks 表的真列**：那张表是固定列表，任务字典里的自定义键
  根本不会被持久化，服务重启或从库里读回时就丢了。补列走 `PRAGMA table_info` +
  `ALTER TABLE ADD COLUMN`（同 `_ensure_posts_image_desc`）
- **关掉增量时必须一并清空 `known_fingerprints` 和 `max_page_number`**
  （落在 `CollectorRunner`）。`facebook_group.js` 的 `seen` 集合是**无条件**用
  `known_fingerprints` 建的（只有水位线提前退出那句看 `incremental`），照旧下发的话
  每条帖子都被判成「见过」→ 不进 `fresh` → **配图也不会重下**，于是 `incremental=False`
  对它完全无效。回归测试见 `TestFullRerunDropsIncrementalAnchorsEndToEnd`
- **`image_desc` 也必须一并放开**（清在 orchestrator 的 sentiment 分支）。它平时与
  `translation` / `sentiment_at` 同规矩「绝不重算」，而 `sentiment_service` 跳过的判据
  正是「已经有 image_desc」—— 不清掉的话，用户为「按当前口径重算一遍」付了钱，图片
  描述却仍是旧模型的产物，而**换了多模态模型正是有人点这个按钮的主要原因**，界面弹窗
  也白纸黑字承诺了「带图的还会再调一次多模态模型」。只清内存那一份：`save_image_descs()`
  不写空串，所以多模态没配 / 这一张没描述出来时，库里旧描述仍在，下一轮照常读回。
  回归测试见 `test_full_rerun_also_re_describes_the_images`（含「普通重跑不得重复付费」的对照组）
- **新建任务和定时任务一律走增量**，只有那个按钮给 True —— 否则每一轮定时都在重复
  付翻译和舆情的钱
- 界面上必须二次确认（`TaskManagementView` 的弹窗），并在任务行挂「全量」徽标：
  两条描述一样的任务摆在一起，没有它就分不出哪条重译过

**连带的一条存储规矩**：`upsert_posts()` 的 UPDATE 分支**只在本轮真抓到图时才覆盖
`images_json`**。写死成「采集到什么就是什么」的话，全量重跑撞上一次网络抖动就会把它
写成 `[]`，而图片文件还好端端躺在 media 目录里 —— 页面上整批消失且毫无提示。
与旁边 `translation` / `image_desc` 「绝不被采集冲回空值」是同一套道理。

## 老帖新回复（避免被时间倒序埋掉）

出口一律「主贴按发表时间从新到旧、评论跟着自己的主贴走」（见上一节），副作用是
**今天发在两个月前主贴上的回复，会被排到两个月前的位置去**。真实数据实测：163 行的
报告里，4 条这样的回复落在 #68 / #133 / #134 / #147，三条都在报告底部。

判据只有一份，在 `post_tree.mark_fresh_replies()`，理由同 `order_by_thread()` ——
导出和结果页共用一条规则，各写各的迟早分家。

**页面上的展示只在「任务结果页」**（`ResultsView.vue`）：一个「🔥 只看新回复」开关
加 3/7/14 窗口选择。舆情页那套面板与开关已整体拆除，只留了一个窗口选择器紧贴导出按钮 ——
它决定**报告**里「近期新回复」工作表和「更新提醒」列用哪个窗口，而导出入口全站只有那一个。

- **筛选必须在服务端做**（`/posts?only_fresh=true`）。一页只有 50 个主贴，而新回复恰恰
  因为主贴按时间倒序被压在后面几页 —— 那正是这个功能要解决的问题，在前端筛只能筛出
  当前页里的，等于什么都没做。舆情页那版能在前端筛，是因为它把帖子全量拉下来了，
  结果页是服务端分页，搬不过来
- `only_fresh` 与 `search` 是 **AND**；`total` 在过滤之后算，否则分页器会显示出翻不到的页码
- **筛空时搜索栏必须照常渲染**（`ResultsView` 的 `hasFilter`）—— 它自己就是关掉
  筛选的唯一入口。整张卡片（搜索栏 + 列表 + 分页）本来一起挂在 `posts.length` 上，
  而空状态那块又要求 `!isCompleted` —— 于是已完成任务一旦「只看新回复 + 近 3 天」
  没命中，**整个查询区域连同开关一起消失**，只能刷新页面（用户实测报过）。
  搜索搜不到结果是同一个坑，只是没人点到。分页条则相反，空时**不能**渲染，
  否则显示成「第 1/0 页」

- **基准是数据集里最新的帖子时间**（`baseline_time()`），不是 `datetime.now()`。
  按 now 算的话，隔一阵子没采集、或翻看几个月前的历史报告时**一条都不会亮**，
  而那份报告当初想标出来的东西并没有变。报告要自洽、可复现
- **窗口 `FRESH_DAYS_CHOICES = (3, 7, 14)` 是封闭集合**，`_validate_fresh_days()` 拦非法值
  并返回 400，**不静默回落到默认值** —— 那样用户以为自己换了窗口，实际看到的还是 7 天
- **三条不标的边界**：主贴也在窗口内（整串都新，本来就排在最前面）；回复或主贴的时间
  读不出来（早期采集故意留空，「实际很新」是推测，宁可漏标）；父贴不在本批数据里
  （`build_tree()` 已把它按主贴处理，它就不是回复）
- Excel 新增「更新提醒」列 + 命中行整行暖色（`FRESH_FILL` **优先于** `REPLY_FILL`，
  两个底色叠在一起就都看不出来了），并新增工作表「近期新回复」，顺序是
  **概览 → 近期新回复 → 帖子明细 → 配图**（工作簿默认停在第一张，第二张最容易被看到）
- **聚焦表把主贴完整带上**：一条「你能通过 HA 控制它吗」脱离主贴根本读不懂在说什么，
  用户不该为了看懂一条新回复再去别处翻主贴。但**不带该主贴的其他旧回复** —— 热帖十几条
  全搬过来就长得没法一口气读完，末尾给一条跳转即可
- **明细表的排序一个字没动**。聚焦是新增入口而不是重排 —— 两个出口共用一条排序规则
  这条既定约束不能破

## 便携包交付（给不装 Python / Node 的使用者）

`build\build.ps1` 出一个免安装 ZIP：前端构建 → 采集脚本打包压缩 → Nuitka 编译后端 →
组装 → **泄漏自检** → 压缩。解压双击「启动 HYXi.bat」即用。

- **单端口自服务前端**：`main.py::mount_frontend()` 挂 `web/` 静态资源 + SPA 回退，
  便携包里没有 nginx。**必须在所有路由注册之后调用**（catch-all 谁都接得住），
  且回退里要**排除 `api/` 开头**的路径 —— 否则打错的接口地址会拿到一张 HTML 页面，
  调用方报的是「JSON 解析失败」，跟真实原因毫无关系。`web/` 不存在就整段跳过，
  开发态（Vite 5173 代理 `/api`）不受影响
- **冻结态路径**：`app/paths.py` 是唯一一份算法。冻结时 `project_root` 取
  `sys.executable` 的上两级（`app\hyxi.exe` → 包根）。**它单独成模块是因为
  `run_server.py` 必须先算出包根、生成 `.env`，才能 import `app.config`** —— `settings`
  是 import 期实例化并当场读 `.env` 的，顺序反了就读不到刚生成的密钥，用户会卡在
  「数据源」页存不了凭据

### 数据目录在包的**同级**，不在包里

包目录带版本号，升级就是解压出一个全新的空目录 —— 数据装在包里的话，用户
每升一次级，LLM 配置、数据源、跑过的任务和舆情结论全部要重来（用户实测报过）。

```
C:\HYXi├── HYXi-1.8.1-win64\     ← 旧版本，升级后可直接删
├── HYXi-1.8.2-win64\     ← 新版本，启动即接上数据
└── HYXi-数据\           ← hyxi.db / .env / media / sessions / logs
```

- **不放 `%LOCALAPPDATA%`**：那样一来「免安装、整个目录拷走就能换机器、删目录即卸载」
  三条同时不成立。同级目录把三条都保住了 —— 拷走父目录即搬家，删父目录即卸载
- **`.env` 必须跟着数据走**（`paths.env_file()`）。里面的 `TWEAKERS_SECRET_KEY` 是数据源
  密码的加密密钥，它和 `hyxi.db` 一分家，库里的密文就再也解不开，界面只会说
  「与保存时的密钥不一致，请重新录入凭据」
- **包内已有 `data/` 时一律沿用它**（`data_dir()` 的 legacy 分支，`env_file()` 同理）。
  老版本的数据就在那儿，升上来的用户重启一次不能凭空变成空库。**新解压的包里
  没有这个目录**（ZIP 不含 `data/`，已核实），所以这条只对既有安装生效
- **升上来时从旁边的旧包接数据**（`run_server.adopt_previous_install()`）：同级目录里找
  **装着用户数据**的 `data/`，多个就取 `hyxi.db` 最近改过的那个（按目录名比版本号会在改过
  名的目录上判错）。**复制而不是移动** —— 旧包留在原地照样能跑，用户想回退就回退
- **接管必须排在 `ensure_env_file()` 之前**。反过来会先生成一把**新**密钥，旧 `.env`
  因为「已存在」不再被复制，于是数据搬过来了、密码却全部解不开
- **接管的判据是「数据目录里有没有用户的东西」，不是「目录在不在」**
  （`run_server._has_user_data()`）。用户拿到新版本往往先双击试一下，那一下就把外部
  数据目录连同空库建出来了；他发现要重配、回去接着用旧版本，再来试新版本时
  「目录已存在」就把接管**永久**短路掉 —— 配置一条都带不过来，而且看不到任何原因
  （用户实测报过：在 1.8.1 配了 `deepseek-chat123`，启 1.9.0 读到的还是默认值）。
  「先试试新的、发现不行退回旧的」正是最自然的升级姿势
- **判据表里 `sources` 的门槛是 1 而不是 0**：首启时 `seed_default_sources()` 会自动补
  一条 Tweakers 源，门槛给 0 的话每个刚建出来的空目录都算「有数据」，接管永远不发生；
  而完全不看 `sources` 又会把「只加了数据源、还没配 LLM」的目录静默冲掉
- **读不动那个库时一律当作有数据**：宁可不接管（用户还能自己复制），也不能拿旧数据
  覆盖一个可能装着东西的目录。反过来 `_previous_install()` 也要求旧包**真有**数据 ——
  接管一个空库之后 target 仍是空的，下次启动照做一遍，还每次都打一条「已接管」的假日志
- **外部数据目录被顶替时先改名让路、成功后才删**：`os.rename` 是原子的，中途失败还挪得
  回去（那份 `.env` 就是当前生效的密钥）。两步都失败时要说清楚东西暂时叫什么名字 ——
  静默让用户以为数据目录凭空没了，正是这个 bug 最难查的地方
- 回归测试见 `TestPortableDataSurvivesUpgradeEndToEnd`；`verify_package.ps1` 另有三段真跑：
  常规升级、从旧包接管、**先试跑再接管**（把旧包的 `data` 先藏起来造出真实时序）
- **`node_modules` 必须放在包根**：Node 的 `require` 从请求文件所在目录逐级向上找，
  `collectors/` 的上一级正好是包根。挪进 `app\` 就解析不到 playwright。保持这个布局，
  `collector_runner.py` 那句依赖自检一行都不用改
- **node 可执行文件走 `config.resolve_node_executable()`**：显式配置 → 包内
  `node\node.exe` → PATH 上的 `node`。目标机器不会装 Node
- **启动器 `启动 HYXi.bat` 必须是纯 ASCII，且不能带 BOM** —— 与 `start.ps1`
  「必须存成 UTF-8 带 BOM」正好相反，别混。`cmd.exe` 按**读取时生效的代码页**逐行解析
  批处理文件，`chcp 65001` 之后再遇到中文，多字节序列会被按 GBK 拆断、整行碎成不存在的
  命令。用户实测双击后报 `'锛夎蛋涓嶅埌杩欓噷銆傝蛋鍒颁簡灏辨槸鍚姩澶辫触锛?REM' 不是内部或外部命令`。
  中文一律由 `hyxi.exe` 输出（它写 UTF-8，而控制台此时已被启动器切到 65001），
  连「服务已停止」这句收尾也在 `run_server.py` 里。`build.ps1` 的自检会拦住任何非 ASCII 字节

### 采集脚本只能压缩，不能编成字节码（实测结论，别再试）

最初用 bytenode 编 V8 字节码，真跑 fixture 站点当场炸：
`page.evaluate: Passed function is not well-serializable!`

根因是原理冲突，不是 bytenode 的 bug：**Playwright 的 `page.evaluate(fn)` 靠
`fn.toString()` 把函数当文本送进浏览器执行**，而 bytenode 把函数源文本替换成等长的
零宽字符（实测 `(a) => a.querySelectorAll(...)` 的 `toString()` 长度仍是 52，
内容全是 U+200B）。采集器的 DOM 提取全建立在 `page.evaluate` 上。

**反过来说：只要 `page.evaluate` 要能用，那段 DOM 代码就必须以可读源文本存在于进程里。**
这对 pkg / SEA / 任何字节码方案一视同仁。所以采集脚本的上限就是 esbuild
`bundle + minify`（标识符改名、注释格式抹掉、死代码消除），与任何线上 web 应用同级；
后端 Python 那边是 Nuitka 真编译，不受这条限制。

顺带一个坑：压缩**必须限行宽**（`lineLimit: 120`）。压成一整行时 Node 打栈回溯会把
那一整行原样吐进 stderr，而 `CollectorRunner` 只取前 500 字符拼进 `error_message` ——
真正的报错会被挤掉。

验证方式是与源码版跑同一个 fixture 站点逐条比对（指纹 / 作者 / 时间 / 正文 /
父子关系 / 层级全同），压缩不该改变任何行为。

## 测试

**368 个测试，必须全部 PASSED**（本机实测 `368 passed`）。修改任何核心逻辑后必须在仓库根目录运行：

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
- **限流即停**：`gotoPage()` 遇 429/403/503 读 `Retry-After` 退让一次（无则 60s，上限 300s），仍失败即抛错终止。`handleConsent()` 里跳转 DPG 回调那一跳**也必须走 `gotoPage`** —— 被拒时正是那个请求返回 403，而它落地后的 URL 仍是正常的 `/forum/...`，只看 URL 发现不了
- **明确不做**：代理池 / IP 轮换、验证码破解、提高并发或速度、无视 robots.txt、stealth 插件
- **升级路径**：若仍被拦，先改有头模式（`headless=False`）观察实际拦截页面，而不是继续叠伪装

> **当前状态（2026-07-31 实测）：本机出口 IP `43.110.141.12` 已被 Tweakers 防火墙整体封禁**，任何请求都会跳 DPG 隐私 gate 后拿到 403，页面原文是「De toegang tot Tweakers vanaf dit IP is geweigerd」，申诉邮箱 `gathering@tweakers.net`（需在邮件里附上该 IP）。这是 IP 级封禁，**不是**指纹或节奏问题 —— 继续调 UA / 延时没有任何意义，换 IP 也在「明确不做」之列。抓取链路本身已验证可用（403 会被正确识别并让任务失败），恢复抓取需要先解封或换一台出网机器。



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
- **爬虫必须通过 `node` 子进程调用**，不能 import
- **日志有两套命名空间**：`logging_config.get_logger()` 用全局 `_logger` 缓存，**第一个调用者的 name 定死了整个 logger**（实际是 orchestrator 的 `app.services.orchestrator`）；其余 service 用 `logging.getLogger("hyxi.xxx")`，拿不到那些 handler。加日志时注意实际输出去向
- **`TaskInputView.vue` 是死代码**（未注册路由，全项目零引用，功能已并入 `TaskManagementView`）
- **`Bash` 工具不保持 CWD**，每条命令都要自己 `cd` 到正确目录
