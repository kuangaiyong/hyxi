"""帖子的跨来源索引键、父子关系组装与出口排序。

存储层始终是扁平数组 —— 整条处理链有 8 处假设如此（增量过滤、指纹合并、翻译下标对齐、
舆情绝对索引、Excel、切片、Node 端合并）。嵌套和排序只在出口（API / Excel）现场组装，
所以这里只提供「怎么算键」「怎么组树」「按什么顺序出」，不改变任何存储形态。
"""

import re
from typing import Dict, List, Tuple


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
