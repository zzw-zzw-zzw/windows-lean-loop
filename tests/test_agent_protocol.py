from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lean_loop.agent_protocol import (
    AgentProtocolError,
    AgentRequest,
    AgentRuntime,
    DirectModelBackend,
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
                "actual_model": "model-a",
                "requested_reasoning_effort": "medium",
                "effective_reasoning_effort": "medium",
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
                self.assertEqual(response["metadata"]["actual_model"], "model-a")
            self.assertEqual(
                responses[1]["metadata"]["error_classification"],
                "subscription_unavailable",
            )
            self.assertEqual(responses[1]["error"]["kind"], "subscription_unavailable")

    def test_capabilities_are_versioned(self) -> None:
        value = protocol_capabilities()
        self.assertEqual(value["protocol"], "lean-agent")
        self.assertEqual(value["protocol_version"], 1)
        self.assertIn("auditor", value["roles"])
        self.assertIn("replaceable_backend", value["features"])


if __name__ == "__main__":
    unittest.main()
