/**
 * Tweakers 的串与引用框的冒烟测试 —— 真 Chrome、真前端、真后端，无 mock。
 *
 * 钉住（v1.13.0，规格见 docs/features/tweakers-thread-and-quotes.md 的 R1/R4）：
 *   1. 一个来源就是一串：**只有一张卡**，徽标是「主题」不是「主贴」，回复区标题是
 *      「N 条回复」不是「N 条评论与回复」
 *   2. 引用框渲染在正文上方，条数与 `/posts` 出口里这条楼层的 `quotes` 条数一致 ——
 *      **一条楼层可以引多人**，少渲染一条就是静默丢内容
 *   3. 解析到被引用楼层时显示它的名字与「#序号」锚点；没解析到就写「引用片段（原楼未采集）」
 *   4. 引用块里的图渲染在引用框里（不是引用者自己的配图）
 *
 * 断言一律**由出口数据驱动**（先读 `/posts` 再按 `#序号` 去页面上找那张卡）——
 * 反过来按页面元素筛再断言，在没有这种数据时是恒真的（e2e:link 实测空转通过过）。
 * **找不到「一源一串且带引用」的任务时退出码 2**，不当成通过。
 *
 * 前端没有单元测试框架，理由见 results_filters.js。需要前后端都起着：
 *
 *   .\start.ps1
 *   node frontend/e2e/results_tweakers_quotes.js     # 或 cd frontend; npm run e2e:quotes
 *
 * 数据从哪来：对一个 Tweakers 数据源（例如 thread 2336074）跑一次**全量重跑**。
 * 改造前的存量数据是平铺的 140 个主贴、一条引用都没有，那时这条脚本会以退出码 2 说明原因。
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = process.env.E2E_FRONTEND || 'http://localhost:5173';
const BACKEND = process.env.E2E_BACKEND || 'http://127.0.0.1:8000';
const MAX_TASKS = 12;

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
        return false;
    }
    try {
        const r = await fetch(FRONTEND);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        console.error(`前端 ${FRONTEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        return false;
    }
    return true;
}

/** 一条主贴的整棵子树，先序 —— 与页面的渲染顺序一致 */
function subtree(root) {
    const out = [];
    const walk = (p) => (p.replies || []).forEach((c) => {
        out.push(c);
        walk(c);
    });
    walk(root);
    return out;
}

/** 找「有引用的一源一串」的任务。返回 {id, root, replies}；没有就 null */
async function pickTask() {
    const tasks = await api('/api/v1/tasks');
    for (const t of (Array.isArray(tasks) ? tasks : tasks.tasks || []).slice(0, MAX_TASKS)) {
        inner:
        for (let page = 1; page <= 10; page++) {
            let data;
            try {
                data = await api(`/api/v1/tasks/${t.id}/posts?page=${page}&page_size=50`);
            } catch {
                break;
            }
            for (const root of data.posts || []) {
                if (root.thread_kind !== 'thread') continue;
                const replies = subtree(root);
                if (replies.some((r) => (r.quotes || []).length)) {
                    return { id: t.id, root, replies, page };
                }
            }
            if (page * 50 >= (data.total || 0)) break inner;
        }
    }
    return null;
}

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail !== undefined) console.log(`       ${JSON.stringify(detail).slice(0, 600)}`);
    }
}

