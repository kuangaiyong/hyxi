"""舆情分析服务 - 使用 LLM 分析荷兰语帖子的情感倾向"""

import json
import re
import os
import asyncio
from typing import Optional
from app.services.progress_manager import ProgressManager
from app.services.llm_service import LLMService
from app.models import LLMConfig
from app.config import settings

BATCH_SIZE = 3  # 详细分析每次发送的帖子数

SENTIMENT_SYSTEM_PROMPT = """你是一位精通荷兰语和中文的市场舆情分析专家。你的任务是分析荷兰语论坛帖子对产品 HYXi Halo 家用储能电池的情感倾向。

请对每一条帖子输出以下 JSON：

{
  "sentiment": "positive|negative|neutral",
  "intensity": 1-5,
  "reason_cn": "一句话中文分析理由",
  "dimensions": ["相关维度标签"]
}

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

**维度(dimensions)**，从以下选择相关标签（可多选）：
- 价格/性价比
- 产品质量/可靠性
- 安装/配置体验
- App/软件体验
- 客服/售后支持
- WiFi/连接问题
- 固件更新
- 温度/散热
- P1电表/智能控制
- 认证/合规(如Synergrid)
- 扩展/兼容性
- 与其他品牌对比(如AEG/Marstek)
- 性能/效率
- 安全性

**特别说明**：
- 如果帖子提到问题但后续解决了，要看整体语气：顺利解决→positive，抱怨解决过程→negative
- 如果是纯技术讨论/提问，即使涉及问题也应判断为neutral
- 与其他产品的对比：如果偏向Hyxi→positive，如果偏向竞品→negative

输入是多条荷兰语帖子，请为每条帖子输出JSON，用 '---SENTIMENT_SEPARATOR---' 分隔每条结果。
直接输出JSON对象序列，不要编号，不要额外解释。"""


