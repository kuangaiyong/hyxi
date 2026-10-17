"""邮件通知 —— 把所选数据源近 N 天的新回复（原文 + 译文）发到收件人邮箱。

「新回复」和结果页「只看新回复」是同一个判据（post_tree.mark_fresh_replies），只是基准换成当前时间：
结果页要的是报告隔多久打开都自洽，邮件要的是「这段时间里有什么新的」。邮件只读已采集的数据，不触发采集。

配置存 app_config：`notify.settings`（JSON）+ `notify.password_enc`（SMTP 密码的密文。同数据源凭据：
加密落库、只进不出）。最近一次发送的结果存 `notify_status.last`，页面靠它看到定时发送发没发成。
规格与证据见 docs/features/email-notify.md
"""

import asyncio
import html
import ipaddress
import json
import re
import smtplib
import ssl
import time
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
from app.logging_config import get_logger
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
# 每天定时发送在 APScheduler 里的 job id。配置只有一份，job 也只该有一个
JOB_ID = "notify-daily"

# 新回复时间窗口（天），封闭集合：这个数字会进邮件标题（理由同 post_tree.FRESH_DAYS_CHOICES）
DAYS_CHOICES = (1, 3, 7, 14)
# 不设置 = 当前时间往前一天（用户定的）
DAYS_DEFAULT = 1
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

_DEFAULTS = {
    "recipients": [], "source_ids": [], "days": None,
    "schedule_enabled": False, "schedule_time": "09:00",
    "smtp_host": "", "smtp_port": 25, "smtp_ssl_port": 465, "use_ssl": True,
    "sender": "", "username": "",
}

_EMAIL = re.compile(r'^[^@\s,;<>"]+@[^@\s,;<>"]+\.[^@\s,;<>"]+$')
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 同一时刻只跑一轮。用普通字典不用 asyncio.Lock，理由同 orchestrator._translation_claims
_state: Dict[str, Optional[str]] = {"running": None, "progress": ""}
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
        raise ValueError("请填写 SMTP 服务器，例如 QQ 邮箱是 smtp.qq.com")
    cfg["sender"] = (cfg["sender"] or "").strip()
    if not _valid_email(cfg["sender"]):
        raise ValueError("发件人邮箱格式不对")
    cfg["username"] = (cfg["username"] or "").strip()
    if not cfg["username"]:
        raise ValueError("请填写 SMTP 用户名（QQ 邮箱填完整的邮箱地址）")

    password = payload.get("password") or ""
    if password:
        cfg["password_enc"] = crypto.encrypt(password)   # 没配 TWEAKERS_SECRET_KEY 时抛 ValueError，不降级成明文
    elif not saved.get("password_enc"):
        raise ValueError("请填写 SMTP 密码（QQ、163 等邮箱填「授权码」，不是登录密码）")
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
    sync_schedule()
    return public_config()


def delete_config() -> dict:
    """连同发送记录一起清：旧配置的记录留着只会误导（「已发送给 2 个收件人」说的是已经删掉的那份）。

    正在发送时不许清：那一轮手里拿着配置的快照（连同密码），清了照样把信发出去，结束时还会把发送记录写回来
    """
    if _state["running"]:
        raise NotifyBusy("正在发送，等这一轮结束再清除 —— 现在清，它照样会用这份配置把信发出去")
    storage.delete_app_config(CONFIG_PREFIX)
    storage.delete_app_config(STATUS_PREFIX)
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
    if not _begin():
        logger.warning("上一轮邮件还在发送，跳过这次定时发送")
        return
    await _run("schedule")


def start_send() -> bool:
    """「立即发送」：起后台作业，立刻返回。已有一轮在跑就返回 False。

    运行标记必须在这里**同步**置上（同 start_backfill_translation）：等协程被调度起来再置的话，连点两下会起出两轮
    """
    if not _begin():
        return False
    job = asyncio.create_task(_run("manual"))
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
        "last": json.loads(raw["last"]) if raw.get("last") else None,
        "next_run": _next_run(scheduler if scheduler is not None else _global_scheduler()),
    }


