"""FastAPI 应用入口"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from app.auth import require_api_key
from app.config import settings
from app.logging_config import get_logger
from app.routers import config as config_router
from app.routers import tasks as tasks_router
from app.routers import results as results_router
from app.routers import schedules as schedules_router
from app.routers import sources as sources_router
from app.services.scheduler_service import scheduler_service
from app.services.source_service import seed_default_sources


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.tasks_dir, exist_ok=True)
    os.makedirs(settings.exports_dir, exist_ok=True)
    if not settings.api_key:
        # 用全局 logger 而非 hyxi.* 命名空间：后者拿不到 file handler，
        # 服务在后台运行时这条安全告警会随控制台输出一起丢掉
        get_logger().warning(
            "未设置 TWEAKERS_API_KEY，/api/v1/* 处于无鉴权状态；"
            "任何能访问本端口的人都可覆写 LLM 密钥、导出全部数据，请勿绑定 0.0.0.0"
        )
    # 与下面的调度器同理：补默认数据源是便利功能，磁盘满或库被占用时不该让整个服务起不来
    try:
        seed_default_sources()
    except Exception:
        logging.getLogger("hyxi.source").exception("默认数据源初始化失败，服务继续运行")
    # 启动定时任务调度器：调度是附属能力，起不来也不该让整个 API 服务不可用
    try:
        scheduler_service.start()
    except Exception:
        logging.getLogger("hyxi.scheduler").exception("定时任务调度器启动失败，服务继续运行")
    yield
    # 关闭调度器
    scheduler_service.shutdown()


app = FastAPI(
    title="HYXi 舆情分析 API",
    description="HYXi 舆情分析平台 — 论坛帖子抓取、翻译与情感分析服务",
    version="1.3.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """422 报文里剔掉 input —— FastAPI 默认会把提交的原始值原样回显，
    密码、LLM api_key 这类只进不出的字段会因为一次长度校验失败就整个吐回去"""
    errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": errors}))


_protected = [Depends(require_api_key)]
app.include_router(config_router.router, dependencies=_protected)
app.include_router(tasks_router.router, dependencies=_protected)
app.include_router(results_router.router, dependencies=_protected)
app.include_router(schedules_router.router, dependencies=_protected)
app.include_router(sources_router.router, dependencies=_protected)


@app.get("/api/v1/media/{rel_path:path}", dependencies=_protected)
async def get_media(rel_path: str):
    """回读采集时下载的正文图。

    不用 StaticFiles 挂载：`<img>` 没法自定义请求头，而 require_api_key 已经支持
    `?api_key=` 查询参数（当初为 SSE 开的口子），所以走普通端点复用同一套鉴权。

    **路径必须校验**：`../../config.json` 里是明文 LLM API Key，realpath 解析后
    不在 media 目录内一律 404 —— 不回 403，免得把目录结构也告诉对方。
    """
    # 裸的 ../ 通常在客户端就被规范化掉了，但 %2e%2e%2f 会被框架解码后原样送到这里 ——
    # 实测确认过，所以这段校验不是摆设
    root = os.path.realpath(os.path.join(settings.data_dir, "media"))
    target = os.path.realpath(os.path.join(root, rel_path))
    if target != root and not target.startswith(root + os.sep):
        raise HTTPException(status_code=404, detail="不存在")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="不存在")
    return FileResponse(target)


@app.get("/")
async def root():
    return {
        "service": "HYXi 舆情分析 API",
        "version": "1.3.0",
        "docs": "/docs",
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}
