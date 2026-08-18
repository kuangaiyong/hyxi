"""结果查询端点"""

import re
import csv
from io import StringIO
from collections import Counter
from datetime import datetime
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from app.models import PostsResponse, PostData, TaskStats
from app.services import source_service, storage
from app.services.excel_service import (
    ExcelService, EXPORT_COLUMNS, SENTIMENT_CN, UNANALYZED,
)
from app.services.post_tree import (
    build_tree, normalize_timestamp, order_by_thread, post_key, sort_time,
)
from app.services.sentiment_service import is_analyzable
from app.services.orchestrator import orchestrator, load_task_posts

router = APIRouter(prefix="/api/v1/tasks/{task_id}", tags=["结果"])


def _normalize_post(post: dict) -> dict:
    """规范化帖子数据（日期格式等）"""
    post = dict(post)
    post["timestamp"] = normalize_timestamp(post.get("timestamp", ""))
    return post


def _get_task_or_404(task_id: str):
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _source_names(task: dict = None) -> dict:
    """来源 id → 显示名。任务里记过的名字兜底，来源被删掉后列名不至于退成一串 id"""
    names = {s["id"]: s["name"] for s in source_service.list_sources()}
    for entry in ((task or {}).get("result") or {}).get("sources", []):
        if entry.get("id"):
            names.setdefault(entry["id"], entry.get("name") or entry["id"])
    return names


_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _safe_segment(text: str, limit: int = 20) -> str:
    """来源名要进文件名：剔掉 Windows 不接受的字符，空白收成连字符，再截断"""
    cleaned = _FILENAME_BAD.sub("", text)
    # strip 放在截断之后：先 strip 再截，正好切在连字符上就会留一个尾巴
    return re.sub(r"\s+", "-", cleaned)[:limit].strip("-. ")


def _export_filename(task: dict, task_id: str, kind: str, ext: str) -> str:
    """下载文件名：`HYXi舆情_{内容类型}_{来源}_{任务ID前8位}_{导出时间}{ext}`

    每次下载现算，所以时间是用户点下载的那一刻。**不能拿落盘名当下载名** ——
    任务 Excel 是任务跑完那一刻生成的，同一份文件下载十次都会带着同一个时间戳，
    而且那个名字里根本没有来源。
    """
    names = _source_names(task)
    labels = [
        _safe_segment(names.get(s.get("id")) or s.get("name") or "")
        for s in (task.get("result") or {}).get("sources", [])
    ]
    labels = [x for x in labels if x]
    parts = ["HYXi舆情", kind]
    if len(labels) == 1:
        parts.append(labels[0])
    elif labels:
        parts.append(f"{len(labels)}个来源")
    parts += [task_id[:8], f"{datetime.now():%Y%m%d_%H%M}"]
    return "_".join(parts) + ext


def _attachment(name: str) -> dict:
    """非 ASCII 文件名必须走 RFC 5987 编码，直接塞进 header 会被 latin-1 卡住。
    形式与 FileResponse 内部保持一致，前端 download.ts 优先读 filename*。
    """
    return {"Content-Disposition": f"attachment; filename*=utf-8''{quote(name)}"}


def _to_post_data(post: dict, index: int, names: dict, matched: bool = False) -> PostData:
    return PostData(
        index=index,
        username=post.get("username", ""),
        timestamp=normalize_timestamp(post.get("timestamp", "")),
        content=post.get("content", ""),
        translation=post.get("translation", ""),
        page_number=post.get("page_number", 1),
        source=post.get("source", ""),
        source_name=names.get(post.get("source", ""), post.get("source", "")),
        reply_level=int(post.get("reply_level", 0) or 0),
        matched=matched,
        images=post.get("images") or [],
        image_desc=post.get("image_desc") or "",
    )




