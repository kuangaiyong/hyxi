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
 * 这几种情况的处置完全不同，混成一个「登录失败」会让用户无从下手：
 * 密码错要改凭据，两步验证和安全检查要人来点，成功则继续。
 *
 * 「还停在登录页且没有报错」返回 pending 而不是失败 —— 点下提交到页面落地之间
 * 必然有一段时间密码框还在，当场判死会把每一次正常登录都误报成密码错。
 */
async function classifyLanding(page, selectors) {
    const url = page.url();
    if (/\/checkpoint\//.test(url)) {
        return { state: 'checkpoint', reason: '账号触发了安全检查（checkpoint）' };
    }
    // 实测真站在这里给的是 Arkose Labs 的人机验证（flow=pre_authentication），
    // 不是输个短信码就完事的两步验证 —— 必须人来过
    if (/\/two_step_verification\//.test(url)) {
        return { state: 'checkpoint', reason: 'Facebook 要求完成安全验证（人机验证 / 两步验证）' };
    }
    if (await page.$(selectors.twoFactorInput)) {
        return { state: 'two_factor', reason: '账号开启了两步验证，需要输入验证码' };
    }
    if (await page.$(selectors.loggedIn)) return { state: 'ok', reason: '' };
    if (await page.$(selectors.loginError)) {
        return { state: 'bad_credentials', reason: '账号或密码不正确' };
    }
    if (await page.$(selectors.passwordInput)) {
        return { state: 'pending', reason: '' };
    }
    return { state: 'unknown', reason: '登录后落地页无法识别' };
}

/**
 * 点完提交后等落地状态定下来。
 *
 * 不去赌「点登录一定触发整页导航」—— 真站很可能是局部刷新，waitForLoadState 会
 * 立刻在旧页面上 resolve，读到的就是提交前的状态。轮询到状态确定为止，
 * 导航还是 AJAX 都不影响。
 */
async function waitForLanding(page, selectors, timeoutMs = 25000) {
    const deadline = Date.now() + timeoutMs;
    let last = { state: 'pending', reason: '' };
    while (Date.now() < deadline) {
        last = await classifyLanding(page, selectors);
        if (last.state !== 'pending') return last;
        await page.waitForTimeout(1000);
    }
    // 一直停在登录页且始终没弹错误框：当作凭据不对处理，但说清楚是怎么判的
    return { state: 'bad_credentials', reason: '提交后一直停留在登录页，未跳转也未报错' };
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
    // 未登录访问目标页时站点通常已经把我们重定向到登录页了；只有确实没有密码框才再跳一次
    if (!(await page.$(selectors.passwordInput))) {
        await gotoPage(page, loginUrl, timeout);
    }
    await page.fill(selectors.usernameInput, username);
    await page.fill(selectors.passwordInput, password);
    // 在密码框按回车走原生表单提交，不去点按钮。实测（2026-08-03，真 facebook.com）
    // 登录表单里那个 input[type=submit] 是 0×0 不可见的，而 DOM 顺序第一个
    // [role="button"] 是 24×24 的「显示密码」图标 —— 按选择器点过去只会把密码显示出来，
    // 表单一次都没提交。回车不依赖任何按钮选择器，也不受界面语言影响。
    await page.press(selectors.passwordInput, 'Enter');

    const { state, reason } = await waitForLanding(page, selectors);
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
    waitForLanding,
    ensureLogin,
    waitForManualLogin,
};
