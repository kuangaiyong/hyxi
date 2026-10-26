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

开发态下这份产物没人用（前端跑在 Vite 5173 上，由它把 `/api` 代理到 8000）。
交付给使用者时它会被拷进便携包的 `web/`，**由后端自己发布**，见下一节 —— 不需要 nginx。

---

## 近期新回复

报告和列表都按「主贴发表时间从新到旧」排序，评论跟着自己的主贴走。这带来一个盲区：
**一条今天发的回复，如果挂在两个月前的主贴上，就会被排到两个月前的位置**，翻不到。

系统把**近 N 天内发的回复**单独标出来。只看回复时间，主贴新不新不管，所以窗口越宽结果越多，
近 3 天的一定都在近 7 天里：

- **Excel**：明细表多一列「更新提醒」，命中行整行橙色底；另有一张 **「近期新回复」** 工作表
  排在概览之后，按讨论串成块 —— 主贴完整带上，下面跟着本次的新回复，两张表可互相跳转
- **舆情页**：顶部「🔥 近期新回复」面板，一个讨论串一张卡片；筛选区有「只看新回复」开关
- **结果页**：主贴卡片上有「🔥 N 条新回复」徽标，命中的回复行加橙色左边框

时间窗口可在页面上切换 **3 / 7 / 14 天**，导出时跟着一起传。

**基准是数据集里最新的帖子时间，不是当前时间** —— 这样同一份报告隔多久打开，标出来的
都是同一批；采集隔了一阵子没跑也不会一条都不亮。概览表里会写明基准是哪一天。

**Tweakers 论坛**：一个数据源就是一个主题，楼层平铺入库、没有父子关系。这里把**时间最早的那一楼**
当主题，之后的楼层都算它的回复，窗口内发的楼层就是新回复（主题自己不算）。
结果页上它们仍是一张张「主贴」卡片，打开「只看新回复」就只剩窗口内的新楼层；导出的「近期新回复」表里挂在主题下。

---

## 结果页补译

只采集、没让它翻译的任务（描述里没写「翻译成中文」），结果页上会留下一片「（尚未翻译）」。
统计栏下面的提示条写出还缺多少条，点「翻译这 N 条」就在后台补齐：

- **只翻缺的**：判据是「有正文，且译文为空或是失败标记」（`translator_service.needs_translation()`），
  已有译文的一条不动。提示条上的 N、页面上的标签、`/stats` 用的是同一个判据
- **每 50 条落一次库**：几百条要十几分钟，中途关掉程序不会白花钱，重开再点接着翻
- **同一来源不会被翻两遍**：补译与任务里的翻译步骤互斥（来源级占用）。任务撞上补译会等它结束
  再算待翻译；反过来任务正在翻时点按钮只回「正在翻译」，不另起一轮付费翻译

进度走独立的 SSE 频道（`/api/v1/tasks/{id}/translate/events`），不会串到任务进度流或舆情流上。
补译期间关掉页面也不影响，作业在后台继续，回到页面还能接上进度。

---

## 邮件通知

左侧「邮件通知」页把所选数据源**近 N 天的新回复和新主贴**（原文 + 译文）发到邮箱：

- **新回复的口径**和上面「近期新回复」相同（只看回复时间），但**基准是发送那一刻**，不是数据集里最新的帖子 ——
  定时摘要要的是「这段时间里有什么新的」。窗口可选：不设置（近 1 天，即发送前整 24 小时，不按自然日）/ 近 3 / 7 / 14 天
- **新主贴**（这段时间里新发的顶层主贴，没人回复的也算）标「🆕 新主贴」、全文照登；Tweakers 一源一串，不出新主贴
- **近 24 小时发表的帖子垫浅绿底色**（主贴、回复都算，不管窗口选几天，各按自己的发表时间）；有垫色的帖子时，抬头放一枚同色的「近 24 小时发表」图例
- **发送前先补采**：所选（启用的）数据源今天还没成功采集并翻译过的，先自动发起「采集 + 翻译」任务（任务列表里看得到），
  跑完再挑新回复。手动「立即发送」会先弹框问「先采集再发送 / 直接用已有数据发送 / 取消」，每天定时不问、一律先采；
  自动采集没成功就不发，原因写在「发送记录」里。停用的数据源不采，只用已有数据
- **不会漏**：每封除了近 N 天的新内容，还把没随邮件发出去过、发表在这个来源第一次进邮件那一轮的窗口起点之后的补上，
  标「补发：之前没发出」—— 采集晚了、某一轮发送失败、服务停了一天都算（靠 `posts.notified_at` 记「发没发过」，只在发送成功后写）
