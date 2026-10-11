import json
import unittest

from agent_runtime.model_proxy import ModelProxyError, prepare_request, sse_completion


class ModelRequestTests(unittest.TestCase):
    def test_remote_fetch_inputs_and_provider_parameters_are_rejected(self):
        for extra in ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://169.254.169.254"}}]}]},
                      {"base_url": "https://other.invalid"}, {"api_key": "no"}, {"model": "unapproved"}):
            with self.assertRaises(ModelProxyError):
                prepare_request({"model": "allowed", "messages": [{"role": "user", "content": "hello"}], **extra}, "allowed")

    def test_input_and_output_are_bounded_before_reservation(self):
        for extra in ({"max_tokens": 100000}, {"max_tokens": True}, {"messages": []}):
            with self.assertRaises(ModelProxyError):
                prepare_request({"model": "allowed", "messages": [{"role": "user", "content": "hello"}], **extra}, "allowed")
        with self.assertRaises(ModelProxyError):
            prepare_request({"model": "allowed", "messages": [{"role": "user", "content": "a" * 1000}]}, "allowed", max_bytes=100)

    def test_streaming_is_buffered_and_usage_reserved_conservatively(self):
        payload, streaming, reserved = prepare_request({"model": "allowed", "messages": [{"role": "user", "content": "hello"}], "stream": True, "max_tokens": 100}, "allowed")
        self.assertTrue(streaming)
        self.assertFalse(payload["stream"])
        self.assertGreater(reserved, 4196)

    def test_sse_contains_tool_indices_finish_and_usage(self):
        events = list(sse_completion({"id": "c1", "model": "allowed", "choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 12}}))
        first = json.loads(events[0].removeprefix("data: "))
        self.assertEqual(first["choices"][0]["delta"]["tool_calls"][0]["index"], 0)
        self.assertEqual(events[-1], "data: [DONE]\n\n")
        self.assertIn('"total_tokens": 12', events[-2])
