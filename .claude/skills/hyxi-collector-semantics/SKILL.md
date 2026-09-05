---
name: hyxi-collector-semantics
description: hyxi 采集语义：正文图抓取（tweakers + facebook_group）、全量重跑 force_full、老帖新回复如何避免被时间倒序埋掉。改采集脚本的增量/去重/翻页逻辑之前读这份。
---

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

