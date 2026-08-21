/**
 * Tweakers.net 论坛帖子采集器
 *
 * 用法: node collectors/tweakers.js --job=<path/to/job.json>
 */

const fs = require('fs');
const { readJob, log, progress, writeOutput } = require('./lib/job');
const { sleep, randInt, humanDelay, humanRead } = require('./lib/human');
const { gotoPage } = require('./lib/http');
const { launchBrowser, newContext, saveStorageState } = require('./lib/browser');
const { attachImageCapture, saveImages, logImageScan, newImageTally, logImageTally }
    = require('./lib/media');
const { makeFingerprint } = require('./lib/fingerprint');

const job = readJob(process.argv.slice(2));
const params = job.params || {};
const pacing = job.pacing || {};

const CONFIG = {
    threadId: params.thread_id,
    startPage: params.start_page || 1,   // 显示页码（1-based）
    headless: params.headless !== false,
    delayMin: pacing.delay_min || 4000,  // 翻页间隔随机区间（毫秒）
    delayMax: pacing.delay_max || 11000,
    timeout: 30000,
    incremental: !!job.incremental,
    // 增量去重的锚点由 Python 从 posts 表算好下发，脚本不再读旧落盘文件
    knownFingerprints: job.known_fingerprints || [],
    baseUrl: (job.base_url || 'https://gathering.tweakers.net').replace(/\/+$/, ''),
    outputFile: job.output_path,
    stateFile: job.state_file,
    // 图片落盘根目录，由 job 指定（与 output_path 同理，文件位置只有一个来源）。
    // 没配就不抓图，老 job 文件照样能跑
    mediaDir: job.media_dir || '',
    sourceId: job.source_id || 'tweakers',
    // 正文图的渲染尺寸下限。比 Facebook 那边（100）松一档：那个数字是对真站核实过的，
    // 而 Tweakers 的真实页面本机访问不到，宁可多收几张让日志报出来，也不要静默漏掉
    imageMinSize: 80,
};

// URL格式: /0 = 显示第1页, /1 = 显示第2页
function displayToUrl(displayPage) { return displayPage - 1; }
function threadUrl(displayPage) {
    return `${CONFIG.baseUrl}/forum/list_messages/${CONFIG.threadId}/${displayToUrl(displayPage)}`;
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
    const { posts, scan } = await page.evaluate(({ displayPage, minSize }) => {
        const QUOTE_SEL = '.quote, .bb_quote, blockquote, [class*="quote"], .quotetext, .cite';
        const scan = { candidates: [], accepted: [], rejected: [] };
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
                    clone.querySelectorAll(QUOTE_SEL).forEach(el => el.remove());
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

                // 正文图。**必须在原始元素上量尺寸**：上面那个 clone 游离于文档之外，
                // getBoundingClientRect() 会一律返回 0，照着 clone 取等于把每张图都
                // 过滤掉。引用块里的图不算这条帖子的 —— 与 clone 删掉引用块同一个意思。
                // **不设 host 白名单**：Facebook 那边能写死 scontent 是因为对真站核实过，
                // Tweakers 的真实 DOM 本机访问不到，写死 host 等于赌。这里只按
                // 「在正文容器内」+「非 data: URI」+「渲染尺寸」筛，其余交给日志。
                // 取图的容器要跟着正文走：没有 .messagecontent 时正文来自 .post，
                // 图自然也在那里 —— 盯死 contentEl 会让这条路上的帖子静默丢图。
                const imageUrls = [];
                const imgRoot = contentEl || block.querySelector('.post');
                if (imgRoot) {
                    imgRoot.querySelectorAll('img').forEach(im => {
                        const url = im.currentSrc || im.src || '';
                        if (!url || url.startsWith('data:')) return;   // 界面图标，不算候选
                        scan.candidates.push(url);
                        const r = im.getBoundingClientRect();
                        const rej = (why) => scan.rejected.push({
                            why, url, w: Math.round(r.width), h: Math.round(r.height),
                        });
                        // 引用块也要记账：QUOTE_SEL 里的 [class*="quote"] 相当宽，
                        // 真站上万一误伤了正文图，不报出来就完全看不见
                        if (im.closest(QUOTE_SEL)) return rej('引用块');
                        if (r.width < minSize || r.height < minSize) return rej('尺寸');
                        scan.accepted.push(url);
                        imageUrls.push(url);
                    });
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
                        // 临时字段：saveImages() 落盘后就删，换成本地路径的 images。
                        // 指纹只吃 username|timestamp|content[:100]，多挂一个不影响它
                        _imageUrls: imageUrls,
                    });
                }
            } catch (e) { /* skip */ }
        });

        return { posts: results, scan };
    }, { displayPage, minSize: CONFIG.imageMinSize });

    return { posts, scan };
}

