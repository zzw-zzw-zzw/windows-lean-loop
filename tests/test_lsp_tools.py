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
    apply_line_local_snippet,
    classify_local_target,
    prepare_local_snippets,
    resolve_rg_path,
    select_local_attempt,
    validate_local_repair_proposal,
)
from lean_loop.mcp_client import McpError, StdioMcpClient
from lean_loop.mathlib_search import retrieval_prompt_block


class LspToolsTests(unittest.TestCase):
    def test_local_repair_context_does_not_treat_timed_out_diagnostics_as_clean(self) -> None:
        class FakeClient:
            def call_tool(self, name, arguments, *, timeout_seconds=None):
                del arguments, timeout_seconds
                if name == "lean_diagnostic_messages":
                    return {
                        "result": {
                            "partial": True,
                            "timed_out": True,
                            "items": [],
                        }
                    }
                raise AssertionError(f"Unexpected tool: {name}")

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = "example : True := by\n  trivial\n"
            path = project / "Main.lean"
            path.write_text(source, encoding="utf-8")
            collector = LspEvidenceCollector(
                project=project,
                settings=LspSettings(mode="stdio"),
            )
            collector.client = FakeClient()
            collector.available_tools = {
                "lean_diagnostic_messages",
                "lean_goal",
                "lean_code_actions",
                "lean_multi_attempt",
            }
            context = collector.local_repair_context(
                file_path=path,
                source=source,
            )

        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["reason"], "diagnostics_timed_out")

    def test_local_repair_context_requires_an_open_proof_goal(self) -> None:
        class FakeClient:
            def call_tool(self, name, arguments, *, timeout_seconds=None):
                del arguments, timeout_seconds
                if name == "lean_diagnostic_messages":
                    return {
                        "result": {
                            "partial": False,
                            "timed_out": False,
                            "items": [{
                                "severity": "error",
                                "message": "bad declaration continuation",
                                "line": 2,
                                "column": 3,
                            }],
                        }
                    }
                if name == "lean_goal":
                    return {
                        "result": {
                            "status": "no_goal_at_position",
                            "goals": None,
                        }
                    }
                raise AssertionError(f"Unexpected tool: {name}")

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            source = "theorem demo\n    : True := by\n  trivial\n"
            path = project / "Main.lean"
            path.write_text(source, encoding="utf-8")
            collector = LspEvidenceCollector(
                project=project,
                settings=LspSettings(mode="stdio"),
            )
            collector.client = FakeClient()
            collector.available_tools = {
                "lean_diagnostic_messages",
                "lean_goal",
                "lean_code_actions",
                "lean_multi_attempt",
            }
            context = collector.local_repair_context(
                file_path=path,
                source=source,
            )

        self.assertEqual(context["status"], "unsupported")
        self.assertEqual(
            context["reason"], "diagnostic_has_no_open_proof_goal"
        )

    def test_local_repair_proposal_filters_unsafe_and_duplicate_snippets(self) -> None:
        value = validate_local_repair_proposal(
            {
                "snippets": [
                    "exact True.intro",
                    "exact True.intro",
                    "sorry",
                    "trivial",
                ],
                "reason": "two safe choices",
            },
            max_candidates=6,
        )
        self.assertEqual(value["snippets"], ["exact True.intro", "trivial"])

    def test_local_attempt_selection_prefers_completed_safe_result(self) -> None:
        selected = select_local_attempt(
            {
                "items": [
                    {
                        "snippet": "apply And.intro",
                        "goals": ["case left => True"],
                        "diagnostics": [],
                    },
                    {
                        "snippet": "trivial",
                        "goals": [],
                        "diagnostics": [],
                        "proof_status": "Completed",
                    },
                    {
                        "snippet": "exact missing",
                        "goals": [],
                        "diagnostics": [{"severity": "error"}],
                    },
                ]
            }
        )
        self.assertEqual(selected["snippet"], "trivial")

    def test_local_attempt_accepts_safe_partial_progress_and_nested_result(self) -> None:
        partial = select_local_attempt(
            {
                "items": [
                    {
                        "snippet": "apply And.intro",
                        "diagnostics": [],
                        "goals": ["case left => True"],
                    }
                ]
            },
            target_diagnostic={
                "severity": "error", "message": "unknown", "line": 2, "column": 3
            },
        )
        self.assertEqual(partial["snippet"], "apply And.intro")
        selected = select_local_attempt(
            {
                "result": {
                    "items": [
                        {
                            "snippet": "trivial",
                            "diagnostics": [],
                            "goals": [],
                        }
                    ]
                }
            }
        )
        self.assertEqual(selected["snippet"], "trivial")

    def test_local_attempt_rejects_unchanged_target_and_new_hard_error(self) -> None:
        target = {
            "severity": "error",
            "message": "Unknown identifier `missing`",
            "line": 2,
            "column": 3,
        }
        self.assertIsNone(select_local_attempt(
            {"items": [{"snippet": "exact missing", "goals": [], "diagnostics": [target]}]},
            target_diagnostic=target,
            baseline_diagnostics=[target],
        ))
        self.assertIsNone(select_local_attempt(
            {"items": [{
                "snippet": "exact bad",
                "goals": [],
                "diagnostics": [{
                    "severity": "error", "message": "unexpected token", "line": 2, "column": 4
                }],
            }]},
            target_diagnostic=target,
            baseline_diagnostics=[target],
        ))

    def test_proof_tail_target_is_validated_as_a_whole_line(self) -> None:
        source_line = "    lhs = rhs := by ac_rfl"
        self.assertEqual(classify_local_target(source_line), "proof_tail")
        self.assertEqual(
            prepare_local_snippets(
                source_line=source_line,
                target_kind="proof_tail",
                snippets=["by\n  ring", "simp"],
            ),
            ["lhs = rhs := by\n  ring", "lhs = rhs := by\n  simp"],
        )
        self.assertEqual(classify_local_target("    : True"), "unsupported")
        self.assertEqual(classify_local_target("      have h : True := by trivial"), "proof_tail")
        self.assertEqual(classify_local_target("      exact h"), "tactic_line")
        self.assertEqual(classify_local_target("      Function.update x i g"), "unsupported")

    def test_rg_explicit_path_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "rg.exe"
            executable.write_bytes(b"")
            self.assertEqual(resolve_rg_path(str(executable)), str(executable.resolve()))

    def test_qualified_local_search_retries_with_basename(self) -> None:
        class FakeClient:
            def call_tool(self, name, arguments, *, timeout_seconds=None):
                del timeout_seconds
                if name == "lean_diagnostic_messages":
                    return {"result": {"items": [], "partial": False, "timed_out": False}}
                if name == "lean_local_search":
                    if arguments["query"] == "Real.pi_gt_three":
                        return {"items": []}
                    self.assert_basename(arguments["query"])
                    return {"items": [{
                        "name": "Real.pi_gt_three",
                        "kind": "theorem",
                        "file": "Mathlib/Analysis/Real/Pi/Bounds.lean",
                    }]}
                raise AssertionError(name)

            @staticmethod
            def assert_basename(query):
                if query != "pi_gt_three":
                    raise AssertionError(query)

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / "Main.lean"
            target.write_text("example : True := by trivial\n", encoding="utf-8")
            collector = LspEvidenceCollector(
                project=project,
                settings=LspSettings(mode="stdio", remote_search=False),
            )
            collector.client = FakeClient()  # type: ignore[assignment]
            collector.available_tools = {
                "lean_diagnostic_messages", "lean_local_search"
            }
            evidence = collector.collect(
                file_path=target,
                source=target.read_text(encoding="utf-8"),
                diagnostics="",
                search_terms=["Real.pi_gt_three"],
            )
            row = evidence["search"][0]
            self.assertEqual(row["local_fallback_query"], "pi_gt_three")
            self.assertEqual(row["local"]["items"][0]["name"], "Real.pi_gt_three")

    def test_line_local_patch_preserves_indentation_and_rejects_structure(self) -> None:
        source = "example : True := by\n  exact missing\n"
        updated = apply_line_local_snippet(
            source,
            line=2,
            expected_line="  exact missing",
            snippet="first\ntrivial",
        )
        self.assertEqual(updated, "example : True := by\n  first\n  trivial\n")
        with self.assertRaisesRegex(ValueError, "structural"):
            apply_line_local_snippet(
                source,
                line=1,
                expected_line="example : True := by",
                snippet="trivial",
            )
        for structural in (
            "private theorem hidden : True := by",
            "noncomputable section",
            "instance : Inhabited Nat where",
        ):
            with self.subTest(structural=structural), self.assertRaisesRegex(
                ValueError, "structural"
            ):
                apply_line_local_snippet(
                    structural + "\n  trivial\n",
                    line=1,
                    expected_line=structural,
                    snippet="trivial",
                )

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
