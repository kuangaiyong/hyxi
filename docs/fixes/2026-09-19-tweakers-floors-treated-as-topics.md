# Tweakers 的 140 条楼层被当成了 140 个主贴

## 分诊

- **修复**：一个来源（= 一个 thread）在结果页显示成 140 张「主贴」卡，每张下面一条回复都没有 ——
  现有行为与预期不符
- **新功能**：引用块要能看见（引用了谁、基于哪段内容）、引用里的图要采到且归属正确 ——
  外部可见行为变了

两者一起交付：呈现的前提是采得到。

## 现象

用户报的是「Tweakers 不是主贴 + 回复贴那种结构，是类似朋友圈那样：一个人发主题，
大家基于主题评论，有人再引用别人的话评论」。

平台上的实际样子（改造前，真实产物）：

- `backend/data/exports/tweakers_report_926d4d2f.xlsx`：141 行（表头 + 140 条），
  **「层级」列全是 0**，「序号」1…140 平铺 —— 140 条楼层没有任何高低关系
- 结果页：140 张卡片，每张都标「主贴」，每张的「评论与回复」都是空的
- 引用块里的内容一个字都没有；被引用的楼层如果没采到，那段内容就永久丢了
- 引用块里的图一张都没落盘（`rej('引用块')`）

连带失效的三处（同一个根因的表现，都不是独立 bug）：

| 功能 | 判据 | 后果 |
|---|---|---|
| 🔥 只看新回复 | `mark_fresh_replies()` 第一句 `if not post.get("parent_fingerprint"): continue` | Tweakers 侧恒为空集，这个开关从来没生效过 |
| 舆情按串给上下文 | `thread_of()` 给每条楼层返回的「讨论串」就是它自己 | 每条楼层都在**零上下文**下判正向/负向 |
| Excel 的层级列与 `　└─` 前缀 | `reply_level` 全 0 | 永远为空 |

## 根因

**`collectors/tweakers.js` 一个 `parent_fingerprint` 都不写。**

三个采集器里 `facebook_group.js`（`flatten()` / `flattenComments()`）与 `group_feed.js`
都写这两个字段，只有 Tweakers 没写。`storage.upsert_posts` 于是取默认值
（`reply_level=0`、`parent_fingerprint=NULL`），`post_tree.build_tree()` 把它读成
140 条「父贴不在本批数据里的评论」，按既定语义全部当主贴处理。

出处在 `docs/features/facebook-comment-threads.md` 当年的非目标一节：

> **Tweakers 不动**：论坛楼层没有「回复的回复」这一层结构

「没有回复的回复」是对的。但由此推出「所以不需要父子关系」是错的 —— 楼层之间**有**一条
关系（都属于同一个主题），只是没有第二层。这一条推理差把 140 个楼层变成了 140 个主题。

在此之上还有两个次生根因：

- **引用块被删掉后没有任何字段接住**。`clone.querySelectorAll(QUOTE_SEL).forEach(el => el.remove())`
  本身是对的（正文进指纹，一个字符都不能动），错的是删完就没有下文 —— 引用者的正文
  只剩「我对那条内容的看法」，孤零零没有上下文
- **引用块里的图既不采集也不归属**。`if (im.closest(QUOTE_SEL)) return rej('引用块')`：
  不排除 → 别人的图算到引用者头上；排除 → 那张图一张都不落盘。两条路都不对，
  得开第三条

## 真站实测（2026-09-19，本机真 Chrome 直连）

串 `https://gathering.tweakers.net/forum/list_messages/2336074/`，3 页（100 / 100 / 3 = 203 楼）。
只读探查脚本在 `/tmp/tw_probe*.js`（未进仓库），跑的是真 Chrome、真 HTTP、真 DOM。

**先说一件推翻旧结论的事**：本机出口 IP 并没有被整体封禁 ——
`curl` 拿页面会被 DPG 的 WAF 403（`Reference: … ClientIP: 101.71.39.248`），
但**真 Chrome 完全打得开**。以前「本机访问不到真站」这个前提是错的，
CLAUDE.md 与几个 skill 里那句话已经在这次一并改掉。

