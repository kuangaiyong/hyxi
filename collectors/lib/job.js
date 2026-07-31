const fs = require('fs');
const path = require('path');

// 采集器与 Python runner 之间的全部约定都在这里：
//   入参 → 一个 JSON job 文件（--job=<path>），不再有 argv 分支
//   进度 → stdout 上的 NDJSON 事件行，解析不出 JSON 的行 runner 原样当日志转发
//   出参 → job.output_path 指定的文件，调用方不再猜文件名
function readJob(argv) {
    const arg = argv.find(a => a.startsWith('--job='));
    if (!arg) throw new Error('缺少 --job=<path> 参数');
    const raw = fs.readFileSync(arg.slice('--job='.length), 'utf-8');
    return JSON.parse(raw);
}

function emit(event) {
    process.stdout.write(JSON.stringify(event) + '\n');
}

function log(msg) {
    console.log(`[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}] ${msg}`);
}

function progress(current, total, msg) {
    emit({ evt: 'progress', current, total, msg });
}

function writeOutput(job, result) {
    const dir = path.dirname(job.output_path);
    if (dir) fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(job.output_path, JSON.stringify(result, null, 2), 'utf-8');
}

module.exports = { readJob, emit, log, progress, writeOutput };
