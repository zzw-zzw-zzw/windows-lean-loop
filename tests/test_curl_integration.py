import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lean_loop.api import call_model
from lean_loop.config import ApiConfig


class _Handler(BaseHTTPRequestHandler):
    authorization = ""
    request_json = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self).authorization = self.headers.get("Authorization", "")
        type(self).request_json = json.loads(self.rfile.read(length))
        body = json.dumps(
            {
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"content":"example : True := by trivial\\n"}',
                            }
                        ]
                    }
                ]
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class CurlIntegrationTests(unittest.TestCase):
    def test_responses_request_through_curl(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ApiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-secret",
                model="test-model",
                mode="responses",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="xhigh",
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(config, "repair it", Path(directory))
            self.assertEqual(content, "example : True := by trivial\n")
            self.assertEqual(_Handler.authorization, "Bearer test-secret")
            self.assertEqual(_Handler.request_json["model"], "test-model")
            self.assertFalse(_Handler.request_json["store"])
            self.assertEqual(_Handler.request_json["reasoning"], {"effort": "xhigh"})
            self.assertEqual(_Handler.request_json["max_output_tokens"], 8192)
            self.assertTrue(_Handler.request_json["stream"])
        finally:
            server.shutdown()
            server.server_close()

    def test_responses_sse_stream_reports_progress(self) -> None:
        progress: list[tuple[str, dict[str, object]]] = []

        class Control:
            def cancel_requested(self) -> bool:
                return False

            def process_started(self, pid: int, kind: str) -> None:
                return

            def process_finished(self, pid: int) -> None:
                return

            def process_progress(self, kind: str, details: dict[str, object]) -> None:
                progress.append((kind, details))

        class StreamHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
                self.assert_stream = request.get("stream")
                rows = [
                    ("response.created", {"type": "response.created"}),
                    (
                        "response.reasoning_summary_text.delta",
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "delta": "working",
                        },
                    ),
                    (
                        "response.output_text.delta",
                        {
                            "type": "response.output_text.delta",
                            "delta": '{"content":"example : True := by trivial\\n"}',
                        },
                    ),
                    ("response.completed", {"type": "response.completed"}),
                ]
                body = "".join(
                    f"event: {event}\ndata: {json.dumps(value)}\n\n"
                    for event, value in rows
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ApiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-secret",
                model="test-model",
                mode="responses",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="high",
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(
                    config,
                    "repair it",
                    Path(directory),
                    process_control=Control(),
                )
            self.assertEqual(content, "example : True := by trivial\n")
            events = [str(details.get("event")) for _, details in progress]
            self.assertIn("response.created", events)
            self.assertIn("response.reasoning_summary_text.delta", events)
            self.assertIn("response.output_text.delta", events)
            self.assertIn("response.completed", events)
        finally:
            server.shutdown()
            server.server_close()

    def test_timeout_retries_lower_effort_without_lean_attempt(self) -> None:
        requests: list[dict[str, object]] = []

        class TimeoutHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(length)))
                if len(requests) == 1:
                    time.sleep(2)
                body = (
                    'event: response.output_text.delta\n'
                    'data: {"type":"response.output_text.delta","delta":"{\\"content\\":\\"example : True := by trivial\\\\n\\"}"}\n\n'
                    'event: response.completed\n'
                    'data: {"type":"response.completed"}\n\n'
                ).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), TimeoutHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ApiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-secret",
                model="test-model",
                mode="responses",
                timeout_seconds=1,
                curl_executable="curl.exe",
                reasoning_effort="high",
                api_timeout_retries=1,
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(config, "repair it", Path(directory))
            self.assertEqual(content, "example : True := by trivial\n")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["reasoning"], {"effort": "high"})
            self.assertEqual(requests[1]["reasoning"], {"effort": "medium"})
        finally:
            server.shutdown()
            server.server_close()

    def test_transient_502_retries_without_returning_a_candidate_failure(self) -> None:
        requests: list[dict[str, object]] = []

        class GatewayHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(length)))
                if len(requests) == 1:
                    body = b"<html><h1>502 Bad Gateway</h1></html>"
                    self.send_response(502)
                    self.send_header("Content-Type", "text/html")
                else:
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
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ApiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-secret",
                model="test-model",
                mode="chat-completions",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="high",
                api_timeout_retries=1,
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(config, "repair it", Path(directory))
            self.assertEqual(content, "example : True := by trivial\n")
            self.assertEqual(len(requests), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_reasoning_only_response_retries_at_lower_effort(self) -> None:
        requests: list[dict[str, object]] = []

        class RetryHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(length)))
                if len(requests) == 1:
                    response = {
                        "id": "resp_reasoning_only",
                        "status": "completed",
                        "model": "test-model",
                        "output": [
                            {
                                "type": "reasoning",
                                "encrypted_content": "must-not-leak",
                            }
                        ],
                        "usage": {"total_tokens": 10},
                    }
                else:
                    response = {
                        "output": [
                            {
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": '{"content":"example : True := by trivial\\n"}',
                                    }
                                ]
                            }
                        ]
                    }
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), RetryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ApiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-secret",
                model="test-model",
                mode="responses",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="high",
                empty_response_retries=1,
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(config, "repair it", Path(directory))
            self.assertEqual(content, "example : True := by trivial\n")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["reasoning"], {"effort": "high"})
            self.assertEqual(requests[1]["reasoning"], {"effort": "medium"})
        finally:
            server.shutdown()
            server.server_close()

    def test_stream_without_final_output_retries_at_lower_effort(self) -> None:
        requests: list[dict[str, object]] = []

        class EmptyStreamHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                requests.append(json.loads(self.rfile.read(length)))
                if len(requests) == 1:
                    body = (
                        'event: response.reasoning_summary_text.delta\n'
                        'data: {"type":"response.reasoning_summary_text.delta","delta":"working"}\n\n'
                        'event: response.completed\n'
                        'data: {"type":"response.completed"}\n\n'
                    ).encode("utf-8")
                else:
                    body = (
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

        server = ThreadingHTTPServer(("127.0.0.1", 0), EmptyStreamHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = ApiConfig(
                api_base=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-secret",
                model="test-model",
                mode="responses",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="high",
                empty_response_retries=1,
            )
            with tempfile.TemporaryDirectory() as directory:
                content = call_model(config, "repair it", Path(directory))
            self.assertEqual(content, "example : True := by trivial\n")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["reasoning"], {"effort": "high"})
            self.assertEqual(requests[1]["reasoning"], {"effort": "medium"})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
