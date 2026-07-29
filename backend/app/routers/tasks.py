"""任务管理端点"""

import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.models import TaskCreate, TaskResponse, TaskListResponse, TaskStatus
from app.services.orchestrator import orchestrator
from app.services.progress_manager import progress_manager

router = APIRouter(prefix="/api/v1/tasks", tags=["任务"])


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(body: TaskCreate):
    """提交新任务"""
    task_id = str(uuid.uuid4())
    orchestrator.create_task(task_id, body.description)

    # 后台异步执行
    orchestrator.run_task_async(task_id)

    task = orchestrator.get_task(task_id)
    return TaskResponse(**task)


@router.get("", response_model=TaskListResponse)
async def list_tasks():
    """获取所有任务列表"""
    tasks = orchestrator.get_all_tasks()
    return TaskListResponse(
        tasks=[TaskResponse(**t) for t in tasks],
        total=len(tasks),
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """获取单个任务详情"""
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return TaskResponse(**task)


@router.delete("/{task_id}")
async def cancel_or_delete_task(task_id: str, force: bool = False):
    """取消运行中的任务，或强制删除已完成/失败的任务记录"""
    # 先尝试取消（针对运行中的任务）
    if orchestrator.cancel_task(task_id):
        return {"message": "任务已取消", "task_id": task_id}
    # force=true 时删除已完成/失败的任务记录
    if force and orchestrator.delete_task(task_id):
        return {"message": "任务记录已删除", "task_id": task_id}
    raise HTTPException(status_code=400, detail="任务无法操作（可能不存在或正在运行中不可删除）")


@router.post("/{task_id}/retry", response_model=TaskResponse, status_code=201)
async def retry_task(task_id: str):
    """重试失败/已完成的任务（复用原描述创建新任务）"""
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 仅允许重试终态任务
    if task["status"] not in (TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.COMPLETED):
        raise HTTPException(status_code=400, detail="只能重试已完成、失败或取消的任务")

    new_id = str(uuid.uuid4())
    orchestrator.create_task(new_id, task["description"])
    orchestrator.run_task_async(new_id)
    new_task = orchestrator.get_task(new_id)
    return TaskResponse(**new_task)


@router.get("/{task_id}/events")
async def task_events(task_id: str):
    """SSE 实时进度流"""
    task = orchestrator.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return StreamingResponse(
        progress_manager.event_generator(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
