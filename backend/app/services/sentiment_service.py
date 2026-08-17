"""舆情分析服务 - 使用 LLM 分析多来源帖子的情感倾向"""

import json
import re
import asyncio
import logging
from typing import List, Optional
from app.services.post_tree import post_key
from app.services.progress_manager import ProgressManager
from app.services.llm_service import LLMService
from app.services.llm_utils import get_llm_service, get_vision_service
from app.services.storage import (
    save_sentiment as db_save_sentiment,
    mark_sentiment_analyzed as db_mark_sentiment_analyzed,
    save_image_descs as db_save_image_descs,
)
from app.services.vision_service import describe_post_images
from app.models import LLMConfig

logger = logging.getLogger("hyxi.sentiment")

BATCH_SIZE = 3  # 详细分析每次发送的帖子数

# 单条帖子的讨论串上下文字符上限。实测最长的一串才 1069 字符，平时够不着 ——
# 这是给信息流类来源（一个主贴挂几十条评论）留的保险，防止 prompt 无上限膨胀
THREAD_CONTEXT_LIMIT = 3000

# 纯图帖在 prompt 里的正文占位。**不能留空**：一片空白看起来像一条被截断的帖子，
# 模型会照着「没说什么」去判 neutral，而信息其实全在紧随其后的 [图片: ...] 里
NO_TEXT_PLACEHOLDER = "（本帖没有文字，内容全在配图上）"

# 正文空、图也读不出来的那一类：`drop_empty_posts()` 因为它下面有人发言才保留了它
# （多半是正文提取失败）、历史迁移帖，以及有图但描述没到手的（图丢了 / 多模态没配 /
# 调用失败）。**不能套上面那句** —— 说「内容全在配图上」是假话，后面还没有
# [图片: ...] 跟着，等于诱导模型去读一张不存在的图
UNREADABLE_PLACEHOLDER = "（本帖正文为空或未能提取）"


def _body_or_placeholder(post: dict, text: str) -> str:
    """正文为空时的占位。**不能留空**：一行光秃秃的「主贴 @某人:」看着像渲染坏了，
    模型会当它什么都没说，而它恰恰是这一整串讨论的由头。

    判据是**描述有没有真的到手**，不是这条帖子有没有配图 —— `[图片: ...]` 那行只在
    `image_desc` 非空时才跟在后面。按 `images` 判的话，图丢了 / 多模态没配 / 调用失败
    的那条会说出「内容全在配图上」却没有任何图跟着，正是这句占位要避免的那种假话。
    """
    if text:
        return text
    return NO_TEXT_PLACEHOLDER if (post.get("image_desc") or "").strip() else UNREADABLE_PLACEHOLDER


def is_analyzable(post: dict) -> bool:
    """这条帖子能不能送去判情感。

    **正文为空但有配图的照样算**：图会先被多模态模型转成中文描述，而那正是最该看的
    一类内容 —— 实测那条真实数据正文一个字都没有，图上是 HYXi 安装检查报告
    （总分 88、发电异常 8/20 标橙）。只看正文的过滤器一刀切掉的恰恰是信息量最大的帖子。

    这里只能看 `images` 不能看 `image_desc`：调用方（入库口、流水线、页面按钮）
    要在图片理解**之前**就判断该不该把这条排进队列。理解完之后还拿不到描述的，
    由 analyze() 内部再滤一次。
    """
    return bool((post.get("content") or "").strip()) or bool(post.get("images"))


# 维度必须是封闭集合。放任 LLM 自由生成会把 top_dimensions 碎成上百个近义标签，
# 跨来源对比直接失效 —— 维度表的全部价值就在于它是封闭的。
DEFAULT_DIMENSIONS = [
    "价格/性价比",
    "产品质量/可靠性",
    "安装/配置体验",
    "App/软件体验",
    "客服/售后支持",
    "WiFi/连接问题",
    "固件更新",
    "温度/散热",
    "P1电表/智能控制",
    "认证/合规(如Synergrid)",
    "扩展/兼容性",
    "与其他品牌对比(如AEG/Marstek)",
    "性能/效率",
    "安全性",
]

