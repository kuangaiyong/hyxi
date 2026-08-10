"""LLM 配置 CRUD 端点"""

from fastapi import APIRouter
from app.models import LLMConfig, LLMConfigPublic, ConfigTestResult
from app.services import storage
from app.services.llm_service import LLMService
from app.services.llm_utils import (
    LLM_CONFIG_PREFIX,
    VISION_CONFIG_PREFIX,
    VISION_DEFAULT_BASE_URL,
    VISION_DEFAULT_MODEL,
)

router = APIRouter(prefix="/api/v1/config", tags=["配置"])


@router.get("", response_model=LLMConfigPublic)
async def get_config():
    """获取当前配置（不返回 API Key）"""
    cfg = storage.get_app_config(LLM_CONFIG_PREFIX)
    if cfg.get("api_key"):
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
    storage.set_app_config(LLM_CONFIG_PREFIX, cfg)
    return LLMConfigPublic(
        base_url=cfg["base_url"],
        model_name=cfg["model_name"],
        is_configured=True,
    )


@router.post("/test", response_model=ConfigTestResult)
async def test_connection(config: LLMConfig):
    """测试 LLM 连接"""
    service = None
    try:
        service = LLMService(config)
        ok = await service.test_connection()
        if ok:
            return ConfigTestResult(success=True, message="连接成功！LLM API 可正常访问。")
        else:
            return ConfigTestResult(success=False, message="连接失败，请检查 API Key 和 Base URL。")
    except Exception as e:
        return ConfigTestResult(success=False, message=f"连接异常: {str(e)}")
    finally:
        if service:
            await service.close()


@router.delete("", response_model=LLMConfigPublic)
async def reset_config():
    """重置配置为默认值"""
    storage.delete_app_config(LLM_CONFIG_PREFIX)
    return LLMConfigPublic(
        base_url="https://api.deepseek.com",
        model_name="deepseek-chat",
        is_configured=False,
    )


# ===== 多模态（图片理解）模型 =====
#
# 与上面 LLM 那组完全同形，只是换了 app_config 的前缀。它是**可选**的：没配置时
# 舆情分析降级为纯文本，不影响任何既有功能。


@router.get("/vision", response_model=LLMConfigPublic)
async def get_vision_config():
    """获取多模态模型配置（不返回 API Key）"""
    cfg = storage.get_app_config(VISION_CONFIG_PREFIX)
    if cfg.get("api_key"):
        return LLMConfigPublic(
            base_url=cfg.get("base_url", ""),
            model_name=cfg.get("model_name", ""),
            is_configured=True,
        )
    return LLMConfigPublic(
        base_url=VISION_DEFAULT_BASE_URL,
        model_name=VISION_DEFAULT_MODEL,
        is_configured=False,
    )


@router.post("/vision", response_model=LLMConfigPublic)
async def save_vision_config(config: LLMConfig):
    """保存多模态模型配置"""
    cfg = config.model_dump()
    storage.set_app_config(VISION_CONFIG_PREFIX, cfg)
    return LLMConfigPublic(
        base_url=cfg["base_url"],
        model_name=cfg["model_name"],
        is_configured=True,
    )


@router.post("/vision/test", response_model=ConfigTestResult)
async def test_vision_connection(config: LLMConfig):
    """测试多模态模型连接。

    注意它只证明「密钥有效、端点可达」：test_connection 先探 /models，那个接口
    **配额用尽时照样返回 200**。所以这里成功不等于图片理解就能跑起来。
    """
    service = None
    try:
        service = LLMService(config)
        ok = await service.test_connection()
        if ok:
            return ConfigTestResult(
                success=True,
                message="连接成功！注意：这只验证了密钥与地址，不代表该模型支持图片输入或额度充足。",
            )
        return ConfigTestResult(success=False, message="连接失败，请检查 API Key 和 Base URL。")
    except Exception as e:
        return ConfigTestResult(success=False, message=f"连接异常: {str(e)}")
    finally:
        if service:
            await service.close()


@router.delete("/vision", response_model=LLMConfigPublic)
async def reset_vision_config():
    """清除多模态模型配置（清除后舆情分析回到纯文本）"""
    storage.delete_app_config(VISION_CONFIG_PREFIX)
    return LLMConfigPublic(
        base_url=VISION_DEFAULT_BASE_URL,
        model_name=VISION_DEFAULT_MODEL,
        is_configured=False,
    )
