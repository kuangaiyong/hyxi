const fs = require('fs');
const path = require('path');
const { log } = require('./job');
const { sleep } = require('./human');

// 单张图上限。超过就跳过 —— 图片是附加信息，不值得为一张大图拖慢整轮抓取
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
// 响应缓存总量上限。长帖滚下来能有几百张图，不设上限会把 Node 堆吃光
const MAX_CACHE_BYTES = 64 * 1024 * 1024;
// 回源之间的间隔。页面已经加载过这些图，但集中回源仍是一次突发
const REFETCH_GAP_MS = 300;

function short(url) {
    return String(url).replace(/\?.*$/, '').slice(0, 100);
}

function hostOf(url) {
    try { return new URL(url).host; } catch (e) { return '?'; }
}

function extOf(contentType, url) {
    const ct = (contentType || '').toLowerCase();
    if (ct.includes('png')) return 'png';
    if (ct.includes('webp')) return 'webp';
    if (ct.includes('gif')) return 'gif';
    const m = url.match(/\.(jpe?g|png|webp|gif)(\?|$)/i);
    return m ? m[1].toLowerCase().replace('jpeg', 'jpg') : 'jpg';
}

/**
 * 把浏览器**已经下载过**的图片字节留下来，供随后落盘。
 *
 * **不要为页面上已经渲染出来的图再发一次请求。** `context.request` 是 Playwright 在
 * Node 进程里自带的 HTTP 客户端，**不是 Chrome**：它不读代理环境变量（实测
 * playwright-core 整个包里 HTTPS_PROXY 出现 0 次），browser.js 也从没给它传过 proxy。
 * 于是「Chrome 走系统代理把图显示出来了、脚本直连回源却连不上」是完全可能的状态 ——
 * 页面抓得到、图一张都下不来，正是用户实测到的现象。而本地 fixture 打的是 127.0.0.1，
 * 代理对 localhost 一律不生效，所以那条路在测试里永远是绿的，bug 因此逃逸到了用户那里。
 *
 * 走响应缓存则与页面同一条网络栈、同一套 cookie、同一份指纹，且**请求总数减半**
 * （原先是「页面加载一次 + 回源一次」），与反爬虫姿态一致。
 */
function attachImageCapture(page) {
    const cache = new Map();   // 最终响应 URL -> { buf, contentType }
    // 原始请求 URL -> 最终响应 URL。图片被 302 时，DOM 上的 src 是原始那个，
    // 而 response 事件报的是最终那个 —— 不建这层别名，命中率直接归零
    const alias = new Map();
    const inflight = new Set();
    let bytes = 0;
    const stats = { captured: 0, evicted: 0 };

    page.on('response', (resp) => {
        let contentType = '';
        try {
            if (resp.request().resourceType() !== 'image') return;
            if (!resp.ok()) return;
            contentType = (resp.headers()['content-type'] || '').toLowerCase();
            if (!contentType.startsWith('image/')) return;
        } catch (e) {
            return;
        }
        const url = resp.url();
        if (cache.has(url)) return;
        try {
            let r = resp.request().redirectedFrom();
            while (r) { alias.set(r.url(), url); r = r.redirectedFrom(); }
        } catch (e) { /* 拿不到重定向链就只按最终 URL 建键 */ }
        // body() 是异步的，且对重定向之类的响应会直接抛错 —— 一律吞掉：
        // 缓存没命中会回落到回源，不该因为一张图让整轮抓取出声
        const p = resp.body().then((buf) => {
            if (!buf || !buf.length || buf.length > MAX_IMAGE_BYTES) return;
            // 覆盖同一个键之前必须把旧的字节数减掉。上面那个 cache.has() 只挡得住
            // 前一次 body() 已经 resolve 的情况，两次重叠的读取会双计 —— 而 bytes
            // 一旦漂过上限，下面这个驱逐循环每插一张就把整个缓存清空一次
            // （它只在 cache.size 归零时才退出），于是所有图片静默退回到回源那条路，
            // 正是这次改动要摆脱的那条
            const prev = cache.get(url);
            if (prev) { bytes -= prev.buf.length; cache.delete(url); }
            while (bytes + buf.length > MAX_CACHE_BYTES && cache.size) {
                const oldest = cache.keys().next().value;
                bytes -= cache.get(oldest).buf.length;
                cache.delete(oldest);
                stats.evicted++;
            }
            cache.set(url, { buf, contentType });
            bytes += buf.length;
            stats.captured++;
        }).catch(() => {}).then(() => { inflight.delete(p); });
        inflight.add(p);
    });

    return {
        // **读了不删**：同一个 URL 在一批里出现两次是常事（同一张图配在两条帖子上），
        // 删掉的话第二次必然落空、白跑一次回源，还会多写一个文件。
        // 内存由上面那个按总量驱逐兜住，不需要靠读后即删来收
        get(url) {
            return cache.get(url) || cache.get(alias.get(url)) || null;
        },
        /** 等已经收到的响应体读完。滚动刚停就落盘时，body() 可能还在路上 */
        async settle() {
            for (let i = 0; i < 3 && inflight.size; i++) {
                await Promise.all([...inflight]);
            }
        },
        stats() { return { ...stats, held: cache.size }; },
    };
}

