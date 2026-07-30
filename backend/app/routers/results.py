"""结果查询端点"""

import os
import re
import json
import glob
from collections import Counter
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from app.models import PostsResponse, PostData, TaskStats
from app.config import settings
from app.services.orchestrator import orchestrator

router = APIRouter(prefix="/api/v1/tasks/{task_id}", tags=["结果"])


def _normalize_timestamp(ts: str) -> str:
    """将荷兰/欧洲日期格式 (dd-mm-yyyy) 转为 ISO 格式 (yyyy-mm-dd)"""
    if not ts:
        return ts
    match = re.match(r"(\d{2})-(\d{2})-(\d{4})\s+(.+)", ts)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)} {match.group(4)}"
    return ts


def _normalize_post(post: dict) -> dict:
    """规范化帖子数据（日期格式等）"""
    post = dict(post)
    post["timestamp"] = _normalize_timestamp(post.get("timestamp", ""))
    return post


def _get_task_or_404(task_id: str):
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


def _load_posts_from_json(task: dict) -> list:
    """从 JSON 文件加载帖子数据"""
    # 方法1: 从任务 plan 中找 thread_id（包括 translate/generate_excel 步骤）
    thread_id = None
    plan = task.get("plan", [])
    for step in plan:
        tid = step.get("params", {}).get("thread_id")
        if tid:
            thread_id = tid
            break

    # 方法2: 从任务描述中提取
    if not thread_id:
        desc = task.get("description", "")
        match = re.search(r"(\d{5,})", desc)
        if match:
            thread_id = int(match.group(1))

    # 方法3: 扫描项目根目录下的 tweakers_thread_*.json
    if not thread_id:
        json_files = glob.glob(os.path.join(settings.project_root, "tweakers_thread_*.json"))
        if json_files:
            json_files.sort(key=os.path.getmtime, reverse=True)
            thread_id_match = re.search(r"tweakers_thread_(\d+)\.json", json_files[0])
            if thread_id_match:
                thread_id = int(thread_id_match.group(1))

    if not thread_id:
        return []

    json_path = os.path.join(settings.project_root, f"tweakers_thread_{thread_id}.json")
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("posts", [])

    return []


@router.get("/posts", response_model=PostsResponse)
async def get_posts(
    task_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str = Query("", description="搜索关键词（匹配用户名、原文、翻译）"),
):
    """获取分页帖子结果，支持全文搜索"""
    task = _get_task_or_404(task_id)

    # 优先从内存，fallback 到 JSON 文件
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = _load_posts_from_json(task)

    # 全文搜索过滤
    if search and search.strip():
        kw = search.strip().lower()
        posts = [
            p for p in posts
            if kw in (p.get("username", "") or "").lower()
            or kw in (p.get("content", "") or "").lower()
            or kw in (p.get("translation", "") or "").lower()
        ]

    total = len(posts)
    start = (page - 1) * page_size
    end = start + page_size
    page_posts = posts[start:end]

    items = [
        PostData(
            index=i + 1,
            username=p.get("username", ""),
            timestamp=_normalize_timestamp(p.get("timestamp", "")),
            content=p.get("content", ""),
            translation=p.get("translation", ""),
            page_number=p.get("page_number", 1),
        )
        for i, p in enumerate(page_posts, start=start)
    ]

    return PostsResponse(posts=items, total=total, page=page, page_size=page_size)


@router.get("/posts/{post_index}")
async def get_post_detail(task_id: str, post_index: int):
    """获取单条帖子详情"""
    task = _get_task_or_404(task_id)
    result = task.get("result") or {}
    posts = result.get("posts") or []
    if not posts:
        posts = _load_posts_from_json(task)

    if post_index < 0 or post_index >= len(posts):
        raise HTTPException(status_code=404, detail="帖子不存在")

    p = posts[post_index]
    return PostData(
        index=post_index + 1,
        username=p.get("username", ""),
        timestamp=_normalize_timestamp(p.get("timestamp", "")),
        content=p.get("content", ""),
        translation=p.get("translation", ""),
        page_number=p.get("page_number", 1),
    )


@router.get("/stats", response_model=TaskStats)
async def get_stats(task_id: str):
    """获取统计信息"""
    task = _get_task_or_404(task_id)
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = _load_posts_from_json(task)

    usernames = [p.get("username", "") for p in posts]
    counter = Counter(usernames)
    pages = set(p.get("page_number", 1) for p in posts)
    timestamps = [p.get("timestamp", "") for p in posts if p.get("timestamp")]

    top_users = [
        {"username": user, "count": count}
        for user, count in counter.most_common(10)
    ]

    return TaskStats(
        total_posts=len(posts),
        unique_users=len(counter),
        total_pages=len(pages),
        time_range_start=_normalize_timestamp(timestamps[0]) if timestamps else None,
        time_range_end=_normalize_timestamp(timestamps[-1]) if timestamps else None,
        top_users=top_users,
    )


@router.get("/download")
async def download_excel(task_id: str):
    """下载生成的 Excel 文件"""
    task = _get_task_or_404(task_id)
    result = task.get("result") or {}
    excel_path = result.get("excel_path", "")

    if not excel_path or not os.path.exists(excel_path):
        raise HTTPException(status_code=404, detail="Excel 文件不存在或未生成")

    file_name = os.path.basename(excel_path)
    return FileResponse(
        path=excel_path,
        filename=file_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/export/csv")
async def export_csv(task_id: str):
    """导出帖子数据为 CSV"""
    task = _get_task_or_404(task_id)
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = _load_posts_from_json(task)
    if not posts:
        raise HTTPException(status_code=404, detail="无帖子数据")

    import csv
    from io import StringIO
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["序号", "用户名", "时间", "原文", "中文翻译", "页码"])
    for i, p in enumerate(posts, 1):
        writer.writerow([
            i,
            p.get("username", ""),
            _normalize_timestamp(p.get("timestamp", "")),
            p.get("content", ""),
            p.get("translation", ""),
            p.get("page_number", ""),
        ])
    from fastapi.responses import Response
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=posts_{task_id[:8]}.csv"},
    )


