/**
 * Tweakers.net 论坛帖子提取脚本 v3 (最终版)
 *
 * 用法: node tweakers_scraper_playwright.js [--headless] [--thread=ID] [--start=N]
 */

const { chromium } = require('playwright');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const CONFIG = {
    threadId: 2336074,
    startPage: 1,       // 显示页码（1-based）
    headless: false,
    delayMin: 4000,     // 翻页间隔随机区间（毫秒）
    delayMax: 11000,
    timeout: 30000,
    incremental: false,  // 增量模式：只抓取新页面
};
for (const arg of process.argv.slice(2)) {
    if (arg === '--headless') CONFIG.headless = true;
    if (arg === '--incremental') CONFIG.incremental = true;
    if (arg.startsWith('--thread=')) CONFIG.threadId = parseInt(arg.split('=')[1]);
    if (arg.startsWith('--start=')) CONFIG.startPage = parseInt(arg.split('=')[1]);
}

CONFIG.outputFile = path.join(__dirname, `tweakers_thread_${CONFIG.threadId}.json`);

// 浏览器会话状态（cookie / localStorage）：复用后不必每轮重走 DPG 隐私 gate，
// 请求数更少，也不再表现为「每次来都是全新访客」
const STATE_FILE = path.join(__dirname, '.scraper_state.json');

