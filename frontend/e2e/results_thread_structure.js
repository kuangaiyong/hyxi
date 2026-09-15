/**
 * 结果页回复结构的冒烟测试 —— 真 Chrome、真前端、真后端，无 mock。
 *
 * 钉住（v1.12.0，规格见 docs/features/facebook-comment-threads.md 的 R6）：
 *   1. 回复全部展开：每张卡片渲染出来的回复条数 = /posts 出口里这条主贴的子树条数
 *   2. 缩进随层级递增；第 2 层起行头写「回复 <父贴作者>」，名字就是出口里它父贴的作者；
 *      有回复的第 1 层评论标「N 条回复」
 *   3. 原帖评论数读到了就摆「已采 X · 原帖 Y」：X < Y 标黄并说「可能不全」，X ≥ Y 不标黄；
 *      读不到就没有这个徽标
 *
 * 用户是拿着原帖对着看「采全了没有」的：少渲染一条、挂错一层，页面照样「能用」，肉眼扫
 * 列表发现不了。所以逐张卡片、逐条回复和出口对账，**断言由出口数据驱动** —— 反过来按页面
 * 元素筛再断言，在没有这种数据时是恒真的（e2e:link 实测空转通过过）。
 * **找不到带第 2 层回复的主贴时以退出码 2 退出**，不当成通过。
 *
 * 前端没有单元测试框架，理由见 results_filters.js。需要前后端都起着：
 *
 *   .\start.ps1
 *   node frontend/e2e/results_thread_structure.js     # 或 cd frontend; npm run e2e:thread
 *
 * 便携包是单端口形态（页面和 API 都在 8000），真数据在那边时这样指过去：
 *   $env:E2E_FRONTEND='http://127.0.0.1:8000'; node frontend/e2e/results_thread_structure.js
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = process.env.E2E_FRONTEND || 'http://localhost:5173';
const BACKEND = process.env.E2E_BACKEND || 'http://127.0.0.1:8000';
const PAGE_SIZE = 50;       // 与 ResultsView 的 pageSize 一致：按页对账，页面和出口必须切得一样
const MAX_PAGES = 10;       // 一个任务最多翻这么多页找场景，别让冒烟测试跑成全量巡检

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

/** 前后端有一个没起来就返回 false —— 环境不具备，退出码 2，既不算失败也不算通过 */
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

/** 主贴的整棵子树，先序（和页面的渲染顺序一样），每条带上父贴作者 */
function subtree(root) {
    const out = [];
    const walk = (p) => (p.replies || []).forEach((c) => {
        out.push({ post: c, parentName: p.username, descendants: 0 });
        const row = out[out.length - 1];
        const before = out.length;
        walk(c);
        row.descendants = out.length - before;
    });
    walk(root);
    return out;
}

const hasNested = (root) => subtree(root).some((r) => r.post.reply_level >= 2);