class SentimentService:
    """LLM 舆情分析"""

    @staticmethod
    async def analyze(
        task_id: str,
        posts: list,
        progress: ProgressManager,
        existing_results: list = None,
        fp_to_idx: dict = None,
    ) -> dict:
        """对帖子进行舆情分析（增量：仅分析传入的帖子，合并已有结果）"""
        total = len(posts)
        non_empty = [(i, p) for i, p in enumerate(posts) if p.get("content", "").strip()]
        total_non_empty = len(non_empty)

        await progress.emit(task_id, "log", {
            "level": "info",
            "message": f"开始舆情分析，共 {total} 条帖子，{total_non_empty} 条有内容",
        })

        # 加载 LLM
        config_path = settings.config_file
        if not os.path.exists(config_path):
            raise Exception("请先配置 LLM API")
        with open(config_path, "r") as f:
            cfg_data = json.load(f)
        llm_config = LLMConfig(**cfg_data)
        llm = LLMService(llm_config)

        results = [None] * total
        success_count = 0
        fail_count = 0

        try:
            for batch_start in range(0, total_non_empty, BATCH_SIZE):
                batch = non_empty[batch_start:batch_start + BATCH_SIZE]

                # 构建批量分析请求
                user_message = "请分析以下荷兰语论坛帖子对HYXi Halo产品的情感倾向：\n\n"
                for idx_in_batch, (orig_idx, p) in enumerate(batch):
                    content = p.get("content", "")
                    truncated = content[:2000] if len(content) > 2000 else content
                    user_message += f"帖子{orig_idx + 1}:\n{truncated}\n\n"

                try:
                    resp = await llm.client.post(
                        "/chat/completions",
                        json={
                            "model": llm.config.model_name,
                            "messages": [
                                {"role": "system", "content": SENTIMENT_SYSTEM_PROMPT},
                                {"role": "user", "content": user_message},
                            ],
                            "temperature": 0.2,
                            "max_tokens": 4096,
                        },
                    )

                    if resp.status_code != 200:
                        raise Exception(f"LLM API 错误: {resp.status_code}")

                    data = resp.json()
                    result_text = data["choices"][0]["message"]["content"]

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
                                    "sentiment": "neutral",
                                    "intensity": 1,
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
                            "sentiment": "neutral",
                            "intensity": 1,
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

        finally:
            await llm.close()

        # 标记已分析的帖子的 _processed.sentiment_at
        now_iso = __import__('datetime').datetime.now().isoformat()
        for orig_idx, __p in non_empty:
            if results[orig_idx] and results[orig_idx].get("sentiment"):
                processed = posts[orig_idx].setdefault("_processed", {})
                if not processed.get("sentiment_at"):
                    processed["sentiment_at"] = now_iso

        # 合并已有结果（增量模式）：用指纹匹配绝对位置
        if existing_results and fp_to_idx:
            full_results = list(existing_results)
            # 确保 full_results 长度 = max(existing, all_posts 总数)
            all_total = len(fp_to_idx)
            while len(full_results) < all_total:
                full_results.append(None)

            # 将新分析结果按指纹映射到绝对位置
            for i, (orig_idx_in_pending, __p) in enumerate(non_empty):
                r = results[orig_idx_in_pending]
                if r and r.get("sentiment"):
                    fp = posts[orig_idx_in_pending].get("fingerprint", "")
                    abs_idx = fp_to_idx.get(fp)
                    if abs_idx is not None and abs_idx < len(full_results):
                        full_results[abs_idx] = r
            results = full_results

        # 保存结果到 JSON
        sentiment_path = os.path.join(settings.data_dir, f"sentiment_{task_id}.json")
        # 全量总数：优先用 fp_to_idx 的长度（最准确），其次为已有结果+本次分析数
        if fp_to_idx:
            all_total = len(fp_to_idx)
        elif existing_results:
            all_total = len(existing_results) + total
        else:
            all_total = total
        summary = SentimentService._build_summary(results)

        output = {
            "task_id": task_id,
            "analyzed_at": now_iso,
            "total": all_total,
            "success": sum(1 for r in results if r and r.get("sentiment")),
            "failed": sum(1 for r in results if not r or not r.get("sentiment")),
            "summary": summary,
            "results": results,
        }

        os.makedirs(os.path.dirname(sentiment_path), exist_ok=True)
        with open(sentiment_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        # 保存帖子的 _processed 标记回 JSON（按指纹匹配，更新所有 JSON 文件）
        import glob as _g
        for jf in _g.glob(os.path.join(settings.project_root, "tweakers_thread_*.json")):
            with open(jf, "r", encoding="utf-8") as f:
                jdata = json.load(f)
            jposts = jdata.get("posts", [])
            updated = 0
            for p in posts:
                fp = p.get("fingerprint")
                if fp:
                    for jp in jposts:
                        if jp.get("fingerprint") == fp:
                            jp["_processed"] = p.get("_processed", {})
                            updated += 1
                            break
            if updated > 0:
                jdata["posts"] = jposts
                with open(jf, "w", encoding="utf-8") as f:
                    json.dump(jdata, f, ensure_ascii=False, indent=2)

        await progress.emit(task_id, "log", {
            "level": "success",
            "message": f"舆情分析完成: {output['success']} 成功, {output['failed']} 失败",
        })

        return output

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
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _build_summary(results: list) -> dict:
        """构建汇总统计"""
        sentiments = {"positive": 0, "negative": 0, "neutral": 0}
        dimension_counter = {}
        intensity_sum = 0
        intensity_count = 0
        user_sentiments = {}

        for r in results:
            if not r:
                sentiments["neutral"] += 1
                continue
            s = r.get("sentiment", "neutral")
            sentiments[s] = sentiments.get(s, 0) + 1

            i = r.get("intensity", 1)
            if isinstance(i, (int, float)):
                intensity_sum += i
                intensity_count += 1

            for dim in r.get("dimensions", []):
                dimension_counter[dim] = dimension_counter.get(dim, 0) + 1

        total = len(results) or 1
        return {
            "sentiment_distribution": sentiments,
            "sentiment_percentages": {
                k: round(v / total * 100, 1) for k, v in sentiments.items()
            },
            "avg_intensity": round(intensity_sum / intensity_count, 2) if intensity_count > 0 else 0,
            "top_dimensions": sorted(dimension_counter.items(), key=lambda x: x[1], reverse=True)[:10],
        }
