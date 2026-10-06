"""流式模型协议边界：用 HTTP MockTransport 验证结束标记，禁止外部请求。"""
import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.infrastructure.ai.llm_provider import LLMProvider, LLMStreamIncompleteError


def _frame(content=None, finish_reason=None):
    return {"choices": [{"delta": {"content": content}, "finish_reason": finish_reason}]}


def _wire(*frames):
    return "".join(
        f"data: {frame if isinstance(frame, str) else json.dumps(frame, ensure_ascii=False)}\n\n"
        for frame in frames
    )


class LLMProviderCompletionTest(unittest.TestCase):
    def setUp(self):
        with patch("app.infrastructure.ai.llm_provider.get_settings", return_value=SimpleNamespace(
            openai_api_key="test-only-key", openai_base_url="https://llm.invalid", openai_model="test-model",
        )):
            self.provider = LLMProvider()

    @contextmanager
    def response(self, finish_reason):
        def handler(request):
            self.request_body = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "完整正文。"}, "finish_reason": finish_reason}]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with patch("app.infrastructure.ai.llm_provider.httpx.post", side_effect=client.post):
                yield

    def test_strict_completion_keeps_request_protocol_and_complete_body(self):
        with self.response("stop"):
            result = self.provider.complete([{"role": "user", "content": "修订"}], max_tokens=8192, require_complete=True)
        self.assertEqual(result, "完整正文。")
        self.assertEqual(self.request_body, {"model": "test-model", "messages": [{"role": "user", "content": "修订"}], "temperature": 0.7, "max_tokens": 8192})

    def test_strict_completion_rejects_truncated_filtered_or_unconfirmed_output(self):
        for reason in ("length", "content_filter", "tool_calls", None, "unknown"):
            with self.subTest(reason=reason), self.response(reason):
                with self.assertRaises(RuntimeError) as caught:
                    self.provider.complete([{"role": "user", "content": "修订"}], require_complete=True)
                self.assertNotIn("https://", str(caught.exception))

    def test_legacy_completion_remains_compatible_without_new_argument(self):
        with self.response(None):
            self.assertEqual(self.provider.complete([{"role": "user", "content": "检查"}]), "完整正文。")


