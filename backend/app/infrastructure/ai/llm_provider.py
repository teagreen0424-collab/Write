"""LLM Provider（OpenAI 兼容协议）。

业务层只能依赖此抽象入口，禁止直接 import 具体 Provider。
阶段一用 httpx 直连，避免引入 openai SDK / 向量库等额外依赖。
"""
import httpx

from app.config import get_settings


class LLMProvider:
    """OpenAI 兼容 LLM 客户端抽象，默认指向 DeepSeek。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.openai_api_key
        self._base_url = settings.openai_base_url.rstrip("/")
        self._model = settings.openai_model

    def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
    ) -> str:
        """发起一次补全，返回文本内容。"""
        if not self._api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY，请在 backend/.env 中填写")

        resp = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
