/**
 * 结果页补译提示条的冒烟测试 —— 真 Chrome、真前端、真后端，无 mock。
 *
 * 钉住（v1.13.0，规格见 docs/features/results-backfill-translation.md 的 R6/S2、S15）：
 *   场景一 有待补译的任务：提示条上的 N = /stats 的 untranslated_count = 出口 /posts 里同口径的条数；
 *          第 1 页「（尚未翻译）」标签数 + 失败标记条数 = 出口第 1 页同口径的条数
 *   场景二 没有待补译的任务：不显示提示条（一条「有 0 条帖子还没有译文」比没有更糟）
 *   场景三 点「翻译这 N 条」：出现「正在补译」、按钮禁用；完成后提示条消失、这一页不再有
 *          「（尚未翻译）」、/stats 归零。**这一步会真的调模型花钱，默认不跑**，
 *          要跑设 E2E_BACKFILL_CLICK=1（可用 E2E_BACKFILL_TIMEOUT_MIN 调等待上限，默认 30 分钟）
 *
 * 「有多少条还没译文」这个数在三处出现：提示条、页面上的标签、后端的统计。三处各算一份
 * 迟早对不上，而对不上的样子是静默的 —— 页面上明明还有「（尚未翻译）」，提示条却说 0 条。
 * 所以**断言由出口数据驱动**：先从 /posts 里按后端的判据数出来，再去页面上核对。
 *
 * 前端没有单元测试框架，理由见 results_filters.js。需要前后端都起着：
 *
 *   .\start.ps1
 *   node frontend/e2e/results_backfill_translation.js     # 或 cd frontend; npm run e2e:backfill
 *
 * 便携包是单端口形态（页面和 API 都在 8000），真数据在那边时这样指过去：
 *   $env:E2E_FRONTEND='http://127.0.0.1:8000'; node frontend/e2e/results_backfill_translation.js
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = process.env.E2E_FRONTEND || 'http://localhost:5173';
const BACKEND = process.env.E2E_BACKEND || 'http://127.0.0.1:8000';
const PAGE_SIZE = 50;        // 与 ResultsView 的 pageSize 一致：按页对账，页面和出口必须切得一样
const MAX_PAGES = 30;        // 全库对账最多翻这么多页（真实库 1076 条 = 不到 10 页主贴）
const CLICK = process.env.E2E_BACKFILL_CLICK === '1';
const CLICK_TIMEOUT = (Number(process.env.E2E_BACKFILL_TIMEOUT_MIN) || 30) * 60 * 1000;

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

// 判据抄后端 translator_service 的 needs_translation / FAILED_PREFIXES：
// 有正文，且译文去空白后为空、或以失败标记开头
const FAILED_PREFIXES = ['[翻译失败', '[翻译为空', '[翻译解析失败'];
const isFailed = (t) => FAILED_PREFIXES.some((p) => (t || '').trim().startsWith(p));
const hasBody = (p) => !!(p.content || '').trim();
const noTranslation = (p) => hasBody(p) && !(p.translation || '').trim();
const failedTranslation = (p) => hasBody(p) && isFailed(p.translation);
const needsTranslation = (p) => noTranslation(p) || failedTranslation(p);

/** 主贴 + 它的整棵子树，摊平成一条条帖子（出口按主贴分页，评论挂在 replies 里） */
function flatten(roots) {
    const out = [];
    const walk = (p) => {
        out.push(p);
        (p.replies || []).forEach(walk);
    };
    roots.forEach(walk);
    return out;
}

/** 某一页的出口数据（摊平）。page=1 时顺带把这一页的原始响应也带出来 */
async function outletPage(taskId, page) {
    const data = await api(`/api/v1/tasks/${taskId}/posts?page=${page}&page_size=${PAGE_SIZE}`);
    return { flat: flatten(data.posts || []), total: data.total || 0 };
}

