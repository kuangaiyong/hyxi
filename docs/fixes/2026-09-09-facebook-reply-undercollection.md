# Facebook 主贴的回复采不全（4 条只入库 3 条），以及回复挂错主贴

> 产物按用户口径落在 `docs/` 下，不新建 `.superpowers/` 目录树。

## 现象

用户使用 v1.10.1 反馈：Facebook 主贴
`https://www.facebook.com/groups/2407063016436085/permalink/2494381381037581/`
下的回复贴对不上 —— 只采集到 3 条，实际应该有 4 条。

## 复现

**稳定复现，靠数据不靠猜。**这台机器不访问 Facebook（自动化抓取违反其服务条款），
所以复现走两条都不需要访问真站的路：

**(a) 真实库跑不变量**（`backend/data/hyxi.db`，来源 `src_b32bc603`，与用户同一个小组）：

```
主贴 79 条 / 回复 92 条
每主贴回复数分布： 1条→17个   2条→30个   3条→5个   ≥4条→ 0 个
```

**上限恰好卡在 3，79 条主贴一条都没超过。**真实小组的评论数不可能是这个形状 ——
这是首屏渲染上限的指纹，不是自然分布。用户说的「只采到 3 条、实际 4 条」正落在
这个天花板上。同一次排查还查到：`seq 17/18` 两条回复的父指针
`5de87dc32c712b76` 在 posts 表里**根本不存在**。

**(b) fixture 站点复刻**（`backend/tests/fixtures/login_site.py`，选择器与
`facebook_group.js` 逐字一致，真 Chrome、真 HTTP、无 mock）：给第一条主贴加上
「查看更多评论」（故意分两页）和「查看 1 条回复」两处折叠。修复前采到
`{5501, 5502, 9001, 9002}` —— 折叠里那三条评论一条都没有。

## 根因

三条，全部指向同一个症状「主贴下的回复少了 / 挂错了」：

### ① 评论区的折叠从来没被点开（这一条是用户报的那个）

`collectors/facebook_group.js` 里唯一的「点开折叠」是 `expandBodies()`，它只匹配
`^(展开|See more|Meer weergeven)$` —— 那是**正文**的折叠按钮。评论区另有两类按钮：

- 「查看更多评论」：首屏只渲染前两三条评论，其余藏在后面，**而且分页**（点一次只多出一页）
- 「查看 N 条回复」：一条评论底下的嵌套回复

两者都不在 `expandText` 的三个词里，一次都没被点过，所以只采到首屏渲染的那几条。

**为什么以前没出问题？**它一直有问题，只是**看不出来**：采到 3 条也是一个像样的
回复列表，页面上不会有任何异常，唯一的破绽是「所有主贴的回复数都不超过 3」这个
统计形状 —— 而那要把整库拉出来做分布才看得见。

### ② 父指针没过 `post_aliases`，回复指向一个已被归并掉的指纹

v1.10.1 引入 `post_aliases` 时，只把**帖子自己**的身份过了别名表，
`parent_fingerprint` 仍然只过 `canon`（本批下发帖子的指纹映射）。漏的那条路：

1. 主贴 P 首次入库拿指纹 F1
2. 下一轮 P 漂成 F2 → 按 message_id 归并回 F1，F2 记进 `post_aliases`
3. 再下一轮 P 又渲染成 F2 —— 而 `known_fingerprints()` **并上了 post_aliases**，
   采集器于是把 P 当已见过的**过滤掉，不再下发**
4. 这一轮新出现的回复 R 算出的父指针是 F2，本批里却没有 P，`canon` 查不到 F2 →
   原样落库

F2 在 posts 表里不存在，R 成了悬空回复；下次启动 `merge_duplicate_posts()`
把它**提成主贴** —— 那一步不可逆。用户看到的就是「这条回复不在它该在的主贴下」。

**为什么以前没出问题？**这条缝是 v1.10.1 自己引入的：在别名表存在之前，
`known_fingerprints()` 只回 posts 表里的指纹，第 3 步那个「已见过所以不下发」
根本不成立。修一个 bug 时开的新路径没有跟着覆盖到父指针这一支。

### ③ `drop_empty_posts()` 永远捞不回一条空正文的回复

它的「有评论就不算空」只捞得到**父贴**（`anchored` 要求有个非空的*子*帖）。
叶子回复没有子帖，**永远进不了 anchored**。而纯贴图 / 表情回复正文是空的，
`imagesOf()` 又只认 scontent 上 ≥100px 的图（贴图和 emoji 都不在其列），
images 也是空的 —— 于是在 posts 表这个唯一入口被静默丢掉。

采集脚本的 `isNotAPost()` 早就是「id 和正文全缺才丢」这个口径（注释里写着
「纯图片帖有 id 没正文…两种都是真帖子」），存储层比它严就是**两层自相矛盾**。

**为什么以前没出问题？**在真实库里目前**观测不到实例**（`reply_level=1` 且正文空的
行是 0 条）—— 机制成立、尚未命中。之所以一起修，是因为它与 ① 的症状完全无法区分：
真出现时同样表现为「4 条回复只剩 3 条」，而且照样一点痕迹都不留。

## 修法