class LLMProviderStreamTest(unittest.TestCase):
    def setUp(self):
        settings = SimpleNamespace(
            openai_api_key="test-only-key", openai_base_url="https://llm.invalid",
            openai_model="test-model",
        )
        with patch("app.infrastructure.ai.llm_provider.get_settings", return_value=settings):
            self.provider = LLMProvider()
        self.requests = []

    @contextmanager
    def _response(self, text, **params):
        def handle(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, text=text, headers={"Content-Type": "text/event-stream"})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with patch(
                "app.infrastructure.ai.llm_provider.httpx.stream", side_effect=client.stream
            ):
                yield self.provider.stream_complete([{"role": "user", "content": "写章"}], **params)

    def test_stop_keeps_last_delta_and_allows_usage_frame_and_done(self):
        frames = _wire(
            _frame("第一段。"), _frame("结尾。", "stop"),
            {"choices": [], "usage": {"completion_tokens": 20}}, "[DONE]",
        )
        with self._response(": keepalive\n\nevent: message\n" + frames) as stream:
            self.assertEqual(list(stream), ["第一段。", "结尾。"])

    def test_explicit_stop_is_complete_without_done_marker(self):
        with self._response(_wire(_frame("完整正文。", "stop"))) as stream:
            self.assertEqual(list(stream), ["完整正文。"])

    def test_done_only_gateway_is_supported(self):
        with self._response(_wire(_frame("兼容网关正文。"), "[DONE]")) as stream:
            self.assertEqual(list(stream), ["兼容网关正文。"])

    def test_nullable_delta_in_stop_frame_is_supported(self):
        with self._response(_wire(
            _frame("完整正文。"), {"choices": [{"delta": None, "finish_reason": "stop"}]},
            "[DONE]",
        )) as stream:
            self.assertEqual(list(stream), ["完整正文。"])

    def test_length_keeps_final_delta_then_reports_truncation(self):
        with self._response(_wire(_frame("前文。"), _frame("尚未说完", "length"), "[DONE]")) as stream:
            self.assertEqual(next(stream), "前文。")
            self.assertEqual(next(stream), "尚未说完")
            with self.assertRaises(LLMStreamIncompleteError) as caught:
                next(stream)
        self.assertEqual(caught.exception.code, "output_truncated")
        self.assertEqual(caught.exception.finish_reason, "length")
        self.assertIn("输出上限", str(caught.exception))

    def test_bare_eof_after_partial_text_is_not_success(self):
        with self._response(_wire(_frame("未完成稿"))) as stream:
            self.assertEqual(next(stream), "未完成稿")
            with self.assertRaises(LLMStreamIncompleteError) as caught:
                next(stream)
        self.assertEqual(caught.exception.code, "stream_incomplete")

    def test_filtered_tool_call_and_unknown_finish_are_not_success(self):
        for reason, code in (
            ("content_filter", "output_filtered"), ("tool_calls", "unsupported_finish_reason"),
            ("function_call", "unsupported_finish_reason"), ("unexpected", "unsupported_finish_reason"),
        ):
            with self.subTest(reason=reason):
                with self._response(_wire(_frame("片段", reason), "[DONE]")) as stream:
                    self.assertEqual(next(stream), "片段")
                    with self.assertRaises(LLMStreamIncompleteError) as caught:
                        next(stream)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.finish_reason, reason)

    def test_upstream_error_frame_is_not_ignored(self):
        with self._response(_wire({"error": {"message": "upstream failure"}}, "[DONE]")) as stream:
            with self.assertRaises(LLMStreamIncompleteError) as caught:
                list(stream)
        self.assertEqual(caught.exception.code, "upstream_error")

    def test_malformed_frames_are_not_ignored(self):
        malformed = (
            "data: {bad json}\n\n", "invalid SSE line\n\n", _wire([]), _wire({}),
            _wire({"choices": []}), _wire({"choices": "bad"}),
            _wire({"choices": [{"delta": "bad"}]}), _wire(_frame(["bad content"])),
        )
        for text in malformed:
            with self.subTest(text=text):
                with self._response(text + _wire("[DONE]")) as stream:
                    with self.assertRaises(LLMStreamIncompleteError) as caught:
                        list(stream)
                self.assertEqual(caught.exception.code, "invalid_stream_frame")

    def test_empty_or_whitespace_output_cannot_succeed(self):
        for frames in (_wire("[DONE]"), _wire(_frame(None, "stop")), _wire(_frame(" \n", "stop"))):
            with self.subTest(frames=frames):
                with self._response(frames) as stream:
                    with self.assertRaises(LLMStreamIncompleteError) as caught:
                        list(stream)
                self.assertEqual(caught.exception.code, "empty_output")

    def test_text_after_stop_is_rejected(self):
        with self._response(_wire(_frame("已结束。", "stop"), _frame("额外正文"), "[DONE]")) as stream:
            self.assertEqual(next(stream), "已结束。")
            with self.assertRaises(LLMStreamIncompleteError) as caught:
                next(stream)
        self.assertEqual(caught.exception.code, "invalid_stream_frame")

    def test_multiline_sse_data_is_parsed_as_one_frame(self):
        text = 'data: {"choices":\ndata: [{"delta":{"content":"正文。"},"finish_reason":"stop"}]}\n\n'
        with self._response(text) as stream:
            self.assertEqual(list(stream), ["正文。"])

    def test_explicit_budget_is_sent_without_changing_stream_default(self):
        for params, budget in (({}, 2048), ({"max_tokens": 8192}, 8192)):
            with self._response(_wire(_frame("正文。", "stop")), **params) as stream:
                self.assertEqual(list(stream), ["正文。"])
            self.assertEqual(self.requests[-1]["max_tokens"], budget)


class WritingTokenSettingsTest(unittest.TestCase):
    def test_default_and_environment_budget(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings(_env_file=None).writing_max_tokens, 8192)
        with patch.dict("os.environ", {"WRITING_MAX_TOKENS": "4096"}, clear=True):
            self.assertEqual(Settings(_env_file=None).writing_max_tokens, 4096)

    def test_budget_bounds_and_integer_validation(self):
        with patch.dict("os.environ", {}, clear=True):
            for value in (0, 255, 32769, 12.5):
                with self.subTest(value=value):
                    with self.assertRaises(ValidationError):
                        Settings(_env_file=None, writing_max_tokens=value)
            for value in (256, 32768):
                self.assertEqual(Settings(_env_file=None, writing_max_tokens=value).writing_max_tokens, value)


if __name__ == "__main__":
    unittest.main()
