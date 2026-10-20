"""邮件通知 —— 把所选数据源近 N 天的新回复（原文 + 译文）发到收件人邮箱。

「新回复」和结果页「只看新回复」是同一个判据（post_tree.mark_fresh_replies），只是基准换成当前时间：
结果页要的是报告隔多久打开都自洽，邮件要的是「这段时间里有什么新的」。
发送前先看所选数据源今天有没有成功采集翻译过，没有就自动发起「采集 + 翻译」任务，跑完再挑新回复
（每天定时一律如此；手动发送由页面的确认框决定）。

配置存 app_config：`notify.settings`（JSON）+ `notify.password_enc`（SMTP 密码的密文。同数据源凭据：
加密落库、只进不出）。最近一次发送的结果存 `notify_status.last`，页面靠它看到定时发送发没发成。
规格与证据见 docs/features/email-notify.md
"""

import asyncio
import base64
import html
import ipaddress
import json
import math
import re
import smtplib
import ssl
import time
import uuid
from datetime import datetime, timedelta
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger

from app.collectors import get_collector
from app.collectors.base import Collector
from app.config import settings
from app.logging_config import get_logger
from app.models import TaskStatus
from app.services import crypto, source_service, storage
from app.services.orchestrator import _BackfillProgress, _refresh_translations, orchestrator
from app.services.post_tree import (
    flat_thread_starts, mark_fresh_replies, normalize_timestamp, parent_key, parse_time,
    post_key, sort_time, thread_of,
)
from app.services.translator_service import TranslatorService, needs_translation

logger = get_logger(__name__)

CONFIG_PREFIX = "notify"
# **不能挂在 notify. 下面**：set_app_config 按前缀整组覆盖，保存一次配置就会把发送记录一起删掉
STATUS_PREFIX = "notify_status"
# 补发起点：{来源: 起点}（这个来源第一次进邮件那一轮的窗口起点，之后不动）。单独一个前缀，写发送记录时不会把它冲掉
MARKS_PREFIX = "notify_marks"
# 每天定时发送在 APScheduler 里的 job id。配置只有一份，job 也只该有一个
JOB_ID = "notify-daily"

# 新回复时间窗口（天），封闭集合：这个数字会进邮件标题（理由同 post_tree.FRESH_DAYS_CHOICES）
DAYS_CHOICES = (1, 3, 7, 14)
# 不设置 = 当前时间往前一天，即近 24 小时、不按自然日（用户定的，2026-10-20 再次明确）
DAYS_DEFAULT = 1
# 近这么多小时发表的帖子在邮件里垫一层浅底色，不管窗口选几天（用户定的）
RECENT_HOURS = 24
# 新回复超过这么多条：正文只列最新的这么多条，完整内容放进 HTML 附件（用户定的）
BODY_LIMIT = 20
# 补译失败后整轮再翻几次（用户定的）；仍有失败就不发
TRANSLATE_RETRIES = 3
# 主贴只作上下文，太长就截断（同结果页卡片的折叠长度）。新回复本身一律全文
ROOT_CLIP = 600
# 占翻译来源时用的占用者名（见 orchestrator.claim_translation）
CLAIM_OWNER = "notify"
# 同一来源正被别人翻译时最多等多久（任务超时的默认值也是 30 分钟）
CLAIM_WAIT_SECONDS = 30 * 60
SMTP_TIMEOUT = 30
# 发送前自动发起的「采集 + 翻译」任务打的 scheduled_by 标记：任务列表里认得出是谁发起的
AUTO_TASK_OWNER = "notify"
# 等任务时多久看一次它的状态
POLL_SECONDS = 0.5
# 每天定时发送先让几秒，等同一时刻触发的定时采集把任务建出来（见 scheduled_send）
SCHEDULE_GRACE_SECONDS = 2
_UNFINISHED = (TaskStatus.PENDING, TaskStatus.PARSING, TaskStatus.RUNNING)

_DEFAULTS = {
    "recipients": [], "source_ids": [], "days": None,
    "schedule_enabled": False, "schedule_time": "09:00",
    "smtp_host": "", "smtp_port": 25, "smtp_ssl_port": 465, "use_ssl": True,
    "sender": "", "username": "",
}

_EMAIL = re.compile(r'^[^@\s,;<>"]+@[^@\s,;<>"]+\.[^@\s,;<>"]+$')
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 同一时刻只跑一轮。用普通字典不用 asyncio.Lock，理由同 orchestrator._translation_claims
_state: Dict[str, Optional[str]] = {"running": None, "progress": "", "task_id": None}
# asyncio 只弱引用任务，「立即发送」的后台作业要留强引用（同 orchestrator._backfill_jobs）
_jobs: set = set()


class NotifyError(Exception):
    """发信失败。消息是给用户看的、能照着改的人话"""


class NotifyBusy(Exception):
    """有一轮正在发送，这个操作现在不能做"""


# ===== 配置 =====

def load_config() -> dict:
    """已保存的配置，含 password_enc（只在本模块里用，绝不出接口）。没保存过就是默认值"""
    raw = storage.get_app_config(CONFIG_PREFIX)
    cfg = dict(_DEFAULTS)
    if raw.get("settings"):
        cfg.update(json.loads(raw["settings"]))
    cfg["password_enc"] = raw.get("password_enc", "")
    cfg["saved"] = bool(raw.get("settings"))
    return cfg


def public_config() -> dict:
    cfg = load_config()
    out = {k: cfg.get(k, v) for k, v in _DEFAULTS.items()}
    out["has_password"] = bool(cfg["password_enc"])
    out["is_configured"] = cfg["saved"]
    return out


def is_configured() -> bool:
    return load_config()["saved"]


def _valid_email(addr: str) -> bool:
    """正则之外还要过发信时那一关：email.headerregistry.Address 不认的地址（带 [ ] ( ) : 之类），
    保存时就得拒 —— 否则存得下，每次发送都在组邮件时失败"""
    if not _EMAIL.match(addr):
        return False
    try:
        return Address(addr_spec=addr).addr_spec == addr
    except (ValueError, HeaderParseError):
        return False


