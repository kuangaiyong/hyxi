"""邮件通知：配置、测试邮件、立即发送、运行状态。规格见 docs/features/email-notify.md"""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.models import (
    ConfigTestResult, NotifyConfigInput, NotifyConfigPublic, NotifyPrecheck, NotifySendInput, NotifyStatus,
)
from app.services import notify_service

router = APIRouter(prefix="/api/v1/notify", tags=["邮件通知"])


@router.get("/config", response_model=NotifyConfigPublic)
async def get_config():
    """当前配置。密码只回「有没有」"""
    return notify_service.public_config()


@router.put("/config", response_model=NotifyConfigPublic)
async def save_config(payload: NotifyConfigInput):
    """保存配置（密码加密落库，留空 = 沿用已保存的），并照「每天定时发送」挂上 / 摘掉定时"""
    try:
        return notify_service.save_config(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/config", response_model=NotifyConfigPublic)
async def delete_config():
    """清除配置，连同已保存的密码、每天定时和发送记录。正在发送时回 409"""
    try:
        return notify_service.delete_config()
    except notify_service.NotifyBusy as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/test", response_model=ConfigTestResult)
async def send_test(payload: NotifyConfigInput):
    """用页面上填的 SMTP 设置（不必先保存）发一封测试邮件。填得不对回 400；发不出去回 success=false 和原因"""
    try:
        message = await asyncio.to_thread(notify_service.send_test, payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except notify_service.NotifyError as e:
        return ConfigTestResult(success=False, message=str(e))
    return ConfigTestResult(success=True, message=message)


@router.get("/precheck", response_model=NotifyPrecheck)
async def precheck():
    """点「立即发送」前问一句：所选的（启用的）数据源里哪些今天还没采集翻译过，页面据此弹确认框"""
    if not notify_service.is_configured():
        raise HTTPException(status_code=400, detail="请先填好邮件通知配置并保存")
    return notify_service.precheck()


@router.post("/send", response_model=NotifyStatus, status_code=202)
async def send_now(body: Optional[NotifySendInput] = None):
    """用已保存的配置立即发一轮。在后台跑（补采、补译可能要几分钟），结果看 GET /status。
    collect=True 先把今天还没采集翻译过的数据源采一遍；不带请求体照旧直接发"""
    if not notify_service.is_configured():
        raise HTTPException(status_code=400, detail="请先填好邮件通知配置并保存")
    if not notify_service.start_send(bool(body and body.collect)):
        raise HTTPException(status_code=409, detail="上一轮还在发送，等它结束再试")
    return notify_service.status()


@router.get("/status", response_model=NotifyStatus)
async def get_status():
    """是否正在发送、进行到哪一步、上一次的结果、下次定时发送的时间"""
    return notify_service.status()
