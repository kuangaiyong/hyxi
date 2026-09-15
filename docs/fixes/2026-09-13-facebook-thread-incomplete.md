# Facebook 主贴的回复采不全、回复的回复被压平（v1.12.0 修）

> 呈现方式（回复按原帖结构展示、完整性提示）属于新功能，规格见
> `docs/features/facebook-comment-threads.md`。这份只讲采集侧的故障。

## 现象

用户用 v1.11.4 全量重跑（任务 `49f060f0`，force_full）后反馈：主贴
`https://www.facebook.com/groups/2407063016436085/permalink/2534929036982815/`
原帖上有 6 条评论与回复，结果页只采到 1 条（Richard Boer「De accu zal eerst volledig
opladen…」），Richard Boer 另一条带图的评论和它下面 4 条回复（含回复的回复）全部没有。

**不是个例。**同一份数据（便携包数据目录，来源 `src_0178204f`，306 行）：

```
reply_level 分布          {0: 124, 1: 182}      ← 第 2 层 0 条
每主贴直接评论数分布      {0: 21, 1: 35, 2: 60, 3: 6, 4: 1, 5: 1}
```

- 真站上的「回复的回复」**一条都没有入库成第 2 层**；有 2 条回复正文以被回复人名字
  开头（「Alan Vdh gemaild op 8/08…」），却挂在第 1 层 —— 采到了，但层级被压平
- 「Hugo van Touw」那条主贴在库里有两行：有 id 的一行挂 1 条评论，**没 id、没时间**的
  一行挂 5 条；旁边还有一条葡萄牙语的「匿名」主贴，同样没 id 没时间，与这个小组无关

那次运行的逐批日志只走 SSE，没有留存，无法事后从日志判断。

## 复现

### 本地（稳定复现，真 Chrome + `login_site.py` fixture）

用测试类同一套临时库与启动参数跑一轮，打印入库结构：

```
mid= 9001 L0 Marieke_V    父=    -
mid= 5501 L1 Joost1988    父= 9001
mid= 5502 L1 Sanne_K      父= 9001   ← DOM 上嵌在 5501 里面
mid= 5504 L1 Ruud_T       父= 9001   ← 链接是 comment_id=5501&reply_comment_id=5504
mid= 5503 L1 Bram_H       父= 9001
mid= 5505 L1 Lieke_dV     父= 9001
mid= 9004 L0 Sofie_M      父=    -   ← 详情页里的评论 5601 从来采不到
mid= 9002 L0 TechNerd_NL  父=    -
```

### 真站（只读探查两次，用户授权；共 3 次页面加载、1 次浮层）

时间（北京时间）23:33:47–23:34:21、23:37:28–23:38:01，两次结束时会话都仍有效。

**信息流里这张卡片**（`[role=button]` 的文字 / aria-label）：

```
('6', '发表评论')          ← 原帖评论数，含回复（2 条评论 + 4 条回复）
('查看更多回答', '')       ← 折叠按钮。采集器的模式里只有「查看更多评论」
('展开', '')               ← 首屏那条评论的正文也折叠着
```

**点「查看更多回答」**（DOM `click()`，与采集器同一种点法）：

```
URL 变成 /groups/2407063016436085/permalink/2534929036982815/，同时出现 [role=dialog]
浮层里 7 个 article（主贴 + 6 条）；点浮层的「关闭」后 URL 回到信息流，卡片仍在，未重载
```

**浮层里的真实结构**（depth 是相对主贴 article 的祖先 article 数，left 是渲染缩进）：

```
depth left  aria-label                                   链接
1     418   评论者：Richard Boer1 天前                    ?comment_id=2535013143641071
1     479   1 天前Hans Bohlmeijer回复了Richard Boer的评论   ?comment_id=2535013143641071&reply_comment_id=2535092480299804
1     521   1 天前Richard Boer回复了Hans Bohlmeijer的回复   ?comment_id=2535013143641071&reply_comment_id=2535093520299700
1     521   1 天前Richard Boer回复了Hans Bohlmeijer的回复   ?comment_id=2535013143641071&reply_comment_id=2535095646966154
1     479   1 天前Hans Bohlmeijer回复了Richard Boer的评论   ?comment_id=2535013143641071&reply_comment_id=2535480800260972
1     418   评论者：Richard Boer1 天前                    ?comment_id=2535010646974654
```

