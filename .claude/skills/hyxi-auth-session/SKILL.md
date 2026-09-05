---
name: hyxi-auth-session
description: hyxi 的登录、会话复用与人工授权：facebook.com 真站实测结论（表单选择器、提交方式、Arkose 人机验证）、小组页 DOM 实测结论、已排除且不要再试的三条路。改 collectors/lib/auth.js、facebook_group.js、人工授权接口或任何 Playwright 选择器之前，必须先读这份。
---

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