/** 全库同口径条数：一页页翻到底。页数超过 MAX_PAGES 时返回 null（这一项本轮验证不了） */
async function outletCount(taskId) {
    let count = 0;
    for (let page = 1; page <= MAX_PAGES; page++) {
        const { flat, total } = await outletPage(taskId, page);
        count += flat.filter(needsTranslation).length;
        if (page * PAGE_SIZE >= total) return count;
    }
    return null;
}

/** 挑两个任务：一个有待补译的（场景一、三），一个没有的（场景二）。写死 ID 换台机器就跑不了 */
async function pickTasks() {
    const tasks = await api('/api/v1/tasks');
    const list = (Array.isArray(tasks) ? tasks : tasks.tasks || []).filter((t) => t.status === 'completed');
    let pending = null;
    let clean = null;
    for (const t of list) {
        if (pending && clean) break;
        let stats;
        try {
            stats = await api(`/api/v1/tasks/${t.id}/stats`);
        } catch {
            continue;                                  // 没有结果数据的任务，跳过
        }
        if (stats.untranslated_count === undefined) {
            throw new Error('/stats 里没有 untranslated_count —— 后端还是补译之前那一版');
        }
        if (stats.untranslated_count > 0 && !stats.translating && !pending) pending = { task: t, stats };
        if (stats.untranslated_count === 0 && !stats.translating && !clean && stats.total_posts > 0) {
            clean = { task: t, stats };
        }
    }
    return { pending, clean };
}

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail !== undefined) console.log(`       ${JSON.stringify(detail).slice(0, 600)}`);
    }
}

/** 页面上这一页的补译相关元素：提示条文案、按钮、「（尚未翻译）」标签数、失败标记条数 */
function readBar(page) {
    return page.evaluate(() => {
        const bar = document.querySelector('[data-testid="backfill-bar"]');
        const btn = document.querySelector('[data-testid="backfill-start"]');
        const zh = [...document.querySelectorAll('.pc-zh')].map((e) => e.textContent.trim());
        return {
            bar: !!bar,
            status: (document.querySelector('[data-testid="backfill-status"]')?.textContent || '').replace(/\s+/g, ' ').trim(),
            button: btn ? { text: btn.textContent.replace(/\s+/g, ' ').trim(), disabled: btn.disabled } : null,
            error: (document.querySelector('[data-testid="backfill-error"]')?.textContent || '').trim(),
            untranslatedLabels: document.querySelectorAll('.pc-untranslated').length,
            failedShown: zh.filter((t) => t.startsWith('[翻译')).length,
        };
    });
}

async function openResults(page, taskId) {
    await page.goto(`${FRONTEND}/tasks/${taskId}/results`, { waitUntil: 'networkidle' });
    await page.waitForSelector('.thread', { timeout: 20000 });
}

async function sceneOne(page, { task, stats }) {
    console.log(`\n场景一 有待补译的任务 ${task.id.slice(0, 8)}：/stats 说 ${stats.untranslated_count} 条`);
    await openResults(page, task.id);
    const ui = await readBar(page);
    const whole = await outletCount(task.id);
    const first = await outletPage(task.id, 1);

    check('显示了补译提示条', ui.bar, ui);
    check(`提示条写明「有 ${stats.untranslated_count} 条帖子还没有译文」`,
        ui.status.includes(`有 ${stats.untranslated_count} 条帖子还没有译文`), ui.status);
    check(`按钮写明「翻译这 ${stats.untranslated_count} 条」且可点`,
        !!ui.button && ui.button.text.includes(`翻译这 ${stats.untranslated_count} 条`) && !ui.button.disabled,
        ui.button);
    if (whole === null) {
        console.log(`  ⚠️ 帖子超过 ${MAX_PAGES} 页，「N 与出口全库条数一致」这一项本轮验证不到（不是通过）`);
    } else {
        check(`N 与出口全库同口径条数一致（出口 ${whole}）`, whole === stats.untranslated_count,
            { 提示条: stats.untranslated_count, 出口: whole });
    }
    const labels = first.flat.filter(noTranslation).length;
    const failed = first.flat.filter(failedTranslation).length;
    check(`第 1 页「（尚未翻译）」标签 ${labels} 个`, ui.untranslatedLabels === labels,
        { 页面: ui.untranslatedLabels, 出口: labels });
    check(`第 1 页失败标记 ${failed} 条照原样显示`, ui.failedShown === failed,
        { 页面: ui.failedShown, 出口: failed });
    if (!failed) console.log('  ⚠️ 第 1 页没有失败标记的帖子，那一条只验证了「不多显示」');
    return { labels, failed };
}

