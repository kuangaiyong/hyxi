"""舆情分析服务 - 使用 LLM 分析多来源帖子的情感倾向"""

import json
import re
import os
import asyncio
import logging
from typing import List, Optional
from app.collectors import get_collector
from app.services.post_tree import post_key
from app.services.progress_manager import ProgressManager
from app.services.llm_service import LLMService
from app.services.llm_utils import get_llm_service
from app.services.storage import save_sentiment as db_save_sentiment
from app.models import LLMConfig
from app.config import settings

logger = logging.getLogger("hyxi.sentiment")

BATCH_SIZE = 3  # 详细分析每次发送的帖子数


def _source_output_paths() -> List[str]:
    """所有已注册数据源的落盘文件（存在的那些）"""
    from app.services import source_service

    paths = []
    for source in source_service.list_sources():
        try:
            path = get_collector(source["collector_id"]).output_path(source)
        except (ValueError, KeyError):
            continue
        if os.path.exists(path):
            paths.append(path)
    return paths


def _sync_processed_flags(json_path: str, posts: list) -> int:
    """把帖子的 _processed 标记按指纹合并回源 JSON。

    必须是合并而非替换：舆情分析只带 sentiment_at，整体覆盖会抹掉 translated
    标记，导致几百条已翻译的帖子下次重新走一遍付费翻译。
    """
    with open(json_path, "r", encoding="utf-8") as f:
        jdata = json.load(f)
    jposts = jdata.get("posts", [])
    by_fp = {jp.get("fingerprint"): jp for jp in jposts if jp.get("fingerprint")}

    updated = 0
    for p in posts:
        jp = by_fp.get(p.get("fingerprint"))
        if jp is not None:
            jp.setdefault("_processed", {}).update(p.get("_processed", {}))
            updated += 1

    if updated > 0:
        jdata["posts"] = jposts
        # 源数据文件写坏就再也拿不回来了，先写临时文件再原子替换
        tmp_path = json_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(jdata, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)
    return updated

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
- 标了 [回复上文: ...] 的是对上文的回复，请结合上文判断；「+1」「同上」这类附和
  要继承上文的情感倾向，而不是一律判 neutral

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
    async def analyze(
        task_id: str,
        posts: list,
        progress: ProgressManager,
        existing_results: list = None,
        fp_to_idx: dict = None,
        source_names: dict = None,
        parent_by_key: dict = None,
        all_posts: list = None,
    ) -> dict:
        """对帖子进行舆情分析（增量：仅分析传入的帖子，合并已有结果）"""
        total = len(posts)
        non_empty = [(i, p) for i, p in enumerate(posts) if p.get("content", "").strip()]
        total_non_empty = len(non_empty)

        await progress.emit(task_id, "log", {
            "level": "info",
            "message": f"开始舆情分析，共 {total} 条帖子，{total_non_empty} 条有内容",
        })

        # 加载 LLM 配置（使用统一工具函数）
        llm = get_llm_service()
        if not llm:
            raise Exception("请先配置 LLM API")

        results = [None] * total
        success_count = 0
        fail_count = 0

        def _post_block(orig_idx: int, p: dict) -> str:
            """一条帖子在 prompt 里的样子。批量和单条重试必须用同一份 ——
            来源标签和父贴上文都影响判定，重试时少给就成了另一道题。"""
            content = p.get("content", "")
            truncated = content[:2000] if len(content) > 2000 else content
            sid = p.get("source", "")
            label = (source_names or {}).get(sid, sid) or "未知来源"
            block = f"帖子{orig_idx + 1} [来源: {label}]:\n"
            # 评论多是「+1」「same here」，单独看全是 neutral 噪音，
            # 带上父贴前 200 字才判得准
            parent = (parent_by_key or {}).get(post_key(p))
            if parent:
                block += f"[回复上文: {(parent.get('content') or '')[:200]}]\n"
            return block + f"{truncated}\n\n"

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
                    "step": 0,
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

        # 保存帖子的 _processed 标记回源 JSON（按指纹匹配）。
        # 不再 glob `tweakers_thread_*.json` —— 文件名只有 Collector.output_path() 一个来源。
        # 多来源之后一批帖子会横跨多个文件，不能匹配够数就 break。
        synced = 0
        for path in _source_output_paths():
            try:
                synced += _sync_processed_flags(path, posts)
                if synced >= len(posts):
                    break
            except Exception as e:
                # 单个文件损坏不该让整轮分析结果丢失
                logger.warning("回写 _processed 标记失败 %s: %s", path, e)

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