- 时间戳不带时区：Facebook 是本机时间，Tweakers 是荷兰时间（`Collector.timestamp_tz`），算「近 N 天」时按各来源的时钟换算
- 按数据源分节（「来自：Facebook 公开小组「…」」），每条新回复带上它的主贴作上下文；不含舆情结论、不含原帖链接。
  新内容（新回复 + 新主贴）超过 20 条时正文只列最新 20 条，完整内容放进 HTML 附件
- **缺译文先补译**（用「LLM 配置」里的模型，与结果页补译、任务里的翻译按来源互斥），失败整轮重试最多 3 次，
  新回复、新主贴仍有翻不成的就不发，原因写在页面的「发送记录」里；作上下文的老主贴和补发的帖子翻不成不影响发送，只放原文
- 窗口比发送间隔长时，同一条会连着出现在几封里：照列，前几封发过的标「已发过」；窗口里全是发过的那天不发
- 手动「立即发送」，或开「每天定时发送」（没有新内容的那天不发）

SMTP 密码同数据源凭据：用 `TWEAKERS_SECRET_KEY` 加密落库，任何接口都读不回去，没配密钥时拒绝保存。
密码留空表示沿用已保存的，但**SMTP 服务器、实际连的端口、「使用 SSL」或用户名改了，或新信任了一张证书，必须重填** ——
否则已保存的密码会被发给另一台服务器（或同一台机器上的另一个端口）。
证书严格校验，唯一的例外是你按 SHA-256 指纹信任的证书（见下）；外部服务器既不走 SSL、又不支持 STARTTLS 时不发密码
（本机 127.0.0.1 上的中继除外）。

**怎么填**：SMTP 服务器填邮箱服务商提供的地址；一般勾选「使用 SSL」、SSL 端口 `465`；用户名和发件人都填完整的邮箱地址；
不少邮箱的密码要填**授权码**（在邮箱网页版的设置里开启 SMTP 服务后生成），不是登录密码。

**公司内网的邮件服务器用自签证书时**：「发送测试邮件」会报「证书校验没通过（self-signed certificate）」，并列出证书的
SHA-256 指纹、主体和有效期。和邮件管理员核对指纹一致后，点「信任这张证书」（密码框空着时要先重填密码），收到测试邮件再「保存配置」。
之后只认信任过的证书：换了证书、或有人冒充那台服务器，照样拦下（密码不会发出去）；换上正规证书后照常发，不受之前信任的影响。
服务器后面有好几台、各用一张证书时，撞上哪张就信任哪张，可以信任多张（最多 10 张）。信任绑在服务器地址上：
换了地址（包括同一台改用 IP 访问）要给新地址再信任一次。核对时比的是 SHA-256 指纹（页面上的写法和 `openssl x509 -fingerprint -sha256` 的输出一样，
大小写和冒号不算数）；Windows 证书窗口里默认显示的「指纹」是 SHA-1，不是这个。

---

## 交付给使用者

给不装 Python / Node 的人用时，打一个**免安装便携包**：

```powershell
.\build\build.ps1
```

产出 `build\out\HYXi-<版本>-win64.zip`（解压约 250MB）。对方解压后双击
「启动 HYXi.bat」即可，不需要管理员权限、不写注册表、删目录即卸载。

包里的东西：

| 内容 | 说明 |
|---|---|
| `app\hyxi.exe` | Nuitka 编译成原生机器码的后端，自带 uvicorn，单端口同时供 API 和页面 |
| `web\` | 前端构建产物 |
| `collectors\` | 采集脚本，esbuild 打包压缩过 |
| `node\node.exe` | 便携 Node 运行时，目标机器不必装 Node |
| `node_modules\` | playwright（第三方依赖，原样带） |
| `data\` `.env` | 首次启动时自动创建，加密密钥当场生成 |

构建脚本最后一步是**泄漏自检**：包里出现任何 `.py`、未压缩的采集脚本、
`.env` / `hyxi.db` / 会话文件 / 构建期依赖，一律中止出包。

目标机器需要预装 **Google Chrome**（采集脚本用它驱动）。缺了不影响启动和其余功能，
启动自检会给出提示。

**`node_modules` 必须留在包根**：Node 的 `require` 从请求文件所在目录逐级向上找，
`collectors/` 的上一级正好是包根。挪进 `app\` 就解析不到 playwright。

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
├── build/                       便携包构建（build.ps1 + 采集脚本打包 + 启动器与说明）
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
