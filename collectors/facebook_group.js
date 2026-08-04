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

const fs = require('fs');
const { readJob, log, progress, writeOutput } = require('./lib/job');
const { makeFingerprint } = require('./lib/fingerprint');
const { launchBrowser, newContext, saveStorageState } = require('./lib/browser');
const { gotoPage } = require('./lib/http');
const { humanDelay, humanRead } = require('./lib/human');
const { ensureLogin, waitForManualLogin, needManualAuth } = require('./lib/auth');

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
    baseUrl: (job.base_url || 'https://www.facebook.com').replace(/\/+$/, ''),
    outputFile: job.output_path,
    stateFile: job.state_file,
    maxBatches: params.max_batches || 10,
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
    post: '[role="article"]',
    comment: '[role="article"] [role="article"]',
    // 主贴和评论都没有 h3 / strong，作者只剩小组内的个人主页链接。同一个人会连出几个
    // 这样的链接，**排在前面的是头像、文本为空** —— 直接 querySelector 取到的就是空的
    // 那个，于是每条帖子都成了匿名（取法见 nameOf）
    author: 'a[href*="/user/"]',
    // 时间锚点：主贴是头部那个固定链接，评论自带 comment_id。
    // 两者的 aria-label 都不能用，原因见 resolveTimes()
    postTime: 'a[href*="/posts/"]:not([href*="comment_id"])',
    commentTime: 'a[href*="comment_id"]',
    body: '[data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]',
    // 评论没有专用正文容器，第一个 div[dir=auto] 就是正文
    commentBody: 'div[dir="auto"]',
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
async function hoverTime(page, key) {
    try {
        const el = await page.$(`[${TIME_MARK}="${key}"]`);
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

async function extractBatch(page) {
    const raw = await page.evaluate((sel) => {
        const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
        // 主贴的字段只能在主贴自己这一层找：评论也是 article，嵌在里面
        const own = (root, selector) => [...root.querySelectorAll(selector)]
            .find((el) => el.closest(sel.post) === root) || null;
        // 作者：取第一个**有文字**的个人主页链接，前面那几个是同一个人的头像链接
        const nameOf = (root, scoped) => text([...root.querySelectorAll(sel.author)].find(
            (el) => (!scoped || el.closest(sel.post) === root) && el.textContent.trim()));
        const idOf = (link, kind) => {
            const href = link ? link.getAttribute('href') : '';
            const m = href && (kind === 'comment'
                ? href.match(/comment_id=([^&#]+)/)
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

        const out = [];
        // 只取顶层 article：评论也是 article，嵌在主贴里面
        [...document.querySelectorAll(sel.post)]
            .filter((a) => !a.parentElement.closest(sel.post))
            .forEach((article) => {
                const comments = [...article.querySelectorAll(sel.comment)].map((c) => {
                    const link = c.querySelector(sel.commentTime);
                    const id = idOf(link, 'comment');
                    return {
                        username: nameOf(c, false),
                        timeKey: mark(link, id && `c${id}`),
                        content: text(c.querySelector(sel.commentBody)),
                        message_id: id,
                    };
                });
                const link = own(article, sel.postTime);
                const id = idOf(link, 'post');
                out.push({
                    username: nameOf(article, true),
                    timeKey: mark(link, id && `p${id}`),
                    content: text(own(article, sel.body)),
                    message_id: id,
                    comments,
                });
            });
        return out;
    }, SELECTORS);

    const items = raw.flatMap((p) => [p, ...p.comments]);
    for (const item of items) {
        if (item.timeKey && !timeCache.has(item.timeKey)) {
            timeCache.set(item.timeKey, await hoverTime(page, item.timeKey));
        }
        item.rawTime = timeCache.get(item.timeKey) || '';
    }
    return raw;
}

// 取不到作者时一律填同一个「匿名」，**不要按序号编名字**：信息流每一批都会把上一批的
// 帖子重新提取一遍，序号会跟着变，而 username 进指纹 —— 实测同一条帖子因此在两个批次里
// 拿到两个指纹，一轮抓下来就翻倍。同名不会把两个人混成一条：指纹里还有时间和正文。
function flatten(rawPosts, displayBatch) {
    const flat = [];
    rawPosts.forEach((raw) => {
        const post = {
            username: raw.username || '匿名',
            timestamp: normalizeTime(raw.rawTime),
            content: raw.content,
            page_number: displayBatch,
            message_id: raw.message_id,
            parent_fingerprint: null,
            reply_level: 0,
        };
        post.fingerprint = makeFingerprint(post);
        flat.push(post);
        (raw.comments || []).forEach((c) => {
            const comment = {
                username: c.username || '匿名',
                timestamp: normalizeTime(c.rawTime),
                content: c.content,
                page_number: displayBatch,
                message_id: c.message_id,
                parent_fingerprint: post.fingerprint,
                reply_level: 1,
            };
            comment.fingerprint = makeFingerprint(comment);
            flat.push(comment);
        });
    });
    return flat;
}

/** 无限滚动：往下滚一屏并等新内容渲染，返回是否还有增长 */
async function scrollOnce(page) {
    const before = await page.evaluate(() => document.body.scrollHeight);
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(2500);
    const after = await page.evaluate(() => document.body.scrollHeight);
    return after > before;
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

    // 增量：先读旧数据。信息流没有页码可续，只能重扫再按指纹去重 ——
    // 绝不能只写这一轮抓到的，落盘文件同时承载 translation 和 _processed 标记
    const existingPosts = [];
    const seen = new Set();
    if (CONFIG.incremental && fs.existsSync(CONFIG.outputFile)) {
        try {
            const existing = JSON.parse(fs.readFileSync(CONFIG.outputFile, 'utf-8'));
            (existing.posts || []).forEach((p) => {
                if (p.fingerprint && !seen.has(p.fingerprint)) {
                    seen.add(p.fingerprint);
                    existingPosts.push(p);
                }
            });
            log(`   增量模式: 已有 ${existingPosts.length} 条，本轮只追加新出现的`);
        } catch (e) {
            log(`   ⚠️ 读取已有数据失败，回退到全量模式: ${e.message}`);
        }
    }

    const fresh = [];
    let batch = 0;
    let complete = true;
    let stopReason = null;

    try {
        await gotoPage(page, groupUrl(), CONFIG.timeout);
        for (batch = 1; batch <= CONFIG.maxBatches; batch++) {
            await humanRead(page);
            const flat = flatten(await extractBatch(page), batch);

            if (batch === 1 && flat.length === 0) {
                throw new Error('第一批未提取到任何帖子，提取器可能已失效或页面被拦截');
            }

            let added = 0;
            flat.forEach((p) => {
                if (!seen.has(p.fingerprint)) {
                    seen.add(p.fingerprint);
                    fresh.push(p);
                    added++;
                }
            });
            log(`  批次 ${batch}：提取 ${flat.length} 条（含评论），新增 ${added} 条`);
            progress(batch, CONFIG.maxBatches, `批次 ${batch}/${CONFIG.maxBatches}`);

            // 水位线：信息流按时间倒序，整批都见过就说明已经翻到旧内容
            if (CONFIG.incremental && existingPosts.length && added === 0) {
                log('   已翻到历史数据，停止继续滚动');
                break;
            }
            if (!(await scrollOnce(page))) {
                log('   页面不再增长，已到底');
                break;
            }
            await humanDelay(CONFIG.delayMin, CONFIG.delayMax);
        }
    } catch (e) {
        complete = false;
        stopReason = e.message;
        if (existingPosts.length === 0 && fresh.length === 0) {
            await saveStorageState(context, CONFIG.stateFile);
            await browser.close();
            process.stderr.write(`${stopReason}\n`);
            process.exit(1);
        }
    }

    await saveStorageState(context, CONFIG.stateFile);
    await browser.close();

    // 历史在前、新增在后：已有帖子连同 translation 和 _processed 原样保留
    const merged = [...existingPosts, ...fresh];

    writeOutput(job, {
        group_id: CONFIG.groupId,
        total_pages: batch,
        total_posts: merged.length,
        complete,
        stop_reason: stopReason,
        posts: merged,
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
