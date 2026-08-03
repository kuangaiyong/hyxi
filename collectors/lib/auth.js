const { emit, log } = require('./job');

// 凭据只从环境变量取。绝不进 argv（会出现在进程列表和任何回显命令行的日志里），
// 也绝不进 job 文件（要落磁盘）。
function readCredentials() {
    return {
        username: process.env.HYXI_CRED_USERNAME || '',
        password: process.env.HYXI_CRED_PASSWORD || '',
    };
}

// 退出码 3：需要人工完成登录。明确区别于 1（硬失败）和 2（部分完成）——
// 前者让用户去查故障，这个是让用户去点一下「人工登录」。
const EXIT_NEEDS_MANUAL_AUTH = 3;

function needManualAuth(reason) {
    emit({ evt: 'need_manual_auth', reason });
    process.stderr.write(`${reason}\n`);
    process.exit(EXIT_NEEDS_MANUAL_AUTH);
}

/**
 * 判定登录后落地页的状态。
 *
 * 这四种情况的处置完全不同，混成一个「登录失败」会让用户无从下手：
 * 密码错要改凭据，两步验证和安全检查要人来点，成功则继续。
 */
async function classifyLanding(page, selectors) {
    const url = page.url();
    if (/\/checkpoint\//.test(url)) return { state: 'checkpoint', reason: '账号触发了安全检查（checkpoint）' };
    if (await page.$(selectors.twoFactorInput)) {
        return { state: 'two_factor', reason: '账号开启了两步验证，需要输入验证码' };
    }
    if (await page.$(selectors.loggedIn)) return { state: 'ok', reason: '' };
    if (await page.$(selectors.loginError)) {
        return { state: 'bad_credentials', reason: '账号或密码不正确' };
    }
    if (await page.$(selectors.passwordInput)) {
        return { state: 'bad_credentials', reason: '提交后仍停留在登录页' };
    }
    return { state: 'unknown', reason: '登录后落地页无法识别' };
}

/**
 * 确保已登录：优先复用会话，会话失效才动用密码。
 *
 * 「第一次登录后尽量缓存，避免每次采集都用账号密码」就落在这里 —— storageState 有效时
 * 整个函数不碰任何凭据，也不产生一次登录请求。
 */
async function ensureLogin(page, { entryUrl, loginUrl, selectors, timeout, gotoPage }) {
    await gotoPage(page, entryUrl, timeout);
    if (await page.$(selectors.loggedIn)) {
        log('   复用会话，跳过登录');
        return 'session';
    }

    const { username, password } = readCredentials();
    if (!username || !password) {
        needManualAuth('会话已失效且未配置凭据');
    }

    log('   会话失效，使用凭据登录');
    await gotoPage(page, loginUrl, timeout);
    await page.fill(selectors.usernameInput, username);
    await page.fill(selectors.passwordInput, password);
    await Promise.all([
        page.waitForLoadState('domcontentloaded'),
        page.click(selectors.submitButton),
    ]);

    const { state, reason } = await classifyLanding(page, selectors);
    if (state === 'ok') {
        log('   凭据登录成功');
        return 'password';
    }
    needManualAuth(reason);
}

/**
 * 人工授权模式：开有头浏览器让人自己完成验证，轮询到登录成功为止。
 *
 * 这是「撞上验证码就交给人，不硬闯」那条既有姿态的落点，不是绕过验证。
 */
async function waitForManualLogin(page, { entryUrl, selectors, timeout, gotoPage, maxWaitMs }) {
    await gotoPage(page, entryUrl, timeout);
    const deadline = Date.now() + maxWaitMs;
    let reported = 0;
    while (Date.now() < deadline) {
        if (await page.$(selectors.loggedIn)) {
            log('   检测到登录成功');
            return true;
        }
        const left = Math.round((deadline - Date.now()) / 1000);
        if (left <= reported - 15 || reported === 0) {
            reported = left;
            log(`   等待人工完成登录…剩余 ${left}s`);
        }
        await new Promise(r => setTimeout(r, 2000));
    }
    return false;
}

module.exports = {
    EXIT_NEEDS_MANUAL_AUTH,
    readCredentials,
    needManualAuth,
    classifyLanding,
    ensureLogin,
    waitForManualLogin,
};
