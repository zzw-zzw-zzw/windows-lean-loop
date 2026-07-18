import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.api import (
    MissingFinalOutputError,
    call_model_json,
    extract_file_content,
    extract_json_object,
    extract_response_text,
    _request_payload,
)
from lean_loop.config import ApiConfig


class ApiParsingTests(unittest.TestCase):
    def test_extract_chat_text(self) -> None:
        response = {"choices": [{"message": {"content": '{"content":"example : True := by trivial\\n"}'}}]}
        text = extract_response_text(response, "chat-completions")
        self.assertIn("example", text)

    def test_extract_responses_text(self) -> None:
        response = {"output": [{"content": [{"type": "output_text", "text": '{"content":"x\\n"}'}]}]}
        self.assertEqual(extract_response_text(response, "responses"), '{"content":"x\\n"}')

    def test_extract_content_from_fence(self) -> None:
        value = extract_file_content('```json\n{"content":"example : True := by trivial\\n"}\n```')
        self.assertEqual(value, "example : True := by trivial\n")

    def test_extract_raw_lean_without_json_wrapper(self) -> None:
        value = extract_file_content(
            "import Mathlib\n\ntheorem goal : True := by trivial\n"
        )
        self.assertEqual(value, "import Mathlib\n\ntheorem goal : True := by trivial\n")

    def test_extract_lean_markdown_fence(self) -> None:
        value = extract_file_content(
            "Here is the file:\n```lean\nimport Mathlib\ntheorem goal : True := by trivial\n```"
        )
        self.assertIn("theorem goal", value)

    def test_extract_content_wrapper_with_literal_newlines(self) -> None:
        value = extract_file_content(
            '{"content":"import Mathlib\ntheorem goal : True := by trivial\n"}'
        )
        self.assertIn("theorem goal", value)

    def test_json_call_retries_malformed_final_output(self) -> None:
        config = ApiConfig(
            api_base="http://example.invalid",
            api_key="x",
            model="test",
            mode="responses",
            timeout_seconds=10,
            curl_executable="curl.exe",
            reasoning_effort="low",
            empty_response_retries=0,
        )
        with patch(
            "lean_loop.api.call_model_text",
            side_effect=["not json", '{"verdict":"retry"}'],
        ) as mocked:
            value = call_model_json(config, "system", "user", Path("."))
        self.assertEqual(value, {"verdict": "retry"})
        self.assertEqual(mocked.call_count, 2)

    def test_extract_generic_json_object(self) -> None:
        value = extract_json_object('result:\n```json\n{"verdict":"retry"}\n```')
        self.assertEqual(value, {"verdict": "retry"})

    def test_reasoning_only_error_is_sanitized(self) -> None:
        response = {
            "id": "resp_test",
            "status": "completed",
            "model": "test-model",
            "output": [
                {
                    "type": "reasoning",
                    "encrypted_content": "secret-encrypted-payload",
                }
            ],
            "usage": {"total_tokens": 42},
        }
        with self.assertRaises(MissingFinalOutputError) as context:
            extract_response_text(response, "responses")
        message = str(context.exception)
        self.assertIn("resp_test", message)
        self.assertIn("reasoning", message)
        self.assertNotIn("secret-encrypted-payload", message)

    def test_deepseek_payload_uses_official_chat_parameters(self) -> None:
        config = ApiConfig(
            api_base="https://api.deepseek.com",
            api_key="secret",
            model="deepseek-reasoner",
            mode="chat-completions",
            timeout_seconds=60,
            curl_executable="curl.exe",
            reasoning_effort="high",
            provider_kind="deepseek",
        )
        payload = _request_payload(config, "system", "user")
        self.assertEqual(payload["max_tokens"], config.max_output_tokens)
        self.assertNotIn("store", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("max_completion_tokens", payload)


if __name__ == "__main__":
    unittest.main()
