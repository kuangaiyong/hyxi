/**
 * 舆情详情页【发表时间】列的冒烟测试 —— 真 Chrome、真前端、真后端，无 mock。
 *
 * 钉住三件事：
 *   1. 列的位置：必须夹在【分析理由】和【涉及维度】中间，不是随便加在末尾
 *   2. 默认倒序：最新的排在最前
 *   3. 点表头能自由切换正序 / 倒序，三角形符号跟着翻
 *
 * 另有一条不显眼但要紧的：**「时间未知」的行永远沉底**，正序倒序都一样。
 * 那批帖子是采集时故意留空的（读不到 tooltip 绝对时间，写相对时间会污染指纹），
 * 实际很新 —— 当成「最早」会在倒序下把首屏整个占满，结果页为此踩过一次。
 *
 * 前端没有单元测试框架，理由见 results_filters.js。需要前后端都起着：
 *
 *   .\start.ps1                        # 先把两个服务拉起来
 *   node frontend/e2e/sentiment_time_column.js
 *   # 或： cd frontend; npm run e2e:sentiment
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = 'http://localhost:5173';
const BACKEND = 'http://127.0.0.1:8000';
const UNKNOWN = '时间未知';
const TABLE = 'table[data-testid="post-sentiment-table"]';

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

/** 找一个跑过舆情、且真有结论的任务。写死 ID 的话换台机器就跑不了 */
async function pickTask() {
    const tasks = await api('/api/v1/tasks');
    for (const t of (Array.isArray(tasks) ? tasks : tasks.tasks || [])) {
        let s;
        try {
            s = await api(`/api/v1/tasks/${t.id}/sentiment`);
        } catch {
            continue;
        }
        const rows = (s.results || []).filter(Boolean).length;
        if (s.task_id && rows > 0) return { id: t.id, rows };
    }
    return null;
}

/** 表头文本 + 【发表时间】那一列自上而下的取值。列位置按表头现算，不写死下标。
 *  **必须按 data-testid 选**：页面上另有两张 .data-table（按来源对比 / 跨来源维度），
 *  且排在详情表**前面** —— querySelector('.data-table') 在多来源任务上会选中它们，
 *  于是找不到【发表时间】，这个脚本会对一个好好的功能报失败。 */
const snapshot = (page) => page.evaluate((sel) => {
    const table = document.querySelector(sel);
    if (!table) return null;
    const heads = [...table.querySelectorAll('thead th')]
        .map(th => th.textContent.replace(/\s+/g, ' ').trim());
    const col = heads.findIndex(h => h.startsWith('发表时间'));
    const times = col < 0 ? [] : [...table.querySelectorAll('tbody tr')]
        .map(tr => tr.children[col])
        .filter(Boolean)
        .map(td => td.textContent.trim());
    const arrow = col < 0 ? '' : ((heads[col].match(/[▼▲]/) || [''])[0]);
    return { heads, col, times, arrow };
}, TABLE);

/** 有时间的那些是否单调，以及「时间未知」有没有混在中间 */
function order(times, dir) {
    const known = [];
    let sawUnknown = false;
    let unknownInMiddle = false;
    for (const t of times) {
        if (t === UNKNOWN) { sawUnknown = true; continue; }
        if (sawUnknown) unknownInMiddle = true;
        known.push(t);
    }
    let monotonic = true;
    for (let i = 1; i < known.length; i++) {
        if (dir === 'desc' ? known[i] > known[i - 1] : known[i] < known[i - 1]) monotonic = false;
    }
    return { monotonic, unknownInMiddle, known, unknown: times.length - known.length };
}

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail) console.log(`       ${JSON.stringify(detail)}`);
    }
}

const settle = (page) => page.waitForTimeout(800);

async function main() {
    await preflight();
    const task = await pickTask();
    if (!task) {
        console.error('没有「跑过舆情且有结论」的任务可测。先跑一次舆情分析再来。');
        process.exit(2);
    }
    console.log(`任务 ${task.id.slice(0, 8)}（${task.rows} 条结论）`);

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const page = await (await browser.newContext()).newPage();
    await page.addInitScript(k => localStorage.setItem('hyxi_api_key', k), KEY);
    try {
        await page.goto(`${FRONTEND}/tasks/${task.id}/sentiment`, { waitUntil: 'networkidle' });
        // 帖子是分页拉完再填进 postsMap 的，比首屏渲染晚
        await page.waitForSelector(`${TABLE} tbody tr`, { timeout: 20000 });
        await settle(page);

        let s = await snapshot(page);
        if (!s) {
            console.error('页面上没有帖子情感详情表');
            process.exit(2);
        }

        console.log('\n场景一：列在不在、位置对不对');
        check('表头有【发表时间】', s.col >= 0, s.heads);
        if (s.col < 0) return;
        check('夹在【分析理由】和【涉及维度】中间',
            s.heads[s.col - 1] === '分析理由' && s.heads[s.col + 1] === '涉及维度', s.heads);
        check('每一行都渲染出了这一列', s.times.length > 0 && s.times.every(t => t.length > 0),
            { 行数: s.times.length });

        console.log('\n场景二：默认倒序');
        let o = order(s.times, 'desc');
        console.log(`       ${o.known.length} 行有时间，${o.unknown} 行「${UNKNOWN}」`);
        check('三角形朝下', s.arrow === '▼', { arrow: s.arrow });
        // loadPosts() 出错是静默吞掉的，postsMap 空了就满屏「时间未知」，
        // 而那时下面的单调性判定是**真空成立**的 —— 功能死了这个脚本照样全绿
        check('确实有行拿到了真实时间（否则下面的判定是空过的）', o.known.length >= 2,
            { 有时间: o.known.length, 前几行: s.times.slice(0, 6) });
        check('有时间的行从新到旧', o.monotonic, o.known.slice(0, 6));
        check(`「${UNKNOWN}」全部沉底`, !o.unknownInMiddle, s.times.slice(0, 12));

        console.log('\n场景三：点表头切正序');
        await page.getByRole('columnheader', { name: /发表时间/ }).click();
        await settle(page);
        s = await snapshot(page);
        o = order(s.times, 'asc');
        check('三角形朝上', s.arrow === '▲', { arrow: s.arrow });
        check('有时间的行从旧到新', o.monotonic, o.known.slice(0, 6));
        check(`「${UNKNOWN}」仍然沉底（不许翻到最前面）`, !o.unknownInMiddle, s.times.slice(0, 12));

        console.log('\n场景四：键盘也要能切（回车），不能只有鼠标点得动');
        await page.getByRole('columnheader', { name: /发表时间/ }).focus();
        await page.keyboard.press('Enter');
        await settle(page);
        s = await snapshot(page);
        o = order(s.times, 'desc');
        check('三角形朝下', s.arrow === '▼', { arrow: s.arrow });
        check('回到从新到旧', o.monotonic, o.known.slice(0, 6));
        check('排序状态告诉了读屏软件',
            await page.getAttribute(`${TABLE} th[aria-sort]`, 'aria-sort') === 'descending');
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
