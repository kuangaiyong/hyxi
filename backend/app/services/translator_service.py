"""翻译服务 - 使用配置的 LLM 进行翻译"""

import json
import re
import asyncio
import logging
from app.services.progress_manager import ProgressManager
from app.services.llm_service import LLMService
from app.services.llm_utils import get_llm_service
from app.models import LLMConfig
from app.config import settings

logger = logging.getLogger("hyxi.translator")


# 批量翻译时每次 LLM 调用处理的帖子数量
BATCH_SIZE = 5

TRANSLATION_SYSTEM_PROMPT = """你是一位精通荷兰语、英语和中文的新能源光储行业专业翻译专家。
你的任务是将论坛与社交媒体帖子翻译成中文。源文本可能是荷兰语或英语，同一批次内可能混杂，
请自动识别语种并统一译为中文；已经是中文的原样返回。需要特别注意：

1. **行业术语准确**：
   - thuisbatterij / thuisaccu → 家用储能电池
   - omvormer → 逆变器
   - terugleveren → 余电上网/向电网送电
   - salderen → 净计量/余额结算
   - zonnepanelen / PV → 光伏组件
   - warmtepomp → 热泵
   - groepenkast → 配电箱
   - aardlekschakelaar → 漏电保护器
   - kWh / kW → 保持原单位
   - P1-poort / P1-meter → P1端口/P1电表
   - NOM (Nul Op de Meter) → 零电表/自发自用
   - LiFePO4 → 磷酸铁锂
   - BMS (Battery Management System) → 电池管理系统
   - firmware → 固件
   - mesh-netwerk → 网状网络
   - stopcontact → 插座

2. **语言风格**：自然流畅的口语化中文，保留原文的语气和情感

3. **格式要求**：
   - 保留原文中的数字、单位、百分比
   - 产品名称（如 HYXi Halo）保持原样
   - 保留 URL 链接

4. 输入是一段外文文本，直接输出对应的中文翻译，不要添加任何解释或前缀"""


def _strip_numbering(text: str) -> str:
    """剥掉 LLM 可能带上的「1. 」「[2]. 」「3) 」「[1]\\n」编号前缀。

    裸数字必须带分隔符才算编号，否则会吞掉正文开头的真实数值（如「5 kWh 就够了」）；
    方括号本身就是分隔符，`[1]` 后面直接换行也要剥 —— 请求里的编号就是这个形态，
    模型经常原样抄回来，不剥就会印进 Excel 和结果页。
    """
    return re.sub(r'^(?:\[?\d+\]?[\.\)、]|\[\d+\])\s*', '', text)


def _has_cjk(text: str) -> bool:
    return any('一' <= ch <= '鿿' for ch in text)


def _looks_untranslated(translation: str, content: str) -> bool:
    """译文与原文一字不差、且原文本来就不是中文 —— 判定为漏译。

    只在完全相同时才判，避免把「HYXi Halo 5 kWh」这类本就该原样保留的短内容误判。
    """
    t, c = translation.strip(), content.strip()
    return bool(t) and t == c and not _has_cjk(c)


