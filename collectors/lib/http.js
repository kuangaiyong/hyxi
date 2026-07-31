const { sleep } = require('./human');
const { log } = require('./job');

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

module.exports = { gotoPage };