// 指纹生成（用户名 + 时间戳 + 内容前100字符）
function makeFingerprint(post) {
    const raw = `${post.username}|${post.timestamp}|${(post.content || '').slice(0, 100)}`;
    return crypto.createHash('sha256').update(raw).digest('hex').slice(0, 16);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function randInt(min, max) { return min + Math.floor(Math.random() * (max - min + 1)); }
function humanDelay() { return sleep(randInt(CONFIG.delayMin, CONFIG.delayMax)); }
function log(msg) { console.log(`[${new Date().toLocaleTimeString('zh-CN', {hour12: false})}] ${msg}`); }

// 落地即抓、整页零输入事件的访问在行为分析里很扎眼
async function humanRead(page) {
    for (let i = randInt(2, 4); i > 0; i--) {
        await page.mouse.wheel(0, randInt(300, 900)).catch(() => {});
        await sleep(randInt(400, 1400));
    }
}

// 收到限流就退让一次，仍被拒绝即停止抓取——不做指数重试链，对方说停就停
async function gotoPage(page, url, timeout) {
    for (let attempt = 0; attempt < 2; attempt++) {
        const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
        const status = resp ? resp.status() : 0;
        if (status !== 429 && status !== 403 && status !== 503) return resp;
        if (attempt === 1) {
            // 打标记：沿途那些「失败就当没有下一页」的宽容 catch 必须放它过去，
            // 否则被拒会被读成「这个帖子只有一页」，残缺结果照样写成 complete
            const err = new Error(`目标站拒绝访问 (HTTP ${status})，已主动停止抓取`);
            err.blocked = true;
            throw err;
        }
        const retryAfter = parseInt(resp.headers()['retry-after'], 10);
        const waitMs = Math.min(Number.isFinite(retryAfter) ? retryAfter * 1000 : 60000, 300000);
        log(`  ⏸️ 收到 HTTP ${status}，等待 ${Math.round(waitMs / 1000)}s 后重试一次`);
        await sleep(waitMs);
    }
}

// URL格式: /0 = 显示第1页, /1 = 显示第2页
function displayToUrl(displayPage) { return displayPage - 1; }
function urlToDisplay(urlPage) { return urlPage + 1; }
function threadUrl(displayPage) {
    return `https://gathering.tweakers.net/forum/list_messages/${CONFIG.threadId}/${displayToUrl(displayPage)}`;
}

// ===== 隐私确认 =====
async function handleConsent(page) {
    const url = page.url();
    if (url.includes('privacygate') || url.includes('myprivacy')) {
        log('  处理DPG隐私gate...');
        await page.waitForSelector('a[href*="accept"], button[title*="Akkoord"], #pg-accept-button', { timeout: 5000 }).catch(() => {});
        await sleep(1500);
        const content = await page.content();
        const m = content.match(/callbackUrl\s*=\s*new\s+URL\(decodeURIComponent\('([^']+)'\)\)/);
        if (m) {
            // 必须走 gotoPage：被拒时这一跳才是返回 403 的那个请求，而它落地后的 URL 仍然是
            // 正常的 /forum/... 路径，只看 URL 根本发现不了
            await gotoPage(page, decodeURIComponent(m[1]), 20000);
            await page.waitForSelector('.message, .pageIndex, .forum', { timeout: 10000 }).catch(() => {});
            await sleep(1000);
        }
    }
}

// ===== 提取帖子 =====
async function extractPosts(page, displayPage) {
    const posts = await page.evaluate((displayPage) => {
        const results = [];
        const msgBlocks = document.querySelectorAll('.message[data-message-id]');

        msgBlocks.forEach(block => {
            try {
                // 用户名: .poster .user (span.user inside poster div)
                let username = '';
                const userSpan = block.querySelector('.poster .user, .userklipklap .user, .username .user');
                if (userSpan) {
                    username = userSpan.textContent.trim();
                }

                // 时间戳: data-datetime 属性
                let timestamp = '';
                const timeSpan = block.querySelector('[data-datetime]');
                if (timeSpan) {
                    timestamp = timeSpan.getAttribute('data-datetime') || '';
                }

                // 内容: .messagecontent 或 .post div
                let content = '';
                const contentEl = block.querySelector('.messagecontent, .ugcContent, .forumUgcContent');
                if (contentEl) {
                    // 克隆以移除引用块
                    const clone = contentEl.cloneNode(true);
                    clone.querySelectorAll('.quote, .bb_quote, blockquote, [class*="quote"], .quotetext, .cite').forEach(el => el.remove());
                    clone.querySelectorAll('script, style, .signature, .message_actions').forEach(el => el.remove());
                    content = clone.textContent.trim();
                }
                if (!content) {
                    // fallback: .post div
                    const postDiv = block.querySelector('.post');
                    if (postDiv) {
                        const clone = postDiv.cloneNode(true);
                        clone.querySelectorAll('.quote, .bb_quote, blockquote, [class*="quote"]').forEach(el => el.remove());
                        content = clone.textContent.trim();
                    }
                }

                // 清理
                content = content
                    .replace(/\s+/g, ' ')
                    .replace(/\[Afbeelding\]/gi, '[图片]')
                    .replace(/&nbsp;/g, ' ')
                    .replace(/\[b\]|\[\/b\]|\[i\]|\[\/i\]|\[u\]|\[\/u\]/gi, '')
                    .replace(/\s{2,}/g, ' ')
                    .trim();

                // 消息ID: data-message-id 属性
                const messageId = block.getAttribute('data-message-id') || '';

                if (username || content) {
                    results.push({
                        username: username || ('用户' + (results.length + 1)),
                        timestamp: timestamp,
                        content: content,
                        page_number: displayPage,
                        message_id: messageId,
                    });
                }
            } catch (e) { /* skip */ }
        });

        return results;
    }, displayPage);

    return posts;
}

// ===== 获取总页数 =====
async function getTotalPages(page) {
    return await page.evaluate(() => {
        let maxDisplay = 1;

        // 从 .pageIndex 中的链接提取
        const pageIndex = document.querySelector('.pageIndex');
        if (pageIndex) {
            const links = pageIndex.querySelectorAll('a');
            links.forEach(link => {
                const href = link.getAttribute('href') || '';
                const text = link.textContent.trim();

                // 从href提取URL页码（/0 = 显示1, /1 = 显示2）
                const hm = href.match(/\/forum\/list_messages\/\d+\/(\d+)/);
                if (hm) {
                    const displayPage = parseInt(hm[1]) + 1;  // URL page -> display page
                    if (displayPage > maxDisplay) maxDisplay = displayPage;
                }

                // 从显示文本提取
                const tm = text.match(/^(\d+)$/);
                if (tm) {
                    const p = parseInt(tm[1]);
                    if (p > maxDisplay) maxDisplay = p;
                }
            });

            // "Laatste" 链接
            const lastLink = pageIndex.querySelector('a[href*="last"]');
            if (lastLink) {
                // 需要实际访问来获取页码，暂时设为未知
                if (maxDisplay <= 1) maxDisplay = 0;  // 0 = unknown, probe
            }
        }

        return maxDisplay;
    });
}

// ===== 探测最后一页的URL页码 =====
async function probeLastPageUrl(page) {
    const lastHref = await page.evaluate(() => {
        const lastLink = document.querySelector('.pageIndex a[href*="last"]');
        return lastLink ? lastLink.getAttribute('href') : null;
    });
    return lastHref;
}

// ===== 解析最后一页URL =====
async function getLastDisplayPage(page) {
    const lastHref = await probeLastPageUrl(page);
    if (lastHref) {
        // 导航到最后一页来获取实际URL
        try {
            await gotoPage(page, lastHref, 20000);
            await sleep(randInt(1200, 3000));
            await handleConsent(page);
            const urlPage = await page.evaluate(() => {
                const m = window.location.href.match(/\/forum\/list_messages\/\d+\/(\d+)/);
                return m ? parseInt(m[1]) : null;
            });
            if (urlPage !== null) {
                return urlPage + 1;  // URL page -> display page
            }
        } catch (e) {
            if (e.blocked) throw e;
        }
    }
    return null;
}

// ===== 浏览器身份 =====
// 无头模式下 UA 会自报 HeadlessChrome，这是唯一需要抹掉的标记（客户端提示不受影响）。
// 硬编码一整串 UA 会在 Chrome 升级后与客户端提示失配，所以运行期从浏览器自己取。
// 探测页停在 about:blank，不产生任何网络请求。
async function resolveUserAgent(browser) {
    const probe = await browser.newContext();
    const ua = await (await probe.newPage()).evaluate(() => navigator.userAgent);
    await probe.close();
    return ua.replace('HeadlessChrome', 'Chrome');
}

// ===== 主流程 =====
async function main() {
    const modeStr = CONFIG.incremental ? '增量' : '全量';
    log(`🚀 Tweakers论坛抓取器 v4 (${modeStr}模式)`);
    log(`   帖子ID: ${CONFIG.threadId} | 起始页: ${CONFIG.startPage} | 模式: ${CONFIG.headless ? '无头' : '有头'}`);
    if (CONFIG.startPage > 1 && !CONFIG.incremental) {
        // 抓取循环只前进不回补，起始页之前的内容这一轮不会出现在结果里
        log(`   ⚠️ 第 1~${CONFIG.startPage - 1} 页不会被抓取`);
    }

    // 增量模式：加载已有数据，确定起始页
    let existingPosts = [];
    let existingFingerprints = new Set();
    if (CONFIG.incremental && fs.existsSync(CONFIG.outputFile)) {
        try {
            const existing = JSON.parse(fs.readFileSync(CONFIG.outputFile, 'utf-8'));
            existingPosts = existing.posts || [];
            existingFingerprints = new Set(existingPosts.map(p => p.fingerprint).filter(Boolean));
            const maxPage = Math.max(...existingPosts.map(p => p.page_number || 1), 0);
            if (maxPage >= CONFIG.startPage) {
                CONFIG.startPage = maxPage + 1;
                log(`   增量模式: 已有 ${existingPosts.length} 条帖子，最大页码 ${maxPage}，从第 ${CONFIG.startPage} 页开始`);
            }
        } catch (e) {
            log(`   ⚠️ 读取已有数据失败，回退到全量模式: ${e.message}`);
        }
    }

    const browser = await chromium.launch({
        headless: CONFIG.headless,
        channel: 'chrome',
        args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
    });

    const allPosts = [];
    // 站点声明的总页数：只跟随站点上调，绝不被抓取中断点覆盖——
    // 一旦被覆盖，残缺结果就会以「总共就这么多页」的姿态落盘，下游完全看不出来
    let detectedTotalPages = CONFIG.startPage;
    let lastConfirmedPage = CONFIG.startPage;
    let incomplete = false;
    let stopReason = null;
    // 建 context 也可能抛错，必须一起罩进 try —— 漏掉 browser.close() 会留下常驻的 Chrome 进程
    let context = null;

    try {
        const userAgent = await resolveUserAgent(browser);

        let storageState;
        if (fs.existsSync(STATE_FILE)) {
            try {
                const parsed = JSON.parse(fs.readFileSync(STATE_FILE, 'utf-8'));
                if (Array.isArray(parsed.cookies)) storageState = parsed;
            } catch (e) {
                log(`   ⚠️ 会话状态文件损坏，本轮不复用: ${e.message}`);
            }
        }

        context = await browser.newContext({
            userAgent,
            locale: 'nl-NL',
            viewport: { width: 1536, height: 864 },
            storageState,
            // 时区故意不设，跟随宿主机（Asia/Shanghai）：检测方比对的是时区与出口 IP 的地理
            // 位置，从中国境内的机器发出的流量配 Europe/Amsterdam 反而是更硬的矛盾。
            // 将来若改从欧洲机器出网，这里要一并设成当地时区。
            //
            // 客户端提示（sec-ch-ua*）和 accept-language 都不手工覆盖：实测设了 userAgent 之后
            // Chrome 会自己把 sec-ch-ua 同步成不含 HeadlessChrome 的值，且与页面侧
            // navigator.userAgentData.brands 逐字一致；手写一份反而会在 GREASE 品牌上对不上。
            // accept-language 同理由 locale 决定，硬塞 q 值列表要么被覆盖、要么让
            // navigator.language 与请求头分家。
        });

        const page = await context.newPage();
        page.setDefaultTimeout(CONFIG.timeout);

        // 访问首页
        log(`📄 访问: ${threadUrl(CONFIG.startPage)}`);
        await gotoPage(page, threadUrl(CONFIG.startPage), 30000);
        await page.waitForSelector('.message, .pageIndex, .forum', { timeout: 15000 }).catch(() => {});
        await sleep(randInt(1200, 3000));
        await handleConsent(page);
        log(`  当前URL: ${page.url()}`);

        // 提取第一页
        log('🔍 提取帖子...');
        let posts = await extractPosts(page, CONFIG.startPage);
        allPosts.push(...posts);
        log(`  ✅ 第 ${CONFIG.startPage} 页: ${posts.length} 条帖子`);

        if (posts.length > 0) {
            log(`  📝 第1条: [${posts[0].username}] ${posts[0].timestamp} — ${posts[0].content.substring(0, 100)}...`);
        } else if (existingPosts.length === 0) {
            // 一条都没拿到又没有历史数据，继续走下去会写出一份 complete:true 的空结果，
            // 下游会把它当成「这个帖子本来就是空的」照常翻译、导出
            throw new Error(`第 ${CONFIG.startPage} 页未提取到任何帖子（可能被目标站拦截或页面结构已变化）`);
        } else {
            // 增量模式下起始页是 maxPage+1，超出末尾时本来就该是空的——这是「没有新回帖」，
            // 不是残缺，历史数据完好，complete 仍然为 true
            log(`  起始页无新帖，已有 ${existingPosts.length} 条历史数据`);
        }

        // 检测总页数
        detectedTotalPages = await getTotalPages(page);
        log(`📊 分页检测: ${detectedTotalPages > 0 ? detectedTotalPages + ' 页' : '需要探测'}`);

        // 如果检测结果为0（未知），通过last链接确定
        if (detectedTotalPages <= 0) {
            const lastPage = await getLastDisplayPage(page);
            if (lastPage) {
                detectedTotalPages = lastPage;
                log(`  最后一页: ${detectedTotalPages}`);

                // 回到起始页
                await gotoPage(page, threadUrl(CONFIG.startPage), 20000);
                await sleep(randInt(1200, 3000));
                await handleConsent(page);
            } else {
                detectedTotalPages = CONFIG.startPage;
            }
        } else if (detectedTotalPages <= 1) {
            // 看起来只有1页，但探测一下下一页
            const nextPage = CONFIG.startPage + 1;
            log(`  探测第 ${nextPage} 页...`);
            try {
                await gotoPage(page, threadUrl(nextPage), 15000);
                await sleep(randInt(1200, 3000));
                await handleConsent(page);

                if (page.url().includes('/forum/')) {
                    const testPosts = await extractPosts(page, nextPage);
                    if (testPosts.length > 0) {
                        allPosts.push(...testPosts);
                        lastConfirmedPage = nextPage;
                        log(`  ✅ 第 ${nextPage} 页: ${testPosts.length} 条帖子`);
                        detectedTotalPages = await getTotalPages(page);
                        if (detectedTotalPages <= 0) {
                            detectedTotalPages = await getLastDisplayPage(page) || nextPage;
                        }
                        log(`  📊 更新总页数: ${detectedTotalPages}`);
                    }
                }
            } catch (e) {
                if (e.blocked) throw e;
                /* 其余失败当作没有下一页 */
            }
        }

        // 逐页抓取
        let pagesUntilRest = randInt(8, 15);
        for (let dp = lastConfirmedPage + 1; dp <= detectedTotalPages; dp++) {
            await humanDelay();
            if (--pagesUntilRest <= 0) {
                const restMs = randInt(25000, 60000);
                log(`☕ 休息 ${Math.round(restMs / 1000)}s...`);
                await sleep(restMs);
                pagesUntilRest = randInt(8, 15);
            }
            log(`📄 第 ${dp}/${detectedTotalPages} 页...`);

            try {
                await gotoPage(page, threadUrl(dp), 20000);
                await sleep(randInt(1200, 3000));
                await handleConsent(page);

                if (!page.url().includes('/forum/')) {
                    log(`  ⚠️ 被重定向，终止`);
                    incomplete = true;
                    stopReason = `第 ${dp} 页被重定向到非论坛页面（${page.url()}）`;
                    break;
                }

                await humanRead(page);

                const newPosts = await extractPosts(page, dp);
                if (newPosts.length === 0) {
                    if (dp < detectedTotalPages) {
                        log(`  ⚠️ 第 ${dp} 页无帖子，但站点声明共 ${detectedTotalPages} 页，抓取中断`);
                        incomplete = true;
                        stopReason = `第 ${dp} 页无帖子，站点声明共 ${detectedTotalPages} 页`;
                    } else {
                        log(`  ⚠️ 第 ${dp} 页无帖子，已达末尾`);
                    }
                    break;
                }

                allPosts.push(...newPosts);
                log(`  ✅ 第 ${dp} 页: ${newPosts.length} 条帖子 (累计: ${allPosts.length})`);

                // 定期更新总页数
                if (dp % 5 === 0 || dp === detectedTotalPages) {
                    const updated = await getTotalPages(page);
                    if (updated > detectedTotalPages) {
                        log(`  🔄 总页数: ${detectedTotalPages} → ${updated}`);
                        detectedTotalPages = updated;
                    }
                }
            } catch (e) {
                log(`  ❌ 失败: ${e.message}`);
                incomplete = true;
                stopReason = `第 ${dp} 页抓取失败: ${e.message}`;
                break;
            }
        }

    } catch (e) {
        log(`💥 错误: ${e.message}`);
        incomplete = true;
        stopReason = stopReason || `抓取过程异常: ${e.message}`;
    } finally {
        if (context) {
            try {
                await context.storageState({ path: STATE_FILE });
            } catch (e) {
                log(`   ⚠️ 会话状态保存失败: ${e.message}`);
            }
        }
        await browser.close();
        log('🔒 浏览器已关闭');
    }

    // ===== 后处理：指纹生成 + 去重 + 合并 =====
    // 为每个帖子生成指纹和初始化 _processed 字段
    for (const post of allPosts) {
        post.fingerprint = makeFingerprint(post);
        post._processed = post._processed || { translated: false, sentiment_at: null };
    }

    // 增量模式：去重并合并已有帖子
    let mergedPosts;
    if (CONFIG.incremental && existingPosts.length > 0) {
        const newPosts = allPosts.filter(p => !existingFingerprints.has(p.fingerprint));
        mergedPosts = [...existingPosts, ...newPosts];
        log(`   增量合并: 已有 ${existingPosts.length} + 新增 ${newPosts.length} = 总计 ${mergedPosts.length}`);
    } else {
        mergedPosts = allPosts;
    }

    // ===== 保存 =====
    const uniqueUsers = new Set(mergedPosts.map(p => p.username));
    const result = {
        thread_id: CONFIG.threadId,
        thread_url: `https://gathering.tweakers.net/forum/list_messages/${CONFIG.threadId}/`,
        total_pages: detectedTotalPages,
        total_posts: mergedPosts.length,
        pages_fetched: new Set(allPosts.map(p => p.page_number)).size,
        unique_users: uniqueUsers.size,
        complete: !incomplete,
        stop_reason: stopReason,
        extracted_at: new Date().toISOString(),
        posts: mergedPosts,
    };

    fs.writeFileSync(CONFIG.outputFile, JSON.stringify(result, null, 2), 'utf-8');

    // 数据照常落盘（--incremental 下轮可从 maxPage+1 续抓），但退出码必须说实话
    if (incomplete) {
        console.error(stopReason);
        process.exitCode = allPosts.length > 0 ? 2 : 1;
        log(`\n⚠️ 抓取未完成: ${stopReason}`);
    }

    log(`\n${'='.repeat(60)}`);
    log(`📊 提取完成!`);
    log(`   总页数: ${result.total_pages}`);
    log(`   总帖子: ${result.total_posts}`);
    log(`   用户数: ${result.unique_users}`);
    log(`   文件: ${CONFIG.outputFile}`);
    log(`${'='.repeat(60)}`);

    // 显示用户列表
    log(`\n👥 用户列表: ${[...uniqueUsers].join(', ')}`);

    // 前5条预览
    log('\n📋 帖子预览:');
    allPosts.slice(0, 5).forEach((p, i) => {
        log(`  [${i+1}] ${p.username} | ${p.timestamp}`);
        log(`      ${(p.content || '').substring(0, 150)}...\n`);
    });

    return result;
}

// 退出码约定：0 完整 / 1 硬失败（无可用数据）/ 2 部分完成（数据已落盘，可增量续抓）
main().catch(e => {
    console.error(e && e.stack ? e.stack : String(e));
    process.exit(1);
});
