from __future__ import annotations

import tempfile
import unittest
import sys
import time
from pathlib import Path

from lean_loop.lsp_tools import (
    LspEvidenceCollector,
    LspSettings,
    _hover_positions,
)
from lean_loop.mcp_client import McpError, StdioMcpClient
from lean_loop.mathlib_search import retrieval_prompt_block


class LspToolsTests(unittest.TestCase):
    def test_stdio_client_initializes_lists_tools_and_calls_tool(self) -> None:
        server = (
            "import json, sys\n"
            "for line in sys.stdin:\n"
            "    request = json.loads(line)\n"
            "    method = request.get('method')\n"
            "    request_id = request.get('id')\n"
            "    if method == 'initialize':\n"
            "        result = {'protocolVersion': '2025-06-18', 'serverInfo': {'name': 'fake'}}\n"
            "    elif method == 'tools/list':\n"
            "        result = {'tools': [{'name': 'fake_tool'}]}\n"
            "    elif method == 'tools/call':\n"
            "        result = {'content': [{'type': 'text', 'text': '{\\\"ok\\\":true}'}]}\n"
            "    else:\n"
            "        continue\n"
            "    if request_id is not None:\n"
            "        print(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            client = StdioMcpClient(
                command=sys.executable,
                args=["-c", server],
                cwd=Path(directory),
                startup_timeout_seconds=5,
                call_timeout_seconds=5,
            )
            client.start()
            self.assertEqual(client.server_info["name"], "fake")
            self.assertEqual(client.list_tools()[0]["name"], "fake_tool")
            self.assertEqual(client.call_tool("fake_tool", {}), {"ok": True})
            client.close()
            self.assertIsNone(client.process)

    def test_stdio_client_reports_early_server_exit_without_startup_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = StdioMcpClient(
                command=sys.executable,
                args=["-c", "raise SystemExit(3)"],
                cwd=Path(directory),
                startup_timeout_seconds=30,
                call_timeout_seconds=5,
            )
            started = time.monotonic()
            with self.assertRaisesRegex(McpError, "closed its output stream"):
                client.start()
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNone(client.process)

    def test_settings_default_to_disabled_and_validate_loopback_http(self) -> None:
        settings = LspSettings.from_values({})
        self.assertEqual(settings.mode, "off")
        self.assertEqual(settings.call_timeout_seconds, 60)
        self.assertEqual(
            LspSettings.from_values({"lsp_mode": "http"}).url,
            "http://127.0.0.1:8000/mcp",
        )
        with self.assertRaises(ValueError):
            LspSettings.from_values(
                {"lsp_mode": "http", "lsp_url": "https://example.com/mcp"}
            )

    def test_disabled_collector_is_safe_and_does_not_start_a_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / "Main.lean"
            target.write_text("example : True := by trivial\n", encoding="utf-8")
            collector = LspEvidenceCollector(
                project=project, settings=LspSettings.from_values({})
            )
            evidence = collector.collect(
                file_path=target,
                source=target.read_text(encoding="utf-8"),
                diagnostics="",
                search_terms=["True"],
            )
            self.assertEqual(evidence["session"]["status"], "disabled")
            collector.close()

    def test_collection_total_timeout_skips_remaining_tools(self) -> None:
        class FakeClient:
            server_info = {"name": "fake"}
            pid = None

            def __init__(self) -> None:
                self.calls = 0

            def call_tool(self, name, arguments, *, timeout_seconds=None):
                del name, arguments, timeout_seconds
                self.calls += 1
                return {}

            def close(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / "Main.lean"
            target.write_text("example : True := by trivial\n", encoding="utf-8")
            collector = LspEvidenceCollector(
                project=project,
                settings=LspSettings(mode="stdio"),
            )
            client = FakeClient()
            collector.client = client  # type: ignore[assignment]
            collector.available_tools = {
                "lean_diagnostic_messages",
                "lean_goal",
                "lean_hover_info",
                "lean_local_search",
            }
            evidence = collector.collect(
                file_path=target,
                source=target.read_text(encoding="utf-8"),
                diagnostics="",
                search_terms=["True"],
                total_timeout_seconds=0,
            )
            self.assertIn("collection_timeout", evidence)
            self.assertEqual(client.calls, 0)
            collector.close()

    def test_hover_positions_prioritize_diagnostic_and_search_identifiers(self) -> None:
        source = "import Mathlib\nexample : 3 < Real.pi := by exact Real.pi_gt_three\n"
        positions = _hover_positions(
            source,
            "Main.lean:2:35: error: unknown identifier `Real.pi_gt_three`",
            ["Real.pi_gt_three"],
        )
        self.assertEqual(positions[0][0], "Real.pi_gt_three")
        self.assertEqual(positions[0][1], 2)

    def test_lsp_evidence_is_rendered_with_mathlib_evidence(self) -> None:
        prompt = retrieval_prompt_block(
            {
                "hits": [],
                "module_checks": [],
                "import_suggestions": [],
                "lsp": {
                    "diagnostics": {"items": [{"message": "unknown identifier"}]},
                    "goals": [{"result": {"status": "goals"}}],
                    "hover": [{"result": {"info": "Real.pi_gt_three : 3 < Real.pi"}}],
                    "search": [{"query": "Real.pi_gt_three"}],
                },
            }
        )
        self.assertIn("Lean LSP evidence", prompt)
        self.assertIn("unknown identifier", prompt)
        self.assertIn("Real.pi_gt_three", prompt)


if __name__ == "__main__":
    unittest.main()
