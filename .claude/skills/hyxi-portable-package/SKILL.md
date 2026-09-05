---
name: hyxi-portable-package
description: hyxi 便携包交付（给不装 Python / Node 的使用者）：数据目录为何在包的同级而不在包里、采集脚本只能压缩不能编字节码的实测结论。打便携包、改打包脚本或改数据目录布局之前读这份。
---

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

