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
    config_file: str = os.path.join(data_dir, "config.json")
    tasks_dir: str = os.path.join(data_dir, "tasks")
    exports_dir: str = os.path.join(data_dir, "exports")

    # 服务配置
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # 任务限制
    max_concurrent_tasks: int = 1
    task_timeout_minutes: int = 30
    sse_keepalive_seconds: int = 15

    model_config = {"env_prefix": "TWEAKERS_", "env_file": ".env"}


settings = Settings()