| 事实 | 实测值 |
|---|---|
| `.message[data-message-id]` | /0 100/100、/1 100/100 |
| 主题（发起帖） | **`/0` 的第一条 `.message`**：id `85322114`、Dorpjes、`22-05-2026 17:06` |
| `.message.topicstarter` | **15 条**（/0）、8 条（/1）—— 标的是「发起人的**所有**楼层」，**不是**主题 |
| 正文容器 | `.messagecontent`（`.ugcContent`、`.forumUgcContent` 同） |
| `.quote` / `.bb_quote` / `.cite` / `.quotetext` | **全部为 0** |
| 引用块位置 | `blockquote` 是 `.messagecontent` 的**直接子元素**（42/42、53/53、2/2） |
| 引用嵌套 | **0 处** |
| 引用块数量 | 97 条，其中带 `a.messagelink` 的 **94** 条 |
| 引用块里的真 `<img>` | /0 **0 张**；被引用的照片只留 `<a href="…原图…">[Afbeelding]</a>` |
| 正文真图 | `tweakers.net/i/<sig>/x800/…`，渲染 369×800 ~ 800×600；表情 16×16 / 34×16 / 30×17 |
| 单楼固定链接 | `{base}/forum/list_message/<id>#<id>` |
| 分页 | 末页是 `class="lastpage"`，**不是** href 里带 last |

引用块的真实标记：

```html
<blockquote>
  <div class="message-quote-div[ large-quote]">
    <b><a href="…/forum/list_message/85325992#85325992"
          rel="external noopener" class="messagelink" target="_blank"
       >Storms schreef op zaterdag 23 mei 2026 @ 12:01</a>:</b>
    <br> 被引用的正文（长引用被服务端截断，中间插一个字面量 [...]）
    <a href="…/i/<sig>/fit-in/4920x3264/…/image/xxx.jpg">[Afbeelding]</a>
  </div>
  <span class="toggle-quote">toon volledige bericht</span>   <!-- 仅 large-quote -->
</blockquote>
```

三条决定设计走向的实测：

1. **被引用楼层的 id 就在 `a.messagelink` 的 href 里** → 引用关系有精确 join key，
   不用解析荷兰语日期
2. **`large-quote` 的截断是服务端的，点不开**：点 `.toggle-quote` 只给 `.message-quote-div`
   加了个 `open` class，**文本长度 1211 → 1211 一个字符都没变**，`[...]` 还在，0 个新 XHR。
   → 要拿被引用内容的全文，只有**读那个楼层自己**这一条路
3. **被引用的图不是 `<img>`** → 靠「`<img>` + 渲染尺寸」永远收不到它

### fixture 一直在替错误假设作证

`backend/tests/fixtures/tweakers_site/page_0.html` 里写的引用块是
`<blockquote class="quote"><span class="cite">…` —— 这套标记**真站上一个都不存在**。
任何照 fixture 写的引用选择器都会测试全绿、真站一根毛都收不到。
fixture 已按上面的实测结构重写（消息集、`data-message-id`、时间戳、正文一字未动，
`golden_tweakers.json` 的 9 条指纹因此一个都没变）。

## 修法

| 层 | 改动 |
|---|---|
| `collectors/tweakers.js` | 主题 = 第 1 页第一条；其余楼层 `parent_fingerprint` 指向它、`reply_level=1`（**平铺**，Tweakers 没有第二层）；新增 `readQuote()` 把**每一个**引用块读成独立的一份；引用图两路来源（`[Afbeelding]` 锚点 + 过了尺寸线的 `<img>`）；`loadLazyImages()` 把整页滚一遍让浏览器加载懒加载的图；`prefetchQuotedImages()` 为 `[Afbeelding]` 锚点建游离 `Image` |
| `collectors/lib/media.js` | 引用图落到 `<指纹>_q<引用序号>_<图序号>.<ext>`，写进 `quote.images`，**与引用者自己的图分开**；汇总行会计如实（不再把引用图记成「排除」） |
| `Collector.thread_kind` | 新声明：`"thread"`（一源一串）/ `"feed"`（一源多主贴）。站点知识留在声明里，Python 与前端都不按 collector_id 分支 |
| `CollectorRunner` | 一源一串且增量时，从库里查主题指纹塞进 source；`TweakersCollector.build_job()` 透传为 `job.topic_fingerprint` |
| `storage.thread_topic_fingerprint()` | 判据是「父指针为空且 `reply_level=0` 的行**恰好一条**」 |
| `storage.posts.quotes_json` | 新列（数组），空值不覆盖已有的（同 `images_json`） |
| `results._resolve_quotes()` | 出口拿 `message_id` 找被引用楼层，**用它自己的正文/译文/配图**；找不到才退回快照并标 `resolved=false` |
| `excel_service.export_columns()` | 报告里真有引用时才插一列「引用」（一条楼层引多人时逐条列出，`｜` 分隔） |
| 前端 | `PostContent.vue` 在正文上方按顺序渲染**每条**引用；`ResultsView.vue` 按 `thread_kind` 说「主题 / 回复」还是「主贴 / 评论与回复」 |