/**
 * 提取一页 + 把这页的正文图落盘。
 *
 * 指纹在这里就先算出来 —— 图片文件名要用它，而正式那轮指纹循环在浏览器关掉之后才跑。
 * makeFingerprint 是纯函数，那一轮重算得到的是同一个值。
 * 逐页落盘而不是攒到最后：响应缓存不会越堆越大，中途限流退出（退出码 2）时
 * 已抓到的那些页也保住了自己的图。
 */
async function grabPage(page, context, capture, tally, displayPage) {
    const { posts, scan } = await extractPosts(page, displayPage);
    logImageScan(scan, tally);
    posts.forEach(p => { p.fingerprint = makeFingerprint(p); });
    await saveImages(context, capture, posts, {
        mediaDir: CONFIG.mediaDir, sourceId: CONFIG.sourceId, tally,
    });
    return posts;
}

// ===== 获取总页数 =====
async function getTotalPages(page) {
    return await page.evaluate(() => {
        let maxDisplay = 1;

        const pageIndex = document.querySelector('.pageIndex');
        if (pageIndex) {
            const links = pageIndex.querySelectorAll('a');
            links.forEach(link => {
                const href = link.getAttribute('href') || '';
                const text = link.textContent.trim();

                // 从href提取URL页码（/0 = 显示1, /1 = 显示2）
                const hm = href.match(/\/forum\/list_messages\/\d+\/(\d+)/);
                if (hm) {
                    const displayPage = parseInt(hm[1]) + 1;
                    if (displayPage > maxDisplay) maxDisplay = displayPage;
                }

                const tm = text.match(/^(\d+)$/);
                if (tm) {
                    const p = parseInt(tm[1]);
                    if (p > maxDisplay) maxDisplay = p;
                }
            });

            const lastLink = pageIndex.querySelector('a[href*="last"]');
            if (lastLink) {
                if (maxDisplay <= 1) maxDisplay = 0;  // 0 = unknown, probe
            }
        }

        return maxDisplay;
    });
}

