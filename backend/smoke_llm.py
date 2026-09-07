"""阶段零验收：验证 LLM 补全。

用法（在 backend 目录下）：
    .venv\\Scripts\\python smoke_llm.py
"""
from app.infrastructure.ai.llm_provider import LLMProvider
from app.infrastructure.ai.prompts import SCAFFOLD_SMOKE_PROMPT


def test_llm() -> str:
    provider = LLMProvider()
    text = provider.complete([{"role": "user", "content": SCAFFOLD_SMOKE_PROMPT}])
    print(f"[LLM] 返回：{text}")
    return text


if __name__ == "__main__":
    test_llm()
    print("阶段零 LLM 验收通过")