- **回复不嵌在父评论的 article 里**：六条全是主贴 article 下的兄弟节点，层级只体现在
  缩进（418 / 479 / 521）和 DOM 深度上
- 评论的链接只有 `comment_id=<自己>`；**回复不论第几层**都是
  `comment_id=<所属顶层评论>&reply_comment_id=<自己>`
- 浮层里的主贴头部**没有** `/posts/<id>` 链接
- 评论排序默认「最相关」；菜单三项为「最相关 / 由新到旧 / 所有评论（含疑似垃圾信息）」，
  底部注明「评论排序方式将应用于 Facebook」—— **这是账号级设置**

## 根因

三个独立缺陷叠在一起，外加一个由第二个派生出来的脏数据来源。

### 1. 折叠文案认不出「回答」—— 按钮从来没被点过

`collectors/facebook_group.js` 的 `SELECTORS.commentFoldText` 只收「评论 / 回复」两种
叫法。这条帖子的折叠叫「查看更多回答」，一个模式都匹配不上，`expandComments()` 直接跳过，
只剩首屏那 1 条。

**为什么以前没出问题**：模式是 v1.11.0 按「应该叫什么」写的，交付时标过「按钮文案未在
真站核实」，此后一直没核实；这类帖子的回复本来就只有首屏那几条，页面上看不出少了。

### 2. 点折叠会打开详情浮层，v1.11.4 把它当事故处理、整轮拉黑

文案认得出的折叠（「查看更多评论」），真站上点下去是 **pushState 换 URL + 弹浮层**。
v1.11.4 的 `recoverFromDetailView()` 退回信息流后把这条主贴加进 `detailPosts`
（`facebook_group.js:328-331`），**本轮再也不展开**。同一个按钮每一轮都是同样的行为，
所以这类主贴的隐藏评论**在任何一轮都采不到**。`test_core.py` 里
`test_a_fold_that_opens_the_post_detail_does_not_swallow_the_feed` 还断言了
`"5601" not in got`，注释写「下一轮再补」—— 这句不成立。

**为什么以前没出问题**：v1.11.4 引入。之前的 v1.11.0~v1.11.3 点下去之后没退回来，
表现为整轮只剩一条帖子；再之前根本不点。这条主贴的回复，任何一个版本都没采全过。

### 3. 回复的层级被写死成「主贴下的第 1 层」

`extractBatch()` 用 `article.querySelectorAll('[role="article"] [role="article"]')`
把主贴下所有 article 一次取平（`facebook_group.js:434`），`flatten()` 再把它们一律写成
`parent_fingerprint: post.fingerprint, reply_level: 1`（`:500-509`）。`test_core.py`
的 `test_folded_comments_are_expanded_before_extraction` 断言「嵌套回复必须挂在主贴上
—— 存储层是扁平的，嵌套只在出口组装」，把这个行为钉成了「正确」。

那句断言**误读了存储红线**：「存储层扁平」说的是 posts 是一张扁平表、嵌套靠父指针在
出口组装（`build_tree()` 本来就支持任意深度，`order_by_thread()` 的回归里就有
`[0, 1, 2, 0]`），不是说父指针只能指向主贴。

而且真站的 DOM 让「按 DOM 嵌套找父评论」这条路也走不通：浮层里回复是兄弟节点，
只能靠缩进 + 链接里的 `comment_id` 定层级。

**为什么以前没出问题**：v1.11.0 之前折叠不点，嵌套回复几乎不进 DOM；进了 DOM 的也和
普通评论长得一样挂在主贴下，页面上看不出来。

### 派生：提取没有排除浮层

提取器取的是整个 document 里的顶层 article。退回信息流的那一刻浮层若还没撤下，
浮层里那条主贴（头部没有 `/posts/` 链接 → 没 id、没 hover 到时间）连同它的评论会被
当成信息流里的一条新主贴入库；浮层后面的背景信息流也可能混进来。这正是
「Hugo van Touw」无 id 重复行和那条葡萄牙语主贴的来路。

