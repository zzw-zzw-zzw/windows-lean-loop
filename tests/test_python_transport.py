import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lean_loop.api import ApiError, call_model, effective_api_transport
from lean_loop.config import ApiConfig


class PythonTransportTests(unittest.TestCase):
    def _config(self, server: ThreadingHTTPServer, **changes: object) -> ApiConfig:
        values = {
            "api_base": f"http://127.0.0.1:{server.server_port}/v1",
            "api_key": "python-transport-secret",
            "model": "test-model",
            "mode": "responses",
            "timeout_seconds": 10,
            "curl_executable": "curl.exe",
            "api_transport": "python",
            "reasoning_effort": "high",
        }
        values.update(changes)
        return ApiConfig(**values)

    def test_auto_uses_python_on_windows_and_preserves_explicit_curl(self) -> None:
        config = ApiConfig(
            api_base="https://example.invalid",
            api_key="secret",
            model="model",
            mode="responses",
            timeout_seconds=10,
            curl_executable="curl.exe",
        )
        self.assertEqual(
            effective_api_transport(config), "python" if os.name == "nt" else "curl"
        )
        self.assertEqual(
            effective_api_transport(
                ApiConfig(**{**config.__dict__, "api_transport": "curl"})
            ),
            "curl",
        )

    def test_responses_sse_uses_python_transport_and_reports_progress(self) -> None:
        requests: list[dict[str, object]] = []
        authorization: list[str] = []
        progress: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(length)))
                authorization.append(self.headers.get("Authorization", ""))
                body = (
                    'event: response.created\n'
                    'data: {"type":"response.created"}\n\n'
                    'event: response.reasoning_summary_text.delta\n'
                    'data: {"type":"response.reasoning_summary_text.delta","delta":"working"}\n\n'
                    'event: response.output_text.delta\n'
                    'data: {"type":"response.output_text.delta","delta":"{\\"content\\":\\"example : True := by trivial\\\\n\\"}"}\n\n'
                    'event: response.completed\n'
                    'data: {"type":"response.completed"}\n\n'
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        class Control:
            def cancel_requested(self) -> bool:
                return False

            def process_started(self, pid: int, kind: str) -> None:
                return

            def process_finished(self, pid: int) -> None:
                return

            def process_progress(self, kind: str, details: dict[str, object]) -> None:
                progress.append(str(details.get("event")))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(
                    self._config(server),
                    "prove it",
                    Path(directory),
                    process_control=Control(),
                )
            self.assertEqual(content, "example : True := by trivial\n")
            self.assertEqual(authorization, ["Bearer python-transport-secret"])
            self.assertTrue(requests[0]["stream"])
            self.assertIn("transport.selected", progress)
            self.assertIn("response.output_text.delta", progress)
            self.assertIn("response.completed", progress)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_chat_completions_and_http_errors_use_same_transport(self) -> None:
        request_count = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                nonlocal request_count
                request_count += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                if request_count == 1:
                    body = json.dumps(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "content": '{"content":"example : True := by trivial\\n"}'
                                    }
                                }
                            ]
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                else:
                    body = b'{"error":{"message":"bad key"}}'
                    self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = self._config(
                server,
                mode="chat-completions",
                provider_kind="deepseek",
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(config, "prove it", Path(directory))
                self.assertEqual(content, "example : True := by trivial\n")
                with self.assertRaises(ApiError) as context:
                    call_model(config, "prove it", Path(directory))
            self.assertIn("python exit 22", str(context.exception))
            self.assertIn("HTTP 401", str(context.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