SENTIMENT_SYSTEM_PROMPT = f"""你是一位精通多语种和中文的市场舆情分析专家。你的任务是分析论坛与社交媒体帖子对产品 HYXi Halo 家用储能电池的情感倾向。原文可能是荷兰语或英语。

请对每一条帖子输出以下 JSON：

{{
  "sentiment": "positive|negative|neutral",
  "intensity": 1-5,
  "reason_cn": "一句话中文分析理由",
  "dimensions": ["相关维度标签"]
}}

分析标准：

**情感判断(sentiment)**：
- positive：用户对产品有正面评价、满意体验、推荐意愿、成功解决问题
- negative：用户遇到问题、失望、批评、投诉产品缺陷
- neutral：纯技术咨询、信息分享、客观描述无明显情感倾向

**强度(intensity)**：
- 1：轻微倾向，带有中性色彩
- 2：稍有倾向，语气温和
- 3：明显倾向，有清晰的态度
- 4：强烈倾向，带有情绪色彩
- 5：极度强烈，明显的赞美或愤怒/失望

**维度(dimensions)**，只能从以下列表中选择（可多选），不得自创新标签：
{chr(10).join('- ' + d for d in DEFAULT_DIMENSIONS)}

**特别说明**：
- 如果帖子提到问题但后续解决了，要看整体语气：顺利解决→positive，抱怨解决过程→negative
- 如果是纯技术讨论/提问，即使涉及问题也应判断为neutral
- 与其他产品的对比：如果偏向Hyxi→positive，如果偏向竞品→negative
- 每条帖子前会标注 [来源: xxx]。不同平台的表达语气基线不同（论坛偏技术克制，
  社交媒体偏情绪化），评分请按**该平台内部的相对水平**判断，不要因为平台不同就
  预设某个平台更正面或更负面
- 帖子会附带它所在讨论串的上下文（主贴 + 该串的回复）。其中带 ▶ 标记、注明
  「← 本条为待分析对象」的那一条**才是你要判定的对象**，其余仅供理解语境。
  「+1」「同上」这类附和要结合上下文继承相应的情感倾向，而不是一律判 neutral；
  但**绝不要**把上下文里别人的态度直接当成待分析对象自己的态度
- [图片: ...] 是多模态模型对该帖配图的客观描述，请与正文同等参考 —— 有些帖子正文
  很短，真正的信息（报错码、安装现场、账单金额）都在图上
- 有的帖子**完全没有文字**（正文位置写着「本帖没有文字，内容全在配图上」），判断就
  全靠图片描述加讨论串上下文。这类帖子照常给出结论，**不要**因为没有正文就一律判
  neutral —— 一张写着报错码或异常读数的截图本身就是明确的表态
- 上下文里若出现「本帖正文为空或未能提取」，说明那条的内容我们没取到，**不是**它什么
  都没说。不要据此推断它的态度，按它下面的回复去理解这一串在讨论什么

输入是多条帖子，请为每条帖子输出JSON，用 '---SENTIMENT_SEPARATOR---' 分隔每条结果。
直接输出JSON对象序列，不要编号，不要额外解释。"""