async function sceneTwo(page, { task, stats }) {
    console.log(`\n场景二 没有待补译的任务 ${task.id.slice(0, 8)}（${stats.total_posts} 条帖子）`);
    await openResults(page, task.id);
    const ui = await readBar(page);
    check('不显示补译提示条', !ui.bar, ui);
    check('页面上也没有「（尚未翻译）」', ui.untranslatedLabels === 0, ui.untranslatedLabels);
}

async function sceneThree(page, { task, stats }) {
    console.log(`\n场景三 点「翻译这 ${stats.untranslated_count} 条」（真调模型，上限 ${CLICK_TIMEOUT / 60000} 分钟）`);
    await openResults(page, task.id);
    await page.click('[data-testid="backfill-start"]');
    await page.waitForFunction(
        () => (document.querySelector('[data-testid="backfill-status"]')?.textContent || '').includes('正在补译'),
        null, { timeout: 30000 },
    );
    const running = await readBar(page);
    check('按钮点下后显示「正在补译 X / N」', running.status.includes('正在补译'), running.status);
    check('补译期间按钮禁用', !!running.button && running.button.disabled, running.button);
    check('没有报错', running.error === '', running.error);

    const started = Date.now();
    await page.waitForSelector('[data-testid="backfill-bar"]', { state: 'detached', timeout: CLICK_TIMEOUT });
    console.log(`  补译结束，用了 ${Math.round((Date.now() - started) / 1000)} 秒`);

    const after = await readBar(page);
    const stats2 = await api(`/api/v1/tasks/${task.id}/stats`);
    check('提示条消失', !after.bar, after);
    check('这一页不再有「（尚未翻译）」', after.untranslatedLabels === 0, after.untranslatedLabels);
    check('/stats 的 untranslated_count 归零', stats2.untranslated_count === 0, stats2.untranslated_count);
    check('/stats 的 translating 回到 false', stats2.translating === false, stats2.translating);
}

async function main() {
    if (!(await preflight())) return 'skip';
    const { pending, clean } = await pickTasks();
    if (!pending && !clean) {
        console.error('没有已完成且有结果数据的任务，验证不了（先跑一个采集任务）。');
        return 'skip';
    }

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await (await browser.newContext()).newPage();
    await page.addInitScript((k) => localStorage.setItem('hyxi_api_key', k), KEY);
    try {
        if (pending) await sceneOne(page, pending);
        else console.log('\n⚠️ 没有「有待补译帖子」的任务，场景一验证不到（不是通过）');
        if (clean) await sceneTwo(page, clean);
        else console.log('\n⚠️ 没有「译文都齐了」的任务，场景二验证不到（不是通过）');
        if (CLICK && pending) await sceneThree(page, pending);
        else if (!CLICK) console.log('\n场景三 跳过（会真的调模型花钱）：设 E2E_BACKFILL_CLICK=1 才跑');
    } finally {
        await browser.close();
    }
}

// 汇总和退出码留在 main() 外面，理由同 results_filters.js：里面有提前 return 的
// 分支，写在末尾那条路会被跳过，失败的一次会被当成通过。
// **设 exitCode、不调 process.exit()**：刚用完 fetch 就强退，Windows 上的 Node 24 会撞 libuv 的
// 句柄断言，退出码变成 127（实测，见 results_thread_structure.js）
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