/** 页面上所有卡片的形状：徽标、回复区标题、每条回复的断言点 */
async function readPage(page) {
    return page.evaluate(() => [...document.querySelectorAll('.thread')].map((t) => ({
        idx: ((t.querySelector('.thread-head')?.textContent || '').match(/#(\d+)/) || [])[1] || '',
        badge: (t.querySelector('.badge-role')?.textContent || '').trim(),
        repliesLabel: (t.querySelector('.replies-label')?.textContent || '').replace(/\s+/g, ' ').trim(),
        replies: [...t.querySelectorAll('.reply')].map((r) => ({
            idx: ([...(r.querySelector('.reply-head')?.textContent || '').matchAll(/#(\d+)/g)].pop() || [])[1] || '',
            quoteBoxes: [...r.querySelectorAll('[data-testid="quote-box"]')].map((q) => ({
                user: (q.querySelector('.pc-quote-user')?.textContent || '').trim(),
                jump: (q.querySelector('.pc-quote-jump')?.textContent || '').trim(),
                unresolved: !!q.querySelector('[data-testid="quote-unresolved"]'),
                imgs: q.querySelectorAll('.pc-quote-images img').length,
                // 引用框必须在正文之前 —— 原站就是「引用在上、正文在下」
                aboveBody: !!(q.compareDocumentPosition(r.querySelector('.pc-orig, .pc-zh'))
                    & Node.DOCUMENT_POSITION_FOLLOWING),
            })),
            ownImgs: r.querySelectorAll('.pc-images:not(.pc-quote-images) img').length,
        })),
    })));
}

async function main() {
    if (!(await preflight())) return 'skip';
    const found = await pickTask();
    if (!found) {
        console.error('没有「一源一串（thread_kind=thread）且带引用」的任务可验证。'
            + '\n这通常意味着那个 Tweakers 数据源还是改造前的平铺结构 —— '
            + '对它跑一次「全量重跑」，引用只能靠重新回源采到。');
        return 'skip';
    }
    const { id, root, replies } = found;
    console.log(`任务 ${id.slice(0, 8)}，主题 #${root.index}，回复 ${replies.length} 条`);

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await (await browser.newContext()).newPage();
    await page.addInitScript((k) => localStorage.setItem('hyxi_api_key', k), KEY);
    try {
        await page.goto(`${FRONTEND}/tasks/${id}/results`, { waitUntil: 'networkidle' });
        // 一源一串在结果页上是一张很长的卡，回复多时渲染要一点时间
        await page.waitForSelector('.thread', { timeout: 30000 });
        await page.waitForTimeout(1500);
        const cards = await readPage(page);
        const card = cards.find((c) => c.idx === String(root.index));
        check(`主题 #${root.index} 在页面上找得到`, !!card);
        if (!card) return 'fail';

        // 1. 一个来源就是一串：这一串只出一张卡，措辞是「主题 / 回复」
        const family = new Set([root.index, ...replies.map((r) => r.index)]);
        const extra = cards.filter((c) => c.idx !== String(root.index)
            && !family.has(Number(c.idx)));
        check('这一串只出了一张卡（没有把楼层当成并列的主贴）',
            card.idx === String(root.index) && extra.length === 0,
            { 页面卡片数: cards.length, 意外多出来的: extra.map((c) => c.idx) });
        check('徽标是「主题」不是「主贴」', card.badge === '主题', card.badge);
        check(`回复区标题是「${replies.length} 条回复」`,
            card.repliesLabel === `💬 ${replies.length} 条回复`, card.repliesLabel);

        // 2. 逐条回复对账，断言全部由出口数据驱动
        const same = card.replies.length === replies.length
            && replies.every((r, i) => card.replies[i].idx === String(r.index));
        check(`渲染出全部 ${replies.length} 条回复，顺序与出口一致`, same,
            { 页面: card.replies.map((r) => r.idx), 出口: replies.map((r) => r.index) });
        const shown = same ? replies.map((r, i) => ({ post: r, ui: card.replies[i] })) : [];

        const quoteRows = shown.filter((r) => (r.post.quotes || []).length);
        check('有引用的回复不止一条（否则这条脚本没什么可验的）', quoteRows.length > 0,
            quoteRows.length);
        const wrongCount = quoteRows.filter((r) => r.ui.quoteBoxes.length !== r.post.quotes.length);
        check('引用框条数 = 出口里的引用条数（一条楼层可以引多人）', wrongCount.length === 0,
            wrongCount.map((r) => ({
                序号: r.post.index, 出口: r.post.quotes.length, 页面: r.ui.quoteBoxes.length,
            })));
        const notAbove = quoteRows.filter((r) => r.ui.quoteBoxes.some((q) => !q.aboveBody));
        check('引用框渲染在正文上方', notAbove.length === 0, notAbove.map((r) => r.post.index));

        // 3. 解析到的显示名字 + #序号；没解析到的明说「原楼未采集」
        const badName = [], badMark = [];
        quoteRows.forEach((r) => {
            r.post.quotes.forEach((q, i) => {
                const ui = r.ui.quoteBoxes[i];
                if (!ui) return;
                const who = q.username || q.cite || '（未署名）';
                if (ui.user !== who) badName.push({ 序号: r.post.index, 第几条: i + 1, 应为: who, 页面: ui.user });
                if (q.resolved && ui.jump !== `#${q.index}`) {
                    badMark.push({ 序号: r.post.index, 第几条: i + 1, 应为: `#${q.index}`, 页面: ui.jump });
                }
                if (!q.resolved && !ui.unresolved) {
                    badMark.push({ 序号: r.post.index, 第几条: i + 1, 应为: '引用片段（原楼未采集）', 页面: ui.jump });
                }
            });
        });
        check('引用框写的是被引用者的名字', badName.length === 0, badName);
        check('解析到的给「#序号」，没解析到的明说「原楼未采集」', badMark.length === 0, badMark);

        // 4. 引用图落在引用框里，不算引用者自己的配图
        const badImg = quoteRows.filter((r) => r.post.quotes.reduce(
            (n, q) => n + (q.images || []).length, 0) !== r.ui.quoteBoxes.reduce((n, q) => n + q.imgs, 0));
        check('引用块里的图渲染在引用框里', badImg.length === 0,
            badImg.map((r) => ({
                序号: r.post.index,
                出口: r.post.quotes.map((q) => (q.images || []).length),
                页面: r.ui.quoteBoxes.map((q) => q.imgs),
            })));
    } finally {
        await browser.close();
    }
    return 'done';
}

// 汇总额外留着（理由同 results_filters.js）：main() 里有提前 return 的分支。
// **设 exitCode、不调 process.exit()**：刚用完 fetch 就强退，Windows 上 Node 24 会撞
// libuv 句柄断言，退出码变成 127，「没有可验证的数据」那个 2 就丢了（实测）
main().then((result) => {
    if (result === 'skip') {
        process.exitCode = 2;
        return;
    }
    if (result === 'fail') {
        console.log('\n没有可对账的卡片');
        process.exitCode = 1;
        return;
    }
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exitCode = failures === 0 ? 0 : 1;
}).catch((e) => {
    console.error('\n跑挂了:', e.message);
    process.exitCode = 1;
});
