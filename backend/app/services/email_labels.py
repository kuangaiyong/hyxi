"""邮件帖子类型分类 —— 只服务于邮件正文/附件，不写数据库、不改舆情结论。

为什么要单独一套：现有舆情结论是「正面/负面/中立 + 强度 + 产品维度」，不是
「问题反馈 / 咨询 / 正常交流」的意图分类；而且任务可以没跑舆情、新帖子也可能没有结论。
这里用同一条 LLM 配置做一次轻量分类，只给邮件用；失败只降级成「未分类」，不拦发信。
"""

import json
import logging
import re
from typing import Dict, List, Tuple

from app.services.llm_utils import get_llm_service

logger = logging.getLogger("hyxi.email_labels")

# 封闭集合。模型返回别的值一律按 unknown 处理，避免自由标签碎成上百个近义词
LABELS = ("issue", "consultation", "discussion", "unknown")
LABEL_TEXT = {
    "issue": "🛠 问题反馈",
    "consultation": "❓ 咨询",
    "discussion": "💬 正常交流",
    "unknown": "未分类",
}

# 模型偶尔会回中文标签；归一到封闭集合，避免整批被判非法
_LABEL_ALIASES = {
    "issue": "issue", "问题反馈": "issue", "反馈问题": "issue", "故障": "issue",
    "consultation": "consultation", "咨询": "consultation", "询问": "consultation",
    "discussion": "discussion", "正常交流": "discussion", "交流": "discussion", "讨论": "discussion",
    "unknown": "unknown", "未分类": "unknown", "无法判断": "unknown", "未知": "unknown",
}

# 每批多少条。推理型模型的 reasoning 也计入 max_tokens，单批过大/输出预算过小会让
# content 变成空串 —— 2026-11-24 对 deepseek-v4-flash 实测：正文最长的 10 条一批、
# 1024 tokens 返回空；同样 10 条给 4096 才能返回全部标签。这里按翻译的批量大小取 5，
# 给 reasoning 留足空间。
LABEL_BATCH_SIZE = 5
MAX_OUTPUT_TOKENS = 4096
RETRY_MAX_TOKENS = 4096
MAX_TARGET_CHARS = 500
MAX_TRANSLATION_CHARS = 500
MAX_ROOT_CHARS = 300
MAX_IMAGE_DESC_CHARS = 200
TEMPERATURE = 0.2

SYSTEM_PROMPT = """你是 HYXi 邮件摘要的帖子类型分类器。只判断帖子作者的主要意图，不判断情感正负。

对每条帖子只输出一个标签：
- issue：问题反馈。报告故障、异常、缺陷、损坏、安装/售后问题，或明确投诉、要求解决；主要目的是反映问题。
- consultation：咨询。询问产品功能、使用方法、安装配置、兼容性、购买、价格、售后流程等，期待得到答案；通常没有在报告已经发生的故障。
- discussion：正常交流。经验分享、使用感受、观点、评测、对比、闲聊、附和等，不以反映问题或求答案为主要目的。
- unknown：无法判断。**只有** content、translation、image_desc 全部为空，或内容确实读不出来时才用；
  只要有任何可读内容，就必须在 issue / consultation / discussion 里三选一，拿不准时归 discussion（正常交流），不要轻易标 unknown。

边界：
- 抱怨/求助同时存在 -> issue；只是提问、没有明确故障 -> consultation；分享/评论 -> discussion。
- 「+1」「同问」「我也是」这类附和结合上下文判断，但标签判的是本条帖子，不要把主贴的类型直接当成回复的类型。
- 不要根据情感正负反推类型：负面不一定是问题反馈，中性不一定是咨询。
- 输入里的 root 只是讨论串主贴上下文，判的是本条帖子的 content/translation/image_desc。
- 短回复（谢谢、同问、已回、表情等）只要能读出交流意图，就归 discussion，不要标 unknown。

返回严格 JSON 数组，每项 {"id": "输入里的 id", "label": "issue|consultation|discussion|unknown"}。
不要输出解释、Markdown 代码块或任何多余文字。"""


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text[:limit] + "…" if len(text) > limit else text


def _payload(item: Dict) -> Dict:
    post = item.get("post") or {}
    return {
        "id": item.get("id") or "",
        "role": item.get("role") or "",
        "root": _clip(item.get("root") or "", MAX_ROOT_CHARS),
        "content": _clip(post.get("content") or "", MAX_TARGET_CHARS),
        "translation": _clip(item.get("translation") or "", MAX_TRANSLATION_CHARS),
        "image_desc": _clip(post.get("image_desc") or "", MAX_IMAGE_DESC_CHARS),
    }


