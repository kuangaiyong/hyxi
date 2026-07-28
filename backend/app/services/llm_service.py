"""LLM API 客户端服务"""

import json
from typing import List, Dict, Any
import httpx
from app.models import LLMConfig


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
            timeout=60.0,
        )

    async def test_connection(self) -> bool:
        """测试 API 连接"""
        try:
            # 尝试列出模型 或发送简单请求
            resp = await self.client.get("/models")
            if resp.status_code == 200:
                return True
            # 如果 /models 不可用，尝试 chat completion
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

    async def parse_intent(self, user_description: str) -> List[Dict]:
        """调用 LLM 解析用户意图，返回执行计划"""
        system_prompt = """你是一个 Tweakers.net 论坛数据抓取工具的智能调度器。
你可以执行以下操作：
1. scrape - 抓取帖子（参数：thread_id 帖子ID, start_page 起始页默认1, headless 是否无头模式默认true）
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
- 如果用户没有指定帖子ID，设置为0（后续提示用户补充）
- 如果用户只要求翻译已有的JSON文件，不要包含 scrape 步骤
- 如果用户只要求抓取不翻译，不要包含 translate 步骤
- 默认启用 headless 模式
- 翻译使用 LLM 大模型进行专业翻译
- 输出纯JSON，不要包含markdown代码块标记"""

        resp = await self.client.post(
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

        if resp.status_code != 200:
            raise Exception(f"LLM API 返回错误: {resp.status_code} - {resp.text}")

        data = resp.json()
        content = data["choices"][0]["message"]["content"]

        # 清理可能的 markdown 代码块
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        return json.loads(content)

    async def chat(self, messages: List[Dict], **kwargs) -> str:
        """通用聊天接口"""
        resp = await self.client.post(
            "/chat/completions",
            json={
                "model": self.config.model_name,
                "messages": messages,
                **kwargs,
            },
        )
        if resp.status_code != 200:
            raise Exception(f"LLM API 错误: {resp.status_code} - {resp.text}")
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def close(self):
        await self.client.aclose()
