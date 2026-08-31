const { sleep } = require('./human');
const { log } = require('./job');

// 网络层重试。**这跟限流退让是两回事**：限流是拿到了响应、对方明确说停；这里是
// 一次响应都没拿到，站点可能根本没事，只是路上抖了一下
const NETWORK_RETRIES = 2;
const NETWORK_BACKOFF_MS = 5000;

// Chrome 对**没有正文**的 4xx/5xx 不返回 Response，而是直接抛
// net::ERR_HTTP_RESPONSE_CODE_FAILURE（实测 503 + Content-Length: 0 就是这样）。
// 那是「服务器答复了一个错误状态」，不是网络失败 —— 当成网络失败重试，等于对着
// 一个可能正在限流的站点多打几次，正是「限流即停」要避免的
const RESPONDED_WITH_ERROR = /ERR_HTTP_RESPONSE_CODE_FAILURE/;

// page.goto 自己抛出来的（导航超时、连接被重置、DNS 解析不了）是「一次响应都没拿到」。
// 这类要重试：一次超时就判死的话，跨境线路抖一下就足以让整轮采集失败，而站点其实
// 好好的。用户实测报过——首个页面 goto 超时 30s → 退出码 1 → 整个任务失败。
async function gotoTolerant(page, url, timeout, retries) {
    for (let attempt = 0; ; attempt++) {
        try {
            return await page.goto(url, { waitUntil: 'domcontentloaded', timeout });
        } catch (e) {
            const msg = String((e && e.message) || e);
            if (RESPONDED_WITH_ERROR.test(msg) || attempt >= retries) throw e;
            const waitMs = NETWORK_BACKOFF_MS * (attempt + 1);
            // 只取首行：Playwright 的报错后面跟着一整段 Call log，全打出来会把日志淹掉
            const why = msg.split('\n')[0];
            log(`  ⏳ 访问失败（${why}），${waitMs / 1000}s 后重试第 ${attempt + 1}/${retries} 次`);
            await sleep(waitMs);
        }
    }
}

// 收到限流就退让一次，仍被拒绝即停止抓取——不做指数重试链，对方说停就停。
//
// **网络重试必须包在限流循环的里面，不能反过来**：包在外面的话，退让后的那一次要是
// goto 抛了异常，整个限流循环会从头再走一遍 —— 一个已经说「别打了」的站点会被连打
// 3 轮、每轮还睡满一次 Retry-After（最坏 6 个请求 + 15 分钟），正是这段注释声称
// 要避免的事。
//
// 同理，站点一旦说过一次「别打了」，退让后的那一次就不再容忍网络抖动（retries 传 0）：
// 连不上就是连不上，不该再对它多打几次。于是限流路径上最多 4 个请求、其中真正拿到
// 响应的仍然只有 2 个，「退让一次即停」这条约束一个字没松。
async function gotoPage(page, url, timeout) {
    let throttled = false;
    for (let attempt = 0; attempt < 2; attempt++) {
        const resp = await gotoTolerant(page, url, timeout, throttled ? 0 : NETWORK_RETRIES);
        const status = resp ? resp.status() : 0;
        if (status !== 429 && status !== 403 && status !== 503) return resp;
        throttled = true;
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

module.exports = { gotoPage };