async def _run(trigger: str) -> dict:
    """跑一轮并记下结果。后台作业和定时回调的异常没人接，这里一律变成一条 failed 记录"""
    started = datetime.now()
    try:
        result = await _send_digest(started)
    except Exception as e:
        logger.exception("邮件通知发送失败")
        result = {"outcome": "failed", "message": str(e)}
    record = {
        "trigger": trigger,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        **result,
    }
    try:
        storage.set_app_config(STATUS_PREFIX, {"last": json.dumps(record, ensure_ascii=False)})
    finally:
        _state.update(running=None, progress="")
    logger.info("邮件通知（%s）：%s %s", trigger, record["outcome"], record.get("message", ""))
    return record


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

    sections = collect_fresh(sources, days, now)
    total = _count(sections)
    if not total:
        return {"outcome": "skipped", "message": f"近 {days} 天没有新回复，这次没有发送邮件{note}"}

    failed, roots_failed, reason = await _backfill(sections)
    if failed:
        return {
            "outcome": "failed", "reply_count": total,
            "message": f"有 {failed} 条新回复翻译失败（已重试 {TRANSLATE_RETRIES} 次），这次没有发送邮件。"
                       f"原因：{reason}。到「LLM 配置」页点「测试连接」检查后，再回来点「立即发送」",
        }
    if roots_failed:
        note = f"（另有 {roots_failed} 条主贴没翻成，邮件里只放了原文）{note}"

    _state["progress"] = "正在发送邮件"
    msg = build_message(cfg, sections, days, now)
    await asyncio.to_thread(deliver, cfg, msg)
    return {
        "outcome": "sent", "reply_count": total, "subject": str(msg["Subject"]),
        "attachment": total > BODY_LIMIT,
        "message": f"已发送给 {len(cfg['recipients'])} 个收件人，共 {total} 条新回复{note}",
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


def collect_fresh(sources: List[dict], days: int, now: datetime) -> List[dict]:
    """每个来源一节：{source, collector_name, threads: [{root, replies: [{post, reply_to}]}]}。

    判据就是结果页「只看新回复」那一个（mark_fresh_replies），基准换成 now（换到来源时间戳的时区）。主贴按发表时间从新到旧
    （同结果页和导出），串里的新回复按发表先后。主贴只作上下文 —— 回复脱离主贴读不懂，但主贴本身不算新回复
    """
    sections = []
    for source in sources:
        sid = source["id"]
        collector = _collector(source)
        posts = storage.load_posts([sid])
        flat = {sid} if collector and collector.thread_kind == "thread" else set()
        fresh = mark_fresh_replies(posts, days, flat, base=_source_now(collector, now))
        starts = flat_thread_starts(posts, flat)
        threads = thread_of(posts)
        by_key = {post_key(p): p for p in posts}
        groups: Dict[str, dict] = {}
        for p in posts:
            if post_key(p) not in fresh:
                continue
            # 有父指针的走讨论串（串首就是顶层主贴）；Tweakers 平铺的楼层挂在主题楼层下
            root = threads[post_key(p)][0] if p.get("parent_fingerprint") else starts[sid]
            parent = by_key.get(parent_key(p))
            # 第 2 层起写明回的是谁（同结果页）；直接回主贴的不用写
            reply_to = parent["username"] if parent and post_key(parent) != post_key(root) else ""
            groups.setdefault(post_key(root), {"root": root, "replies": []})["replies"].append(
                {"post": p, "reply_to": reply_to or ""})
        ordered = sorted(groups.values(), key=lambda g: sort_time(g["root"].get("timestamp")), reverse=True)
        for g in ordered:
            g["replies"].sort(key=lambda r: parse_time(r["post"].get("timestamp")))
        sections.append({"source": source, "threads": ordered,
                         "collector_name": collector.display_name if collector else source.get("collector_id", "")})
    return sections


def _count(sections: List[dict]) -> int:
    return sum(len(t["replies"]) for s in sections for t in s["threads"])


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


async def _backfill(sections: List[dict]) -> Tuple[int, int, str]:
    """要显示的帖子缺译文的先补，返回 (没翻成的新回复条数, 没翻成的主贴条数, 原因)。

    新回复和它们的主贴一起翻，但**只有新回复翻不成才拦住邮件**：主贴只是上下文，一条总被拒译的老主贴不能让
    每天的邮件都发不出去（评审发现），它在邮件里只放原文。翻完还有失败的就整轮再翻，最多再翻 TRANSLATE_RETRIES 轮
    （用户定的）。译者每轮已经对失败条目单条重译过一次，这里的重试是隔一轮再来：模型临时抽风、限流退避用完这类情况，
    下一轮往往就过了
    """
    replies = {post_key(r["post"]): r["post"] for s in sections for t in s["threads"] for r in t["replies"]}
    roots = {post_key(t["root"]): t["root"] for s in sections for t in s["threads"]}
    pending = [p for p in {**roots, **replies}.values() if needs_translation(p)]
    if not pending:
        return 0, 0, ""
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
        failed_replies = sum(1 for p in replies.values() if needs_translation(p))
        failed_roots = sum(1 for k, p in roots.items() if k not in replies and needs_translation(p))
        return failed_replies, failed_roots, (warnings[0] if warnings else "模型返回的都是空内容")
    finally:
        orchestrator.release_translation(source_ids, CLAIM_OWNER)


# ===== 组邮件 =====

_FONT = "font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif"
_META = "color:#6b7280;font-size:12px"
_ORIG = "margin:6px 0 0;color:#6b7280;font-size:13px;line-height:1.6"
_ZH = "margin:4px 0 0;color:#111827;font-size:14px;line-height:1.6"
_CARD = "border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px;margin:0 0 12px"
_REPLY = "margin:12px 0 0 14px;padding:2px 0 2px 10px;border-left:3px solid #f59e0b"
_BADGE = "display:inline-block;padding:0 6px;border-radius:4px;background:#eef2ff;color:#4338ca;font-size:12px"


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
    head = (f'<div><span style="{_BADGE}">主贴</span> <b>{_esc(root.get("username"))}</b> '
            f'<span style="{_META}">{_esc(_time(root))}</span></div>')
    replies = []
    for r in thread["replies"]:
        p = r["post"]
        to = f' <span style="{_META}">回复 {_esc(r["reply_to"])}</span>' if r["reply_to"] else ""
        replies.append(
            f'<div class="reply" style="{_REPLY}"><div>↳ <b>{_esc(p.get("username"))}</b>{to} '
            f'<span style="{_META}">{_esc(_time(p))}</span></div>{_body_html(p)}</div>')
    return f'<div class="thread" style="{_CARD}">{head}{_body_html(root, ROOT_CLIP)}{"".join(replies)}</div>'


def _section_html(section: dict, days: int) -> str:
    shown = sum(len(t["replies"]) for t in section["threads"])
    total = section.get("total", shown)
    title = f'来自：{_esc(section["collector_name"])}「{_esc(section["source"].get("name"))}」'
    count = f"{total} 条新回复" + (f"（正文列最新 {shown} 条，其余见附件）" if shown < total else "")
    if section["threads"]:
        body = "".join(_thread_html(t) for t in section["threads"])
    elif total:
        body = f'<p style="{_META}">这 {total} 条比正文里的都早，见附件</p>'
    else:
        body = f'<p style="{_META}">近 {days} 天没有新回复</p>'
    return (f'<h3 style="margin:20px 0 8px;font-size:15px">{title}'
            f'<span style="{_META};font-weight:normal"> · {count}</span></h3>{body}')


def render_html(sections: List[dict], days: int, now: datetime, title: str, total: int, note: str = "") -> str:
    """一份完整的 HTML 文档：邮件正文和附件都用它（附件要能单独用浏览器打开）。样式全部内联 —— 邮件客户端常把 <style> 剥掉"""
    start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    end = now.strftime("%Y-%m-%d %H:%M")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        f'<title>{_esc(title)}</title></head>'
        '<body style="margin:0;padding:16px;background:#ffffff">'
        f'<div style="{_FONT};max-width:760px;margin:0 auto;color:#111827;font-size:14px">'
        '<h2 style="margin:0 0 4px;font-size:18px">HYXi 新回复摘要</h2>'
        f'<p style="{_META};margin:0 0 12px">近 {days} 天（{start} ～ {end}）· 共 {total} 条新回复 · 只含已采集到的数据</p>'
        f'{note}{"".join(_section_html(s, days) for s in sections)}'
        f'<p style="{_META};margin-top:20px">由 HYXi 舆情分析平台发送 · 生成于 {end}</p>'
        '</div></body></html>'
    )


