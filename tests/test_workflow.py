import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.api import ApiError, MalformedModelOutputError
from lean_loop.audit import audit_source as real_audit_source
from lean_loop.config import ApiConfig
from lean_loop.jsonutil import atomic_write_text as real_atomic_write_text, sha256_text
from lean_loop.lean import LeanCheck, check_lean
from lean_loop.mathlib_search import (
    has_broad_import,
    optimize_broad_imports,
    validate_mathlib_imports as real_validate_mathlib_imports,
)
from lean_loop.mathlib_index import build_mathlib_index
from lean_loop.process_control import ProcessCancelled
from lean_loop.prompts import PROVER_SYSTEM_PROMPT
from lean_loop.workflow import (
    _effective_import_policy,
    _historical_effective_import_policy,
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
    def test_prover_prompt_keeps_proof_phase_broad_imports_orchestrator_owned(self) -> None:
        prompt = " ".join(PROVER_SYSTEM_PROMPT.split())
        self.assertIn(
            "During `proof-first` and `broad` proof phases, import breadth is "
            "orchestrator-owned.",
            prompt,
        )
        self.assertIn(
            "Do not narrow or remove a standalone `import Mathlib`.",
            prompt,
        )
        self.assertIn(
            "Retrieval remains available for theorem and premise selection, not "
            "proof-time import restriction.",
            prompt,
        )
        self.assertIn(
            "Only explicit `precise` may use locally evidenced fine imports.",
            prompt,
        )
        self.assertNotIn(
            "A broad `import Mathlib` is automatically probed for replacement before Lean\n"
            "checks.",
            PROVER_SYSTEM_PROMPT,
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

    def _run_terminal_reduction_case(
        self,
        root: Path,
        *,
        source: str = "example : True := by exact missing\n",
        import_policy: str = "proof-first",
        suggestions: list[dict[str, object]] | None = None,
        steps: list[dict[str, object]] | None = None,
        candidates: list[str] | None = None,
        global_verdict: str = "accept",
        checker_override=None,
        keep_failed: bool = False,
        workflow_created_callback=None,
    ):
        project, target = self._project(root, source)
        timeline: list[tuple[str, object]] = []
        retrieval_calls: list[list[str]] = []
        prover_index = 0
        completed_reviewers = 0
        plan_steps = steps or [
            {
                "id": "step-1",
                "goal": "prove True",
                "success_criteria": "Lean passes",
                "search_terms": ["plan_term"],
            }
        ]
        prover_candidates = candidates or [
            "example : True := by exact useful_true\n"
        ]
        reduction_suggestions = suggestions
        if reduction_suggestions is None:
            reduction_suggestions = [
                {
                    "module": "Mathlib.Logic.Basic",
                    "confidence": "high",
                    "queries": ["useful_true"],
                    "evidence": ["Mathlib/Logic/Basic.lean:1 theorem useful_true"],
                }
            ]

        def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
            checked_source = target.read_text(encoding="utf-8")
            timeline.append(("lean", checked_source))
            if checker_override is not None:
                return checker_override(checked_source)
            ok = "missing" not in checked_source
            return LeanCheck(
                ok,
                0 if ok else 1,
                "" if ok else "Unknown identifier `missing`",
                (lake, "env", "lean", "Main.lean"),
            )

        def json_model(config, system, user, temp):
            nonlocal completed_reviewers
            if "Planner" in system:
                timeline.append(("model", "planner"))
                return {
                    "summary": "terminal reduction plan",
                    "steps": plan_steps,
                    "preserve": [],
                    "risks": [],
                }
            if "global-final-audit" in user:
                timeline.append(("model", "auditor"))
                verdict = global_verdict
            else:
                timeline.append(("model", "reviewer"))
                completed_reviewers += 1
                verdict = "accept"
            return {
                "verdict": verdict,
                "summary": "reviewed",
                "failure_analysis": [] if verdict == "accept" else ["reject final"],
                "next_actions": [],
                "search_terms": [],
            }

        def file_model(config, prompt, temp):
            nonlocal prover_index
            timeline.append(("model", "prover"))
            candidate = prover_candidates[prover_index]
            prover_index += 1
            return candidate

        def fake_retrieval(
            project: Path,
            *,
            diagnostics: str,
            requested_terms: list[str],
            process_control=None,
        ) -> dict[str, object]:
            queries = list(requested_terms)
            current_suggestions = (
                list(reduction_suggestions)
                if completed_reviewers == len(plan_steps)
                else []
            )
            retrieval_calls.append(queries)
            timeline.append(("retrieval", queries))
            timeline.append(("retrieval_suggestions", current_suggestions))
            return {
                "queries": queries,
                "hits": [],
                "module_checks": [],
                "import_suggestions": current_suggestions,
            }

        def phase_callback(phase: str, attempt: int | None) -> None:
            timeline.append(("phase", phase))

        with patch("lean_loop.workflow._safe_retrieval", side_effect=fake_retrieval):
            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove using `plan_term`",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=max(2, len(plan_steps)),
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                keep_failed=keep_failed,
                import_policy=import_policy,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
                phase_callback=phase_callback,
                workflow_created_callback=workflow_created_callback,
            )
        return result, target, timeline, retrieval_calls

    def test_new_import_policy_resolution_is_proof_first_for_existing_source(self) -> None:
        source = "theorem existing : True := by trivial\n"

        self.assertEqual(_effective_import_policy("auto", source), "proof-first")
        self.assertEqual(_effective_import_policy("precise", source), "precise")
        self.assertEqual(_effective_import_policy("broad", source), "broad")

    def test_missing_historical_effective_policy_preserves_explicit_and_legacy_auto(self) -> None:
        existing_source = "theorem existing : True := by trivial\n"
        new_source = "-- new theorem\n"

        for raw_policy in ("proof-first", "precise", "broad"):
            with self.subTest(raw_policy=raw_policy):
                self.assertEqual(
                    _historical_effective_import_policy(
                        {"settings": {"import_policy": raw_policy}}, existing_source
                    ),
                    (raw_policy, False),
                )
        self.assertEqual(
            _historical_effective_import_policy(
                {"settings": {"import_policy": "auto"}}, existing_source
            ),
            ("precise", True),
        )
        self.assertEqual(
            _historical_effective_import_policy(
                {"settings": {"import_policy": "auto"}}, new_source
            ),
            ("proof-first", True),
        )
        for historical_effective in ("proof-first", "precise", "broad"):
            with self.subTest(historical_effective=historical_effective):
                self.assertEqual(
                    _historical_effective_import_policy(
                        {
                            "settings": {
                                "import_policy": "auto",
                                "effective_import_policy": historical_effective,
                            }
                        },
                        existing_source,
                    ),
                    (historical_effective, False),
                )

    def test_raw_import_policy_change_forces_resume_replan(self) -> None:
        previous = {
            "settings": {
                "import_policy": "auto",
                "effective_import_policy": "precise",
            },
            "attempts": [],
        }
        current = {
            "import_policy": "broad",
            "effective_import_policy": "broad",
        }

        self.assertEqual(
            _resume_replan_reason(None, previous, current),
            "import_policy_changed",
        )

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
                import_policy="precise",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertTrue(result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), "example : True := by trivial\n")
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "succeeded")
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
                import_policy="precise",
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

    def test_precise_preflight_remove_only_no_suggestion_and_failure_restore(self) -> None:
        scenarios = (
            {
                "name": "remove_only",
                "suggestions": [
                    {"module": "Mathlib.Logic.Basic", "confidence": "high"}
                ],
                "candidate": (
                    "import Mathlib.Logic.Basic\n"
                    "-- prover remove-only candidate\n"
                    "example : True := by trivial\n"
                ),
                "expected_preflight": (
                    "import Mathlib.Logic.Basic\n"
                    "example : True := by exact missing\n"
                ),
                "expected_reason": "high_confidence_remove_only",
                "expected_selected": ["Mathlib.Logic.Basic"],
                "expected_added": [],
                "expect_success": True,
                "expect_broad": False,
            },
            {
                "name": "no_suggestion",
                "suggestions": [
                    {"module": "Mathlib.Logic.Basic", "confidence": "candidate"}
                ],
                "candidate": (
                    "import Mathlib.Logic.Basic\n"
                    "import Mathlib\n"
                    "-- prover no-suggestion candidate\n"
                    "example : True := by trivial\n"
                ),
                "expected_preflight": (
                    "import Mathlib.Logic.Basic\n"
                    "import Mathlib\n"
                    "example : True := by exact missing\n"
                ),
                "expected_reason": "no_high_confidence_imports",
                "expected_selected": [],
                "expected_added": [],
                "expect_success": True,
                "expect_broad": True,
            },
            {
                "name": "failed_remove_only_probe",
                "suggestions": [
                    {"module": "Mathlib.Logic.Basic", "confidence": "high"}
                ],
                "candidate": (
                    "import Mathlib.Logic.Basic\n"
                    "-- prover failing candidate\n"
                    "example : True := by exact missing\n"
                ),
                "expected_preflight": (
                    "import Mathlib.Logic.Basic\n"
                    "example : True := by exact missing\n"
                ),
                "expected_reason": "high_confidence_remove_only",
                "expected_selected": ["Mathlib.Logic.Basic"],
                "expected_added": [],
                "expect_success": False,
                "expect_broad": True,
            },
        )
        for scenario in scenarios:
            with self.subTest(name=scenario["name"]), tempfile.TemporaryDirectory() as directory:
                original = (
                    "import Mathlib.Logic.Basic\n"
                    "import Mathlib\n"
                    "example : True := by exact missing\n"
                )
                project, target = self._project(Path(directory), original)

                def fake_retrieval(
                    project: Path,
                    *,
                    diagnostics: str,
                    requested_terms: list[str],
                    process_control=None,
                ) -> dict[str, object]:
                    return {
                        "queries": list(requested_terms),
                        "hits": [],
                        "module_checks": [],
                        "import_suggestions": list(scenario["suggestions"]),
                    }

                checked_sources: list[str] = []

                def checker(
                    project: Path, target: Path, timeout: int, lake: str
                ) -> LeanCheck:
                    source = target.read_text(encoding="utf-8")
                    checked_sources.append(source)
                    ok = "by trivial" in source
                    return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

                def json_model(config, system, user, temp):
                    if "Planner" in system:
                        return {
                            "summary": "precise preflight",
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
                        "verdict": "accept" if "Lean check success: true" in user else "retry",
                        "summary": "reviewed",
                        "failure_analysis": [],
                        "next_actions": [],
                        "search_terms": [],
                    }

                with patch(
                    "lean_loop.workflow._safe_retrieval", side_effect=fake_retrieval
                ):
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
                        import_policy="precise",
                        json_model_call=json_model,
                        file_model_call=lambda config, prompt, temp: str(
                            scenario["candidate"]
                        ),
                        lean_checker=checker,
                    )

                self.assertEqual(result.ok, scenario["expect_success"])
                initial_retrieval = json.loads(
                    (result.state_dir / "initial-retrieval.json").read_text(
                        encoding="utf-8"
                    )
                )
                optimization = initial_retrieval["import_optimization"]
                self.assertEqual(optimization["reason"], scenario["expected_reason"])
                self.assertEqual(
                    optimization["selected_modules"], scenario["expected_selected"]
                )
                self.assertEqual(
                    optimization["added_modules"], scenario["expected_added"]
                )
                self.assertFalse(optimization["probe_ok"])
                self.assertEqual(optimization["probe_returncode"], 1)
                self.assertGreaterEqual(len(checked_sources), 2)
                self.assertEqual(checked_sources[0], scenario["expected_preflight"])
                self.assertEqual(checked_sources[1], scenario["candidate"])
                self.assertNotEqual(checked_sources[0], checked_sources[1])
                final_source = target.read_text(encoding="utf-8")
                self.assertEqual(has_broad_import(final_source), scenario["expect_broad"])
                if not scenario["expect_success"]:
                    self.assertEqual(final_source, original)
                    manifest = json.loads(
                        (result.state_dir / "run.json").read_text(encoding="utf-8")
                    )
                    self.assertTrue(manifest["restored"])
                    self.assertEqual(manifest["current_sha256"], sha256_text(original))

    def test_auto_keeps_existing_declaration_broad_for_planner_prover_and_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory),
                "-- original comment\nexample : True := by exact missing\n",
            )
            planner_prompt = ""
            prover_prompt = ""
            reviewer_candidates: list[str] = []
            checked_sources: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                checked_sources.append(source)
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal planner_prompt
                if "Planner" in system:
                    planner_prompt = user
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
                if "Reviewer" in system:
                    reviewer_candidates.append(user)
                return {
                    "verdict": "accept",
                    "summary": "accepted",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_prompt
                prover_prompt = prompt
                return "-- candidate comment\nexample : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix the existing declaration",
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

            self.assertTrue(result.ok)
            self.assertIn("import Mathlib\n", planner_prompt)
            self.assertIn("import Mathlib\n", prover_prompt)
            self.assertTrue(reviewer_candidates)
            self.assertTrue(all("import Mathlib\n" in row for row in reviewer_candidates))
            self.assertTrue(all("import Mathlib\n" in row for row in checked_sources))
            checkpoint = next((result.state_dir / "checkpoints").glob("*"))
            self.assertIn(
                "import Mathlib\n",
                (checkpoint / "source.lean").read_text(encoding="utf-8"),
            )
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["settings"]["effective_import_policy"], "proof-first")
            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(artifact["attempted"])
            self.assertEqual(artifact["effective_import_policy"], "proof-first")

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

    def test_resume_retains_historical_effective_policy_and_resolves_legacy_auto(self) -> None:
        for missing_effective in (False, True):
            with self.subTest(missing_effective=missing_effective):
                with tempfile.TemporaryDirectory() as directory:
                    project, target = self._project(
                        Path(directory),
                        "import Mathlib\nexample : True := by exact missing\n",
                    )
                    build_mathlib_index(project)
                    resuming = False
                    resumed_checks: list[str] = []

                    def checker(
                        project: Path, target: Path, timeout: int, lake: str
                    ) -> LeanCheck:
                        source = target.read_text(encoding="utf-8")
                        if resuming:
                            resumed_checks.append(source)
                            ok = (
                                "import Mathlib.Logic.Basic" in source
                                and "import Mathlib\n" not in source
                            )
                        else:
                            ok = False
                        return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

                    def json_model(config, system, user, temp):
                        if "Planner" in system:
                            return {
                                "summary": "historical plan",
                                "steps": [{
                                    "id": "step-1",
                                    "goal": "prove True",
                                    "success_criteria": "Lean passes",
                                    "search_terms": ["useful_true"],
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

                    first = run_structured_workflow(
                        project=project,
                        target=target,
                        task="repair using `useful_true`",
                        plan_config=_config(),
                        prove_config=_config(),
                        review_config=_config(),
                        max_attempts=1,
                        lean_timeout_seconds=10,
                        lake_executable="lake",
                        import_policy="auto",
                        json_model_call=json_model,
                        file_model_call=lambda config, prompt, temp: (
                            "import Mathlib\nexample : True := by exact missing\n"
                        ),
                        lean_checker=checker,
                    )
                    self.assertFalse(first.ok)
                    manifest_path = first.state_dir / "run.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if missing_effective:
                        manifest["settings"].pop("effective_import_policy", None)
                    else:
                        manifest["settings"]["effective_import_policy"] = "precise"
                    manifest_path.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )

                    resuming = True
                    resumed = run_structured_workflow(
                        project=project,
                        target=target,
                        task="repair using `useful_true`",
                        plan_config=_config(),
                        prove_config=_config(),
                        review_config=_config(),
                        max_attempts=2,
                        lean_timeout_seconds=10,
                        lake_executable="lake",
                        import_policy="auto",
                        resume_run_id=first.run_id,
                        json_model_call=json_model,
                        file_model_call=lambda config, prompt, temp: (
                            "import Mathlib.Logic.Basic\n"
                            "example : True := by trivial\n"
                        ),
                        lean_checker=checker,
                    )

                    self.assertTrue(resumed.ok)
                    self.assertTrue(resumed_checks)
                    self.assertIn("import Mathlib.Logic.Basic", resumed_checks[0])
                    self.assertNotIn("import Mathlib\n", resumed_checks[0])
                    resumed_manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        resumed_manifest["settings"]["effective_import_policy"],
                        "precise",
                    )
                    events = (resumed.state_dir / "events.jsonl").read_text(
                        encoding="utf-8"
                    )
                    if missing_effective:
                        self.assertIn("legacy_import_policy_resolved", events)
                    else:
                        self.assertNotIn("legacy_import_policy_resolved", events)

    def test_resume_historical_precise_checkpoint_uses_verified_broad_working_source(self) -> None:
        precise_source = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by exact useful_true\n"
        )
        suggestions = [{
            "module": "Mathlib.Logic.Basic",
            "confidence": "high",
            "queries": ["useful_true"],
            "evidence": ["Mathlib/Logic/Basic.lean:1 theorem useful_true"],
        }]
        for cancel_global_audit in (False, True):
            with self.subTest(
                cancel_global_audit=cancel_global_audit
            ), tempfile.TemporaryDirectory() as directory:
                first, target, _, _ = self._run_terminal_reduction_case(
                    Path(directory),
                    import_policy="precise",
                    candidates=[precise_source],
                )
                self.assertTrue(first.ok)
                manifest_path = first.state_dir / "run.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                checkpoint = Path(manifest["steps"][0]["checkpoint"])
                checkpoint_source_before = (checkpoint / "source.lean").read_bytes()
                checkpoint_meta_before = (checkpoint / "checkpoint.json").read_bytes()
                checkpoint_sha = json.loads(
                    checkpoint_meta_before.decode("utf-8")
                )["candidate_sha256"]
                self.assertEqual(checkpoint_sha, sha256_text(precise_source))

                manifest["status"] = "failed"
                manifest["phase"] = "complete"
                manifest["error"] = "historical interruption before global audit"
                manifest["settings"]["import_policy"] = "proof-first"
                manifest["settings"]["effective_import_policy"] = "proof-first"
                manifest["current_sha256"] = checkpoint_sha
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                target.write_text(precise_source, encoding="utf-8")

                checked_sources: list[str] = []

                def checker(
                    project: Path, target: Path, timeout: int, lake: str
                ) -> LeanCheck:
                    source = target.read_text(encoding="utf-8")
                    checked_sources.append(source)
                    if cancel_global_audit and len(checked_sources) == 3:
                        raise ProcessCancelled("cancel historical global audit")
                    return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

                def json_model(config, system, user, temp):
                    self.assertIn("global-final-audit", user)
                    return {
                        "verdict": "accept",
                        "summary": "historical resume accepted",
                        "failure_analysis": [],
                        "next_actions": [],
                        "search_terms": [],
                    }

                with patch(
                    "lean_loop.workflow._safe_retrieval",
                    return_value={
                        "queries": ["useful_true"],
                        "hits": [],
                        "module_checks": [],
                        "import_suggestions": suggestions,
                    },
                ):
                    if cancel_global_audit:
                        with self.assertRaises(ProcessCancelled):
                            run_structured_workflow(
                                project=Path(directory),
                                target=target,
                                task="prove using `plan_term`",
                                plan_config=_config(),
                                prove_config=_config(),
                                review_config=_config(),
                                max_attempts=2,
                                max_attempts_per_step=1,
                                lean_timeout_seconds=10,
                                lake_executable="lake",
                                import_policy="proof-first",
                                resume_run_id=first.run_id,
                                json_model_call=json_model,
                                file_model_call=lambda *args: self.fail(
                                    "a completed historical plan must not call the Prover"
                                ),
                                lean_checker=checker,
                            )
                        resumed = None
                    else:
                        resumed = run_structured_workflow(
                            project=Path(directory),
                            target=target,
                            task="prove using `plan_term`",
                            plan_config=_config(),
                            prove_config=_config(),
                            review_config=_config(),
                            max_attempts=2,
                            max_attempts_per_step=1,
                            lean_timeout_seconds=10,
                            lake_executable="lake",
                            import_policy="proof-first",
                            resume_run_id=first.run_id,
                            json_model_call=json_model,
                            file_model_call=lambda *args: self.fail(
                                "a completed historical plan must not call the Prover"
                            ),
                            lean_checker=checker,
                        )

                self.assertGreaterEqual(len(checked_sources), 1)
                verified_broad_source = checked_sources[0]
                self.assertIn("import Mathlib\n", verified_broad_source)
                self.assertNotEqual(sha256_text(verified_broad_source), checkpoint_sha)
                self.assertEqual(
                    (checkpoint / "source.lean").read_bytes(), checkpoint_source_before
                )
                self.assertEqual(
                    (checkpoint / "checkpoint.json").read_bytes(), checkpoint_meta_before
                )
                final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if cancel_global_audit:
                    self.assertEqual(
                        target.read_text(encoding="utf-8"), verified_broad_source
                    )
                    self.assertEqual(final_manifest["status"], "cancelled")
                    self.assertTrue(final_manifest["restored"])
                    self.assertEqual(
                        final_manifest["current_sha256"],
                        sha256_text(verified_broad_source),
                    )
                    self.assertIsNone(final_manifest["restored_to_checkpoint"])
                else:
                    self.assertIsNotNone(resumed)
                    self.assertTrue(resumed.ok)
                    self.assertEqual(target.read_text(encoding="utf-8"), precise_source)
                    artifact = json.loads(
                        (first.state_dir / "final-import-reduction.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(
                        artifact["broad_source_sha256"],
                        sha256_text(verified_broad_source),
                    )
                    self.assertEqual(artifact["selected_source"], "precise")

    def test_resume_raw_policy_change_uses_new_resolver_and_replans_visibly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            planner_calls = 0
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": f"plan {planner_calls}",
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
                    "verdict": "accept" if "Lean check success: true" in user else "retry",
                    "summary": "reviewed",
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
                task="fix proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                import_policy="precise",
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
                max_attempts=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                import_policy="broad",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(planner_calls, 2)
            manifest = json.loads(
                (resumed.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["settings"]["import_policy"], "broad")
            self.assertEqual(
                manifest["settings"]["effective_import_policy"], "broad"
            )
            self.assertTrue(has_broad_import(target.read_text(encoding="utf-8")))
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "plan_replaced_on_resume"', events)
            self.assertIn('"reason": "import_policy_changed"', events)

    def test_resume_sha_gates_reject_tampering_before_models_or_overwrite(self) -> None:
        scenarios = (
            ("original", "Original workflow source hash no longer matches", False),
            ("target", "Target file changed after the last verified checkpoint", False),
            ("checkpoint", "Resume checkpoint hash mismatch", True),
        )
        for name, expected_error, needs_checkpoint in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                project, target = self._project(Path(directory), "-- start\n")
                candidates = iter(
                    [
                        "def helper : True := by trivial\n",
                        (
                            "def helper : True := by trivial\n"
                            "example : True := by exact missing\n"
                        ),
                    ]
                    if needs_checkpoint
                    else ["example : True := by exact missing\n"]
                )

                def checker(
                    project: Path, target: Path, timeout: int, lake: str
                ) -> LeanCheck:
                    source = target.read_text(encoding="utf-8")
                    ok = (
                        needs_checkpoint
                        and "def helper" in source
                        and "missing" not in source
                    )
                    return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

                def json_model(config, system, user, temp):
                    if "Planner" in system:
                        steps = [{
                            "id": "helper",
                            "goal": "add helper",
                            "success_criteria": "helper passes",
                            "search_terms": [],
                        }]
                        if needs_checkpoint:
                            steps.append({
                                "id": "final",
                                "goal": "finish proof",
                                "success_criteria": "final passes",
                                "search_terms": [],
                            })
                        return {
                            "summary": "prepare resume SHA gate",
                            "steps": steps,
                            "preserve": [],
                            "risks": [],
                        }
                    return {
                        "verdict": (
                            "accept" if "Lean check success: true" in user else "retry"
                        ),
                        "summary": "reviewed",
                        "failure_analysis": [],
                        "next_actions": [],
                        "search_terms": [],
                    }

                first = run_structured_workflow(
                    project=project,
                    target=target,
                    task="exercise resume SHA gates",
                    plan_config=_config(),
                    prove_config=_config(),
                    review_config=_config(),
                    max_attempts=2 if needs_checkpoint else 1,
                    max_attempts_per_step=1,
                    lean_timeout_seconds=10,
                    lake_executable="lake",
                    json_model_call=json_model,
                    file_model_call=lambda config, prompt, temp: next(candidates),
                    lean_checker=checker,
                )
                self.assertFalse(first.ok)
                manifest = json.loads(
                    (first.state_dir / "run.json").read_text(encoding="utf-8")
                )

                if name == "original":
                    original_path = first.state_dir / "original.lean"
                    original_path.write_text(
                        original_path.read_text(encoding="utf-8") + "-- tampered\n",
                        encoding="utf-8",
                    )
                elif name == "target":
                    target.write_text(
                        target.read_text(encoding="utf-8") + "-- external edit\n",
                        encoding="utf-8",
                    )
                else:
                    checkpoint = Path(manifest["steps"][0]["checkpoint"])
                    checkpoint_source = checkpoint / "source.lean"
                    checkpoint_source.write_text(
                        checkpoint_source.read_text(encoding="utf-8") + "-- tampered\n",
                        encoding="utf-8",
                    )
                target_before_resume = target.read_text(encoding="utf-8")
                resume_model_calls = 0

                def unexpected_model_call(*args, **kwargs):
                    nonlocal resume_model_calls
                    resume_model_calls += 1
                    raise AssertionError("resume SHA gate must run before model invocation")

                with self.assertRaisesRegex(ValueError, expected_error):
                    run_structured_workflow(
                        project=project,
                        target=target,
                        task="exercise resume SHA gates",
                        plan_config=_config(),
                        prove_config=_config(),
                        review_config=_config(),
                        max_attempts=3,
                        max_attempts_per_step=1,
                        lean_timeout_seconds=10,
                        lake_executable="lake",
                        resume_run_id=first.run_id,
                        json_model_call=unexpected_model_call,
                        file_model_call=unexpected_model_call,
                        lean_checker=checker,
                    )

                self.assertEqual(resume_model_calls, 0)
                self.assertEqual(target.read_text(encoding="utf-8"), target_before_resume)

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

    def test_preflight_write_terminal_state_matches_target_bytes(self) -> None:
        original = "example : True := by exact missing\n"
        broad = "import Mathlib\nexample : True := by exact missing\n"
        scenarios = (
            ("cancelled", ProcessCancelled, False),
            ("cancelled", ProcessCancelled, True),
            ("failed", RuntimeError, False),
            ("failed", RuntimeError, True),
        )
        for status, error_type, keep_failed in scenarios:
            with self.subTest(
                status=status,
                error_type=error_type.__name__,
                keep_failed=keep_failed,
            ), tempfile.TemporaryDirectory() as directory:
                project, target = self._project(Path(directory), original)
                run_ids: list[str] = []

                def checker(
                    project: Path, target: Path, timeout: int, lake: str
                ) -> LeanCheck:
                    self.assertEqual(target.read_text(encoding="utf-8"), broad)
                    raise error_type("preflight terminal state test")

                with patch(
                    "lean_loop.workflow._safe_retrieval",
                    return_value={"queries": [], "import_suggestions": []},
                ), self.assertRaises(error_type):
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
                        keep_failed=keep_failed,
                        json_model_call=lambda *args: self.fail(
                            "preflight failure must not call a model"
                        ),
                        file_model_call=lambda *args: self.fail(
                            "preflight failure must not call a model"
                        ),
                        lean_checker=checker,
                        workflow_created_callback=run_ids.append,
                    )

                expected_source = broad if keep_failed else original
                self.assertEqual(target.read_text(encoding="utf-8"), expected_source)
                manifest = json.loads(
                    (
                        project
                        / ".lean-agent"
                        / "workflows"
                        / run_ids[0]
                        / "run.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["status"], status)
                self.assertEqual(
                    manifest["current_sha256"], sha256_text(expected_source)
                )
                self.assertEqual(manifest["restored"], not keep_failed)
                self.assertIsNone(manifest["restored_to_checkpoint"])

    @unittest.skipUnless(
        os.environ.get("LEAN_LOOP_REAL_PROJECT"),
        "set LEAN_LOOP_REAL_PROJECT to run the real Lean smoke test",
    )
    def test_real_lean_post_success_import_reduction_smoke(self) -> None:
        project = Path(os.environ["LEAN_LOOP_REAL_PROJECT"]).expanduser().resolve()
        self.assertTrue((project / "lean-toolchain").is_file())
        self.assertTrue(
            (project / "lakefile.toml").is_file()
            or (project / "lakefile.lean").is_file()
        )
        broad_source = "import Mathlib\nexample : True := by exact True.intro\n"
        retrieval = {
            "import_suggestions": [
                {"module": "Mathlib.Logic.Basic", "confidence": "high"}
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".lean",
            prefix="LeanLoopImportReductionSmoke-",
            dir=project,
            delete=False,
            encoding="utf-8",
        ) as temporary:
            temporary.write(broad_source)
            target = Path(temporary.name)
        try:
            broad_check = check_lean(project, target, 120, "lake")
            self.assertTrue(broad_check.ok, broad_check.output)

            candidate, metadata = optimize_broad_imports(broad_source, retrieval)
            self.assertEqual(
                candidate,
                "import Mathlib.Logic.Basic\n"
                "example : True := by exact True.intro\n",
            )
            self.assertEqual(metadata["selected_modules"], ["Mathlib.Logic.Basic"])
            self.assertTrue(metadata["changed"])
            target.write_text(candidate, encoding="utf-8")

            reduced_check = check_lean(project, target, 120, "lake")
            self.assertTrue(reduced_check.ok, reduced_check.output)
        finally:
            target.unlink(missing_ok=True)

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

    def test_unrepairable_fine_import_fails_closed_before_lean_under_proof_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            original = "example : True := by exact missing\n"
            project, target = self._project(Path(directory), original)
            build_mathlib_index(project)
            checked_sources: list[str] = []
            reviewer_prompts: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                checked_sources.append(source)
                return LeanCheck(False, 1, "missing", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "reject invalid fine import",
                        "steps": [{
                            "id": "step-1",
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        }],
                        "preserve": [],
                        "risks": [],
                    }
                reviewer_prompts.append(user)
                return {
                    "verdict": "retry",
                    "summary": "invalid import",
                    "failure_analysis": ["invalid import"],
                    "next_actions": [],
                    "search_terms": [],
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
                import_policy="proof-first",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: (
                    "import Mathlib.Does.Not.Exist\n"
                    "example : True := by trivial\n"
                ),
                lean_checker=checker,
            )

            self.assertFalse(result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertFalse(
                any("Mathlib.Does.Not.Exist" in source for source in checked_sources)
            )
            attempt_dir = result.state_dir / "attempts" / "001"
            check = json.loads((attempt_dir / "check.json").read_text(encoding="utf-8"))
            retrieval = json.loads(
                (attempt_dir / "retrieval.json").read_text(encoding="utf-8")
            )
            self.assertEqual(check["command"], ["import-validation", "Main.lean"])
            self.assertFalse(retrieval["import_validation"]["ok"])
            self.assertFalse(retrieval["deterministic_import_repair"]["changed"])
            self.assertTrue(reviewer_prompts)
            self.assertIn("Mathlib module does not exist", reviewer_prompts[0])

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

    def test_proof_first_reduces_only_after_the_step_reviewer_accepts(self) -> None:
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
            self.assertTrue(
                any("import Mathlib.Logic.Basic" in source for source in checked_sources)
            )
            self.assertNotIn("import Mathlib\n", target.read_text(encoding="utf-8"))
            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(artifact["attempted"])
            self.assertEqual(artifact["selected_source"], "precise")

    def test_terminal_reduction_success_is_fresh_transactional_and_budget_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, target, timeline, retrieval_calls = self._run_terminal_reduction_case(
                Path(directory)
            )

            self.assertTrue(result.ok)
            selected_source = target.read_text(encoding="utf-8")
            self.assertIn("import Mathlib.Logic.Basic\n", selected_source)
            self.assertNotIn("import Mathlib\n", selected_source)
            reviewer_index = max(
                index
                for index, row in enumerate(timeline)
                if row == ("model", "reviewer")
            )
            audit_phase_index = next(
                index
                for index, row in enumerate(timeline)
                if row == ("phase", "auditing")
            )
            terminal_retrievals = [
                row
                for row in timeline[reviewer_index + 1:audit_phase_index]
                if row[0] == "retrieval"
            ]
            terminal_suggestion_events = [
                row
                for row in timeline[reviewer_index + 1:audit_phase_index]
                if row[0] == "retrieval_suggestions"
            ]
            self.assertEqual(len(terminal_retrievals), 1)
            self.assertEqual(len(terminal_suggestion_events), 1)
            self.assertTrue(
                all(
                    row[1] == []
                    for row in timeline[:reviewer_index]
                    if row[0] == "retrieval_suggestions"
                )
            )
            precise_lean_indexes = [
                index
                for index, row in enumerate(timeline)
                if row[0] == "lean"
                and "import Mathlib.Logic.Basic\n" in str(row[1])
                and not has_broad_import(str(row[1]))
            ]
            self.assertEqual(len(precise_lean_indexes), 2)
            self.assertGreater(precise_lean_indexes[0], reviewer_index)

            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            required_fields = {
                "attempted",
                "changed",
                "effective_import_policy",
                "broad_source_sha256",
                "candidate_source_sha256",
                "selected_source_sha256",
                "retrieval",
                "selected_modules",
                "added_modules",
                "optimization",
                "source_audit",
                "import_validation",
                "lean_probe",
                "selected_source",
                "fallback_reason",
            }
            self.assertEqual(set(artifact), required_fields)
            self.assertNotIn('"provenance"', json.dumps(artifact))
            self.assertEqual(artifact["effective_import_policy"], "proof-first")
            self.assertTrue(artifact["attempted"])
            self.assertTrue(artifact["changed"])
            self.assertEqual(artifact["selected_modules"], ["Mathlib.Logic.Basic"])
            self.assertEqual(artifact["added_modules"], ["Mathlib.Logic.Basic"])
            self.assertEqual(
                artifact["retrieval"]["queries"],
                ["plan_term", "useful_true", "exact"],
            )
            self.assertEqual(
                artifact["retrieval"]["import_suggestions"],
                [{
                    "module": "Mathlib.Logic.Basic",
                    "confidence": "high",
                    "queries": ["useful_true"],
                    "evidence": [
                        "Mathlib/Logic/Basic.lean:1 theorem useful_true"
                    ],
                }],
            )
            self.assertEqual(
                terminal_suggestion_events[0][1],
                artifact["retrieval"]["import_suggestions"],
            )
            self.assertEqual(
                terminal_retrievals[0][1], artifact["retrieval"]["queries"]
            )
            self.assertIn(artifact["retrieval"]["queries"], retrieval_calls)
            self.assertEqual(
                set(artifact["lean_probe"]),
                {"ok", "returncode", "diagnostics", "command"},
            )
            self.assertTrue(artifact["lean_probe"]["ok"])
            self.assertEqual(artifact["selected_source"], "precise")
            self.assertIsNone(artifact["fallback_reason"])

            checkpoint = next((result.state_dir / "checkpoints").glob("*"))
            checkpoint_source = (checkpoint / "source.lean").read_text(encoding="utf-8")
            checkpoint_sha = sha256_text(checkpoint_source)
            selected_sha = sha256_text(selected_source)
            self.assertTrue(has_broad_import(checkpoint_source))
            self.assertEqual(artifact["broad_source_sha256"], checkpoint_sha)
            self.assertEqual(artifact["candidate_source_sha256"], selected_sha)
            self.assertEqual(artifact["selected_source_sha256"], selected_sha)
            final_audit = json.loads(
                (result.state_dir / "final-audit.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(final_audit["source_sha256"], selected_sha)
            self.assertEqual(manifest["current_sha256"], selected_sha)
            summary = manifest["final_import_reduction"]
            self.assertEqual(
                set(summary),
                {
                    "artifact",
                    "attempted",
                    "changed",
                    "effective_import_policy",
                    "broad_source_sha256",
                    "candidate_source_sha256",
                    "selected_source_sha256",
                    "selected_source",
                    "fallback_reason",
                },
            )
            self.assertEqual(summary["artifact"], "final-import-reduction.json")
            self.assertEqual(result.attempts, 1)
            self.assertEqual(len(manifest["attempts"]), 1)
            self.assertEqual(manifest["settings"]["max_attempts_total"], 2)
            self.assertEqual(manifest["settings"]["max_attempts_per_step"], 1)
            self.assertEqual(
                len(list((result.state_dir / "attempts").glob("*"))),
                1,
            )
            agent_requests = [
                json.loads((path / "request.json").read_text(encoding="utf-8"))
                for path in sorted((result.state_dir / "agent-calls").iterdir())
            ]
            self.assertEqual(
                [request["role"] for request in agent_requests],
                ["planner", "prover", "reviewer", "auditor"],
            )
            self.assertEqual(
                [request["model"] for request in agent_requests],
                ["fake-model", "fake-model", "fake-model", "fake-model"],
            )

    def test_terminal_reduction_supports_remove_only_candidate(self) -> None:
        source = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by exact missing\n"
        )
        candidate = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by exact useful_true\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            result, target, _, _ = self._run_terminal_reduction_case(
                Path(directory), source=source, candidates=[candidate]
            )

            self.assertTrue(result.ok)
            selected_source = target.read_text(encoding="utf-8")
            self.assertEqual(selected_source.count("import Mathlib.Logic.Basic\n"), 1)
            self.assertNotIn("import Mathlib\n", selected_source)
            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(artifact["selected_modules"], ["Mathlib.Logic.Basic"])
            self.assertEqual(artifact["added_modules"], [])
            self.assertEqual(
                artifact["optimization"]["reason"],
                "high_confidence_remove_only",
            )
            self.assertTrue(artifact["changed"])

    def test_terminal_reduction_fallback_matrix_runs_full_final_audit(self) -> None:
        def failing_audit(original: str, candidate: str, **kwargs):
            if not has_broad_import(candidate):
                return {"ok": False, "violations": ["forced terminal audit failure"]}
            return real_audit_source(original, candidate, **kwargs)

        def failing_validation(project: Path, source: str):
            if not has_broad_import(source) and "Mathlib.Logic.Basic" in source:
                return {
                    "ok": False,
                    "imports": ["Mathlib.Logic.Basic"],
                    "invalid": [{"module": "Mathlib.Logic.Basic"}],
                }
            return real_validate_mathlib_imports(project, source)

        def lean_failure(source: str) -> LeanCheck:
            if "missing" in source:
                return LeanCheck(False, 1, "missing", ())
            if not has_broad_import(source) and "Mathlib.Logic.Basic" in source:
                return LeanCheck(False, 1, "forced terminal Lean failure", ("lake",))
            return LeanCheck(True, 0, "", ("lake",))

        scenarios = (
            ("no_suggestion", [], "no_high_confidence_imports", None, None),
            ("source_audit", None, "source_audit_failed", "audit", None),
            ("import_validation", None, "import_validation_failed", "validation", None),
            ("lean", None, "lean_probe_failed", None, lean_failure),
            ("exception", None, "reduction_exception", "exception", None),
        )
        for name, suggestions, reason, injected, checker in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                patches = []
                if injected == "audit":
                    patches.append(patch("lean_loop.workflow.audit_source", side_effect=failing_audit))
                elif injected == "validation":
                    patches.append(
                        patch(
                            "lean_loop.workflow.validate_mathlib_imports",
                            side_effect=failing_validation,
                        )
                    )
                elif injected == "exception":
                    patches.append(
                        patch(
                            "lean_loop.workflow.optimize_broad_imports",
                            side_effect=RuntimeError("forced terminal reduction error"),
                        )
                    )
                for active_patch in patches:
                    active_patch.start()
                try:
                    result, target, _, _ = self._run_terminal_reduction_case(
                        Path(directory),
                        suggestions=suggestions,
                        checker_override=checker,
                    )
                finally:
                    for active_patch in reversed(patches):
                        active_patch.stop()

                self.assertTrue(result.ok)
                broad_source = target.read_text(encoding="utf-8")
                self.assertTrue(has_broad_import(broad_source))
                artifact = json.loads(
                    (result.state_dir / "final-import-reduction.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(artifact["attempted"])
                self.assertFalse(artifact["changed"])
                self.assertEqual(artifact["selected_source"], "broad")
                self.assertEqual(artifact["fallback_reason"], reason)
                self.assertEqual(
                    artifact["selected_source_sha256"], sha256_text(broad_source)
                )
                if name == "no_suggestion":
                    self.assertIsNone(artifact["candidate_source_sha256"])
                    self.assertIsNone(artifact["source_audit"])
                    self.assertIsNone(artifact["import_validation"])
                    self.assertIsNone(artifact["lean_probe"]["ok"])
                elif name == "source_audit":
                    self.assertIsNotNone(artifact["candidate_source_sha256"])
                    self.assertFalse(artifact["source_audit"]["ok"])
                    self.assertIsNone(artifact["import_validation"])
                    self.assertIsNone(artifact["lean_probe"]["ok"])
                elif name == "import_validation":
                    self.assertTrue(artifact["source_audit"]["ok"])
                    self.assertFalse(artifact["import_validation"]["ok"])
                    self.assertIsNone(artifact["lean_probe"]["ok"])
                elif name == "lean":
                    self.assertTrue(artifact["source_audit"]["ok"])
                    self.assertTrue(artifact["import_validation"]["ok"])
                    self.assertFalse(artifact["lean_probe"]["ok"])
                final_audit = json.loads(
                    (result.state_dir / "final-audit.json").read_text(encoding="utf-8")
                )
                self.assertTrue(final_audit["ok"])
                self.assertEqual(final_audit["source_sha256"], sha256_text(broad_source))

    def test_terminal_reduction_cancellation_restores_broad_even_when_keep_failed(self) -> None:
        run_ids: list[str] = []

        def cancel_precise(source: str) -> LeanCheck:
            if "missing" in source:
                return LeanCheck(False, 1, "missing", ())
            if not has_broad_import(source) and "Mathlib.Logic.Basic" in source:
                raise ProcessCancelled("cancel terminal reduction")
            return LeanCheck(True, 0, "", ("lake",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ProcessCancelled):
                self._run_terminal_reduction_case(
                    root,
                    checker_override=cancel_precise,
                    keep_failed=True,
                    workflow_created_callback=run_ids.append,
                )

            self.assertEqual(len(run_ids), 1)
            state_dir = root / ".lean-agent" / "workflows" / run_ids[0]
            target = root / "Main.lean"
            broad_source = target.read_text(encoding="utf-8")
            self.assertTrue(has_broad_import(broad_source))
            artifact = json.loads(
                (state_dir / "final-import-reduction.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["fallback_reason"], "cancelled")
            self.assertEqual(artifact["selected_source"], "broad")
            self.assertEqual(artifact["selected_source_sha256"], sha256_text(broad_source))
            self.assertEqual(manifest["status"], "cancelled")
            self.assertEqual(manifest["current_sha256"], sha256_text(broad_source))
            self.assertIsNotNone(manifest["restored_to_checkpoint"])
            checkpoint_source = Path(manifest["restored_to_checkpoint"]).joinpath(
                "source.lean"
            ).read_text(encoding="utf-8")
            self.assertEqual(checkpoint_source, broad_source)
            self.assertEqual(
                artifact["broad_source_sha256"], sha256_text(checkpoint_source)
            )

    def test_global_audit_cancellation_after_reduction_restores_broad_with_keep_failed(self) -> None:
        run_ids: list[str] = []
        precise_checks = 0

        def cancel_global_audit(source: str) -> LeanCheck:
            nonlocal precise_checks
            if "missing" in source:
                return LeanCheck(False, 1, "missing", ())
            if not has_broad_import(source) and "Mathlib.Logic.Basic" in source:
                precise_checks += 1
                if precise_checks == 2:
                    raise ProcessCancelled("cancel global audit")
            return LeanCheck(True, 0, "", ("lake",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ProcessCancelled):
                self._run_terminal_reduction_case(
                    root,
                    checker_override=cancel_global_audit,
                    keep_failed=True,
                    workflow_created_callback=run_ids.append,
                )

            self.assertEqual(precise_checks, 2)
            self.assertEqual(len(run_ids), 1)
            state_dir = root / ".lean-agent" / "workflows" / run_ids[0]
            target = root / "Main.lean"
            artifact = json.loads(
                (state_dir / "final-import-reduction.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((state_dir / "run.json").read_text(encoding="utf-8"))
            checkpoint = Path(manifest["restored_to_checkpoint"])
            broad_source = (checkpoint / "source.lean").read_text(encoding="utf-8")
            broad_sha = sha256_text(broad_source)

            self.assertTrue(artifact["attempted"])
            self.assertTrue(artifact["changed"])
            self.assertEqual(artifact["selected_source"], "precise")
            self.assertNotEqual(artifact["selected_source_sha256"], broad_sha)
            self.assertEqual(artifact["broad_source_sha256"], broad_sha)
            self.assertEqual(target.read_text(encoding="utf-8"), broad_source)
            self.assertEqual(manifest["current_sha256"], broad_sha)
            self.assertEqual(manifest["status"], "cancelled")
            self.assertTrue(manifest["restored"])

    def test_global_audit_ordinary_exception_keeps_selected_source_with_keep_failed(self) -> None:
        run_ids: list[str] = []
        precise_checks = 0

        def fail_global_audit(source: str) -> LeanCheck:
            nonlocal precise_checks
            if "missing" in source:
                return LeanCheck(False, 1, "missing", ())
            if not has_broad_import(source) and "Mathlib.Logic.Basic" in source:
                precise_checks += 1
                if precise_checks == 2:
                    raise RuntimeError("global audit infrastructure failed")
            return LeanCheck(True, 0, "", ("lake",))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                RuntimeError, "global audit infrastructure failed"
            ):
                self._run_terminal_reduction_case(
                    root,
                    checker_override=fail_global_audit,
                    keep_failed=True,
                    workflow_created_callback=run_ids.append,
                )

            self.assertEqual(precise_checks, 2)
            state_dir = root / ".lean-agent" / "workflows" / run_ids[0]
            target = root / "Main.lean"
            artifact = json.loads(
                (state_dir / "final-import-reduction.json").read_text(encoding="utf-8")
            )
            manifest = json.loads((state_dir / "run.json").read_text(encoding="utf-8"))
            target_sha = sha256_text(target.read_text(encoding="utf-8"))

            self.assertTrue(artifact["changed"])
            self.assertEqual(artifact["selected_source"], "precise")
            self.assertEqual(target_sha, artifact["selected_source_sha256"])
            self.assertNotEqual(target_sha, artifact["broad_source_sha256"])
            self.assertEqual(manifest["current_sha256"], target_sha)
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(manifest["restored"])

    def test_terminal_reduction_restore_write_failure_is_fail_closed(self) -> None:
        restore_failed = False

        def fail_first_transaction_restore(path: Path, text: str) -> None:
            nonlocal restore_failed
            if (
                path.name == "Main.lean"
                and has_broad_import(text)
                and path.is_file()
                and "import Mathlib.Logic.Basic\n"
                in path.read_text(encoding="utf-8")
                and not has_broad_import(path.read_text(encoding="utf-8"))
                and not restore_failed
            ):
                restore_failed = True
                raise OSError("forced broad restore failure")
            real_atomic_write_text(path, text)

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "lean_loop.workflow.atomic_write_text",
                side_effect=fail_first_transaction_restore,
            ):
                with self.assertRaisesRegex(OSError, "forced broad restore failure"):
                    self._run_terminal_reduction_case(Path(directory))
            self.assertTrue(restore_failed)

    def test_terminal_reduction_global_rejection_keeps_audited_sha_then_restores(self) -> None:
        original = "-- start\n"
        steps = [
            {
                "id": "helper",
                "goal": "add helper",
                "success_criteria": "helper passes",
                "search_terms": ["helper"],
            },
            {
                "id": "final",
                "goal": "finish proof",
                "success_criteria": "final passes",
                "search_terms": ["useful_true"],
            },
        ]
        candidates = [
            "def helper : True := by trivial\n",
            (
                "def helper : True := by trivial\n"
                "example : True := by exact useful_true\n"
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            result, target, _, _ = self._run_terminal_reduction_case(
                Path(directory),
                source=original,
                steps=steps,
                candidates=candidates,
                global_verdict="retry",
            )

            self.assertFalse(result.ok)
            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            final_audit = json.loads(
                (result.state_dir / "final-audit.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                final_audit["source_sha256"], artifact["selected_source_sha256"]
            )
            self.assertNotEqual(final_audit["source_sha256"], manifest["current_sha256"])
            self.assertIn("rejected_checkpoint", manifest["steps"][-1])
            rejected = Path(manifest["steps"][-1]["rejected_checkpoint"])
            self.assertTrue(
                has_broad_import((rejected / "source.lean").read_text(encoding="utf-8"))
            )
            restored_checkpoint = Path(manifest["restored_to_checkpoint"])
            restored_source = (restored_checkpoint / "source.lean").read_text(
                encoding="utf-8"
            )
            self.assertEqual(target.read_text(encoding="utf-8"), restored_source)
            self.assertEqual(manifest["current_sha256"], sha256_text(restored_source))
            self.assertTrue(has_broad_import(restored_source))

    def test_terminal_reduction_multistep_checkpoints_stay_broad_and_do_not_add_budget(self) -> None:
        steps = [
            {
                "id": "helper",
                "goal": "add helper",
                "success_criteria": "helper passes",
                "search_terms": ["helper"],
            },
            {
                "id": "final",
                "goal": "add final",
                "success_criteria": "final passes",
                "search_terms": ["useful_true"],
            },
        ]
        candidates = [
            "def helper : True := by trivial\n",
            (
                "def helper : True := by trivial\n"
                "example : True := by exact useful_true\n"
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            result, _, timeline, _ = self._run_terminal_reduction_case(
                Path(directory),
                source="-- start\n",
                steps=steps,
                candidates=candidates,
            )

            self.assertTrue(result.ok)
            checkpoints = sorted((result.state_dir / "checkpoints").glob("*"))
            self.assertEqual(len(checkpoints), 2)
            self.assertTrue(
                all(
                    has_broad_import((path / "source.lean").read_text(encoding="utf-8"))
                    for path in checkpoints
                )
            )
            reviewer_indexes = [
                index
                for index, row in enumerate(timeline)
                if row == ("model", "reviewer")
            ]
            precise_indexes = [
                index
                for index, row in enumerate(timeline)
                if row[0] == "lean"
                and "Mathlib.Logic.Basic" in str(row[1])
                and not has_broad_import(str(row[1]))
            ]
            self.assertEqual(len(reviewer_indexes), 2)
            self.assertEqual(len(precise_indexes), 2)
            self.assertGreater(precise_indexes[0], reviewer_indexes[-1])
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result.attempts, 2)
            self.assertEqual(len(manifest["attempts"]), 2)
            self.assertEqual(len(list((result.state_dir / "attempts").glob("*"))), 2)
            self.assertEqual(manifest["settings"]["max_attempts_total"], 2)
            self.assertEqual(manifest["settings"]["max_attempts_per_step"], 1)
            self.assertEqual(
                [step["attempts"] for step in manifest["steps"]], [[1], [2]]
            )
            agent_roles = [
                json.loads((path / "request.json").read_text(encoding="utf-8"))["role"]
                for path in sorted((result.state_dir / "agent-calls").iterdir())
            ]
            self.assertEqual(
                agent_roles,
                ["planner", "prover", "reviewer", "prover", "reviewer", "auditor"],
            )

    def test_terminal_reduction_artifact_is_not_applicable_for_explicit_policies(self) -> None:
        for policy in ("precise", "broad"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                result, target, timeline, _ = self._run_terminal_reduction_case(
                    Path(directory), import_policy=policy
                )

                self.assertTrue(result.ok)
                artifact = json.loads(
                    (result.state_dir / "final-import-reduction.json").read_text(
                        encoding="utf-8"
                    )
                )
                selected_source = target.read_text(encoding="utf-8")
                self.assertFalse(artifact["attempted"])
                self.assertFalse(artifact["changed"])
                self.assertEqual(artifact["fallback_reason"], "policy_not_applicable")
                self.assertEqual(artifact["effective_import_policy"], policy)
                self.assertEqual(
                    artifact["selected_source"],
                    "broad" if policy == "broad" else "precise",
                )
                self.assertEqual(
                    artifact["selected_source_sha256"], sha256_text(selected_source)
                )
                reviewer_index = max(
                    index
                    for index, row in enumerate(timeline)
                    if row == ("model", "reviewer")
                )
                audit_phase_index = next(
                    index
                    for index, row in enumerate(timeline)
                    if row == ("phase", "auditing")
                )
                self.assertFalse(
                    any(
                        row[0] == "retrieval"
                        for row in timeline[reviewer_index + 1:audit_phase_index]
                    )
                )

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
                return (
                    "example : True := by exact missing\n"
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
