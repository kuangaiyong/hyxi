"""应用配置 - 使用 pydantic-settings 管理"""

import os
from pathlib import Path
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用全局配置"""

    # 项目路径
    project_root: str = str(Path(__file__).parent.parent.parent)
    data_dir: str = os.path.join(project_root, "backend", "data")
    tasks_dir: str = os.path.join(data_dir, "tasks")
    exports_dir: str = os.path.join(data_dir, "exports")

    # 服务配置
    # 注意：host/port 目前无任何代码引用（项目不调 uvicorn.run），
    # 实际监听地址取决于启动命令的 --host。此处默认值仅表达「无鉴权应只绑本机」的意图。
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    enable_docs: bool = True  # 对外暴露时设 TWEAKERS_ENABLE_DOCS=false 关闭 /docs
    # /api/v1/* 的共享密钥，留空则不鉴权（见 app/auth.py）
    api_key: str = ""
    # 数据源凭据的加密密钥（Fernet），留空则拒绝保存凭据（见 app/services/crypto.py）
    secret_key: str = ""

    # 任务限制
    max_concurrent_tasks: int = 1
    task_timeout_minutes: int = 30
    sse_keepalive_seconds: int = 15

    # env_file 锚死在项目根：写相对路径时 pydantic-settings 按 cwd 找，而文档里的启动命令
    # 先 cd 进 backend，根目录的 .env 会被整个跳过（密钥漏配却毫无提示）
    model_config = {"env_prefix": "TWEAKERS_", "env_file": os.path.join(project_root, ".env")}


settings = Settings()
