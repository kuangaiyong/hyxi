"""LLM 配置 CRUD 端点"""

import json
import os
from typing import Optional
from fastapi import APIRouter, HTTPException
from app.models import LLMConfig, LLMConfigPublic, ConfigTestResult
from app.config import settings
from app.services.llm_service import LLMService

router = APIRouter(prefix="/api/v1/config", tags=["配置"])


def _load_config() -> Optional[dict]:
    """从文件加载配置"""
    if os.path.exists(settings.config_file):
        with open(settings.config_file, "r") as f:
            return json.load(f)
    return None


def _save_config(config: dict) -> None:
    """保存配置到文件"""
    os.makedirs(os.path.dirname(settings.config_file), exist_ok=True)
    with open(settings.config_file, "w") as f:
        json.dump(config, f, indent=2)


@router.get("", response_model=LLMConfigPublic)
async def get_config():
    """获取当前配置（不返回 API Key）"""
    cfg = _load_config()
    if cfg:
        return LLMConfigPublic(
            base_url=cfg.get("base_url", ""),
            model_name=cfg.get("model_name", ""),
            is_configured=True,
        )
    return LLMConfigPublic(
        base_url="https://api.deepseek.com",
        model_name="deepseek-chat",
        is_configured=False,
    )


@router.post("", response_model=LLMConfigPublic)
async def save_config(config: LLMConfig):
    """保存 LLM 配置"""
    cfg = config.model_dump()
    _save_config(cfg)
    return LLMConfigPublic(
        base_url=cfg["base_url"],
        model_name=cfg["model_name"],
        is_configured=True,
    )


@router.post("/test", response_model=ConfigTestResult)
async def test_connection(config: LLMConfig):
    """测试 LLM 连接"""
    try:
        service = LLMService(config)
        ok = await service.test_connection()
        if ok:
            return ConfigTestResult(success=True, message="连接成功！LLM API 可正常访问。")
        else:
            return ConfigTestResult(success=False, message="连接失败，请检查 API Key 和 Base URL。")
    except Exception as e:
        return ConfigTestResult(success=False, message=f"连接异常: {str(e)}")


@router.delete("", response_model=LLMConfigPublic)
async def reset_config():
    """重置配置为默认值"""
    if os.path.exists(settings.config_file):
        os.remove(settings.config_file)
    return LLMConfigPublic(
        base_url="https://api.deepseek.com",
        model_name="deepseek-chat",
        is_configured=False,
    )
