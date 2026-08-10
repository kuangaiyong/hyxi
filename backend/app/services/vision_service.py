"""图片理解服务 —— 把帖子配图交给多模态模型，转成可参与舆情判断的中文描述。

**只服务于舆情分析**。翻译不走这条路：译文对着的是正文，把图塞进去只会污染译文，
而且要为每条带图的帖子多付一次多模态调用的钱。

角色沿用翻译那份（INDUSTRY_ROLE + INDUSTRY_GLOSSARY）—— 看懂储能设备照片、
App 截图、配电箱接线图，靠的正是那张行业术语表。
"""

import base64
import logging
import mimetypes
import os
from typing import Optional

from app.config import settings
from app.services.llm_service import LLMService
from app.services.translator_service import INDUSTRY_GLOSSARY, INDUSTRY_ROLE

logger = logging.getLogger("hyxi.vision")

# 单张图的字节上限。超过就跳过 —— base64 会再膨胀 1/3，一张 10MB 的图能把
# 单次请求顶到几百万 token，既贵又必然超上下文
MAX_IMAGE_BYTES = 6 * 1024 * 1024

# 一条帖子最多送几张图。实测最多的一条带 3 张，这个上限是给异常数据兜底的
MAX_IMAGES_PER_POST = 6

VISION_SYSTEM_PROMPT = f"""{INDUSTRY_ROLE}
你的任务是看懂论坛与社交媒体帖子里的配图，并用中文客观描述图片内容，供后续舆情分析使用。

1. **行业术语准确**：
{INDUSTRY_GLOSSARY}

2. **重点关注**（有则必须写出来，这些直接决定舆情判断）：
   - 设备型号、品牌标识、铭牌参数
   - App / 监控界面上的数值、曲线走势、报错码与报错文案
   - 安装现场的接线、走线、固定方式是否规范，有无明显隐患
   - 损坏、发热变色、渗液、锈蚀等异常迹象
   - 截图里的聊天内容、账单金额、客服回复

3. **只描述你真正看到的**。看不清就说看不清，**不要推测**，更不要替用户下判断
   （不要写「用户对此不满」这类结论，那是下一步的事）

4. 直接输出一段 100 字以内的中文描述，不要分点，不要加任何前缀或解释"""


def _media_path(rel_path: str) -> Optional[str]:
    """把 images 里的相对路径解析成 media 目录内的绝对路径。

    包含性校验照抄 main.py 的 get_media：图片路径来自采集脚本，理论上可信，
    但这道校验只要几行，而 media 目录之外就是数据库和明文密钥。
    """
    root = os.path.realpath(os.path.join(settings.data_dir, "media"))
    target = os.path.realpath(os.path.join(root, rel_path))
    if target != root and not target.startswith(root + os.sep):
        logger.warning("图片路径越界，已忽略: %r", rel_path)
        return None
    if not os.path.isfile(target):
        return None
    return target


def _data_uri(abs_path: str) -> Optional[str]:
    """读成 data URI。任何读取问题都返回 None，由调用方跳过这一张。"""
    try:
        size = os.path.getsize(abs_path)
        if size > MAX_IMAGE_BYTES:
            logger.warning("图片过大已跳过 (%d 字节): %s", size, abs_path)
            return None
        with open(abs_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("图片读取失败 %s: %s", abs_path, e)
        return None
    mime = mimetypes.guess_type(abs_path)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


async def describe_post_images(post: dict, vision: Optional[LLMService]) -> str:
    """把一条帖子的配图理解成一段中文描述。取不到就返回空串。

    **绝不抛异常**：模型没配、配额用尽、图片丢了、网络断了 —— 一律降级成
    「这条帖子没有图片信息」，舆情分析照常按正文进行。
    """
    if vision is None:
        return ""

    images = post.get("images") or []
    if not images:
        return ""

    parts = []
    for rel in images[:MAX_IMAGES_PER_POST]:
        abs_path = _media_path(rel)
        if not abs_path:
            logger.info("图片不存在，跳过: %r", rel)
            continue
        uri = _data_uri(abs_path)
        if uri:
            parts.append({"type": "image_url", "image_url": {"url": uri}})

    if not parts:
        return ""

    hint = post.get("content") or ""
    parts.append({
        "type": "text",
        "text": (
            "请描述以上图片的内容。"
            + (f"\n帖子正文（供理解语境，不要翻译它）：{hint[:300]}" if hint.strip() else "")
        ),
    })

    try:
        text = await vision.chat(
            [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user", "content": parts},
            ],
            temperature=0.2,
            max_tokens=512,
        )
    except Exception as e:
        logger.warning("图片理解失败（已降级为纯文本分析）: %s", str(e)[:120])
        return ""

    return (text or "").strip()
