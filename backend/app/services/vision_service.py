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

# 输出 token 预算。**必须给得足够宽**：kimi-for-coding 这类推理模型会先吐一大段
# reasoning_content，而那部分**照样计入 max_tokens**。实测给 512 时 512 个全被推理
# 吃掉，finish_reason=length、content 是空串 —— 于是每张图都「理解成功但没有描述」，
# 界面上完全看不出异常。实测推理约 300~700 token，2048 留足余量。
MAX_OUTPUT_TOKENS = 2048

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

    # 空描述重试一次。推理模型偶尔陷进长推理，把整个 max_tokens 烧在 reasoning 上，
    # 返回 HTTP 200 + finish_reason=length + 空 content。**调大预算解决不了** ——
    # 真机实测 4096 照样被烧穿（4096 全进推理），只是每次跑飞多浪费一倍 token；
    # 而重试立刻就好（同一张图另外几次只用 300~600 token）。实测 23 张里 2~4 张会中。
    for attempt in (1, 2):
        try:
            # **不要传 temperature**：真机实测 kimi-for-coding 只接受 temperature=1，
            # 传 0.2 会被整个请求打回 400 `invalid temperature: only 1 is allowed for
            # this model`，再被下面的降级逻辑吞成「这条没有描述」—— 功能看着在跑，实际
            # 一张图都没理解。各家视觉模型对这个参数的约束不一样，交给服务端默认值最稳。
            # 描述任务本身受 prompt 强约束，不依赖低温度。
            text = await vision.chat(
                [
                    {"role": "system", "content": VISION_SYSTEM_PROMPT},
                    {"role": "user", "content": parts},
                ],
                max_tokens=MAX_OUTPUT_TOKENS,
            )
        except Exception as e:
            logger.warning("图片理解失败（已降级为纯文本分析）: %s", str(e)[:120])
            return ""

        desc = (text or "").strip()
        if desc:
            return desc
        # 单独报出来：HTTP 200 + 空 content 是推理模型把 token 预算烧光的典型症状，
        # 混进「没有描述」里会让人以为是模型看不懂图，从而查错方向
        logger.warning(
            "多模态模型返回了空描述（HTTP 正常，第 %d 次）。推理模型多半是 max_tokens "
            "被 reasoning 吃光，当前预算 %d", attempt, MAX_OUTPUT_TOKENS,
        )
    return ""
