/**
 * 任务结果页筛选条件的冒烟测试 —— 真 Chrome、真前端、真后端，无 mock。
 *
 * 钉住的是一条用户实测报过的 bug：「只看新回复 + 近 3 天」一条都没命中时，
 * 整个查询区域连同开关一起消失，只能刷新页面才能回去。根因是整张卡片
 * （搜索栏 + 筛选开关 + 列表 + 分页）一起挂在 posts.length 上，而下面那个
 * 空状态又要求 !isCompleted —— 已完成任务两边都落空。
 *
 * **搜索栏必须照常渲染**：它自己就是关掉筛选的唯一入口。
 *
 * 前端没有单元测试框架，为这一条引入 vitest 不值当；playwright 根目录本来就有，
 * 所以这条回归靠真浏览器守。代价是它需要前后端都起着，进不了 pytest。
 *
 *   .\start.ps1                 # 先把两个服务拉起来
 *   node frontend/e2e/results_filters.js
 *   # 或： cd frontend; npm run e2e
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = 'http://localhost:5173';
const BACKEND = 'http://127.0.0.1:8000';
const NONSENSE = 'zzz这个词一定搜不到zzz';

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

/** 两个服务都得起着，否则报错会长得像前端坏了 */
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

/** 找一个已完成、且有帖子的任务。写死 ID 的话换台机器就跑不了 */
async function pickTask() {
    const tasks = await api('/api/v1/tasks');
    for (const t of (Array.isArray(tasks) ? tasks : tasks.tasks || [])) {
        if (t.status !== 'completed') continue;
        const posts = await api(`/api/v1/tasks/${t.id}/posts?page=1&page_size=1`);
        if (posts.total > 0) return { id: t.id, total: posts.total };
    }
    return null;
}

/**
 * 找一个「只看新回复」会筛出 0 条的窗口，以及一个筛得出东西的窗口。
 * 数据凑不出来就返回 null —— 那时这一段跳过，而不是判失败：
 * 空态那条代码路径搜索场景已经走过了。
 */
async function pickWindows(taskId) {
    const counts = {};
    for (const d of [3, 7, 14]) {
        counts[d] = (await api(
            `/api/v1/tasks/${taskId}/posts?page=1&page_size=1&fresh_days=${d}&only_fresh=true`
        )).total;
    }
    const empty = [3, 7, 14].find(d => counts[d] === 0);
    const nonEmpty = [14, 7, 3].find(d => counts[d] > 0);
    return empty && nonEmpty ? { empty, nonEmpty, counts } : null;
}

/** 页面当前的可观察状态。控件「在不在」按真实渲染尺寸判，不看 DOM 里有没有 */
const snapshot = (page) => page.evaluate(() => {
    const shown = (el) => !!el && el.getBoundingClientRect().width > 0;
    const button = (text) => [...document.querySelectorAll('button')]
        .some(b => b.textContent.includes(text) && shown(b));
    const empty = document.querySelector('.empty-filtered');
    return {
        搜索框: shown(document.querySelector('input[placeholder*="搜索"]')),
        只看新回复: button('只看新回复'),
        线程数: document.querySelectorAll('article.thread').length,
        空提示: empty ? empty.innerText.replace(/\s+/g, ' ').trim() : null,
        分页条: [...document.querySelectorAll('span')]
            .some(s => /^第 \d+\/\d+ 页，共 \d+ 条$/.test(s.textContent.trim())),
    };
});

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail) console.log(`       ${JSON.stringify(detail)}`);
    }
}

/** 每次操作后要等一轮请求回来再取快照 */
const settle = (page) => page.waitForTimeout(1200);

async function main() {
    await preflight();
    const task = await pickTask();
    if (!task) {
        console.error('没有「已完成且有帖子」的任务可测。先跑一个任务再来。');
        process.exit(2);
    }
    const windows = await pickWindows(task.id);
    console.log(`任务 ${task.id.slice(0, 8)}（${task.total} 个主贴）`
        + (windows ? `，新回复窗口 ${JSON.stringify(windows.counts)}` : '，无可用的新回复窗口'));

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await (await browser.newContext()).newPage();
    await page.addInitScript(k => localStorage.setItem('hyxi_api_key', k), KEY);
    try {
        await page.goto(`${FRONTEND}/tasks/${task.id}/results`, { waitUntil: 'networkidle' });

        console.log('\n场景一：搜一个搜不到的词');
        await page.fill('input[placeholder*="搜索"]', NONSENSE);
        await page.getByRole('button', { name: '搜索' }).click();
        await settle(page);
        let s = await snapshot(page);
        check('搜索栏还在（它是关掉筛选的唯一入口）', s.搜索框, s);
        check('给出了空提示而不是一片空白', !!s.空提示 && s.空提示.includes(NONSENSE), s);
        check('一条都没有时不显示分页条', !s.分页条, s);

        // 搜索栏没了就是那个 bug 本身，后面每一步都要点它旁边的按钮 ——
        // 继续跑只会换来一串 30 秒的定位超时，把真正的原因埋掉
        if (!s.搜索框) {
            console.log('\n搜索栏已消失 —— 正是这个脚本要守的那个 bug，后续场景无从点起');
            return;
        }

        console.log('\n场景二：点「清除」退回去');
        await page.getByRole('button', { name: '清除' }).click();
        await settle(page);
        s = await snapshot(page);
        check('帖子回来了', s.线程数 > 0, s);
        check('空提示消失', s.空提示 === null, s);

        if (!windows) {
            console.log('\n场景三：跳过 —— 现有数据凑不出「筛得空」和「筛得出」两个窗口');
        } else {
            console.log(`\n场景三：只看新回复，切到近 ${windows.empty} 天（后端返回 0 条）`);
            await page.getByRole('button', { name: /只看新回复/ }).click();
            await settle(page);
            await page.getByRole('button', { name: new RegExp(`近 ${windows.empty} 天`) }).click();
            await settle(page);
            s = await snapshot(page);
            check('搜索栏与开关都还在', s.搜索框 && s.只看新回复, s);
            check('给出了空提示', !!s.空提示, s);
            check('没有帖子', s.线程数 === 0, s);

            console.log(`\n场景四：切到近 ${windows.nonEmpty} 天走回去`);
            await page.getByRole('button', { name: new RegExp(`近 ${windows.nonEmpty} 天`) }).click();
            await settle(page);
            s = await snapshot(page);
            check('帖子回来了', s.线程数 > 0, s);
            check('空提示消失', s.空提示 === null, s);

            console.log('\n场景五：关掉开关回到全部');
            await page.getByRole('button', { name: /只看新回复/ }).click();
            await settle(page);
            s = await snapshot(page);
            check('主贴数回到未筛选时的量', s.线程数 > 0, s);
            check('分页条回来了', s.分页条, s);
        }
    } finally {
        await browser.close();
    }
}

// 汇总和退出码**必须留在 main() 外面**：里面有提前 return 的分支，
// 写在 main() 末尾的话那条路会跳过 process.exit()，Node 自然退出成 0 ——
// 失败的一次会被当成通过。踩过。
main().then(() => {
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exit(failures === 0 ? 0 : 1);
}).catch(e => {
    console.error('\n跑挂了:', e.message);
    process.exit(1);
});
