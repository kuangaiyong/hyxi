function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function randInt(min, max) { return min + Math.floor(Math.random() * (max - min + 1)); }

function humanDelay(min, max) { return sleep(randInt(min, max)); }

// 落地即抓、整页零输入事件的访问在行为分析里很扎眼
async function humanRead(page) {
    for (let i = randInt(2, 4); i > 0; i--) {
        await page.mouse.wheel(0, randInt(300, 900)).catch(() => {});
        await sleep(randInt(400, 1400));
    }
}

// 逐字输入而不是一次性 fill()。fill() 是直接改值再补一个 input 事件，落到行为分析里
// 就是「零耗时填完一整个密码」；这里走真实键盘事件，节奏也随机。
//
// 这只影响**要不要弹验证**，不影响**弹出来之后能不能过** —— 后者是解题，属于「明确不做」。
async function humanType(page, selector, text) {
    await page.click(selector);
    await sleep(randInt(120, 400));
    await page.type(selector, text, { delay: randInt(55, 130) });
    await sleep(randInt(200, 600));
}

module.exports = { sleep, randInt, humanDelay, humanRead, humanType };