### 真站验收又逼出来的两个缺陷

第一轮真站全量（3 页 203 楼）跑完，结构那部分全对（1 个主题 + 202 条第 1 层回复），
但冒出两个**只有真站才会暴露**的问题：

**① 16/35 张图 403 —— 而它表现为「图静默缺失」。**

日志里是 `回源 HTTP 403 https://tweakers.net/i/…`。定位下来是两件事叠在一起：

- **Chrome 根本没加载那些图**：真站实测不滚动时，第 1 页 13 张正文图**只加载了 1 张**
  （懒加载），其余全落到 `context.request` 那条回源路
- **那条路一轮连打十几次会被 WAF 挡掉一批**：单发 3 张时 200（探查脚本里试过），
  一场采集里连打十几次就有一半以上 403

修法就是回到 `media.js` 原本的立论 —— **图该由浏览器取，不该另开一条通道**：

- `loadLazyImages()`：整页滚一遍（步长 1200px、120ms），滚完 13/13 张由浏览器加载
- `prefetchQuotedImages()`：被引用的照片在引用块里是 `<a href>[Afbeelding]</a>`，
  浏览器**永远不会去请求它** —— 给每个这样的地址建一个游离的 `Image`，请求就由页面
  自己的网络栈发出（同一套 cookie 与指纹），`attachImageCapture` 直接留下响应体

顺带把「页面加载一次 + 回源一次」减成「只加载一次」，与反爬虫姿态一致。

**② 一条楼层真有两个引用块 —— 只取第一个就是静默丢内容。**

原代码「一条帖子最多一个引用块（实测 97/97）」这个前提是**错的**。真站上确实有
一条楼层先引 `tonko020445`（带 messagelink）再引另一个人（没有链接）的写法。
只取第一个，第二段被引用的内容就永远看不见，而页面上完全看不出来。

`quote`（单个）因此改成 `quotes`（数组），全链路（采集 → 存储 → 出口 → 前端 → 导出）
一起改。fixture 也补了一条带两个引用块的楼层把这个行为钉住 ——
**这次的教训与 fixture 那条一模一样：把「抽查了几个样本」当成「站点一定如此」。**

## 为什么不猜主题（存量库怎么办）

真实库里那 140 条全是并列主贴。此时**不下发**主题指纹，脚本也不挑一条当主题，而是打一句：

> ⚠️ 本轮确定不了主题：没抓第 1 页，库里也没有可用的历史主题指纹
> ↳ 若这个数据源的历史数据是改造前的平铺结构，请对它的任务点一次「全量重跑」

三个理由：

1. **猜错的代价是静默的**：随手挑一条普通楼层当主题，新楼层全挂到它下面，
   页面看上去像对的，实际每个串都错
2. **DB-only 的启发式在真数据上就会选错**。本机那份历史快照
   `tweakers_thread_2336074.json` 里，`page_number=1` 的 100 条**散落在下标 0…138**，
   主题（正文头「Sinds gisteren…」）在**下标 63** —— 「page 1 第一条」这条规则会选中
   25-05-2026 的 Dorpjes，而不是 22-05 的发起帖
3. 项目已有先例：Facebook 层级被压平时就是「不写历史迁移，靠全量重跑」

存量楼层要拿到**引用**更是只能重采 —— 引用正文和引用图只有回源才有，
DB 迁移填不出来。

## 回归

