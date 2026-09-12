/**
 * 「服务端换了一份构建，页面还开着」时点导航必须有反馈 —— 真 Chrome、真后端、无 mock。
 *
 * 这是用户实测报过的一次：浏览器标签页里开着 SPA，8000 端口背后的服务被换掉了，
 * 于是点【LLM 配置】**没有任何反应** —— URL 不变、标题不变、侧栏高亮不变，
 * 界面上一个字都不提示，只有控制台里有一行
 * `Failed to fetch dynamically imported module`。
 *
 * 根子在两处叠加：
 *   1. 路由全是懒加载 `() => import('@/views/XxxView.vue')`，chunk 文件名带内容哈希，
 *      换一份构建就是一批新名字，旧名字全部 404
 *   2. vue-router 对导航失败**不做任何界面反馈**，`router.onError` 不注册就是静默吞掉
 *
 * 真实触发场景是**升级便携包但不硬刷新**：这是每个用户升级时都会走到的路，
 * 不是只有开发机上换端口才会碰。
 *
 * 不 mock：真的把 chunk 文件从磁盘上挪走，让真实服务器真的回 404。
 *
 * 前置条件是**单端口部署形态**（后端同时供页面和 API，即便携包的样子）：
 *
 *   cd frontend; npm run build
 *   cp -r frontend/dist web              # 项目根的 web/，mount_frontend() 认这个目录
 *   cd backend; ..\.venv\Scripts\python.exe -m uvicorn main:app --port 8000
 *   node frontend/e2e/stale_bundle_navigation.js
 *   # 或： cd frontend; npm run e2e:stale
 */
import { chromium } from 'playwright';
import { readdirSync, renameSync, existsSync, readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const ASSETS = join(ROOT, 'web', 'assets');
const BASE = 'http://127.0.0.1:8000';

/** 后端设了密钥时列表会被 401 挡空，页面上就没有任务可点。密钥同其它脚本从项目根 .env 读 */
function apiKey() {
    try {
        const m = readFileSync(join(ROOT, '.env'), 'utf-8').match(/^TWEAKERS_API_KEY=(.*)$/m);
        return (m && m[1].trim()) || '';
    } catch {
        return '';
    }
}
const KEY = apiKey();
const seedKey = (ctx) => ctx.addInitScript(
    k => { try { localStorage.setItem('hyxi_api_key', k) } catch {} }, KEY);

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail) console.log(`       ${JSON.stringify(detail)}`);
    }
}

async function preflight() {
    if (!existsSync(ASSETS)) {
        console.error(`没有 ${ASSETS}\n这个脚本要的是单端口部署形态，见文件头的前置条件。`);
        process.exit(2);
    }
    try {
        const r = await fetch(`${BASE}/tasks`);
        const ct = r.headers.get('content-type') || '';
        if (!ct.includes('text/html')) {
            console.error(`${BASE}/tasks 回的是 ${ct}，不是页面 —— 后端没有托管 web/`);
            process.exit(2);
        }
    } catch (e) {
        console.error(`后端 ${BASE} 没响应：${e.message}`);
        process.exit(2);
    }
}

async function main() {
    await preflight();
    const chunk = readdirSync(ASSETS).find(f => f.startsWith('ConfigView-') && f.endsWith('.js'));
    if (!chunk) {
        console.error('web/assets 里找不到 ConfigView 的 chunk，构建产物形状变了？');
        process.exit(2);
    }
    console.log(`目标 chunk: ${chunk}`);

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    await seedKey(ctx);
    const page = await ctx.newPage();
    const errs = [];
    page.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
    page.on('pageerror', e => errs.push(`[pageerror] ${e.message}`));

    const nav = [];
    page.on('framenavigated', f => { if (f === page.mainFrame()) nav.push(f.url()); });

    const moved = join(ASSETS, chunk + '.e2e-gone');
    let renamed = false;
    try {
        await page.goto(`${BASE}/tasks`, { waitUntil: 'networkidle' });
        await page.waitForTimeout(1200);
        const before = {
            url: page.url(),
            title: await page.locator('.app-header h2').textContent(),
        };
        console.log(`加载完成：${before.url}（${before.title}）`);

        // 服务端从这一刻起换了一份构建：旧 chunk 名字 404
        renameSync(join(ASSETS, chunk), moved);
        renamed = true;
        console.log(`已把 ${chunk} 挪走 —— 相当于服务端换了构建\n`);

        console.log('场景：点【LLM 配置】，chunk 拿不到');
        nav.length = 0;
        await page.locator('.sidebar-nav a[href="/config"]').click();
        // 修复后会自动整页刷一次；刷完仍拿不到才提示。两步都要等到
        await page.waitForTimeout(6000);

        const after = {
            url: page.url(),
            toasts: await page.locator('.toast-item').allTextContents(),
        };

        // 三条断言分别钉住三件不同的事，别写成同一个条件的两种说法 ——
        // 那样过与不过都分不清是哪条路径生效了
        check('① 自动整页刷到了目标路径（换了构建时这一步就能自愈）',
            nav.length > 0 && after.url.endsWith('/config'),
            { 导航序列: nav, 最终URL: after.url });

        check('② 刷完仍拿不到时，界面上有可见提示而不是只有控制台报错',
            after.toasts.some(t => /加载失败/.test(t)),
            { toast: after.toasts, 控制台: errs.filter(e => /dynamic/i.test(e)).slice(0, 1) });

        // 没有这道闸，服务端真的挂了就会变成页面一直闪 —— 比原来的静默失败更糟
        check('③ 只自动刷一次，不许无限循环', nav.length <= 2, { 导航次数: nav.length, 序列: nav });

        // 兜底：无论走哪条路径，都绝不许「URL、标题、高亮全没变，界面一个字不提示」
        check('④ 绝不是「什么都没发生」',
            after.url.endsWith('/config') || after.toasts.some(t => /加载失败/.test(t)),
            { url: after.url, toast: after.toasts });
    } finally {
        if (renamed) renameSync(moved, join(ASSETS, chunk));
        await browser.close();
    }

    // ConfigView 没有自己的 CSS，所以上面那一路走的是「JS chunk 404」。**带 CSS 的视图
    // 是另一条路**：Vite 的 __vitePreload 先等 CSS link 加载，失败时抛的是
    // `Unable to preload CSS for ...`，而且是在 import() 执行**之前**就抛 ——
    // 认不出它，router.onError 直接 return，照样「点了没反应」。
    // 结果页和舆情详情页恰好是最常用的两个详情页，而且两个都带 CSS。
    await cssScenario();
    await blockedStorageScenario();
}