// ===== 解析最后一页URL =====
async function getLastDisplayPage(page) {
    const lastHref = await page.evaluate(() => {
        const lastLink = document.querySelector('.pageIndex a[href*="last"]');
        return lastLink ? lastLink.getAttribute('href') : null;
    });
    if (lastHref) {
        try {
            await gotoPage(page, new URL(lastHref, CONFIG.baseUrl).href, 20000);
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

// ===== 主流程 =====
async function main() {
    if (!CONFIG.threadId) throw new Error('job.params 缺少 thread_id');

    const modeStr = CONFIG.incremental ? '增量' : '全量';
    log(`🚀 Tweakers论坛采集器 (${modeStr}模式)`);
    log(`   帖子ID: ${CONFIG.threadId} | 起始页: ${CONFIG.startPage} | 模式: ${CONFIG.headless ? '无头' : '有头'}`);
    if (CONFIG.startPage > 1 && !CONFIG.incremental) {
        // 抓取循环只前进不回补，起始页之前的内容这一轮不会出现在结果里
        log(`   ⚠️ 第 1~${CONFIG.startPage - 1} 页不会被抓取`);
    }

    // 增量所需的信息由 job 下发：帖子存在 posts 表里，脚本这边没有旧文件可读。
    // 续抓页码已由 Python 算进 CONFIG.startPage
    const existingFingerprints = new Set(CONFIG.knownFingerprints);
    if (CONFIG.incremental && existingFingerprints.size) {
        log(`   增量模式: 已有 ${existingFingerprints.size} 条帖子，从第 ${CONFIG.startPage} 页开始`);
    }

    const browser = await launchBrowser(CONFIG.headless);

    const allPosts = [];
    // 站点声明的总页数：只跟随站点上调，绝不被抓取中断点覆盖——
    // 一旦被覆盖，残缺结果就会以「总共就这么多页」的姿态落盘，下游完全看不出来
    let detectedTotalPages = CONFIG.startPage;
    let lastConfirmedPage = CONFIG.startPage;
    let incomplete = false;
    let stopReason = null;
    let capture = null;
    const tally = newImageTally();
    // 建 context 也可能抛错，必须一起罩进 try —— 漏掉 browser.close() 会留下常驻的 Chrome 进程
    let context = null;

    try {
        context = await newContext(browser, { stateFile: CONFIG.stateFile, locale: 'nl-NL' });

        const page = await context.newPage();
        page.setDefaultTimeout(CONFIG.timeout);
        if (CONFIG.mediaDir) capture = attachImageCapture(page);

        log(`📄 访问: ${threadUrl(CONFIG.startPage)}`);
        await gotoPage(page, threadUrl(CONFIG.startPage), 30000);
        await page.waitForSelector('.message, .pageIndex, .forum', { timeout: 15000 }).catch(() => {});
        await sleep(randInt(1200, 3000));
        await handleConsent(page);
        log(`  当前URL: ${page.url()}`);

        // 提取第一页
        log('🔍 提取帖子...');
        let posts = await grabPage(page, context, capture, tally, CONFIG.startPage);
        allPosts.push(...posts);
        log(`  ✅ 第 ${CONFIG.startPage} 页: ${posts.length} 条帖子`);

        if (posts.length > 0) {
            log(`  📝 第1条: [${posts[0].username}] ${posts[0].timestamp} — ${posts[0].content.substring(0, 100)}...`);
        } else if (existingFingerprints.size === 0) {
            // 一条都没拿到又没有历史数据，继续走下去会写出一份 complete:true 的空结果，
            // 下游会把它当成「这个帖子本来就是空的」照常翻译、导出
            throw new Error(`第 ${CONFIG.startPage} 页未提取到任何帖子（可能被目标站拦截或页面结构已变化）`);
        } else {
            // 增量模式下起始页是 maxPage+1，超出末尾时本来就该是空的——这是「没有新回帖」，
            // 不是残缺，历史数据完好，complete 仍然为 true
            log(`  起始页无新帖，已有 ${existingFingerprints.size} 条历史数据`);
        }

        // 检测总页数
        detectedTotalPages = await getTotalPages(page);
        log(`📊 分页检测: ${detectedTotalPages > 0 ? detectedTotalPages + ' 页' : '需要探测'}`);

        if (detectedTotalPages <= 0) {
            const lastPage = await getLastDisplayPage(page);
            if (lastPage) {
                detectedTotalPages = lastPage;
                log(`  最后一页: ${detectedTotalPages}`);

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
                    const testPosts = await grabPage(page, context, capture, tally, nextPage);
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

        progress(lastConfirmedPage, detectedTotalPages, `已抓取第 ${lastConfirmedPage}/${detectedTotalPages} 页`);

        // 逐页抓取
        let pagesUntilRest = randInt(8, 15);
        for (let dp = lastConfirmedPage + 1; dp <= detectedTotalPages; dp++) {
            await humanDelay(CONFIG.delayMin, CONFIG.delayMax);
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

                const newPosts = await grabPage(page, context, capture, tally, dp);
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

                progress(dp, detectedTotalPages, `已抓取第 ${dp}/${detectedTotalPages} 页`);
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
        logImageTally(tally);
        if (context) await saveStorageState(context, CONFIG.stateFile);
        await browser.close();
        log('🔒 浏览器已关闭');
    }

    // ===== 后处理：指纹生成 + 去重 + 合并 =====
    for (const post of allPosts) {
        post.fingerprint = makeFingerprint(post);
        post._processed = post._processed || { translated: false, sentiment_at: null };
    }

    // 只输出本轮抓到的：历史数据在 posts 表里，合并由 Python 侧的 upsert 完成
    // （它会保住已有帖子的 translation 和 _processed 标记）
    const seenThisRound = new Set();
    const freshPosts = [];
    for (const p of allPosts) {
        // 翻页期间帖子总数变化会让同一条回帖跨页出现两次，同一批里必须先自去重
        if (seenThisRound.has(p.fingerprint)) continue;
        seenThisRound.add(p.fingerprint);
        if (CONFIG.incremental && existingFingerprints.has(p.fingerprint)) continue;
        freshPosts.push(p);
    }
    if (CONFIG.incremental) {
        log(`   增量: 本轮提取 ${allPosts.length} 条，其中新增 ${freshPosts.length} 条`);
    }

    // ===== 保存 =====
    const uniqueUsers = new Set(freshPosts.map(p => p.username));
    const result = {
        thread_id: CONFIG.threadId,
        thread_url: `${CONFIG.baseUrl}/forum/list_messages/${CONFIG.threadId}/`,
        total_pages: detectedTotalPages,
        total_posts: freshPosts.length,
        pages_fetched: new Set(allPosts.map(p => p.page_number)).size,
        unique_users: uniqueUsers.size,
        complete: !incomplete,
        stop_reason: stopReason,
        extracted_at: new Date().toISOString(),
        posts: freshPosts,
    };

    writeOutput(job, result);

    // 数据照常落盘（增量模式下轮可从 maxPage+1 续抓），但退出码必须说实话
    if (incomplete) {
        console.error(stopReason);
        process.exitCode = allPosts.length > 0 ? 2 : 1;
        log(`\n⚠️ 抓取未完成: ${stopReason}`);
    }

    log(`${'='.repeat(60)}`);
    log(`📊 采集完成! 总页数 ${result.total_pages} | 总帖子 ${result.total_posts} | 用户 ${result.unique_users}`);
    log(`   文件: ${CONFIG.outputFile}`);
    log(`${'='.repeat(60)}`);

    return result;
}

// 退出码约定：0 完整 / 1 硬失败（无可用数据）/ 2 部分完成（数据已落盘，可增量续抓）
main().catch(e => {
    console.error(e && e.stack ? e.stack : String(e));
    process.exit(1);
});
