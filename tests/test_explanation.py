import json
import tempfile
import unittest
from pathlib import Path

from lean_loop.config import ApiConfig
from lean_loop.explanation import generate_workflow_explanation
from lean_loop.jsonutil import atomic_write_json, atomic_write_text, sha256_text
from lean_loop.state import WorkflowStore


def _config() -> ApiConfig:
    return ApiConfig(
        api_base="http://example.invalid/v1",
        api_key="not-used",
        model="fake-explainer",
        mode="responses",
        timeout_seconds=10,
        curl_executable="curl.exe",
        reasoning_effort="medium",
    )


class ExplanationTests(unittest.TestCase):
    def _workflow(self, root: Path, *, succeeded: bool = True) -> WorkflowStore:
        original = "example : True := by exact missing\n"
        candidate = "example : True := by trivial\n"
        store = WorkflowStore.create(
            project=root,
            target_file="Main.lean",
            task="prove True",
            settings={},
            original_sha256=sha256_text(original),
        )
        atomic_write_text(store.paths.original, original)
        atomic_write_json(
            store.paths.plan,
            {
                "summary": "replace the missing proof",
                "steps": [{"goal": "prove True", "success_criteria": "Lean passes"}],
            },
        )
        attempt = store.paths.attempt_dir(1)
        atomic_write_text(attempt / "candidate.lean", candidate)
        atomic_write_json(
            attempt / "check.json",
            {"ok": succeeded, "returncode": 0 if succeeded else 1, "output": ""},
        )
        atomic_write_json(
            store.paths.reviews / "001.json",
            {"verdict": "accept" if succeeded else "retry", "summary": "checked"},
        )
        store.update(
            status="succeeded" if succeeded else "failed",
            phase="complete",
            completed_attempt=1 if succeeded else None,
            current_sha256=sha256_text(candidate),
        )
        return store

    def test_generates_json_and_markdown_from_archived_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = self._workflow(project)

            def model(config, system, user, temp):
                self.assertIn("already passed Lean", system)
                self.assertIn("Requested explanation language: zh-CN", user)
                self.assertIn("example : True := by trivial", user)
                return {
                    "title": "真命题的证明",
                    "statement": "命题 True 成立。",
                    "proof_outline": ["使用 True 的构造器。"],
                    "detailed_proof": "True 按定义有一个直接证明。",
                    "lean_correspondence": [
                        {
                            "lean_fragment": "by trivial",
                            "mathematical_meaning": "直接构造 True 的证明。",
                        }
                    ],
                    "assumptions": [],
                }

            result = generate_workflow_explanation(
                project=project,
                run_id=store.paths.run_id,
                config=_config(),
                language="zh-CN",
                json_model_call=model,
            )
            self.assertTrue(result.ok)
            self.assertIn("详细证明", result.markdown_path.read_text(encoding="utf-8"))
            value = json.loads(result.json_path.read_text(encoding="utf-8"))
            self.assertTrue(value["source"]["lean_check_ok"])
            manifest = store.read()
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["explanation_status"], "succeeded")

    def test_model_failure_is_non_fatal_to_lean_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = self._workflow(project)

            def broken_model(config, system, user, temp):
                raise RuntimeError("relay unavailable")

            result = generate_workflow_explanation(
                project=project,
                run_id=store.paths.run_id,
                config=_config(),
                json_model_call=broken_model,
            )
            self.assertFalse(result.ok)
            manifest = store.read()
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["explanation_status"], "failed")

    def test_rejects_non_successful_workflow_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = self._workflow(project, succeeded=False)
            called = False

            def model(config, system, user, temp):
                nonlocal called
                called = True
                return {}

            with self.assertRaisesRegex(ValueError, "requires a succeeded workflow"):
                generate_workflow_explanation(
                    project=project,
                    run_id=store.paths.run_id,
                    config=_config(),
                    json_model_call=model,
                )
            self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
