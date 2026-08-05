"""LLM 工具函数 — 统一加载配置和创建 LLMService 实例"""

import logging
from typing import Optional
from app.services import storage
from app.services.llm_service import LLMService
from app.models import LLMConfig

logger = logging.getLogger("hyxi.llm_utils")

# app_config 表里的键前缀。配置项按 llm.api_key / llm.base_url / llm.model_name 分列存
LLM_CONFIG_PREFIX = "llm"


def load_llm_config() -> Optional[LLMConfig]:
    """从 app_config 表加载 LLM 配置"""
    cfg_data = storage.get_app_config(LLM_CONFIG_PREFIX)
    if not cfg_data.get("api_key"):
        logger.warning("LLM 配置尚未录入")
        return None
    try:
        return LLMConfig(**cfg_data)
    except Exception as e:
        logger.error("加载 LLM 配置失败: %s", str(e))
        return None


def get_llm_service() -> Optional[LLMService]:
    """创建 LLMService 实例（调用方负责在完成后调用 close()）"""
    config = load_llm_config()
    if config is None:
        return None
    return LLMService(config)
