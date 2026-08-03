"""Pydantic 数据模型"""

from __future__ import annotations
from datetime import datetime
from typing import Optional, List, Dict
from enum import Enum
from pydantic import BaseModel, Field


# ===== LLM 配置 =====

class LLMConfig(BaseModel):
    """LLM 配置模型"""
    api_key: str = Field(..., description="API密钥", min_length=1)
    base_url: str = Field("https://api.deepseek.com", description="API基础URL")
    model_name: str = Field("deepseek-chat", description="模型名称")

    class Config:
        json_schema_extra = {
            "example": {
                "api_key": "sk-xxxxxxxxxxxxx",
                "base_url": "https://api.deepseek.com",
                "model_name": "deepseek-v4-pro"
            }
        }


class LLMConfigPublic(BaseModel):
    """公开的LLM配置（不含api_key）"""
    base_url: str
    model_name: str
    is_configured: bool = False


class ConfigTestResult(BaseModel):
    """连接测试结果"""
    success: bool
    message: str


# ===== 数据源 =====

class SourceCreate(BaseModel):
    """注册一个数据源"""
    collector_id: str = Field(..., description="采集器 ID", min_length=1)
    name: str = Field("", description="显示名称", max_length=100)
    params: Dict = Field(default_factory=dict, description="采集参数，字段由采集器声明")
    enabled: bool = True


class SourceUpdate(BaseModel):
    """更新数据源，只改传上来的字段"""
    name: Optional[str] = Field(None, max_length=100)
    params: Optional[Dict] = None
    enabled: Optional[bool] = None


class SourcePublic(BaseModel):
    """数据源出口模型。凭据只回「有没有」和用户名，密码与密文都不出后端"""
    id: str
    collector_id: str
    collector_name: str
    name: str
    params: Dict = Field(default_factory=dict)
    enabled: bool
    needs_credentials: bool
    has_credential: bool
    credential_username: str = ""
    last_auth_at: Optional[str] = None
    created_at: Optional[str] = None


class CredentialInput(BaseModel):
    """录入凭据。只进不出——没有任何端点会把它读回去"""
    username: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=500)


class CollectorInfo(BaseModel):
    """可用采集器，param_fields 驱动前端表单渲染"""
    id: str
    display_name: str
    needs_credentials: bool
    incremental_strategy: str
    param_fields: List[Dict] = Field(default_factory=list)


# ===== 任务相关 =====

class TaskStatus(str, Enum):
    PENDING = "pending"
    PARSING = "parsing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStep(BaseModel):
    """LLM解析出的执行步骤"""
    action: str = Field(..., description="动作: scrape/translate/generate_excel")
    params: dict = Field(default_factory=dict, description="步骤参数")
    status: str = "pending"  # pending / running / completed / failed
    error: Optional[str] = None


class TaskCreate(BaseModel):
    """创建任务请求"""
    # 描述会随每条日志、每个步骤被 _persist() 全量 upsert 一遍，无上限时写放大随文本二次增长
    description: str = Field(..., description="自然语言任务描述", min_length=1, max_length=2000)

    class Config:
        json_schema_extra = {
            "example": {
                "description": "抓取帖子2336074，翻译成中文，导出Excel"
            }
        }


class TaskResponse(BaseModel):
    """任务响应"""
    id: str
    status: TaskStatus
    description: str
    plan: List[PlanStep] = Field(default_factory=list)
    logs: List[dict] = Field(default_factory=list)
    progress: float = 0.0
    current_step: Optional[str] = None
    result: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TaskListResponse(BaseModel):
    """任务列表响应"""
    tasks: List[TaskResponse]
    total: int


# ===== 帖子/结果相关 =====

class PostData(BaseModel):
    """单条帖子数据"""
    index: int
    username: str
    timestamp: str
    content: str
    translation: str = ""
    page_number: int


class PostsResponse(BaseModel):
    """帖子列表响应"""
    posts: List[PostData]
    total: int
    page: int
    page_size: int


class TaskStats(BaseModel):
    """任务统计信息"""
    total_posts: int
    unique_users: int
    total_pages: int
    time_range_start: Optional[str] = None
    time_range_end: Optional[str] = None
    top_users: List[dict] = Field(default_factory=list)


# ===== SSE 事件 =====

class SSEEvent(BaseModel):
    """SSE 事件"""
    event: str  # step_start / step_progress / step_complete / log / error / task_complete
    data: dict


class LogEvent(BaseModel):
    """日志事件"""
    level: str = "info"  # info / success / warning / error
    message: str
