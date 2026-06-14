import json
import logging
import asyncio
from openai import AsyncOpenAI
from .base import BaseAI, SpamResult
from .prompts import TEXT_PROMPT, IMAGE_PROMPT

logger = logging.getLogger(__name__)

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 1  # 秒

# 并发限制：群组消息高峰时限制同时发往 AI API 的请求数，避免触发速率限制
MAX_CONCURRENT_REQUESTS = 5

class OpenAIClient(BaseAI):
    """OpenAI / 兼容接口客户端"""

    def __init__(self, api_key: str, base_url: str, model: str):
        super().__init__(api_key, base_url, model)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def _call_with_retry(self, messages: list) -> str:
        """带重试机制的 API 调用"""
        async with self._semaphore:
            last_error = None
            for attempt in range(MAX_RETRIES):
                try:
                    response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=messages
                    )
                    return response.choices[0].message.content
                except Exception as e:
                    last_error = e
                    if attempt < MAX_RETRIES - 1:
                        logger.warning(f"AI API 调用失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # 指数退避
                    else:
                        logger.error(f"AI API 调用最终失败: {e}")
            raise last_error

    async def check_text(self, user_info: str, message: str) -> SpamResult:
        prompt = TEXT_PROMPT.format(user_info=user_info, message=message)
        content = await self._call_with_retry([{"role": "user", "content": prompt}])
        return self._parse_result(content)

    async def check_image(self, user_info: str, image_base64: str) -> SpamResult:
        prompt = IMAGE_PROMPT.format(user_info=user_info)
        content = await self._call_with_retry([{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_base64}}
            ]
        }])
        return self._parse_result(content)

    def _parse_result(self, content: str) -> SpamResult:
        try:
            # 尝试提取 JSON（AI 可能返回带有额外文字的响应）
            json_start = content.find('{')
            json_end = content.rfind('}') + 1
            if json_start != -1 and json_end > json_start:
                content = content[json_start:json_end]
            
            data = json.loads(content)
            return SpamResult(
                is_spam=data.get("state", 0) == 1,
                score=data.get("spam_score", 0),
                reason=data.get("spam_reason", ""),
                mock_text=data.get("spam_mock_text", "")
            )
        except json.JSONDecodeError as e:
            logger.warning(f"AI 响应 JSON 解析失败: {e}, 原始内容: {content[:200]}")
            return SpamResult(is_spam=False, score=0, reason="解析失败", mock_text="")