@router.get("/posts", response_model=PostsResponse)
async def get_posts(
    task_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", description="搜索关键词（匹配用户名、原文、翻译）"),
):
    """获取分页帖子结果，支持全文搜索。

    分页粒度是**主贴**，评论挂在 replies 里跟着父贴走 —— 按扁平条数分页会把一个
    主贴的评论切在两页之间。搜索命中评论时连整棵子树一起返回，命中项带 matched=true，
    否则吐出来的是一堆没有上下文的孤儿评论。
    """
    task = _get_task_or_404(task_id)

    # 优先从内存，fallback 到各数据源落盘的 JSON
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = load_task_posts(task)

    roots, children = build_tree(posts)
    hit_keys = set()

    if search and search.strip():
        kw = search.strip().lower()

        def is_hit(p):
            return (kw in (p.get("username", "") or "").lower()
                    or kw in (p.get("content", "") or "").lower()
                    or kw in (p.get("translation", "") or "").lower())

        hit_keys = {post_key(p) for p in posts if is_hit(p)}
        # 命中项所属的主贴：沿 children 反查，命中评论时保留整棵子树
        parent_of = {
            post_key(c): parent for parent, kids in children.items() for c in kids
        }
        kept_roots = set()
        for key in hit_keys:
            cur = key
            while cur in parent_of:
                cur = parent_of[cur]
            kept_roots.add(cur)
        roots = [r for r in roots if post_key(r) in kept_roots]

    # 主贴按发表时间从新到旧，规则同 order_by_thread（导出走那条路）。
    # **必须排在切片之前** —— 只排页内的话，一页 50 个主贴，后面更新的帖子永远
    # 出不了第二页。
    roots.sort(key=lambda p: sort_time(p.get("timestamp")), reverse=True)

    total = len(roots)
    start = (page - 1) * page_size
    page_roots = roots[start:start + page_size]

    # index 取的是**扁平存储数组里的真实位置**（1-based），不是页内序号：
    # 舆情结果数组的下标就是这个位置（orchestrator 用 enumerate(all_posts) 建的），
    # SentimentView 靠 index-1 反查帖子。按页内计数编号的话，只要某页的主贴带了评论，
    # 该页吐出的条目数就超过 page_size，相邻两页的 index 区间会重叠、后续全部错位，
    # 详情弹窗于是显示错帖子。
    index_of = {post_key(p): i + 1 for i, p in enumerate(posts)}

    names = _source_names(task)

    def build(post) -> PostData:
        item = _to_post_data(
            post, index_of.get(post_key(post), 0), names,
            matched=bool(hit_keys) and post_key(post) in hit_keys,
        )
        item.replies = [build(c) for c in children.get(post_key(post), [])]
        return item

    items = [build(r) for r in page_roots]
    return PostsResponse(posts=items, total=total, page=page, page_size=page_size)


@router.get("/posts/{post_index}")
async def get_post_detail(task_id: str, post_index: int):
    """获取单条帖子详情"""
    task = _get_task_or_404(task_id)
    result = task.get("result") or {}
    posts = result.get("posts") or []
    if not posts:
        posts = load_task_posts(task)

    if post_index < 0 or post_index >= len(posts):
        raise HTTPException(status_code=404, detail="帖子不存在")

    return _to_post_data(posts[post_index], post_index + 1, _source_names(task))


@router.get("/stats", response_model=TaskStats)
async def get_stats(task_id: str):
    """获取统计信息"""
    task = _get_task_or_404(task_id)
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = load_task_posts(task)

    usernames = [p.get("username", "") for p in posts]
    counter = Counter(usernames)
    pages = set(p.get("page_number", 1) for p in posts)
    # 先归一化成 ISO 再排序取极值。**不能取数组首尾** —— 存储顺序是抓取顺序
    # （信息流按时间倒序渲染，增量又往后追加），与时间早晚无关，实测出现过
    # time_range_start 比 time_range_end 晚 18 天。也不能直接对落盘的
    # dd-mm-yyyy 排序：那是按「日」排先，01-07 会被排到 28-06 前面。
    timestamps = sorted(
        normalize_timestamp(p["timestamp"]) for p in posts if p.get("timestamp")
    )

    top_users = [
        {"username": user, "count": count}
        for user, count in counter.most_common(10)
    ]

    return TaskStats(
        total_posts=len(posts),
        unique_users=len(counter),
        total_pages=len(pages),
        time_range_start=timestamps[0] if timestamps else None,
        time_range_end=timestamps[-1] if timestamps else None,
        top_users=top_users,
    )


# ===== 导出 =====

def _task_sentiment(task_id: str, posts: list) -> dict:
    """这批帖子已知的舆情结论，按 posts 顺序还原成下标数组。

    结论按帖子身份取（不按 task_id 过滤）—— 同一条帖子不会被重复花钱分析，
    结论也就不该只对触发那次分析的任务可见。

    **summary 在这里现算**，不读存下来的：任务能看到的结论比它自己分析的多，
    存一份就会出现「概览说 4 条、明细列 93 条」这种自相矛盾。
    """
    data = storage.get_sentiment(task_id, posts) or {}
    if data.get("results"):
        from app.services.sentiment_service import SentimentService

        data["summary"] = SentimentService._build_summary(
            data["results"], posts, _source_names()
        )
    return data


