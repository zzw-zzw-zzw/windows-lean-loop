from __future__ import annotations

import hashlib
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
    _redact_text,
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


def _jsonl_events(value: str) -> list[dict]:
    events = [json.loads(line) for line in value.splitlines()]
    if not all(isinstance(event, dict) for event in events):
        raise AssertionError("Expected JSONL object events")
    return events


def _large_codex_stream(
    *,
    final_text: str | None,
    terminal_count: int = 1,
    top_level_error: bool = False,
    leave_tool_open: bool = False,
    secret_fields: dict[str, object] | None = None,
    text_secrets: tuple[str, str] | None = None,
    unparseable_line: str | None = None,
) -> str:
    events: list[dict | str] = [
        {"type": "thread.started", "thread_id": "large-thread"},
        {"type": "turn.started"},
    ]
    secret_items = list((secret_fields or {}).items())
    first_secret_event = 140 - len(secret_items)
    for number in range(140):
        item_id = f"tool-{number:03d}"
        padding = f"TOOL-{number:03d}-" + ("x" * 420)
        if number == 139 and text_secrets is not None:
            bearer_secret, sk_secret = text_secrets
            authorization_scheme = "Bear" + "er "
            padding += f" {authorization_scheme}{bearer_secret} {sk_secret}"
        item = {
            "id": item_id,
            "type": "command_execution",
            "command": f"echo {padding}",
            "status": "in_progress",
        }
        if number >= first_secret_event and secret_items:
            key, value = secret_items[number - first_secret_event]
            item[key] = value
        events.append({"type": "item.started", "item": item})
        events.append(
            {
                "type": "item.completed",
                "item": {
                    **item,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": padding,
                },
            }
        )
    if leave_tool_open:
        events.append(
            {
                "type": "item.started",
                "item": {
                    "id": "open-tool",
                    "type": "command_execution",
                    "command": "echo unfinished",
                    "status": "in_progress",
                },
            }
        )
    if top_level_error:
        events.append({"type": "error", "message": "fatal stream error"})
    if unparseable_line is not None:
        events.append(unparseable_line)
    if final_text is not None:
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "final-result",
                    "type": "agent_message",
                    "text": final_text,
                },
            }
        )
    events.extend(
        {"type": "turn.completed", "usage": {}}
        for _ in range(terminal_count)
    )
    output = "\n".join(
        event if isinstance(event, str) else json.dumps(event)
        for event in events
    ) + "\n"
    if len(output.encode("utf-8")) <= 65536:
        raise AssertionError("Large Codex fixture did not cross the preview boundary")
    return output


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

if mode == "large-codex-valid" and provider == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "large-valid"}))
    print(json.dumps({"type": "turn.started"}))
    for number in range(200):
        print(json.dumps({
            "type": "item.completed",
            "item": {
                "id": f"tool-{number}",
                "type": "command_execution",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": "x" * 420,
            },
        }))
    print(json.dumps({
        "type": "item.completed",
        "item": {
            "id": "final",
            "type": "agent_message",
            "text": "import Mathlib\nexample : True := by trivial\n-- LARGE_FINAL_TAIL",
        },
    }))
    print(json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 123,
            "output_tokens": 56,
            "reasoning_output_tokens": 7,
        },
    }))
    raise SystemExit(0)

if mode == "stream-safety-limit" and provider == "codex":
    sys.stdout.write("x" * 100000 + "UNOBSERVED_TAIL")
    sys.stdout.flush()
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
if mode == "sandbox-secret-filename":
    pathlib.Path("ghp_" + ("A" * 20)).write_text("sandbox-only", encoding="utf-8")

