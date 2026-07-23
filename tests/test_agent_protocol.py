from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lean_loop.agent_protocol import (
    AgentProtocolError,
    AgentRequest,
    AgentRuntime,
    CredentialExposureError,
    DirectModelBackend,
    find_high_confidence_credential,
    is_sensitive_field_name,
    protocol_capabilities,
)
from lean_loop.config import ApiConfig


def _config() -> ApiConfig:
    return ApiConfig(
        api_base="https://example.invalid",
        api_key="must-not-be-persisted",
        model="model-a",
        mode="responses",
        timeout_seconds=10,
        curl_executable="curl.exe",
        reasoning_effort="medium",
    )


class AgentProtocolTests(unittest.TestCase):
    def test_rejects_unknown_role(self) -> None:
        with self.assertRaises(AgentProtocolError):
            AgentRequest(
                request_id="request",
                sequence=1,
                role="unknown",  # type: ignore[arg-type]
                run_id="run",
                phase="test",
                output_type="json",
                model="model",
                reasoning_effort="low",
                system_prompt="system",
                user_prompt="user",
            )

    def test_runtime_persists_versioned_request_and_response_without_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = DirectModelBackend(
                json_model_call=lambda config, system, user, temp: {"ok": True},
                file_model_call=lambda config, user, temp: "example : True := by trivial\n",
            )
            runtime = AgentRuntime(workflow_root=root, run_id="run-1", backend=backend)
            output = runtime.invoke(
                role="planner",
                phase="plan",
                output_type="json",
                config=_config(),
                system_prompt="system",
                user_prompt="user",
                temp_dir=root / "tmp",
                context={"target_file": "Main.lean"},
            )
            self.assertEqual(output, {"ok": True})
            calls = list((root / "agent-calls").iterdir())
            self.assertEqual(len(calls), 1)
            request = json.loads((calls[0] / "request.json").read_text(encoding="utf-8"))
            response = json.loads((calls[0] / "response.json").read_text(encoding="utf-8"))
            self.assertEqual(request["protocol_version"], 1)
            self.assertEqual(request["role"], "planner")
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["metadata"]["backend_id"], "direct")
            self.assertNotIn("must-not-be-persisted", json.dumps(request) + json.dumps(response))

            resumed = AgentRuntime(workflow_root=root, run_id="run-1", backend=backend)
            resumed.invoke(
                role="prover",
                phase="prove",
                output_type="lean_file",
                config=_config(),
                system_prompt="system",
                user_prompt="user",
                temp_dir=root / "tmp",
            )
            names = sorted(path.name for path in (root / "agent-calls").iterdir())
            self.assertTrue(names[0].startswith("0001-"))
            self.assertTrue(names[1].startswith("0002-"))

    def test_runtime_persists_backend_metadata_on_success_and_error(self) -> None:
        class ClassifiedError(RuntimeError):
            kind = "subscription_unavailable"

        class Backend:
            backend_id = "codex-subscription"
            last_metadata = {
                "backend_id": backend_id,
                "cli_version": "codex-cli 1.2.3",
                "requested_model": "model-a",
                "requested_model_catalog_status": "VALIDATED",
                "actual_model": None,
                "actual_model_status": "NOT_REPORTED_BY_CLIENT",
                "model_identity_source": "REQUESTED_MODEL_AND_OFFICIAL_CATALOG_ONLY",
                "requested_reasoning_effort": "medium",
                "effective_reasoning_effort": "medium",
                "tool_execution_policy": "TOOL_ENABLED_AGENT_SANDBOX",
                "filesystem_read_scope": "WINDOWS_BROAD_READ",
                "filesystem_write_scope": "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
                "read_isolation_status": (
                    "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX"
                ),
                "network_policy": "DISABLED",
                "sandbox_profile": {
                    "filesystem": "workspace-write",
                    "filesystem_read_scope": "WINDOWS_BROAD_READ",
                    "filesystem_write_scope": (
                        "REPO_EXTERNAL_EPHEMERAL_WORKSPACE"
                    ),
                    "read_isolation_status": (
                        "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX"
                    ),
                    "network_policy": "DISABLED",
                },
                "tool_events": [{"event_type": "command_execution", "exit_code": 0}],
                "sandbox_manifest": {"protected_state_unchanged": True},
            }

            def invoke(self, request, config, temp_dir):
                if request.phase == "error":
                    raise ClassifiedError("failed")
                return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime(workflow_root=root, run_id="run-1", backend=Backend())
            runtime.invoke(
                role="planner", phase="ok", output_type="json", config=_config(),
                system_prompt="system", user_prompt="user", temp_dir=root / "tmp",
            )
            with self.assertRaises(ClassifiedError):
                runtime.invoke(
                    role="planner", phase="error", output_type="json", config=_config(),
                    system_prompt="system", user_prompt="user", temp_dir=root / "tmp",
                )
            responses = [
                json.loads((path / "response.json").read_text(encoding="utf-8"))
                for path in sorted((root / "agent-calls").iterdir())
            ]
            for response in responses:
                self.assertEqual(response["metadata"]["backend_id"], "codex-subscription")
                self.assertNotIn("tool_events", response["metadata"])
                self.assertTrue(
                    response["metadata"]["tool_events_saved_separately"]
                )
                self.assertIsNone(response["metadata"]["actual_model"])
                self.assertEqual(
                    response["metadata"]["actual_model_status"],
                    "NOT_REPORTED_BY_CLIENT",
                )
                self.assertEqual(
                    response["metadata"]["filesystem_read_scope"],
                    "WINDOWS_BROAD_READ",
                )
                self.assertEqual(
                    response["metadata"]["filesystem_write_scope"],
                    "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
                )
                self.assertEqual(
                    response["metadata"]["read_isolation_status"],
                    "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
                )
                self.assertEqual(response["metadata"]["network_policy"], "DISABLED")
            self.assertEqual(
                responses[1]["metadata"]["error_classification"],
                "subscription_unavailable",
            )
            self.assertEqual(responses[1]["error"]["kind"], "subscription_unavailable")
            for call in sorted((root / "agent-calls").iterdir()):
                self.assertTrue((call / "tool-events.json").is_file())
                self.assertTrue((call / "sandbox-manifest.json").is_file())

    def test_truncated_fallback_is_saved_only_as_diagnostic_preview(self) -> None:
        class LargeError(RuntimeError):
            kind = "malformed_output"
            raw_output = "x" * 70000

        class Backend:
            backend_id = "codex-subscription"
            last_metadata = {"backend_id": backend_id}

            def invoke(self, request, config, temp_dir):
                raise LargeError("failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime(
                workflow_root=root, run_id="run-1", backend=Backend()
            )
            with self.assertRaises(LargeError):
                runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config(),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "tmp",
                )

            call_dir = next((root / "agent-calls").iterdir())
            preview = (call_dir / "diagnostic-preview.txt").read_text(
                encoding="utf-8"
            )
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertLessEqual(len(preview.encode("utf-8")), 65536)
            self.assertFalse((call_dir / "raw-output.txt").exists())
            self.assertFalse(response["metadata"]["raw_output_saved"])
            self.assertTrue(response["metadata"]["diagnostic_preview_saved"])
            self.assertTrue(
                response["metadata"]["diagnostic_preview_truncated"]
            )

    def test_runtime_rejects_credentials_before_response_or_workflow_output(self) -> None:
        cases = (
            (
                "planner",
                "json",
                {"plan": "Bearer abcdefghijklmnop"},
                "abcdefghijklmnop",
            ),
            (
                "prover",
                "lean_file",
                "example : True := by trivial\n-- sk-ABCDEFGHIJKLMNOP\n",
                "sk-ABCDEFGHIJKLMNOP",
            ),
            (
                "reviewer",
                "json",
                {"verdict": "xoxb-1234567890-ABCDEFGHIJ"},
                "xoxb-1234567890-ABCDEFGHIJ",
            ),
            (
                "auditor",
                "json",
                {"audit": "AKIAABCDEFGHIJKLMNOP"},
                "AKIAABCDEFGHIJKLMNOP",
            ),
        )
        for role, output_type, result, secret in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                backend = DirectModelBackend(
                    json_model_call=lambda config, system, user, temp: result,
                    file_model_call=lambda config, user, temp: result,
                )
                runtime = AgentRuntime(
                    workflow_root=root, run_id="run-1", backend=backend
                )
                with self.assertRaises(CredentialExposureError) as raised:
                    runtime.invoke(
                        role=role,
                        phase=str(role),
                        output_type=output_type,
                        config=_config(),
                        system_prompt="system",
                        user_prompt="user",
                        temp_dir=root / "tmp",
                    )
                self.assertEqual(
                    raised.exception.kind, "credential_exposure_detected"
                )
                call_dir = next((root / "agent-calls").iterdir())
                response = json.loads(
                    (call_dir / "response.json").read_text(encoding="utf-8")
                )
                self.assertEqual(response["status"], "error")
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

    def test_sensitive_field_names_are_separate_from_fixed_token_scanning(self) -> None:
        sensitive_fields = (
            "db_password",
            "user_password_hash",
            "authorization_header",
            "cookie_value",
            "credential_blob",
            "aws_secret_access_key",
            "nested_session_token",
            "client_secret_blob",
            "encrypted_content",
        )
        for field_name in sensitive_fields:
            with self.subTest(field_name=field_name):
                self.assertTrue(is_sensitive_field_name(field_name))
                self.assertIsNone(
                    find_high_confidence_credential({field_name: "actual-value"})
                )
                self.assertIsNotNone(
                    find_high_confidence_credential(
                        {field_name: "actual-value"},
                        inspect_sensitive_fields=True,
                    )
                )
        for field_name in (
            "input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            with self.subTest(field_name=field_name):
                self.assertFalse(is_sensitive_field_name(field_name))
        for safe_text, text_context in (
            ("<redacted>", "generic"),
            ("  <redacted>  ", "generic"),
            ("'<redacted>'", "generic"),
            ('"<redacted>"', "generic"),
            ("password=<redacted>", "generic"),
            ("password='  <redacted>  '", "generic"),
            ('password="  <redacted>  "', "generic"),
            ("def token : Nat := 1", "lean_source"),
            ("def secretLemma : token = 1 := by rfl", "lean_source"),
        ):
            with self.subTest(safe_text=safe_text):
                self.assertIsNone(
                    find_high_confidence_credential(
                        safe_text, text_context=text_context
                    )
                )
        for safe_text in (
            "password=<redacted>ACTUAL_SECRET",
            "session_token: <redacted> REAL_PASSWORD_VALUE",
            'password="<redacted>ACTUAL_SECRET"',
        ):
            with self.subTest(safe_text=safe_text):
                self.assertIsNone(find_high_confidence_credential(safe_text))
        self.assertIsNotNone(
            find_high_confidence_credential("Bearer <redacted>ACTUAL_SECRET")
        )

    def test_lean_syntax_is_safe_but_fixed_tokens_fail_closed(
        self,
    ) -> None:
        legal_lean = (
            "theorem foo (password : Nat) : True := by\n"
            "  trivial\n\n"
            "def foo (password : Nat) := password\n\n"
            "structure Credentials where\n"
            "  password : String\n\n"
            "example : True := by\n"
            "  let password := 1\n"
            '  let secret := "value"\n'
            "  -- password=not-a-fixed-token\n"
            "  trivial\n"
        )
        self.assertIsNone(
            find_high_confidence_credential(legal_lean, text_context="lean_source")
        )
        cases = (
            ('-- Bearer "abcdefghijklmnop"\n', "abcdefghijklmnop"),
            ("-- Bearer 'abcdefghijklmnop'\n", "abcdefghijklmnop"),
            ('#check "Bearer \\"abcdefghijklmnop\\""\n', "abcdefghijklmnop"),
        )
        for output, secret in cases:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                backend = DirectModelBackend(
                    json_model_call=lambda config, system, user, temp: {},
                    file_model_call=lambda config, user, temp, value=output: value,
                )
                runtime = AgentRuntime(
                    workflow_root=root, run_id="run-lean-scan", backend=backend
                )
                with self.assertRaises(CredentialExposureError) as raised:
                    runtime.invoke(
                        role="prover",
                        phase="prove",
                        output_type="lean_file",
                        config=_config(),
                        system_prompt="system",
                        user_prompt="user",
                        temp_dir=root / "tmp",
                    )
                self.assertEqual(
                    raised.exception.kind, "credential_exposure_detected"
                )
                call_dir = next((root / "agent-calls").iterdir())
                response = json.loads(
                    (call_dir / "response.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    response["error"]["kind"], "credential_exposure_detected"
                )
                self.assertNotIn(
                    secret,
                    "".join(
                        path.read_text(encoding="utf-8")
                        for path in call_dir.rglob("*")
                        if path.is_file()
                    ),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = DirectModelBackend(
                json_model_call=lambda config, system, user, temp: {},
                file_model_call=lambda config, user, temp: legal_lean,
            )
            runtime = AgentRuntime(
                workflow_root=root, run_id="run-legal-lean", backend=backend
            )
            self.assertEqual(
                runtime.invoke(
                    role="prover",
                    phase="prove",
                    output_type="lean_file",
                    config=_config(),
                    system_prompt="system",
                    user_prompt="user",
                    temp_dir=root / "tmp",
                ),
                legal_lean,
            )
            call_dir = next((root / "agent-calls").iterdir())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(response["output"], legal_lean)

    def test_exception_raw_output_credential_reclassifies_thrown_error(self) -> None:
        secret = "ACTUAL_SECRET"

        class OtherFailure(RuntimeError):
            kind = "other_failure"
            raw_output = f'Bearer "{secret}"'

        original = OtherFailure("backend failed")

        class Backend:
            backend_id = "test-backend"
            last_metadata = {"backend_id": backend_id}

            def invoke(self, request, config, temp_dir):
                raise original

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = AgentRuntime(
                workflow_root=root, run_id="run-reclassified", backend=Backend()
            )
            observed_kind = None
            with self.assertRaises(CredentialExposureError) as raised:
                try:
                    runtime.invoke(
                        role="planner",
                        phase="plan",
                        output_type="json",
                        config=_config(),
                        system_prompt="system",
                        user_prompt="user",
                        temp_dir=root / "tmp",
                    )
                except Exception as exc:
                    observed_kind = getattr(exc, "kind", None)
                    raise
            self.assertEqual(observed_kind, "credential_exposure_detected")
            self.assertIs(raised.exception.__cause__, original)
            call_dir = next((root / "agent-calls").iterdir())
            response = json.loads(
                (call_dir / "response.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                response["error"]["kind"], "credential_exposure_detected"
            )
            self.assertEqual(
                response["metadata"]["error_classification"],
                "credential_exposure_detected",
            )
            self.assertNotIn(
                secret,
                "".join(
                    path.read_text(encoding="utf-8")
                    for path in call_dir.rglob("*")
                    if path.is_file()
                ),
            )

    def test_capabilities_are_versioned(self) -> None:
        value = protocol_capabilities()
        self.assertEqual(value["protocol"], "lean-agent")
        self.assertEqual(value["protocol_version"], 1)
        self.assertIn("auditor", value["roles"])
        self.assertIn("replaceable_backend", value["features"])


if __name__ == "__main__":
    unittest.main()
