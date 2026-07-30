"""FastAPI 应用入口"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers import config as config_router
from app.routers import tasks as tasks_router
from app.routers import results as results_router
from app.routers import schedules as schedules_router
from app.services.scheduler_service import scheduler_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.tasks_dir, exist_ok=True)
    os.makedirs(settings.exports_dir, exist_ok=True)
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
    version="1.0.4",
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

app.include_router(config_router.router)
app.include_router(tasks_router.router)
app.include_router(results_router.router)
app.include_router(schedules_router.router)


@app.get("/")
async def root():
    return {
        "service": "HYXi 舆情分析 API",
        "version": "1.0.4",
        "docs": "/docs",
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}
