"""数据源管理 —— 采集器实例的注册、参数校验与凭据加密存取。

Collector 是代码里的类型（怎么抓），Source 是用户在界面上注册的实例（抓哪个）。
LLM 只看得见 Source，看不见 Collector，所以它编不出 thread_id 这类平台参数。
"""

import uuid
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.collectors import all_collectors, get_collector
from app.services import crypto, storage

logger = logging.getLogger("hyxi.source")

# 字符串而非整数：param_fields 把 thread_id 声明成 text，界面上改一次也会变成字符串，
# seed 跟着走才不会让同一个字段在库里出现两种类型
DEFAULT_TWEAKERS_THREAD_ID = "2336074"


def list_sources(enabled_only: bool = False) -> List[dict]:
    return storage.load_sources(enabled_only=enabled_only)


def get_source(source_id: str) -> Optional[dict]:
    return storage.get_source(source_id)


def create_source(
    collector_id: str, name: str, params: Dict[str, Any], enabled: bool = True
) -> dict:
    collector = get_collector(collector_id)
    params = _validate_params(collector, params)
    source = {
        "id": "src_" + uuid.uuid4().hex[:8],
        "collector_id": collector_id,
        "name": name.strip() or collector.display_name,
        "params": params,
        "enabled": enabled,
        "last_auth_at": None,
        "created_at": datetime.now().isoformat(),
    }
    storage.save_source(source)
    return source


def update_source(
    source_id: str,
    name: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    enabled: Optional[bool] = None,
) -> Optional[dict]:
    source = storage.get_source(source_id)
    if not source:
        return None
    if name is not None:
        source["name"] = name.strip() or source["name"]
    if params is not None:
        source["params"] = _validate_params(get_collector(source["collector_id"]), params)
    if enabled is not None:
        source["enabled"] = enabled
    storage.save_source(source)
    return source


def delete_source(source_id: str) -> bool:
    return storage.delete_source(source_id)


def mark_authorized(source_id: str) -> None:
    """记录一次成功的登录，驱动界面上的「会话正常 / 需重新授权」徽标"""
    source = storage.get_source(source_id)
    if source:
        source["last_auth_at"] = datetime.now().isoformat()
        storage.save_source(source)


def clear_authorization(source_id: str) -> None:
    """会话失效时把授权时间抹掉，让徽标翻回「需重新授权」。

    与 mark_authorized 对称。少了它，界面会在会话过期后一直显示「会话正常」——
    而采集恰恰正因为会话失效在失败，用户被指向错误的排查方向。
    """
    source = storage.get_source(source_id)
    if source and source.get("last_auth_at"):
        source["last_auth_at"] = None
        storage.save_source(source)


# ===== 人工授权 =====

_authorizing: set = set()


def is_authorizing(source_id: str) -> bool:
    return source_id in _authorizing


def start_authorization(source_id: str, source: dict) -> None:
    """后台开一个有头浏览器让人自己完成登录，成功后落会话并记 last_auth_at。

    只做「把人送到真实登录页」这一件事 —— 验证码和两步验证由人来过，脚本不做任何绕过。
    """
    import asyncio

    from app.collectors import get_collector
    from app.services.collector_runner import CollectorRunner, ManualAuthRequired
    from app.services.progress_manager import progress_manager

    channel = f"auth_{source_id}"
    _authorizing.add(source_id)

    async def _run():
        try:
            await progress_manager.emit(channel, "log", {
                "level": "info",
                "message": "正在后端所在机器上打开浏览器窗口，请在窗口里完成登录…",
            })
            collector = get_collector(source["collector_id"])
            job_source = dict(source)
            job_source["mode"] = "login_only"
            # 人来过一次 Arkose 人机验证、必要时还要去手机上取验证码，5 分钟偏紧
            job_source.setdefault("manual_login_timeout_ms", 10 * 60 * 1000)
            await CollectorRunner.execute(
                channel, collector, job_source, progress_manager, 0
            )
            mark_authorized(source_id)
            await progress_manager.emit(channel, "log", {
                "level": "success", "message": "授权成功，会话已保存",
            })
            await progress_manager.emit(channel, "task_complete", {
                "task_id": channel, "status": "completed",
            })
        except ManualAuthRequired as e:
            await progress_manager.emit(channel, "task_complete", {
                "task_id": channel, "status": "failed", "error": e.reason,
            })
        except Exception as e:
            logger.exception("人工授权失败 source=%s", source_id)
            await progress_manager.emit(channel, "task_complete", {
                "task_id": channel, "status": "failed", "error": str(e),
            })
        finally:
            _authorizing.discard(source_id)

    asyncio.create_task(_run())


# ===== 凭据 =====

def set_credential(source_id: str, username: str, password: str, kind: str = "password") -> None:
    """加密保存凭据。密钥没配就直接报错，不会退化成明文落库"""
    storage.save_credential(source_id, kind, username, crypto.encrypt(password))


def delete_credential(source_id: str) -> bool:
    return storage.delete_credential(source_id)


def credential_info(source_id: str) -> dict:
    """给 API 出口用：只回「有没有」和用户名，密文和明文都不出这一层"""
    cred = storage.get_credential(source_id)
    if not cred:
        return {"has_credential": False, "credential_username": ""}
    return {"has_credential": True, "credential_username": cred.get("username", "")}


def get_credential_secret(source_id: str) -> Optional[str]:
    """解出明文密码。只允许采集器子进程的启动路径调用，绝不能进 API 响应或日志"""
    cred = storage.get_credential(source_id)
    if not cred:
        return None
    return crypto.decrypt(cred["secret_enc"])


# ===== 启动时的幂等 seed =====

def seed_default_sources() -> None:
    """首次启动时补一条 Tweakers 数据源，让升级上来的部署不用手工建就能照常跑。

    只在 sources 表整体为空时执行——用户删掉它就是不想要，不该每次启动又长回来。
    """
    if storage.load_sources():
        return
    source = create_source(
        collector_id="tweakers",
        name="Tweakers.net — HYXi Halo 帖子",
        params={"thread_id": DEFAULT_TWEAKERS_THREAD_ID},
    )
    logger.info("已初始化默认数据源: %s (%s)", source["name"], source["id"])


def _validate_params(collector, params: Dict[str, Any]) -> Dict[str, Any]:
    """按采集器声明的 param_fields 过滤并校验。

    只保留声明过的字段：params 会原样进 job 文件，放任未知键通过等于给了一条
    绕过 Collector 声明往采集脚本塞参数的路。
    """
    params = params or {}
    cleaned = {}
    missing = []
    for field in collector.param_fields:
        key = field["name"]
        value = params.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if field.get("required"):
                missing.append(field.get("label") or key)
            continue
        cleaned[key] = value.strip() if isinstance(value, str) else value
    if missing:
        raise ValueError("缺少必填参数: " + "、".join(missing))
    return cleaned


def collector_catalog() -> List[dict]:
    """可用采集器清单，param_fields 驱动前端表单渲染"""
    return [
        {
            "id": c.id,
            "display_name": c.display_name,
            "needs_credentials": c.needs_credentials,
            "incremental_strategy": c.incremental_strategy,
            "param_fields": c.param_fields,
        }
        for c in all_collectors()
    ]
