"""FastAPI 应用入口"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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
    version="1.9.0",
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


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/version")
async def version():
    return {
        "service": "HYXi 舆情分析 API",
        "version": "1.9.0",
        "docs": "/docs",
    }


def mount_frontend(target_app: FastAPI, web_dir: str) -> bool:
    """把前端构建产物挂到同一个应用上。挂上了返回 True。

    便携包里没有 nginx，也不该有第二个要配置的服务：单端口同时供 `/api/*` 和页面。
    `web/` 不存在就是源码开发态（前端跑在 Vite 5173 上，由它代理 /api），整段跳过。

    **必须在所有路由注册之后调用** —— 下面那条 catch-all 谁都接得住。
    """
    index_html = os.path.join(web_dir, "index.html")
    if not os.path.isfile(index_html):
        return False

    assets = os.path.join(web_dir, "assets")
    if os.path.isdir(assets):
        target_app.mount("/assets", StaticFiles(directory=assets), name="assets")

    web_root = os.path.realpath(web_dir)

    @target_app.get("/{full_path:path}")
    async def spa(full_path: str):
        """SPA 回退。

        路由是 `createWebHistory()`，`/tasks/{id}/progress` 这类深链**直接刷新**时
        浏览器会向后端要这个路径，没有回退就是 404，用户看到的是白屏。

        **`api/` 开头的必须排除掉**：这条 catch-all 只接没人认领的路径，而打错的接口
        地址也在其中 —— 回一张 HTML 页面会让排查彻底走偏（调用方拿到 200 +
        `<!doctype html>`，JSON 解析失败，报的错跟真实原因毫无关系）。
        """
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="不存在")
        # favicon 之类根目录下的真实文件直接给，别一律回 index.html。
        # 包含性校验同 get_media：web 目录之外就是数据库和明文密钥
        candidate = os.path.realpath(os.path.join(web_dir, full_path))
        if full_path and candidate.startswith(web_root + os.sep) and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(index_html)

    return True


if not mount_frontend(app, os.path.join(settings.project_root, "web")):
    @app.get("/")
    async def root():
        return {
            "service": "HYXi 舆情分析 API",
            "version": "1.9.0",
            "docs": "/docs",
        }
