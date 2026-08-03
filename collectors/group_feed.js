/**
 * 「主贴 + 嵌套评论」型信息流采集器。
 *
 * 与论坛的区别在于两点：一是评论挂在主贴下面，输出时要给评论打上 parent_fingerprint
 * 和 reply_level；二是没有页码，翻页粒度是批次（batch），增量靠最新主贴的时间戳做水位线
 * 而不是页码 —— 无限滚动的信息流没有稳定页码可言。
 *
 * 提取规则写在 SELECTORS 里，站点改类名时提取器返回 0 条，被「第一批零帖子即硬失败」
 * 的守卫拦住，不会写出一份看起来完整实际是空的结果。
 */

const fs = require('fs');
const { readJob, log, progress, writeOutput } = require('./lib/job');
const { makeFingerprint } = require('./lib/fingerprint');
const { launchBrowser, newContext, saveStorageState } = require('./lib/browser');
const { gotoPage } = require('./lib/http');
const { humanDelay, humanRead } = require('./lib/human');

const job = readJob(process.argv.slice(2));
const params = job.params || {};
const pacing = job.pacing || {};

const CONFIG = {
    groupId: params.group_id,
    startBatch: params.start_page || 1,   // 显示批次，1-based
    headless: params.headless !== false,
    delayMin: pacing.delay_min || 4000,
    delayMax: pacing.delay_max || 11000,
    timeout: 30000,
    incremental: !!job.incremental,
    baseUrl: (job.base_url || '').replace(/\/+$/, ''),
    outputFile: job.output_path,
    stateFile: job.state_file,
};

// 提取选择器集中在这里，站点改版只动这一块
const SELECTORS = {
    feed: '.feed',
    totalBatches: '.feed',
    post: 'article.fp-post',
    comment: '.fp-comment',
    author: '.fp-author',
    time: '.fp-time',
    body: '.fp-body',
};

function batchUrl(displayBatch) {
    return `${CONFIG.baseUrl}/groups/${CONFIG.groupId}/batch/${displayBatch - 1}`;
}

/** 荷兰站点的展示时间不可靠，统一取 <time datetime> 并转成落盘用的 dd-mm-yyyy HH:MM */
function normalizeTime(raw) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(raw || '');
    if (!m) return (raw || '').trim();
    return `${m[3]}-${m[2]}-${m[1]} ${m[4]}:${m[5]}`;
}

async function extractBatch(page, displayBatch) {
    return await page.evaluate((sel) => {
        const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
        const out = [];
        document.querySelectorAll(sel.post).forEach((article) => {
            const root = {
                username: text(article.querySelector(sel.author)),
                rawTime: (article.querySelector(sel.time) || {}).getAttribute
                    ? article.querySelector(sel.time).getAttribute('datetime')
                    : '',
                content: text(article.querySelector(sel.body)),
                message_id: article.getAttribute('data-post-id') || '',
                comments: [],
            };
            article.querySelectorAll(sel.comment).forEach((c) => {
                const t = c.querySelector(sel.time);
                root.comments.push({
                    username: text(c.querySelector(sel.author)),
                    rawTime: t ? t.getAttribute('datetime') : '',
                    content: text(c.querySelector(sel.body)),
                    message_id: c.getAttribute('data-comment-id') || '',
                });
            });
            out.push(root);
        });
        return out;
    }, SELECTORS);
}

async function readTotalBatches(page) {
    return await page.evaluate((sel) => {
        const feed = document.querySelector(sel.feed);
        const n = feed && parseInt(feed.getAttribute('data-total-batches'), 10);
        return Number.isFinite(n) && n > 0 ? n : 1;
    }, SELECTORS);
}

/** 把「主贴带 comments 数组」拍平成扁平数组，评论带上父指纹和层级 */
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

async function main() {
    if (!CONFIG.groupId) throw new Error('job.params 缺少 group_id');
    if (!CONFIG.baseUrl) throw new Error('job 缺少 base_url');

    // 增量：先把已有数据整份读进来。信息流没有页码可续，只能全量重扫再按指纹去重 ——
    // 但**绝不能只写这一轮抓到的**：落盘文件同时承载翻译结果和 _processed 标记，
    // 整体覆盖等于把已翻译的帖子重新变成新帖，下一轮再付一次翻译钱。
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

    const browser = await launchBrowser(CONFIG.headless);
    const context = await newContext(browser, { stateFile: CONFIG.stateFile, locale: 'nl-NL' });
    const page = await context.newPage();

    const fresh = [];
    const anonCounter = { n: 0 };
    let totalBatches = 1;
    let complete = true;
    let stopReason = null;

    try {
        for (let batch = CONFIG.startBatch; batch <= totalBatches; batch++) {
            await gotoPage(page, batchUrl(batch), CONFIG.timeout);
            if (batch === CONFIG.startBatch) {
                totalBatches = await readTotalBatches(page);
            }
            await humanRead(page);

            const raw = await extractBatch(page, batch);
            const flat = flatten(raw, batch, anonCounter);

            // 第一批零帖子即硬失败：站点改版或被拦截时页面外壳仍是 200，
            // 不拦住的话会写出一份 complete:true 的空结果，下游照常翻译 0 条
            if (batch === CONFIG.startBatch && flat.length === 0) {
                throw new Error('第一批未提取到任何帖子，提取器可能已失效或请求被拦截');
            }

            let added = 0;
            flat.forEach((p) => {
                if (!seen.has(p.fingerprint)) {
                    seen.add(p.fingerprint);
                    fresh.push(p);
                    added++;
                }
            });
            log(`  批次 ${batch}/${totalBatches}：提取 ${flat.length} 条（含评论），新增 ${added} 条`);
            progress(batch, totalBatches, `批次 ${batch}/${totalBatches}`);

            // 水位线：信息流按时间倒序排列，整批都是见过的就说明已经翻到旧内容了。
            // 全量模式（incremental=false）不适用，那时就是要把所有批次重扫一遍
            if (CONFIG.incremental && existingPosts.length && added === 0) {
                log('   已翻到历史数据，停止继续翻页');
                break;
            }

            if (batch < totalBatches) await humanDelay(CONFIG.delayMin, CONFIG.delayMax);
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

    // 历史在前、新增在后：已有帖子连同它们的 translation 和 _processed 原样保留
    const merged = [...existingPosts, ...fresh];

    writeOutput(job, {
        group_id: CONFIG.groupId,
        total_pages: totalBatches,
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
