import json
import tempfile
import unittest
from pathlib import Path

from lean_loop.api import ApiError, MalformedModelOutputError
from lean_loop.config import ApiConfig
from lean_loop.lean import LeanCheck
from lean_loop.mathlib_index import build_mathlib_index
from lean_loop.process_control import ProcessCancelled
from lean_loop.workflow import (
    _resume_replan_reason,
    run_structured_workflow,
    validate_formal_goal,
)


def _config() -> ApiConfig:
    return ApiConfig(
        api_base="http://example.invalid/v1",
        api_key="not-used",
        model="fake-model",
        mode="responses",
        timeout_seconds=10,
        curl_executable="curl.exe",
        reasoning_effort="low",
    )


class StructuredWorkflowTests(unittest.TestCase):
    def test_backend_change_is_a_visible_resume_replan_reason(self) -> None:
        previous = {"settings": {"agent_backend": "codex-subscription"}}
        current = {"agent_backend": "claude-subscription"}
        self.assertEqual(
            _resume_replan_reason(None, previous, current),  # type: ignore[arg-type]
            "backend_changed",
        )

    def test_backend_identity_change_is_a_visible_resume_replan_reason(self) -> None:
        previous = {
            "settings": {
                "agent_backend": "codex-subscription",
                "backend_identity": {
                    "cli_version": "codex-cli 0.144.4",
                    "tool_execution_policy": "TOOL_ENABLED_AGENT_SANDBOX",
                },
            }
        }
        current = {
            "agent_backend": "codex-subscription",
            "backend_identity": {
                "cli_version": "codex-cli 0.145.0",
                "tool_execution_policy": "TOOL_ENABLED_AGENT_SANDBOX",
            },
        }
        self.assertEqual(
            _resume_replan_reason(None, previous, current),  # type: ignore[arg-type]
            "backend_identity_changed",
        )
    def _project(self, root: Path, source: str) -> tuple[Path, Path]:
        (root / "lean-toolchain").write_text("fake\n", encoding="utf-8")
        (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
        target = root / "Main.lean"
        target.write_text(source, encoding="utf-8")
        mathlib = root / ".lake" / "packages" / "mathlib" / "Mathlib" / "Logic"
        mathlib.mkdir(parents=True)
        (mathlib / "Basic.lean").write_text(
            "theorem useful_true : True := by trivial\n", encoding="utf-8"
        )
        return root, target

    def test_successful_plan_prove_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(
                    ok,
                    0 if ok else 1,
                    "" if ok else "Unknown identifier `missing`",
                    (lake, "env", "lean", "Main.lean"),
                )

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "replace missing proof",
                        "steps": [
                            {
                                "id": "step-1",
                                "goal": "prove True",
                                "success_criteria": "Lean exits zero",
                                "search_terms": ["useful_true"],
                            }
                        ],
                        "preserve": ["statement"],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                self.assertIn("Mathlib.Logic.Basic", prompt)
                return "example : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertTrue(result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "example : True := by trivial\n")
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "succeeded")
            self.assertEqual(manifest["settings"]["agent_backend"], "direct")
            self.assertTrue((result.state_dir / "plan.json").is_file())
            self.assertTrue((result.state_dir / "attempts" / "001" / "candidate.lean").is_file())
            self.assertTrue((result.state_dir / "reviews" / "001.json").is_file())
            final_audit = json.loads(
                (result.state_dir / "final-audit.json").read_text(encoding="utf-8")
            )
            self.assertTrue(final_audit["ok"])
            agent_roles = [
                json.loads((path / "request.json").read_text(encoding="utf-8"))["role"]
                for path in sorted((result.state_dir / "agent-calls").iterdir())
            ]
            self.assertEqual(agent_roles, ["planner", "prover", "reviewer", "auditor"])
            timings = json.loads(
                (result.state_dir / "timings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(timings["status"], "succeeded")
            self.assertEqual(timings["phase_counts"]["plan_api"], 1)
            self.assertEqual(timings["phase_counts"]["prove_api"], 1)
            self.assertEqual(timings["phase_counts"]["lean_check"], 1)

    def test_subscription_identity_is_persisted_at_run_level(self) -> None:
        class Backend:
            backend_id = "codex-subscription"
            last_metadata: dict[str, object] = {"backend_id": backend_id}

            def inspect(self, *, model: str, reasoning_effort: str):
                return {
                    "status": "ready",
                    "backend_id": self.backend_id,
                    "cli_version": "codex-cli 0.144.4",
                    "authentication_type": "chatgpt",
                    "requested_model": model,
                    "requested_model_catalog_status": "VALIDATED",
                    "actual_model": None,
                    "actual_model_status": "NOT_REPORTED_BY_CLIENT",
                    "model_identity_source": "REQUESTED_MODEL_AND_OFFICIAL_CATALOG_ONLY",
                    "requested_reasoning_effort": reasoning_effort,
                    "effective_reasoning_effort": reasoning_effort,
                    "tool_execution_policy": "TOOL_ENABLED_AGENT_SANDBOX",
                    "filesystem_read_scope": "WINDOWS_BROAD_READ",
                    "filesystem_write_scope": (
                        "REPO_EXTERNAL_EPHEMERAL_WORKSPACE"
                    ),
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
                }

            def invoke(self, request, config, temp_dir):
                self.last_metadata = self.inspect(
                    model=config.model,
                    reasoning_effort=config.reasoning_effort,
                )
                if request.output_type == "lean_file":
                    return "example : True := by trivial\n"
                if request.role == "planner":
                    return {
                        "summary": "repair",
                        "steps": [{
                            "id": "step-1",
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="low",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=Backend(),
                agent_backend_id="codex-subscription",
            )

            self.assertTrue(result.ok)
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            identity = manifest["settings"]["backend_identity"]
            self.assertEqual(identity["backend_id"], "codex-subscription")
            self.assertEqual(identity["cli_version"], "codex-cli 0.144.4")
            self.assertEqual(identity["authentication_type"], "chatgpt")
            self.assertEqual(
                identity["tool_execution_policy"], "TOOL_ENABLED_AGENT_SANDBOX"
            )
            self.assertEqual(identity["filesystem_read_scope"], "WINDOWS_BROAD_READ")
            self.assertEqual(
                identity["filesystem_write_scope"],
                "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
            )
            self.assertEqual(
                identity["read_isolation_status"],
                "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
            )
            self.assertEqual(identity["network_policy"], "DISABLED")
            for role in ("plan", "prove", "review"):
                request_identity = identity["requests"][role]
                self.assertEqual(request_identity["requested_model"], "gpt-5.6-sol")
                self.assertEqual(
                    request_identity["requested_model_catalog_status"], "VALIDATED"
                )
                self.assertIsNone(request_identity["actual_model"])
                self.assertEqual(
                    request_identity["actual_model_status"],
                    "NOT_REPORTED_BY_CLIENT",
                )
                self.assertEqual(request_identity["requested_reasoning_effort"], "low")
                self.assertEqual(request_identity["effective_reasoning_effort"], "low")

    def test_broad_mathlib_import_is_probed_before_lean_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory),
                "import Mathlib\nexample : True := by exact useful_true\n",
            )
            build_mathlib_index(project)
            checked_sources: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                checked_sources.append(source)
                precise = "import Mathlib.Logic.Basic" in source
                return LeanCheck(
                    precise,
                    0 if precise else 1,
                    "" if precise else "broad import was checked",
                    (lake, "env", "lean", "Main.lean"),
                )

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "repair existing proof",
                        "steps": [
                            {
                                "id": "step-1",
                                "goal": "prove the existing proposition",
                                "success_criteria": "Lean exits zero",
                                "search_terms": [],
                            }
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "precise import and proof pass",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="repair the existing sorry proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: target.read_text(
                    encoding="utf-8"
                ),
                lean_checker=checker,
            )
            self.assertTrue(result.ok)
            self.assertTrue(checked_sources)
            self.assertTrue(all("import Mathlib\n" not in source for source in checked_sources))
            self.assertIn("import Mathlib.Logic.Basic", target.read_text(encoding="utf-8"))
            initial_retrieval = json.loads(
                (result.state_dir / "initial-retrieval.json").read_text(encoding="utf-8")
            )
            attempt_retrieval = json.loads(
                (result.state_dir / "attempts" / "001" / "retrieval.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(initial_retrieval["import_optimization"]["probe_ok"])
            self.assertTrue(attempt_retrieval["import_optimization"]["probe_ok"])

    def test_executes_plan_steps_as_checked_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- workspace\n")
            prover_prompts: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "two checked milestones",
                        "steps": [
                            {
                                "id": "helper",
                                "goal": "add helper",
                                "success_criteria": "helper compiles",
                                "search_terms": [],
                            },
                            {
                                "id": "final",
                                "goal": "add final theorem",
                                "success_criteria": "final theorem compiles",
                                "search_terms": [],
                            },
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "active milestone is present and Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                prover_prompts.append(prompt)
                if len(prover_prompts) == 1:
                    return "def helper : True := by trivial\n"
                return (
                    "def helper : True := by trivial\n"
                    "example : True := by exact helper\n"
                )

            result = run_structured_workflow(
                project=project,
                target=target,
                task="build in steps",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=4,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.attempts, 2)
            checkpoints = sorted((result.state_dir / "checkpoints").glob("*"))
            self.assertEqual(len(checkpoints), 2)
            self.assertTrue((checkpoints[0] / "source.lean").is_file())
            self.assertIn("helper", prover_prompts[1])
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [step["status"] for step in manifest["steps"]],
                ["succeeded", "succeeded"],
            )

    def test_failed_candidate_is_next_attempt_working_source(self) -> None:
        original = "example : True := by exact original_missing\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)
            prover_prompts: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "repair incrementally",
                        "steps": [{
                            "id": "repair",
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "review",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                prover_prompts.append(prompt)
                self.assertEqual(target.read_text(encoding="utf-8"), original)
                if len(prover_prompts) == 1:
                    return "example : True := by exact first_candidate_marker\n"
                self.assertIn("first_candidate_marker", prompt)
                return "example : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove the example",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertEqual(len(prover_prompts), 2)
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertIsNone(manifest["attempts"][0]["base_attempt"])
            self.assertEqual(manifest["attempts"][1]["base_attempt"], 1)
            events = (result.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "failed_candidate_kept_as_working_source"', events)

    def test_resume_uses_last_failed_candidate_without_exposing_it_on_disk(self) -> None:
        original = "example : True := by exact original_missing\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)
            prover_prompts: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "repair across resume",
                        "steps": [{
                            "id": "repair",
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "review",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                prover_prompts.append(prompt)
                self.assertEqual(target.read_text(encoding="utf-8"), original)
                if len(prover_prompts) == 1:
                    return "example : True := by exact resume_candidate_marker\n"
                self.assertIn("resume_candidate_marker", prompt)
                return "example : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="prove the example",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertFalse(first.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="prove the example",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            manifest = json.loads((resumed.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["attempts"][1]["base_attempt"], 1)
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "working_candidate_restored"', events)

    def test_resume_reuses_plan_attempts_and_last_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- start\n")
            planner_calls = 0
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": "two steps",
                        "steps": [
                            {"id": "one", "goal": "helper", "success_criteria": "helper", "search_terms": []},
                            {"id": "two", "goal": "final", "success_criteria": "final", "search_terms": []},
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                if prover_calls == 1:
                    return "def helper : True := by trivial\n"
                return "def helper : True := by trivial\ntheorem final : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="resume me",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertFalse(first.ok)
            self.assertIn("helper", target.read_text(encoding="utf-8"))

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="resume me",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=3,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertTrue(resumed.ok)
            self.assertEqual(resumed.run_id, first.run_id)
            self.assertEqual(planner_calls, 1)
            manifest = json.loads((resumed.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["resume_count"], 1)
            self.assertEqual(len(manifest["attempts"]), 2)

    def test_resume_skips_partial_attempt_directory_not_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(
                    ok,
                    0 if ok else 1,
                    "" if ok else "Unknown identifier `missing`",
                    (lake, "env", "lean", "Main.lean"),
                )

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "recover partial attempt",
                        "steps": [
                            {
                                "id": "step-1",
                                "goal": "prove True",
                                "success_criteria": "Lean exits zero",
                                "search_terms": [],
                            }
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                if prover_calls == 1:
                    return "example : True := by exact missing\n"
                return "example : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="recover me",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertFalse(first.ok)

            partial = first.state_dir / "attempts" / "002"
            partial.mkdir()
            (partial / "retrieval.json").write_text("{}\n", encoding="utf-8")

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="recover me",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=3,
                max_attempts_per_step=3,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertTrue((resumed.state_dir / "attempts" / "003").is_dir())
            manifest = json.loads((resumed.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual([row["attempt"] for row in manifest["attempts"]], [1, 3])
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "partial_attempts_recovered"', events)
            self.assertIn('"attempts": [2]', events)

    def test_step_budget_is_separate_from_global_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- start\n")

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(False, 1, "still failing", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "bounded",
                        "steps": [{"id": "one", "goal": "try", "success_criteria": "pass", "search_terms": []}],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "retry",
                    "summary": "retry",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="bounded",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=5,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: "def candidate : True := by trivial\n",
                lean_checker=checker,
            )
            self.assertFalse(result.ok)
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["attempts"]), 2)
            self.assertEqual(manifest["steps"][0]["budget_exhausted"], "step")

    def test_statement_guard_rejects_before_lean_and_rolls_back(self) -> None:
        original = "theorem kept : True := by trivial\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)
            checks = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                nonlocal checks
                checks += 1
                return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "guard",
                        "steps": [{"goal": "change", "success_criteria": "pass", "search_terms": []}],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "retry",
                    "summary": "guard failed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="do not change statement",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: "theorem kept : False := by trivial\n",
                lean_checker=checker,
            )
            self.assertFalse(result.ok)
            self.assertEqual(checks, 1)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            audit = json.loads(
                (result.state_dir / "attempts" / "001" / "audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(audit["ok"])

    def test_global_audit_can_reject_an_individually_accepted_final_step(self) -> None:
        original = "-- baseline\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "one step",
                        "steps": [{"id": "final", "goal": "finish", "success_criteria": "complete task", "search_terms": []}],
                        "preserve": [],
                        "risks": [],
                    }
                if "global-final-audit" in user:
                    return {
                        "verdict": "retry",
                        "summary": "the original task is incomplete",
                        "failure_analysis": ["missing requested theorem"],
                        "next_actions": ["complete the theorem"],
                        "search_terms": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "step accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="finish everything",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: "def helper : True := by trivial\n",
                lean_checker=checker,
            )
            self.assertFalse(result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            final_audit = json.loads(
                (result.state_dir / "final-audit.json").read_text(encoding="utf-8")
            )
            self.assertFalse(final_audit["ok"])
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["steps"][-1]["status"], "failed")
            self.assertIn("rejected_checkpoint", manifest["steps"][-1])

    def test_global_auditor_api_failure_does_not_reject_deterministic_success(self) -> None:
        original = "-- baseline\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "one step",
                        "steps": [{"id": "final", "goal": "finish", "success_criteria": "pass", "search_terms": []}],
                        "preserve": [],
                        "risks": [],
                    }
                if "global-final-audit" in user:
                    raise ApiError("Concurrency limit exceeded")
                return {
                    "verdict": "accept",
                    "summary": "step accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="finish everything",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: "def helper : True := by trivial\n",
                lean_checker=checker,
            )
            self.assertTrue(result.ok)
            self.assertIn("def helper", target.read_text(encoding="utf-8"))
            final_audit = json.loads(
                (result.state_dir / "final-audit.json").read_text(encoding="utf-8")
            )
            self.assertTrue(final_audit["ok"])
            self.assertEqual(final_audit["review"]["review_status"], "unavailable")
            self.assertEqual(
                final_audit["review"]["accepted_by"], "deterministic_final_audit"
            )

    def test_failure_restores_original_but_preserves_candidate(self) -> None:
        original = "example : True := by exact missing\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)
            checks = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                nonlocal checks
                checks += 1
                return LeanCheck(
                    False,
                    1,
                    "Unknown identifier `still_missing`",
                    (lake, "env", "lean", "Main.lean"),
                )

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "try once",
                        "steps": [
                            {
                                "goal": "fix it",
                                "success_criteria": "Lean exits zero",
                                "search_terms": [],
                            }
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "retry",
                    "summary": "still fails",
                    "failure_analysis": ["missing name"],
                    "next_actions": ["search exact name"],
                    "search_terms": ["still_missing"],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: "example : True := by exact still_missing\n",
                lean_checker=checker,
            )
            self.assertFalse(result.ok)
            self.assertTrue(result.restored)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            candidate = result.state_dir / "attempts" / "001" / "candidate.lean"
            self.assertIn("still_missing", candidate.read_text(encoding="utf-8"))
            self.assertIn("still_missing", result.final_check.output)
            self.assertEqual(checks, 2)
            restore_check = json.loads(
                (result.state_dir / "restore-check.json").read_text(encoding="utf-8")
            )
            self.assertTrue(restore_check["reused_initial_check"])

    def test_cancellation_restores_original_and_marks_workflow_cancelled(self) -> None:
        original = "example : True := by exact missing\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)
            checks = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                nonlocal checks
                checks += 1
                if checks > 1:
                    raise ProcessCancelled("cancel test")
                return LeanCheck(False, 1, "missing", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                return {
                    "summary": "try",
                    "steps": [
                        {
                            "goal": "prove",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }
                    ],
                    "preserve": [],
                    "risks": [],
                }

            with self.assertRaises(ProcessCancelled):
                run_structured_workflow(
                    project=project,
                    target=target,
                    task="fix proof",
                    plan_config=_config(),
                    prove_config=_config(),
                    review_config=_config(),
                    max_attempts=1,
                    lean_timeout_seconds=10,
                    lake_executable="lake",
                    json_model_call=json_model,
                    file_model_call=lambda config, prompt, temp: "example : True := by trivial\n",
                    lean_checker=checker,
                )
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            manifests = list((project / ".lean-agent" / "workflows").glob("*/run.json"))
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "cancelled")
            self.assertTrue(manifest["restored"])

    def test_reviewer_stop_is_rejected_while_candidate_budget_remains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            review_count = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal review_count
                if "Planner" in system:
                    return {
                        "summary": "retry until Lean passes",
                        "steps": [{
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                review_count += 1
                if review_count == 1:
                    return {
                        "verdict": "stop",
                        "summary": "incorrectly claims the environment is blocked",
                        "failure_analysis": ["speculative blocker"],
                        "next_actions": [],
                        "search_terms": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            calls = 0

            def file_model(config, prompt, temp):
                nonlocal calls
                calls += 1
                return (
                    "example : True := by exact missing\n"
                    if calls == 1
                    else "example : True := by trivial\n"
                )

            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            first_review = json.loads(
                (result.state_dir / "reviews" / "001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(first_review["verdict"], "retry")
            self.assertTrue(first_review["stop_rejected"])

    def test_invalid_explicit_import_is_rejected_before_lean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            build_mathlib_index(project)
            checked_sources: list[str] = []
            reviewer_saw_correction = False

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                checked_sources.append(source)
                ok = "Mathlib.Logic.Basic" in source and "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal reviewer_saw_correction
                if "Planner" in system:
                    return {
                        "summary": "repair",
                        "steps": [{
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": ["useful_true"],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                if "Mathlib module does not exist" in user:
                    reviewer_saw_correction = "Mathlib.Logic.Basic" in user
                    return {
                        "verdict": "retry",
                        "summary": "use the local module",
                        "failure_analysis": ["invalid import"],
                        "next_actions": ["use Mathlib.Logic.Basic"],
                        "search_terms": ["useful_true"],
                    }
                return {
                    "verdict": "accept",
                    "summary": "Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            calls = 0

            def file_model(config, prompt, temp):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return "import Mathlib.Logic.Missing\nexample : True := by trivial\n"
                return "import Mathlib.Logic.Basic\nexample : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertTrue(reviewer_saw_correction)
            self.assertFalse(any("Mathlib.Logic.Missing" in source for source in checked_sources))

    def test_new_file_goal_is_formalized_and_statement_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- new theorem\n")

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = (
                    target.name == "FormalGoalCheck.lean"
                    or "theorem bounded_identity (n : ℕ) : n = n := by rfl" in source
                    or source == "-- new theorem\n"
                )
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "bad statement", ())

            def json_model(config, system, user, temp):
                if "Goal Formalizer" in system:
                    return {
                        "summary": "choose an explicit theorem",
                        "declaration": "theorem bounded_identity (n : ℕ) : n = n :=",
                        "search_terms": ["Nat"],
                        "assumptions": [],
                        "ambiguities": [],
                    }
                if "Planner" in system:
                    self.assertIn("bounded_identity", user)
                    return {
                        "summary": "prove the fixed declaration",
                        "steps": [{
                            "goal": "prove bounded_identity",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove a bounded identity",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                formalize_goal=True,
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: (
                    "theorem bounded_identity (n : ℕ) : n = n := by rfl\n"
                ),
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            goal = json.loads((result.state_dir / "goal.json").read_text(encoding="utf-8"))
            self.assertTrue(goal["validated"])
            self.assertFalse(goal["declaration"].endswith(":="))
            first_call = sorted(result.state_dir.joinpath("agent-calls").iterdir())[0]
            self.assertEqual(first_call.name.split("-")[1], "formalizer")

    def test_proof_first_checks_broad_import_before_minimizing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- new proof\n")
            build_mathlib_index(project)
            checked_sources: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                checked_sources.append(source)
                ok = source == "-- new proof\n" or (
                    "useful_true" in source
                    and ("import Mathlib\n" in source or "Mathlib.Logic.Basic" in source)
                )
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "bad import", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "prove with broad import first",
                        "steps": [{
                            "goal": "add theorem",
                            "success_criteria": "Lean passes",
                            "search_terms": ["useful_true"],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="add a theorem",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                import_policy="proof-first",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: (
                    "import Mathlib\ntheorem goal : True := by exact useful_true\n"
                ),
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertTrue(any("import Mathlib\n" in source for source in checked_sources))
            self.assertIn("import Mathlib.Logic.Basic", target.read_text(encoding="utf-8"))

    def test_resume_replans_old_new_file_workflow_after_goal_formalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- old generated file\n")
            plan_calls = 0
            prove_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = (
                    source == "-- old generated file\n"
                    or target.name == "FormalGoalCheck.lean"
                    or "theorem fixed_goal : True := by trivial" in source
                )
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal plan_calls
                if "Goal Formalizer" in system:
                    return {
                        "summary": "fix the target",
                        "declaration": "theorem fixed_goal : True",
                        "search_terms": [],
                        "assumptions": [],
                        "ambiguities": [],
                    }
                if "Planner" in system:
                    plan_calls += 1
                    return {
                        "summary": "old wrong plan" if plan_calls == 1 else "new formal plan",
                        "steps": [{
                            "goal": "wrong" if plan_calls == 1 else "prove fixed_goal",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "review",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prove_calls
                prove_calls += 1
                if prove_calls == 1:
                    return "theorem wrong : True := by exact missing\n"
                return "theorem fixed_goal : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="prove a theorem",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertFalse(first.ok)

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="prove a theorem",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                formalize_goal=True,
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(plan_calls, 2)
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("plan_replaced_for_formal_goal", events)

    def test_malformed_prover_output_is_archived_and_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            prove_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "repair",
                        "steps": [{
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "review",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prove_calls
                prove_calls += 1
                if prove_calls == 1:
                    raise MalformedModelOutputError(
                        "Model did not return the required JSON object",
                        "I think the proof should use simp.",
                    )
                return "example : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            first_call = next(
                path
                for path in sorted((result.state_dir / "agent-calls").iterdir())
                if path.name.startswith("0002-prover-")
            )
            self.assertTrue((first_call / "raw-output.txt").is_file())
            self.assertTrue((result.state_dir / "api-failures" / "001" / "error.json").is_file())
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["api_failures"]), 1)
            self.assertEqual([row["attempt"] for row in manifest["attempts"]], [1])

    def test_new_formal_goal_is_required_only_on_final_plan_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- new theorem\n")
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", ())

            def json_model(config, system, user, temp):
                if "Goal Formalizer" in system:
                    return {
                        "summary": "formal goal",
                        "declaration": "theorem final_goal : True",
                        "search_terms": [],
                        "assumptions": [],
                        "ambiguities": [],
                    }
                if "Planner" in system:
                    return {
                        "summary": "helper then final",
                        "steps": [
                            {
                                "id": "helper",
                                "goal": "add helper",
                                "success_criteria": "helper exists",
                                "search_terms": [],
                                "required_declarations": ["helper_goal"],
                            },
                            {
                                "id": "final",
                                "goal": "add final goal",
                                "success_criteria": "final goal exists",
                                "search_terms": [],
                                "required_declarations": [],
                            },
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                if prover_calls == 1:
                    return "lemma helper_goal : True := by trivial\n"
                return (
                    "lemma helper_goal : True := by trivial\n"
                    "theorem final_goal : True := by trivial\n"
                )

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove a new theorem",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                formalize_goal=True,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            first_audit = json.loads(
                (result.state_dir / "attempts" / "001" / "audit.json").read_text(
                    encoding="utf-8"
                )
            )
            second_audit = json.loads(
                (result.state_dir / "attempts" / "002" / "audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(first_audit["required_declaration"])
            self.assertEqual(first_audit["required_declaration_names"], ["helper_goal"])
            self.assertEqual(second_audit["required_declaration"], "final_goal")
            self.assertEqual(
                second_audit["required_declaration_names"],
                ["helper_goal", "final_goal"],
            )

    def test_exact_local_theorem_uses_direct_plan_and_filters_placeholder_terms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory),
                "-- Created by Lean Agent. The formal goal and imports will be generated below.\n",
            )
            build_mathlib_index(project)
            formalizer_prompt = ""

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", ())

            def json_model(config, system, user, temp):
                nonlocal formalizer_prompt
                if "Goal Formalizer" in system:
                    formalizer_prompt = user
                    return {
                        "summary": "direct goal",
                        "declaration": "theorem direct_goal : True",
                        "search_terms": ["useful_true"],
                        "assumptions": [],
                        "ambiguities": [],
                    }
                if "Planner" in system:
                    raise AssertionError("exact theorem evidence should bypass Planner")
                return {
                    "verdict": "accept",
                    "summary": "accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove a theorem using `useful_true`",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                formalize_goal=True,
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: (
                    "import Mathlib.Logic.Basic\n"
                    "theorem direct_goal : True := by exact useful_true\n"
                ),
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            plan = json.loads((result.state_dir / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["steps"][0]["id"], "direct-proof")
            self.assertNotIn("query='Created'", formalizer_prompt)
            events = (result.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("direct_plan_selected", events)

    def test_resume_replans_after_repeated_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            planner_calls = 0
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "same failure", ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": f"plan {planner_calls}",
                        "steps": [{
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                            "required_declarations": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "review",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                if prover_calls <= 2:
                    return "example : True := by exact missing\n"
                return "example : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertFalse(first.ok)

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=3,
                max_attempts_per_step=3,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(planner_calls, 2)
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"reason": "repeated_diagnostics"', events)

    def test_resume_replans_when_model_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            planner_calls = 0
            prover_calls = 0
            prover_prompts: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": f"plan {planner_calls}",
                        "steps": [{
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "review",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                prover_prompts.append(prompt)
                if prover_calls == 2:
                    self.assertNotIn("old_plan_candidate_marker", prompt)
                return (
                    "example : True := by exact old_plan_candidate_marker\n"
                    if prover_calls == 1
                    else "example : True := by trivial\n"
                )

            first = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            changed = ApiConfig(
                api_base="http://example.invalid/v1",
                api_key="not-used",
                model="different-model",
                mode="responses",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="low",
            )
            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=changed,
                prove_config=changed,
                review_config=changed,
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(planner_calls, 2)
            self.assertEqual(len(prover_prompts), 2)
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"reason": "model_changed"', events)

    def test_formal_goal_qualifies_ambiguous_names(self) -> None:
        goal = validate_formal_goal(
            {
                "summary": "bounded sequence",
                "declaration": (
                    "theorem goal (s : Set Nat) (h : IsBounded s) : "
                    "Tendsto id atTop atTop"
                ),
                "search_terms": [],
                "assumptions": [],
                "ambiguities": [],
            }
        )
        declaration = goal["declaration"]
        self.assertIn("Bornology.IsBounded", declaration)
        self.assertIn("Filter.Tendsto", declaration)
        self.assertEqual(declaration.count("Filter.atTop"), 2)

    def test_formal_goal_uses_one_lean_guided_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- new theorem\n")
            formalizer_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                if target.name == "FormalGoalCheck.lean":
                    ok = "MissingType" not in source
                else:
                    ok = source == "-- new theorem\n" or "theorem repaired_goal" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "Unknown identifier `MissingType`", ())

            def json_model(config, system, user, temp):
                nonlocal formalizer_calls
                if "Goal Formalizer" in system:
                    formalizer_calls += 1
                    declaration = (
                        "theorem repaired_goal (x : MissingType) : True"
                        if formalizer_calls == 1
                        else "theorem repaired_goal : True"
                    )
                    return {
                        "summary": "formalize",
                        "declaration": declaration,
                        "search_terms": [],
                        "assumptions": [],
                        "ambiguities": [],
                    }
                if "Planner" in system:
                    return {
                        "summary": "prove repaired goal",
                        "steps": [{
                            "goal": "prove repaired_goal",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                return {
                    "verdict": "accept",
                    "summary": "Lean passed",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove a theorem",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                formalize_goal=True,
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: (
                    "theorem repaired_goal : True := by trivial\n"
                ),
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertEqual(formalizer_calls, 2)


if __name__ == "__main__":
    unittest.main()
