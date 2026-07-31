"""API 共享密钥认证"""

import hmac
from typing import Optional
from fastapi import Header, Query, HTTPException
from app.config import settings


def require_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None, description="仅供浏览器 EventSource 使用"),
) -> None:
    """校验共享密钥。

    未设置 TWEAKERS_API_KEY 时放行：升级到本版本的既有部署不该因为漏配环境变量就
    整个服务不可用，改由启动时的告警提示（见 main.py 的 lifespan）。
    """
    expected = settings.api_key
    if not expected:
        return

    # 浏览器的 EventSource 无法自定义请求头，SSE 端点只能把密钥放在 query 上
    provided = x_api_key or api_key
    # 必须转 bytes 再比：compare_digest 对含非 ASCII 的 str 直接抛 TypeError，
    # 用户把密钥设成中文时每个请求都会变成 500 而不是 401
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="API Key 无效或缺失")