if provider == "codex":
    print(json.dumps({"type": "thread.started", "thread_id": "thread"}))
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

    def test_codex_three_success_fixture_truncation_prefixes_remain_malformed(
        self,
    ) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        for name in (
            "current_multi_message",
            "legacy_single_message",
            "tool_events_with_final",
        ):
            with self.subTest(name=name):
                _, stdout, _ = self._fixture_case(name)
                truncated_prefix = stdout[:-5]
                with self.assertRaises(SubscriptionBackendError) as raised:
                    backend._parse(
                        truncated_prefix,
                        requested_model="gpt-5.6-sol",
                        sandbox_root=Path.cwd(),
                        output_type="lean_file",
                    )
                self.assertEqual(raised.exception.kind, "malformed_output")

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

    def test_codex_rejects_contract_match_before_invalid_last_agent_message(self) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        events = [
            {"type": "thread.started", "thread_id": "thread"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "intermediate",
                    "type": "agent_message",
                    "text": "example : True := by trivial\n",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "last",
                    "type": "agent_message",
                    "text": "I could not produce the requested Lean file.",
                },
            },
            {"type": "turn.completed", "usage": {}},
        ]

        with self.assertRaises(SubscriptionBackendError) as raised:
            backend._parse(
                "\n".join(json.dumps(event) for event in events) + "\n",
                requested_model="gpt-5.6-sol",
                sandbox_root=Path.cwd(),
                output_type="lean_file",
            )

        self.assertEqual(raised.exception.kind, "output_protocol_incompatible")

    def test_codex_rejects_agent_message_after_completed_turn(self) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        events = [
            {"type": "thread.started", "thread_id": "thread"},
            {"type": "turn.started"},
            {"type": "turn.completed", "usage": {}},
            {
                "type": "item.completed",
                "item": {
                    "id": "late",
                    "type": "agent_message",
                    "text": "example : True := by trivial\n",
                },
            },
        ]

        with self.assertRaises(SubscriptionBackendError) as raised:
            backend._parse(
                "\n".join(json.dumps(event) for event in events) + "\n",
                requested_model="gpt-5.6-sol",
                sandbox_root=Path.cwd(),
                output_type="lean_file",
            )

        self.assertEqual(raised.exception.kind, "output_protocol_incompatible")

    def test_codex_rejects_incomplete_second_turn_after_completed_turn(self) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        events = [
            {"type": "thread.started", "thread_id": "thread"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "final",
                    "type": "agent_message",
                    "text": "example : True := by trivial\n",
                },
            },
            {"type": "turn.completed", "usage": {}},
            {"type": "turn.started"},
        ]

        with self.assertRaises(SubscriptionBackendError) as raised:
            backend._parse(
                "\n".join(json.dumps(event) for event in events) + "\n",
                requested_model="gpt-5.6-sol",
                sandbox_root=Path.cwd(),
                output_type="lean_file",
            )

        self.assertEqual(raised.exception.kind, "output_protocol_incompatible")

    def test_codex_rejects_tool_start_after_completed_turn(self) -> None:
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        events = [
            {"type": "thread.started", "thread_id": "thread"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "final",
                    "type": "agent_message",
                    "text": "example : True := by trivial\n",
                },
            },
            {"type": "turn.completed", "usage": {}},
            {
                "type": "item.started",
                "item": {
                    "id": "late-tool",
                    "type": "command_execution",
                    "status": "in_progress",
                },
            },
        ]

        with self.assertRaises(SubscriptionBackendError) as raised:
            backend._parse(
                "\n".join(json.dumps(event) for event in events) + "\n",
                requested_model="gpt-5.6-sol",
                sandbox_root=Path.cwd(),
                output_type="lean_file",
            )

        self.assertEqual(raised.exception.kind, "output_protocol_incompatible")

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
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            saved_events = _jsonl_events(saved_stdout)
            original_events = _jsonl_events(stdout)
            self.assertEqual(
                [event["type"] for event in saved_events],
                [event["type"] for event in original_events],
            )
            self.assertEqual(
                saved_events[-1]["usage"]["input_tokens"],
                1,
            )
            self.assertEqual((call_dir / "stderr.txt").read_text(encoding="utf-8"), stderr)
            self.assertFalse((call_dir / "raw-output.txt").exists())
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

    def test_codex_accepts_large_stream_and_archives_complete_redacted_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            final = (
                "import Mathlib\n\nexample : True := by\n"
                "  trivial\n-- FINAL_TAIL_SENTINEL\n"
            )
            stdout = _large_codex_stream(final_text=final)
            stderr_password = "stderr-" + "password-sensitive"
            stderr_encrypted = "stderr-" + "encrypted-sensitive"
            stderr_session = "stderr-" + "session-sensitive"
            stderr_bearer = "stderr-" + "bearer-sensitive"
            stderr_api_key = "stderr-" + "api-key-sensitive"
            stderr_json = json.dumps(
                {
                    "password": stderr_password,
                    "encrypted_content": stderr_encrypted,
                    "nested": {"session_token": stderr_session},
                },
                separators=(",", ":"),
            )
            authorization_scheme = "Bear" + "er "
            stderr = ("large stderr noise\n" * 5000) + (
                stderr_json
                + "\n"
                + f"Authorization: {authorization_scheme}{stderr_bearer}\n"
                + f"api_key={stderr_api_key}\n"
            )
            all_secrets = [
                stderr_password,
                stderr_encrypted,
                stderr_session,
                stderr_bearer,
                stderr_api_key,
            ]
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
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

            self.assertEqual(output, final)
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            saved_stderr = (call_dir / "stderr.txt").read_text(encoding="utf-8")
            raw_output = saved_stdout
            preview = (call_dir / "diagnostic-preview.txt").read_text(
                encoding="utf-8"
            )
            saved_events = [
                json.loads(line)
                for line in saved_stdout.splitlines()
            ]
            self.assertIn("FINAL_TAIL_SENTINEL", saved_stdout)
            self.assertIn('"type":"turn.completed"', saved_stdout)
            self.assertEqual(saved_events[-1]["type"], "turn.completed")
            self.assertEqual(
                sum(event.get("type") == "turn.completed" for event in saved_events),
                1,
            )
            self.assertEqual(raw_output, saved_stdout)
            self.assertFalse((call_dir / "raw-output.txt").exists())
            self.assertIn("<redacted>", saved_stderr)
            saved_stderr_json = json.loads(
                next(
                    line
                    for line in saved_stderr.splitlines()
                    if line.startswith("{")
                )
            )
            self.assertEqual(saved_stderr_json["password"], "<redacted>")
            self.assertEqual(
                saved_stderr_json["encrypted_content"], "<redacted>"
            )
            self.assertEqual(
                saved_stderr_json["nested"]["session_token"], "<redacted>"
            )
            self.assertLessEqual(len(preview.encode("utf-8")), 65536)
            self.assertNotIn("FINAL_TAIL_SENTINEL", preview)

            tool_events_path = call_dir / "tool-events.json"
            response_path = call_dir / "response.json"
            response = json.loads(
                response_path.read_text(encoding="utf-8")
            )
            evidence_texts = {
                "stdout": saved_stdout,
                "stderr": saved_stderr,
                "preview": preview,
                "tool_events": tool_events_path.read_text(encoding="utf-8"),
                "response": response_path.read_text(encoding="utf-8"),
            }
            for evidence_name, evidence_text in evidence_texts.items():
                for secret_value in all_secrets:
                    self.assertNotIn(
                        secret_value,
                        evidence_text,
                        msg=f"{evidence_name} leaked a test secret",
                    )
            metadata = response["metadata"]
            streams = metadata["stream_evidence"]
            self.assertTrue(metadata["diagnostic_preview_truncated"])
            stdout_process = streams["stdout"]["process_stream"]
            stdout_saved = streams["stdout"]["saved_redacted_evidence"]
            self.assertEqual(stdout_process["char_count"], len(stdout))
            self.assertEqual(
                stdout_process["byte_count"],
                len(stdout.encode("utf-8")),
            )
            self.assertEqual(
                stdout_process["sha256"],
                hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(stdout_saved["char_count"], len(saved_stdout))
            self.assertEqual(
                stdout_saved["byte_count"],
                len(saved_stdout.encode("utf-8")),
            )
            self.assertEqual(
                stdout_saved["sha256"],
                hashlib.sha256(saved_stdout.encode("utf-8")).hexdigest(),
            )
            self.assertTrue(stdout_saved["complete_captured_prefix_saved"])
            self.assertFalse(stdout_saved["saved_evidence_truncated"])
            self.assertFalse(streams["stdout"]["redaction_applied"])
            stderr_process = streams["stderr"]["process_stream"]
            stderr_saved = streams["stderr"]["saved_redacted_evidence"]
            self.assertEqual(stderr_process["char_count"], len(stderr))
            self.assertEqual(
                stderr_process["byte_count"],
                len(stderr.encode("utf-8")),
            )
            self.assertEqual(
                stderr_process["sha256"],
                hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(stderr_saved["char_count"], len(saved_stderr))
            self.assertEqual(
                stderr_saved["byte_count"],
                len(saved_stderr.encode("utf-8")),
            )
            self.assertEqual(
                stderr_saved["sha256"],
                hashlib.sha256(saved_stderr.encode("utf-8")).hexdigest(),
            )
            self.assertTrue(stderr_saved["complete_captured_prefix_saved"])
            self.assertFalse(stderr_saved["saved_evidence_truncated"])
            self.assertTrue(streams["stderr"]["redaction_applied"])
            self.assertEqual(
                streams["raw_output"]["source"], "saved_redacted_stdout"
            )
            raw_saved = streams["raw_output"]["saved_redacted_evidence"]
            self.assertEqual(
                raw_saved["char_count"], len(raw_output)
            )
            self.assertEqual(
                raw_saved["byte_count"],
                len(raw_output.encode("utf-8")),
            )
            self.assertEqual(
                raw_saved["sha256"],
                hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
            )
            self.assertFalse(raw_saved["saved_evidence_truncated"])
            self.assertTrue(
                streams["raw_output"]["complete_captured_prefix_saved"]
            )
            self.assertFalse(
                streams["raw_output"]["contains_unredacted_process_stream"]
            )
            self.assertEqual(streams["diagnostic_preview"]["limit_chars"], 65536)
            self.assertEqual(streams["diagnostic_preview"]["limit_bytes"], 65536)
            self.assertTrue(
                streams["diagnostic_preview"]["preview_truncated"]
            )
            self.assertFalse(
                streams["diagnostic_preview"][
                    "complete_saved_evidence_previewed"
                ]
            )
            saved_preview = streams["diagnostic_preview"]["saved_preview"]
            self.assertEqual(
                saved_preview["sha256"],
                hashlib.sha256(preview.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(saved_preview["char_count"], len(preview))
            self.assertEqual(
                saved_preview["byte_count"], len(preview.encode("utf-8"))
            )
            self.assertNotIn("tool_events", metadata)
            self.assertTrue(metadata["tool_events_saved_separately"])
            self.assertNotIn("TOOL-139-", json.dumps(response))
            tool_events = json.loads(
                tool_events_path.read_text(encoding="utf-8")
            )["events"]
            self.assertEqual(len(tool_events), 280)

    def test_codex_rejects_credentials_in_final_and_unkeyed_tool_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            final_secret = "sk-FINALCREDENTIAL12345"
            final_stdout = _large_codex_stream(
                final_text=(
                    "import Mathlib\nexample : True := by trivial\n"
                    f"-- {final_secret}\n"
                )
            )
            runtime = AgentRuntime(
                workflow_root=root / "final-workflow",
                run_id="run-final",
                backend=backend,
            )
            with (
                patch.object(
                    backend,
                    "_run",
                    return_value=subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=final_stdout,
                        stderr="",
                    ),
                ),
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
            self.assertEqual(
                raised.exception.kind, "credential_exposure_detected"
            )
            final_call = next(
                (root / "final-workflow" / "agent-calls").iterdir()
            )
            final_response = json.loads(
                (final_call / "response.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(final_response["output"])
            self.assertFalse(any(root.rglob("candidate.lean")))
            for path in final_call.rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        final_secret, path.read_text(encoding="utf-8")
                    )

            standalone_secrets = (
                "bearer-credential-123",
                "sk-TOOLCREDENTIAL12345",
                "ghp_ABCDEFGHIJKLMNOPQRST",
                "github_pat_11_ABCDEFGHIJKLMNOPQRST",
                "xoxb-1234567890-ABCDEFGHIJ",
                "AKIAABCDEFGHIJKLMNOP",
                "eyJabcdefgh.abcdefgh.abcdefgh",
                "alpha beta gamma",
            )
            aggregated_output = " ".join(
                (
                    f"Bearer {standalone_secrets[0]}",
                    *standalone_secrets[1:7],
                    f"password={standalone_secrets[7]}",
                )
            )
            tool_events = (
                {"type": "thread.started", "thread_id": "credential-tool"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "tool",
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": aggregated_output,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "final",
                        "type": "agent_message",
                        "text": "example : True := by trivial\n",
                    },
                },
                {"type": "turn.completed", "usage": {}},
            )
            tool_stdout = "\n".join(
                json.dumps(event) for event in tool_events
            ) + "\n"
            tool_runtime = AgentRuntime(
                workflow_root=root / "tool-workflow",
                run_id="run-tool",
                backend=backend,
            )
            with (
                patch.object(
                    backend,
                    "_run",
                    return_value=subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=tool_stdout,
                        stderr="",
                    ),
                ),
                self.assertRaises(SubscriptionBackendError) as raised,
            ):
                tool_runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned"),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )
            self.assertEqual(
                raised.exception.kind, "credential_exposure_detected"
            )
            tool_call = next(
                (root / "tool-workflow" / "agent-calls").iterdir()
            )
            archived = "".join(
                path.read_text(encoding="utf-8")
                for path in tool_call.rglob("*")
                if path.is_file()
            )
            for secret in standalone_secrets:
                self.assertNotIn(secret, archived)
            self.assertFalse((tool_call / "raw-output.txt").exists())

    def test_redaction_rejects_redacted_prefix_bypass_and_is_linear(self) -> None:
        cases = (
            (
                "password=<redacted>ACTUAL_SECRET",
                "password=<redacted>",
                "ACTUAL_SECRET",
            ),
            (
                "session_token: <redacted> REAL_PASSWORD_VALUE",
                "session_token: <redacted>",
                "REAL_PASSWORD_VALUE",
            ),
            (
                'password="<redacted>ACTUAL_SECRET"',
                'password="<redacted>"',
                "ACTUAL_SECRET",
            ),
            (
                'prefix "password=<redacted>ACTUAL_SECRET" trailing-safe',
                'prefix "password=<redacted>" trailing-safe',
                "ACTUAL_SECRET",
            ),
        )
        for raw, expected, forbidden in cases:
            with self.subTest(raw=raw):
                redacted, changed = _redact_text(raw)
                self.assertTrue(changed)
                self.assertEqual(redacted, expected)
                self.assertNotIn(forbidden, redacted)
        for safe in (
            "password=<redacted>",
            "password='  <redacted>  '",
            'password="  <redacted>  "',
        ):
            with self.subTest(safe=safe):
                redacted, changed = _redact_text(safe)
                self.assertFalse(changed)
                self.assertEqual(redacted, safe)

        unit = 'prefix "password=alpha beta gamma" trailing-safe;'
        repeats = ((512 * 1024) // len(unit)) + 1
        adversarial = unit * repeats
        started = time.perf_counter()
        redacted, changed = _redact_text(adversarial)
        elapsed = time.perf_counter() - started
        print(
            "REDACTION_PERFORMANCE "
            f"bytes={len(adversarial.encode('utf-8'))} "
            f"seconds={elapsed:.6f}"
        )
        self.assertGreaterEqual(len(adversarial.encode("utf-8")), 512 * 1024)
        self.assertTrue(changed)
        self.assertLess(elapsed, 10)
        self.assertNotIn("alpha beta gamma", redacted)
        self.assertEqual(
            redacted.count('"password=<redacted>" trailing-safe;'),
            repeats,
        )

    def test_codex_redaction_preserves_quotes_json_and_safe_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            embedded_json = '{"message":"password=alpha beta gamma"}'
            events = (
                {"type": "thread.started", "thread_id": "quoted-redaction"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "tool",
                        "type": "command_execution",
                        "command": embedded_json,
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "final",
                        "type": "agent_message",
                        "text": "example : True := by trivial\n",
                    },
                },
                {"type": "turn.completed", "usage": {}},
            )
            stdout = "\n".join(json.dumps(event) for event in events) + "\n"
            stderr = (
                embedded_json
                + "\n"
                + 'prefix "session_token=alpha beta gamma" trailing-safe\n'
            )
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            with (
                patch.object(
                    backend,
                    "_run",
                    return_value=subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=stdout, stderr=stderr
                    ),
                ),
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
            self.assertEqual(
                raised.exception.kind, "credential_exposure_detected"
            )
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_events = _jsonl_events(
                (call_dir / "stdout.txt").read_text(encoding="utf-8")
            )
            saved_command = saved_events[2]["item"]["command"]
            self.assertEqual(
                saved_command, '{"message":"password=<redacted>"}'
            )
            self.assertEqual(
                json.loads(saved_command), {"message": "password=<redacted>"}
            )
            saved_stderr_lines = (
                call_dir / "stderr.txt"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                json.loads(saved_stderr_lines[0]),
                {"message": "password=<redacted>"},
            )
            self.assertEqual(
                saved_stderr_lines[1],
                'prefix "session_token=<redacted>" trailing-safe',
            )
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                response["error"]["kind"], "credential_exposure_detected"
            )
            self.assertIsNone(response["output"])

    def test_codex_preserves_usage_and_lean_identifiers_but_redacts_real_keys(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            final = (
                "def token : Nat := 1\n"
                "def secretLemma : token = 1 := by rfl\n"
            )
            events = (
                {"type": "thread.started", "thread_id": "narrow-keys"},
                {"type": "turn.started"},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "tool",
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "final",
                        "type": "agent_message",
                        "text": final,
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 123,
                        "output_tokens": 56,
                        "reasoning_output_tokens": 7,
                    },
                },
            )
            stdout = "\n".join(json.dumps(event) for event in events) + "\n"
            composite_values = {
                "db_password": "DB_PASSWORD_VALUE",
                "user_password_hash": "PASSWORD_HASH_VALUE",
                "authorization_header": "AUTHORIZATION_VALUE",
                "cookie_value": "COOKIE_VALUE",
                "credential_blob": "CREDENTIAL_VALUE",
                "aws_secret_access_key": "AWS_SECRET_VALUE",
                "nested_session_token": "SESSION_FIELD_SECRET",
                "client_secret_blob": "CLIENT_FIELD_SECRET",
            }
            stderr = json.dumps(composite_values, separators=(",", ":")) + "\n"
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            with patch.object(
                backend,
                "_run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=stdout, stderr=stderr
                ),
            ):
                output = runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned"),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )
            self.assertEqual(output, final)
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_events = _jsonl_events(
                (call_dir / "stdout.txt").read_text(encoding="utf-8")
            )
            self.assertEqual(saved_events[3]["item"]["text"], final)
            self.assertEqual(
                saved_events[-1]["usage"],
                {
                    "input_tokens": 123,
                    "output_tokens": 56,
                    "reasoning_output_tokens": 7,
                },
            )
            self.assertEqual(
                json.loads(
                    (call_dir / "stderr.txt").read_text(encoding="utf-8")
                ),
                {key: "<redacted>" for key in composite_values},
            )

    def test_codex_redacts_unparseable_late_line_and_large_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            malformed_secret = "malformed-" + "line-sensitive-value"
            stderr_secret = "malformed-" + "stderr-sensitive-value"
            malformed_line = (
                '{"type":"notice","session_token":"'
                + malformed_secret
                + '","broken":['
            )
            stdout = _large_codex_stream(
                final_text="example : True := by trivial\n",
                unparseable_line=malformed_line,
            )
            self.assertGreater(stdout.index(malformed_secret), 65536)
            stderr = ("large stderr noise\n" * 5000) + (
                f'password="{stderr_secret}"\n'
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
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

            self.assertEqual(raised.exception.kind, "malformed_output")
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            saved_stderr = (call_dir / "stderr.txt").read_text(encoding="utf-8")
            expected_malformed_line = (
                '{"type":"notice","session_token":"<redacted>","broken":['
            )
            self.assertIn(expected_malformed_line, saved_stdout.splitlines())
            self.assertIn('password="<redacted>"', saved_stderr)
            evidence_paths = (
                call_dir / "stdout.txt",
                call_dir / "stderr.txt",
                call_dir / "diagnostic-preview.txt",
                call_dir / "tool-events.json",
                call_dir / "response.json",
            )
            for evidence_path in evidence_paths:
                evidence = evidence_path.read_text(encoding="utf-8")
                self.assertNotIn(malformed_secret, evidence)
                self.assertNotIn(stderr_secret, evidence)
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            streams = response["metadata"]["stream_evidence"]
            self.assertTrue(streams["stdout"]["redaction_applied"])
            self.assertTrue(streams["stderr"]["redaction_applied"])
            self.assertEqual(
                streams["stdout"]["process_stream"]["sha256"],
                hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                streams["stderr"]["process_stream"]["sha256"],
                hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            )

    def test_codex_redacts_unquoted_multivalue_text_fields_in_all_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            stdout = _large_codex_stream(
                final_text="example : True := by trivial\n",
                unparseable_line="session_token: alpha beta gamma",
            )
            stderr = "\n".join(
                (
                    "password=alpha beta gamma",
                    'encrypted_content={"payload":"OBJECT_SECRET_SENTINEL"}',
                    "client_secret=[ARRAY_SECRET_A, ARRAY_SECRET_B]",
                )
            ) + "\n"
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
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

            self.assertEqual(raised.exception.kind, "malformed_output")
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            saved_stderr = (call_dir / "stderr.txt").read_text(encoding="utf-8")
            self.assertIn("session_token: <redacted>", saved_stdout)
            self.assertIn("password=<redacted>", saved_stderr)
            self.assertIn("encrypted_content=<redacted>", saved_stderr)
            self.assertIn("client_secret=<redacted>", saved_stderr)
            evidence_paths = (
                call_dir / "stdout.txt",
                call_dir / "stderr.txt",
                call_dir / "diagnostic-preview.txt",
                call_dir / "tool-events.json",
                call_dir / "response.json",
            )
            forbidden_fragments = (
                "alpha beta gamma",
                "beta gamma",
                "OBJECT_SECRET_SENTINEL",
                "ARRAY_SECRET_A",
                "ARRAY_SECRET_B",
            )
            for evidence_path in evidence_paths:
                evidence = evidence_path.read_text(encoding="utf-8")
                for forbidden in forbidden_fragments:
                    self.assertNotIn(
                        forbidden,
                        evidence,
                        msg=f"{evidence_path.name} leaked {forbidden}",
                    )

    def test_codex_redacts_quoted_keys_with_unquoted_values_in_all_evidence(
        self,
    ) -> None:
        cases = (
            (
                "double_quoted_password",
                '"password": alpha beta gamma',
                '"password": <redacted>',
                ("alpha beta gamma", "beta gamma"),
            ),
            (
                "single_quoted_session_token",
                "'session_token': alpha beta gamma",
                "'session_token': <redacted>",
                ("alpha beta gamma", "beta gamma"),
            ),
            (
                "double_quoted_encrypted_object",
                '"encrypted_content": {"payload":"secret"}',
                '"encrypted_content": <redacted>',
                ('{"payload":"secret"}', '"payload":"secret"'),
            ),
            (
                "single_quoted_client_secret_array",
                "'client_secret': [secret-a, secret-b]",
                "'client_secret': <redacted>",
                ("secret-a", "secret-b"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            final = "example : True := by trivial\n"
            for case_name, sensitive_line, expected_line, forbidden in cases:
                for stream in ("stderr", "unparseable_stdout"):
                    with self.subTest(case=case_name, stream=stream):
                        stdout = _large_codex_stream(
                            final_text=final,
                            unparseable_line=(
                                sensitive_line
                                if stream == "unparseable_stdout"
                                else None
                            ),
                        )
                        stderr = (
                            sensitive_line + "\n"
                            if stream == "stderr"
                            else ""
                        )
                        completed = subprocess.CompletedProcess(
                            args=[],
                            returncode=0,
                            stdout=stdout,
                            stderr=stderr,
                        )
                        runtime = AgentRuntime(
                            workflow_root=(
                                root / case_name / stream / "workflow"
                            ),
                            run_id=f"{case_name}-{stream}",
                            backend=backend,
                        )

                        with patch.object(
                            backend, "_run", return_value=completed
                        ):
                            if stream == "unparseable_stdout":
                                with self.assertRaises(
                                    SubscriptionBackendError
                                ) as raised:
                                    runtime.invoke(
                                        role="prover",
                                        phase="prove",
                                        output_type="lean_file",
                                        config=_config("gpt-test-pinned"),
                                        system_prompt="system",
                                        user_prompt="user",
                                        temp_dir=root / "ignored",
                                    )
                                self.assertEqual(
                                    raised.exception.kind,
                                    "malformed_output",
                                )
                            else:
                                output = runtime.invoke(
                                    role="prover",
                                    phase="prove",
                                    output_type="lean_file",
                                    config=_config("gpt-test-pinned"),
                                    system_prompt="system",
                                    user_prompt="user",
                                    temp_dir=root / "ignored",
                                )
                                self.assertEqual(output, final)

                        call_dir = next(
                            (
                                root
                                / case_name
                                / stream
                                / "workflow"
                                / "agent-calls"
                            ).iterdir()
                        )
                        saved_stream = (
                            call_dir
                            / (
                                "stderr.txt"
                                if stream == "stderr"
                                else "stdout.txt"
                            )
                        ).read_text(encoding="utf-8")
                        self.assertIn(expected_line, saved_stream)
                        evidence_paths = (
                            call_dir / "stdout.txt",
                            call_dir / "stderr.txt",
                            call_dir / "diagnostic-preview.txt",
                            call_dir / "tool-events.json",
                            call_dir / "response.json",
                        )
                        for evidence_path in evidence_paths:
                            evidence = evidence_path.read_text(encoding="utf-8")
                            for secret in forbidden:
                                self.assertNotIn(
                                    secret,
                                    evidence,
                                    msg=(
                                        f"{evidence_path.name} leaked "
                                        f"{secret} for {case_name}/{stream}"
                                    ),
                                )

    def test_codex_large_stream_without_final_reports_protocol_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            stdout = _large_codex_stream(final_text=None)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=stdout,
                stderr="",
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

            self.assertEqual(raised.exception.kind, "empty_output")
            self.assertNotEqual(raised.exception.kind, "output_too_large")
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            self.assertEqual(_jsonl_events(saved), _jsonl_events(stdout))
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                response["metadata"]["terminal_state"],
                "empty_output",
            )

    def test_codex_large_stream_fail_closed_variants(self) -> None:
        final = "import Mathlib\n\nexample : True := by\n  trivial\n"
        backend = CodexSubscriptionBackend(command_prefix=("unused",))
        cases = {
            "top-level-error": _large_codex_stream(
                final_text=final, top_level_error=True
            ),
            "duplicate-terminal": _large_codex_stream(
                final_text=final, terminal_count=2
            ),
            "truncated-terminal": _large_codex_stream(
                final_text=final, terminal_count=0
            ),
            "open-tool": _large_codex_stream(
                final_text=final, leave_tool_open=True
            ),
        }
        for name, stdout in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(SubscriptionBackendError) as raised:
                    backend._parse(
                        stdout,
                        requested_model="gpt-5.6-sol",
                        sandbox_root=Path.cwd(),
                        output_type="lean_file",
                    )
                self.assertEqual(
                    raised.exception.kind, "output_protocol_incompatible"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_backend = self._backend("codex", root)
            runtime_backend.inspect(
                model="gpt-test-pinned", reasoning_effort="low"
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=9,
                stdout=_large_codex_stream(final_text=final),
                stderr=("large nonzero stderr\n" * 5000),
            )
            with patch.object(runtime_backend, "_run", return_value=completed):
                with self.assertRaises(SubscriptionBackendError) as raised:
                    runtime_backend.invoke(
                        _request("lean_file"),
                        _config("gpt-test-pinned"),
                        root / "ignored",
                    )
            self.assertEqual(raised.exception.kind, "nonzero_exit")

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
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            saved_events = _jsonl_events(saved_stdout)
            original_events = _jsonl_events(stdout)
            self.assertEqual(
                [event["type"] for event in saved_events],
                [event["type"] for event in original_events],
            )
            self.assertEqual(
                saved_events[-1]["usage"]["input_tokens"],
                1,
            )
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

    def test_codex_fails_closed_and_redacts_all_derived_evidence_fields(
        self,
    ) -> None:
        github_secret = "ghp_" + ("A" * 20)
        slack_secret = "xoxb-" + ("1" * 10) + "-" + ("A" * 10)
        fine_grained_secret = "github_pat_" + ("B" * 20)
        openai_secret = "sk-" + ("C" * 20)
        event_cases = (
            (
                "tool_status",
                {
                    "id": "tool",
                    "type": "command_execution",
                    "command": "echo safe",
                    "status": github_secret,
                    "exit_code": 0,
                },
                github_secret,
            ),
            (
                "file_change_kind",
                {
                    "id": "file",
                    "type": "file_change",
                    "status": "completed",
                    "changes": [{"path": "safe.txt", "kind": slack_secret}],
                },
                slack_secret,
            ),
            (
                "file_change_path",
                {
                    "id": "file",
                    "type": "file_change",
                    "status": "completed",
                    "changes": [
                        {
                            "path": f"nested/{fine_grained_secret}.txt",
                            "kind": "add",
                        }
                    ],
                },
                fine_grained_secret,
            ),
            (
                "command_cwd",
                {
                    "id": "tool",
                    "type": "command_execution",
                    "command": "echo safe",
                    "cwd": f"nested/{openai_secret}",
                    "status": "completed",
                    "exit_code": 0,
                },
                openai_secret,
            ),
        )

        def stdout_for(item: dict[str, object]) -> str:
            events = (
                {"type": "thread.started", "thread_id": "derived-evidence"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": item},
                {
                    "type": "item.completed",
                    "item": {
                        "id": "final",
                        "type": "agent_message",
                        "text": "example : True := by trivial\n",
                    },
                },
                {"type": "turn.completed", "usage": {}},
            )
            return "\n".join(json.dumps(event) for event in events) + "\n"

        def assert_safe_failure(
            root: Path,
            backend: CodexSubscriptionBackend,
            secret: str,
            *,
            completed: subprocess.CompletedProcess[str] | None = None,
        ) -> None:
            runtime = AgentRuntime(
                workflow_root=root / "workflow",
                run_id="run-derived-evidence",
                backend=backend,
            )
            context = (
                patch.object(backend, "_run", return_value=completed)
                if completed is not None
                else patch.object(backend, "_run", wraps=backend._run)
            )
            with context, self.assertRaises(SubscriptionBackendError) as raised:
                runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned"),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )
            self.assertEqual(
                raised.exception.kind, "credential_exposure_detected"
            )
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(response["output"])
            self.assertEqual(
                response["error"]["kind"], "credential_exposure_detected"
            )
            archived = "".join(
                path.read_text(encoding="utf-8")
                for path in call_dir.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(secret, archived)
            self.assertFalse(any(root.rglob("candidate.lean")))

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            for case_name, item, secret in event_cases:
                with self.subTest(case=case_name):
                    root = parent / case_name
                    root.mkdir()
                    backend = self._backend("codex", root)
                    backend.inspect(
                        model="gpt-test-pinned", reasoning_effort="low"
                    )
                    assert_safe_failure(
                        root,
                        backend,
                        secret,
                        completed=subprocess.CompletedProcess(
                            args=[],
                            returncode=0,
                            stdout=stdout_for(item),
                            stderr="",
                        ),
                    )

            sandbox_root = parent / "sandbox_filename"
            sandbox_root.mkdir()
            sandbox_backend = self._backend("codex", sandbox_root)
            sandbox_backend.base_environment["FAKE_MODE"] = (
                "sandbox-secret-filename"
            )
            assert_safe_failure(
                sandbox_root,
                sandbox_backend,
                github_secret,
            )

            boundary_root = parent / "boundary_path"
            boundary_root.mkdir()
            boundary_backend = self._backend("codex", boundary_root)
            boundary_backend.inspect(
                model="gpt-test-pinned", reasoning_effort="low"
            )
            safe_stdout = stdout_for(
                {
                    "id": "tool",
                    "type": "command_execution",
                    "command": "echo safe",
                    "status": "completed",
                    "exit_code": 0,
                }
            )
            before_snapshot = {
                "files": {},
                "boundary_violations": [],
                "scan_errors": [],
            }
            after_snapshot = {
                "files": {},
                "boundary_violations": [f"nested/{github_secret}.txt"],
                "scan_errors": [],
            }
            with patch(
                "lean_loop.subscription_backend._sandbox_snapshot",
                side_effect=(before_snapshot, after_snapshot),
            ):
                assert_safe_failure(
                    boundary_root,
                    boundary_backend,
                    github_secret,
                    completed=subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=safe_stdout,
                        stderr="",
                    ),
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
            self.assertEqual(len(oversized.last_stdout or ""), 65536)
            self.assertNotIn("stream_evidence", oversized.last_metadata)

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

    def test_codex_real_collector_parses_large_tail_within_safety_limit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            backend.base_environment["FAKE_MODE"] = "large-codex-valid"
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            output = runtime.invoke(
                role="prover",
                phase="prove",
                output_type="lean_file",
                config=_config("gpt-test-pinned"),
                system_prompt="system",
                user_prompt="user",
                temp_dir=root / "ignored",
            )
            self.assertIn("LARGE_FINAL_TAIL", output)
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            self.assertGreater(len(saved_stdout.encode("utf-8")), 65536)
            self.assertIn('"type":"turn.completed"', saved_stdout)
            self.assertFalse((call_dir / "raw-output.txt").exists())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            streams = response["metadata"]["stream_evidence"]
            self.assertTrue(streams["complete_process_stream_observed"])
            self.assertTrue(streams["complete_captured_prefix_saved"])
            self.assertFalse(streams["saved_evidence_truncated"])
            self.assertEqual(streams["collection_stop_reason"], "completed")
            self.assertFalse(streams["output_safety_limit_exceeded"])
            self.assertGreater(streams["output_safety_limit_bytes"], 65536)
            self.assertEqual(
                _jsonl_events(saved_stdout)[-1]["usage"],
                {
                    "input_tokens": 123,
                    "output_tokens": 56,
                    "reasoning_output_tokens": 7,
                },
            )

    def test_codex_output_safety_limit_is_distinct_and_truthful(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            backend.base_environment["FAKE_MODE"] = "stream-safety-limit"
            backend.max_process_output_bytes = 4096
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            with self.assertRaises(SubscriptionBackendError) as raised:
                runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned"),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )
            self.assertEqual(
                raised.exception.kind, "output_safety_limit_exceeded"
            )
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            self.assertEqual(len(saved_stdout.encode("utf-8")), 4096)
            self.assertNotIn("UNOBSERVED_TAIL", saved_stdout)
            self.assertFalse((call_dir / "raw-output.txt").exists())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                response["metadata"]["terminal_state"],
                "output_safety_limit_exceeded",
            )
            self.assertEqual(response["metadata"]["captured_process_bytes"], 4096)
            streams = response["metadata"]["stream_evidence"]
            self.assertEqual(streams["captured_process_bytes"], 4096)
            self.assertEqual(streams["output_safety_limit_bytes"], 4096)
            self.assertFalse(streams["complete_process_stream_observed"])
            self.assertTrue(streams["complete_captured_prefix_saved"])
            self.assertFalse(streams["saved_evidence_truncated"])
            self.assertEqual(
                streams["collection_stop_reason"],
                "output_safety_limit_exceeded",
            )
            self.assertTrue(streams["output_safety_limit_exceeded"])
            self.assertEqual(
                streams["stdout"]["process_stream"]["scope"],
                "captured_prefix_only",
            )
            self.assertTrue(
                streams["stdout"]["saved_redacted_evidence"][
                    "complete_captured_prefix_saved"
                ]
            )
            self.assertFalse(
                streams["stdout"]["saved_redacted_evidence"][
                    "saved_evidence_truncated"
                ]
            )
            self.assertTrue(response["metadata"]["raw_output_saved"])

    def test_timeout_archives_complete_captured_prefix_without_false_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = self._backend("codex", root)
            backend.inspect(model="gpt-test-pinned", reasoning_effort="low")
            partial_stdout = ("x" * 70000) + "TAIL"
            partial_stderr = "stderr-tail"
            runtime = AgentRuntime(
                workflow_root=root / "workflow", run_id="run-1", backend=backend
            )
            timeout = subprocess.TimeoutExpired(
                ["fake-codex"],
                1,
                output=partial_stdout,
                stderr=partial_stderr,
            )
            with (
                patch(
                    "lean_loop.subscription_backend.run_controlled_process",
                    side_effect=timeout,
                ),
                self.assertRaises(SubscriptionBackendError) as raised,
            ):
                runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config("gpt-test-pinned", timeout=1),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )
            self.assertEqual(raised.exception.kind, "timeout")
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            saved_stdout = (call_dir / "stdout.txt").read_text(encoding="utf-8")
            self.assertEqual(saved_stdout, partial_stdout)
            self.assertTrue(saved_stdout.endswith("TAIL"))
            self.assertFalse((call_dir / "raw-output.txt").exists())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            metadata = response["metadata"]
            streams = metadata["stream_evidence"]
            captured_bytes = len(partial_stdout.encode("utf-8")) + len(
                partial_stderr.encode("utf-8")
            )
            self.assertEqual(metadata["captured_process_bytes"], captured_bytes)
            self.assertFalse(metadata["complete_process_stream_observed"])
            self.assertFalse(streams["complete_process_stream_observed"])
            self.assertTrue(streams["complete_captured_prefix_saved"])
            self.assertFalse(streams["saved_evidence_truncated"])
            self.assertEqual(streams["collection_stop_reason"], "timeout")
            self.assertFalse(streams["output_safety_limit_exceeded"])
            self.assertEqual(streams["captured_process_bytes"], captured_bytes)
            self.assertEqual(
                streams["stdout"]["process_stream"]["char_count"],
                len(partial_stdout),
            )
            self.assertEqual(
                streams["stdout"]["process_stream"]["sha256"],
                hashlib.sha256(partial_stdout.encode("utf-8")).hexdigest(),
            )
            self.assertTrue(
                streams["stdout"]["saved_redacted_evidence"][
                    "complete_captured_prefix_saved"
                ]
            )
            self.assertFalse(
                streams["stdout"]["saved_redacted_evidence"][
                    "saved_evidence_truncated"
                ]
            )
            self.assertTrue(
                streams["diagnostic_preview"]["preview_truncated"]
            )
            self.assertTrue(metadata["diagnostic_preview_truncated"])
            self.assertTrue(metadata["raw_output_saved"])

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
            runtime = AgentRuntime(
                workflow_root=root / "workflow",
                run_id="run-cancel",
                backend=backend,
            )

            def cancel() -> None:
                time.sleep(0.3)
                control.cancel = True

            thread = threading.Thread(target=cancel)
            thread.start()
            with self.assertRaises(ProcessCancelled):
                runtime.invoke(
                    role="planner",
                    phase="plan",
                    output_type="json",
                    config=_config("gpt-test-pinned", timeout=10),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "ignored",
                )
            thread.join()
            self.assertEqual(control.started, control.finished)
            self.assertEqual(backend.last_metadata["terminal_state"], "cancelled")
            call_dir = next((root / "workflow" / "agent-calls").iterdir())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            streams = response["metadata"]["stream_evidence"]
            self.assertFalse(streams["complete_process_stream_observed"])
            self.assertTrue(streams["complete_captured_prefix_saved"])
            self.assertFalse(streams["saved_evidence_truncated"])
            self.assertEqual(streams["collection_stop_reason"], "cancelled")
            self.assertFalse(streams["output_safety_limit_exceeded"])
            self.assertFalse(
                streams["diagnostic_preview"]["preview_truncated"]
            )
            self.assertTrue(response["metadata"]["raw_output_saved"])
            self.assertTrue((call_dir / "stdout.txt").is_file())
            self.assertTrue((call_dir / "stderr.txt").is_file())

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
