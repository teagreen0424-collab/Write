"""LLM Provider（OpenAI 兼容协议）。

业务层只能依赖此抽象入口，禁止直接 import 具体 Provider。
"""
from openai import OpenAI

from config import get_settings


class LLMProvider:
    """OpenAI 兼容 LLM 客户端抽象，默认指向 DeepSeek。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        self._model = settings.llm_model

    def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """发起一次补全，返回文本内容。"""
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""
