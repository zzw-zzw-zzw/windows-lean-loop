import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.config import ApiConfig
from lean_loop.jsonutil import atomic_write_json
from lean_loop.lean import LeanCheck
from lean_loop.queue import QueueError, QueueStore, run_queue_task
from lean_loop.race import RaceResult
from lean_loop.workflow import WorkflowResult


def _settings() -> dict[str, object]:
    return {
        "agent_backend": "direct",
        "model": "",
        "max_attempts": 2,
        "max_attempts_per_step": 2,
        "lean_timeout": 30,
        "api_timeout": 60,
        "plan_effort": "low",
        "prove_effort": "low",
        "review_effort": "low",
        "review_model": "",
        "lake": "lake",
        "keep_failed": False,
        "explain": False,
        "explain_language": "zh-CN",
        "explain_effort": "low",
        "explain_model": "",
    }


class QueueStoreTests(unittest.TestCase):
    def test_task_model_overrides_all_default_stage_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "Main.lean").write_text(
                "example : True := by trivial\n", encoding="utf-8"
            )
            settings = _settings()
            settings["model"] = "relay-gpt-5.6"
            store = QueueStore(project)
            task = store.add_task(
                target_file="Main.lean", task_text="prove", settings=settings
            )
            claimed = store.claim_next(os.getpid())
            captured: dict[str, object] = {}

            def fake_workflow(**kwargs):
                captured["plan"] = kwargs["plan_config"].model
                captured["prove"] = kwargs["prove_config"].model
                captured["review"] = kwargs["review_config"].model
                kwargs["workflow_created_callback"]("20260714T000000000000Z")
                kwargs["phase_callback"]("proving", 1)
                kwargs["phase_callback"]("lean_checking", 1)
                kwargs["phase_callback"]("reviewing", 1)
                return WorkflowResult(
                    True,
                    "20260714T000000000000Z",
                    1,
                    project,
                    LeanCheck(True, 0, "", ("lake", "env", "lean", "Main.lean")),
                    False,
                )

            base_config = ApiConfig(
                api_base="https://example.invalid",
                api_key="unused",
                model="environment-model",
                mode="responses",
                timeout_seconds=60,
                curl_executable="curl.exe",
                reasoning_effort="medium",
            )
            with patch(
                "lean_loop.queue.run_structured_workflow", side_effect=fake_workflow
            ):
                result = run_queue_task(
                    store=store, task_row=claimed, base_config=base_config
                )

            self.assertTrue(result.ok)
            self.assertEqual(
                captured,
                {
                    "plan": "relay-gpt-5.6",
                    "prove": "relay-gpt-5.6",
                    "review": "relay-gpt-5.6",
                },
            )
            self.assertEqual(store.get_task(task["id"])["state"], "succeeded")

    def test_subscription_task_preserves_backend_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "Main.lean").write_text(
                "example : True := by trivial\n", encoding="utf-8"
            )
            settings = _settings()
            settings["agent_backend"] = "codex-subscription"
            settings["model"] = "gpt-5.6-sol"
            store = QueueStore(project)
            task = store.add_task(
                target_file="Main.lean", task_text="prove", settings=settings
            )
            claimed = store.claim_next(os.getpid())
            captured: dict[str, object] = {}

            def fake_workflow(**kwargs):
                captured["backend"] = kwargs["agent_backend_id"]
                captured["model"] = kwargs["plan_config"].model
                kwargs["workflow_created_callback"]("20260714T000000000000Z")
                kwargs["phase_callback"]("proving", 1)
                kwargs["phase_callback"]("lean_checking", 1)
                kwargs["phase_callback"]("reviewing", 1)
                return WorkflowResult(
                    True, "20260714T000000000000Z", 1, project,
                    LeanCheck(True, 0, "", ("lake",)), False,
                )

            with patch(
                "lean_loop.queue.run_structured_workflow", side_effect=fake_workflow
            ):
                result = run_queue_task(store=store, task_row=claimed)

            self.assertTrue(result.ok)
            self.assertEqual(captured, {
                "backend": "codex-subscription",
                "model": "gpt-5.6-sol",
            })

    def test_persists_tasks_and_claims_dependencies_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = QueueStore(project)
            first = store.add_task(
                target_file="Basic.lean", task_text="prove basic", settings=_settings()
            )
            second = store.add_task(
                target_file="Main.lean",
                task_text="prove main",
                settings=_settings(),
                dependencies=[first["id"]],
            )

            reopened = QueueStore(project)
            self.assertEqual(len(reopened.list_tasks()), 2)
            claimed = reopened.claim_next(os.getpid())
            self.assertEqual(claimed["id"], first["id"])
            self.assertEqual(claimed["state"], "planning")
            self.assertIsNone(reopened.claim_next(os.getpid()))

            reopened.transition(first["id"], "proving")
            reopened.transition(first["id"], "lean_checking")
            reopened.transition(first["id"], "reviewing")
            reopened.transition(first["id"], "succeeded", worker_pid=None)
            claimed_second = reopened.claim_next(os.getpid())
            self.assertEqual(claimed_second["id"], second["id"])

    def test_multi_provider_task_uses_first_verified_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "Main.lean").write_text(
                "example : True := by trivial\n", encoding="utf-8"
            )
            settings = _settings()
            settings["race"] = {
                "strategy": "first_verified_wins",
                "lanes": [
                    {"id": "gpt", "provider": "default", "model": "gpt-test"},
                    {
                        "id": "deepseek",
                        "provider": "deepseek",
                        "model": "deepseek-reasoner",
                    },
                ],
            }
            store = QueueStore(project)
            task = store.add_task(
                target_file="Main.lean", task_text="prove", settings=settings
            )
            claimed = store.claim_next(os.getpid())
            captured: dict[str, object] = {}

            def fake_race(**kwargs):
                captured["lanes"] = kwargs["lane_specs"]
                captured["race_id"] = kwargs["race_id"]
                return RaceResult(
                    True,
                    kwargs["race_id"],
                    "gpt",
                    "20260715T120000000000Z",
                    LeanCheck(True, 0, "", ("lean",)),
                )

            with patch("lean_loop.queue.run_prover_race", side_effect=fake_race):
                result = run_queue_task(store=store, task_row=claimed)
            self.assertTrue(result.ok)
            saved = store.get_task(task["id"])
            self.assertEqual(saved["state"], "succeeded")
            self.assertEqual(saved["workflow_run_id"], "20260715T120000000000Z")
            self.assertTrue(saved["settings"]["race"]["race_id"])
            self.assertEqual(len(captured["lanes"]), 2)

    def test_rejects_illegal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            task = store.add_task(
                target_file="Main.lean", task_text="prove", settings=_settings()
            )
            with self.assertRaisesRegex(QueueError, "Illegal task transition"):
                store.transition(task["id"], "succeeded")

    def test_cancel_and_retry_queued_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueueStore(Path(directory))
            task = store.add_task(
                target_file="Main.lean", task_text="prove", settings=_settings()
            )
            cancelled = store.request_cancel(task["id"])
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertTrue(cancelled["cancel_requested"])
            retried = store.retry(task["id"])
            self.assertEqual(retried["state"], "queued")
            self.assertFalse(retried["cancel_requested"])

    def test_retry_preserves_run_and_extends_exhausted_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = QueueStore(project)
            settings = _settings()
            settings["max_attempts"] = 2
            settings["max_attempts_per_step"] = 1
            task = store.add_task(
                target_file="Main.lean", task_text="prove", settings=settings
            )
            store.claim_next(os.getpid())
            run_id = "20260715T000000000000Z"
            store.attach_workflow(task["id"], run_id)
            manifest = project / ".lean-agent" / "workflows" / run_id / "run.json"
            atomic_write_json(
                manifest,
                {
                    "attempts": [{"attempt": 1}, {"attempt": 2}],
                    "steps": [
                        {
                            "status": "failed",
                            "attempts": [2],
                        }
                    ],
                },
            )
            store.transition(task["id"], "failed")
            retried = store.retry(task["id"])
            self.assertEqual(retried["workflow_run_id"], run_id)
            self.assertEqual(retried["settings"]["max_attempts"], 4)
            self.assertEqual(retried["settings"]["max_attempts_per_step"], 2)


if __name__ == "__main__":
    unittest.main()