def _clean(payload: dict, saved: dict, require_sources: bool) -> dict:
    """校验提交上来的配置并补上密码，返回能直接存、直接发信的 dict（含 password_enc）。不合法抛 ValueError"""
    cfg = {k: payload.get(k, v) for k, v in _DEFAULTS.items()}

    recipients: List[str] = []
    for raw in cfg["recipients"] or []:
        addr = (raw or "").strip()
        if not addr:
            continue
        if not _valid_email(addr):
            raise ValueError(f"收件人邮箱格式不对：{addr}")
        if addr.lower() not in {r.lower() for r in recipients}:
            recipients.append(addr)
    if not recipients:
        raise ValueError("至少填一个收件人邮箱")
    cfg["recipients"] = recipients

    source_ids = list(dict.fromkeys(s for s in cfg["source_ids"] or [] if s))
    known = {s["id"] for s in source_service.list_sources()}
    unknown = [s for s in source_ids if s not in known]
    if unknown:
        raise ValueError(f"数据源不存在（可能已被删除）：{'、'.join(unknown)}")
    if require_sources and not source_ids:
        raise ValueError("至少选一个数据源")
    cfg["source_ids"] = source_ids

    if cfg["days"] is not None and cfg["days"] not in DAYS_CHOICES:
        raise ValueError("新回复时间只能选：不设置（近 1 天）、近 3 天、近 7 天、近 14 天")
    if not _HHMM.match(cfg["schedule_time"] or ""):
        raise ValueError("定时发送的时间要写成 HH:MM（24 小时制），例如 09:00")

    cfg["smtp_host"] = (cfg["smtp_host"] or "").strip()
    if not cfg["smtp_host"]:
        raise ValueError("请填写 SMTP 服务器")
    cfg["sender"] = (cfg["sender"] or "").strip()
    if not _valid_email(cfg["sender"]):
        raise ValueError("发件人邮箱格式不对")
    cfg["username"] = (cfg["username"] or "").strip()
    if not cfg["username"]:
        raise ValueError("请填写 SMTP 用户名（一般是完整的邮箱地址）")

    password = payload.get("password") or ""
    if password:
        cfg["password_enc"] = crypto.encrypt(password)   # 没配 TWEAKERS_SECRET_KEY 时抛 ValueError，不降级成明文
    elif not saved.get("password_enc"):
        raise ValueError("请填写 SMTP 密码（不少邮箱要填「授权码」，不是登录密码）")
    elif (saved.get("smtp_host"), saved.get("username")) != (cfg["smtp_host"], cfg["username"]):
        # 留空沿用旧密码只限同一台服务器、同一个账号：换了服务器还沿用，等于把已保存的密码交给那台新服务器 ——
        # 能调接口的人就能借这个口子，把「只进不出」的密码从自己搭的 SMTP 服务器上套出来
        raise ValueError("SMTP 服务器或用户名改了，请重新填写密码 —— 已保存的密码不会带到新的服务器或账号上")
    else:
        cfg["password_enc"] = saved["password_enc"]
    return cfg


def save_config(payload: dict) -> dict:
    cfg = _clean(payload, load_config(), require_sources=True)
    password_enc = cfg.pop("password_enc")
    storage.set_app_config(CONFIG_PREFIX, {
        "settings": json.dumps(cfg, ensure_ascii=False),
        "password_enc": password_enc,
    })
    _forget_anchors(cfg["source_ids"])
    sync_schedule()
    return public_config()


def delete_config() -> dict:
    """连同发送记录、补发起点和「发过」标记一起清：旧配置的记录留着只会误导（「已发送给 2 个收件人」说的是已经删掉的那份）。

    正在发送时不许清：那一轮手里拿着配置的快照（连同密码），清了照样把信发出去，结束时还会把发送记录写回来
    """
    if _state["running"]:
        raise NotifyBusy("正在发送，等这一轮结束再清除 —— 现在清，它照样会用这份配置把信发出去")
    storage.delete_app_config(CONFIG_PREFIX)
    storage.delete_app_config(STATUS_PREFIX)
    storage.delete_app_config(MARKS_PREFIX)
    # 「发过」也一起忘掉：换一批收件人重新配时，他们一次都没收到过（E2E 在开发库上跑完也靠这一步不留痕）
    storage.clear_notified()
    sync_schedule()
    return public_config()


# ===== 每天定时 =====

def _global_scheduler():
    # 函数内 import：scheduler_service.start() 要回头调本模块恢复 job，两边都在顶部 import 就是循环引用
    from app.services.scheduler_service import scheduler_service
    return scheduler_service.scheduler


def _remove_job(scheduler) -> None:
    """摘掉每天定时的 job。调度器没启动时 remove_job 一次只摘一条排队中的，所以摘到没有为止"""
    while True:
        try:
            scheduler.remove_job(JOB_ID)
        except JobLookupError:
            return


def sync_schedule(scheduler=None) -> None:
    """让「每天定时发送」的 job 和已保存的配置一致：开着就按时间挂上，关着（或没配置）就摘掉。

    job 本身也存在 apscheduler_jobs 里，但以配置为准：保存配置时、调度器启动时都照配置重挂一遍
    """
    if scheduler is None:
        scheduler = _global_scheduler()
    cfg = load_config()
    _remove_job(scheduler)
    if cfg["saved"] and cfg["schedule_enabled"]:
        hour, minute = map(int, cfg["schedule_time"].split(":"))
        scheduler.add_job(scheduled_send, trigger=CronTrigger(hour=hour, minute=minute),
                          id=JOB_ID, replace_existing=True)


def _next_run(scheduler) -> Optional[str]:
    job = scheduler.get_job(JOB_ID)
    try:
        return job.next_run_time.isoformat() if job and job.next_run_time else None
    except AttributeError:
        return None   # 调度器还没启动时，排队中的 job 还没有 next_run_time


# ===== 发送入口 =====

async def scheduled_send() -> None:
    """APScheduler 回调。**必须是模块级函数**：job 存在库里，重启后靠「模块:函数名」找回来"""
    # 定时采集设在同一时刻时，两个回调同时触发、谁先跑说不准：先让一让，等那边把任务建出来，
    # 下面「先等正在运行的任务」才看得见它 —— 不然同一个来源会被采两遍，真站的请求量翻倍
    await asyncio.sleep(SCHEDULE_GRACE_SECONDS)
    if not _begin():
        logger.warning("上一轮邮件还在发送，跳过这次定时发送")
        return
    await _run("schedule", collect=True)     # 每天定时一律先补采，不弹框（用户定的）


def start_send(collect: bool = False) -> bool:
    """「立即发送」：起后台作业，立刻返回。已有一轮在跑就返回 False。
    collect 是页面确认框里的选择：先把今天还没采集翻译过的数据源采一遍再发，还是直接用已有数据发

    运行标记必须在这里**同步**置上（同 start_backfill_translation）：等协程被调度起来再置的话，连点两下会起出两轮
    """
    if not _begin():
        return False
    job = asyncio.create_task(_run("manual", collect))
    _jobs.add(job)
    job.add_done_callback(_jobs.discard)
    return True


def _begin() -> bool:
    if _state["running"]:
        return False
    _state.update(running=datetime.now().isoformat(timespec="seconds"), progress="正在挑选新回复")
    return True


def status(scheduler=None) -> dict:
    raw = storage.get_app_config(STATUS_PREFIX)
    return {
        "running": bool(_state["running"]),
        "progress": _state["progress"] if _state["running"] else "",
        "task_id": _state["task_id"] if _state["running"] else None,
        "last": json.loads(raw["last"]) if raw.get("last") else None,
        "next_run": _next_run(scheduler if scheduler is not None else _global_scheduler()),
    }


