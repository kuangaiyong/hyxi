"""LLM 工具函数 — 统一加载配置和创建 LLMService 实例"""

import json
import os
import logging
from typing import Optional
from app.config import settings
from app.services.llm_service import LLMService
from app.models import LLMConfig

logger = logging.getLogger("hyxi.llm_utils")


def load_llm_config() -> Optional[LLMConfig]:
    """从 config.json 加载 LLM 配置"""
    config_path = settings.config_file
    if not os.path.exists(config_path):
        logger.warning("LLM 配置文件不存在: %s", config_path)
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg_data = json.load(f)
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
