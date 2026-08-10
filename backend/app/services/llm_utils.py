"""LLM 工具函数 — 统一加载配置和创建 LLMService 实例"""

import logging
from typing import Optional
from app.services import storage
from app.services.llm_service import LLMService
from app.models import LLMConfig

logger = logging.getLogger("hyxi.llm_utils")

# app_config 表里的键前缀。配置项按 llm.api_key / llm.base_url / llm.model_name 分列存
LLM_CONFIG_PREFIX = "llm"

# 多模态（图片理解）模型的配置前缀。get_app_config 本来就是按前缀分组的通用实现，
# 所以多这一组配置**不需要动任何表结构**
VISION_CONFIG_PREFIX = "vision"

VISION_DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
VISION_DEFAULT_MODEL = "kimi-for-coding"


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


def load_vision_config() -> Optional[LLMConfig]:
    """从 app_config 表加载多模态模型配置。

    没配就返回 None —— 舆情分析据此降级成纯文本，**不报错**。图片理解是增强，
    不是前置条件；为了一张图让整轮分析失败是不可接受的。
    """
    cfg_data = storage.get_app_config(VISION_CONFIG_PREFIX)
    if not cfg_data.get("api_key"):
        return None
    try:
        return LLMConfig(**cfg_data)
    except Exception as e:
        logger.error("加载多模态模型配置失败: %s", str(e))
        return None


def get_vision_service() -> Optional[LLMService]:
    """创建用于图片理解的 LLMService（调用方负责 close()）。

    复用 LLMService 而不是新写一个客户端：它的 chat() 直接透传 messages，
    OpenAI 兼容的 content parts 天然可用，还自带指数退避与错误信息脱敏。
    """
    config = load_vision_config()
    if config is None:
        return None
    return LLMService(config)