async def _run(trigger: str, collect: bool = False) -> dict:
    """跑一轮并记下结果。collect=True 时先补采今天还没采集翻译过的数据源，补采没成功就不发（用户定的）。
    后台作业和定时回调的异常没人接，这里一律变成一条 failed 记录"""
    started = datetime.now()
    task_ids: List[str] = []
    try:
        failure = await _ensure_fresh(task_ids) if collect else None
        if failure:
            result = {"outcome": "failed", "message": f"{failure}，这次没有发送邮件"}
        else:
            _state["progress"] = "正在挑选新回复"
            # 基准取补采之后的「现在」：补采那几分钟里发表的回复也算进窗口
            result = await _send_digest(datetime.now())
    except Exception as e:
        logger.exception("邮件通知发送失败")
        result = {"outcome": "failed", "message": str(e)}
    record = {
        "trigger": trigger,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "task_ids": task_ids,
        **result,
    }
    try:
        storage.set_app_config(STATUS_PREFIX, {"last": json.dumps(record, ensure_ascii=False)})
    finally:
        _state.update(running=None, progress="", task_id=None)
    logger.info("邮件通知（%s）：%s %s", trigger, record["outcome"], record.get("message", ""))
    return record


# ===== 发送前补采 =====

def _done_today(task: dict, source_id: str, today) -> bool:
    """今天 0 点以后成功完成、既采集了这个来源又做了翻译的任务（用户定的口径）。失败、取消、只采集没翻译的都不算"""
    finished = task.get("completed_at")
    if task.get("status") != TaskStatus.COMPLETED or not isinstance(finished, datetime) or finished.date() != today:
        return False
    steps = task.get("plan") or []
    collected = any(s.get("action") == "collect" and s.get("status") == "completed"
                    and (s.get("params") or {}).get("source_id") == source_id for s in steps)
    return collected and any(s.get("action") == "translate" and s.get("status") == "completed" for s in steps)


def _stale(sources: List[dict]) -> List[dict]:
    """今天还没采集翻译过、要先采的。停用的数据源任务不碰它（用户定的），邮件里照用它已有的帖子"""
    today = datetime.now().date()
    tasks = list(orchestrator.tasks.values())
    return [s for s in sources if s.get("enabled", True) and not any(_done_today(t, s["id"], today) for t in tasks)]


def _selected_sources() -> List[dict]:
    """配置里勾选、而且还在的数据源"""
    registry = {s["id"]: s for s in source_service.list_sources()}
    return [registry[sid] for sid in load_config()["source_ids"] if sid in registry]


def _brief(source: dict) -> dict:
    collector = _collector(source)
    return {"id": source["id"], "name": source["name"],
            "collector_name": collector.display_name if collector else source["collector_id"]}


def precheck() -> dict:
    """点「立即发送」前问一句：所选的（启用的）数据源里哪些今天还没采集翻译过，页面据此弹确认框"""
    return {"stale": [_brief(s) for s in _stale(_selected_sources())]}


async def _ensure_fresh(task_ids: List[str]) -> Optional[str]:
    """今天还没成功采集翻译过的（启用的）数据源，各发起一个「采集 + 翻译」任务并等它跑完。
    一个来源一个任务，失败了说得清是哪个。发起的任务号追加进 task_ids；有一个没成功就返回原因"""
    minutes = settings.task_timeout_minutes
    if not _stale(_selected_sources()):
        return None      # 所选的今天都采集翻译过：不用补采，也不用等别的任务（评审发现）
    # 先等正在排队、运行的任务：定时采集刚好在跑时，等它跑完它就算今天采过，不再重复采一遍。
    # 任务还在解析阶段时看不出它采哪个来源，只能一律等
    waited = 0.0
    while any(t.get("status") in _UNFINISHED for t in orchestrator.tasks.values()):
        if waited >= minutes * 60:
            # 等到头了还有没跑完的就别再发起：它可能正采着同一个来源，再起一个就是把真站采两遍（评审发现）
            return f"等了 {minutes} 分钟还有任务没跑完，没有补采"
        _state["progress"] = "等待正在运行的任务结束"
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    # 前一个来源可能采了几十分钟，这期间被停用、被删掉的来源：任务执行时只认启用的来源（execute_task 对给定计划
    # 也照样校验），会以「已停用或已删除」失败，这一轮按规则不发
    for source in _stale(_selected_sources()):
        task_id = str(uuid.uuid4())
        orchestrator.create_task(
            task_id, f"邮件通知自动发起：采集并翻译「{source['name']}」",
            # source_name 只给任务列表显示用（同 _resolve_sources 补的那个）
            plan=[{"action": "collect", "params": {"source_id": source["id"], "source_name": source["name"]}},
                  {"action": "translate", "params": {"target_lang": "zh-CN"}}],
            scheduled_by=AUTO_TASK_OWNER,
        )
        task_ids.append(task_id)
        _state["task_id"] = task_id
        orchestrator.run_task_async(task_id)
        task = await _wait_task(task_id, source["name"], 2 * minutes * 60)
        if task.get("status") != TaskStatus.COMPLETED:
            if task.get("status") in _UNFINISHED:
                # 超时了就别让它接着跑：这一轮不发，它采回来、翻好的没人用，还要付翻译费（评审发现）
                orchestrator.cancel_task(task_id)
                reason = f"超过 {2 * minutes} 分钟还没跑完，已取消"
            elif task.get("status") == TaskStatus.CANCELLED:
                reason = "任务被取消"
            else:
                reason = task.get("error_message") or "任务失败"
            return f"自动采集「{source['name']}」没成功：{reason}（任务 {task_id[:8]}）"
    _state["task_id"] = None
    return None


async def _wait_task(task_id: str, name: str, timeout: float) -> dict:
    """等任务进入终态（或超时），一边把它进行到哪一步报给页面"""
    waited = 0.0
    while True:
        task = orchestrator.get_task(task_id) or {}
        if task.get("status") not in _UNFINISHED or waited >= timeout:
            return task
        _state["progress"] = f"正在采集并翻译「{name}」：{task.get('current_step') or '排队等待中'}"
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def _raw_anchors() -> Dict[str, str]:
    raw = storage.get_app_config(MARKS_PREFIX).get("anchors")
    try:
        anchors = json.loads(raw) if raw else {}
    except ValueError:
        anchors = {}
    return anchors if isinstance(anchors, dict) else {}