class SentimentService:
    """LLM 舆情分析"""

    @staticmethod
    def _to_absolute(
        results: list,
        pending: list,
        existing_results: list,
        fp_to_idx: dict,
        total_all: int,
    ) -> list:
        """把「待分析批次内的下标」映射回全量帖子数组的绝对下标。

        键用 source:fingerprint —— 只用 fingerprint 会跨来源碰撞，把 A 平台的结论
        盖到 B 平台的帖子上。

        **开关只能是 fp_to_idx，不能是 existing_results**：本任务第一次跑舆情时
        existing_results 必然是空的，而 pending 完全可能只是全量的一小撮 ——
        `_processed.sentiment_at` 写在**源文件**里、跨任务共享，同一批数据被别的
        任务分析过之后，新任务的 pending 只剩新帖。那种情况下不重映射，存下去的
        就是一个批次内下标的数组被当成绝对下标用：结论整体贴到别人身上，页面和
        导出都看不出异常。实测任务 7d24786f 就是这么坏的 —— 90 条帖子只存下 3 条
        结果，首条显示「未分析」，另两条挂在了完全无关的帖子上。
        """
        if not fp_to_idx:
            return results
        full = list(existing_results or [])
        while len(full) < total_all:
            full.append(None)
        for local_idx, post in enumerate(pending):
            r = results[local_idx]
            if not (r and r.get("sentiment")):
                continue
            abs_idx = fp_to_idx.get(post_key(post))
            if abs_idx is not None and abs_idx < len(full):
                full[abs_idx] = r
        return full

    @staticmethod
    async def _understand_images(task_id: str, targets: list, progress: ProgressManager) -> int:
        """把带图帖子的配图交给多模态模型理解，结果写进 post["image_desc"] 并落库。

        **整段是尽力而为**：模型没配就直接跳过，单张失败也只是这条没有描述 ——
        舆情分析照常按正文进行。为一张图让整轮分析失败是不可接受的。
        """
        if not targets:
            return 0
        vision = get_vision_service()
        if vision is None:
            await progress.emit(task_id, "log", {
                "level": "info",
                "message": f"{len(targets)} 条帖子带图，但未配置多模态模型，本轮按纯文本分析",
            })
            return 0

        await progress.emit(task_id, "log", {
            "level": "info",
            "message": f"开始理解 {len(targets)} 条帖子的配图（模型 {vision.config.model_name}）",
        })
        done = 0
        try:
            for post in targets:
                desc = await describe_post_images(post, vision)
                if desc:
                    post["image_desc"] = desc
                    done += 1
                await asyncio.sleep(0.5)
        finally:
            await vision.close()

        try:
            db_save_image_descs(targets)
        except Exception as e:
            # 没落上只是下一轮要重新理解一次，比让整轮分析失败轻得多
            logger.warning("回写 image_desc 失败: %s", e)

        await progress.emit(task_id, "log", {
            "level": "info" if done == len(targets) else "warning",
            "message": f"图片理解完成: {done}/{len(targets)} 条成功",
        })
        return done

    @staticmethod
    async def analyze(
        task_id: str,
        posts: list,
        progress: ProgressManager,
        existing_results: list = None,
        fp_to_idx: dict = None,
        source_names: dict = None,
        thread_by_key: dict = None,
        all_posts: list = None,
        step_index: int = 0,
    ) -> dict:
        """对帖子进行舆情分析（增量：仅分析传入的帖子，合并已有结果）。

        step_index 是进度事件要挂到第几个流水线步骤上。舆情页那条路没有步骤概念，
        缺省 0 即可；进了流水线就必须给真实下标，否则进度条会去刷新早已完成的
        第一个步骤（照着 ExcelService 的 step_index 来）。
        """
        total = len(posts)
        # 先按「有正文 or 有图」圈出候选。**必须排在图片理解之前** —— 用正文过滤的话，
        # 纯图帖压根进不了下面的 need_desc，它的图永远不会被理解，于是永远分析不了
        candidates = [(i, p) for i, p in enumerate(posts) if is_analyzable(p)]
        pic_only = sum(1 for _i, p in candidates if not (p.get("content") or "").strip())

        await progress.emit(task_id, "log", {
            "level": "info",
            "message": (
                f"开始舆情分析，共 {total} 条帖子，{len(candidates)} 条可分析"
                + (f"（其中 {pic_only} 条只有图片）" if pic_only else "")
            ),
        })

        # 加载 LLM 配置（使用统一工具函数）
        llm = get_llm_service()
        if not llm:
            raise Exception("请先配置 LLM API")

        # ===== 图片先交给多模态模型理解 =====
        # 范围是「待分析帖子所在讨论串里的全部带图帖子」，不只是待分析的那几条 ——
        # 主贴的图正是它下面每条回复的上下文，漏掉就等于回复看不见图。
        # 已经有 image_desc 的跳过：那是上一轮花钱换来的，没必要再买一次。
        need_desc = {}
        for _i, p in candidates:
            for member in (thread_by_key or {}).get(post_key(p)) or [p]:
                if member.get("images") and not (member.get("image_desc") or "").strip():
                    need_desc[post_key(member)] = member
        await SentimentService._understand_images(task_id, list(need_desc.values()), progress)

        # 理解完才知道纯图帖到底拿没拿到描述。没拿到（模型没配 / 调用失败 / 图丢了）
        # 就是真的无从判断，剔出去按「未分析」处理 —— 送一个空块给 LLM 只会换回
        # 一条编出来的结论，那比诚实地留「未分析」糟得多
        non_empty = [
            (i, p) for i, p in candidates
            if (p.get("content") or "").strip() or (p.get("image_desc") or "").strip()
        ]
        total_non_empty = len(non_empty)
        if total_non_empty < len(candidates):
            await progress.emit(task_id, "log", {
                "level": "warning",
                "message": f"{len(candidates) - total_non_empty} 条纯图帖没能拿到图片描述，本轮跳过",
            })

        results = [None] * total
        success_count = 0
        fail_count = 0

        def _member_line(m: dict, is_target: bool) -> str:
            """讨论串里的一条。待分析那条给足正文，其余压到 600 字够表达立场即可。"""
            role = "回复" if m.get("parent_fingerprint") else "主贴"
            name = m.get("username") or "匿名"
            text = _body_or_placeholder(
                m, (m.get("content") or "")[:2000 if is_target else 600]
            )
            line = f"{'▶ ' if is_target else '  '}{role} @{name}: {text}"
            desc = (m.get("image_desc") or "").strip()
            if desc:
                line += f"\n    [图片: {desc}]"
            if is_target:
                line += "\n    ← 本条为待分析对象"
            return line

        def _thread_block(thread: list, target: dict) -> str:
            """整串上下文。超长时保留主贴与待分析条目，其余按距离由近及远地收。"""
            # 用 post_key 而不是 `is` 认人：待分析的那批与组串用的 all_posts 今天恰好是
            # 同一批对象，但只要有谁改成各自 load_posts() 一次，身份判断就会全部落空 ——
            # 那时 ▶ 标记会消失、下面的 next() 还会抛 StopIteration 把整轮分析带走
            tk = post_key(target)
            lines = [_member_line(m, post_key(m) == tk) for m in thread]
            target_idx = next((i for i, m in enumerate(thread) if post_key(m) == tk), 0)
            if sum(len(x) for x in lines) > THREAD_CONTEXT_LIMIT:
                keep = {0, target_idx}
                used = sum(len(lines[i]) for i in keep)
                for i in sorted(range(len(lines)), key=lambda i: abs(i - target_idx)):
                    if i in keep:
                        continue
                    if used + len(lines[i]) > THREAD_CONTEXT_LIMIT:
                        break
                    keep.add(i)
                    used += len(lines[i])
                trimmed, prev = [], -1
                for i in sorted(keep):
                    if i != prev + 1:
                        trimmed.append("  …（此处省略部分回复）…")
                    trimmed.append(lines[i])
                    prev = i
                lines = trimmed
            return "\n".join(lines)

        def _post_block(orig_idx: int, p: dict) -> str:
            """一条帖子在 prompt 里的样子。批量和单条重试必须用同一份 ——
            来源标签、讨论串上下文和图片描述都影响判定，重试时少给就成了另一道题。"""
            sid = p.get("source", "")
            label = (source_names or {}).get(sid, sid) or "未知来源"
            block = f"帖子{orig_idx + 1} [来源: {label}]:\n"

            # 回复大量是「+1」「same here」，单独看全是 neutral 噪音；只给父贴前 200 字
            # 又丢掉了同串其他人的信息。整串一起给（实测最长才 1069 字符，很便宜）
            thread = (thread_by_key or {}).get(post_key(p)) or [p]
            if len(thread) > 1:
                block += "[讨论串上下文]\n"
                block += _thread_block(thread, p) + "\n"
                block += "[/讨论串上下文]\n"
            else:
                # 孤立的主贴没有上下文可言，别为它套一层空壳，正文照旧原样给。
                # **不能走 _member_line(p, False)**：那条路按「上下文里的旁人」处理，
                # 只给 600 字 —— 而这条正是待分析对象，改造前给的是 2000 字。
                # 实测 65 个主贴里大多数没有回复，走的都是这一支
                content = _body_or_placeholder(p, (p.get("content") or "")[:2000])
                desc = (p.get("image_desc") or "").strip()
                block += content + (f"\n[图片: {desc}]" if desc else "") + "\n"
            return block + "\n"

        try:
            for batch_start in range(0, total_non_empty, BATCH_SIZE):
                batch = non_empty[batch_start:batch_start + BATCH_SIZE]

                # 构建批量分析请求
                user_message = "请分析以下帖子对HYXi Halo产品的情感倾向：\n\n"
                for orig_idx, p in batch:
                    user_message += _post_block(orig_idx, p)

                try:
                    result_text = await llm.chat_with_retry(
                        system_prompt=SENTIMENT_SYSTEM_PROMPT,
                        user_message=user_message,
                        temperature=0.2,
                        max_tokens=4096,
                        label=f"舆情分析 [{batch_start+1}-{min(batch_start+BATCH_SIZE, total_non_empty)}]",
                    )

                    # 解析批量结果
                    parts = result_text.split("---SENTIMENT_SEPARATOR---")

                    for j, (orig_idx, _p) in enumerate(batch):
                        if j < len(parts):
                            parsed = SentimentService._parse_sentiment(parts[j])
                            if parsed:
                                results[orig_idx] = parsed
                                success_count += 1
                            else:
                                results[orig_idx] = {
                                    "sentiment": None,
                                    "intensity": 0,
                                    "reason_cn": "解析失败",
                                    "dimensions": [],
                                }
                                fail_count += 1
                        else:
                            results[orig_idx] = {
                                "sentiment": "neutral",
                                "intensity": 1,
                                "reason_cn": "解析失败",
                                "dimensions": [],
                            }
                            fail_count += 1

                except Exception as e:
                    for orig_idx, _p in batch:
                        results[orig_idx] = {
                            "sentiment": None,
                            "intensity": 0,
                            "reason_cn": f"分析异常: {str(e)[:50]}",
                            "dimensions": [],
                        }
                        fail_count += 1

                # 更新进度
                done = batch_start + len(batch)
                pct = done / total_non_empty if total_non_empty > 0 else 1.0
                await progress.emit(task_id, "step_progress", {
                    "step": step_index,
                    "progress": min(pct, 0.99),
                    "message": f"舆情分析 {done}/{total_non_empty} 条 ({success_count} 成功, {fail_count} 失败)",
                })
                await progress.emit(task_id, "log", {
                    "level": "info",
                    "message": f"已分析 {done}/{total_non_empty} 条",
                })

                await asyncio.sleep(1.0)

            # ===== 重试解析失败的条目 =====
            # 批量输出靠 ---SENTIMENT_SEPARATOR--- 切分，LLM 偶尔少给一段或吐出非 JSON，
            # 那一条就此判死。逐条再问一次 —— 单条不必切分隔符，解析可靠得多。
            # 翻译早就是这么做的（见 translator_service 的单条重译），舆情漏了这一步。
            retry_targets = [
                (orig_idx, p) for orig_idx, p in non_empty
                if not (results[orig_idx] or {}).get("sentiment")
            ]
            if retry_targets:
                await progress.emit(task_id, "log", {
                    "level": "info",
                    "message": f"开始重试 {len(retry_targets)} 条解析失败的分析...",
                })
                for orig_idx, p in retry_targets:
                    try:
                        text = await llm.chat_with_retry(
                            system_prompt=SENTIMENT_SYSTEM_PROMPT,
                            user_message=(
                                "请分析以下帖子对HYXi Halo产品的情感倾向"
                                "（只有一条，直接输出一个JSON对象，不要分隔符）：\n\n"
                                + _post_block(orig_idx, p)
                            ),
                            temperature=0.2,
                            max_tokens=1024,
                            max_retries=2,
                            label=f"重析单条 #{orig_idx + 1}",
                        )
                        parsed = SentimentService._parse_sentiment(text)
                        if parsed and parsed.get("sentiment"):
                            results[orig_idx] = parsed
                            success_count += 1
                            fail_count -= 1
                    except Exception:
                        # 重试再失败就保留批量那轮写下的「解析失败」记录，不覆盖成别的
                        pass
                    await asyncio.sleep(1.0)

                still = sum(1 for i, _p in retry_targets
                            if not (results[i] or {}).get("sentiment"))
                await progress.emit(task_id, "log", {
                    "level": "info" if still == 0 else "warning",
                    "message": f"重试完成，{len(retry_targets) - still} 条成功，{still} 条仍失败",
                })

        finally:
            await llm.close()

        # 标记已分析的帖子的 _processed.sentiment_at
        now_iso = __import__('datetime').datetime.now().isoformat()
        for orig_idx, __p in non_empty:
            if results[orig_idx] and results[orig_idx].get("sentiment"):
                processed = posts[orig_idx].setdefault("_processed", {})
                if not processed.get("sentiment_at"):
                    processed["sentiment_at"] = now_iso

        # 把「待分析批次内的下标」映射回全量数组的绝对下标
        total_all = len(all_posts) if all_posts else len(fp_to_idx or {})
        results = SentimentService._to_absolute(
            results, posts, existing_results, fp_to_idx, total_all
        )

        # 全量总数：优先用 fp_to_idx 的长度（最准确），其次为已有结果+本次分析数
        if fp_to_idx:
            all_total = len(fp_to_idx)
        elif existing_results:
            all_total = len(existing_results) + total
        else:
            all_total = total
        # 按来源分组要拿全量帖子对齐绝对下标，增量模式下 posts 只是待分析的那批
        summary_posts = all_posts if all_posts and len(all_posts) == len(results) else None
        summary = SentimentService._build_summary(results, summary_posts, source_names)

        output = {
            "task_id": task_id,
            "analyzed_at": now_iso,
            "total": all_total,
            "success": sum(1 for r in results if r and r.get("sentiment")),
            "failed": sum(1 for r in results if not r or not r.get("sentiment")),
            "summary": summary,
            "results": results,
        }

        # 落库。results 的下标在这里还对得上 all_posts，存储层立刻把它换成
        # (source_id, fingerprint) —— 下标只在写入现场有意义
        db_save_sentiment(task_id, output, all_posts or posts)

        # 把 sentiment_at 落到 posts 表。只写这一个字段 —— 舆情分析不该碰
        # translation / translated，否则几百条已翻译的帖子下次要重新付费翻译
        try:
            db_mark_sentiment_analyzed(posts)
        except Exception as e:
            # 标记没落上只会让下一轮重复分析这几条，比让整轮结果丢失轻得多
            logger.warning("回写 sentiment_at 失败: %s", e)

        await progress.emit(task_id, "log", {
            "level": "success",
            "message": f"舆情分析完成: {output['success']} 成功, {output['failed']} 失败",
        })

        return output

    @staticmethod
    def _normalize_dimensions(dims) -> List[str]:
        """把 LLM 返回的维度对齐到封闭集合。

        实测模型会把带括号的标签简写成「认证/合规」，于是它和「认证/合规(如Synergrid)」
        在 top_dimensions 和 cross_source 里各占一行 —— 跨来源对比正是被这种近义碎片废掉的。
        对不上任何一个已知维度的直接丢弃：宁可少一个标签，也不能让维度表不再封闭。
        """
        out = []
        for raw in dims or []:
            if not isinstance(raw, str):
                continue
            name = raw.strip()
            if name in DEFAULT_DIMENSIONS:
                canonical = name
            else:
                canonical = next(
                    (d for d in DEFAULT_DIMENSIONS
                     if d.startswith(name) or name.startswith(d)),
                    None,
                )
                if canonical is None:
                    logger.debug("丢弃不在维度表里的标签: %r", name)
                    continue
            if canonical not in out:
                out.append(canonical)
        return out

    @staticmethod
    def _parse_sentiment(text: str) -> Optional[dict]:
        """解析 LLM 返回的单条情感分析 JSON"""
        text = text.strip()
        # 去掉可能的 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        # 尝试提取 JSON 对象
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                parsed["dimensions"] = SentimentService._normalize_dimensions(
                    parsed.get("dimensions")
                )
            return parsed
        return None

    @staticmethod
    def _build_summary(results: list, posts: list = None, source_names: dict = None) -> dict:
        """构建汇总统计。

        posts 非空时额外产出按来源分组的对比。分组走纯 Python，不需要 LLM 感知来源 ——
        最稳、可测，也不会让模型带着「某平台就该更负面」的先验去污染要比较的那个维度。
        posts 默认 None 保证既有调用方一行不用改。
        """
        sentiments = {"positive": 0, "negative": 0, "neutral": 0}
        not_analyzed = 0
        dimension_counter = {}
        intensity_sum = 0
        intensity_count = 0
        user_sentiments = {}

        for r in results:
            if not r:
                not_analyzed += 1
                continue
            s = r.get("sentiment")
            if s not in sentiments:
                not_analyzed += 1
                if s is not None:
                    logger.warning("舆情分析返回了未知的 sentiment 值: %r", s)
                continue
            sentiments[s] += 1

            i = r.get("intensity", 1)
            if isinstance(i, (int, float)):
                intensity_sum += i
                intensity_count += 1

            for dim in r.get("dimensions", []):
                dimension_counter[dim] = dimension_counter.get(dim, 0) + 1

        analyzed = sum(sentiments.values())
        denominator = analyzed or 1
        summary = {
            "sentiment_distribution": sentiments,
            "analyzed": analyzed,
            "not_analyzed": not_analyzed,
            "sentiment_percentages": {
                k: round(v / denominator * 100, 1) for k, v in sentiments.items()
            },
            "avg_intensity": round(intensity_sum / intensity_count, 2) if intensity_count > 0 else 0,
            "top_dimensions": sorted(dimension_counter.items(), key=lambda x: x[1], reverse=True)[:10],
        }

        if posts:
            by_source, cross_source = SentimentService._group_by_source(
                results, posts, source_names or {}
            )
            summary["by_source"] = by_source
            summary["cross_source"] = cross_source
        return summary

    @staticmethod
    def _group_by_source(results: list, posts: list, source_names: dict):
        """按来源切分同一批结果。results[i] 与 posts[i] 是绝对下标一一对应的。"""
        buckets: dict = {}
        for i, post in enumerate(posts):
            r = results[i] if i < len(results) else None
            if not r or r.get("sentiment") not in ("positive", "negative", "neutral"):
                continue
            sid = post.get("source") or "tweakers"
            b = buckets.setdefault(sid, {
                "name": source_names.get(sid, sid),
                "distribution": {"positive": 0, "negative": 0, "neutral": 0},
                "analyzed": 0,
                "_intensity_sum": 0,
                "_intensity_count": 0,
                "_dimensions": {},
            })
            b["distribution"][r["sentiment"]] += 1
            b["analyzed"] += 1
            inten = r.get("intensity", 1)
            if isinstance(inten, (int, float)):
                b["_intensity_sum"] += inten
                b["_intensity_count"] += 1
            for dim in r.get("dimensions", []):
                b["_dimensions"][dim] = b["_dimensions"].get(dim, 0) + 1

        by_source = {}
        cross_source: dict = {}
        for sid, b in buckets.items():
            count = b["_intensity_count"]
            by_source[sid] = {
                "name": b["name"],
                "distribution": b["distribution"],
                "analyzed": b["analyzed"],
                "avg_intensity": round(b["_intensity_sum"] / count, 2) if count else 0,
                "top_dimensions": sorted(
                    b["_dimensions"].items(), key=lambda x: x[1], reverse=True
                )[:10],
            }
            for dim, n in b["_dimensions"].items():
                cross_source.setdefault(dim, {})[sid] = n
        return by_source, cross_source