## 修法

采集脚本 `collectors/facebook_group.js`（设计取舍见 `docs/features/facebook-comment-threads.md` 的 D1–D9）：

| 根因 | 修法 |
|---|---|
| 1. 文案认不出「回答」 | `commentFoldText` 收「查看更多回答 / 查看之前的回答 / 查看全部回答 / 更多回答」及英文三条 |
| 2. 浮层被当事故、整轮拉黑 | 删掉 `detailPosts` / `recoverFromDetailView()`。一次点一个折叠，`afterFoldClick()` 判出弹了浮层就 `harvestThread()` 就地收割（先核对浮层地址里有这条主贴的 id），`closeThreadDialog()` 按「关闭按钮 → Esc → 后退 → 重新载入信息流」逐级关；卡片上没折叠可点、或点了没收成的，信息流滚完后 `completeFromPermalinks()` 按固定链接补 |
| 3. 层级写死第 1 层 | 提取合成一份在页面里执行的 `extractInPage()`（卡片与浮层共用），`threadOf()` 按「DOM 嵌套 → 缩进栈 → 链接 comment_id 校验」定父评论；`flattenComments()` 按父指针写 `parent_fingerprint`、沿父指针数 `reply_level` |
| 派生：浮层残留 | 信息流提取排除 `[role=dialog]` 里的 article；存量由 `storage.merge_duplicate_posts()` 并掉「没 id、没时间，作者与正文和**唯一一条**带 id 的主贴逐字相同」的主贴（记别名、搬译文 / 舆情、改挂孩子），对不上的留着 |

为了让补齐不拖垮一轮采集，一并加了三件事：

- **原帖评论数**：采集读卡片上的评论数按钮，存 `posts.site_comment_count`（可空，读不到不覆盖）。
  原帖数不比「卡片已有 / 库里已有」多的主贴不打开浮层；库里已有数由 `storage.known_comment_counts()`
  按主贴 message_id 算好随 job 下发（全量重跑时清空）= max(整棵子树条数, `harvested_site_count`)。
  后者是上一次「浮层里折叠点到一个不剩、时间都取了」时原帖显示的数 —— 被「最相关」藏掉的评论
  永远对不上，只数子树的话这类帖子每轮增量都再开一遍
- **两个数每轮都交**：增量运行时老主贴指纹见过就不再输出，脚本另把本轮读到的每条主贴的
  `site_comment_count` / `harvested_site_count` 放进输出的 `thread_counts`，runner 在 upsert 之后调
  `storage.record_thread_counts()` 写回。否则原帖数停在第一次采到时，涨了之后没补齐也不标黄
- **采集时限**：`CollectorRunner` 在 job 里下发 `deadline_at`（`collect_deadline_ms()`：超时前留 3 分钟，
  短超时最多让 20%）。过点不再开浮层、不滚动、不补齐、不再 hover 取时间，按退出码 2 交出已采到的；
  被限流时要求的退让会睡过时限的，不睡、当场按拒绝访问停下（`gotoPage` 的 `deadlineAt`）
- **固定链接补齐逐条兜住**：某一条打不开只跳过这一条；限流（`err.blocked`）和浏览器没了照样停。
  信息流里点开的浮层同样逐串兜住：收割到一半页面调用出错，只放弃这一串、关掉浮层接着采
- **不点评论排序菜单**（账号级设置），只把浮层里当前的排序名记进日志

交付前四轮代码评审补的（都先写了复现用例、确认红，再修）：

