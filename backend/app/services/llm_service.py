"""LLM API 客户端服务 — 支持 DeepSeek / OpenAI 兼容 API，含重试"""

import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional
import httpx
from app.models import LLMConfig

logger = logging.getLogger("hyxi.llm")

# 重试配置
MAX_RETRIES = 3
BASE_DELAY = 1.0  # 秒
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{4,}|Bearer\s+\S+|org-[A-Za-z0-9_\-]+)", re.I)


def _safe_error_detail(resp: httpx.Response, limit: int = 120) -> str:
    """提取上游错误原因用于对外展示。

    这段文字会进 task["error_message"] 并被持久化、被前端展示，而供应商的错误体
    常带组织 ID、账号标识和配额明细，原样存下来等于把这些信息一起留在库里。
    完整响应只记服务端日志。
    """
    logger.warning("上游 API 返回 %s: %s", resp.status_code, resp.text[:1000])
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            detail = err
    except Exception:
        pass
    if not detail:
        return "上游未返回可解析的错误信息，详见服务端日志"
    return _SECRET_RE.sub("***", detail)[:limit]


async def _retry_with_backoff(
    fn,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY,
    label: str = "LLM call",
) -> httpx.Response:
    """指数退避重试包装器"""
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            resp = await fn()
            if resp.status_code < 500 or attempt >= max_retries:
                return resp
            # 5xx 错误，重试
            if resp.status_code in RETRYABLE_STATUSES:
                raise Exception(f"HTTP {resp.status_code}")
            return resp  # 非可重试状态码，直接返回
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "%s 第 %d/%d 次失败: %s，%0.1f 秒后重试...",
                    label, attempt + 1, max_retries, str(e)[:120], delay,
                )
                await asyncio.sleep(delay)
    raise last_exception  # type: ignore


class LLMService:
    """DeepSeek / OpenAI 兼容 API 客户端"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def test_connection(self) -> bool:
        """测试 API 连接"""
        try:
            resp = await self.client.get("/models")
            if resp.status_code == 200:
                return True
            resp = await self.client.post(
                "/chat/completions",
                json={
                    "model": self.config.model_name,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 5,
                },
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def parse_intent(self, user_description: str) -> dict:  # 返回 {"plan": [...]}
        """调用 LLM 解析用户意图，返回执行计划（含自动重试）"""
        system_prompt = """你是一个 Tweakers.net 论坛数据抓取工具的智能调度器。
你可以执行以下操作：
1. scrape - 抓取帖子（参数：thread_id 帖子ID, headless 是否无头模式默认true）
2. translate - 翻译帖子内容（参数：source_lang 源语言默认nl, target_lang 目标语言默认zh-CN）
3. generate_excel - 生成Excel报告（参数：include_stats 是否包含统计表默认true）

请分析用户的自然语言请求，输出一个JSON格式的执行计划。
输出格式必须是严格的JSON，包含 plan 数组：
{
  "plan": [
    {"action": "scrape", "params": {"thread_id": 2336074, "headless": true}},
    {"action": "translate", "params": {"source_lang": "nl", "target_lang": "zh-CN"}},
    {"action": "generate_excel", "params": {"include_stats": true}}
  ]
}

规则：
- 从用户描述中提取帖子ID（数字），如果用户提到"帖子"、"thread"、"帖子ID"后面的数字
- 帖子URL形如 https://gathering.tweakers.net/forum/list_messages/{帖子ID}/{页码}，
  thread_id 只取第一段数字，末尾那段数字是页码不是帖子ID
- scrape 不接受起始页参数，任何情况下都不要输出 start_page
- 如果用户没有指定帖子ID，设置为0（后续提示用户补充）
- 如果用户只要求翻译已有的JSON文件，不要包含 scrape 步骤
- 如果用户只要求抓取不翻译，不要包含 translate 步骤
- 默认启用 headless 模式
- 翻译使用 LLM 大模型进行专业翻译
- 输出纯JSON，不要包含markdown代码块标记"""

        async def _call():
            return await self.client.post(
                "/chat/completions",
                json={
                    "model": self.config.model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_description},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 2000,
                },
            )

        resp = await _retry_with_backoff(_call, label="意图解析")

        if resp.status_code != 200:
            raise Exception(f"LLM API 返回错误: {resp.status_code} - {_safe_error_detail(resp)}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # 清理可能的 markdown 代码块
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        return json.loads(content)

    async def chat(self, messages: List[Dict], **kwargs) -> str:
        """通用聊天接口（含自动重试）"""

        async def _call():
            return await self.client.post(
                "/chat/completions",
                json={
                    "model": self.config.model_name,
                    "messages": messages,
                    **kwargs,
                },
            )

        resp = await _retry_with_backoff(_call, label="LLM chat")

        if resp.status_code != 200:
            raise Exception(f"LLM API 错误: {resp.status_code} - {_safe_error_detail(resp)}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def chat_with_retry(
        self,
        system_prompt: Optional[str],
        user_message: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        max_retries: int = MAX_RETRIES,
        label: str = "LLM call",
    ) -> str:
        """带重试的聊天接口（convenience method，支持自定义重试次数）"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        async def _call():
            return await self.client.post(
                "/chat/completions",
                json={
                    "model": self.config.model_name,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            )

        resp = await _retry_with_backoff(_call, max_retries=max_retries, label=label)

        if resp.status_code != 200:
            raise Exception(f"LLM API 错误: {resp.status_code} - {_safe_error_detail(resp)}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def close(self):
        await self.client.aclose()
