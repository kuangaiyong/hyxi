const crypto = require('crypto');

// 指纹生成（用户名 + 时间戳 + 内容前100字符）
//
// 这是增量去重、跨文件结果合并、_processed 标记的唯一锚点。改动这里会让全部历史数据
// 失配 —— 已翻译的帖子会被判为新帖，重复付费翻译且舆情重复计数。一个字符都不要动。
function makeFingerprint(post) {
    const raw = `${post.username}|${post.timestamp}|${(post.content || '').slice(0, 100)}`;
    return crypto.createHash('sha256').update(raw).digest('hex').slice(0, 16);
}

module.exports = { makeFingerprint };