| 评审发现 | 修法 |
|---|---|
| 固定链接补齐出错会把整轮判残缺 | 逐条 try/catch，限流与浏览器没了照样往外抛 |
| 条数永远对不上的主贴每轮增量都再开一遍；老主贴的原帖数增量时不刷新 | `harvested_site_count` + `thread_counts`（见上） |
| 过了时限还在逐条 hover 取时间 | `resolveTimes()` 过点即停，没取到的照样输出 |
| 浮层一出现主贴就收割，评论若随后才到会被记成「整串读完」 | `waitForThreadSettled()`：article 条数稳定 1.5 秒再收，等不到评论就不收 |
| 链接指向的顶层评论没采到时，回复仍挂在按缩进猜的错误父评论下 | 挂主贴；校验改比「所属顶层评论」，挂了主贴的回复的子回复不被拍平 |
| 找开着的浮层有两处没查可见性 | 统一要求 `getClientRects().length > 0` |
| 判「就地展开了」数整页 article，信息流懒加载会被误判，同一个折叠被点到轮次上限 | 只数这条主贴自己的 article |
| 浮层里确实一条评论都看不到的主贴，被「等不到评论就不收」判失败、每轮重开 | 等到头仍只有主贴且卡片上也没露评论的，收空串记读完；卡片上露过的不收，走固定链接 |
| tooltip 没出来的回复也被算成读完，时间从此补不回来 | 「读完」要求每条带时间锚点的评论都取到了时间 |
| 信息流里的浮层收割到一半页面调用出错（例如点了什么触发整页跳转），异常冒到 main：首轮直接硬失败，这条主贴每轮再点、再失败 | 逐串兜住：放弃这一串、关掉浮层接着采，原帖数对不上的留给固定链接补；浏览器没了照样停 |
| 限流退让最多睡 5 分钟，比时限余量（3 分钟）长：睡醒之前已被按超时杀掉，交接文件没写 | `gotoPage` 带上时限，退让要睡过时限就当场按拒绝访问停下，退出码 2 交出已采到的 |

结果页（`ResultsView.vue`）：回复全部展开、按原帖顺序，第 2 层起写「回复 某某」，有回复的评论标
「N 条回复」，主贴头部「已采 X · 原帖 Y」，X < Y 标黄说「可能不全」。

## 回归证据

### 本地

- 后端全量 **443 passed、0 failed**（基线 412，本次净增 31 条）
- 每条修复都先写复现用例、对改动前的代码确认红，再实现转绿；然后逐条把修复改回去、确认对应用例重新变红
  （反向验证，每次改完逐字节还原并核对 sha256）。共 26 处，全部红在要守的那条行为上。交付前最后几处：
  - 空浮层每轮重开（r16）→ `permalink:9011` 被打开 2 次；浮层评论永远不到（r16b）→ 没按固定链接补；
    时间没取到算读完（r17）→ 下一轮没再开 9004；过了时限还 hover（r18）→ 10 条仍在取时间
  - 收割出错不兜（r19）→ 整轮 `code=1`；退让不看时限（r20）、补齐处不传时限（r20b）→ 睡满 300 秒又打第二次
  - 地址判据改回 `/<id>/`（r21）→ 9004 的浮层不收、退到固定链接整页补
- Facebook E2E 全类 39 条对**便携包里压缩后的采集脚本**（包内 node + playwright，`TWEAKERS_PROJECT_ROOT`
  指向包目录）再跑一遍：39 passed —— 送进 `page.evaluate` 的函数这次新增了好几个，压缩改名最容易在这里坏。
  修完真站暴露的地址判据后重新出包，`build\verify_package.ps1` 通过（首启自举、单端口、三条升级路径），
  并对新包里的脚本跑地址无斜杠 / 原帖结构 / 收割出错 / 限流越时限 4 条：4 passed
- 打包的泄漏自检原来写死「压缩产物超过 200 行就判没压缩」，这次 `facebook_group.js` 压缩后 248 行被误拦
  （限了行宽，行数随体积涨）。改成「行数不到源码的一半」，用压缩产物 / 不压缩的 bundle / 原样源码三种输入核对过：
  只有第一种放行
- 前端 E2E：`e2e:stale` 单端口形态 8 项通过（开发库副本）；`e2e` 5 项、`e2e:link` 12 项、`e2e:sentiment` 13 项、
  `e2e:thread` 241 项，在下面那次真站重跑后的便携包数据上通过。数据凑不出、**没验到**的场景：`e2e` 场景三
  （「筛得空 / 筛得出」两个窗口）、`e2e:link` 场景二之二（排在树根的回复）

### 真站（2026-09-15，v1.12.0 便携包 + 用户那份数据 `src_0178204f`，用户授权）

