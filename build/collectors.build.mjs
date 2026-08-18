/**
 * 把 collectors/ 打包压缩，供便携包分发。
 *
 * ## 为什么不是字节码
 *
 * 最初打算用 bytenode 编成 V8 字节码，实跑 fixture 站点时炸了：
 *   `page.evaluate: Passed function is not well-serializable!`
 *
 * 根因是原理冲突，不是 bytenode 的 bug：**Playwright 的 `page.evaluate(fn)` 靠
 * `fn.toString()` 把函数当文本送进浏览器执行**，而 bytenode 会把函数源文本替换成
 * 等长的零宽字符（实测 `(a) => a.querySelectorAll(...)` 的 toString 长度仍是 52，
 * 内容全是 U+200B）。采集器的 DOM 提取全建立在 page.evaluate 上。
 *
 * 反过来说：**只要 page.evaluate 要能用，那段 DOM 代码就必须以可读源文本存在于进程里** ——
 * 这对 pkg / SEA / 任何字节码方案一视同仁。所以采集脚本能做到的上限就是打包 + 压缩：
 * 标识符全部改名、注释和格式抹掉、死代码消除，与任何线上 web 应用的交付形态同级。
 * 后端 Python 那边是真编译，不受这条限制。
 *
 * 用法: node build/collectors.build.mjs <输出目录>
 */

import { build } from 'esbuild';
import { mkdirSync, statSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = resolve(process.argv[2] || join(ROOT, 'build', 'out', 'collectors'));

// tweakers 和 group_feed 也要带上：前者是原始来源，后者虽然 internal=true 不进下拉框，
// 但已注册的数据源和回归测试都还在用它，缺了会在目标机器上报「采集脚本不存在」
const SCRIPTS = ['tweakers.js', 'facebook_group.js', 'group_feed.js'];

mkdirSync(OUT, { recursive: true });

for (const name of SCRIPTS) {
    const outfile = join(OUT, name);
    await build({
        entryPoints: [join(ROOT, 'collectors', name)],
        outfile,
        bundle: true,
        minify: true,
        platform: 'node',
        format: 'cjs',
        target: 'node20',
        // playwright 必须留外部：它自带浏览器驱动，打进来既没意义也会炸
        external: ['playwright'],
        legalComments: 'none',
        logLevel: 'warning',
        // **必须限行宽**。压缩成一整行时，Node 打栈回溯会把那一整行原样吐进 stderr，
        // 而 CollectorRunner 只取前 500 字符拼进 error_message —— 真正的报错会被挤掉
        lineLimit: 120,
    });

    const size = statSync(outfile).size;
    const lines = readFileSync(outfile, 'utf-8').split('\n').length;
    console.log(`  ${name}  ->  ${(size / 1024).toFixed(0)} KB / ${lines} 行`);
}

console.log(`采集脚本已打包到 ${OUT}`);