def _anchors(source_ids: List[str], now: datetime, days: int) -> Dict[str, datetime]:
    """{来源 id: 补发起点}。起点 = 这个来源第一次进邮件那一轮的窗口起点，**之后一直不动**：
    补发就是「起点之后发表、还没随邮件发出去过」的全部 —— 不管中间采集停了几天、哪一轮失败、天数改过几次。
    （三轮评审：拿每轮都会移动的水位线当下界，总有一种情况漏 —— 采集停摆比窗口长时，「没有新内容」的那几轮把下界挪过头了）
    这一轮才第一次出现的来源就地起一个；记录坏了也当作没有、就地重起（只是这一轮不补发），不让每一轮都在这里失败
    """
    raw = _raw_anchors()
    out: Dict[str, datetime] = {}
    changed = False
    for sid in source_ids:
        try:
            out[sid] = datetime.fromisoformat(raw[sid])
        except (KeyError, TypeError, ValueError):
            out[sid] = now - timedelta(days=days)
            raw[sid] = out[sid].isoformat(timespec="seconds")
            changed = True
    if changed:
        storage.set_app_config(MARKS_PREFIX, {"anchors": json.dumps(raw)})
    return out


def _forget_anchors(keep: List[str]) -> None:
    """取消勾选的来源忘掉起点：再勾回来时从那一刻重新算，不然这段时间的旧帖会被当「补发」一股脑塞进来（评审发现）"""
    raw = _raw_anchors()
    if set(raw) - set(keep):
        storage.set_app_config(MARKS_PREFIX, {"anchors": json.dumps({k: v for k, v in raw.items() if k in keep})})


async def _send_digest(now: datetime) -> dict:
    cfg = load_config()
    if not cfg["saved"]:
        return {"outcome": "failed", "message": "邮件通知还没配置，请先在「邮件通知」页填好并保存"}
    days = cfg["days"] or DAYS_DEFAULT
    registry = {s["id"]: s for s in source_service.list_sources()}
    sources = [registry[sid] for sid in cfg["source_ids"] if sid in registry]
    if not sources:
        return {"outcome": "failed", "message": "所选的数据源都已被删除，请到「邮件通知」页重新选择"}
    dropped = len(cfg["source_ids"]) - len(sources)
    note = f"（已跳过 {dropped} 个被删除的数据源）" if dropped else ""

    sections = collect_fresh(sources, days, now, _anchors([s["id"] for s in sources], now, days))
    replies, roots = _counts(sections)
    if not replies + roots:
        return {"outcome": "skipped", "message": f"近 {days} 天没有新回复和新主贴，这次没有发送邮件{note}"}
    late_n, repeat_n = _extras(sections)
    if repeat_n == replies + roots:
        # 窗口里全是前几封发过的（窗口比发送间隔长、这几天又没新东西）：不发 —— 页面上写的是「没有新内容的那天不发」（评审发现）
        return {"outcome": "skipped",
                "message": f"近 {days} 天没有首次出现的新回复和新主贴（窗口里的 {repeat_n} 条前几封都发过），这次没有发送邮件{note}"}

    failed, roots_failed, late_failed, seen_failed, reason = await _backfill(sections)
    if failed:
        return {
            "outcome": "failed", "reply_count": replies, "root_count": roots,
            "message": f"有 {failed} 条新回复 / 新主贴翻译失败（已重试 {TRANSLATE_RETRIES} 次），这次没有发送邮件。"
                       f"原因：{reason}。到「LLM 配置」页点「测试连接」检查后，再回来点「立即发送」",
        }
    if seen_failed:
        note = f"（另有 {seen_failed} 条已发过的没翻成，邮件里只放了原文）{note}"
    if late_failed:
        note = f"（另有 {late_failed} 条补发的没翻成，邮件里只放了原文）{note}"
    if roots_failed:
        note = f"（另有 {roots_failed} 条主贴没翻成，邮件里只放了原文）{note}"

    _state["progress"] = "正在发送邮件"
    msg = build_message(cfg, sections, days, now)
    await asyncio.to_thread(deliver, cfg, msg)
    # 信已经发出去了，下面记账失败也不能报成「发送失败」—— 只是下一封可能把这几条当补发再发一遍
    try:
        sent = [r["post"] for s in sections for t in s["threads"] for r in t["replies"]]
        sent += [t["root"] for s in sections for t in s["threads"] if t.get("new_root")]
        storage.mark_notified(sent, now.isoformat(timespec="seconds"))
    except Exception:
        logger.exception("邮件已发出，但记录「哪些帖子发过」失败")
        note = f"（记录哪些帖子已发出时出错，下一封可能重复其中几条）{note}"
    return {
        "outcome": "sent", "reply_count": replies, "root_count": roots,
        "subject": str(msg["Subject"]), "attachment": replies + roots > BODY_LIMIT,
        "message": f"已发送给 {len(cfg['recipients'])} 个收件人：{_label(replies, roots)}{note}",
    }


# ===== 选新回复 =====

def _collector(source: dict) -> Optional[Collector]:
    try:
        return get_collector(source["collector_id"])
    except ValueError:
        return None   # 采集器被下掉了：同 results._thread_sources()，不能让整轮发送跟着失败


def _source_now(collector: Optional[Collector], now: datetime) -> datetime:
    """「现在」换成这个来源时间戳所在时区的墙上时间（时间戳都不带时区）。Tweakers 是荷兰时间：
    拿宿主机（上海）的现在直接去比，窗口整体偏 6~7 小时，每天有一段回复哪封邮件都进不去"""
    tz = collector.timestamp_tz if collector else None
    return now.astimezone(ZoneInfo(tz)).replace(tzinfo=None) if tz else now


def _is_new_root(post: dict, base: datetime, days: int) -> bool:
    """近 N 天发的顶层主贴。丢了父贴的回复（reply_level > 0）也排在树根，但它不是主贴（同结果页的徽标判据）"""
    if post.get("parent_fingerprint") or int(post.get("reply_level") or 0):
        return False
    at = parse_time(post.get("timestamp"))
    return bool(at) and at >= base - timedelta(days=days)


def _recent(post: dict, since: datetime) -> bool:
    """近 RECENT_HOURS 小时发表的，邮件里垫底色。每条只看自己的发表时间，不从主贴推到它下面的回复
    （所以主贴单独包一层，见 _thread_html）"""
    at = parse_time(post.get("timestamp"))
    return bool(at) and at >= since