- 起包前备份 `HYXi-数据\hyxi.db.bak-before-v1.12.0`（306 条）。启动时加上两列；`merge_duplicate_posts()` 把
  「Hugo van Touw」那条无 id 主贴并进带 id 的那行（现挂 6 条），葡萄牙语那条对不上、按设计留着
- 对「只采集」任务 `8787b0cf` 全量重跑（新任务 `b4b9a411`；滚动批次临时调到 30、这个进程的任务超时设
  60 分钟，跑完已恢复）：13:41:12 → 14:17:06，约 36 分钟，`completed`。滚 18 批到底，第一批提取 66 条
  （v1.11.0~v1.11.3 同一步 1~11 条）；浮层收割 78 串，固定链接补齐 15 条
- **主贴 `2534929036982815`：8 条，与原帖逐条一致**（用户报障之后原帖又多了 2 条，原帖数 6 → 8）：

  ```
  L1 Richard Boer      De accu zal eerst volledig opladen…
  L1 Richard Boer      De werk modi kan je bij het omcirkel…
    L2 Hans Bohlmeijer   Richard Boer Ha Richard…
      L3 Richard Boer      Hans Bohlmeijer ik zou hem even met…
      L3 Richard Boer      Hans Bohlmeijer deze even op 100%…
    L2 Hans Bohlmeijer   Richard Boer ...Top man !!! Bedankt.
  L1 Hans Bohlmeijer   Heb hem hele weekend op 100% gezet…        （9/14 新增）
    L2 Mark Woudstra     Hans Bohlmeijer Ik kon de aanpassing…    （9/14 新增）
  ```

- 全库 306 → 1076 行；`reply_level` 分布 `{0:124, 1:182}` → `{0:126, 1:397, 2:250, 3:303}`；本轮新增没有
  message_id 的主贴 0 条；「已采 > 原帖」0 条（没有串到别的帖子下）
- 标黄（已采 X < 原帖 Y）18 条：17 条浮层里折叠点到一个不剩、时间全取到（`harvested_site_count = Y`），
  差的 1~7 条浮层里就看不到（「最相关」藏掉的疑似垃圾、删了没减数），增量运行原帖数不涨就不再打开；
  1 条（`2416528175489569`，7/16）折叠没点完、没记成读完，下一轮增量会再打开
- **真站暴露、已修**：浮层地址不带末尾斜杠（`/permalink/2476737042802015`），`harvestThread()` 按 `/<id>/`
  比判成对不上、这一串不收，信息流滚完后按固定链接补回 2 条。改成按路径段比，复现用例
  `test_dialog_whose_address_has_no_trailing_slash_is_harvested`（fixture `slashless_thread_url`）
- 紧接着那个浮层用关闭按钮 / Esc / 后退都关不掉，重新载入信息流 1 次（78 个浮层里唯一一次），**原因没查明**；
  重载后已采的整批重滚回来（第 6~13 批新增 0，每批约 10 秒），本轮照常跑完
- 随后又跑了一轮**增量**（任务 `56757f1c`，重打的包，滚动批次上限 25）：43 秒，滚 2 批就「已翻到历史数据」停下；
  新增 1 条是几分钟前刚发的新主贴，读完的串一个都没重开（R4-S1 / S3），新增没有 message_id 的主贴 0 条

## 顺带发现

- **v1.11.4 那次全量重跑是撞上「每轮最多滚动批次」默认值 10 才停的**（`total_pages` 11），
  不是滚到了信息流底部。更早的主贴那一轮没有被重新采集。全量重跑时建议在数据源页调大
- **探查把账号的评论排序改成了「所有评论」**（为了核实默认排序会不会藏评论）。这是账号级
  设置，已如实告知用户；按用户决定，2026-09-15 真站验证那一轮开始前改回了「最相关」（打开一次目标主贴、
  点一次排序菜单；改后复读确认，会话文件未改写）
- fixture 里两条假设与真站不符，本次一并按真站改：嵌套回复是「嵌在父评论 article 里」、
  详情是「整页跳转」—— 真站分别是「兄弟节点 + 缩进」和「pushState + 浮层」
