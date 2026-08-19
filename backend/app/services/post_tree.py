"""帖子的跨来源索引键、父子关系组装与出口排序。

存储层始终是扁平数组 —— 整条处理链有 8 处假设如此（增量过滤、指纹合并、翻译下标对齐、
舆情绝对索引、Excel、切片、Node 端合并）。嵌套和排序只在出口（API / Excel）现场组装，
所以这里只提供「怎么算键」「怎么组树」「按什么顺序出」，不改变任何存储形态。
"""

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# 「近 N 天」的默认窗口与可选值。**必须是封闭集合** —— 这个数字会进 Excel 的文案和
# 概览统计，放任调用方传任意整数等于让它决定报告口径，两份报告就再也对不上了
FRESH_DAYS_DEFAULT = 7
FRESH_DAYS_CHOICES = (3, 7, 14)


def post_key(post: dict) -> str:
    """跨来源的帖子索引键。

    fingerprint 只吃 username|timestamp|content[:100]，**不含来源**，跨源合并后会碰撞
    （空内容帖尤其危险）。指纹算法绝不能改 —— 改了历史数据全部失配、已翻译的帖子会被
    判成新帖重新付费翻译一遍，所以碰撞在 Python 侧靠加来源前缀解决。
    历史数据没有 source，缺省填 tweakers 即完全兼容。
    """
    return f"{post.get('source') or 'tweakers'}:{post.get('fingerprint') or ''}"


def parent_key(post: dict) -> str:
    """父贴的索引键；不是回复则返回空串"""
    parent_fp = post.get("parent_fingerprint")
    if not parent_fp:
        return ""
    return f"{post.get('source') or 'tweakers'}:{parent_fp}"


_DUTCH_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4})\s+(.+)")


def normalize_timestamp(ts: str) -> str:
    """将荷兰/欧洲日期格式 (dd-mm-yyyy) 转为 ISO 格式 (yyyy-mm-dd)"""
    if not ts:
        return ts
    match = _DUTCH_DATE.match(ts)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)} {match.group(4)}"
    return ts


def sort_time(ts: str) -> tuple:
    """按时间排序用的键。没有时间的排在所有有时间的后面（配 reverse=True）。

    必须先转 ISO：落盘是 dd-mm-yyyy，直接按字符串排是按「日」排先，01-07 会排到
    28-06 前面。
    """
    iso = normalize_timestamp(ts or "").strip()
    return (bool(iso), iso)


def parse_time(ts: str) -> Optional[datetime]:
    """落盘的 dd-mm-yyyy HH:MM → datetime。解析不出来返回 None。

    **解析不出来必须是 None 而不是某个兜底时间**：下游据此决定「不标记」，
    随便给个默认值就会把一批读不出时间的帖子整体误判成新的或旧的。
    """
    iso = normalize_timestamp(ts or "").strip()
    if not iso:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(iso, fmt)
        except ValueError:
            continue
    return None


def baseline_time(posts: List[dict]) -> Optional[datetime]:
    """「近 N 天」的基准 = 数据集里最新的帖子时间。全都读不出时间则返回 None。

    **不用 `datetime.now()`**：那样隔一阵子没采集、或者翻看几个月前的历史报告时，
    一条都不会亮 —— 而那份报告当初想标出来的东西并没有变。报告要自洽、可复现。
    """
    stamps = [t for t in (parse_time(p.get("timestamp")) for p in posts) if t]
    return max(stamps) if stamps else None