def collect_fresh(sources: List[dict], days: int, now: datetime,
                  anchors: Optional[Dict[str, datetime]] = None) -> List[dict]:
    """每个来源一节：{source, collector_name, base, threads: [{root, new_root, late_root, repeat_root, recent_root,
    replies: [{post, reply_to, late, repeat, recent}]}]}。base 是换到这个来源时钟的「现在」。

    新回复的判据就是结果页「只看新回复」那一个（mark_fresh_replies），基准换成 now（换到来源时间戳的时区）。
    另外两样也进邮件（2026-10-17 用户让我定）：
    · **新主贴**：近 N 天发的顶层主贴，没有回复也算 —— 不然一条新发的负面帖没人接话，就哪封都不出现。
      Tweakers 那种一源一串的来源没有新主贴：主题是用户挑好的那一条，而且只是采到的最早一楼
    · **补发**（late）：发表在这个来源的补发起点（anchors[来源]，见 _anchors）之后、还没随哪封发送成功的邮件发出去过
      （posts.notified_at 为空）的回复和新主贴。采集晚了、某一轮失败、采集停了几天，都落在这一条里
    窗口里的条目要是前几封已经发过（窗口比发送间隔长时会这样），照用户定的窗口照列，标「已发过」（repeat）方便跳过。
    近 24 小时发表的（recent / recent_root，主贴、回复都算）垫底色，不管窗口选几天（2026-10-20 用户要求），基准同窗口。
    老主贴只作上下文 —— 回复脱离主贴读不懂。主贴按发表时间从新到旧（同结果页和导出），串里的新回复按发表先后
    """
    sections = []
    for source in sources:
        sid = source["id"]
        collector = _collector(source)
        posts = storage.load_posts([sid])
        flat = {sid} if collector and collector.thread_kind == "thread" else set()
        base = _source_now(collector, now)
        floor = base - timedelta(days=days)
        recent_from = base - timedelta(hours=RECENT_HOURS)
        anchor = (anchors or {}).get(sid)
        start = min(floor, _source_now(collector, anchor)) if anchor else floor
        # 一次组树挑全部候选：窗口和补发都在「起点之后」里（起点最晚也就是窗口起点），再按时间、发没发过分开
        span = max(days, math.ceil((base - start).total_seconds() / 86400))
        marked = mark_fresh_replies(posts, span, flat, base=base)
        by_key = {post_key(p): p for p in posts}
        sent = storage.notified_fingerprints(sid)
        reply_keys = {k for k in marked if parse_time(by_key[k].get("timestamp")) >= floor}
        root_keys = set() if flat else {post_key(p) for p in posts if _is_new_root(p, base, days)}
        late_replies, late_roots = set(), set()
        for p in posts:
            key = post_key(p)
            at = parse_time(p.get("timestamp"))
            if not at or at < start or p.get("fingerprint") in sent or key in reply_keys or key in root_keys:
                continue
            if key in marked:
                late_replies.add(key)
            elif not flat and _is_new_root(p, base, span):
                late_roots.add(key)
        reply_keys |= late_replies
        root_keys |= late_roots
        starts = flat_thread_starts(posts, flat)
        threads = thread_of(posts)
        groups: Dict[str, dict] = {}

        def group(root: dict) -> dict:
            return groups.setdefault(post_key(root), {"root": root, "replies": [], "new_root": False,
                                                      "late_root": False, "repeat_root": False})

        for p in posts:
            key = post_key(p)
            repeat = p.get("fingerprint") in sent
            if key in root_keys:
                group(p).update(new_root=True, late_root=key in late_roots, repeat_root=repeat)
            elif key in reply_keys:
                # 有父指针的走讨论串（串首就是顶层主贴）；Tweakers 平铺的楼层挂在主题楼层下
                root = threads[key][0] if p.get("parent_fingerprint") else starts[sid]
                parent = by_key.get(parent_key(p))
                # 第 2 层起写明回的是谁（同结果页）；直接回主贴的不用写
                reply_to = parent["username"] if parent and post_key(parent) != post_key(root) else ""
                group(root)["replies"].append({"post": p, "reply_to": reply_to,
                                               "late": key in late_replies, "repeat": repeat,
                                               "recent": _recent(p, recent_from)})
        ordered = sorted(groups.values(), key=lambda g: sort_time(g["root"].get("timestamp")), reverse=True)
        for g in ordered:
            g["replies"].sort(key=lambda r: parse_time(r["post"].get("timestamp")))
            g["recent_root"] = _recent(g["root"], recent_from)
        # base 留给 _newest：跨来源比新旧要按各自的时钟算「发表了多久」，墙上时间直接比会差出时区
        sections.append({"source": source, "threads": ordered, "base": base,
                         "collector_name": collector.display_name if collector else source.get("collector_id", "")})
    return sections


def _counts(sections: List[dict]) -> Tuple[int, int]:
    """(新回复条数, 新主贴条数)"""
    replies = sum(len(t["replies"]) for s in sections for t in s["threads"])
    roots = sum(1 for s in sections for t in s["threads"] if t.get("new_root"))
    return replies, roots


def _label(replies: int, roots: int) -> str:
    parts = ([f"新回复 {replies} 条"] if replies else []) + ([f"新主贴 {roots} 条"] if roots else [])
    return "、".join(parts) or "没有新内容"


def _extras(sections: List[dict]) -> Tuple[int, int]:
    """(补发条数, 前几封已发过的条数)，新回复和新主贴一起数"""
    late = repeat = 0
    for s in sections:
        for t in s["threads"]:
            if t.get("new_root"):
                late += bool(t.get("late_root"))
                repeat += bool(t.get("repeat_root"))
            late += sum(1 for r in t["replies"] if r.get("late"))
            repeat += sum(1 for r in t["replies"] if r.get("repeat"))
    return late, repeat


def _extras_note(late: int, repeat: int) -> str:
    """标题和抬头写着「近 N 天」，条数里却有窗口外的补发、有前几封发过的 —— 不说明的话读的人会以为窗口算错了（评审发现）"""
    parts = ([f"补发 {late} 条"] if late else []) + ([f"已发过 {repeat} 条"] if repeat else [])
    return f"（含{'、'.join(parts)}）" if parts else ""


# ===== 补译 =====

async def _claim(source_ids: List[str]) -> None:
    """占住来源再补译。同一来源正被结果页补译或任务里的翻译步骤翻着时另起一轮，就是同一批帖子付两遍钱，
    重译失败还会把别人刚翻好的冲成失败标记。被占着就每秒重试，等太久就放弃这一轮"""
    deadline = time.monotonic() + CLAIM_WAIT_SECONDS
    while orchestrator.claim_translation(source_ids, CLAIM_OWNER):
        if time.monotonic() > deadline:
            raise NotifyError(f"同一数据源的翻译 {CLAIM_WAIT_SECONDS // 60} 分钟还没结束，"
                              "这次没有发送邮件，稍后再点「立即发送」")
        _state["progress"] = "同一数据源正在翻译（结果页补译或任务里的翻译步骤），等它结束再补译"
        await asyncio.sleep(1)


