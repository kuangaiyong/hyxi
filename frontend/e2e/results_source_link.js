/**
 * 结果页主贴【🔗 原帖】链接的冒烟测试 —— 真 Chrome、真前端、真后端，无 mock。
 *
 * 钉住四件事：
 *   1. Facebook 主贴上真的渲染出了链接
 *   2. href 是 Facebook 自己的 permalink 形态，且末尾那串数字就是这条帖子的 message_id
 *   3. **回复贴一条都不许有** —— 用户明确只要主贴
 *   4. target=_blank + rel 里同时有 noopener 和 noreferrer
 *
 * 第 3 条是这个脚本存在的主要理由：把链接错加到回复上，页面照样"能用"，
 * 只是每条回复后面多一个点进去指向它父贴的链接 —— 靠肉眼扫列表发现不了。
 *
 * 不做的事：**不去真的打开那个链接**。这台机器不访问 Facebook，而且自动化访问
 * 违反其服务条款（见 CLAUDE.md）。href 的正确性靠和后端 /posts 出口的 message_id
 * 逐条对账来证明，比点开一次更强 —— 点开只能证明一条。
 *
 * 前端没有单元测试框架，理由见 results_filters.js。需要前后端都起着：
 *
 *   .\start.ps1                        # 先把两个服务拉起来
 *   node frontend/e2e/results_source_link.js
 *   # 或： cd frontend; npm run e2e:link
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = 'http://localhost:5173';
const BACKEND = 'http://127.0.0.1:8000';
const LINK = 'a[data-testid="source-link"]';

/** 密钥从项目根 .env 取 —— 与后端读的是同一份，不必在这里再配一遍 */
function apiKey() {
    try {
        const m = readFileSync(join(ROOT, '.env'), 'utf-8').match(/^TWEAKERS_API_KEY=(.*)$/m);
        return (m && m[1].trim()) || '';
    } catch {
        return '';   // 没配密钥时后端放行，空串照样能用
    }
}

const KEY = apiKey();
const api = async (path) => {
    const r = await fetch(BACKEND + path, { headers: KEY ? { 'X-API-Key': KEY } : {} });
    if (!r.ok) throw new Error(`GET ${path} -> HTTP ${r.status}`);
    return r.json();
};

async function preflight() {
    try {
        await api('/api/health');
    } catch (e) {
        console.error(`后端 ${BACKEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        process.exit(2);
    }
    try {
        const r = await fetch(FRONTEND);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        console.error(`前端 ${FRONTEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        process.exit(2);
    }
}

/** 找一个「至少有一条主贴带 source_url」的任务。写死 ID 换台机器就跑不了 */
async function pickTask() {
    const tasks = await api('/api/v1/tasks');
    for (const t of (Array.isArray(tasks) ? tasks : tasks.tasks || [])) {
        let data;
        try {
            data = await api(`/api/v1/tasks/${t.id}/posts?page=1&page_size=50`);
        } catch {
            continue;
        }
        const roots = data.posts || [];
        const withUrl = roots.filter(p => p.source_url);
        if (withUrl.length) return { id: t.id, roots, withUrl };
    }
    return null;
}

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail) console.log(`       ${JSON.stringify(detail)}`);
    }
}

async function main() {
    await preflight();
    const task = await pickTask();
    if (!task) {
        console.error('没有「主贴带 source_url」的任务可测。先采一次 Facebook 数据源再来。');
        process.exit(2);
    }
    console.log(`任务 ${task.id.slice(0, 8)}（首页 ${task.roots.length} 条主贴，`
        + `${task.withUrl.length} 条带链接）`);

    console.log('\n场景一：后端出口的链接形态');
    for (const p of task.withUrl.slice(0, 3)) {
        check(`#${p.index} 是 permalink 形态且带自己的 message_id`,
            /^https?:\/\/[^/]+\/groups\/\d+\/permalink\/\d+\/$/.test(p.source_url),
            p.source_url);
    }
    // 回复贴一条都不许有 —— 这一条在 API 层先验一遍，再到页面上验一遍
    const replies = task.roots.flatMap(p => p.replies || []);
    check('出口里回复贴的 source_url 全是空串',
        replies.length > 0 && replies.every(r => !r.source_url),
        { 回复数: replies.length, 有链接的: replies.filter(r => r.source_url).map(r => r.index) });

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await (await browser.newContext()).newPage();
    await page.addInitScript(k => localStorage.setItem('hyxi_api_key', k), KEY);
    try {
        await page.goto(`${FRONTEND}/tasks/${task.id}/results`, { waitUntil: 'networkidle' });
        await page.waitForSelector('.thread', { timeout: 20000 });

        console.log('\n场景二：页面上真的渲染出来了');
        const shown = await page.evaluate((sel) => [...document.querySelectorAll(sel)].map(a => ({
            href: a.getAttribute('href'),
            target: a.getAttribute('target'),
            rel: a.getAttribute('rel'),
            text: a.textContent.trim(),
            // 这个链接必须落在**主贴头部**。挂到回复行上页面看着一样正常
            inHead: !!a.closest('.thread-head'),
            inReply: !!a.closest('.replies'),
        })), LINK);

        check('至少渲染出一个链接', shown.length > 0, { 期望: task.withUrl.length });
        check('数量和出口里带链接的主贴数一致', shown.length === task.withUrl.length,
            { 页面: shown.length, 出口: task.withUrl.length });
        check('每个都在主贴头部，没有一个落在回复区',
            shown.every(a => a.inHead && !a.inReply),
            shown.filter(a => !a.inHead || a.inReply));
        check('href 和出口给的逐条对得上',
            shown.every(a => task.withUrl.some(p => p.source_url === a.href)),
            shown.map(a => a.href).slice(0, 3));
        check('文案是「🔗 原帖」', shown.every(a => a.text.includes('原帖')),
            shown.map(a => a.text).slice(0, 3));

        console.log('\n场景三：新标签页打开 + 断掉 opener');
        check('target=_blank', shown.every(a => a.target === '_blank'));
        // noopener 断掉新页面对 window.opener 的引用；noreferrer 不把本机地址带出去
        check('rel 同时有 noopener 和 noreferrer',
            shown.every(a => (a.rel || '').includes('noopener')
                && (a.rel || '').includes('noreferrer')),
            shown.map(a => a.rel).slice(0, 3));
    } finally {
        await browser.close();
    }
}

// 汇总和退出码留在 main() 外面，理由同 results_filters.js：里面有提前 return 的
// 分支，写在末尾那条路会跳过 process.exit()，失败的一次会被当成通过
main().then(() => {
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exit(failures === 0 ? 0 : 1);
}).catch(e => {
    console.error('\n跑挂了:', e.message);
    process.exit(1);
});
