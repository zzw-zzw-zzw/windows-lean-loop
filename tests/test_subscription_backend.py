from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.agent_protocol import AgentRequest, AgentRuntime
from lean_loop.api import extract_file_content
from lean_loop.config import ApiConfig
from lean_loop.process_control import ProcessCancelled, run_controlled_process
from lean_loop.subscription_backend import (
    ClaudeSubscriptionBackend,
    CodexSubscriptionBackend,
    SubscriptionBackendError,
    inspect_subscription_backend,
)


def _config(model: str, effort: str = "low", timeout: int = 5) -> ApiConfig:
    return ApiConfig(
        api_base="",
        api_key="",
        model=model,
        mode="subscription",
        timeout_seconds=timeout,
        curl_executable="curl.exe",
        reasoning_effort=effort,
    )


def _request(output_type: str = "json") -> AgentRequest:
    return AgentRequest(
        request_id="request-1",
        sequence=1,
        role="planner",
        run_id="run-1",
        phase="plan",
        output_type=output_type,  # type: ignore[arg-type]
        model="model",
        reasoning_effort="low",
        system_prompt="SYSTEM SECRET-FREE PROMPT",
        user_prompt="USER PROMPT",
    )


FAKE_CLI = r"""
import json
import os
import pathlib
import sys
import time

args = sys.argv[1:]
provider = os.environ["FAKE_PROVIDER"]
mode = os.environ.get("FAKE_MODE", "success")

if "--version" in args:
    print("fake-cli 1.2.3")
    raise SystemExit(0)

if provider == "codex" and "exec" in args and "--help" in args:
    print("--json --ephemeral --ignore-user-config --ignore-rules "
          "--skip-git-repo-check --sandbox --model -C")
    raise SystemExit(0)

if provider == "codex" and "--help" in args:
    print("--ask-for-approval")
    raise SystemExit(0)

if provider == "claude" and "--help" in args:
    print("--output-format --model --effort --permission-mode --tools "
          "--no-session-persistence --safe-mode --disable-slash-commands "
          "--no-chrome --strict-mcp-config "
          "--setting-sources --prompt-suggestions")
    raise SystemExit(0)

if provider == "codex" and "login" in args and "status" in args:
    if mode == "not-authenticated":
        print("Not logged in", file=sys.stderr)
        raise SystemExit(1)
    print("Logged in using ChatGPT", file=sys.stderr)
    raise SystemExit(0)

if provider == "codex" and "debug" in args and "models" in args:
    print(json.dumps({
        "models": [{
            "slug": "gpt-test-pinned",
            "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
        }]
    }))
    raise SystemExit(0)

if provider == "claude" and "auth" in args and "status" in args:
    print(json.dumps({
        "loggedIn": True,
        "authMethod": os.environ.get("FAKE_AUTH_METHOD", "claude.ai"),
        "apiProvider": "firstParty",
    }))
    raise SystemExit(0)

if mode == "sleep":
    time.sleep(30)

if mode == "rate-limit":
    print("usage limit reached api_key=sk-secret-value", file=sys.stderr)
    raise SystemExit(1)

if mode == "model-unavailable":
    print("model is not available", file=sys.stderr)
    raise SystemExit(1)

if mode == "subscription-unavailable":
    print("HTTP 403: subscription unavailable", file=sys.stderr)
    raise SystemExit(1)

if mode == "nonzero":
    print("unexpected client failure", file=sys.stderr)
    raise SystemExit(9)

if mode == "tool-events-nonzero" and provider == "codex":
    print(json.dumps({
        "type": "item.completed",
        "item": {
            "id": "command",
            "type": "command_execution",
            "command": "echo failed",
            "status": "failed",
            "exit_code": 9,
        },
    }))
    print("tool execution failed", file=sys.stderr)
    raise SystemExit(9)

if mode == "malformed":
    print("not-json")
    raise SystemExit(0)

payload = {
    "argv": args,
    "cwd": str(pathlib.Path.cwd()),
    "api_keys_present": any(
        os.environ.get(name)
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY")
    ),
    "project_environment_present": any(
        os.environ.get(name) for name in ("PYTHONPATH", "VIRTUAL_ENV", "PWD", "OLDPWD")
    ),
    "external_overrides_present": any(
        os.environ.get(name)
        for name in (
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CUSTOM_GATEWAY",
            "MODEL_ENDPOINT",
        )
    ),
    "generic_tokens_present": any(
        os.environ.get(name) for name in ("TUSHARE_TOKEN", "GH_TOKEN", "TOKEN")
    ),
}
result = json.dumps(payload, separators=(",", ":"))
requested_model = args[args.index("--model") + 1] if "--model" in args else ""
if mode == "empty":
    result = ""
if mode == "output-too-large":
    result = "x" * 70000
if os.environ.get("FAKE_OUTPUT_TYPE") == "lean_file":
    result = "import Mathlib\n\nexample : True := by\n  trivial\n"
if os.environ.get("FAKE_MUTATE_PATH"):
    pathlib.Path(os.environ["FAKE_MUTATE_PATH"]).write_text("modified", encoding="utf-8")
if mode == "tool-events":
    pathlib.Path("artifact.txt").write_text("sandbox-only", encoding="utf-8")

if provider == "codex":
    if mode == "top-level-error":
        print(json.dumps({"type": "error", "message": "reconnecting"}))
    if mode == "nonfatal-item-error":
        print(json.dumps({
            "type": "item.completed",
            "item": {"id": "e", "type": "error", "message": "transport fallback"},
        }))
    if mode == "tool-events":
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "id": "command",
                "type": "command_execution",
                "command": "echo sandbox-only > artifact.txt",
                "status": "completed",
                "exit_code": 0,
            },
        }))
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "id": "mcp",
                "type": "mcp_tool_call",
                "server": "fixture",
                "tool": "lookup",
                "status": "completed",
            },
        }))
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "id": "web",
                "type": "web_search",
                "query": "Lean documentation",
                "status": "completed",
            },
        }))
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "id": "file",
                "type": "file_change",
                "changes": [{"path": "artifact.txt", "kind": "add"}],
                "status": "completed",
            },
        }))
    if mode == "sandbox-boundary":
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "id": "file",
                "type": "file_change",
                "changes": [{"path": "../outside.txt", "kind": "add"}],
                "status": "completed",
            },
        }))
    print(json.dumps({"type": "thread.started", "thread_id": "thread"}))
    print(json.dumps({
        "type": "item.completed",
        "item": {"id": "a", "type": "agent_message", "text": result},
    }))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
else:
    print(json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "terminal_reason": "completed",
        "modelUsage": {
            os.environ.get("FAKE_ACTUAL_MODEL", requested_model): {
                "inputTokens": 1,
                "outputTokens": 1,
            },
            "claude-haiku-auxiliary-fixture": {
                "inputTokens": 1,
                "outputTokens": 1,
            },
        },
        "permission_denials": [],
    }))
"""


