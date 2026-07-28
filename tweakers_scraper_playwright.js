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
    delay: 800,
    timeout: 30000,
    maxPages: 200,
    incremental: false,  // 增量模式：只抓取新页面
};
for (const arg of process.argv.slice(2)) {
    if (arg === '--headless') CONFIG.headless = true;
    if (arg === '--incremental') CONFIG.incremental = true;
    if (arg.startsWith('--thread=')) CONFIG.threadId = parseInt(arg.split('=')[1]);
    if (arg.startsWith('--start=')) CONFIG.startPage = parseInt(arg.split('=')[1]);
}

CONFIG.outputFile = path.join(__dirname, `tweakers_thread_${CONFIG.threadId}.json`);

// 指纹生成（用户名 + 时间戳 + 内容前100字符）
function makeFingerprint(post) {
    const raw = `${post.username}|${post.timestamp}|${(post.content || '').slice(0, 100)}`;
    return crypto.createHash('sha256').update(raw).digest('hex').slice(0, 16);
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function log(msg) { console.log(`[${new Date().toLocaleTimeString('zh-CN', {hour12: false})}] ${msg}`); }

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
        await sleep(3000);
        const content = await page.content();
        const m = content.match(/callbackUrl\s*=\s*new\s+URL\(decodeURIComponent\('([^']+)'\)\)/);
        if (m) {
            await page.goto(decodeURIComponent(m[1]), { waitUntil: 'domcontentloaded', timeout: 20000 });
            await sleep(3000);
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
            await page.goto(lastHref, { waitUntil: 'domcontentloaded', timeout: 20000 });
            await sleep(2000);
            await handleConsent(page);
            const urlPage = await page.evaluate(() => {
                const m = window.location.href.match(/\/forum\/list_messages\/\d+\/(\d+)/);
                return m ? parseInt(m[1]) : null;
            });
            if (urlPage !== null) {
                return urlPage + 1;  // URL page -> display page
            }
        } catch { /* */ }
    }
    return null;
}

// ===== 主流程 =====
async function main() {
    const modeStr = CONFIG.incremental ? '增量' : '全量';
    log(`🚀 Tweakers论坛抓取器 v4 (${modeStr}模式)`);
    log(`   帖子ID: ${CONFIG.threadId} | 起始页: ${CONFIG.startPage} | 模式: ${CONFIG.headless ? '无头' : '有头'}`);

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

    const context = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        locale: 'nl-NL',
        viewport: { width: 1280, height: 900 },
    });

    const page = await context.newPage();
    page.setDefaultTimeout(CONFIG.timeout);

    const allPosts = [];
    let totalDisplayPages = CONFIG.startPage;
    let lastConfirmedPage = CONFIG.startPage;

    try {
        // 访问首页
        log(`📄 访问: ${threadUrl(CONFIG.startPage)}`);
        await page.goto(threadUrl(CONFIG.startPage), { waitUntil: 'domcontentloaded', timeout: 30000 });
        await sleep(3000);
        await handleConsent(page);
        log(`  当前URL: ${page.url()}`);

        // 提取第一页
        log('🔍 提取帖子...');
        let posts = await extractPosts(page, CONFIG.startPage);
        allPosts.push(...posts);
        log(`  ✅ 第 ${CONFIG.startPage} 页: ${posts.length} 条帖子`);

        if (posts.length > 0) {
            log(`  📝 第1条: [${posts[0].username}] ${posts[0].timestamp} — ${posts[0].content.substring(0, 100)}...`);
        }

        // 检测总页数
        totalDisplayPages = await getTotalPages(page);
        log(`📊 分页检测: ${totalDisplayPages > 0 ? totalDisplayPages + ' 页' : '需要探测'}`);

        // 如果检测结果为0（未知），通过last链接确定
        if (totalDisplayPages <= 0) {
            const lastPage = await getLastDisplayPage(page);
            if (lastPage) {
                totalDisplayPages = lastPage;
                log(`  最后一页: ${totalDisplayPages}`);

                // 回到起始页
                await page.goto(threadUrl(CONFIG.startPage), { waitUntil: 'domcontentloaded', timeout: 20000 });
                await sleep(2000);
                await handleConsent(page);
            } else {
                totalDisplayPages = CONFIG.startPage;
            }
        } else if (totalDisplayPages <= 1) {
            // 看起来只有1页，但探测一下下一页
            const nextPage = CONFIG.startPage + 1;
            log(`  探测第 ${nextPage} 页...`);
            try {
                await page.goto(threadUrl(nextPage), { waitUntil: 'domcontentloaded', timeout: 15000 });
                await sleep(2000);
                await handleConsent(page);

                if (page.url().includes('/forum/')) {
                    const testPosts = await extractPosts(page, nextPage);
                    if (testPosts.length > 0) {
                        allPosts.push(...testPosts);
                        lastConfirmedPage = nextPage;
                        log(`  ✅ 第 ${nextPage} 页: ${testPosts.length} 条帖子`);
                        totalDisplayPages = await getTotalPages(page);
                        if (totalDisplayPages <= 0) {
                            totalDisplayPages = await getLastDisplayPage(page) || nextPage;
                        }
                        log(`  📊 更新总页数: ${totalDisplayPages}`);
                    }
                }
            } catch { /* no next page */ }
        }

        // 逐页抓取
        for (let dp = lastConfirmedPage + 1; dp <= totalDisplayPages; dp++) {
            await sleep(CONFIG.delay);
            log(`📄 第 ${dp}/${totalDisplayPages} 页...`);

            try {
                await page.goto(threadUrl(dp), { waitUntil: 'domcontentloaded', timeout: 20000 });
                await sleep(2000);
                await handleConsent(page);

                if (!page.url().includes('/forum/')) {
                    log(`  ⚠️ 被重定向，终止`);
                    totalDisplayPages = dp - 1;
                    break;
                }

                const newPosts = await extractPosts(page, dp);
                if (newPosts.length === 0) {
                    log(`  ⚠️ 第 ${dp} 页无帖子，可能已达末尾`);
                    totalDisplayPages = dp - 1;
                    break;
                }

                allPosts.push(...newPosts);
                log(`  ✅ 第 ${dp} 页: ${newPosts.length} 条帖子 (累计: ${allPosts.length})`);

                // 定期更新总页数
                if (dp % 5 === 0 || dp === totalDisplayPages) {
                    const updated = await getTotalPages(page);
                    if (updated > totalDisplayPages) {
                        log(`  🔄 总页数: ${totalDisplayPages} → ${updated}`);
                        totalDisplayPages = updated;
                    }
                }
            } catch (e) {
                log(`  ❌ 失败: ${e.message}`);
                totalDisplayPages = dp - 1;
                break;
            }
        }

    } catch (e) {
        log(`💥 错误: ${e.message}`);
    } finally {
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
        total_pages: totalDisplayPages,
        total_posts: mergedPosts.length,
        unique_users: uniqueUsers.size,
        extracted_at: new Date().toISOString(),
        posts: mergedPosts,
    };

    fs.writeFileSync(CONFIG.outputFile, JSON.stringify(result, null, 2), 'utf-8');

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

main().catch(console.error);