def _export_rows(task: dict, posts: list, results: list) -> list:
    """把舆情结论贴到帖子上，再按「主贴 → 它的评论 → 下一主贴」、主贴时间从新到旧排序。

    `results[i]` 对齐的是**扁平数组**的第 i 条（下标来自 `enumerate(all_posts)`），
    而 order_by_thread 会重排。所以必须先按 post_key 建映射再排序 —— 排完再按下标取，
    每条帖子都会配上别人的情感结论，而表面看毫无异常。
    """
    by_key = {
        post_key(p): results[i]
        for i, p in enumerate(posts)
        if i < len(results) and results[i]
    }
    names = _source_names(task)
    rows = []
    for i, p in enumerate(order_by_thread(posts), 1):
        r = by_key.get(post_key(p)) or {}
        sid = p.get("source", "")
        rows.append({
            "index": i,
            "source": names.get(sid, sid),
            "level": int(p.get("reply_level", 0) or 0),
            "username": p.get("username", ""),
            "timestamp": normalize_timestamp(p.get("timestamp", "")),
            "content": p.get("content", ""),
            "translation": p.get("translation", ""),
            # 多模态读出来的图片内容。纯图帖的全部信息都在这里 —— 报告里少了它，
            # 那条帖子就只剩一行空白
            "image_desc": p.get("image_desc") or "",
            # 相对 media 的路径。Excel 拿它插缩略图，CSV 拼成文本
            "images": p.get("images") or [],
            "sentiment": SENTIMENT_CN.get(r.get("sentiment"), UNANALYZED),
            "intensity": r.get("intensity") if r.get("sentiment") else "",
            "reason": r.get("reason_cn", ""),
            "dimensions": "、".join(r.get("dimensions") or []),
        })
    return rows


def _export_meta(task: dict, rows: list, sentiment: dict) -> dict:
    """概览表要用的任务级信息。时间区间同样排序取极值，不能取首尾（见 _build_stats）"""
    stamps = sorted(r["timestamp"] for r in rows if r["timestamp"])
    users = Counter(r["username"] for r in rows if r["username"])
    return {
        "description": task.get("description", ""),
        "sources": "、".join(dict.fromkeys(r["source"] for r in rows if r["source"])),
        "exported_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "total": len(rows),
        "replies": sum(1 for r in rows if r["level"] > 0),
        "analyzed": sum(1 for r in rows if r["sentiment"] != UNANALYZED),
        "time_start": stamps[0] if stamps else "",
        "time_end": stamps[-1] if stamps else "",
        "summary": sentiment.get("summary") or {},
        "top_users": users.most_common(15),
    }


@router.get("/export")
async def export_report(task_id: str, format: str = Query("xlsx")):
    """导出原文 + 译文 + 舆情分析的单一报告。全站唯一的导出口。

    舆情没跑过照样能导，情感列填「未分析」—— 只翻译不分析的任务否则永远导不出东西。
    """
    fmt = (format or "").lower()
    if fmt not in ("xlsx", "csv"):
        raise HTTPException(status_code=400, detail="format 只支持 xlsx 或 csv")

    task = _get_task_or_404(task_id)
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = load_task_posts(task)
    if not posts:
        raise HTTPException(status_code=404, detail="无帖子数据")

    sentiment = _task_sentiment(task_id, posts)
    rows = _export_rows(task, posts, sentiment.get("results") or [])

    if fmt == "csv":
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow([label for _, label in EXPORT_COLUMNS])
        for row in rows:
            # CSV 放不下图，「配图」列给相对路径 —— 用户照着能在 media 目录里找到原图
            writer.writerow([
                "、".join(row[key]) if key == "images" else row[key]
                for key, _ in EXPORT_COLUMNS
            ])
        # utf-8-sig：没有 BOM 时 Excel 会按本地代码页打开，中文全是乱码
        content = output.getvalue().encode("utf-8-sig")
        media_type = "text/csv; charset=utf-8"
    else:
        content = ExcelService.build_export(
            rows, _export_meta(task, rows, sentiment), EXPORT_COLUMNS
        )
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    return Response(
        content=content,
        media_type=media_type,
        headers=_attachment(_export_filename(task, task_id, "分析报告", f".{fmt}")),
    )


# ===== 舆情分析 =====