/** 找一个带 CSS 的视图（结果页），把它的 CSS 挪走 */
async function cssScenario() {
    const css = readdirSync(ASSETS).find(f => f.startsWith('ResultsView-') && f.endsWith('.css'));
    if (!css) {
        console.log('\n⚠️ 构建产物里没有 ResultsView 的 CSS，跳过「CSS 拿不到」这一路');
        return;
    }
    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    await seedKey(ctx);
    const page = await ctx.newPage();
    const nav = [];
    page.on('framenavigated', f => { if (f === page.mainFrame()) nav.push(f.url()); });

    const moved = join(ASSETS, css + '.e2e-gone');
    let renamed = false;
    try {
        await page.goto(`${BASE}/tasks`, { waitUntil: 'networkidle' });
        // 「查看」是按钮走 router.push，不是链接；先把列表筛成已完成的，
        // 这样点进去一定是结果页（未完成的会跳进度页，那个视图没有自己的 CSS）
        await page.selectOption('select.form-input', 'completed');
        await page.waitForTimeout(600);
        const view = page.getByRole('button', { name: '查看', exact: true }).first();
        if (!(await view.count())) {
            console.log('\n⚠️ 没有已完成的任务可点，跳过「CSS 拿不到」这一路');
            return;
        }
        renameSync(join(ASSETS, css), moved);
        renamed = true;

        console.log(`\n场景：点已完成任务的「查看」，${css} 拿不到`);
        nav.length = 0;
        await view.click();
        await page.waitForTimeout(6000);

        const url = page.url();
        const toasts = await page.locator('.toast-item').allTextContents();
        check('⑤ 带 CSS 的视图同样不许「什么都没发生」',
            url.includes('/results') || toasts.some(t => /加载失败/.test(t)),
            { url, toast: toasts });
        check('⑥ 只自动刷一次', nav.length <= 2, { 导航次数: nav.length, 序列: nav });
    } finally {
        if (renamed) renameSync(moved, join(ASSETS, css));
        await browser.close();
    }
}

/**
 * 浏览器禁止站点保存数据时（隐私模式 / Chrome 的「不允许网站保存数据」），
 * 访问 sessionStorage 直接抛。「只刷一次」的标记就存不下了 —— 每次都判成「还没刷过」，
 * 于是无限整页刷新，页面一直闪，比原来的静默失败更糟。
 * 发版前评审实测过：8 秒内整页导航 213 次，始终没有提示。
 */
async function blockedStorageScenario() {
    const chunk = readdirSync(ASSETS).find(f => f.startsWith('ConfigView-') && f.endsWith('.js'));
    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    await seedKey(ctx);
    await ctx.addInitScript(() => {
        Object.defineProperty(window, 'sessionStorage', {
            get() { throw new DOMException('denied', 'SecurityError'); },
        });
    });
    const page = await ctx.newPage();
    const nav = [];
    page.on('framenavigated', f => { if (f === page.mainFrame()) nav.push(f.url()); });

    const moved = join(ASSETS, chunk + '.e2e-gone');
    let renamed = false;
    try {
        await page.goto(`${BASE}/tasks`, { waitUntil: 'networkidle' });
        await page.waitForTimeout(1000);
        renameSync(join(ASSETS, chunk), moved);
        renamed = true;

        console.log('\n场景：sessionStorage 被浏览器禁掉，chunk 又持续拿不到');
        nav.length = 0;
        await page.locator('.sidebar-nav a[href="/config"]').click();
        await page.waitForTimeout(8000);

        // 一直在刷的话这一步会抛「Execution context was destroyed」—— 那正是失败的样子
        const toasts = await page.locator('.toast-item').allTextContents()
            .catch(() => ['(读不到 —— 页面还在不停刷新)']);
        check('⑦ 不许无限整页刷新', nav.length <= 2, { '8秒内整页导航次数': nav.length });
        check('⑧ 存不下标记就直接提示，别闷着刷', toasts.some(t => /加载失败/.test(t)), toasts);
    } finally {
        if (renamed) renameSync(moved, join(ASSETS, chunk));
        await browser.close();
    }
}

main().then(() => {
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exit(failures === 0 ? 0 : 1);
}).catch(e => {
    console.error('\n跑挂了:', e.message);
    process.exit(1);
});
