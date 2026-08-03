"""数据源与凭据管理端点"""

from typing import List

from fastapi import APIRouter, HTTPException

from app.collectors import get_collector
from app.models import (
    CollectorInfo,
    CredentialInput,
    SourceCreate,
    SourcePublic,
    SourceUpdate,
)
from app.services import source_service

router = APIRouter(prefix="/api/v1", tags=["数据源"])


def _to_public(source: dict) -> SourcePublic:
    collector = get_collector(source["collector_id"])
    return SourcePublic(
        id=source["id"],
        collector_id=source["collector_id"],
        collector_name=collector.display_name,
        name=source["name"],
        params=source.get("params", {}),
        enabled=source.get("enabled", True),
        needs_credentials=collector.needs_credentials,
        last_auth_at=source.get("last_auth_at"),
        created_at=source.get("created_at"),
        **source_service.credential_info(source["id"]),
    )


@router.get("/collectors", response_model=List[CollectorInfo])
async def list_collectors():
    """可用采集器清单"""
    return source_service.collector_catalog()


@router.get("/sources", response_model=List[SourcePublic])
async def list_sources():
    return [_to_public(s) for s in source_service.list_sources()]


@router.post("/sources", response_model=SourcePublic, status_code=201)
async def create_source(payload: SourceCreate):
    try:
        source = source_service.create_source(
            payload.collector_id, payload.name, payload.params, payload.enabled
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _to_public(source)


@router.get("/sources/{source_id}", response_model=SourcePublic)
async def get_source(source_id: str):
    source = source_service.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return _to_public(source)


@router.patch("/sources/{source_id}", response_model=SourcePublic)
async def update_source(source_id: str, payload: SourceUpdate):
    try:
        source = source_service.update_source(
            source_id, payload.name, payload.params, payload.enabled
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return _to_public(source)


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str):
    if not source_service.delete_source(source_id):
        raise HTTPException(status_code=404, detail="数据源不存在")
    return {"message": "已删除"}


@router.put("/sources/{source_id}/credential", response_model=SourcePublic)
async def set_credential(source_id: str, payload: CredentialInput):
    """录入凭据。密码经 Fernet 加密后落库，任何端点都不会把它读回去"""
    source = source_service.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    try:
        source_service.set_credential(source_id, payload.username, payload.password)
    except ValueError as e:
        # 没配 TWEAKERS_SECRET_KEY 时走这里，报文里直接给生成密钥的命令
        raise HTTPException(status_code=400, detail=str(e))
    return _to_public(source)


@router.delete("/sources/{source_id}/credential", response_model=SourcePublic)
async def delete_credential(source_id: str):
    source = source_service.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    source_service.delete_credential(source_id)
    return _to_public(source)
