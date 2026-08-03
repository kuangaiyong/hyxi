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
    // 登录态判定：出现发帖入口即视为已登录
    loggedIn: '[role="feed"], [data-pagelet^="GroupsFeed"], [aria-label="创建帖子"], [aria-label="Create a post"]',
    usernameInput: 'input[name="email"], #email',
    passwordInput: 'input[name="pass"], #pass',
    submitButton: 'button[name="login"], [data-testid="royal_login_button"]',
    twoFactorInput: 'input[name="approvals_code"], #approvals_code',
    loginError: '#error_box, [data-testid="login_error_message"]',
    // 内容提取
    post: '[role="article"]',
    comment: '[role="article"] [role="article"]',
    author: 'h3 a, strong a, [data-ad-rendering-role="profile_name"]',
    time: 'abbr[data-utime], a[href*="/posts/"] span[title]',
    body: '[data-ad-comet-preview="message"], [data-ad-rendering-role="story_message"]',
};

function groupUrl() {
    return `${CONFIG.baseUrl}/groups/${CONFIG.groupId}`;
}

function loginUrl() {
    return `${CONFIG.baseUrl}/login`;
}

/** 统一成落盘格式 dd-mm-yyyy HH:MM；拿不到结构化时间就原样保留，绝不编造 */
function normalizeTime(raw) {
    const asEpoch = Number(raw);
    const d = Number.isFinite(asEpoch) && asEpoch > 1e9
        ? new Date(asEpoch * 1000)
        : new Date(raw);
    if (isNaN(d.getTime())) return (raw || '').trim();
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getDate())}-${p(d.getMonth() + 1)}-${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function extractBatch(page) {
    return await page.evaluate((sel) => {
        const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
        const timeOf = (el) => {
            const t = el.querySelector(sel.time);
            if (!t) return '';
            return t.getAttribute('data-utime') || t.getAttribute('title') || text(t);
        };
        const out = [];
        // 只取顶层 article：评论也是 article，嵌在主贴里面
        const articles = [...document.querySelectorAll(sel.post)].filter(
            (a) => !a.parentElement.closest(sel.post)
        );
        articles.forEach((article) => {
            const comments = [...article.querySelectorAll(sel.comment)].map((c) => ({
                username: text(c.querySelector(sel.author)),
                rawTime: timeOf(c),
                content: text(c.querySelector(sel.body)),
                message_id: c.getAttribute('data-comment-id') || '',
            }));
            out.push({
                username: text(article.querySelector(sel.author)),
                rawTime: timeOf(article),
                content: text(article.querySelector(sel.body)),
                message_id: article.getAttribute('data-post-id') || '',
                comments,
            });
        });
        return out;
    }, SELECTORS);
}

function flatten(rawPosts, displayBatch, anonCounter) {
    const flat = [];
    rawPosts.forEach((raw) => {
        const post = {
            username: raw.username || `用户${++anonCounter.n}`,
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
                username: c.username || `用户${++anonCounter.n}`,
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
    const context = await newContext(browser, { stateFile: CONFIG.stateFile, locale: 'nl-NL' });
    const page = await context.newPage();

    // ===== 人工授权模式：开有头浏览器让人自己过验证，只落会话不采数据 =====
    if (CONFIG.mode === 'login_only') {
        const ok = await waitForManualLogin(page, {
            ...authOpts, maxWaitMs: CONFIG.manualLoginTimeout,
        });
        if (!ok) {
            await browser.close();
            needManualAuth('等待人工登录超时（5 分钟）');
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
    const anonCounter = { n: 0 };
    let batch = 0;
    let complete = true;
    let stopReason = null;

    try {
        await gotoPage(page, groupUrl(), CONFIG.timeout);
        for (batch = 1; batch <= CONFIG.maxBatches; batch++) {
            await humanRead(page);
            const flat = flatten(await extractBatch(page), batch, anonCounter);

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