/**
 * 提取阶段的战报。
 *
 * 「页面上就没有图」「选择器没选中」「被尺寸过滤掉了」「下载失败」四种情况在界面上
 * 长得一模一样：什么都不显示。不把候选数和排除原因报出来，远端只能靠猜。
 */
/**
 * 全程累计。**候选和命中按 URL 去重**：信息流每一批都会把上一批已加载的帖子
 * 重新提取一遍（见 CLAUDE.md），逐批累加的话 10 批下来汇总行会显示成
 * 「候选 220 · 保存 40」，看着像丢了 82%，而实际一张没丢 —— 这行恰恰是
 * 这次改动最主要的诊断输出，它自己说谎就白做了。
 * saved / failed 是按「本批新增」算的，天然不重复，用普通计数即可。
 */
function newImageTally() {
    return { candidates: new Set(), accepted: new Set(), saved: 0, failed: 0, cached: 0 };
}

function logImageScan(scan, tally) {
    if (!scan) return;
    const candidates = scan.candidates || [];
    const accepted = scan.accepted || [];
    if (tally) {
        candidates.forEach((u) => tally.candidates.add(u));
        accepted.forEach((u) => tally.accepted.add(u));
    }
    if (!candidates.length) return;
    if (accepted.length === candidates.length) {
        log(`   图片：页面上命中 ${accepted.length} 张`);
        return;
    }
    log(`   图片：页面上 ${candidates.length} 张候选，命中 ${accepted.length} 张，`
        + `排除 ${candidates.length - accepted.length} 张`);
    (scan.rejected || []).slice(0, 3).forEach((r) => {
        log(`     ↳ 排除(${r.why}) ${r.w}x${r.h} ${hostOf(r.url)} ${short(r.url)}`);
    });
}

async function fetchOne(context, capture, url, errors) {
    const hit = capture ? capture.get(url) : null;
    if (hit) return { buf: hit.buf, contentType: hit.contentType, cached: true };
    // 缓存没命中才回源：页面没真正加载过这张图（懒加载没触发、或直接命中了 Chrome
    // 的内存缓存因而没有 response 事件），或者响应体读不出来。这条路走的是上面
    // 说的那个 Node 侧客户端，不经系统代理 —— 保留它只是为了不制造新的回归面
    try {
        const resp = await context.request.get(url, { timeout: 30000 });
        if (!resp.ok()) {
            if (errors.length < 3) errors.push(`回源 HTTP ${resp.status()} ${short(url)}`);
            return null;
        }
        const buf = await resp.body();
        if (buf.length > MAX_IMAGE_BYTES) {
            if (errors.length < 3) errors.push(`超过 ${MAX_IMAGE_BYTES} 字节已跳过 ${short(url)}`);
            return null;
        }
        return { buf, contentType: resp.headers()['content-type'], cached: false };
    } catch (e) {
        // **报错原文必须留下**：ECONNREFUSED / ETIMEDOUT / 407 指向完全不同的处置，
        // 吞成一个计数就等于什么都没说 —— 这正是这个问题拖到用户那边才暴露的原因
        if (errors.length < 3) {
            errors.push(`回源失败 ${String(e.message).split('\n')[0].slice(0, 120)}`);
        }
        return null;
    }
}