@router.post("/sentiment")
async def trigger_sentiment_analysis(task_id: str, force: bool = False):
    """触发舆情分析（增量：自动跳过已分析的帖子）。

    `force=true` 忽略 `_processed.sentiment_at`，把所有可分析的帖子（有正文**或**有配图，
    见 `sentiment_service.is_analyzable`）重新分析一遍。
    用在分析口径变了的时候（比如刚接上图片理解、或改了 prompt）—— 增量粒度是帖子
    身份，不重跑的话老帖子永远停在旧口径下的结论上。**它会重新花钱**，所以不是默认。
    """
    task = _get_task_or_404(task_id)

    # 检查是否正在分析中
    if orchestrator.is_sentiment_running(task_id):
        return {"message": "舆情分析正在进行中", "task_id": task_id, "status": "running"}

    # 加载帖子数据
    result = task.get("result") or {}
    posts = result.get("posts") or []
    if not posts:
        posts = load_task_posts(task)
    if not posts:
        raise HTTPException(status_code=400, detail="没有可分析的帖子数据")

    # 增量：优先根据 _processed.sentiment_at 过滤已分析的帖子。force 时全都算待分析
    if force:
        already_analyzed = []
        pending = list(posts)
    else:
        already_analyzed = [p for p in posts if p.get("_processed", {}).get("sentiment_at")]
        pending = [p for p in posts if not p.get("_processed", {}).get("sentiment_at")]
    # 区分可分析和真空帖。**纯图帖算可分析** —— 图会先被多模态转成中文描述，
    # 判据统一在 sentiment_service.is_analyzable，三条入口不各写各的
    pending_analyzable = [p for p in pending if is_analyzable(p)]
    pending_empty = len(pending) - len(pending_analyzable)

    if not pending_analyzable:
        msg = "所有帖子已完成舆情分析"
        if pending_empty > 0:
            msg = f"所有可分析的帖子已完成舆情分析（{pending_empty} 条无正文无配图的帖子已跳过）"
        return {"message": msg, "task_id": task_id, "status": "completed"}

    # 这批帖子已知的结论（含别的任务分析过的）。它们会作为 existing_results 参与合并，
    # 保证本轮写回去的是「已有 + 本轮新增」的全量，而不是把别人的结论覆盖掉
    existing_results = _task_sentiment(task_id, posts).get("results") or []

    # 后台启动分析（仅分析增量帖子，合并已有结果）
    orchestrator.run_sentiment_async(task_id, posts, pending, existing_results)
    if force:
        msg = f"强制重新分析: {len(pending_analyzable)} 条全部重跑"
    else:
        msg = f"增量舆情分析: {len(already_analyzed)} 条已跳过, {len(pending_analyzable)} 条待分析"
    if pending_empty > 0:
        msg += f"（{pending_empty} 条无正文无配图的帖子跳过）"
    return {
        "message": msg,
        "pending_count": len(pending_analyzable),
        "total_pending": len(pending),
        "task_id": task_id,
        "status": "started",
    }


@router.get("/sentiment")
async def get_sentiment_result(task_id: str):
    """获取舆情分析结果。

    结论按帖子身份存，这里按当前帖子顺序还原成前端要的下标数组。
    """
    task = _get_task_or_404(task_id)

    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = load_task_posts(task)

    # **「分析中」必须先判**。结论跨任务共享之后，只要这批帖子里有一条被别的任务
    # 分析过，_task_sentiment 就会返回真值 —— 放在后面判的话，本轮还在跑就先把
    # 一份「看起来完整、实际少了本轮新分析那几条」的结果返回了，前端据此认为分析
    # 已结束，既不连 SSE 也不再轮询，用户只能手动刷新才看得到最终数据
    if orchestrator.is_sentiment_running(task_id):
        return {"status": "running", "message": "分析进行中..."}

    result = _task_sentiment(task_id, posts)
    if result:
        return result

    # 返回 200 + not_found 状态
    return {"status": "not_found", "message": "舆情分析结果不存在，请先触发分析"}


@router.get("/sentiment/events")
async def sentiment_events(task_id: str):
    """舆情分析 SSE 进度流"""
    # 不校验的话任意 id 都能开一条长连接，无限占用订阅槽位
    _get_task_or_404(task_id)

    from fastapi.responses import StreamingResponse
    from app.services.progress_manager import progress_manager

    return StreamingResponse(
        progress_manager.event_generator(task_id, "sentiment_complete"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