def mark_fresh_replies(posts: List[dict], days: int = FRESH_DAYS_DEFAULT) -> Dict[str, int]:
    """找出「老主贴上的新回复」，返回 {回复的 post_key: 距其主贴的天数}。

    出口按「主贴发表时间从新到旧」排、评论跟着自己的主贴走（见 order_by_thread），
    于是**今天发在两个月前主贴上的回复，会被排到两个月前的位置去**，用户根本翻不到。
    真实数据里有一条 08-15 的回复挂在 06-06 的主贴上，排在第 40 多个主贴之后。

    判据：这条是回复 且 它的时间 >= 基准-days 且 它的**顶层主贴**时间 < 基准-days。

    三条边界都是有意为之：
    · **主贴也在窗口内 → 不标**。整串都是新的，它本来就排在最前面，没被埋
    · **时间读不出来 → 不标**（回复和主贴任一为空都算）。早期采集读不到 tooltip 时是
      故意留空的，这批帖子实际很新 —— 但那是推测，宁可漏标也不能凭空标一条出来
    · **父贴不在本批数据里的回复 → 不标**。build_tree() 已把它按主贴处理，它就不是回复

    判定按**顶层主贴**算（thread_of 已把嵌套回复归到顶层），跨来源用 post_key。
    """
    base = baseline_time(posts)
    if not base:
        return {}
    cutoff = base - timedelta(days=days)

    threads = thread_of(posts)
    fresh: Dict[str, int] = {}
    for post in posts:
        if not post.get("parent_fingerprint"):
            continue
        thread = threads.get(post_key(post)) or []
        # 串首就是顶层主贴。孤儿回复自成一串，串首是它自己 —— 那它不算回复
        if len(thread) < 2 or post_key(thread[0]) == post_key(post):
            continue
        reply_at = parse_time(post.get("timestamp"))
        root_at = parse_time(thread[0].get("timestamp"))
        if not reply_at or not root_at:
            continue
        if reply_at >= cutoff and root_at < cutoff:
            fresh[post_key(post)] = (reply_at - root_at).days
    return fresh


def build_tree(posts: List[dict]) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """组成 (roots, children)，children 的键是父贴的 post_key。

    父贴不在本批数据里的评论按主贴处理 —— 否则增量只抓到评论那一轮会把它们整个丢掉。
    """
    by_key = {post_key(p): p for p in posts}
    children: Dict[str, List[dict]] = {}
    roots: List[dict] = []
    for p in posts:
        pk = parent_key(p)
        if pk and pk in by_key:
            children.setdefault(pk, []).append(p)
        else:
            roots.append(p)
    return roots, children


def thread_of(posts: List[dict]) -> Dict[str, List[dict]]:
    """每条帖子 → 它所属的完整讨论串（主贴在前，其余按 posts 里的原顺序）。

    舆情分析拿它做上下文：回复贴大量是「+1」「same here」，只看自己判出来全是
    neutral 噪音，只看父贴前 200 字也丢掉了同串其他人的信息。

    嵌套回复归到它的**顶层主贴**那一串，不按层级再分组 —— 讨论是围绕主贴发生的，
    按子树切会把同一场讨论劈成几段。父贴不在本批数据里的回复自成一串，
    与 build_tree() 的既有语义一致。
    """
    roots, children = build_tree(posts)
    mapping: Dict[str, List[dict]] = {}
    for root in roots:
        thread: List[dict] = []

        def walk(post: dict):
            thread.append(post)
            for child in children.get(post_key(post), []):
                walk(child)

        walk(root)
        for member in thread:
            mapping[post_key(member)] = thread
    return mapping


def order_by_thread(posts: List[dict]) -> List[dict]:
    """按「主贴 → 它的评论 → 下一个主贴」重排，主贴按发表时间从新到旧，并回填 reply_level。

    评论跟着自己的主贴走、不参与排序 —— 它们本来就该按发表先后读。没解析出时间的主贴
    沉到最后（早期采集读不到 tooltip 的绝对时间时是故意留空的，这批帖子实际很新），
    它们之间靠稳定排序保持采集顺序。

    这里只改呈现次序，存储层的 seq 不动 —— 它是全链路的顺序锚点，舆情结论按它对齐。
    """
    roots, children = build_tree(posts)
    roots = sorted(roots, key=lambda p: sort_time(p.get("timestamp")), reverse=True)
    ordered: List[dict] = []

    def walk(post: dict, level: int):
        post["reply_level"] = level
        ordered.append(post)
        for child in children.get(post_key(post), []):
            walk(child, level + 1)

    for root in roots:
        walk(root, 0)
    return ordered