class SubscriptionBackendTests(unittest.TestCase):
    fixture_root = Path(__file__).parent / "fixtures" / "codex_cli_0_144_4"

    def _fixture_case(self, name: str) -> tuple[dict, str, str]:
        manifest = json.loads(
            (self.fixture_root / "manifest.json").read_text(encoding="utf-8")
        )
        case = dict(manifest["cases"][name])
        stdout = (self.fixture_root / case["stdout"]).read_text(encoding="utf-8")
        stderr = (
            (self.fixture_root / case["stderr"]).read_text(encoding="utf-8")
            if case.get("stderr")
            else ""
        )
        return case, stdout, stderr

    def _fake_cli(self, root: Path) -> Path:
        path = root / "fake_cli.py"
        path.write_text(textwrap.dedent(FAKE_CLI), encoding="utf-8")
        return path

    def _backend(self, provider: str, root: Path, **kwargs):
        cli = self._fake_cli(root)
        common = {
            "command_prefix": (sys.executable, str(cli)),
            "protected_root": root / "project",
            "base_environment": {
                **os.environ,
                "FAKE_PROVIDER": provider,
                "OPENAI_API_KEY": "must-be-removed",
                "ANTHROPIC_API_KEY": "must-be-removed",
                "ANTHROPIC_BASE_URL": "https://gateway.invalid",
                "CLAUDE_CODE_OAUTH_TOKEN": "must-be-removed",
                "CUSTOM_GATEWAY": "https://gateway.invalid",
                "MODEL_ENDPOINT": "https://endpoint.invalid",
                "TUSHARE_TOKEN": "fixture-secret",
                "GH_TOKEN": "fixture-secret",
                "TOKEN": "fixture-secret",
            },
            **kwargs,
        }
        (root / "project").mkdir(exist_ok=True)
        if provider == "codex":
            return CodexSubscriptionBackend(**common)
        return ClaudeSubscriptionBackend(**common)

    def test_codex_accepts_final_result_after_nonfatal_item_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            self.assertNotIn("OPENAI_API_KEY", backend.base_environment)
            self.assertNotIn("ANTHROPIC_API_KEY", backend.base_environment)
            self.assertNotIn("TUSHARE_TOKEN", backend.base_environment)
            self.assertNotIn("GH_TOKEN", backend.base_environment)
            self.assertNotIn("TOKEN", backend.base_environment)
            backend.base_environment["FAKE_MODE"] = "nonfatal-item-error"
            output = backend.invoke(
                _request(), _config("gpt-test-pinned"), root / "ignored"
            )

            self.assertEqual(output["api_keys_present"], False)
            self.assertEqual(output["project_environment_present"], False)
            self.assertEqual(output["external_overrides_present"], False)
            self.assertEqual(output["generic_tokens_present"], False)
            self.assertIn("--ephemeral", output["argv"])
            self.assertIn("workspace-write", output["argv"])
            self.assertIn(
                "sandbox_workspace_write.network_access=false", output["argv"]
            )
            self.assertIn(
                "sandbox_workspace_write.exclude_tmpdir_env_var=true", output["argv"]
            )
            self.assertIn(
                "sandbox_workspace_write.exclude_slash_tmp=true", output["argv"]
            )
            self.assertEqual(backend.last_metadata["requested_model"], "gpt-test-pinned")
            self.assertEqual(
                backend.last_metadata["requested_model_catalog_status"], "VALIDATED"
            )
            self.assertIsNone(backend.last_metadata["actual_model"])
            self.assertEqual(
                backend.last_metadata["actual_model_status"],
                "NOT_REPORTED_BY_CLIENT",
            )
            self.assertEqual(
                backend.last_metadata["model_identity_source"],
                "REQUESTED_MODEL_AND_OFFICIAL_CATALOG_ONLY",
            )
            self.assertEqual(backend.last_metadata["authentication_type"], "chatgpt")
            self.assertEqual(backend.last_metadata["nonfatal_event_count"], 1)
            self.assertEqual(
                backend.last_metadata["tool_execution_policy"],
                "TOOL_ENABLED_AGENT_SANDBOX",
            )
            expected_risk = {
                "filesystem_read_scope": "WINDOWS_BROAD_READ",
                "filesystem_write_scope": "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
                "read_isolation_status": (
                    "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX"
                ),
                "network_policy": "DISABLED",
            }
            for field, value in expected_risk.items():
                self.assertEqual(backend.last_metadata[field], value)
                self.assertEqual(
                    backend.last_metadata["sandbox_profile"][field], value
                )

    def test_codex_rejects_top_level_error_even_with_valid_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.base_environment["FAKE_MODE"] = "top-level-error"

            with self.assertRaises(SubscriptionBackendError) as raised:
                backend.invoke(
                    _request(), _config("gpt-test-pinned"), root / "ignored"
                )

            self.assertEqual(raised.exception.kind, "output_protocol_incompatible")
            self.assertEqual(raised.exception.metadata["exit_code"], 0)
            self.assertEqual(
                raised.exception.metadata["terminal_state"],
                "output_protocol_incompatible",
            )

    def test_codex_replays_current_and_legacy_final_result_formats(self) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        expected_message_counts = {
            "current_multi_message": 2,
            "legacy_single_message": 1,
            "tool_events_with_final": 1,
        }
        for name, message_count in expected_message_counts.items():
            with self.subTest(name=name):
                case, stdout, _ = self._fixture_case(name)
                final_text, metadata = backend._parse(
                    stdout,
                    requested_model="gpt-5.6-sol",
                    sandbox_root=Path.cwd(),
                    output_type="lean_file",
                )
                self.assertEqual(
                    extract_file_content(final_text), case["expected_content"]
                )
                self.assertEqual(metadata["final_result_candidate_count"], 1)
                self.assertEqual(
                    metadata["actual_model_status"], "NOT_REPORTED_BY_CLIENT"
                )
                self.assertEqual(metadata["agent_message_event_count"], message_count)

    def test_codex_fixture_protocol_failures_are_fail_closed(self) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        for name in (
            "no_final",
            "duplicate_final",
            "truncated_no_terminal",
            "malformed_event",
        ):
            with self.subTest(name=name):
                case, stdout, _ = self._fixture_case(name)
                with self.assertRaises(SubscriptionBackendError) as raised:
                    backend._parse(
                        stdout,
                        requested_model="gpt-5.6-sol",
                        sandbox_root=Path.cwd(),
                        output_type="lean_file",
                    )
                self.assertEqual(raised.exception.kind, case["expected_kind"])

    def test_codex_archives_current_stream_stderr_and_tool_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            case, stdout, stderr = self._fixture_case("current_multi_message")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=case["exit_code"],
                stdout=stdout,
                stderr=stderr,
            )
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            with patch.object(backend, "_run", return_value=completed):
                output = runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned"),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )

            self.assertEqual(output, case["expected_content"])
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            self.assertEqual((call_dir / "stdout.txt").read_text(encoding="utf-8"), stdout)
            self.assertEqual((call_dir / "stderr.txt").read_text(encoding="utf-8"), stderr)
            self.assertEqual((call_dir / "raw-output.txt").read_text(encoding="utf-8"), stdout)
            tool_events = json.loads(
                (call_dir / "tool-events.json").read_text(encoding="utf-8")
            )["events"]
            self.assertEqual(
                [event["event_type"] for event in tool_events],
                ["command_execution", "command_execution"],
            )
            self.assertEqual(
                [event["protocol_event_type"] for event in tool_events],
                ["item.started", "item.completed"],
            )

    def test_codex_nonzero_exit_rejects_valid_final_and_archives_streams(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            case, stdout, stderr = self._fixture_case("nonzero_with_valid_final")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=case["exit_code"],
                stdout=stdout,
                stderr=stderr,
            )
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            with (
                patch.object(backend, "_run", return_value=completed),
                self.assertRaises(SubscriptionBackendError) as raised,
            ):
                runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned"),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )

            self.assertEqual(raised.exception.kind, case["expected_kind"])
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            self.assertEqual((call_dir / "stdout.txt").read_text(encoding="utf-8"), stdout)
            self.assertEqual((call_dir / "stderr.txt").read_text(encoding="utf-8"), stderr)
            response = json.loads((call_dir / "response.json").read_text(encoding="utf-8"))
            self.assertEqual(response["status"], "error")
            self.assertEqual(response["error"]["kind"], "nonzero_exit")


    def test_codex_archives_tool_events_and_allows_sandbox_internal_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.base_environment["FAKE_MODE"] = "tool-events"

            output = backend.invoke(
                _request(), _config("gpt-test-pinned"), root / "ignored"
            )

            self.assertEqual(output["api_keys_present"], False)
            self.assertEqual(
                backend.last_metadata["tool_event_counts"],
                {
                    "command_execution": 1,
                    "file_change": 1,
                    "mcp_tool_call": 1,
                    "web_search": 1,
                },
            )
            self.assertEqual(len(backend.last_metadata["tool_events"]), 4)
            command = next(
                event
                for event in backend.last_metadata["tool_events"]
                if event["event_type"] == "command_execution"
            )
            self.assertEqual(command["command_summary"], "echo sandbox-only > artifact.txt")
            manifest = backend.last_metadata["sandbox_manifest"]
            self.assertEqual(manifest["filesystem_read_scope"], "WINDOWS_BROAD_READ")
            self.assertEqual(
                manifest["filesystem_write_scope"],
                "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
            )
            self.assertEqual(
                manifest["read_isolation_status"],
                "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
            )
            self.assertEqual(manifest["network_policy"], "DISABLED")
            self.assertEqual(manifest["protected_state_unchanged"], True)
            self.assertEqual(
                manifest["file_changes"],
                [{
                    "change": "created",
                    "path": "artifact.txt",
                    "sha256": "e4a050cf6646accf34674fd578f0919574845a633d4477b9757aded17608adf5",
                    "size_bytes": 12,
                }],
            )

    def test_codex_rejects_tool_event_that_escapes_sandbox_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.base_environment["FAKE_MODE"] = "sandbox-boundary"

            with self.assertRaises(SubscriptionBackendError) as raised:
                backend.invoke(_request(), _config("gpt-test-pinned"), root / "ignored")

            self.assertEqual(raised.exception.kind, "sandbox_boundary_violation")
            self.assertEqual(
                backend.last_metadata["terminal_state"], "sandbox_boundary_violation"
            )

    def test_codex_archives_tool_events_when_client_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.base_environment["FAKE_MODE"] = "tool-events-nonzero"

            with self.assertRaises(SubscriptionBackendError) as raised:
                backend.invoke(_request(), _config("gpt-test-pinned"), root / "ignored")

            self.assertEqual(raised.exception.kind, "nonzero_exit")
            self.assertEqual(
                backend.last_metadata["tool_event_counts"], {"command_execution": 1}
            )
            self.assertEqual(
                backend.last_metadata["tool_events"][0]["status"], "failed"
            )
            self.assertEqual(backend.last_metadata["exit_code"], 9)

    def test_claude_reports_actual_model_and_disables_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("claude", root)
            output = backend.invoke(
                _request(), _config("claude-test-5"), root / "ignored"
            )

            self.assertEqual(output["api_keys_present"], False)
            self.assertEqual(output["generic_tokens_present"], False)
            self.assertIn("--no-session-persistence", output["argv"])
            self.assertIn("--safe-mode", output["argv"])
            self.assertIn("--tools=", output["argv"])
            self.assertEqual(
                output["argv"][output["argv"].index("--permission-mode") + 1],
                "dontAsk",
            )
            self.assertEqual(
                output["argv"][output["argv"].index("--setting-sources") + 1],
                "local",
            )
            self.assertEqual(backend.last_metadata["actual_model"], "claude-test-5")
            self.assertEqual(backend.last_metadata["authentication_type"], "claude.ai")
            self.assertEqual(
                backend.last_metadata["final_result_event"],
                "result:success:completed",
            )
            self.assertEqual(backend.last_metadata["output_type"], "json")

    def test_claude_accepts_explicit_fable_only_when_usage_matches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("claude", root)
            output = backend.invoke(_request(), _config("fable"), root / "ignored")

            self.assertEqual(output["argv"][output["argv"].index("--model") + 1], "fable")
            self.assertEqual(backend.last_metadata["actual_model"], "fable")

            mismatched = self._backend("claude", root)
            mismatched.base_environment["FAKE_ACTUAL_MODEL"] = "different-model"
            with self.assertRaises(SubscriptionBackendError) as raised:
                mismatched.invoke(_request(), _config("fable"), root / "ignored")
            self.assertEqual(raised.exception.kind, "model_identity_required")

    def test_claude_rejects_non_subscription_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("claude", root)
            backend.base_environment["FAKE_AUTH_METHOD"] = "api_key"
            with self.assertRaises(SubscriptionBackendError) as raised:
                backend.inspect(model="fable", reasoning_effort="low")
            self.assertEqual(raised.exception.kind, "not_authenticated")

    def test_both_backends_return_complete_lean_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for provider, model in (
                ("codex", "gpt-test-pinned"),
                ("claude", "claude-test-5"),
            ):
                backend = self._backend(provider, root)
                backend.base_environment["FAKE_OUTPUT_TYPE"] = "lean_file"
                output = backend.invoke(
                    _request("lean_file"), _config(model), root / "ignored"
                )
                self.assertEqual(
                    output,
                    "import Mathlib\n\nexample : True := by\n  trivial\n",
                )

    def test_errors_are_classified_and_credentials_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for mode, expected in (
                ("rate-limit", "usage_limit"),
                ("model-unavailable", "model_unavailable"),
                ("subscription-unavailable", "subscription_unavailable"),
                ("malformed", "malformed_output"),
            ):
                backend = self._backend("claude", root)
                backend.base_environment["FAKE_MODE"] = mode
                backend.base_environment["FAKE_SECRET"] = "sk-secret-value"
                with self.assertRaises(SubscriptionBackendError) as raised:
                    backend.invoke(
                        _request(),
                        _config("claude-test-5"),
                        root / "ignored",
                    )
                self.assertEqual(raised.exception.kind, expected)
                self.assertNotIn("sk-secret-value", str(raised.exception))
                self.assertLessEqual(len(raised.exception.raw_output), 65536)

    def test_bounded_output_and_protected_state_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = self._backend("claude", root)
            oversized.base_environment["FAKE_MODE"] = "output-too-large"
            with self.assertRaises(SubscriptionBackendError) as raised:
                oversized.invoke(
                    _request(), _config("claude-test-5"), root / "ignored"
                )
            self.assertEqual(raised.exception.kind, "output_too_large")
            self.assertLessEqual(len(raised.exception.raw_output), 65536)

            target = root / "project" / "Main.lean"
            target.write_text("original", encoding="utf-8")
            mutating = self._backend("codex", root)
            mutating.protected_target = target.resolve()
            mutating.base_environment["FAKE_MUTATE_PATH"] = str(target)
            with self.assertRaises(SubscriptionBackendError) as raised:
                mutating.invoke(
                    _request(), _config("gpt-test-pinned"), root / "ignored"
                )
            self.assertEqual(raised.exception.kind, "side_effect_detected")

    def test_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.base_environment["FAKE_MODE"] = "sleep"
            with self.assertRaises(SubscriptionBackendError) as raised:
                backend.invoke(
                    _request(), _config("gpt-test-pinned", timeout=1), root / "ignored"
                )
            self.assertEqual(raised.exception.kind, "timeout")

    def test_parent_exception_terminates_process_tree(self) -> None:
        class BrokenProcess:
            pid = 4242
            returncode = None

            def poll(self):
                return None

            def communicate(self, **kwargs):
                raise RuntimeError("parent collection failed")

        process = BrokenProcess()
        with (
            patch("lean_loop.process_control.subprocess.Popen", return_value=process),
            patch("lean_loop.process_control.terminate_process_tree") as terminate,
            self.assertRaisesRegex(RuntimeError, "parent collection failed"),
        ):
            run_controlled_process(
                ["fake-cli"],
                timeout_seconds=5,
                kind="parent-exception",
            )
        terminate.assert_called_once_with(process)

    def test_missing_cli_not_authenticated_empty_and_nonzero_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = CodexSubscriptionBackend(
                command_prefix=(str(root / "missing-cli.exe"),),
                protected_root=root,
                protected_target=root / "Main.lean",
            )
            with self.assertRaises(SubscriptionBackendError) as raised:
                missing.inspect(model="gpt-test-pinned", reasoning_effort="low")
            self.assertEqual(raised.exception.kind, "cli_missing")

            for mode, expected in (
                ("not-authenticated", "not_authenticated"),
                ("empty", "empty_output"),
                ("nonzero", "nonzero_exit"),
            ):
                backend = self._backend("codex", root)
                backend.base_environment["FAKE_MODE"] = mode
                with self.assertRaises(SubscriptionBackendError) as raised:
                    backend.invoke(
                        _request(), _config("gpt-test-pinned"), root / "ignored"
                    )
                self.assertEqual(raised.exception.kind, expected)

    def test_cancel_propagates_after_process_tree_cleanup(self) -> None:
        class Control:
            cancel = False
            started: list[int] = []
            finished: list[int] = []

            def cancel_requested(self) -> bool:
                return self.cancel

            def process_started(self, pid: int, kind: str) -> None:
                self.started.append(pid)

            def process_finished(self, pid: int) -> None:
                self.finished.append(pid)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = Control()
            backend = self._backend("codex", root, process_control=control)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            backend.base_environment["FAKE_MODE"] = "sleep"

            def cancel() -> None:
                time.sleep(0.3)
                control.cancel = True

            thread = threading.Thread(target=cancel)
            thread.start()
            with self.assertRaises(ProcessCancelled):
                backend.invoke(
                    _request(), _config("gpt-test-pinned", timeout=10), root / "ignored"
                )
            thread.join()
            self.assertEqual(control.started, control.finished)
            self.assertEqual(backend.last_metadata["terminal_state"], "cancelled")

    def test_doctor_distinguishes_ready_clients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex = self._backend("codex", root)
            claude = self._backend("claude", root)

            codex_report = inspect_subscription_backend(
                codex, model="gpt-test-pinned", reasoning_effort="high"
            )
            claude_report = inspect_subscription_backend(
                claude, model="claude-test-5", reasoning_effort="low"
            )

            self.assertEqual(codex_report["status"], "ready")
            self.assertEqual(codex_report["cli_version"], "fake-cli 1.2.3")
            self.assertEqual(codex_report["authentication_type"], "chatgpt")
            self.assertEqual(
                codex_report["filesystem_read_scope"], "WINDOWS_BROAD_READ"
            )
            self.assertEqual(
                codex_report["filesystem_write_scope"],
                "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
            )
            self.assertEqual(
                codex_report["read_isolation_status"],
                "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
            )
            self.assertEqual(codex_report["network_policy"], "DISABLED")
            self.assertEqual(claude_report["status"], "ready")
            self.assertEqual(claude_report["authentication_type"], "claude.ai")

    def test_readme_discloses_legacy_windows_broad_read_risk(self) -> None:
        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

        self.assertIn("filesystem_read_scope=WINDOWS_BROAD_READ", readme)
        self.assertIn(
            "read_isolation_status=NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
            readme,
        )
        self.assertIn("本项目不保证用户目录或认证目录在操作系统层面不可读", readme)
        self.assertNotIn("两者均不读取、复制或转换本地认证文件", readme)

    def test_unsupported_reasoning_is_rejected_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            with self.assertRaises(SubscriptionBackendError) as raised:
                backend.invoke(
                    _request(),
                    _config("gpt-test-pinned", effort="ultra"),
                    root / "ignored",
                )
            self.assertEqual(raised.exception.kind, "unsupported_reasoning")


if __name__ == "__main__":
    unittest.main()
