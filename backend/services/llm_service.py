"""
LLM 通用调用服务
- 底层 HTTP 客户端，提供 chat / chat_json 能力
- 评分业务逻辑请使用 services.llm_scoring_service
"""
import json
import os
import httpx
from typing import Optional
from config import LLM_API_KEY, LLM_API_URL, LLM_MODEL

# 强制print输出（避免日志缓冲/过滤问题）
def log(msg):
    print(f"[LLM] {msg}", flush=True)


class LLMService:
    """大模型调用服务"""

    def __init__(self):
        self.api_key = LLM_API_KEY
        self.api_url = LLM_API_URL
        self.model = LLM_MODEL
        self.timeout = 120  # 超时时间（秒）

    async def chat(self, system_prompt: str, user_prompt: str,
                   temperature: float = 0.3, model: str = None,
                   seed: int = None) -> Optional[str]:
        """调用LLM对话接口

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 温度参数
            model: 指定模型（可选，默认使用配置中的模型）
        """
        if not self.api_key:
            log("未配置API Key，跳过LLM调用")
            return None

        # 使用指定模型或默认模型
        use_model = model or self.model

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload = {
            "model": use_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
        }
        if seed is not None:
            payload["seed"] = seed

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.api_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                log(f"原始响应长度: {len(content)} 字符")
                return content
        except httpx.HTTPStatusError as e:
            log(f"HTTP错误: {e.response.status_code} - {e.response.text[:200]}")
            return None
        except Exception as e:
            log(f"调用失败: {str(e)}")
            return None

    async def chat_json(self, system_prompt: str, user_prompt: str,
                        temperature: float = 0.2, model: str = None,
                        seed: int = None) -> Optional[dict]:
        """调用LLM并解析JSON响应

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: 温度参数
            model: 指定模型（可选）
            seed: 随机种子（P2-2 确定性模式）
        """
        content = await self.chat(system_prompt, user_prompt, temperature, model, seed)
        if not content:
            return None

        # 尝试从响应中提取JSON
        try:
            # 尝试直接解析
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # 尝试从markdown代码块中提取
        import re
        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # 尝试找到第一个 { 和最后一个 }
        first_brace = content.find('{')
        last_brace = content.rfind('}')
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            try:
                return json.loads(content[first_brace:last_brace + 1])
            except json.JSONDecodeError:
                pass

        log(f"JSON解析失败，原始响应: {content[:300]}")
        return None

# 全局实例
llm_service = LLMService()