def _newest(sections: List[dict], limit: int) -> List[dict]:
    """只留最新的 limit 条新回复。来源、串的结构不变，空了的串去掉；每节记下原来的条数，标题照实说"""
    stamped = [(parse_time(r["post"].get("timestamp")), id(r))
               for s in sections for t in s["threads"] for r in t["replies"]]
    keep = {rid for _, rid in sorted(stamped, key=lambda x: x[0], reverse=True)[:limit]}
    out = []
    for s in sections:
        threads = [dict(t, replies=[r for r in t["replies"] if id(r) in keep]) for t in s["threads"]]
        out.append(dict(s, threads=[t for t in threads if t["replies"]],
                        total=sum(len(t["replies"]) for t in s["threads"])))
    return out


def subject_for(sections: List[dict], days: int, total: int) -> str:
    """只点有新回复的来源：选了却没有新回复的只在正文里说一声 —— 拿它打头的标题会让人以为新回复是从那来的
    （真实数据实测过：Tweakers 来源一条帖子都没有，标题却是「Tweakers…等 2 个数据源」）"""
    active = [s for s in sections if s["threads"]] or sections
    first = active[0]
    label = f'{first["collector_name"]}「{first["source"].get("name")}」'
    more = f"等 {len(active)} 个数据源" if len(active) > 1 else ""
    return f"【HYXi 新回复】{label}{more} · 近 {days} 天 · 共 {total} 条"