class TranslatorService:
    """使用 LLM 翻译帖子内容"""

    @staticmethod
    async def execute(
        task_id: str,
        posts: list,
        params: dict,
        progress: ProgressManager,
        step_index: int = 1,
    ) -> dict:
        """执行 LLM 翻译。

        step_index 不能再写死 1 —— plan 里有多个 collect 步骤时翻译就不在第 2 位了，
        写死会让前端把进度画到别的步骤上。
        """
        total = len(posts)
        source_lang = params.get("source_lang", "nl")
        target_lang = params.get("target_lang", "zh-CN")

        await progress.emit(task_id, "step_progress", {
            "step": step_index,
            "progress": 0.0,
            "message": f"正在使用 LLM 翻译 {total} 条帖子 ({source_lang} → {target_lang})...",
        })

        # 加载 LLM 配置，创建 LLM 客户端（使用统一工具函数）
        llm = get_llm_service()
        if not llm:
            raise Exception("请先配置 LLM API")

        translations = []
        success_count = 0
        fail_count = 0

        try:
            # 分批翻译：将有内容的帖子收集起来批量调用 LLM
            non_empty_indices = [i for i, p in enumerate(posts) if p.get("content", "").strip()]
            total_non_empty = len(non_empty_indices)

            await progress.emit(task_id, "log", {
                "level": "info",
                "message": f"共 {total} 条帖子，其中 {total_non_empty} 条需要翻译，使用 {llm.config.model_name} 模型",
            })

            # 初始化所有翻译为待翻译状态
            translations = [""] * total

            for batch_start in range(0, total_non_empty, BATCH_SIZE):
                batch_indices = non_empty_indices[batch_start:batch_start + BATCH_SIZE]

                # 构建批量翻译提示
                texts_for_batch = []
                for idx in batch_indices:
                    content = posts[idx].get("content", "")
                    truncated = content[:2500] if len(content) > 2500 else content
                    texts_for_batch.append(truncated)

                # 构建消息：一次发送多条待翻译文本
                user_message = "请将以下帖子翻译成中文（荷兰语或英语，逐条识别），每条翻译之间用 '---POST_SEPARATOR---' 分隔，保持顺序：\n\n"
                for i, text in enumerate(texts_for_batch):
                    user_message += f"[{i + 1}]\n{text}\n\n"

                try:
                    result_text = await llm.chat_with_retry(
                        system_prompt=TRANSLATION_SYSTEM_PROMPT,
                        user_message=user_message,
                        temperature=0.3,
                        max_tokens=4096,
                        label=f"批量翻译 [{batch_start+1}-{min(batch_start+BATCH_SIZE, total_non_empty)}]",
                    )

                    # 解析批量翻译结果
                    batch_translations = result_text.split("---POST_SEPARATOR---")
                    # 也可能 LLM 返回了编号分隔的格式
                    if len(batch_translations) <= 1:
                        # 尝试用数字编号分割
                        parts = re.split(r'\n\s*(?=\d+\.|\d+\)|\【\d+\】)', result_text.strip())
                        if len(parts) > 1:
                            batch_translations = parts

                    for j, idx in enumerate(batch_indices):
                        if j < len(batch_translations):
                            trans = _strip_numbering(batch_translations[j].strip())
                            if trans:
                                translations[idx] = trans
                                success_count += 1
                            else:
                                translations[idx] = f"[翻译为空] {texts_for_batch[j][:100]}"
                                fail_count += 1
                        else:
                            translations[idx] = f"[翻译解析失败] {texts_for_batch[j][:100]}"
                            fail_count += 1

                except Exception as e:
                    await progress.emit(task_id, "log", {
                        "level": "warning",
                        "message": f"批量翻译失败 (第 {batch_start + 1}-{min(batch_start + BATCH_SIZE, total_non_empty)} 条): {str(e)[:100]}",
                    })
                    # 失败时给这批帖子标上待翻译
                    for idx in batch_indices:
                        translations[idx] = f"[翻译失败] {posts[idx].get('content', '')[:200]}"
                        fail_count += 1

                # 更新进度
                translated_count = batch_start + len(batch_indices)
                pct = translated_count / total_non_empty if total_non_empty > 0 else 1.0
                await progress.emit(task_id, "step_progress", {
                    "step": step_index,
                    "progress": min(pct, 0.99),
                    "message": f"已翻译 {translated_count}/{total_non_empty} 条 ({success_count} 成功, {fail_count} 失败)",
                })

                # API 调用间短暂延迟，避免限流
                await asyncio.sleep(1.0)

            # ===== 重试失败的翻译 =====
            # 批内混语种时 LLM 有时会「统一」成一种语言而把另一种原样吐回来。
            # 译文与原文完全相同且原文非中文 = 漏译，走同一条单条重译队列。
            failed_indices = [
                i for i, t in enumerate(translations)
                if t.startswith("[翻译失败") or t.startswith("[翻译为空")
                or t.startswith("[翻译解析失败")
                or _looks_untranslated(t, posts[i].get("content", ""))
            ]

            if failed_indices:
                await progress.emit(task_id, "log", {
                    "level": "info",
                    "message": f"开始重试 {len(failed_indices)} 条失败的翻译...",
                })

                still_failed = []
                for idx in failed_indices:
                    content = posts[idx].get("content", "")
                    truncated = content[:2500] if len(content) > 2500 else content

                    try:
                        trans = await llm.chat_with_retry(
                            system_prompt=TRANSLATION_SYSTEM_PROMPT,
                            user_message=f"请将以下荷兰语翻译成中文（直接输出翻译，不要额外说明）：\n{truncated}",
                            temperature=0.2,
                            max_tokens=2048,
                            max_retries=2,
                            label=f"重译单条 #{idx}",
                        )
                        if trans and len(trans.strip()) > 2:
                            translations[idx] = trans.strip()
                            success_count += 1
                            fail_count -= 1
                        else:
                            still_failed.append(idx)
                    except Exception:
                        still_failed.append(idx)

                    await asyncio.sleep(1.0)

                failed_indices = still_failed

                if still_failed:
                    await progress.emit(task_id, "log", {
                        "level": "warning",
                        "message": f"重试完成，仍有 {len(still_failed)} 条翻译失败",
                    })

            await progress.emit(task_id, "log", {
                "level": "info",
                "message": f"翻译结果: {success_count} 成功, {fail_count} 失败 (已重试)",
            })

        finally:
            await llm.close()

        # 将翻译写回帖子
        for i, trans in enumerate(translations):
            posts[i]["translation"] = trans

        await progress.emit(task_id, "step_progress", {
            "step": step_index,
            "progress": 1.0,
            "message": f"LLM 翻译完成: {success_count}/{total} 成功 ({llm.config.model_name})",
        })

        await progress.emit(task_id, "log", {
            "level": "success",
            "message": f"翻译结果: {success_count} 条成功, {fail_count} 条失败, 使用模型 {llm.config.model_name}",
        })

        return {"posts": posts, "translated_count": success_count}
