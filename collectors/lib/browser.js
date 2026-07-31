const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { log } = require('./job');

// 无头模式下 UA 会自报 HeadlessChrome，这是唯一需要抹掉的标记（客户端提示不受影响）。
// 硬编码一整串 UA 会在 Chrome 升级后与客户端提示失配，所以运行期从浏览器自己取。
// 探测页停在 about:blank，不产生任何网络请求。
async function resolveUserAgent(browser) {
    const probe = await browser.newContext();
    const ua = await (await probe.newPage()).evaluate(() => navigator.userAgent);
    await probe.close();
    return ua.replace('HeadlessChrome', 'Chrome');
}

function loadStorageState(stateFile) {
    if (!stateFile || !fs.existsSync(stateFile)) return undefined;
    try {
        const parsed = JSON.parse(fs.readFileSync(stateFile, 'utf-8'));
        if (Array.isArray(parsed.cookies)) return parsed;
    } catch (e) {
        log(`   ⚠️ 会话状态文件损坏，本轮不复用: ${e.message}`);
    }
    return undefined;
}

async function saveStorageState(context, stateFile) {
    if (!stateFile) return;
    try {
        const dir = path.dirname(stateFile);
        if (dir) fs.mkdirSync(dir, { recursive: true });
        await context.storageState({ path: stateFile });
    } catch (e) {
        log(`   ⚠️ 会话状态保存失败: ${e.message}`);
    }
}

function launchBrowser(headless) {
    return chromium.launch({
        headless,
        channel: 'chrome',
        args: ['--no-sandbox', '--disable-blink-features=AutomationControlled'],
    });
}

// 会话复用走 storageState 而不是 launchPersistentContext：后者的用户数据目录不能被两个
// 进程同时占用，max_concurrent_tasks > 1 时会直接启动失败。
async function newContext(browser, { stateFile, locale }) {
    const userAgent = await resolveUserAgent(browser);
    return browser.newContext({
        userAgent,
        locale,
        viewport: { width: 1536, height: 864 },
        storageState: loadStorageState(stateFile),
        // 时区故意不设，跟随宿主机（Asia/Shanghai）：检测方比对的是时区与出口 IP 的地理
        // 位置，从中国境内的机器发出的流量配 Europe/Amsterdam 反而是更硬的矛盾。
        // 将来若改从欧洲机器出网，这里要一并设成当地时区。
        //
        // 客户端提示（sec-ch-ua*）和 accept-language 都不手工覆盖：实测设了 userAgent 之后
        // Chrome 会自己把 sec-ch-ua 同步成不含 HeadlessChrome 的值，且与页面侧
        // navigator.userAgentData.brands 逐字一致；手写一份反而会在 GREASE 品牌上对不上。
        // accept-language 同理由 locale 决定，硬塞 q 值列表要么被覆盖、要么让
        // navigator.language 与请求头分家。
    });
}

module.exports = { launchBrowser, newContext, saveStorageState, resolveUserAgent };