@router.get("/export/json")
async def export_json(task_id: str):
    """导出帖子数据为 JSON"""
    task = _get_task_or_404(task_id)
    posts = (task.get("result") or {}).get("posts") or []
    if not posts:
        posts = _load_posts_from_json(task)
    if not posts:
        raise HTTPException(status_code=404, detail="无帖子数据")

    import json as _json
    from fastapi.responses import Response
    return Response(
        content=_json.dumps(posts, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=posts_{task_id[:8]}.json"},
    )


# ===== 舆情分析 =====

@router.post("/sentiment")
async def trigger_sentiment_analysis(task_id: str):
    """触发舆情分析（增量：自动跳过已分析的帖子）"""
    task = _get_task_or_404(task_id)

    # 检查是否正在分析中
    if orchestrator.is_sentiment_running(task_id):
        return {"message": "舆情分析正在进行中", "task_id": task_id, "status": "running"}

    # 加载帖子数据
    result = task.get("result") or {}
    posts = result.get("posts") or []
    if not posts:
        posts = _load_posts_from_json(task)
    if not posts:
        raise HTTPException(status_code=400, detail="没有可分析的帖子数据")

    # 增量：优先根据 _processed.sentiment_at 过滤已分析的帖子
    already_analyzed = [p for p in posts if p.get("_processed", {}).get("sentiment_at")]
    pending = [p for p in posts if not p.get("_processed", {}).get("sentiment_at")]
    # 区分有内容和空内容（空内容帖子不会被 LLM 分析）
    pending_with_content = [p for p in pending if (p.get("content") or "").strip()]
    pending_empty = len(pending) - len(pending_with_content)

    if not pending_with_content:
        msg = "所有帖子已完成舆情分析"
        if pending_empty > 0:
            msg = f"所有有内容的帖子已完成舆情分析（{pending_empty} 条空内容帖子已跳过）"
        return {"message": msg, "task_id": task_id, "status": "completed"}

    # 只读本任务自己的舆情文件：猜来的文件会被当作 existing_results 合并后写回目标任务
    existing_results = {}
    sentiment_path = os.path.join(settings.data_dir, f"sentiment_{task_id}.json")
    if os.path.exists(sentiment_path):
        with open(sentiment_path, "r", encoding="utf-8") as f:
            existing_data = json.load(f)
            existing_results = existing_data.get("results", [])

    # 后台启动分析（仅分析增量帖子，合并已有结果）
    orchestrator.run_sentiment_async(task_id, posts, pending, existing_results)
    msg = f"增量舆情分析: {len(already_analyzed)} 条已跳过, {len(pending_with_content)} 条待分析"
    if pending_empty > 0:
        msg += f"（{pending_empty} 条空内容帖子跳过）"
    return {
        "message": msg,
        "pending_count": len(pending_with_content),
        "total_pending": len(pending),
        "task_id": task_id,
        "status": "started",
    }


@router.get("/sentiment")
async def get_sentiment_result(task_id: str):
    """获取舆情分析结果（优先 SQLite，其次本任务的 JSON 文件）"""
    # 校验任务存在：task_id 会被拼进文件路径，未校验则可构造穿越路径读取任意文件
    _get_task_or_404(task_id)

    # 优先从 SQLite 获取
    from app.services.storage import get_sentiment
    result = get_sentiment(task_id)
    if result:
        return result

    # Fallback: JSON 文件
    sentiment_path = os.path.join(settings.data_dir, f"sentiment_{task_id}.json")
    if os.path.exists(sentiment_path):
        with open(sentiment_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 检查是否正在分析
    if orchestrator.is_sentiment_running(task_id):
        return {"status": "running", "message": "分析进行中..."}

    # 返回 200 + not_found 状态
    return {"status": "not_found", "message": "舆情分析结果不存在，请先触发分析"}


@router.get("/sentiment/download")
async def download_sentiment_excel(task_id: str):
    """下载舆情分析 Excel 报告"""
    # 同 get_sentiment_result：task_id 参与拼路径，必须先校验其为真实任务
    _get_task_or_404(task_id)

    sentiment_path = os.path.join(settings.data_dir, f"sentiment_{task_id}.json")
    if not os.path.exists(sentiment_path):
        raise HTTPException(status_code=404, detail="舆情分析结果不存在，请先触发分析")

    with open(sentiment_path, "r", encoding="utf-8") as f:
        sentiment_data = json.load(f)

    from app.services.excel_service import ExcelService
    result = ExcelService.generate_sentiment_report(task_id, sentiment_data)

    return FileResponse(
        path=result["file_path"],
        filename=result["file_name"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/sentiment/events")
async def sentiment_events(task_id: str):
    """舆情分析 SSE 进度流"""
    # 不校验的话任意 id 都能开一条长连接，无限占用订阅槽位
    _get_task_or_404(task_id)

    from fastapi.responses import StreamingResponse
    from app.services.progress_manager import progress_manager

    return StreamingResponse(
        progress_manager.event_generator(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
