/**
 * Facebook 公开小组采集器。
 *
 * 与 group_feed.js 同形（主贴 + 嵌套评论、批次翻页、读旧再合并），多的是一层登录：
 * 优先复用 storageState，会话失效才动用凭据；撞上两步验证或安全检查就以退出码 3
 * 交回给人，绝不尝试绕过。
 *
 * ⚠️ Facebook 服务条款禁止自动化登录与抓取，账号存在被封风险。请使用专用小号，
 *    不要复用任何有价值的账号。
 */

const { readJob, log, progress, writeOutput } = require('./lib/job');
const { makeFingerprint } = require('./lib/fingerprint');
const { launchBrowser, newContext, saveStorageState } = require('./lib/browser');
const { gotoPage } = require('./lib/http');
const { humanDelay, humanRead } = require('./lib/human');
const { ensureLogin, waitForManualLogin, needManualAuth } = require('./lib/auth');
const { attachImageCapture, saveImages, logImageScan, newImageTally, logImageTally }
    = require('./lib/media');

const job = readJob(process.argv.slice(2));
const params = job.params || {};
const pacing = job.pacing || {};

const CONFIG = {
    groupId: params.group_id,
    startBatch: params.start_page || 1,
    // 人工授权时必须有头，否则人看不见验证页也点不了
    headless: job.mode === 'login_only' ? false : params.headless !== false,
    mode: job.mode || 'collect',
    delayMin: pacing.delay_min || 4000,
    delayMax: pacing.delay_max || 11000,
    timeout: 30000,
    // 人工授权的等待上限。可配是为了能在验证里跑短一点，默认 5 分钟
    manualLoginTimeout: params.manual_login_timeout_ms || 5 * 60 * 1000,
    incremental: !!job.incremental,
    // 已有指纹由 Python 从 posts 表算好下发，脚本不再读旧落盘文件
    knownFingerprints: job.known_fingerprints || [],
    // 主贴 message_id → 不必再打开补齐的门槛：库里已有几条评论与回复、上次整串读完时原帖显示几条，
    // 取大的（全量重跑时为空，见 storage.known_comment_counts）。原帖评论数不比它多就不打开帖子补齐
    knownCommentCounts: job.known_comment_counts || {},
    baseUrl: (job.base_url || 'https://www.facebook.com').replace(/\/+$/, ''),
    outputFile: job.output_path,
    stateFile: job.state_file,
    // 图片落盘根目录，由 job 指定（与 output_path 同理，文件位置只有一个来源）。
    // 没配就不抓图，老 job 文件照样能跑
    mediaDir: job.media_dir || '',
    sourceId: job.source_id || 'unknown',
    maxBatches: params.max_batches || 10,
    // 几点前必须收尾（毫秒时间戳），runner 按任务超时算好下发。过点就不再开帖子、不再滚动、
    // 不再补齐，按退出码 2 交出已采到的 —— runner 到了超时是直接杀进程，交接文件没写就一条不剩
    deadlineAt: Number(job.deadline_at) || 0,
};

// 提取与登录判定都靠选择器。站点改版时提取器返回 0 条，被「第一批零帖子即硬失败」拦住，
// 不会写出一份看起来完整实际是空的结果。
const SELECTORS = {
    // 以下选择器于 2026-08-03 对真实 facebook.com 探测核实过（见 CLAUDE.md）：
    // 表单元素的 id 是随机的（形如 _R_1h6kqsqppb6amH1_），只能按 name 选；
    // 登录页上 [data-testid] 和 [name="login"] 都不存在，别再往回加。
    loggedIn: '[role="feed"], [data-pagelet^="GroupsFeed"], [aria-label="创建帖子"], [aria-label="Create a post"]',
    usernameInput: 'input[name="email"]',
    passwordInput: 'input[name="pass"]',
    // 没有 submitButton：表单靠在密码框按回车提交（原因见 lib/auth.js 的 ensureLogin）
    twoFactorInput: 'input[name="approvals_code"], #approvals_code',
    // 只认 #error_box。**别加 [role="alert"]** —— 填入密码后页面会冒出一条
    // aria-live 提示（荷兰语「Je wachtwoord wordt weergegeven」/「你的密码正在显示」），
    // 那是无障碍朗读用的，不是错误；把它当错误会让每一次正常登录都被判成密码不对。
    loginError: '#error_box',
    // 内容提取。以下于 2026-08-04 对真实小组页（已登录）核实：
    // 页面上没有 abbr[data-utime]，也没有 data-post-id / data-comment-id，
    // 帖子和评论的 id 只能从固定链接的 URL 里取。
    // 评论与回复也是 article，就在主贴 article 里面（卡片上可能再嵌一层，浮层里是兄弟节点）
    post: '[role="article"]',
    // 主贴和评论都没有 h3 / strong，作者只剩小组内的个人主页链接。同一个人会连出几个
    // 这样的链接，**排在前面的是头像、文本为空** —— 直接 querySelector 取到的就是空的
    // 那个，于是每条帖子都成了匿名（取法见 nameOf）
    author: 'a[href*="/user/"]',
    // 时间锚点：主贴是头部那个固定链接，评论自带 comment_id。
    // 两者的 aria-label 都不能用，原因见 resolveTimes()
    postTime: 'a[href*="/posts/"]:not([href*="comment_id"])',
    commentTime: 'a[href*="comment_id"]',
    body: '[data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]',
    // 评论没有专用正文容器，正文是一串并列的 div[dir=auto]，**一段一个**（见 commentText）
    commentBody: 'div[dir="auto"]',
    // 以下两个是**文本模式不是选择器**：折叠正文那个按钮只能按文字认。
    // 登录后的界面语言由 Facebook 账号自己的设置决定，与采集器的 locale 无关
    // （见 CLAUDE.md），所以中英荷三种都收。
    expandText: '展开|See more|Meer weergeven',
    // 展开后按钮文字变成「收起」，textContent 会把它一起吃进正文；没点开的则残留
    // 「… 展开」。都是界面文案不是正文，而 content 前 100 字进指纹 —— 留着等于把
    // UI 文案写进去重锚点，还会让同一条帖子展开前后算出两个指纹。
    bodyTrail: '\\s*(…\\s*)?(展开|收起|See more|See less|Meer weergeven|Minder weergeven)$',
    // **评论区自己的折叠，和上面正文那个「展开」是两回事**：首屏每条主贴只渲染前
    // 两三条评论，其余藏在「查看更多评论」后面（还分页，点一次只多出一页）；一条评论
    // 底下的嵌套回复另有一个「查看 N 条回复」。expandText 那三个词一个都碰不到它们。
    // 这些文案里**带条数**，所以不能像 expandText 那样当成完整词精确匹配。
    // 必须挡住「回复」「评论」「分享」这类动作按钮：每一条模式都要么带「查看/更多/
    // weergeven/bekijken」，要么带一个数字，光是「回复」两个字匹配不上 ——
    // 误点「分享」会弹出对话框，误点「回复」会打开输入框，两者都会把页面搞乱。
    // 回复数那几条允许带「某某 已回复 · 」这样的前缀：折叠起来的回复串，按钮上常常
    // 先写是谁回的、再写条数（「Jan replied · 2 replies」）。前缀里必须有「·」，
    // 动作按钮不会长这样。
    // 回复也分页：第一页点开后按钮变成「查看更多回复」/「View more replies」，三种语言都要收。
    // **还有叫「回答」的**：2026-09-13 真站实测，原帖 6 条、只采到 1 条的那条主贴，折叠就是
    // 「查看更多回答」—— 模式里没有它，按钮从来没被点过（英文两条是照同一叫法补的，未核实）
    commentFoldText: [
        '查看更多回答', '查看之前的回答', '查看全部回答', '更多回答',
        'View more answers', 'View previous answers', 'View all answers',
        '查看更多评论', '查看之前的评论', '查看全部评论', '更多评论',
        'View more comments', 'View previous comments', 'View all comments',
        'Meer reacties weergeven', 'Eerdere reacties weergeven', 'Alle reacties weergeven',
        '查看(全部)?\\s*\\d+\\s*条回复', '查看更多回复', '(.+[·•]\\s*)?\\d+\\s*条回复',
        'View\\s*(all\\s*|more\\s*|previous\\s*)?(\\d+\\s*)?(more\\s*)?repl(y|ies)',
        '(.+[·•]\\s*)?\\d+\\s*repl(y|ies)',
        '(Alle\\s*)?\\d+\\s*antwoord(en)?\\s*bekijken', 'Meer antwoorden weergeven',
        '(.+[·•]\\s*)?\\d+\\s*antwoord(en)?',
    ].join('|'),
    // 大小写不敏感：英文界面是「2 Replies」「View More Replies」这类大写开头的写法。
    // 和上面的模式放在一起，expandComments() 与回归测试都从这里取，别各写一份
    commentFoldFlags: 'i',
    // 正文图。2026-08-04 对真实小组页实测：
    //   - 正文图是 <img>，host 为 scontent-*.xx.fbcdn.net，渲染尺寸 367×795 这个量级
    //   - 界面图标是 data:image/svg+xml（16~18px），emoji 在 static.xx.fbcdn.net，
    //     两者都不在 scontent 上，按 host 一刀就切干净
    //   - **头像不是 img 而是 <svg><image>**，压根不会被 querySelectorAll('img') 选中
    // 尺寸下限是第二道保险，防的是将来冒出小尺寸的 scontent 图标
    image: 'img',
    imageHost: 'scontent',
    imageMinSize: 100,
    // 帖子浮层（2026-09-13 真站实测）：点「查看更多回答 / 评论」= pushState 到
    // /groups/<gid>/permalink/<id>/ + 弹 [role=dialog]；浮层里是主贴 article，评论与回复全是
    // 它下面的**兄弟节点**，层级只看得出缩进和链接（算法见 extractInPage 的 threadOf）
    dialog: '[role="dialog"]',
    // 卡片 / 浮层上的评论数按钮：文字是纯数字（**含回复的回复**），真站 aria-label「发表评论」。
    // 只有中文在真站核实过，英文 / 荷兰语是照常见译法写的
    commentCountLabel: '评论|回答|comment|answer|reactie',
    // 浮层的关闭按钮没有文字，只能按 aria-label 认（真站是「关闭」）
    closeLabel: '关闭|Close|Sluiten',
    // 评论排序按钮的文字，**只记日志、绝不点**：菜单里写着「评论排序方式将应用于 Facebook」，
    // 是账号级设置。「最相关」会藏掉疑似垃圾评论，这类帖子会对不上原帖评论数
    sortText: '最相关|由新到旧|所有评论|Most relevant|Newest|All comments|Meest relevant|Nieuwste|Alle reacties',
};

