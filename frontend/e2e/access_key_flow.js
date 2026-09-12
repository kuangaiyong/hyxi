/**
 * 「被 401 挡住 → 照提示去填服务访问密钥」这段旅程 —— 真 Chrome、真前后端、无 mock。
 *
 * 用户实测走过这条路，一路三个坎：
 *   1. 提示说去填「服务访问密钥」，页面上那个框却叫「Access Key」，《使用说明》又叫
 *      「接口密钥」—— 同一个东西三个名字，照着提示在页面上**根本找不到**
 *   2. 那个框排在两组大模型「API Key」**下面**，1280×800 要滚动才看得到；而首屏那两个
 *      「API Key」是完全不同的东西，极易填错
 *   3. 填完点保存，只报一句「已保存」—— 填错了也这么说；填对了，右上角那条常驻的红色
 *      「访问被拒绝」也还挂着，两张卡片仍是空的，**看不出自己做对没有**
 *
 * 所以这里按**用户的找法**定位：从 401 提示里读出它说的那个名字，再去页面上找同名的
 * 标签。按 data-testid 找就钉不住第 1 条 —— 名字对不上照样找得到。
 *
 * 前置：`.\start.ps1` 起两个服务，且项目根 `.env` 设了 `TWEAKERS_API_KEY`
 * （没设就不会 401，这条旅程不存在，脚本以退出码 2 说明）。
 *
 *   node frontend/e2e/access_key_flow.js
 *   # 或： cd frontend; npm run e2e:key
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = 'http://localhost:5173';
const BACKEND = 'http://127.0.0.1:8000';
const VIEWPORT = { width: 1280, height: 800 };

function apiKey() {
    try {
        const m = readFileSync(join(ROOT, '.env'), 'utf-8').match(/^TWEAKERS_API_KEY=(.*)$/m);
        return (m && m[1].trim()) || '';
    } catch {
        return '';
    }
}

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail !== undefined) console.log(`       ${JSON.stringify(detail)}`);
    }
}

async function preflight(key) {
    try {
        const r = await fetch(FRONTEND);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        console.error(`前端 ${FRONTEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        process.exit(2);
    }
    let status;
    try {
        status = (await fetch(`${BACKEND}/api/v1/tasks`)).status;
    } catch (e) {
        console.error(`后端 ${BACKEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        process.exit(2);
    }
    if (!key || status !== 401) {
        console.error('后端没有要求服务访问密钥（项目根 .env 没设 TWEAKERS_API_KEY），'
            + '不会出现 401，这条旅程不存在');
        process.exit(2);
    }
}

const toasts = (page) => page.locator('.toast-item').allTextContents();
const waitToast = (page, re, ms = 6000) => page.waitForFunction(
    (src) => [...document.querySelectorAll('.toast-item')]
        .some((t) => new RegExp(src).test(t.textContent || '')),
    re.source, { timeout: ms },
).then(() => true, () => false);

async function main() {
    const KEY = apiKey();
    await preflight(KEY);

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    // 全新上下文 = localStorage 里没有密钥，就是用户换了个地址访问时的样子
    const page = await (await browser.newContext({ viewport: VIEWPORT })).newPage();
    const configCalls = [];
    page.on('response', (r) => {
        if (r.url().endsWith('/api/v1/config')) configCalls.push(r.status());
    });

    try {
        await page.goto(`${FRONTEND}/tasks`, { waitUntil: 'networkidle' });
        const got401 = await waitToast(page, /访问被拒绝/);
        const denied = (await toasts(page)).find((t) => /访问被拒绝/.test(t)) || '';
        // 提示原话：「请在「LLM 配置」页填写与后端一致的服务访问密钥」—— 取它说的那个名字
        const named = (denied.match(/一致的(.+?)$/) || [])[1]?.trim() || '';
        console.log(`401 提示：${denied}\n它让用户去找：「${named}」\n`);
        check('没有密钥时出现了 401 提示', got401 && !!named, denied);

        await page.locator('.sidebar-nav a[href="/config"]').click();
        await page.waitForURL('**/config');
        await page.waitForTimeout(800);

        console.log('场景一：照着提示的名字，在页面上找得到那个框');
        const field = await page.evaluate((name) => {
            const label = [...document.querySelectorAll('.form-label')]
                .find((l) => l.textContent.trim() === name);
            if (!label) {
                return { found: false, labels: [...document.querySelectorAll('.form-label')]
                    .map((l) => l.textContent.trim()) };
            }
            const input = label.parentElement.querySelector('input');
            const r = input.getBoundingClientRect();
            const firstCard = document.querySelector('.card .card-header');
            return {
                found: true,
                top: Math.round(r.top), bottom: Math.round(r.bottom),
                inFirstScreen: r.top >= 0 && r.bottom <= window.innerHeight,
                firstCard: firstCard ? firstCard.textContent.trim() : '',
            };
        }, named);
        check(`页面上有一个标签就叫「${named}」`, field.found, field.labels);
        if (!field.found) return;

        console.log('\n场景二：不用滚动就看得到，而且排在大模型 API Key 前面');
        check('框在首屏之内（1280×800）', field.inFirstScreen,
            { top: field.top, bottom: field.bottom, 视口高: VIEWPORT.height });
        check('它所在的卡片是页面上第一张', field.firstCard.includes(named), field.firstCard);

        const card = page.locator('.card', { has: page.locator('.form-label', { hasText: named }) });
        const input = card.locator('input');
        const save = card.getByRole('button', { name: /保存/ });

        console.log('\n场景三：填错了要明说，不能也报「已保存」');
        // 必须用 ASCII 的错密钥：中文密钥会先被「非 Latin-1」那条分支拦下，
        // 测的就不是「填了个能发出去但不对的密钥」了（写这条时实测撞过）
        await input.fill('definitely-not-the-right-key');
        await save.click();
        const saidWrong = await waitToast(page, /不一致/);
        await page.waitForTimeout(300);
        const afterWrong = await toasts(page);
        check('提示了密钥与后端不一致', saidWrong, afterWrong);
        check('没有给出「验证通过」这类让人以为好了的提示',
            !afterWrong.some((t) => /验证通过/.test(t)), afterWrong);

        console.log('\n场景三之二：非 Latin-1 的密钥（如中文）要说清是浏览器装不下');
        // 后端是按 utf-8 字节比的（auth.py 专门为中文密钥改过），密钥本身合法；
        // 但浏览器的请求头只认 Latin-1，axios 发出去之前会把 U+00FF 以上的字符直接删掉，
        // X-API-Key 变成空串 → 401。这时报「与后端不一致」会让用户反复重填一个对的密钥
        // 只看**这次点击新增的** toast：上一场景那条「与后端不一致」还没过期（6 秒），
        // 而且不能手动点掉常驻的 401 提示 —— 场景四要靠它自己消失来判定
        const beforeCn = await toasts(page);
        await input.fill('中文密钥');
        await save.click();
        await page.waitForTimeout(1500);
        const added = (await toasts(page)).filter(t => !beforeCn.includes(t));
        check('提示指向「浏览器装不下非 Latin-1 字符」而不是「填错了」',
            added.some(t => /Latin-1|请求头/.test(t)) && !added.some(t => /与后端不一致/.test(t)),
            { 新增: added, 点击前: beforeCn });

        console.log('\n场景四：填对了，界面要当场变过来');
        configCalls.length = 0;
        await input.fill(KEY);
        await save.click();
        const saidOk = await waitToast(page, /验证通过/);
        await page.waitForTimeout(500);
        const afterRight = await toasts(page);
        check('提示验证通过', saidOk, afterRight);
        check('那条常驻的「访问被拒绝」自己消失了', !afterRight.some((t) => /访问被拒绝/.test(t)),
            afterRight);
        check('保存后当场用新密钥拉回了配置（不必手动刷新）', configCalls.includes(200),
            { '/api/v1/config 的响应': configCalls });

        console.log('\n场景五：换个页面再回来，不再被挡');
        await page.goto(`${FRONTEND}/tasks`, { waitUntil: 'networkidle' });
        await page.waitForTimeout(800);
        const later = await toasts(page);
        check('刷新后没有 401 提示（密钥真的存下来了）', !later.some((t) => /访问被拒绝/.test(t)), later);
    } finally {
        await browser.close();
    }
}

main().then(() => {
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exit(failures === 0 ? 0 : 1);
}).catch((e) => {
    console.error('\n跑挂了:', e.message);
    process.exit(1);
});