/** 找一个「有第 2 层回复」的任务，返回要去页面上对账的页码。写死 ID 换台机器就跑不了 */
async function pickTask() {
    const tasks = await api('/api/v1/tasks');
    for (const t of (Array.isArray(tasks) ? tasks : tasks.tasks || [])) {
        const pages = [];
        const seen = { nested: false, short: false, full: false, unknown: false };
        for (let page = 1; page <= MAX_PAGES; page++) {
            let data;
            try {
                data = await api(`/api/v1/tasks/${t.id}/posts?page=${page}&page_size=${PAGE_SIZE}`);
            } catch {
                break;
            }
            const roots = data.posts || [];
            const found = {
                nested: roots.some(hasNested),
                short: roots.some((r) => r.site_comment_count != null && subtree(r).length < r.site_comment_count),
                full: roots.some((r) => r.site_comment_count != null && subtree(r).length >= r.site_comment_count),
                unknown: roots.some((r) => r.site_comment_count == null && subtree(r).length),
            };
            // 只挑能补上新场景的页，已经见过的场景不为它多翻一页
            if (Object.keys(found).some((k) => found[k] && !seen[k])) {
                pages.push(page);
                Object.keys(found).forEach((k) => { seen[k] = seen[k] || found[k]; });
            }
            if (page * PAGE_SIZE >= (data.total || 0)) break;
        }
        if (seen.nested) return { id: t.id, pages, seen };
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

/** 翻到结果页的第 n 页（页面没有页码参数，只能点「›」） */
async function gotoUiPage(page, n) {
    for (let cur = 1; cur < n; cur++) {
        const resp = page.waitForResponse((r) => r.url().includes('/posts?') && r.status() === 200);
        // 精确匹配：舆情徽标「📊 负面 ›」也是带 › 的按钮
        await page.getByRole('button', { name: '›', exact: true }).click();
        await resp;
        await page.waitForTimeout(300);
    }
}

async function checkPage(page, taskId, n) {
    const data = await api(`/api/v1/tasks/${taskId}/posts?page=${n}&page_size=${PAGE_SIZE}`);
    // 卡片和回复都按行头里的「#序号」认（e2e:link 同一个认法），不靠为测试加的属性 ——
    // 这样对着改动前的界面跑，红在「少渲染了 / 没写回复谁 / 没有对账徽标」这些行为上
    const cards = await page.evaluate(() => [...document.querySelectorAll('.thread')].map((t) => {
        const badge = t.querySelector('[data-testid="completeness"]');
        const lastIndex = (el) => ([...(el?.textContent || '').matchAll(/#(\d+)/g)].pop() || [])[1] || '';
        return {
            idx: ((t.querySelector('.thread-head')?.textContent || '').match(/#(\d+)/) || [])[1] || '',
            badge: badge ? {
                text: badge.textContent.replace(/\s+/g, ' ').trim(),
                short: badge.classList.contains('short'),
                title: badge.getAttribute('title') || '',
            } : null,
            replies: [...t.querySelectorAll('.reply')].map((r) => ({
                idx: lastIndex(r.querySelector('.reply-head')),
                margin: parseFloat(getComputedStyle(r).marginLeft) || 0,
                replyTo: (r.querySelector('[data-testid="reply-to"]')?.textContent || '').trim(),
                head: (r.querySelector('.reply-head')?.textContent || '').replace(/\s+/g, ' '),
            })),
        };
    }));
    console.log(`\n第 ${n} 页：页面 ${cards.length} 张卡片，出口 ${data.posts.length} 个主贴`);
    check(`第 ${n} 页卡片数与出口一致`, cards.length === data.posts.length,
        { 页面: cards.length, 出口: data.posts.length });

    for (const root of data.posts) {
        const card = cards.find((c) => c.idx === String(root.index));
        const rows = subtree(root);
        if (!card) {
            check(`#${root.index} 在页面上找得到`, false);
            continue;
        }
        // 1. 全部展开：条数、顺序都要和出口一致
        const same = card.replies.length === rows.length
            && rows.every((r, i) => card.replies[i].idx === String(r.post.index));
        if (!same || hasNested(root) || root.site_comment_count != null) {
            check(`#${root.index} 渲染出全部 ${rows.length} 条回复，顺序与原帖一致`, same,
                { 页面: card.replies.map((r) => r.idx), 出口: rows.map((r) => r.post.index) });
        }

        // 2. 层级：缩进随层级递增、「回复 某某」、第 1 层的「N 条回复」。
        // 层级取出口的，只核对和出口逐条对得上的那一段（上面那条已经管了少渲染的情况）
        const agreed = rows.findIndex((r, i) => card.replies[i]?.idx !== String(r.post.index));
        const shown = agreed === -1 ? rows : rows.slice(0, agreed);
        if (shown.some((r) => r.post.reply_level >= 2)) {
            const byLevel = {};
            shown.forEach((r, i) => {
                const level = r.post.reply_level;
                (byLevel[level] = byLevel[level] || new Set()).add(card.replies[i].margin);
            });
            const levels = Object.keys(byLevel).map(Number).sort((a, b) => a - b);
            const oneMarginPerLevel = levels.every((l) => byLevel[l].size === 1);
            const deeperIsFurther = levels.every((l, i) => i === 0
                || l > 4 || [...byLevel[l]][0] > [...byLevel[levels[i - 1]]][0]);
            check(`#${root.index} 缩进随层级递增（层级 ${levels.join('/')}）`,
                oneMarginPerLevel && deeperIsFurther,
                levels.map((l) => ({ 层级: l, 缩进: [...byLevel[l]] })));
            const wrongTo = shown.filter((r, i) => (r.post.reply_level >= 2
                ? card.replies[i]?.replyTo !== `回复 ${r.parentName}`
                : !!card.replies[i]?.replyTo));
            check(`#${root.index} 第 2 层起写明「回复 某某」，且是它真正的父贴作者`, wrongTo.length === 0,
                wrongTo.map((r) => ({ 序号: r.post.index, 层级: r.post.reply_level, 应为: r.parentName })));
            const wrongCount = shown.filter((r, i) => r.post.reply_level === 1 && r.descendants
                && !(card.replies[i]?.head || '').includes(`${r.descendants} 条回复`));
            check(`#${root.index} 有回复的评论标出「N 条回复」`, wrongCount.length === 0,
                wrongCount.map((r) => ({ 序号: r.post.index, 应为: `${r.descendants} 条回复` })));
        }

        // 3. 原帖评论数
        if (root.site_comment_count == null) {
            if (card.badge) check(`#${root.index} 没读到原帖评论数就不该有对账徽标`, false, card.badge);
            continue;
        }
        const x = rows.length;
        const y = root.site_comment_count;
        check(`#${root.index} 显示「已采 ${x} · 原帖 ${y}」`, !!card.badge && card.badge.text === `已采 ${x} · 原帖 ${y}`,
            card.badge);
        if (card.badge) {
            check(`#${root.index} ${x < y ? '采少了要标黄并说可能不全' : '采够了不标黄'}`,
                card.badge.short === (x < y) && (x >= y || card.badge.title.includes('可能不全')), card.badge);
        }
    }
}

async function main() {
    if (!(await preflight())) return 'skip';
    const task = await pickTask();
    if (!task) {
        console.error('没有「带第 2 层回复」的任务可验证（没有可验证的数据）。'
            + '先用 v1.12.0 对 Facebook 数据源跑一次全量重跑再来。');
        return 'skip';
    }
    console.log(`任务 ${task.id.slice(0, 8)}，对账页 ${task.pages.join(', ')}`);
    const missing = Object.entries({ short: '采少了（标黄）', full: '采够了（不标黄）', unknown: '没读到原帖评论数' })
        .filter(([k]) => !task.seen[k]).map(([, v]) => v);
    if (missing.length) {
        console.log(`  ⚠️ 前 ${MAX_PAGES} 页里没有这些情形，本轮验证不到（不是通过）：${missing.join('、')}`);
    }

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await (await browser.newContext()).newPage();
    await page.addInitScript((k) => localStorage.setItem('hyxi_api_key', k), KEY);
    try {
        await page.goto(`${FRONTEND}/tasks/${task.id}/results`, { waitUntil: 'networkidle' });
        await page.waitForSelector('.thread', { timeout: 20000 });
        let at = 1;
        for (const n of task.pages) {
            await gotoUiPage(page, n - at + 1);
            at = n;
            await checkPage(page, task.id, n);
        }
    } finally {
        await browser.close();
    }
}

// 汇总和退出码留在 main() 外面，理由同 results_filters.js：里面有提前 return 的
// 分支，写在末尾那条路会被跳过，失败的一次会被当成通过。
// **设 exitCode、不调 process.exit()**：刚用完 fetch 就强退，Windows 上的 Node 24 会撞 libuv 的
// 句柄断言（`!(handle->flags & UV_HANDLE_CLOSING)`），退出码变成 127 —— 「没有可验证的数据」
// 那个 2 就这么丢了（实测）
main().then((result) => {
    if (result === 'skip') {
        process.exitCode = 2;
        return;
    }
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exitCode = failures === 0 ? 0 : 1;
}).catch((e) => {
    console.error('\n跑挂了:', e.message);
    process.exitCode = 1;
});
