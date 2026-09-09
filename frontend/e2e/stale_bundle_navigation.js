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
import { readdirSync, renameSync, existsSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const ASSETS = join(ROOT, 'web', 'assets');
const BASE = 'http://127.0.0.1:8000';

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
    const page = await (await browser.newContext({ viewport: { width: 1280, height: 800 } })).newPage();
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
}

main().then(() => {
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exit(failures === 0 ? 0 : 1);
}).catch(e => {
    console.error('\n跑挂了:', e.message);
    process.exit(1);
});