def _normalize_label(raw) -> str:
    text = str(raw or "").strip().lower()
    return _LABEL_ALIASES.get(text, text)


def _parse_labels(text: str, ids: set) -> Dict[str, str]:
    """从模型输出里取 {id: label}。格式不对、缺 id、未知 label 都只返回能确认的部分。"""
    text = (text or "").strip()
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return {}
    try:
        rows = json.loads(match.group(0))
    except (TypeError, ValueError):
        return {}
    if not isinstance(rows, list):
        return {}
    out: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get("id")
        label = _normalize_label(row.get("label"))
        if item_id in ids and label in LABELS:
            out[item_id] = label
    return out


async def _request(
    llm, items: List[Dict], max_tokens: int, label: str
) -> Dict[str, str]:
    """请求一批（或单条）标签。空返回/解析失败只记日志并返回空字典，不抛异常。"""
    ids = {item["id"] for item in items}
    payload = [_payload(item) for item in items]
    try:
        text = await llm.chat_with_retry(
            SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            temperature=TEMPERATURE,
            max_tokens=max_tokens,
            label=label,
        )
    except Exception as e:
        logger.warning("邮件帖子类型分类失败（%s，%d 条，保持未分类）: %s",
                       label, len(items), str(e)[:120])
        return {}
    parsed = _parse_labels(text, ids)
    if len(parsed) < len(ids):
        logger.warning("邮件帖子类型分类返回不完整（%s，max_tokens=%d，需要 %d 条，解析出 %d 条，"
                       "原文长度 %d）: %r", label, max_tokens, len(ids), len(parsed),
                       len(text or ""), (text or "")[:200])
    return parsed


async def classify_posts(items: List[Dict]) -> Tuple[Dict[str, str], str]:
    """给邮件里所有帖子打类型标签。

    items 每项：{id, role, root, post, translation}。返回 (labels, labels_note)。
    任何失败都降级成 unknown，绝不抛异常拦住邮件；labels_note 给邮件顶部说明。

    推理型模型（如 deepseek-v4-flash）的 reasoning 也计入 max_tokens：批量太小/预算不够时
    content 会直接为空。所以用 5 条/批 + 4096 输出预算，并对缺失的条目做单条重试。
    """
    unique = {}
    for item in items or []:
        item_id = item.get("id")
        if item_id:
            unique[item_id] = item
    labels = {item_id: "unknown" for item_id in unique}
    if not labels:
        return labels, ""

    llm = None
    try:
        llm = get_llm_service()
    except Exception as e:
        logger.warning("加载邮件帖子类型分类模型失败: %s", str(e)[:120])
    if llm is None:
        logger.info("未配置 LLM，邮件帖子类型全部为未分类")
        return labels, "帖子类型标签：未配置 LLM，本次全部标为「未分类」。"

    ids = list(unique)
    failed = set()
    explicit_unknown = set()
    try:
        for start in range(0, len(ids), LABEL_BATCH_SIZE):
            batch_ids = ids[start:start + LABEL_BATCH_SIZE]
            parsed = await _request(
                llm, [unique[i] for i in batch_ids], MAX_OUTPUT_TOKENS,
                f"邮件帖子类型 [{start + 1}-{start + len(batch_ids)}]",
            )
            missing = [item_id for item_id in batch_ids if item_id not in parsed]
            # 推理型模型可能整批返回空，或只返回一部分；缺的逐条再问一次
            for item_id in missing:
                one = await _request(
                    llm, [unique[item_id]], RETRY_MAX_TOKENS,
                    f"邮件帖子类型单条重试 {item_id}",
                )
                if item_id in one:
                    parsed[item_id] = one[item_id]
            for item_id in batch_ids:
                label = parsed.get(item_id)
                if label in LABELS:
                    labels[item_id] = label
                    if label == "unknown":
                        explicit_unknown.add(item_id)
                else:
                    failed.add(item_id)
    finally:
        try:
            await llm.close()
        except Exception:
            logger.debug("关闭邮件帖子类型分类模型连接失败", exc_info=True)

    unknown = sum(1 for label in labels.values() if label == "unknown")
    logger.info("邮件帖子类型：%d 条，%d 条未分类（%d 条模型未返回，%d 条内容不足）",
                len(labels), unknown, len(failed), len(explicit_unknown))
    if unknown:
        reasons = []
        if failed:
            reasons.append(f"{len(failed)} 条模型未返回有效结果")
        if explicit_unknown:
            reasons.append(f"{len(explicit_unknown)} 条内容不足")
        note = f"帖子类型标签：{'、'.join(reasons)}，已标「未分类」。"
    else:
        note = ""
    return labels, note