/**
 * 把 `post._imageUrls` 里的图落到 `<mediaDir>/<sourceId>/` 下，`images` 存相对路径。
 *
 * **不能只存站点的原始链接**：fbcdn 之类的 URL 带签名和过期时间，几天后页面上就是
 * 一片裂图，而舆情报告本来就是要回溯的。存相对路径而不是绝对路径，数据才能跨机器搬。
 *
 * 单张失败只跳过这一张，帖子照常入库 —— 图片是附加信息，不该把整条拖失败。
 */
async function saveImages(context, capture, posts, { mediaDir, sourceId, tally }) {
    if (!mediaDir) {
        posts.forEach((p) => { delete p._imageUrls; });
        return;
    }
    const dir = path.join(mediaDir, sourceId);
    if (capture) await capture.settle();

    let wanted = 0;
    let saved = 0;
    let failed = 0;
    let cached = 0;
    const errors = [];

    for (const post of posts) {
        const urls = post._imageUrls || [];
        delete post._imageUrls;
        if (!urls.length) continue;
        wanted += urls.length;
        const rels = [];
        for (let i = 0; i < urls.length; i++) {
            const got = await fetchOne(context, capture, urls[i], errors);
            // 走了回源就得留间隔 —— 请求节奏是反爬纪律，谁都不能改。
            // 缓存命中那条路没发请求，不需要等
            if (!got || !got.cached) await sleep(REFETCH_GAP_MS);
            if (!got) { failed++; continue; }
            try {
                fs.mkdirSync(dir, { recursive: true });
                const name = `${post.fingerprint}_${i}.${extOf(got.contentType, urls[i])}`;
                fs.writeFileSync(path.join(dir, name), got.buf);
                rels.push(`${sourceId}/${name}`);
                saved++;
                if (got.cached) cached++;
            } catch (e) {
                failed++;
                if (errors.length < 3) errors.push(`落盘失败 ${e.message}`);
            }
        }
        if (rels.length) post.images = rels;
    }

    if (tally) {
        tally.saved += saved;
        tally.failed += failed;
        tally.cached += cached;
    }
    // 这一批没有配图就不出声 —— 长帖逐页喊一遍会把日志刷没。
    // 「全程一张都没有」由 logImageTally() 的汇总行兜住
    if (!wanted) return;
    log(`   图片：保存 ${saved}/${wanted} 张（${cached} 张取自浏览器缓存）`
        + (failed ? `，失败 ${failed} 张` : ''));
    errors.forEach((e) => log(`     ↳ ${e}`));
}

/**
 * 一轮跑完必出的一行汇总。
 *
 * **零张图这种情况恰恰不能沉默** —— 它正是用户报上来的现象。逐页那几行只在页面上
 * 真有候选时才打，全程一张候选都没有的话反而什么都看不到，于是「这个帖子本来就没配图」
 * 和「提取器失效了」在日志上又变得一模一样。
 */
function logImageTally(tally) {
    if (!tally) return;
    // 前两个是**去重后的地址数**、后两个是**文件数**，措辞必须区分开：
    // 同一张图配在两条帖子上时会出现「通过筛选 1 个、落盘 2 张」，
    // 写成「命中 1 张 · 保存 2 张」看着像自相矛盾
    log(`   图片汇总：候选图片地址 ${tally.candidates.size} 个 · `
        + `通过筛选 ${tally.accepted.size} 个 · `
        + `落盘 ${tally.saved} 张（${tally.cached} 张取自浏览器缓存）· 失败 ${tally.failed} 张`);
}

module.exports = {
    attachImageCapture, saveImages, logImageScan, newImageTally, logImageTally,
    MAX_IMAGE_BYTES,
};