async def _backfill(sections: List[dict]) -> Tuple[int, int, int, int, str]:
    """要显示的帖子缺译文的先补，返回 (没翻成的：新内容, 老主贴, 补发, 已发过, 原因)。

    全都一起翻，但**只有按窗口挑出来、首次发出的新内容（新回复、新主贴）翻不成才拦住邮件**（用户定的）。其余翻不成只放原文：
    · 作上下文的老主贴 —— 一条总被拒译的老主贴不能让每天的邮件都发不出去（评审发现）
    · 补发的 —— 补发起点不动，它没发出去就会一直被挑出来；它要是拦邮件，就每轮都失败、永远发不出去（评审发现）。
      按窗口挑出来的过了窗口会自己掉出去，所以只有它们拦得起
    · 前几封已发过的 —— 收件人早就收到过；它要是拦邮件，改宽窗口后一条早先只带原文发出去的就能让邮件连着失败（评审发现）
    翻完还有失败的就整轮再翻，最多再翻 TRANSLATE_RETRIES 轮（用户定的）。译者每轮已经对失败条目单条重译过一次，
    这里的重试是隔一轮再来：模型临时抽风、限流退避用完这类情况，下一轮往往就过了
    """
    required: Dict[str, dict] = {}
    context: Dict[str, dict] = {}
    late: Dict[str, dict] = {}
    seen: Dict[str, dict] = {}

    def bucket(is_late: bool, is_repeat: bool) -> Dict[str, dict]:
        return late if is_late else seen if is_repeat else required

    for s in sections:
        for t in s["threads"]:
            root = t["root"]
            (bucket(t.get("late_root"), t.get("repeat_root")) if t.get("new_root") else context)[post_key(root)] = root
            for r in t["replies"]:
                bucket(r.get("late"), r.get("repeat"))[post_key(r["post"])] = r["post"]
    pending = [p for p in {**context, **late, **seen, **required}.values() if needs_translation(p)]
    if not pending:
        return 0, 0, 0, 0, ""
    source_ids = sorted({p.get("source") or "tweakers" for p in pending})
    await _claim(source_ids)
    try:
        # 占到来源后先回库刷一遍译文（同任务里的翻译步骤）：等别人翻完才占到时，它刚翻好的在手里这份还是空译文，
        # 不刷就会再翻一遍。原地写回 —— 邮件就是用这几个对象渲染的
        for p, latest in zip(pending, _refresh_translations(pending)):
            p["translation"] = latest.get("translation", "")
            if latest.get("_processed"):
                p["_processed"] = latest["_processed"]
        warnings: List[str] = []
        for attempt in range(1 + TRANSLATE_RETRIES):
            todo = [p for p in pending if needs_translation(p)]
            if not todo:
                break
            _state["progress"] = (f"正在补译 {len(todo)} 条缺译文的帖子" if not attempt
                                  else f"还有 {len(todo)} 条没翻成，第 {attempt} 次重试")
            # 进度对象只为收 warning：批量调用的异常被译者吃掉、只留一条 warning，翻不成时拿它当原因。
            # 它顺带发的进度事件落在没人订阅的 notify 频道上，直接丢掉
            sink = _BackfillProgress("notify", 0, len(todo), len(todo), warnings)
            try:
                result = await TranslatorService.execute("notify", todo, {}, sink, 0)
            except Exception as e:
                warnings.append(str(e))
                continue
            translated = result.get("posts", [])
            for p in translated:
                p.setdefault("_processed", {})["translated"] = True
            storage.save_translations(translated)
        def failed(bucket: Dict[str, dict]) -> int:
            return sum(1 for p in bucket.values() if needs_translation(p))

        return (failed(required), failed(context), failed(late), failed(seen),
                warnings[0] if warnings else "模型返回的都是空内容")
    finally:
        orchestrator.release_translation(source_ids, CLAIM_OWNER)


# ===== 组邮件 =====

_FONT = "font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif"
_META = "color:#6b7280;font-size:12px"
_ORIG = "margin:6px 0 0;color:#6b7280;font-size:13px;line-height:1.6"
_ZH = "margin:4px 0 0;color:#111827;font-size:14px;line-height:1.6"
_CARD = "border:1px solid #e5e7eb;border-radius:8px;padding:4px;margin:0 0 12px"
# 主贴、回复各占一块，垫不垫底色内边距都一样：只在垫色时加内边距，同一封信里的卡片会错位（评审发现）。
# 卡片的内边距相应减掉，文字的横向位置和原来一样；纵向每条回复多出几像素，相邻的两个垫色块才隔得开
_ROOT = "padding:8px 10px;border-radius:6px"
_REPLY = "margin:8px 0 0 24px;padding:6px 10px;border-left:3px solid #f59e0b;border-radius:0 6px 6px 0"
_BADGE = "display:inline-block;padding:0 6px;border-radius:4px;background:#eef2ff;color:#4338ca;font-size:12px"
_NEW_BADGE = "display:inline-block;padding:0 6px;border-radius:4px;background:#fff7ed;color:#c2410c;font-size:12px"
_LATE_TAG = "display:inline-block;padding:0 6px;border-radius:4px;background:#f1f5f9;color:#475569;font-size:12px"
# 近 24 小时发表的帖子垫的底色：浅、不艳（用户要求）。用绿是因为蓝、橙、灰几种标签垫在上面还分得清
_RECENT_BG = "#e8f5e9"
_TINT = f"background:{_RECENT_BG}"
_RECENT_TAG = f"display:inline-block;padding:0 6px;border-radius:4px;background:{_RECENT_BG};color:#374151;font-size:12px"


def _esc(text: str) -> str:
    """转义，换行还原成 <br>：采集时多段正文用换行拼接，邮件客户端未必认 white-space:pre-wrap"""
    return html.escape(text or "").replace("\n", "<br>")


def _time(post: dict) -> str:
    return normalize_timestamp(post.get("timestamp") or "").strip() or "时间未知"


def _body_html(post: dict, clip: Optional[int] = None) -> str:
    """原文在上（灰）、译文在下，同结果页的「双语」视图"""
    def cut(text):
        text = (text or "").strip()
        return text[:clip] + "…" if clip and len(text) > clip else text

    content = cut(post.get("content"))
    if content:
        # 判据同 needs_translation：失败标记（「[翻译失败] 原文…」）不是译文，不能当译文放进邮件
        parts = [f'<p style="{_ORIG}">{_esc(content)}</p>',
                 f'<p style="{_ZH}">{_esc(cut(post.get("translation")))}</p>' if not needs_translation(post)
                 else f'<p style="{_ORIG};font-style:italic">（尚未翻译）</p>']
    else:
        parts = [f'<p style="{_ORIG};font-style:italic">（没有文字，可能是图片或表情）</p>']
    images = len(post.get("images") or [])
    if images:
        parts.append(f'<p style="{_META};margin:4px 0 0">含 {images} 张图片（邮件里不附图片，到结果页查看）</p>')
    return "".join(parts)