| 处 | 改动 | 为什么是最小的 |
|---|---|---|
| `collectors/facebook_group.js` | `SELECTORS.commentFoldText` + `expandComments()`，在 `extractBatch()` 里紧跟 `expandBodies()` 调用 | 与既有的 `expandBodies()` 同形（同样按文字认按钮、同样等 DOM 变化、同样打日志），没有引入新概念 |
| `backend/app/services/storage.py` | `upsert_posts()` 里 `parent_fp = canon.get(parent_fp) or aliases.get(parent_fp) or parent_fp` | 一行。`aliases` 是同一函数里已经查好的，不新增查询 |
| 同上 | `drop_empty_posts()` 的 `empty` 判据加一条 `and not message_id` | 一行。把存储层的口径对齐到采集脚本 `isNotAPost()` 已有的口径，不是新发明一个规则 |

三处都**没有**新增数据库列、没有改指纹算法、没有改 `pacing` 配置面。

原本打算给回复加一个 `parent_message_id` 字段作为兜底（G1 里就是这么向用户描述 ③ 的），
排查到 ② 之后发现**不需要**：别名表那一行覆盖了「父贴没被下发」的全部可达情形，
加字段属于多余抽象。

**评论折叠的循环节奏**：每轮之间照常 `humanDelay(delayMin, delayMax)` —— 连着几十次
点「加载更多」在行为分析里比翻页还扎眼，与「像一个诚实、有礼貌的真实浏览器用户」
这条姿态一致。轮次上限 `COMMENT_FOLD_ROUNDS = 8`，**撞上限会打日志**：静默停在半路
等于又一次少采，而那回连日志都不会提。

## 回归证据

红 → 绿，四条新测试：

```
# 红（修复前）
FAILED test_folded_comments_are_expanded_before_extraction
  AssertionError: 「查看更多评论」没点开，第一页折叠评论漏了
  assert '5503' in {'5501', '5502', '9001', '9002'}
FAILED test_reply_pointing_at_a_merged_away_fingerprint_is_repointed
  AssertionError: 回复挂在一个已被归并掉的指纹上 —— 启动时会被当孤儿提成主贴
  assert 'F2' == 'F1'
FAILED test_a_reply_with_an_id_but_no_text_is_not_silently_dropped
  AssertionError: 有 message_id 的空正文回复被静默丢掉了

# 绿（修复后，全量）
394 passed
```

`test_a_truly_empty_post_with_no_id_is_still_dropped` 是配套的**反向**用例：
id 和正文都没有的空壳照旧要丢掉（广告 / 推荐卡片），防止把门开太大。它修复前后都绿。

fixture 站点的产出从 4 条变 7 条（2 主贴 + 3 顶层评论 + 2 嵌套回复），
三条写死 `== 4` 的老断言同步更新为 `== 7` —— 那是 fixture 的应采量真的变了，
不是把断言改松。

## 顺带发现（看到但没动）

- **真实库里 `seq 17/18` 那两条悬空回复修不回去。**它们的父指针
  `5de87dc32c712b76` 已经不在库里，父贴的真实身份**无从得知**（本地也没有那一轮的
  采集产出文件）。`merge_duplicate_posts()` 会按既定的诚实兜底把它们提成主贴。
  ② 修的是「以后不再产生新的悬空」，不是「把这两条接回去」—— 后者只能靠猜。
- **`imagesOf()` 只认 scontent host + ≥100px**，所以贴图 / GIF / 表情回复一律没有配图，
  在报告里会是一行空正文的「未分析」。③ 让它至少**存在**（不再静默消失），
  但要看懂它的内容得让多模态去读贴图 —— 那是另一件事，本次没做。
- **`expandComments()` 的按钮文案是照着中/英/荷三种界面写的模式，没有在真站上核实过**
  （这台机器不访问 Facebook，且自动化抓取违反其服务条款）。真站上若还有别的措辞，
  表现是「那一处折叠没点开」，而日志里会有 `点开评论折叠 N 处` 可以对账 ——
  用户在自己机器上跑一轮就能看出 N 是否为 0。

## 后续（2026-09-12，v1.11.0 发版前评审）

发版前的多视角评审在同一条链路上又挖出四条，全部已修，详见
`docs/reviews/2026-09-12-v1.11.0-prerelease-review.md`：

- **`expandComments()` 必须排在 `expandBodies()` 之前** —— 否则折叠里加载出来的长评论
  以残文入库，可见部分超 100 字时**永久**修不回来（截断版和完整版同一个指纹）
- **嵌套回复的 id 要认 `reply_comment_id`** —— 只认 `comment_id` 会拿到父评论的 id，
  入库按 id 归并后**父评论那一行被回复整条覆盖**
- **折叠文案漏了「某某 已回复 · N 条回复」等 7 种写法**，其中一种中文界面就会遇到
- **`merge_duplicate_posts()` 判孤儿前也要查 `post_aliases`** —— 本文档「修法」一节里
  ② 只修了 `upsert_posts` 那一路，存量那批回复在升级后的第一次启动仍会被提成主贴

**上面「顺带发现」第一条经核实是准确的**：核实者只读查过真实库，
`5de87dc32c712b76` 在 `post_aliases` 里既不是 fingerprint 也不是 canonical，
posts 表里也没有，代码里也没有任何 `DELETE FROM post_aliases`。
所以 seq 17/18 的父贴身份确实无从得知，不要试图按别名表去「修回」它们。