| 用例 | 钉住 |
|---|---|
| `TestTweakersThreadAndQuotesEndToEnd`（6 条，真 Chrome + 真 fixture 站点） | 主题唯一、其余楼层全是它的第 1 层回复（`.topicstarter` 不是主题标记）、`build_tree` 只出一个 root、引用快照字段、**一条楼层引多人两条都在**、**引用绝不进指纹**（与黄金基线逐字相等）、引用图归属 |
| `TestTweakersIncrementalTopicEndToEnd`（2 条） | 增量跑用库里的主题指纹；root 不唯一时不猜且说得出怎么修 |
| `TestQuoteApiEndToEnd`（11 条，真 HTTP → 真 SQLite） | `quotes_json` 往返、解析到楼层用它的全文与译文、解析不到退回快照、没有引用是空数组、`thread_kind`、搜索命中引用、一楼多引用逐条给、导出逐条列、**没有引用时列集合与改动前完全一致** |
| `TestTweakersCollectorGoldenEndToEnd`（5 条） | 9 条正文与指纹逐字等于重构前的脚本；图片汇总那一行按新契约逐字重钉 |
| `frontend/e2e/results_tweakers_quotes.js`（`npm run e2e:quotes`） | 一源一串只出一张「主题」卡、回复区标题、引用框条数与出口逐条一致、解析到的给「#序号」、引用图落在引用框里；没有这种数据时退出码 2，不空转通过 |

### 真站验收（2026-09-19，本机真 Chrome 直连，临时库）

串 `2336074` 跑一轮**全量**（3 页，`complete=true`，退出码 0）。用临时数据目录与临时库，
**没碰用户真实库**。

| 指标 | 期望 | 实测 |
|---|---|---|
| `total_posts` | ≥ 203 | **203** |
| `reply_level == 0` | **恰好 1 条** | **1**：`85322114` / Dorpjes / `22-05-2026 17:06` / 正文以「Sinds gisteren heb ik de HYXi Halo…」开头 |
| `reply_level == 1` | 其余全部 | **202** |
| 带 `quotes[].message_id` 的引用 | ≥ 90 | **94**（引用总数 **97**） |
| `truncated` | > 0 | **6** |
| **一楼引多人** | — | **2 处**（就是「只取第一个会丢内容」的那个场景） |
| 被引用楼层解析不到 | 0 | **0** |
| 引用图落盘 | ≥ 5 张 | **13 张** |
| 正文图落盘 | ≥ 13 张（第 1 页） | **20 张**（14 个楼层） |
| `media` 目录文件数 | = 正文图 + 引用图 | **33** = 20 + 13 |
| **指纹稳定** | 与历史快照重叠的楼层逐字相等 | **138 条重叠、0 处不一致**（快照 140 条里 2 条已不在线上） |
| 图片失败 | —— | **2/35**（改之前是 **16/35**，全是回源 403） |

第二轮（加了两处图片修复之后）的日志里，图片那一行是
`图片汇总：候选图片地址 35 个 · 通过筛选 29 个 · 落盘 33 张（32 张取自浏览器缓存）· 失败 2 张` ——
**32 张由浏览器自己取回**，只有 2 张落到回源那条路并且被 403。
那 2 张的地址原样打在日志里（`↳ 回源 HTTP 403 https://tweakers.net/i/…`），
不再是一句「少了 16 张」都看不见。

任务日志里能一眼核对主题认得对不对：

```
🧵 主题（第 1 页第一条）: [Dorpjes] 22-05-2026 17:06 — Sinds gisteren heb ik de HYXi Halo thuisbatterij in gebruik ...
```

## 非目标

- **不写存量数据迁移**：填不出引用，且启发式会选错主题（见上）
- **不点 `.toggle-quote`**：实测点了没用（文本不变、0 XHR），而且「点按钮」的改动
  fixture 通过不算验证
- **不解析荷兰语日期**：`quotes[].timestamp` 只取自解析成功后的被引用楼层；
  快照保留 `cite` 原文由 UI 直接显示
- **不给回复加「🔗 原帖」**：`_post_url()` 的「只给 `reply_level==0`」判据是为 Facebook
  设的，动它会影响 Facebook
- **引用图不进多模态理解、不进舆情**：它属于被引用者，算进引用者的图片描述就是张冠李戴