def _thread_html(thread: dict) -> str:
    root = thread["root"]
    new = thread.get("new_root")
    badge = f'<span style="{_NEW_BADGE}">🆕 新主贴</span>' if new else f'<span style="{_BADGE}">主贴</span>'
    head = (f'<div>{badge} <b>{_esc(root.get("username"))}</b> '
            f'<span style="{_META}">{_esc(_time(root))}</span>'
            f'{_tags(thread.get("late_root"), new and thread.get("repeat_root"))}</div>')
    replies = []
    for r in thread["replies"]:
        p = r["post"]
        to = f' <span style="{_META}">回复 {_esc(r["reply_to"])}</span>' if r["reply_to"] else ""
        style = f"{_REPLY};{_TINT}" if r.get("recent") else _REPLY
        replies.append(
            f'<div class="reply" style="{style}"><div>↳ <b>{_esc(p.get("username"))}</b>{to} '
            f'<span style="{_META}">{_esc(_time(p))}</span>{_tags(r.get("late"), r.get("repeat"))}</div>'
            f'{_body_html(p)}</div>')
    # 新主贴是新内容本身，全文照登；老主贴只是上下文，太长截断。正文截取时没排进最新那几条、只因为它的回复排进去了
    # 才出现的新主贴（clip_root），在正文里也只是上下文 —— 全文在附件里
    body = _body_html(root, None if new and not thread.get("clip_root") else ROOT_CLIP)
    # 主贴单独包一层：底色只垫主贴自己，不连着下面的回复（见 _recent）
    root_style = f"{_ROOT};{_TINT}" if thread.get("recent_root") else _ROOT
    return (f'<div class="thread" style="{_CARD}"><div class="root" style="{root_style}">{head}{body}</div>'
            f'{"".join(replies)}</div>')


def _tags(late: bool, repeat: bool) -> str:
    """补发的标一下：它的发表时间已经在窗口外，不说明的话读的人会以为窗口算错了。
    前几封已经发过的也标一下：窗口比发送间隔长时同一条会连着出现几封，读的人好跳过"""
    if late:
        return f' <span style="{_LATE_TAG}">补发：之前没发出</span>'
    return f' <span style="{_LATE_TAG}">已发过</span>' if repeat else ""


def _section_html(section: dict, days: int) -> str:
    replies, roots = section.get("counts") or _counts([section])
    title = f'来自：{_esc(section["collector_name"])}「{_esc(section["source"].get("name"))}」'
    count = _label(replies, roots) + ("（正文只列了其中最新的一部分，其余见附件）" if section.get("truncated") else "")
    if section["threads"]:
        body = "".join(_thread_html(t) for t in section["threads"])
    elif replies or roots:
        body = f'<p style="{_META}">这些比正文里的都早，见附件</p>'
    else:
        body = f'<p style="{_META}">近 {days} 天没有新回复和新主贴</p>'
    return (f'<h3 style="margin:20px 0 8px;font-size:15px">{title}'
            f'<span style="{_META};font-weight:normal"> · {count}</span></h3>{body}')


def render_html(sections: List[dict], days: int, now: datetime, title: str,
                counts: Tuple[int, int], note: str = "", extras: str = "") -> str:
    """一份完整的 HTML 文档：邮件正文和附件都用它（附件要能单独用浏览器打开）。样式全部内联 —— 邮件客户端常把 <style> 剥掉"""
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    end = now.strftime("%Y-%m-%d %H:%M")
    # 图例只在真有垫了底色的帖子时放：有图例却找不到对应的帖子，读的人会以为漏了
    legend = (f' · <span style="{_RECENT_TAG}">近 {RECENT_HOURS} 小时发表</span>'
              if any(t.get("recent_root") or any(r.get("recent") for r in t["replies"])
                     for s in sections for t in s["threads"]) else "")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<title>{_esc(title)}</title></head>'
        '<body style="margin:0;padding:16px;background:#ffffff">'
        f'<div style="{_FONT};max-width:760px;margin:0 auto;color:#111827;font-size:14px">'
        '<h2 style="margin:0 0 4px;font-size:18px">HYXi 新回复 / 新主贴摘要</h2>'
        f'<p style="{_META};margin:0 0 12px">近 {days} 天（{start} ～ {end}）· {_label(*counts)}{extras} · 只含已采集到的数据{legend}</p>'
        f'{note}{"".join(_section_html(s, days) for s in sections)}'
        f'<p style="{_META};margin-top:20px">由 HYXi 舆情分析平台发送 · 生成于 {end}</p>'
        '</div></body></html>'
    )


def _newest(sections: List[dict], limit: int) -> List[dict]:
    """只留最新的 limit 条新内容（新回复和新主贴放在一起，按发表了多久排）。来源、串的结构不变，空了的串去掉；
    每节记下原来的条数和「截过没有」，标题照实说。「发表了多久」按各来源自己的时钟算（collect_fresh 留下的 base）：
    墙上时间直接比的话，荷兰时间的楼层会被当成早了 6~7 小时、挤出正文（评审发现）"""
    aged = []
    for s in sections:
        for t in s["threads"]:
            if t.get("new_root"):
                aged.append((s["base"] - parse_time(t["root"].get("timestamp")), id(t)))
            aged.extend((s["base"] - parse_time(r["post"].get("timestamp")), id(r)) for r in t["replies"])
    keep = {i for _, i in sorted(aged, key=lambda x: x[0])[:limit]}
    out = []
    for s in sections:
        threads, shown = [], 0
        for t in s["threads"]:
            replies = [r for r in t["replies"] if id(r) in keep]
            root_kept = bool(t.get("new_root")) and id(t) in keep
            if replies or root_kept:
                threads.append(dict(t, replies=replies, clip_root=not root_kept))
                shown += len(replies) + root_kept
        counts = _counts([s])
        out.append(dict(s, threads=threads, counts=counts, truncated=shown < sum(counts)))
    return out


def subject_for(sections: List[dict], days: int, counts: Tuple[int, int]) -> str:
    """只点有新内容的来源：选了却没有的只在正文里说一声 —— 拿它打头的标题会让人以为新回复是从那来的
    （真实数据实测过：Tweakers 来源一条帖子都没有，标题却是「Tweakers…等 2 个数据源」）"""
    active = [s for s in sections if s["threads"]] or sections
    first = active[0]
    label = f'{first["collector_name"]}「{first["source"].get("name")}」'
    more = f"等 {len(active)} 个数据源" if len(active) > 1 else ""
    # 前缀不写「新回复」：只有新主贴的那天会变成「【HYXi 新回复】… · 新主贴 1 条」，自相矛盾（评审发现）
    return f"【HYXi】{label}{more} · 近 {days} 天 · {_label(*counts)}{_extras_note(*_extras(sections))}"


