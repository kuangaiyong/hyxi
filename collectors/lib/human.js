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

module.exports = { sleep, randInt, humanDelay, humanRead };
