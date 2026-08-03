"""帖子的跨来源索引键与父子关系组装。

存储层始终是扁平数组 —— 整条处理链有 8 处假设如此（增量过滤、指纹合并、翻译下标对齐、
舆情绝对索引、Excel、切片、Node 端合并）。嵌套只在出口（API / Excel）现场组装，
所以这里只提供「怎么算键」和「怎么组树」，不改变任何存储形态。
"""

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
    """按「主贴 → 它的评论 → 下一个主贴」重排，并回填 reply_level。

    没有任何回复时原样返回，避免给纯论坛数据平白做一次重排。
    """
    if not any(p.get("parent_fingerprint") for p in posts):
        return posts

    roots, children = build_tree(posts)
    ordered: List[dict] = []

    def walk(post: dict, level: int):
        post["reply_level"] = level
        ordered.append(post)
        for child in children.get(post_key(post), []):
            walk(child, level + 1)

    for root in roots:
        walk(root, 0)
    return ordered