// 时间链接的标记属性 + 按标记缓存的 tooltip 文本。信息流是往下追加，上一批的帖子
// 每一批都会被重新提取一次，不缓存就要把同一条帖子 hover 十遍。
const TIME_MARK = 'data-hyxi-t';
const timeCache = new Map();

function groupUrl() {
    return `${CONFIG.baseUrl}/groups/${CONFIG.groupId}`;
}

function loginUrl() {
    return `${CONFIG.baseUrl}/login`;
}

/**
 * 统一成落盘格式 dd-mm-yyyy HH:MM。
 *
 * 解析不出绝对时间就返回空串，**绝不原样保留**：timestamp 是指纹的一部分，
 * 把「6天」这类会随天数变化的文本写进去，第二天同一条帖子就变成新帖 ——
 * 全部历史数据失配，已翻译的重新付费翻译，舆情重复计数。
 */
function normalizeTime(raw) {
    const s = (raw || '').trim();
    if (!s) return '';
    const p = (n) => String(n).padStart(2, '0');
    // 中文界面的 tooltip：2026年7月28日周二19:53
    const cn = s.match(/(\d{4})年(\d{1,2})月(\d{1,2})日\D*?(\d{1,2}):(\d{2})/);
    if (cn) return `${p(cn[3])}-${p(cn[2])}-${cn[1]} ${p(cn[4])}:${cn[5]}`;
    const asEpoch = Number(s);
    const d = Number.isFinite(asEpoch) && asEpoch > 1e9 ? new Date(asEpoch * 1000) : new Date(s);
    if (isNaN(d.getTime())) return '';
    return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/**
 * hover 时间链接读 tooltip —— 这是页面上唯一一处绝对且本地的时间。
 *
 * 主贴头部链接的 aria-label 是相对时间（「6天」），明天再抓就是「7天」，进指纹即失配；
 * 评论的 aria-label 是绝对时间，但用的是 **Facebook 账号自己的时区**（实测比宿主机
 * 早 15 小时 = PDT vs Asia/Shanghai），和主贴根本对不上。tooltip 两者都给本地绝对时间。
 */
async function hoverTime(page, key, scope = '') {
    try {
        // scope 用来限定在帖子浮层里找：浮层盖着信息流，同一条评论在后面卡片上的那个链接
        // 被挡住、hover 不到，而 page.$ 按文档顺序会先找到它
        const el = await page.$(`${scope}[${TIME_MARK}="${key}"]`);
        if (!el) return '';
        // 上一个 tooltip 不消失，读到的就分不清是谁的
        await page.mouse.move(2, 2);
        await page.waitForFunction(
            () => !document.querySelector('[role="tooltip"]'), null, { timeout: 1200 });
        await el.hover({ timeout: 5000 });
        const tip = await page.waitForFunction(() => {
            const t = document.querySelector('[role="tooltip"]');
            return t ? t.textContent.trim() : null;
        }, null, { timeout: 2000 });
        return String(await tip.jsonValue());
    } catch (e) {
        // 页面/浏览器没了是真故障，要往外抛让这一轮判为残缺（退出码 2）。吞掉的话
        // 剩下的帖子会一路拿到空时间，最后写出一份 complete: true 的、时间全空的结果
        if (/has been closed|target closed|crashed/i.test(e.message)) throw e;
        return '';
    }
}

/**
 * 提取前先把折叠的正文点开。
 *
 * Facebook 对长帖只渲染前几行，末尾挂一个 role=button 的「展开」。不点它，
 * textContent 拿到的就是残缺正文 —— 真站实测有一条整条正文只剩 16 个字符
 * （`Goedemiddag,… 展开`），点开后是 208 个。翻译和舆情都建立在正文上，残文比
 * 没有更糟：它看起来是完整的一句话。
 *
 * 已展开的帖子按钮文字变成「收起」，不会再被匹配到，所以信息流每批重新提取时
 * 不会重复点击。
 */
async function expandBodies(page) {
    const clicked = await page.evaluate((sel) => {
        const re = new RegExp(`^(${sel.expandText})$`);
        const btns = [...document.querySelectorAll(`${sel.post} [role="button"]`)]
            .filter((el) => re.test(el.textContent.trim()));
        btns.forEach((b) => b.click());
        return btns.length;
    }, SELECTORS);
    if (!clicked) return;
    try {
        // 等正文真的换掉再提取。等不到就往下走并说出来 —— 那一批正文会截断，
        // 而截断的正文和完整的正文是两个指纹，闷着不说会变成重复数据
        await page.waitForFunction((sel) => {
            const re = new RegExp(`^(${sel.expandText})$`);
            return ![...document.querySelectorAll(`${sel.post} [role="button"]`)]
                .some((el) => re.test(el.textContent.trim()));
        }, SELECTORS, { timeout: 5000 });
    } catch (e) {
        if (/has been closed|target closed|crashed/i.test(e.message)) throw e;
        log('   ⚠️ 有正文没能展开，这一批可能存在截断');
    }
}

// 评论折叠的展开轮次上限。**一轮只展开一条主贴**（见 expandComments），评论又是分页
// 加载的，一屏十来条主贴、热帖要翻好几页，所以给到 30；但也不能无上限，否则一条几百条
// 评论的帖子会把整批时间耗光。撞上限要打日志
const COMMENT_FOLD_ROUNDS = 30;
// 两轮之间的间隔。点「查看更多评论」是页面内的轻动作，不是翻页，所以比 pacing 短 ——
// 一条一条地展开，用翻页那种 4~11 秒的话一批就要好几分钟。仍然随机、仍然不为零
const FOLD_CLICK_DELAY_MIN = 800;
const FOLD_CLICK_DELAY_MAX = 2000;
// 以下几份状态**整轮共用，不能每批重建**：信息流是往下追加的，上一批的帖子每一批都会被
// 重新提取一遍。重建的话同一条帖子每批再开一次浮层，访问量翻倍。
// 本轮在浮层里收割到的主贴：id → { comments, siteCount, scan }。卡片上只露一两条，提取时拿它顶替
const harvested = new Map();
// 点了折叠却没收割成的主贴（整页跳走 / 浮层地址对不上 / 等不到浮层），本轮不再点 ——
// v1.11.4 那种「每批点一次、每次重载信息流」会把滚动加载出来的后续内容一起冲掉。
// 原帖评论数对不上的，信息流滚完后按固定链接补（completeFromPermalinks）
const failedPosts = new Set();
// 每条主贴最近一次提取时的原帖评论数与已有条数，信息流滚完后据此挑出要补的
const cardInfo = new Map();
// 主贴 message_id → { site_comment_count, harvested_site_count }，随输出交给 storage.record_thread_counts()。
// **老主贴见过就不再作为帖子输出**，而这两个数每轮都可能变：原帖数不刷新，结果页的「原帖 Y」就停在
// 第一次采到时；整串读完时的原帖数不记，条数永远对不上的主贴每轮增量都要再开一遍
const threadCounts = new Map();
// 信息流的地址（进小组页后实际落到的那个），关浮层 / 退回信息流时对照用
let feedUrl = '';
let deadlineHit = false;

// 「在不在信息流上」只比路径、去掉末尾斜杠：关浮层后恢复的地址可能多一个斜杠或带查询参数，
// 严格相等会被判成「还没关掉」，一路退到后退、重新载入，白丢滚动加载出来的内容
const pathOf = (url) => {
    try {
        return new URL(url).pathname.replace(/\/+$/, '');
    } catch (e) {
        return '';
    }
};
const onFeed = (page) => pathOf(page.url()) === pathOf(feedUrl);

const DEADLINE_REASON = '接近任务时限，已提前收尾：本轮只交出了已采到的部分'
    + '（全量重跑可调大 TWEAKERS_TASK_TIMEOUT_MINUTES 后重试）';

function pastDeadline() {
    if (CONFIG.deadlineAt && Date.now() >= CONFIG.deadlineAt) deadlineHit = true;
    return deadlineHit;
}

const isGone = (e) => /has been closed|target closed|crashed/i.test(e.message);

/** 记下一条主贴的两个数。null 是「这回没读到」，不冲掉本轮先前读到的 */
function noteThread(id, site, harvestedAt) {
    const prev = threadCounts.get(id) || { site_comment_count: null, harvested_site_count: null };
    threadCounts.set(id, {
        site_comment_count: site ?? prev.site_comment_count,
        harvested_site_count: harvestedAt ?? prev.harvested_site_count,
    });
}

/** 页面上有没有开着的帖子浮层（在页面里执行，不许引用外面的变量） */
function threadDialogOpen(sel) {
    return [...document.querySelectorAll(sel.dialog)]
        .some((d) => d.querySelector(sel.post) && d.getClientRects().length > 0);
}

async function waitForThreadDialog(page, timeout) {
    try {
        await page.waitForFunction(threadDialogOpen, SELECTORS, { timeout });
        return true;
    } catch (e) {
        if (isGone(e)) throw e;
        return false;
    }
}

/**
 * 点完一个折叠之后发生了什么：'dialog' 弹出了帖子浮层 / 'moved' 页面被带离了信息流 /
 * 'grew' 就地多出了评论 / 'stale' 什么都没变。target 是 { id, nested: 点之前这条主贴里的 article 数 }。
 *
 * 点完不检查的后果不是「少采几条」而是**整轮采集报废**（v1.11.0~v1.11.3）：留在浮层上，
 * 这一批只提取得到那一条帖子，信息流也滚不动，`scrollOnce()` 判「已到底」。
 * 「就地多出了评论」**只数这条主贴自己的**：数整页的话，信息流恰好懒加载进一条新帖就算成展开了，
 * 同一个没用的折叠被反复点到轮次上限，这一批别的主贴轮不到
 */
async function afterFoldClick(page, target) {
    let state = 'stale';
    try {
        const handle = await page.waitForFunction(([sel, id, n, path]) => {
            if ([...document.querySelectorAll(sel.dialog)]
                .some((d) => d.querySelector(sel.post) && d.getClientRects().length > 0)) return 'dialog';
            if (location.pathname.replace(/\/+$/, '') !== path) return 'moved';
            const link = [...document.querySelectorAll(sel.postTime)].find((a) => !a.closest(sel.dialog)
                && ((a.getAttribute('href') || '').match(/\/posts\/([^/?#]+)/) || [])[1] === id);
            const art = link && link.closest(sel.post);
            return art && art.querySelectorAll(sel.post).length > n ? 'grew' : false;
        }, [SELECTORS, target.id, target.nested, pathOf(feedUrl)], { timeout: 5000 });
        state = await handle.jsonValue();
    } catch (e) {
        if (isGone(e)) throw e;
        if (!onFeed(page)) state = 'moved';
    }
    if (state !== 'moved') return state;
    // 真站是先换地址（pushState 到 /permalink/<id>/）、浮层内容随后才由 XHR 取回来，再等它一会儿。
    // 整页跳到了别处的不必多等
    const wait = /\/permalink\//.test(page.url()) ? 10000 : 1500;
    return (await waitForThreadDialog(page, wait)) ? 'dialog' : 'moved';
}

/** 被整页带离了信息流：先试后退（通常保得住滚动加载出来的内容），回不去才重新载入 */
async function recoverToFeed(page) {
    log('   ⚠️ 折叠按钮把页面带离了信息流，已退回，这条主贴本轮不再点');
    try {
        await page.goBack({ waitUntil: 'domcontentloaded', timeout: CONFIG.timeout });
    } catch (e) {
        if (isGone(e)) throw e;
    }
    if (!onFeed(page)) await gotoPage(page, feedUrl, CONFIG.timeout, CONFIG.deadlineAt);
}

/**
 * 关掉帖子浮层、回到信息流，逐级退：关闭按钮 → Esc → 后退 → 重新载入信息流。
 *
 * 前三步都保得住滚动加载出来的内容（真站实测点「关闭」后 URL 回到信息流、卡片还在、没有
 * 重载）；重新载入会丢，所以放在最后并在日志里说明。**绝不能留在浮层上**：见 afterFoldClick。
 */
async function closeThreadDialog(page) {
    const back = async (ms) => {
        try {
            await page.waitForFunction(([sel, path]) => location.pathname.replace(/\/+$/, '') === path
                && ![...document.querySelectorAll(sel.dialog)]
                    .some((d) => d.querySelector(sel.post) && d.getClientRects().length > 0),
            [SELECTORS, pathOf(feedUrl)], { timeout: ms });
            return true;
        } catch (e) {
            if (isGone(e)) throw e;
            return false;
        }
    };
    const clicked = await page.evaluate((sel) => {
        const re = new RegExp(`^(${sel.closeLabel})$`, 'i');
        // 要求可见：关掉后没拿走、只是藏起来的旧浮层排在文档前面，点它的关闭按钮什么都不会发生
        const dlg = [...document.querySelectorAll(sel.dialog)]
            .find((d) => d.querySelector(sel.post) && d.getClientRects().length > 0);
        const btn = dlg && [...dlg.querySelectorAll('[aria-label]')]
            .find((b) => re.test(b.getAttribute('aria-label')));
        if (btn) btn.click();
        return !!btn;
    }, SELECTORS);
    if (clicked && await back(3000)) return;
    await page.keyboard.press('Escape');
    if (await back(2000)) return;
    // 地址没变过的（浮层没有 pushState）不能后退：那会退出小组页
    if (!onFeed(page)) {
        try {
            await page.goBack({ waitUntil: 'domcontentloaded', timeout: CONFIG.timeout });
        } catch (e) {
            if (isGone(e)) throw e;
        }
        if (await back(2000)) return;
    }
    log('   ⚠️ 帖子浮层用关闭按钮 / Esc / 后退都关不掉，已重新载入信息流（本批已展开的折叠要重新点）');
    await gotoPage(page, feedUrl, CONFIG.timeout, CONFIG.deadlineAt);
}

/**
 * 浮层里自己还有折叠（「查看更多回复」之类），同样一次点一个、点到不再增长。
 * 返回 { clicks: 点了几处, done: 是否点到一个折叠都不剩 } —— 到了时限、撞了轮次上限、点了没多出来
 * 都不算读完：读完的主贴原帖数不涨就不再打开，算错了差的那几条就再也补不回来
 */
async function expandDialogFolds(page) {
    let clicks = 0;
    for (let round = 1; round <= COMMENT_FOLD_ROUNDS; round++) {
        if (pastDeadline()) return { clicks, done: false };
        const before = await page.evaluate((sel) => {
            const re = new RegExp(`^(${sel.commentFoldText})$`, sel.commentFoldFlags);
            // 要求可见，理由同 closeThreadDialog：点到藏着的旧浮层上，开着的这个会被当成折叠已经点完
            const dlg = [...document.querySelectorAll(sel.dialog)]
                .find((d) => d.querySelector(sel.post) && d.getClientRects().length > 0);
            const btn = dlg && [...dlg.querySelectorAll('[role="button"]')]
                .find((el) => re.test(el.textContent.replace(/\s+/g, ' ').trim()));
            if (!btn) return null;
            const n = dlg.querySelectorAll(sel.post).length;
            btn.click();
            return n;
        }, SELECTORS);
        if (before === null) return { clicks, done: true };
        clicks += 1;
        try {
            await page.waitForFunction(([sel, n]) => {
                const dlg = [...document.querySelectorAll(sel.dialog)]
                    .find((d) => d.querySelector(sel.post) && d.getClientRects().length > 0);
                return !!dlg && dlg.querySelectorAll(sel.post).length > n;
            }, [SELECTORS, before], { timeout: 5000 });
        } catch (e) {
            if (isGone(e)) throw e;
            // 点了没多出来：到底了，或者那个按钮压根不是「加载更多」（后面的折叠也就没点）
            return { clicks, done: false };
        }
        await humanDelay(FOLD_CLICK_DELAY_MIN, FOLD_CLICK_DELAY_MAX);
    }
    log(`   ⚠️ 浮层里的折叠展开了 ${COMMENT_FOLD_ROUNDS} 轮仍未见底，这条主贴的回复可能不全`);
    return { clicks, done: false };
}

/**
 * hover 取时间，按标记缓存。scope 见 hoverTime。
 *
 * **过了时限就不再 hover**：一条最多要等好几秒，几百条回复的一串能把收尾余量整个耗光，
 * 然后被 runner 按超时杀掉、交接文件没写，整轮一条不剩（D5 要防的正是这个）。
 * 没取到时间的照样输出：下一轮取到时间后指纹会变，入库时按 message_id 归并回同一行
 */
async function resolveTimes(page, items, scope = '') {
    for (const item of items) {
        if (item.timeKey && !timeCache.has(item.timeKey) && !pastDeadline()) {
            timeCache.set(item.timeKey, await hoverTime(page, item.timeKey, scope));
        }
        item.rawTime = timeCache.get(item.timeKey) || '';
    }
}

// 浮层里评论条数要稳定这么久才算加载完，最多等这么久
const THREAD_SETTLE_MS = 1500;
const THREAD_SETTLE_MAX_MS = 10000;

/**
 * 等浮层里的评论加载完：article 条数（主贴 + 评论）至少 2 条、且稳定 THREAD_SETTLE_MS 不再变。
 * 返回 { settled, n }，n 是最后看到的条数（1 = 浮层里只有主贴）。
 *
 * 浮层内容是 XHR 取回来的（2026-09-13 真站实测先换地址、内容后到），主贴和评论不一定同时到。
 * 一出现主贴就收割的话，折叠一个都还没渲染出来，这一串会被记成「整串读完」—— 原帖数不涨就再也
 * 不打开；收到的空串还会把卡片上露出来的那几条顶掉。
 */
async function waitForThreadSettled(page) {
    const count = () => page.evaluate((sel) => {
        const dlg = [...document.querySelectorAll(sel.dialog)]
            .find((d) => d.querySelector(sel.post) && d.getClientRects().length > 0);
        return dlg ? dlg.querySelectorAll(sel.post).length : 0;
    }, SELECTORS);
    const until = Date.now() + THREAD_SETTLE_MAX_MS;
    let last = await count();
    let since = Date.now();
    while (Date.now() < until) {
        await page.waitForTimeout(300);
        const n = await count();
        if (n !== last) {
            last = n;
            since = Date.now();
        } else if (n > 1 && Date.now() - since >= THREAD_SETTLE_MS) {
            return { settled: true, n };
        }
    }
    return { settled: false, n: last };
}

/**
 * 在开着的帖子浮层里展开、提取、取时间，返回 { comments, siteCount, scan, complete }；收不成返回 null。
 * cardHave：卡片上露出来几条评论。complete：浮层里的折叠点到一个不剩、每条评论的时间都取到了 ——
 * 只有这样才算「整串读完」（见 noteThread）：读完的主贴原帖数不涨就不再打开，差的、没取到时间的都补不回来。
 *
 * **浮层地址不是这条主贴的就不收**：把别的帖子的评论挂到这条下面，比少采几条糟得多
 * （用户报过「主贴 A 的回复贴不是他的」）。地址里的 id 与卡片 /posts/<id>/ 是同一串，
 * 「🔗 原帖」链接就是这么拼的，用户那份数据核对过。
 */
async function harvestThread(page, id, cardHave) {
    // 按路径段比：真站浮层地址有的带末尾斜杠、有的不带（2026-09-15 实测 /permalink/2476737042802015），
    // 拿 `/${id}/` 去 includes 会把这条主贴自己的浮层判成对不上，白开一次、再整页补
    if (!new URL(page.url()).pathname.split('/').includes(id)) {
        log(`   ⚠️ 浮层的地址对不上主贴 ${id}（${page.url()}），这一串不收`);
        return null;
    }
    const { settled, n } = await waitForThreadSettled(page);
    // 等到头浮层里还是只有主贴：卡片上也一条都没露的，是原帖确实看不到评论（删了没减数、被「最相关」
    // 藏掉），照常收一个空串、记成读完 —— 否则这类主贴每轮增量都要再开一遍、再白等一次。
    // 卡片上露过评论的，就是评论还没加载出来
    if (!settled && !(n === 1 && cardHave === 0)) {
        log(`   ⚠️ 主贴 ${id} 的浮层里等了 ${THREAD_SETTLE_MAX_MS / 1000} 秒没等到评论加载出来，这一串不收`);
        return null;
    }
    const { clicks: folds, done } = await expandDialogFolds(page);
    await expandBodies(page);
    const got = await page.evaluate(extractInPage, { sel: SELECTORS, scope: 'dialog' });
    const root = got.posts[0];
    if (!root) return null;
    await resolveTimes(page, root.comments, `${SELECTORS.dialog} `);
    log(`   主贴 ${id}：浮层里 ${root.comments.length} 条评论与回复（原帖 ${root.siteCount ?? '读不到'}`
        + (got.sort ? `，排序「${got.sort}」` : '') + (folds ? `，点开折叠 ${folds} 处` : '') + '）'
        + (got.mismatches ? `；层级与链接不一致 ${got.mismatches} 处，已按链接改挂（链接指向的顶层评论没采到的挂主贴）` : ''));
    // 时间没取到的（过了时限没 hover、tooltip 没出来）不算读完
    const timed = root.comments.every((c) => !c.timeKey || c.rawTime);
    return { comments: root.comments, siteCount: root.siteCount, scan: got.scan, complete: done && timed };
}

/**
 * 提取前把评论区的折叠点开 —— 和 expandBodies() 点的**正文**折叠是两回事。
 *
 * Facebook 首屏每条主贴只渲染前两三条评论，其余藏在「查看更多评论」后面，
 * 一条评论底下的嵌套回复另有一个「查看 N 条回复」。不点它们，采到的就只是首屏
 * 那几条：真实库实测 79 条主贴的回复数分布 {1条:17, 2条:30, 3条:5}，
 * **上限死死卡在 3、一条都没超过** —— 那不是自然分布，是首屏渲染上限的指纹
 * （用户报「只采到 3 条、实际应有 4 条」）。回复采不全，舆情就少统计一份声音，
 * 而页面上完全看不出少了什么。
 *
 * 评论**分页加载**，点一次只多出一页，所以要循环点到不再增长；每轮之间照常
 * humanDelay —— 连着几十次点「加载更多」在行为分析里比翻页还扎眼。
 * 撞上轮次上限**必须说出来**：静默停在半路等于又一次少采，而这回连日志都不提。
 *
 * **真站上多数折叠点下去是弹帖子浮层，不是就地展开**（2026-09-13 实测）。v1.11.4 把它当事故
 * 退回来并整轮拉黑，这类主贴的隐藏评论于是任何一轮都采不到。现在就地收割：在浮层里展开、
 * 提取、关掉，卡片提取时拿收割到的整串顶替。返回 { folds: 点了几处, scans: 浮层里的图片扫描 }。
 */
async function expandComments(page) {
    const stale = [];       // 本批点了却没多出评论的主贴：这一批到底了，下一批可能又有新的
    const scans = [];
    let total = 0;
    let exhausted = true;   // 是否是撞了轮次上限才停的
    for (let round = 1; round <= COMMENT_FOLD_ROUNDS; round++) {
        if (pastDeadline()) {
            exhausted = false;
            break;
        }
        // **一次只点一个折叠**。一次点一整批的话，万一其中一个把页面带走了，根本不知道是
        // 哪一个；两个都弹浮层就更说不清谁是谁的评论。
        // 原帖评论数读得到时，已经采够的主贴不点（增量运行时老帖不必每轮再开一遍浮层）；
        // 读不到数就退回「有折叠就点」
        const target = await page.evaluate(([sel, skip, known]) => {
            const re = new RegExp(`^(${sel.commentFoldText})$`, sel.commentFoldFlags);
            const countRe = new RegExp(sel.commentCountLabel, 'i');
            const txt = (el) => el.textContent.replace(/\s+/g, ' ').trim();
            const roots = [...document.querySelectorAll(sel.post)]
                .filter((a) => !a.parentElement.closest(sel.post) && !a.closest(sel.dialog));
            for (const art of roots) {
                const link = art.querySelector(sel.postTime);
                const m = link && (link.getAttribute('href') || '').match(/\/posts\/([^/?#]+)/);
                const id = m ? m[1] : '';
                if (!id || skip.includes(id)) continue;
                const buttons = [...art.querySelectorAll('[role="button"]')];
                const counter = buttons.find((b) => b.closest(sel.post) === art
                    && /^\d+$/.test(txt(b)) && countRe.test(b.getAttribute('aria-label') || ''));
                const siteCount = counter ? Number(txt(counter)) : null;
                const nested = art.querySelectorAll(sel.post).length;
                if (siteCount !== null && siteCount <= Math.max(nested, known[id] || 0)) continue;
                const fold = buttons.find((b) => re.test(txt(b)));
                if (!fold) continue;
                fold.click();
                return { id, nested };
            }
            return null;
        }, [SELECTORS, [...harvested.keys(), ...failedPosts, ...stale], CONFIG.knownCommentCounts]);
        if (!target) {
            exhausted = false;
            break;
        }
        total += 1;
        const state = await afterFoldClick(page, target);
        if (state === 'dialog') {
            // 收割到一半页面调用出错（浮层里点了什么触发整页跳转之类）只放弃这一串、关掉浮层接着采，
            // 原帖数对不上的留给固定链接补。冒到 main 的话首轮一条都还没交出去就整轮硬失败，
            // 而这条主贴每轮都会再点、再失败
            let got = null;
            try {
                got = await harvestThread(page, target.id, target.nested);
            } catch (e) {
                if (isGone(e)) throw e;
                log(`   ⚠️ 主贴 ${target.id} 的浮层收割出错（${String(e.message).split('\n')[0]}），这一串不收`);
            }
            if (got) {
                harvested.set(target.id, got);
                scans.push(got.scan);
            } else {
                failedPosts.add(target.id);
            }
            await closeThreadDialog(page);
        } else if (state === 'moved') {
            failedPosts.add(target.id);
            await recoverToFeed(page);
        } else if (state === 'stale') {
            // 点了却没变多：这条主贴的折叠已经到底（或那个按钮压根不是「加载更多」），换下一条
            stale.push(target.id);
        }
        // 点「查看更多评论」是页面内的轻动作，不是翻页，所以用比 pacing 短的间隔 ——
        // 一条帖子一条帖子地展开，用 4~11 秒的话一批就要好几分钟
        await humanDelay(FOLD_CLICK_DELAY_MIN, FOLD_CLICK_DELAY_MAX);
    }
    if (exhausted) {
        log(`   ⚠️ 评论折叠展开了 ${COMMENT_FOLD_ROUNDS} 轮仍未见底，这一批的回复可能不全`);
    }
    return { folds: total, scans };
}

/**
 * 提取主贴与它的评论。scope='feed' 取信息流，scope='dialog' 取开着的帖子浮层里那一条。
 *
 * **在页面里执行**（page.evaluate 按源码序列化过去），不许引用外面的任何变量。卡片和浮层
 * 共用这一份：两处 DOM 形态不同，层级算法两种都认，分成两份迟早一边改了另一边没改。
 *
 * 每条评论带 parent：它回复的那条在 comments 里的下标，-1 是回复主贴。
 */
function extractInPage({ sel, scope }) {
    const scan = { candidates: [], accepted: [], rejected: [] };
    const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
    // 正文要额外剥掉末尾的展开/收起按钮文字，用户名等字段不需要
    const stripTrail = (s) => s.replace(new RegExp(sel.bodyTrail), '');
    const bodyText = (el) => stripTrail(text(el));
    // 主贴的字段只能在主贴自己这一层找：评论也是 article，嵌在里面
    const own = (root, selector) => [...root.querySelectorAll(selector)]
        .find((el) => el.closest(sel.post) === root) || null;
    // 评论正文是一串**并列**的 div[dir=auto]，一段一个 —— 取第一个就只剩第一段。
    // 实测一条 9 段的评论只存下第一段的 71 个字符，原文 811 个。
    // 层级限定不能省：嵌套回复也是 article，不限定就会把子回复的正文并进父评论。
    const commentText = (root) => stripTrail(
        [...root.querySelectorAll(sel.commentBody)]
            .filter((el) => el.closest(sel.post) === root)
            .map(text).filter(Boolean).join('\n'));
    // 作者：取第一个**有文字**的个人主页链接，前面那几个是同一个人的头像链接
    const nameOf = (root, scoped) => text([...root.querySelectorAll(sel.author)].find(
        (el) => (!scoped || el.closest(sel.post) === root) && el.textContent.trim()));
    // 正文图 URL。层级限定同 own()：评论的图不能算到主贴头上。
    // **每一步排除都要记账**：候选数和排除原因不报出来，「页面上就没有图」
    // 「host 对不上」「被尺寸门限挡了」在日志上完全没有区别，远端只能靠猜
    const imagesOf = (root) => {
        const urls = [];
        [...root.querySelectorAll(sel.image)].forEach((im) => {
            if (im.closest(sel.post) !== root) return;
            const url = im.currentSrc || im.src || '';
            // data: 是界面图标（实测 16~18px 的 svg），不算候选，免得刷屏
            if (!url || url.startsWith('data:')) return;
            const r = im.getBoundingClientRect();
            const rej = (why) => scan.rejected.push({
                why, url, w: Math.round(r.width), h: Math.round(r.height),
            });
            scan.candidates.push(url);
            if (!url.includes(sel.imageHost)) return rej('host');
            if (r.width < sel.imageMinSize || r.height < sel.imageMinSize) return rej('尺寸');
            scan.accepted.push(url);
            urls.push(url);
        });
        return urls;
    };
    // 回复的固定链接是 ?comment_id=<所属顶层评论>&reply_comment_id=<自己>（2026-09-13 真站
    // 核实，不论第几层都是这个形态）。只认 comment_id 就拿到父评论的 id ——
    // message_id 撞车，入库时按 id 归并，**父评论那一行整条被回复覆盖**（作者、正文都
    // 换成回复的），回复自己的指纹进了别名表、永远不会再下发；时间锚点的标记也由 id
    // 派生，回复连时间都继承了父评论的。所以先认 reply_comment_id；comment_id 要求
    // 前面是 ? 或 &，免得参数顺序反过来时从「reply_comment_id=」里半截匹配出来
    const idOf = (link, kind) => {
        const href = link ? link.getAttribute('href') : '';
        const m = href && (kind === 'comment'
            ? (href.match(/[?&]reply_comment_id=([^&#]+)/)
                || href.match(/[?&]comment_id=([^&#]+)/))
            : href.match(/\/posts\/([^/?#]+)/));
        return m ? m[1] : '';
    };
    // 给时间链接打标记，随后在页面外逐个 hover。已打过的不重打 ——
    // 滚动是往下追加，上一批的元素还在 DOM 里，重打会让缓存全部落空
    window.__hyxiSeq = window.__hyxiSeq || 0;
    const mark = (el, id) => {
        if (!el) return '';
        const existing = el.getAttribute('data-hyxi-t');
        if (existing) return existing;
        const key = id.replace(/[^A-Za-z0-9_]/g, '') || `n${++window.__hyxiSeq}`;
        el.setAttribute('data-hyxi-t', key);
        return key;
    };

    // 原帖评论数：主贴自己这一层的按钮，文字是纯数字、aria-label 是「发表评论」一类
    const countOf = (root) => {
        const re = new RegExp(sel.commentCountLabel, 'i');
        const btn = [...root.querySelectorAll('[role="button"]')].find((b) => b.closest(sel.post) === root
            && /^\d+$/.test(text(b)) && re.test(b.getAttribute('aria-label') || ''));
        return btn ? Number(text(btn)) : null;
    };
    // 评论的层级。2026-09-13 真站实测：浮层里回复**不嵌在**父评论的 article 里，全是主贴 article
    // 下的兄弟节点，层级只体现在缩进上（418 / 479 / 521px）；链接上评论是 comment_id=<自己>，
    // 回复不论第几层都是 comment_id=<所属顶层评论>&reply_comment_id=<自己>。所以：
    //   ① DOM 上嵌在某条评论 article 里的，直接认那一条（卡片上是这种形态）
    //   ② 否则是回复的，认文档顺序里前面最近一条**缩进更小**的
    //   ③ 最后拿 comment_id 校验：顺着父指针走到头的那一条，它所属的顶层评论（评论是它自己，回复看它
    //      链接上的 comment_id）要和链接一致。对不上以链接为准挂到那条顶层评论下，并计数；**那条没采到**
    //      （折叠没点完、被「最相关」藏掉）**就挂主贴** —— 按缩进猜的那条已知不是它回复的，挂上去就是
    //      「回复挂错人」。比所属顶层评论而不比 id：挂了主贴的回复，它自己的回复链接上也指着那条没采到的，
    //      按 id 比会跟着被判不一致、一起拍平
    // aria-label（「X回复了Y的回复」）写得最明白，但随账号界面语言变，不当判据
    let mismatches = 0;
    const threadOf = (root) => {
        const list = [];
        const stack = [];
        [...root.querySelectorAll(sel.post)].forEach((c) => {
            const link = own(c, sel.commentTime);
            const href = link ? link.getAttribute('href') || '' : '';
            const isReply = /[?&]reply_comment_id=/.test(href);
            const id = idOf(link, 'comment');
            const top = isReply ? ((href.match(/[?&]comment_id=([^&#]+)/) || [])[1] || '') : id;
            const entry = { el: c, link, id, top, left: c.getBoundingClientRect().left, parent: -1 };
            while (stack.length && stack[stack.length - 1].left >= entry.left) stack.pop();
            const host = c.parentElement.closest(sel.post);
            if (host !== root) {
                entry.parent = list.findIndex((e) => e.el === host);
            } else if (isReply && stack.length) {
                entry.parent = list.indexOf(stack[stack.length - 1]);
            }
            if (isReply && top) {
                let head = entry.parent;
                while (head >= 0 && list[head].parent >= 0) head = list[head].parent;
                if (head < 0 || list[head].top !== top) {
                    mismatches += 1;
                    entry.parent = list.findIndex((e) => e.parent === -1 && e.id === top);
                }
            }
            list.push(entry);
            stack.push(entry);
        });
        return list.map((e) => ({
            username: nameOf(e.el, false),
            timeKey: mark(e.link, e.id && `c${e.id}`),
            content: commentText(e.el),
            message_id: e.id,
            imageUrls: imagesOf(e.el),
            parent: e.parent,
        }));
    };

    let roots;
    let sort = '';
    if (scope === 'dialog') {
        const dlg = [...document.querySelectorAll(sel.dialog)]
            .find((d) => d.querySelector(sel.post) && d.getClientRects().length > 0);
        roots = dlg ? [...dlg.querySelectorAll(sel.post)]
            .filter((a) => !a.parentElement.closest(sel.post)).slice(0, 1) : [];
        const sortRe = new RegExp(`^(${sel.sortText})$`);
        const sorter = dlg && [...dlg.querySelectorAll('[role="button"]')].find((b) => sortRe.test(text(b)));
        sort = sorter ? text(sorter) : '';
    } else {
        // 只取顶层 article（评论也是 article，嵌在主贴里面），而且**不要浮层里的**：浮层里的
        // 主贴头部没有 /posts/ 链接，浮层开着时提取，它就被存成一条没 id、没时间的新主贴
        roots = [...document.querySelectorAll(sel.post)]
            .filter((a) => !a.parentElement.closest(sel.post) && !a.closest(sel.dialog));
    }
    const out = roots.map((article) => {
        const comments = threadOf(article);
        const link = own(article, sel.postTime);
        const id = idOf(link, 'post');
        return {
            username: nameOf(article, true),
            timeKey: mark(link, id && `p${id}`),
            content: bodyText(own(article, sel.body)),
            message_id: id,
            imageUrls: imagesOf(article),
            siteCount: countOf(article),
            comments,
        };
    });
    return { posts: out, scan, mismatches, sort };
}

async function extractBatch(page) {
    // **先点评论折叠，再点正文「展开」，顺序不能反**：被「查看更多评论」加载出来的长评论，
    // 它自己的正文也折叠着。反过来的话，点正文「展开」那一轮它还不在 DOM 里，等它出现
    // 本批已经没人去点了，残文剥掉「… 展开」后看起来就是一句完整的话，照样入库。
    // 截断点在 100 字之后时这是永久的：指纹只吃正文前 100 字，截断版和完整版同一个指纹，
    // 下一批就算展开了也会被 seen 当成已见过丢掉（发版前评审用真脚本复现过）
    // 点开了几处折叠要报出来：这是远端唯一能看出「这一轮到底有没有多采到回复」的地方
    const { folds, scans } = await expandComments(page);
    if (folds) log(`   点开评论折叠 ${folds} 处`);
    await expandBodies(page);
    const { posts: raw, scan, mismatches } = await page.evaluate(
        extractInPage, { sel: SELECTORS, scope: 'feed' });
    if (mismatches) log(`   ⚠️ 卡片上层级与链接不一致 ${mismatches} 处，已按链接改挂（链接指向的顶层评论没采到的挂主贴）`);
    for (const post of raw) {
        // 在浮层里收割过的，拿整串（带层级）顶替卡片上露出来的那一两条
        const got = harvested.get(post.message_id);
        if (got) {
            post.comments = got.comments;
            if (post.siteCount === null) post.siteCount = got.siteCount;
        }
        if (post.message_id) {
            cardInfo.set(post.message_id, { siteCount: post.siteCount, have: post.comments.length });
        }
    }
    await resolveTimes(page, raw.flatMap((p) => [p, ...p.comments]));
    return { posts: raw, scans: [scan, ...scans] };
}

/**
 * 信息流里混着不是帖子的 article：广告、推荐小组卡片之类，既没有固定链接也没有正文。
 * 留着会白占一次翻译调用，还会在结果里显示成一条什么都没有的空帖。
 *
 * **只在两者都缺时才丢**：纯图片帖有 id 没正文，正文没渲染出来的帖子有正文没 id，
 * 两种都是真帖子。只有 id 和正文全都没有，才是真的没有任何东西可分析。
 */
function isNotAPost(item) {
    return !item.message_id && !(item.content || '').trim();
}

// 取不到作者时一律填同一个「匿名」，**不要按序号编名字**：信息流每一批都会把上一批的
// 帖子重新提取一遍，序号会跟着变，而 username 进指纹 —— 实测同一条帖子因此在两个批次里
// 拿到两个指纹，一轮抓下来就翻倍。同名不会把两个人混成一条：指纹里还有时间和正文。
function flatten(rawPosts, displayBatch) {
    const flat = [];
    rawPosts.filter((raw) => !isNotAPost(raw)).forEach((raw) => {
        const post = {
            username: raw.username || '匿名',
            timestamp: normalizeTime(raw.rawTime),
            content: raw.content,
            page_number: displayBatch,
            message_id: raw.message_id,
            parent_fingerprint: null,
            reply_level: 0,
            site_comment_count: raw.siteCount,
        };
        post.fingerprint = makeFingerprint(post);
        // 临时字段：saveImages() 落盘后就删，换成本地路径的 images。
        // 指纹只吃 username|timestamp|content[:100]，多挂一个字段不影响它
        post._imageUrls = raw.imageUrls || [];
        flat.push(post);
        flat.push(...flattenComments(post.fingerprint, raw.comments || [], displayBatch));
    });
    return flat;
}

/**
 * 评论按父子关系展开。父指针指向它回复的那一条，层级沿父指针数 —— **不是一律挂主贴、第 1 层**：
 * 那样写了好几个版本，真实库 306 行里第 2 层 0 条，回复的回复全被压成了评论。
 * 「存储层扁平」说的是一张表 + 父指针，不是父指针只能指主贴（build_tree 本来就支持任意深度）。
 */
function flattenComments(rootFingerprint, comments, displayBatch) {
    const made = comments.map((c) => {
        if (isNotAPost(c)) return null;
        const item = {
            username: c.username || '匿名',
            timestamp: normalizeTime(c.rawTime),
            content: c.content,
            page_number: displayBatch,
            message_id: c.message_id,
        };
        item.fingerprint = makeFingerprint(item);
        item._imageUrls = c.imageUrls || [];
        return item;
    });
    const levels = [];
    const out = [];
    comments.forEach((c, i) => {
        // 父贴被当成「不是帖子」丢掉时往上找（父贴总排在前面），一直找不到就挂主贴
        let p = c.parent;
        while (p >= 0 && !made[p]) p = comments[p].parent;
        levels[i] = p >= 0 ? levels[p] + 1 : 1;
        if (!made[i]) return;
        out.push({
            ...made[i],
            parent_fingerprint: p >= 0 ? made[p].fingerprint : rootFingerprint,
            reply_level: levels[i],
        });
    });
    return out;
}

/**
 * 信息流滚完后，按固定链接补齐「原帖评论数对不上、信息流里又没补成」的主贴。
 *
 * 固定链接是整页加载，比点开浮层重得多，所以只当兜底：卡片上没有折叠可点、或点了没弹出浮层的
 * 才走这里。整页打开是**首页信息流上盖一层同样的浮层**（2026-09-13 真站实测），背景里是别的
 * 帖子 —— 只从浮层里取。原帖评论数读不到的不补：没有依据说它不全，不为它多发一次请求。
 *
 * **某一条打不开只跳过这一条**：信息流已经滚完，少的只是这条的几条回复，结果页会把它标黄；
 * 把整轮判成残缺的话任务显示「异常退出」，理由还是一段 Playwright 原始报错。站点拒绝访问
 * （限流）和浏览器没了照样往外抛 —— 前者对方说停就停，后者后面每一条都会一样失败。
 */
async function completeFromPermalinks(page, run) {
    const todo = [...cardInfo.entries()].filter(([id, info]) => !harvested.has(id)
        && info.siteCount !== null
        && info.siteCount > Math.max(info.have, CONFIG.knownCommentCounts[id] || 0));
    if (!todo.length || pastDeadline()) return;
    log(`   信息流里补不齐的主贴 ${todo.length} 条，按固定链接逐条补齐`);
    for (const [id, info] of todo) {
        if (pastDeadline()) return;
        try {
            await humanDelay(CONFIG.delayMin, CONFIG.delayMax);
            await gotoPage(page, `${CONFIG.baseUrl}/groups/${CONFIG.groupId}/permalink/${id}/`,
                CONFIG.timeout, CONFIG.deadlineAt);
            if (!(await waitForThreadDialog(page, 15000))) {
                log(`   ⚠️ 主贴 ${id} 的固定链接页上没有出现帖子浮层，跳过`);
                continue;
            }
            const got = await harvestThread(page, id, info.have);
            if (!got) continue;
            logImageScan(got.scan, run.tally);
            const items = flattenComments(run.rootFingerprints.get(id), got.comments, run.batch);
            // 卡片上露出来的那几条本轮已经输出过了，层级以整串为准改过来：卡片上可能缺了中间那层
            const earlier = new Map(run.fresh.map((p) => [p.fingerprint, p]));
            items.forEach((p) => {
                const prior = earlier.get(p.fingerprint);
                if (prior) Object.assign(prior, { parent_fingerprint: p.parent_fingerprint, reply_level: p.reply_level });
            });
            const added = items.filter((p) => !run.seen.has(p.fingerprint));
            added.forEach((p) => {
                run.seen.add(p.fingerprint);
                run.fresh.push(p);
            });
            await saveImages(run.context, run.capture, added, {
                mediaDir: CONFIG.mediaDir, sourceId: CONFIG.sourceId, tally: run.tally,
            });
            // 放在最后：记成「读完」却没交出去的话，下一轮原帖数不涨就不会再来补
            noteThread(id, got.siteCount, got.complete ? (got.siteCount ?? info.siteCount) : null);
            log(`   主贴 ${id}：按固定链接补齐，新增 ${added.length} 条`);
        } catch (e) {
            if (e.blocked || isGone(e)) throw e;
            log(`   ⚠️ 主贴 ${id} 按固定链接补齐失败（${String(e.message).split('\n')[0]}），跳过这一条`);
        }
    }
}

/** 无限滚动：往下滚一屏并等新内容渲染，返回是否还有增长 */
// 滚到底之后等下一批插进来的预算。**分成几次「推一把」而不是一次长等**：Facebook 的
// 懒加载靠滚动位置触发，插入新内容后页面变长、原来的「底」就不在底了，得再推一次
const SCROLL_NUDGES = 3;
const SCROLL_GROWTH_MS = 4000;

/**
 * 往下滚一屏并等新内容渲染，返回是否还有增长。
 *
 * **不能滚一次、等 2.5 秒就下结论**：信息流是懒加载的，慢一点就会被判成「已到底」，
 * 整轮采集只看得到首屏。真站实测（用户那份数据，force_full 一轮）：
 * 「批次 1：提取 8 条（含评论）」之后立刻「页面不再增长，已到底」，而那个小组库里
 * 已经攒了 115 条主贴 —— 后果是同一轮里点开的 12 处折叠只有 1 条主贴吃得到。
 *
 * 轮询而不是一次长等：真到底时不必白等满 12 秒。等待期间不发任何请求，
 * 只是把「多久算没有下一批」放宽 —— 与「像一个有耐心的真实用户」的姿态一致。
 */
async function scrollOnce(page) {
    const height = () => page.evaluate(() => document.body.scrollHeight);
    const before = await height();
    for (let nudge = 0; nudge < SCROLL_NUDGES; nudge++) {
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        const until = Date.now() + SCROLL_GROWTH_MS;
        while (Date.now() < until) {
            await page.waitForTimeout(500);
            if (await height() > before) return true;
        }
    }
    return false;
}

async function main() {
    if (!CONFIG.groupId) throw new Error('job.params 缺少 group_id');

    const authOpts = {
        entryUrl: groupUrl(),
        loginUrl: loginUrl(),
        selectors: SELECTORS,
        timeout: CONFIG.timeout,
        gotoPage,
    };

    const browser = await launchBrowser(CONFIG.headless);
    // 不跟 tweakers.js 用 nl-NL：locale 决定未登录界面的语言，而人工授权模式下这个窗口
    // 是给操作者看的。荷兰语页面上最显眼的绿色按钮是「Nieuw account maken」（创建新账户），
    // 实测被认成注册页。zh-CN 同时与宿主机时区 Asia/Shanghai、中国出口 IP 自洽。
    // 注意：登录之后 Facebook 按账号自己的语言设置渲染，与这里无关。
    const context = await newContext(browser, { stateFile: CONFIG.stateFile, locale: 'zh-CN' });
    const page = await context.newPage();
    // 必须在任何导航之前挂上：图片字节取自浏览器自己的响应，晚一步第一屏的图就漏了。
    // 人工授权模式不采数据，不需要它
    const capture = (CONFIG.mode === 'login_only' || !CONFIG.mediaDir)
        ? null : attachImageCapture(page);
    const tally = newImageTally();

    // ===== 人工授权模式：开有头浏览器让人自己过验证，只落会话不采数据 =====
    if (CONFIG.mode === 'login_only') {
        const outcome = await waitForManualLogin(page, {
            ...authOpts, maxWaitMs: CONFIG.manualLoginTimeout,
        });
        if (outcome !== 'ok') {
            await browser.close().catch(() => {});
            // 分钟数从实际配置算，别写死：这句会变成界面上的失败提示，
            // 和页面倒计时对不上会让用户以为超时判定出了错
            needManualAuth(outcome === 'closed'
                ? '浏览器窗口被关闭，授权未完成'
                : `等待人工登录超时（${Math.round(CONFIG.manualLoginTimeout / 60000)} 分钟）`);
        }
        await saveStorageState(context, CONFIG.stateFile);
        await browser.close();
        log('   会话已保存，后续采集不再需要密码');
        process.exit(0);
    }

    // ===== 采集模式 =====
    const usedPath = await ensureLogin(page, authOpts);
    // 会话刚建立就先落一次盘：后面抓取环节出错也不必再登一次
    if (usedPath === 'password') await saveStorageState(context, CONFIG.stateFile);

    // 增量：信息流没有页码可续，只能重扫再按指纹去重。已有指纹由 job 下发
    // （帖子的家在 posts 表里，这里没有旧文件可读）。本轮只输出新出现的，
    // 合并交给 Python 侧的 upsert —— 它会保住已有帖子的 translation 和 _processed
    const seen = new Set(CONFIG.knownFingerprints);
    if (CONFIG.incremental && seen.size) {
        log(`   增量模式: 已有 ${seen.size} 条，本轮只追加新出现的`);
    }

    const fresh = [];
    let batch = 0;
    let complete = true;
    let stopReason = null;

    try {
        await gotoPage(page, groupUrl(), CONFIG.timeout);
        feedUrl = page.url();
        // 主贴 message_id → 指纹：按固定链接补齐时，评论要挂到它下面
        const rootFingerprints = new Map();
        for (batch = 1; batch <= CONFIG.maxBatches; batch++) {
            await humanRead(page);
            const extracted = await extractBatch(page);
            extracted.scans.forEach((scan) => logImageScan(scan, tally));
            const flat = flatten(extracted.posts, batch);
            flat.forEach((p) => {
                if (!p.parent_fingerprint && p.message_id) rootFingerprints.set(p.message_id, p.fingerprint);
            });

            if (batch === 1 && flat.length === 0) {
                throw new Error('第一批未提取到任何帖子，提取器可能已失效或页面被拦截');
            }

            const added = [];
            flat.forEach((p) => {
                if (!seen.has(p.fingerprint)) {
                    seen.add(p.fingerprint);
                    fresh.push(p);
                    added.push(p);
                }
            });
            // 见过的主贴也要记：它们不再输出，两个数只能随 thread_counts 交出去。
            // 排在入 fresh 之后 —— 浮层里收割到的这一串此刻才算交出去了
            flat.filter((p) => !p.parent_fingerprint && p.message_id).forEach((p) => {
                const got = harvested.get(p.message_id);
                noteThread(p.message_id, p.site_comment_count,
                    got && got.complete ? (got.siteCount ?? p.site_comment_count) : null);
            });
            // 只给新增的帖子下图：已见过的这一轮会被指纹去重，图早就下过了
            await saveImages(context, capture, added, {
                mediaDir: CONFIG.mediaDir, sourceId: CONFIG.sourceId, tally,
            });
            log(`  批次 ${batch}：提取 ${flat.length} 条（含评论），新增 ${added.length} 条`);
            progress(batch, CONFIG.maxBatches, `批次 ${batch}/${CONFIG.maxBatches}`);

            // 水位线：信息流按时间倒序，整批都见过就说明已经翻到旧内容
            if (CONFIG.incremental && seen.size > fresh.length && added.length === 0) {
                log('   已翻到历史数据，停止继续滚动');
                break;
            }
            if (pastDeadline()) break;
            if (!(await scrollOnce(page))) {
                log('   页面不再增长，已到底');
                break;
            }
            await humanDelay(CONFIG.delayMin, CONFIG.delayMax);
        }
        await completeFromPermalinks(page, {
            context, capture, tally, fresh, seen, rootFingerprints,
            batch: Math.min(batch, CONFIG.maxBatches),
        });
        if (deadlineHit) {
            log(`   ⚠️ ${DEADLINE_REASON}`);
            complete = false;
            stopReason = DEADLINE_REASON;
        }
    } catch (e) {
        complete = false;
        stopReason = e.message;
        if (CONFIG.knownFingerprints.length === 0 && fresh.length === 0) {
            await saveStorageState(context, CONFIG.stateFile);
            await browser.close();
            process.stderr.write(`${stopReason}\n`);
            process.exit(1);
        }
    }

    logImageTally(tally);
    await saveStorageState(context, CONFIG.stateFile);
    await browser.close();

    // 只输出本轮新出现的。历史数据在 posts 表里，合并由 Python 侧的 upsert 完成 ——
    // 它按 (source_id, fingerprint) 更新，已有帖子的 translation 和 _processed 原样保留
    writeOutput(job, {
        group_id: CONFIG.groupId,
        total_pages: batch,
        total_posts: fresh.length,
        complete,
        stop_reason: stopReason,
        posts: fresh,
        thread_counts: Object.fromEntries(threadCounts),
    });

    if (!complete) {
        process.stderr.write(`${stopReason}\n`);
        process.exit(2);
    }
    process.exit(0);
}

main().catch((e) => {
    process.stderr.write(`${e.message}\n`);
    process.exit(1);
});
