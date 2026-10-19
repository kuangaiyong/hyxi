/**
 * 「邮件通知」页 —— 真 Chrome、真前后端、真 SMTP（脚本自带一个本机收件端），无 mock。
 *
 * 守的是用户在这一页上的整段操作：
 *   1. 侧栏点得进来，标题对；配置卡片里不放说明文字（用户要求，保存后开着定时再查一次）；
 *      输入框的占位提示里没有 QQ 之类的邮箱厂商信息（用户要求）
 *   2. 填好表单点「发送测试邮件」：收件端真的收到了，登录用的就是页面上填的用户名和密码
 *   3. 保存后刷新：填的都还在；密码框是空的、提示「已保存」；任何一次读配置的响应里都没有密码
 *   4. 改了没保存时「立即发送」点不了（它用的是已保存的配置，免得用户以为发的是眼前这份）
 *   5. 点「立即发送」：按数据走三条路之一，每条都有硬断言 ——
 *      已发送（收件端收到、标题和发送记录一致）/ 没有新内容（明说、没发信）/ 失败（明说、没发信）
 *   6. 开了每天定时，发送记录里显示下次发送时间
 *
 * 收件端只听 127.0.0.1、不加密：后端对本机中继允许不加密登录，对外部服务器不允许（后端用例守那一条）。
 * 收尾会调接口「清除」配置，开发库里不留痕。**已经有邮件通知配置时以退出码 2 拒跑** ——
 * 密码只进不出，覆盖掉就还原不回来；确实要跑就设 E2E_NOTIFY_OVERWRITE=1（跑完配置会被清掉）。
 * 第 5 步若窗口里有缺译文的新回复，会用「LLM 配置」里的模型补译（同结果页补译，按条计费）。
 *
 * 前置：`.\start.ps1` 起两个服务（改过后端要重启它）。
 *
 *   node frontend/e2e/notify_settings.js
 *   # 或： cd frontend; npm run e2e:notify
 */
import { chromium } from 'playwright';
import { readFileSync } from 'fs';
import net from 'net';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FRONTEND = 'http://localhost:5173';
const BACKEND = 'http://127.0.0.1:8000';
const VIEWPORT = { width: 1280, height: 900 };

function apiKey() {
    try {
        const m = readFileSync(join(ROOT, '.env'), 'utf-8').match(/^TWEAKERS_API_KEY=(.*)$/m);
        return (m && m[1].trim()) || '';
    } catch {
        return '';
    }
}
const KEY = apiKey();
const api = (path, init = {}) => fetch(`${BACKEND}/api/v1${path}`, {
    ...init, headers: { 'Content-Type': 'application/json', ...(KEY ? { 'X-API-Key': KEY } : {}), ...(init.headers || {}) },
});

let failures = 0;
function check(name, ok, detail) {
    console.log(`  ${ok ? '✅' : '❌'} ${name}`);
    if (!ok) {
        failures++;
        if (detail !== undefined) console.log(`       ${JSON.stringify(detail)}`);
    }
}

/** 最小 SMTP 收件端：EHLO / AUTH PLAIN / MAIL / RCPT / DATA / QUIT。按字节（latin1）切行，正文里的 UTF-8 不受影响 */
function startSmtp() {
    const mails = [];
    const auths = [];
    const server = net.createServer((sock) => {
        let buf = '';
        let inData = false;
        let lines = [];
        let session = { from: '', to: [] };
        const send = (l) => sock.write(`${l}\r\n`);
        send('220 e2e ESMTP');
        sock.on('data', (chunk) => {
            buf += chunk.toString('latin1');
            let i;
            while ((i = buf.indexOf('\r\n')) >= 0) {
                const line = buf.slice(0, i);
                buf = buf.slice(i + 2);
                if (inData) {
                    if (line === '.') {
                        inData = false;
                        mails.push({ ...session, raw: lines.join('\r\n') });
                        lines = [];
                        send('250 queued');
                    } else {
                        lines.push(line.startsWith('..') ? line.slice(1) : line);
                    }
                    continue;
                }
                const [cmd, ...rest] = line.split(' ');
                const arg = rest.join(' ');
                switch (cmd.toUpperCase()) {
                    case 'EHLO': case 'HELO': send('250-e2e'); send('250 AUTH PLAIN'); break;
                    case 'AUTH': {
                        const [, user, pass] = Buffer.from(arg.split(' ')[1] || '', 'base64').toString('utf8').split('\0');
                        auths.push({ user, pass });
                        send('235 ok');
                        break;
                    }
                    case 'MAIL': session = { from: arg, to: [] }; send('250 ok'); break;
                    case 'RCPT': session.to.push(arg); send('250 ok'); break;
                    case 'DATA': inData = true; send('354 go ahead'); break;
                    case 'RSET': case 'NOOP': send('250 ok'); break;
                    case 'QUIT': send('221 bye'); sock.end(); break;
                    default: send('502 not implemented');
                }
            }
        });
        sock.on('error', () => {});
    });
    return new Promise((resolve) => server.listen(0, '127.0.0.1',
        () => resolve({ server, port: server.address().port, mails, auths })));
}

