"""LLM Provider（OpenAI 兼容协议）。

业务层只能依赖此抽象入口，禁止直接 import 具体 Provider。
阶段一/二用 httpx 直连，避免引入 openai SDK / 向量库等额外依赖。
"""
import json

import httpx

from app.config import get_settings


class LLMStreamIncompleteError(RuntimeError):
    """流式输出未完整结束；code 供调用方区分可保留的未完成稿。"""

    def __init__(self, message: str, *, code: str, finish_reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.finish_reason = finish_reason


def _sse_data(lines):
    """按 SSE 空行分帧，支持注释/标准字段和多行 data，拒绝损坏的帧。"""
    data = []
    for line in lines:
        if not line:
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if field == "data" and separator:
            data.append(value.removeprefix(" "))
        elif field not in ("event", "id", "retry"):
            raise LLMStreamIncompleteError(
                "模型服务返回的流式数据格式异常，生成未完成。", code="invalid_stream_frame"
            )
    if data:
        yield "\n".join(data)


class LLMProvider:
    """OpenAI 兼容 LLM 客户端抽象，默认指向 DeepSeek。"""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.openai_api_key
        self._base_url = settings.openai_base_url.rstrip("/")
        self._model = settings.openai_model
        self._url = f"{self._base_url}/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _ensure_key(self) -> None:
        if not self._api_key:
            raise RuntimeError("未配置 OPENAI_API_KEY，请在 backend/.env 中填写")

    def complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 1024,
        *,
        require_complete: bool = False,
    ) -> str:
        """发起一次补全；整章替换预览可要求明确完整结束，旧调用兼容。"""
        self._ensure_key()
        resp = httpx.post(
            self._url,
            headers=self._headers,
            json={
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=180.0 if require_complete else 60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if require_complete and data["choices"][0].get("finish_reason") != "stop":
            raise RuntimeError("模型输出未完整结束，不能作为整章修订稿应用")
        return data["choices"][0]["message"]["content"] or ""

    def stream_complete(
        self,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ):
        """流式补全；只有明确完成且有正文才成功，截断等异常不静默落库。"""
        self._ensure_key()
        finish_reason = None
        saw_done = False
        has_text = False
        with httpx.stream(
            "POST",
            self._url,
            headers=self._headers,
            json={
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            },
            timeout=180.0,
        ) as resp:
            resp.raise_for_status()
            for data in _sse_data(resp.iter_lines()):
                chunk = data.strip()
                if chunk == "[DONE]":
                    saw_done = True
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError as exc:
                    raise LLMStreamIncompleteError(
                        "模型服务返回的流式数据格式异常，生成未完成。", code="invalid_stream_frame"
                    ) from exc
                if not isinstance(obj, dict):
                    raise LLMStreamIncompleteError(
                        "模型服务返回的流式数据格式异常，生成未完成。", code="invalid_stream_frame"
                    )
                if "error" in obj:
                    raise LLMStreamIncompleteError(
                        "模型服务返回错误，生成未完成，请稍后重试。", code="upstream_error"
                    )
                choices = obj.get("choices")
                if choices == [] and isinstance(obj.get("usage"), dict):
                    continue  # 正常结束后的独立 token 用量帧，不含正文。
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise LLMStreamIncompleteError(
                        "模型服务返回的流式数据格式异常，生成未完成。", code="invalid_stream_frame"
                    )
                choice = choices[0]
                delta_obj = choice.get("delta")
                if delta_obj is None:
                    delta_obj = {}  # 部分兼容网关的结束帧 delta 为 null。
                if not isinstance(delta_obj, dict):
                    raise LLMStreamIncompleteError(
                        "模型服务返回的流式数据格式异常，生成未完成。", code="invalid_stream_frame"
                    )
                delta = delta_obj.get("content")
                if delta is not None and not isinstance(delta, str):
                    raise LLMStreamIncompleteError(
                        "模型服务返回的正文格式异常，生成未完成。", code="invalid_stream_frame"
                    )
                if delta:
                    if finish_reason is not None:
                        raise LLMStreamIncompleteError(
                            "模型服务在结束后仍返回正文，生成结果无法确认。", code="invalid_stream_frame"
                        )
                    has_text = has_text or bool(delta.strip())
                    yield delta
                reason = choice.get("finish_reason")
                if reason is None:
                    continue
                if reason == "length":
                    raise LLMStreamIncompleteError(
                        "生成达到输出上限，正文尚未完成，请检查未完成稿后重试或补写。",
                        code="output_truncated", finish_reason=reason,
                    )
                if reason == "content_filter":
                    raise LLMStreamIncompleteError(
                        "模型服务中止了内容输出，正文尚未完成。",
                        code="output_filtered", finish_reason=reason,
                    )
                if reason != "stop":
                    raise LLMStreamIncompleteError(
                        "模型服务未正常完成正文输出，请检查未完成稿。",
                        code="unsupported_finish_reason",
                        finish_reason=reason if isinstance(reason, str) else None,
                    )
                finish_reason = reason

        if finish_reason != "stop" and not saw_done:
            raise LLMStreamIncompleteError(
                "模型连接已结束，但未收到完成标记，正文可能不完整。", code="stream_incomplete"
            )
        if not has_text:
            raise LLMStreamIncompleteError(
                "模型未返回有效正文，请重试。", code="empty_output", finish_reason=finish_reason
            )
