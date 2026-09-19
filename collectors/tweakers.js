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
    // 而 Tweakers 的真实页面本机访问不到，宁可多收几张让日志报出来，也不要静默漏掉。
    // 2026 真站实测：正文真图渲染 369x800 ~ 800x600，表情是 16x16 / 34x16 / 30x17，
    // 80 这条线两边都留了足够余量，没有真图落在它下面
    imageMinSize: 80,
    // 增量跑（从 max_page_number + 1 起抓）时看不到第 1 页，主题指纹由 Python 从库里下发。
    // 拿不到又没抓第 1 页时**不猜**，见 markTopicAndReplies()
    topicFingerprint: job.topic_fingerprint || '',
};

// 本轮的「主题」（帖子发起帖）指纹。grabPage() 抓到显示第 1 页时定下来，
// 后处理阶段拿它给全部楼层写父指针
let topicFingerprint = CONFIG.topicFingerprint;

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
        // 引用块的真实标记（2026 真站实测）：<blockquote> 是 .messagecontent 的**直接子元素**，
        // 里面包一个 .message-quote-div。**旧版那串 `.quote / .bb_quote / .cite / .quotetext`
        // 在真站上一个都不存在**（实测计数全 0）—— 它们是照着 fixture 猜的，而那份 fixture
        // 里的引用块 DOM 同样是猜的（`.cite` 这个 span 真站没有）。照它写选择器，测试全绿，
        // 真站上却一段引用都收不到。
        const QUOTE_SEL = 'blockquote, .message-quote-div';
        const scan = { candidates: [], accepted: [], rejected: [], quoteExtra: 0, skippedNoId: 0 };
        const results = [];
        const msgBlocks = document.querySelectorAll('.message[data-message-id]');
        // 每一条 `.message` 都该带 data-message-id（真站 3 页实测 203/203）。少了就说明
        // 这个选择器收窄出了问题，那些楼层会被**静默跳过** —— 页面上完全看不出来
        scan.skippedNoId = document.querySelectorAll('.message').length - msgBlocks.length;

        /**
         * 读一个引用块。**引用块是独立的一份数据，绝不许并进正文** ——
         * 指纹吃 `用户名|时间戳|正文前100字`，并进去全部历史数据失配、已翻译的帖子会被
         * 重新付费翻译。原站本来也是「引用框 + 正文」两块。
         *
         * 真站实测的三条形态：
         *  · 引用行是 `<b><a class="messagelink" href="…/list_message/<id>#<id>">张三 schreef
         *    op …</a>:</b>` —— **被引用楼层的 id 就在 href 里**，引用关系有精确的 join key
         *  · 长引用（`.large-quote`）的正文被**服务端**截断，中间插一个字面量 `[...]`；
         *    点 `.toggle-quote` 只加一个 CSS class，文本一个字符都不变、0 个新 XHR —— 点不开
         *  · 被引用的图**不是 `<img>`**，是一个 `<a href="…原图…">[Afbeelding]</a>` 锚点，
         *    所以「`<img>` + 渲染尺寸」那条路永远收不到它
         */
        function readQuote(bq) {
            const div = bq.querySelector('.message-quote-div') || bq;
            const link = div.querySelector('a.messagelink');
            let cite = '';
            let quotedId = '';
            if (link) {
                cite = link.textContent.trim();
                const m = (link.href || '').match(/list_message\/(\d+)/);
                if (m) quotedId = m[1];
            }

            const imageUrls = [];
            // 图必须在**原始元素**上量尺寸：下面那个 clone 游离于文档之外，
            // getBoundingClientRect() 一律返回 0，照着它取会把每张图都过滤掉
            div.querySelectorAll('img').forEach(im => {
                const url = im.currentSrc || im.src || '';
                if (!url || url.startsWith('data:')) return;
                scan.candidates.push(url);
                const r = im.getBoundingClientRect();
                if (r.width < minSize || r.height < minSize) {
                    scan.rejected.push({ why: '引用块尺寸', url,
                                         w: Math.round(r.width), h: Math.round(r.height) });
                    return;
                }
                scan.accepted.push(url);
                imageUrls.push(url);
            });
            // [Afbeelding] 锚点：真站上被引用的照片只以这个形态出现。它不是 <img>，
            // 没有渲染尺寸，所以只能按「锚点文字就是 [Afbeelding]」认 —— 引用块里
            // 别的 <a>（指向别的页面、用户相册）不是图，不能误收
            div.querySelectorAll('a[href]').forEach(a => {
                if ((a.textContent || '').trim() !== '[Afbeelding]') return;
                const url = a.href || '';
                if (!url || url.startsWith('data:')) return;
                scan.candidates.push(url);
                scan.accepted.push(url);
                imageUrls.push(url);
            });

            // 正文用克隆：剔掉引用行（<b> 里那个 messagelink）与 toggle-quote
            const clone = div.cloneNode(true);
            clone.querySelectorAll('.toggle-quote').forEach(el => el.remove());
            const cloneLink = clone.querySelector('a.messagelink');
            if (cloneLink) {
                const b = cloneLink.closest('b');
                (b || cloneLink).remove();
            }
            const content = clone.textContent
                .replace(/\s+/g, ' ')
                .replace(/\[Afbeelding\]/gi, '[图片]')
                .replace(/&nbsp;/g, ' ')
                .trim();

            // 引用行的文字形如「Storms schreef op zaterdag 23 mei 2026 @ 12:01」，
            // 作者是 " schreef op" 之前那一段。**只当兜底**：被引用楼层采到时，
            // 出口一律用那条楼层自己的用户名与时间（这里不解析荷兰语日期）
            const um = cite.match(/^(.*?)\s+schreef\s+op\s/i);
            return {
                message_id: quotedId,
                cite: cite,
                username: um ? um[1].trim() : '',
                content: content,
                truncated: div.classList.contains('large-quote') && content.includes('[...]'),
                _imageUrls: imageUrls,   // saveImages() 落盘后就删
            };
        }

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
                        clone.querySelectorAll(QUOTE_SEL).forEach(el => el.remove());
                        content = clone.textContent.trim();
                    }
                }

                // 引用块。**先读它再读正文图**：引用块里的图归被引用者，
                // 正文图那一轮要按 QUOTE_SEL 把它们跳过，谁收谁不收必须只有一个说法
                let quote = null;
                if (contentEl) {
                    const bqs = Array.prototype.filter.call(
                        contentEl.children, el => el.tagName === 'BLOCKQUOTE');
                    // 真站上一条帖子最多一个引用块（实测 97/97），多出来的只取第一个并记账：
                    // 静默丢掉一段引用，页面上完全看不出来
                    if (bqs.length > 1) scan.quoteExtra += bqs.length - 1;
                    if (bqs[0]) quote = readQuote(bqs[0]);
                }

                // 正文图。**必须在原始元素上量尺寸**：上面那个 clone 游离于文档之外，
                // getBoundingClientRect() 会一律返回 0，照着 clone 取等于把每张图都
                // 过滤掉。**不设 host 白名单**：Tweakers 的正文图在 tweakers.net/i/ 下，
                // 但写死 host 是赌站点不改，这里只按「在正文容器内」+「非 data: URI」
                // +「渲染尺寸」筛，其余交给日志。
                // 取图的容器要跟着正文走：没有 .messagecontent 时正文来自 .post，
                // 图自然也在那里 —— 盯死 contentEl 会让这条路上的帖子静默丢图。
                const imageUrls = [];
                const imgRoot = contentEl || block.querySelector('.post');
                if (imgRoot) {
                    imgRoot.querySelectorAll('img').forEach(im => {
                        // 引用块里的图已经由 readQuote() 收走、归被引用者了。
                        // 这里**不能再记一次排除** —— 那不是丢掉，是归了别人，
                        // 记账说成「排除」会让「图片汇总」那行谎报丢失
                        if (im.closest(QUOTE_SEL)) return;
                        const url = im.currentSrc || im.src || '';
                        if (!url || url.startsWith('data:')) return;   // 界面图标，不算候选
                        scan.candidates.push(url);
                        const r = im.getBoundingClientRect();
                        const rej = (why) => scan.rejected.push({
                            why, url, w: Math.round(r.width), h: Math.round(r.height),
                        });
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
                        // 引用块单独一份，**绝不并进 content**（指纹）。没有引用就是 null，
                        // 出口按 null 决定不渲染，不需要判断来源
                        quote: quote,
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
 * 提取一页 + 把这页的图落盘（正文图与引用图分开）。
 *
 * 指纹在这里就先算出来 —— 图片文件名要用它，而正式那轮指纹循环在浏览器关掉之后才跑。
 * makeFingerprint 是纯函数，那一轮重算得到的是同一个值。
 * 逐页落盘而不是攒到最后：响应缓存不会越堆越大，中途限流退出（退出码 2）时
 * 已抓到的那些页也保住了自己的图。
 */
async function grabPage(page, context, capture, tally, displayPage) {
    const { posts, scan } = await extractPosts(page, displayPage);
    logImageScan(scan, tally);
    if (scan.skippedNoId) {
        log(`   ⚠️ 有 ${scan.skippedNoId} 条 .message 没有 data-message-id，这一页漏掉了它们`);
    }
    if (scan.quoteExtra) {
        log(`   ⚠️ 有 ${scan.quoteExtra} 处多余引用块只取了第一个 —— 一条帖子按理只有一个`);
    }
    posts.forEach(p => { p.fingerprint = makeFingerprint(p); });
    // 主题（帖子发起帖）= **显示第 1 页的第一条楼层**。**不能用 `.message.topicstarter`**：
    // 真站实测那个 class 标的是「这条是发起人写的」，发起人在第 1 页就有 15 条楼层
    if (!topicFingerprint && displayPage === 1 && posts.length) {
        topicFingerprint = posts[0].fingerprint;
        log(`   🧵 主题（第 1 页第一条）: [${posts[0].username}] ${posts[0].timestamp} — `
            + `${posts[0].content.substring(0, 60)}...`);
    }
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
//
// 末页链接真站是 `class="lastpage"`（实测：`<a href="…/2" class="lastpage">3</a>`），
// **不是** href 里带 "last"。旧版写的 `a[href*="last"]` 永远匹配不到，等于这条路一直是死的
// —— 只是 getTotalPages() 从页码链里算得出总页数，所以没暴露出来。
async function getLastDisplayPage(page) {
    const lastHref = await page.evaluate(() => {
        const lastLink = document.querySelector('.pageIndex a.lastpage')
            || document.querySelector('.pageIndex a[href*="last"]');
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

/**
 * 给全部楼层写父子关系：**一个数据源就是一串 —— 一条主题 + 按时间平铺的回复**。
 *
 * 改造前这里一个字段都不写，于是 140 条楼层全落成 `reply_level=0`、父指针为空，
 * `build_tree()` 得到 140 个 root：结果页 140 张卡、每张标「主贴」、下面一条回复都没有，
 * Excel 的「层级」列全是 0，「只看新回复」和按串给的舆情上下文一起失效。
 *
 * **回复一律是主题的第 1 层**，不嵌套 —— Tweakers 没有「回复的回复」这一层，
 * 表达「我在回谁」靠的是引用块，而引用是另一份数据（`post.quote`），不是父指针。
 *
 * 主题拿不到时**不猜**：猜错会让新楼层挂到一个不相干的楼层上，页面看着像对的。
 * 存量库（140 条并列主贴）就是这种状态，所以这里必须把原因和解法都说出来。
 */
function markTopicAndReplies(posts) {
    if (!topicFingerprint) {
        log('   ⚠️ 本轮确定不了主题：没抓第 1 页，库里也没有可用的历史主题指纹');
        log('   ↳ 这批楼层先按「主贴」呈现，父子关系留给下一次全量跑');
        if (CONFIG.incremental && CONFIG.startPage > 1) {
            // 存量库的典型样子：改造前采的全是并列主贴，root 不唯一。全量重跑一次就修好
            // （Python 侧只认「root 恰好一条」，两条以上一律不下发主题指纹）
            log('   ↳ 若这个数据源的历史数据是改造前的平铺结构，请对它的任务点一次「全量重跑」');
        }
        return;
    }
    let topics = 0;
    for (const post of posts) {
        if (post.fingerprint === topicFingerprint) {
            post.parent_fingerprint = null;
            post.reply_level = 0;
            topics++;
        } else {
            post.parent_fingerprint = topicFingerprint;
            post.reply_level = 1;
        }
    }
    if (!topics && posts.length) {
        // 主题这一轮不在结果里（增量只抓到新楼层）—— 正常，父指针照样指向它
        log(`   🧵 主题不在本轮结果里（库里的历史楼层），本轮 ${posts.length} 条楼层全挂在它下面`);
    }
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

    // ===== 后处理：指纹生成 + 父子关系 + 去重 + 合并 =====
    for (const post of allPosts) {
        post.fingerprint = makeFingerprint(post);
        post._processed = post._processed || { translated: false, sentiment_at: null };
    }
    markTopicAndReplies(allPosts);

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
