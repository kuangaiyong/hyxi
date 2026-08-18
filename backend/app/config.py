"""应用配置 - 使用 pydantic-settings 管理"""

import os
from typing import List
from pydantic_settings import BaseSettings
from app.paths import data_dir as _resolve_data_dir, project_root as _resolve_project_root

# 冻结后 __file__ 不再指向源码树，这两条路径错了会连锁带偏数据、密钥、采集脚本和
# playwright 四个位置。算法只有 app/paths.py 那一份，别在这里另写
_ROOT = _resolve_project_root()
_DATA = _resolve_data_dir()


class Settings(BaseSettings):
    """应用全局配置"""

    # 项目路径
    project_root: str = _ROOT
    data_dir: str = _DATA
    tasks_dir: str = os.path.join(_DATA, "tasks")
    exports_dir: str = os.path.join(_DATA, "exports")

    # 服务配置
    # 源码态用命令行 `uvicorn --host` 起，这两项不生效；打包态由 run_server.py
    # 传给 uvicorn.run()，那时它们才是真配置。默认值表达「无鉴权就只绑本机」的意图。
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

    # 采集子进程用的 node 可执行文件，留空则按 resolve_node_executable() 自动找
    node_path: str = ""

    # env_file 锚死在项目根：写相对路径时 pydantic-settings 按 cwd 找，而文档里的启动命令
    # 先 cd 进 backend，根目录的 .env 会被整个跳过（密钥漏配却毫无提示）
    model_config = {"env_prefix": "TWEAKERS_", "env_file": os.path.join(_ROOT, ".env")}


settings = Settings()


def resolve_node_executable() -> str:
    """采集子进程该用哪个 node。

    便携包的目标机器上不会装 Node，也不会有 PATH 上的 `node` —— 包内自带一个，
    优先用它。源码态包内没有这个目录，回退到 PATH，开发流程不受影响。
    """
    if settings.node_path:
        return settings.node_path
    bundled = os.path.join(settings.project_root, "node", "node.exe")
    if os.path.exists(bundled):
        return bundled
    return "node"