def _new_message(cfg: dict, subject: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Address(display_name="HYXi 舆情分析", addr_spec=cfg["sender"])
    msg["To"] = ", ".join(cfg["recipients"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["sender"].rsplit("@", 1)[-1])
    return msg


def build_message(cfg: dict, sections: List[dict], days: int, now: datetime) -> EmailMessage:
    total = _count(sections)
    subject = subject_for(sections, days, total)
    msg = _new_message(cfg, subject)
    attach = total > BODY_LIMIT
    filename = f"hyxi-new-replies-{now:%Y%m%d-%H%M}.html"
    body_sections, note = sections, ""
    if attach:
        body_sections = _newest(sections, BODY_LIMIT)
        note = (f'<p style="margin:0 0 12px;padding:8px 10px;background:#fffbeb;border:1px solid #fde68a;'
                f'border-radius:6px">新回复共 {total} 条，正文只列最新的 {BODY_LIMIT} 条；'
                f'完整的 {total} 条见附件「{filename}」（用浏览器打开）。</p>')
    msg.set_content(f"{subject}\n\n本邮件是 HTML 格式，请用支持 HTML 的邮件客户端查看"
                    + (f"；完整内容见附件 {filename}" if attach else "") + "。\n")
    msg.add_alternative(render_html(body_sections, days, now, subject, total, note), subtype="html")
    if attach:
        msg.add_attachment(render_html(sections, days, now, subject, total), subtype="html", filename=filename)
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
                                      "请勾选「使用 SSL」并填 SSL 端口（QQ 邮箱是 465）")
            server.login(cfg["username"], password)
            server.send_message(msg)
    except NotifyError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        raise NotifyError(f"SMTP 登录被拒绝（{e.smtp_code}）：用户名或密码不对。QQ、163 等邮箱这里要填「授权码」"
                          "而不是登录密码 —— 在邮箱网页版的设置里开启 SMTP 服务后生成") from None
    except smtplib.SMTPSenderRefused as e:
        raise NotifyError(f"发件人 {cfg['sender']} 被服务器拒绝（{e.smtp_code}）。"
                          "QQ 等邮箱要求发件人邮箱和登录用户名是同一个") from None
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