/** 邮件头里的 RFC 2047 编码词（=?utf-8?b?...?= / ?q?）解回中文 */
function decodeWords(v) {
    return v.replace(/=\?([^?]+)\?([bBqQ])\?([^?]*)\?=(\s+(?==\?))?/g, (_, _cs, enc, text) => {
        const bytes = enc.toUpperCase() === 'B'
            ? Buffer.from(text, 'base64')
            : Buffer.from(text.replace(/_/g, ' ').replace(/=([0-9A-Fa-f]{2})/g,
                (__, h) => String.fromCharCode(parseInt(h, 16))), 'latin1');
        return bytes.toString('utf8');
    });
}
function subjectOf(mail) {
    const head = mail.raw.split('\r\n\r\n')[0].replace(/\r\n[ \t]+/g, ' ');
    const m = head.match(/^Subject: (.*)$/mi);
    return m ? decodeWords(m[1]) : '';
}

async function preflight() {
    try {
        const r = await fetch(FRONTEND);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        console.error(`前端 ${FRONTEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        process.exit(2);
    }
    let cfg;
    try {
        const r = await api('/notify/config');
        if (r.status === 404) {
            console.error('后端没有 /api/v1/notify/config：8000 上跑的是改动前的代码，重启 .\\start.ps1');
            process.exit(2);
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        cfg = await r.json();
    } catch (e) {
        console.error(`后端 ${BACKEND} 没响应：${e.message}\n先跑 .\\start.ps1`);
        process.exit(2);
    }
    if (cfg.is_configured && process.env.E2E_NOTIFY_OVERWRITE !== '1') {
        console.error('这台后端上已经有邮件通知配置。密码只进不出，被这个脚本覆盖后还原不回来，所以不跑。\n'
            + '确实要跑就设 E2E_NOTIFY_OVERWRITE=1（跑完配置会被清掉）');
        process.exit(2);
    }
    const sources = await (await api('/sources')).json();
    if (!sources.length) {
        console.error('后端没有任何数据源，邮件通知无从选起');
        process.exit(2);
    }
    return sources;
}

const toasts = (page) => page.locator('.toast-item').allTextContents();
const waitToast = (page, re, ms = 8000) => page.waitForFunction(
    (src) => [...document.querySelectorAll('.toast-item')].some((t) => new RegExp(src).test(t.textContent || '')),
    re.source, { timeout: ms },
).then(() => true, () => false);
const byId = (page, id) => page.locator(`[data-testid="${id}"]`);

/**
 * 两张配置卡片里的说明文字（用户要求这一页不放）。标题、输入框、按钮不算；标签自己的文字是字段名，不算，
 * 但标签里再套一层元素写的字照样算。allowed 逐段放行字段名（定时那一行的「每天」「发送」），多一个字都不行。
 * 发送记录是结果，不在其列。
 */
const strayText = (page, allowed = []) => page.evaluate((allowed) => {
    const out = [];
    const cards = [...document.querySelectorAll('.card')]
        .filter((c) => c.querySelector('[data-testid^="notify-"]') && c.dataset.testid !== 'notify-status');
    for (const card of cards) {
        const clone = card.cloneNode(true);
        clone.querySelectorAll('.card-header, input, textarea, select, button').forEach((el) => el.remove());
        clone.querySelectorAll('label').forEach((l) => [...l.childNodes]
            .filter((n) => n.nodeType === Node.TEXT_NODE).forEach((n) => n.remove()));
        const walker = document.createTreeWalker(clone, NodeFilter.SHOW_TEXT);
        for (let n = walker.nextNode(); n; n = walker.nextNode()) {
            const t = n.textContent.replace(/\s+/g, ' ').trim();
            if (t && !allowed.includes(t)) out.push(t);
        }
    }
    return out;
}, allowed);

async function main() {
    const sources = await preflight();
    const smtp = await startSmtp();
    const source = sources[0];
    const PASSWORD = `e2e-pw-${Date.now()}`;
    const SENDER = 'e2e-sender@example.test';
    const RECIPIENTS = ['e2e-a@example.test', 'e2e-b@example.test'];
    console.log(`本机收件端：127.0.0.1:${smtp.port}；勾选数据源：${source.collector_name}「${source.name}」\n`);

    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    const context = await browser.newContext({ viewport: VIEWPORT });
    await context.addInitScript((key) => { if (key) localStorage.setItem('hyxi_api_key', key); }, KEY);
    const page = await context.newPage();
    page.on('dialog', (d) => d.accept());
    const configBodies = [];
    page.on('response', async (r) => {
        if (r.url().endsWith('/api/v1/notify/config')) {
            try { configBodies.push(await r.text()); } catch { /* 页面跳走时响应体可能已经拿不到 */ }
        }
    });

    try {
        console.log('场景一：侧栏点得进来');
        await page.goto(`${FRONTEND}/tasks`, { waitUntil: 'networkidle' });
        await page.locator('.sidebar-nav a[href="/notify"]').click();
        await page.waitForURL('**/notify');
        await byId(page, 'notify-recipients').waitFor();
        check('页面标题是「邮件通知」', (await page.locator('.app-header h2').textContent()).trim() === '邮件通知');
        // 表单一挂上就渲染，数据源要等接口回来；在那之前卡片里是「还没有数据源」的空态，不能拿来判
        await page.locator('[data-testid="notify-source"]').first().waitFor();
        const stray = await strayText(page);
        check('配置卡片里只有标题、字段和按钮，没有说明文字', stray.length === 0, stray);
        // 用户要求：输入框里不要出现 QQ 之类的邮箱厂商信息（占位提示也算）
        const vendor = await page.$$eval('.card input, .card textarea', (els) => els
            .map((e) => e.getAttribute('placeholder') || '')
            .filter((p) => /qq|163|126|foxmail|sina|aliyun|gmail|outlook|hotmail|yahoo/i.test(p)));
        check('输入框里没有 QQ 之类的邮箱厂商信息', vendor.length === 0, vendor);

        console.log('\n场景二：填表、发送测试邮件，收件端真的收到');
        await byId(page, 'notify-recipients').fill(RECIPIENTS.join('\n'));
        await page.locator(`[data-testid="notify-source"][value="${source.id}"]`).check();
        await byId(page, 'notify-days').selectOption({ label: '近 3 天' });
        await byId(page, 'notify-smtp-host').fill('127.0.0.1');
        await byId(page, 'notify-smtp-port').fill(String(smtp.port));
        await byId(page, 'notify-use-ssl').uncheck();
        await byId(page, 'notify-sender').fill(SENDER);
        await byId(page, 'notify-username').fill(SENDER);
        await byId(page, 'notify-password').fill(PASSWORD);
        await byId(page, 'notify-test').click();
        await byId(page, 'notify-test-result').waitFor({ timeout: 40000 });
        const testText = (await byId(page, 'notify-test-result').textContent()).trim();
        check('页面说测试邮件已发出', /已发出/.test(testText), testText);
        check('收件端收到 1 封、标题是测试邮件', smtp.mails.length === 1 && /测试/.test(subjectOf(smtp.mails[0])),
            smtp.mails.map(subjectOf));
        check('登录用的就是页面上填的用户名和密码', smtp.auths.length === 1
            && smtp.auths[0].user === SENDER && smtp.auths[0].pass === PASSWORD, smtp.auths.map((a) => a.user));
        check('两个收件人都在信封上', smtp.mails[0] && smtp.mails[0].to.length === 2, smtp.mails[0] && smtp.mails[0].to);

        console.log('\n场景三：保存、刷新，填的都还在，密码读不回来');
        await byId(page, 'notify-schedule').check();
        await byId(page, 'notify-schedule-time').fill('07:30');
        await byId(page, 'notify-save').click();
        check('提示已保存', await waitToast(page, /已保存/), await toasts(page));
        await page.reload({ waitUntil: 'networkidle' });
        await byId(page, 'notify-recipients').waitFor();
        const form = {
            recipients: await byId(page, 'notify-recipients').inputValue(),
            sourceChecked: await page.locator(`[data-testid="notify-source"][value="${source.id}"]`).isChecked(),
            days: await byId(page, 'notify-days').evaluate((el) => el.options[el.selectedIndex].text),
            port: await byId(page, 'notify-smtp-port').inputValue(),
            ssl: await byId(page, 'notify-use-ssl').isChecked(),
            schedule: await byId(page, 'notify-schedule').isChecked(),
            time: await byId(page, 'notify-schedule-time').inputValue(),
            password: await byId(page, 'notify-password').inputValue(),
            placeholder: await byId(page, 'notify-password').getAttribute('placeholder'),
        };
        check('收件人、数据源、时间窗口、端口、SSL、定时都还在',
            form.recipients.split('\n').join(',') === RECIPIENTS.join(',') && form.sourceChecked
            && form.days === '近 3 天' && form.port === String(smtp.port) && !form.ssl
            && form.schedule && form.time === '07:30', form);
        check('密码框是空的，提示「已保存」', form.password === '' && /已保存/.test(form.placeholder || ''), form);
        check(`读配置的 ${configBodies.length} 次响应里都没有密码`,
            configBodies.length >= 2 && configBodies.every((b) => !b.includes(PASSWORD)), configBodies.length);
        const nextRun = (await byId(page, 'notify-next-run').textContent()).trim();
        check('发送记录里显示下次定时发送的时间', /下次定时发送：.*07:30/.test(nextRun), nextRun);
        const strayLater = await strayText(page, ['每天', '发送']);
        check('保存过、开着定时：配置卡片里也没有说明文字', strayLater.length === 0, strayLater);

        console.log('\n场景四：改了没保存，「立即发送」点不了');
        const send = byId(page, 'notify-send');
        check('刚保存完可以点', await send.isEnabled());
        await byId(page, 'notify-recipients').fill(`${RECIPIENTS.join('\n')}\nextra@example.test`);
        check('改了收件人没保存：点不了', await send.isDisabled());
        await byId(page, 'notify-recipients').fill(RECIPIENTS.join('\n'));
        check('改回去：又可以点', await send.isEnabled());

        console.log('\n场景五：立即发送，结果照实说');
        const before = smtp.mails.length;
        await send.click();
        await page.waitForFunction(() => {
            const last = document.querySelector('[data-testid="notify-last-outcome"]');
            return last && !document.querySelector('[data-testid="notify-progress"]');
        }, null, { timeout: 15 * 60 * 1000 });
        const outcome = (await byId(page, 'notify-last-outcome').textContent()).trim();
        const message = (await byId(page, 'notify-last-message').textContent()).trim();
        const status = await (await api('/notify/status')).json();
        const newMails = smtp.mails.slice(before);
        console.log(`  → ${outcome}：${message}`);
        if (status.last.outcome === 'sent') {
            check('收件端收到 1 封摘要', newMails.length === 1, newMails.length);
            check('邮件标题和发送记录一致', newMails[0] && subjectOf(newMails[0]) === status.last.subject,
                { 收到: newMails[0] && subjectOf(newMails[0]), 记录: status.last.subject });
            check('标题写明数据源和时间窗口', status.last.subject.includes(source.name)
                && status.last.subject.includes('近 3 天'), status.last.subject);
        } else if (status.last.outcome === 'skipped') {
            // 两种「没新内容」：窗口里一条都没有；或者窗口里的前几封都发过（v1.15.0 起不再重发一封全是「已发过」的信）
            check('明说近 3 天没有（首次出现的）新回复', /近 3 天没有(首次出现的)?新回复/.test(message), message);
            check('没有发信', newMails.length === 0, newMails.length);
        } else {
            check('失败时明说没有发送', /没有发送|失败/.test(message), message);
            check('失败时没有发信', newMails.length === 0, newMails.length);
        }
    } finally {
        await browser.close();
        smtp.server.close();
        const r = await api('/notify/config', { method: 'DELETE' });
        console.log(r.ok ? '\n已清除本脚本保存的邮件通知配置' : `\n清除配置失败（HTTP ${r.status}），请到页面上点「清除」`);
    }
}

main().then(() => {
    console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项未通过`);
    process.exit(failures === 0 ? 0 : 1);
}).catch((e) => {
    console.error('\n跑挂了:', e.message);
    process.exit(1);
});
