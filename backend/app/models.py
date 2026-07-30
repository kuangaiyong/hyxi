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
