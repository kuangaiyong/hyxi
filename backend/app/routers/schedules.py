"""定时任务管理端点"""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.services.scheduler_service import scheduler_service, PRESETS

router = APIRouter(prefix="/api/v1/schedules", tags=["定时任务"])


class ScheduleCreate(BaseModel):
    description: str = Field(..., description="任务描述", min_length=1)
    interval: str = Field(..., description="调度间隔: hourly/6h/12h/daily")
    time: str = Field("09:00", description="daily 模式的时间，如 09:00")


class ScheduleUpdate(BaseModel):
    description: Optional[str] = None
    interval: Optional[str] = None
    time: Optional[str] = None


@router.get("/presets")
async def get_presets():
    """获取可用的调度预设"""
    return {"presets": PRESETS}


@router.get("")
async def list_schedules():
    """获取所有定时任务"""
    return {"schedules": scheduler_service.get_all()}


@router.post("", status_code=201)
async def create_schedule(body: ScheduleCreate):
    """创建定时任务"""
    if body.interval not in PRESETS:
        raise HTTPException(status_code=400, detail=f"无效的调度间隔: {body.interval}")
    return scheduler_service.create(body.description, body.interval, body.time)


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str):
    cfg = scheduler_service.get_one(schedule_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return cfg


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, body: ScheduleUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    result = scheduler_service.update(schedule_id, updates)
    if not result:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return result


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: str):
    ok = scheduler_service.delete(schedule_id)
    if not ok:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"message": "已删除"}


@router.post("/{schedule_id}/toggle")
async def toggle_schedule(schedule_id: str):
    result = scheduler_service.toggle(schedule_id)
    if not result:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return result


@router.post("/{schedule_id}/run")
async def run_schedule_now(schedule_id: str):
    """手动触发一次"""
    try:
        task_id = await scheduler_service.run_now(schedule_id)
        return {"message": "已触发执行", "task_id": task_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