def _new_message(cfg: dict, subject: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Address(display_name="HYXi 舆情分析", addr_spec=cfg["sender"])
    msg["To"] = ", ".join(cfg["recipients"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["sender"].rsplit("@", 1)[-1])
    return msg


def build_message(cfg: dict, sections: List[dict], days: int, now: datetime) -> EmailMessage:
    counts = _counts(sections)
    total = sum(counts)
    subject = subject_for(sections, days, counts)
    msg = _new_message(cfg, subject)
    attach = total > BODY_LIMIT
    filename = f"hyxi-digest-{now:%Y%m%d-%H%M}.html"
    body_sections, note = sections, ""
    if attach:
        body_sections = _newest(sections, BODY_LIMIT)
        note = (f'<p style="margin:0 0 12px;padding:8px 10px;background:#fffbeb;border:1px solid #fde68a;'
                f'border-radius:6px">新内容共 {total} 条（{_label(*counts)}），正文只列最新的 {BODY_LIMIT} 条；'
                f'完整的 {total} 条见附件「{filename}」（用浏览器打开）。</p>')
    msg.set_content(f"{subject}\n\n本邮件是 HTML 格式，请用支持 HTML 的邮件客户端查看"
                    + (f"；完整内容见附件 {filename}" if attach else "") + "。\n")
    extras = _extras_note(*_extras(sections))
    msg.add_alternative(render_html(body_sections, days, now, subject, counts, note, extras), subtype="html")
    if attach:
        msg.add_attachment(render_html(sections, days, now, subject, counts, extras=extras),
                           subtype="html", filename=filename)
    return msg


# ===== 发信 =====

def _is_loopback(host: str) -> bool:
    """本机上的中继：流量不出这台机器，不加密也不怕被截"""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _login(server: smtplib.SMTP, user: str, password: str) -> None:
    """就是 smtplib.login()，只多一处：登录最终失败时，报真正被拒的那一次。

    smtplib.login() 被拒后会换下一种方式再试（服务器只认其中一种时靠这个登上，不能去掉），失败时抛的是最后
    一次的错，而第一次被拒之后的那次往往说明不了问题：QQ 邮箱拒绝时就挂断，第二次撞上断开的连接，535 被盖成
    「Connection unexpectedly closed」（用户实测报过）；也有服务器再回个 421 才挂断。所以有 535 先报第一个
    535，没有就报最后一次被拒（同 smtplib）；从没被拒过就断开的，照实报断开，不猜原因
    """
    rejected = []
    auth = server.auth

    def remember_rejection(*args, **kwargs):
        try:
            return auth(*args, **kwargs)
        except smtplib.SMTPAuthenticationError as e:
            rejected.append(e)
            raise

    server.auth = remember_rejection    # login() 逐个方式调的就是它
    try:
        server.login(user, password)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected):
        if rejected:
            raise next((e for e in rejected if e.smtp_code == 535), rejected[-1]) from None
        raise


def _server_said(e: smtplib.SMTPResponseException, user: str, password: str) -> str:
    """服务器回复的原话，收成一行、截到 200 字；没说就讲明没说。

    有的服务器拒绝时会回显收到的认证串（PLAIN 的 base64 里就是密码），而这句话会进发送记录、显示在页面上 ——
    密码只进不出，先抹掉明文和它的两种 base64 形式
    """
    said = e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
    for secret in (base64.b64encode(f"\0{user}\0{password}".encode()).decode(),
                   base64.b64encode(password.encode()).decode(), password):
        if secret:
            said = said.replace(secret, "***")
    said = " ".join(said.split())[:200]
    return f"服务器原话：{said}" if said else "服务器没有说明原因"


def deliver(cfg: dict, msg: EmailMessage) -> None:
    """把信交给 SMTP 服务器（阻塞，调用方放进线程跑）。失败一律抛 NotifyError，消息是能照着改的人话。

    证书照常严格校验（ssl.create_default_context）：不校验的话，冒充的服务器就能拿到密码
    """
    host = cfg["smtp_host"]
    port = cfg["smtp_ssl_port"] if cfg["use_ssl"] else cfg["smtp_port"]
    where = f"{host}:{port}"
    password = crypto.decrypt(cfg["password_enc"])
    try:
        if cfg["use_ssl"]:
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
        with server:
            if not cfg["use_ssl"]:
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                elif not _is_loopback(host):
                    raise NotifyError(f"SMTP 服务器 {where} 不支持加密连接（STARTTLS）。为了不明文传输密码，"
                                      "请勾选「使用 SSL」并填 SSL 端口（一般是 465）")
            _login(server, cfg["username"], password)
            server.send_message(msg)
    except NotifyError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        # 附上服务器原话：QQ 的 535 不只是密码错，没开 SMTP 服务、登录太频繁也是它
        said = _server_said(e, cfg["username"], password)
        if e.smtp_code == 535:
            # 535 是服务器说凭据不对；QQ 在 SMTP 服务没开、登录太频繁时也回它，所以几种情况都列出来，不说「多半」。
            # 提示里不点名哪家邮箱（用户要求「QQ 字样都要去掉」）
            raise NotifyError(f"SMTP 登录被拒绝（535，服务器认为用户名或密码不对）。{said}。不少邮箱这里要填"
                              "「授权码」而不是登录密码 —— 在邮箱网页版的设置里开启 SMTP 服务后生成；"
                              "SMTP 服务没开启、登录太频繁时，也可能被这样拒绝") from None
        # 别的码不是「凭据不对」（454 临时失败、504 不支持这种方式……），照实报，不猜
        raise NotifyError(f"SMTP 登录被拒绝（{e.smtp_code}）。{said}") from None
    except smtplib.SMTPSenderRefused as e:
        raise NotifyError(f"发件人 {cfg['sender']} 被服务器拒绝（{e.smtp_code}）。"
                          "不少邮箱要求发件人邮箱和登录用户名是同一个") from None
    except smtplib.SMTPRecipientsRefused as e:
        raise NotifyError(f"收件人被服务器拒绝：{'、'.join(e.recipients)}") from None
    except smtplib.SMTPException as e:
        raise NotifyError(f"SMTP 发送失败（{where}）：{e}") from None
    except ssl.SSLCertVerificationError as e:
        raise NotifyError(f"{where} 的证书校验没通过（{e.verify_message}），"
                          "为了不把密码交给冒充的服务器，没有继续发送") from None
    except ssl.SSLError as e:
        raise NotifyError(f"和 {where} 的 SSL 握手失败（{e.reason or e}）：端口和「使用 SSL」对得上吗？"
                          "465 一般是 SSL 端口，25 / 587 不是") from None
    except TimeoutError:
        raise NotifyError(f"连接 {where} 超时：检查服务器地址、端口和「使用 SSL」是否对应，"
                          "以及这个端口有没有被防火墙拦住") from None
    except OSError as e:
        raise NotifyError(f"连不上 SMTP 服务器 {where}（{e.strerror or e}）") from None


def send_test(payload: dict) -> str:
    """「发送测试邮件」：用页面上填的（不必先保存的）SMTP 设置发一封，验证发不发得出去。阻塞，调用方放进线程跑"""
    cfg = _clean(payload, load_config(), require_sources=False)
    msg = _new_message(cfg, "【HYXi】邮件通知测试")
    text = "这是 HYXi 舆情分析平台的测试邮件。收到它，说明「邮件通知」页的 SMTP 设置可以用。"
    msg.set_content(text + "\n")
    msg.add_alternative(f'<!doctype html><html><head><meta charset="utf-8"></head><body>'
                        f'<p style="{_FONT}">{_esc(text)}</p></body></html>', subtype="html")
    deliver(cfg, msg)
    return f"测试邮件已发出，请到 {'、'.join(cfg['recipients'])} 的收件箱查看（没看到就翻一下垃圾箱）"
