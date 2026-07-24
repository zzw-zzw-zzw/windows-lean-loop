import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.api import ApiError, MalformedModelOutputError
from lean_loop.audit import audit_source as real_audit_source
from lean_loop.config import ApiConfig
from lean_loop.jsonutil import sha256_text
from lean_loop.lean import LeanCheck, check_lean
from lean_loop.mathlib_search import (
    has_broad_import,
    optimize_broad_imports,
    validate_mathlib_imports as real_validate_mathlib_imports,
)
from lean_loop.mathlib_index import build_mathlib_index
from lean_loop.process_control import ProcessCancelled
from lean_loop.prompts import PROVER_SYSTEM_PROMPT
from lean_loop.subscription_backend import SubscriptionBackendError
from lean_loop.workflow import (
    _exact_theorem_hits,
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


class _CodexWorkflowBackend:
    backend_id = "codex-subscription"

    def __init__(
        self,
        prover_results: list[str | None],
        *,
        reviewer_verdict: str = "accept",
    ) -> None:
        self.prover_results = prover_results
        self.reviewer_verdict = reviewer_verdict
        self.prover_calls = 0
        self.prover_prompts: list[str] = []
        self.last_metadata: dict[str, object] = {"backend_id": self.backend_id}

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
            "filesystem_write_scope": "REPO_EXTERNAL_EPHEMERAL_WORKSPACE",
            "read_isolation_status": "NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX",
            "network_policy": "DISABLED",
            "sandbox_profile": {"network_policy": "DISABLED"},
        }

    def invoke(self, request, config, temp_dir):
        del temp_dir
        identity = self.inspect(
            model=config.model,
            reasoning_effort=config.reasoning_effort,
        )
        self.last_metadata = dict(identity)
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
        if request.role == "prover":
            self.prover_prompts.append(request.user_prompt)
            if self.prover_calls >= len(self.prover_results):
                raise AssertionError("Unexpected extra Prover call")
            result = self.prover_results[self.prover_calls]
            self.prover_calls += 1
            if result is None:
                metadata = {
                    **identity,
                    "exit_code": 0,
                    "terminal_state": "output_protocol_incompatible",
                }
                self.last_metadata = metadata
                raise SubscriptionBackendError(
                    "output_protocol_incompatible",
                    "Codex did not return one completed final result",
                    raw_output='{"type":"turn.completed","usage":{}}\n',
                    metadata=metadata,
                )
            return result
        return {
            "verdict": self.reviewer_verdict,
            "summary": f"reviewer verdict: {self.reviewer_verdict}",
            "failure_analysis": (
                [] if self.reviewer_verdict == "accept" else ["semantic revision needed"]
            ),
            "next_actions": (
                [] if self.reviewer_verdict == "accept" else ["revise the candidate"]
            ),
            "search_terms": [],
        }


class StructuredWorkflowTests(unittest.TestCase):
    def test_legacy_workflow_replans_into_default_planner_mode(self) -> None:
        previous = {"settings": {}}
        current = {"planning_mode": "planner"}
        self.assertEqual(
            _resume_replan_reason(None, previous, current),  # type: ignore[arg-type]
            "planning_mode_changed",
        )

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

    def test_import_policy_resolution_and_prompt_contract(self) -> None:
        existing_source = "theorem existing : True := by trivial\n"
        new_source = "-- new theorem\n"
        self.assertEqual(_effective_import_policy("auto", existing_source), "precise")
        self.assertEqual(_effective_import_policy("auto", new_source), "precise")
        for explicit in ("proof-first", "precise", "broad"):
            with self.subTest(explicit=explicit):
                self.assertEqual(
                    _effective_import_policy(explicit, new_source), explicit
                )
        prompt = " ".join(PROVER_SYSTEM_PROMPT.split())
        self.assertIn(
            "During `proof-first` and `broad` proof phases, import breadth is "
            "orchestrator-owned.",
            prompt,
        )
        self.assertIn(
            "Do not narrow or remove a standalone `import Mathlib`.", prompt
        )
        self.assertIn(
            "Only the resolved `precise` policy may use locally evidenced fine "
            "imports.",
            prompt,
        )

    def test_historical_policy_resolution_and_raw_change(self) -> None:
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
        for effective in ("proof-first", "precise", "broad"):
            self.assertEqual(
                _historical_effective_import_policy(
                    {
                        "settings": {
                            "import_policy": "auto",
                            "effective_import_policy": effective,
                        }
                    },
                    existing_source,
                ),
                (effective, False),
            )
        self.assertEqual(
            _resume_replan_reason(
                None,  # type: ignore[arg-type]
                {
                    "settings": {
                        "import_policy": "auto",
                        "effective_import_policy": "precise",
                    },
                    "attempts": [],
                },
                {"import_policy": "broad", "effective_import_policy": "broad"},
            ),
            "import_policy_changed",
        )

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
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["error"],
                "Lean did not pass within the configured candidate budgets.",
            )

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
            planner_prompts: list[str] = []
            reviewer_prompts: list[str] = []
            prover_prompts: list[str] = []

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
                    planner_prompts.append(user)
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
                reviewer_prompts.append(user)
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
                    prover_prompts.append(prompt)
                    or "theorem goal : True := by exact useful_true\n"
                ),
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertTrue(any("import Mathlib\n" in source for source in checked_sources))
            self.assertIn("import Mathlib.Logic.Basic", target.read_text(encoding="utf-8"))
            self.assertIn("import Mathlib\n", planner_prompts[0])
            self.assertIn("import Mathlib\n", prover_prompts[0])
            self.assertIn("import Mathlib\n", reviewer_prompts[0])
            attempt_source = (
                result.state_dir / "attempts" / "001" / "candidate.lean"
            ).read_text(encoding="utf-8")
            checkpoint = next((result.state_dir / "checkpoints").glob("*"))
            checkpoint_source = (checkpoint / "source.lean").read_text(encoding="utf-8")
            self.assertEqual(attempt_source, checkpoint_source)
            self.assertTrue(has_broad_import(checkpoint_source))
            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                artifact["broad_source_sha256"], sha256_text(checkpoint_source)
            )
            self.assertEqual(result.attempts, 1)

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

    def test_codex_protocol_failure_consumes_candidate_attempt_and_retries(self) -> None:
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
                reasoning_effort="high",
            )
            backend = _CodexWorkflowBackend(
                [None, "example : True := by trivial\n"]
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=backend,
                agent_backend_id="codex-subscription",
            )

            self.assertTrue(result.ok)
            self.assertEqual(backend.prover_calls, 2)
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [row["attempt"] for row in manifest["attempts"]], [1, 2]
            )
            self.assertEqual(manifest["attempts"][0]["failure_stage"], "prover_output_protocol")
            self.assertEqual(manifest["attempts"][0]["review_verdict"], "not_run")
            self.assertEqual(manifest.get("api_failures", []), [])
            first_attempt = result.state_dir / "attempts" / "001"
            self.assertTrue((first_attempt / "protocol-failure.json").is_file())
            self.assertFalse((first_attempt / "candidate.lean").exists())
            events = (result.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "prover_output_rejected"', events)

    def test_codex_protocol_retry_guidance_survives_resume(self) -> None:
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
                reasoning_effort="high",
            )
            backend = _CodexWorkflowBackend(
                [None, "example : True := by trivial\n"]
            )
            first = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=1,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=backend,
                agent_backend_id="codex-subscription",
            )
            first_manifest = json.loads(
                (first.state_dir / "run.json").read_text(encoding="utf-8")
            )
            guidance = first_manifest["final_review"]
            self.assertFalse(first.ok)
            self.assertFalse((first.state_dir / "reviews" / "001.json").exists())

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                lean_checker=checker,
                agent_backend=backend,
                agent_backend_id="codex-subscription",
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(backend.prover_calls, 2)
            self.assertIn(
                json.dumps(guidance, ensure_ascii=False, indent=2),
                backend.prover_prompts[1],
            )
            self.assertIn(
                "example : True := by exact missing", backend.prover_prompts[1]
            )
            self.assertFalse((resumed.state_dir / "reviews" / "001.json").exists())
            manifest = json.loads(
                (resumed.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertIsNone(manifest["attempts"][1]["base_attempt"])
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn('"event": "working_candidate_restored"', events)

    def test_codex_protocol_resume_uses_latest_real_candidate_and_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact original_missing\n"
            )

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                if ok:
                    diagnostics = ""
                elif "candidate_marker" in source:
                    diagnostics = "CANDIDATE_DIAGNOSTIC"
                else:
                    diagnostics = "ORIGINAL_DIAGNOSTIC"
                return LeanCheck(ok, 0 if ok else 1, diagnostics, ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="high",
            )
            backend = _CodexWorkflowBackend(
                [
                    "example : True := by exact candidate_marker\n",
                    None,
                    "example : True := by trivial\n",
                ]
            )
            first = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=backend,
                agent_backend_id="codex-subscription",
            )
            first_manifest = json.loads(
                (first.state_dir / "run.json").read_text(encoding="utf-8")
            )
            guidance = first_manifest["final_review"]
            self.assertFalse(first.ok)
            self.assertIsNotNone(first_manifest["attempts"][0]["candidate_sha256"])
            self.assertIsNone(first_manifest["attempts"][1]["candidate_sha256"])
            self.assertFalse((first.state_dir / "reviews" / "002.json").exists())

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=3,
                max_attempts_per_step=3,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                lean_checker=checker,
                agent_backend=backend,
                agent_backend_id="codex-subscription",
            )

            self.assertTrue(resumed.ok)
            self.assertIn("candidate_marker", backend.prover_prompts[2])
            self.assertIn("CANDIDATE_DIAGNOSTIC", backend.prover_prompts[2])
            self.assertNotIn("ORIGINAL_DIAGNOSTIC", backend.prover_prompts[2])
            self.assertIn(
                json.dumps(guidance, ensure_ascii=False, indent=2),
                backend.prover_prompts[2],
            )
            manifest = json.loads(
                (resumed.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["attempts"][2]["base_attempt"], 1)
            events = [
                json.loads(line)
                for line in (resumed.state_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(
                any(
                    event.get("event") == "working_candidate_restored"
                    and event.get("attempt") == 1
                    for event in events
                )
            )

    def test_codex_protocol_resume_fails_closed_for_invalid_candidate_artifacts(self) -> None:
        invalid_check_values = {
            "empty-check": {},
            "missing-ok": {
                "returncode": 1,
                "output": "x",
                "command": [],
            },
            "missing-returncode": {
                "ok": False,
                "output": "x",
                "command": [],
            },
            "missing-output": {
                "ok": False,
                "returncode": 1,
                "command": [],
            },
            "missing-command": {
                "ok": False,
                "returncode": 1,
                "output": "x",
            },
            "string-ok": {
                "ok": "false",
                "returncode": 1,
                "output": "x",
                "command": [],
            },
            "string-returncode": {
                "ok": False,
                "returncode": "1",
                "output": "x",
                "command": [],
            },
            "bool-returncode": {
                "ok": False,
                "returncode": True,
                "output": "x",
                "command": [],
            },
            "list-output": {
                "ok": False,
                "returncode": 1,
                "output": [],
                "command": [],
            },
            "string-command": {
                "ok": False,
                "returncode": 1,
                "output": "x",
                "command": "lake",
            },
            "non-string-command-element": {
                "ok": False,
                "returncode": 1,
                "output": "x",
                "command": ["lake", 1],
            },
        }
        for mutation, expected_error in (
            ("missing", "Resume candidate is missing"),
            ("hash-mismatch", "Resume candidate hash mismatch"),
            ("missing-check", "Resume candidate check is missing"),
            ("malformed-check", "Resume candidate check is invalid"),
            *(
                (mutation, "Resume candidate check is invalid")
                for mutation in invalid_check_values
            ),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                project, target = self._project(
                    Path(directory), "example : True := by exact original_missing\n"
                )

                def checker(
                    project: Path, target: Path, timeout: int, lake: str
                ) -> LeanCheck:
                    return LeanCheck(False, 1, "missing", ())

                config = ApiConfig(
                    api_base="",
                    api_key="",
                    model="gpt-5.6-sol",
                    mode="subscription",
                    timeout_seconds=10,
                    curl_executable="",
                    reasoning_effort="high",
                )
                first = run_structured_workflow(
                    project=project,
                    target=target,
                    task="fix proof",
                    plan_config=config,
                    prove_config=config,
                    review_config=config,
                    max_attempts=2,
                    max_attempts_per_step=2,
                    lean_timeout_seconds=10,
                    lake_executable="lake",
                    lean_checker=checker,
                    agent_backend=_CodexWorkflowBackend(
                        ["example : True := by exact candidate_marker\n", None]
                    ),
                    agent_backend_id="codex-subscription",
                )
                candidate = first.state_dir / "attempts" / "001" / "candidate.lean"
                candidate_check = first.state_dir / "attempts" / "001" / "check.json"
                if mutation == "missing":
                    candidate.unlink()
                elif mutation == "hash-mismatch":
                    candidate.write_text(
                        "example : True := by exact tampered\n", encoding="utf-8"
                    )
                elif mutation == "missing-check":
                    candidate_check.unlink()
                elif mutation == "malformed-check":
                    candidate_check.write_text("{malformed\n", encoding="utf-8")
                else:
                    candidate_check.write_text(
                        json.dumps(invalid_check_values[mutation]) + "\n",
                        encoding="utf-8",
                    )

                with self.assertRaisesRegex(ValueError, expected_error):
                    run_structured_workflow(
                        project=project,
                        target=target,
                        task="fix proof",
                        plan_config=config,
                        prove_config=config,
                        review_config=config,
                        max_attempts=3,
                        max_attempts_per_step=3,
                        lean_timeout_seconds=10,
                        lake_executable="lake",
                        resume_run_id=first.run_id,
                        lean_checker=checker,
                        agent_backend=_CodexWorkflowBackend(
                            ["example : True := by trivial\n"]
                        ),
                        agent_backend_id="codex-subscription",
                    )

    def test_codex_pure_protocol_failures_report_no_valid_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(False, 1, "missing", ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="high",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=_CodexWorkflowBackend([None, None]),
                agent_backend_id="codex-subscription",
            )

            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertFalse(result.ok)
            self.assertEqual(
                manifest["error"],
                "Prover output did not produce a valid Lean candidate within the "
                "configured candidate budgets.",
            )
            self.assertTrue(
                all(row["candidate_sha256"] is None for row in manifest["attempts"])
            )

    def test_codex_mixed_candidate_and_protocol_failures_are_reported_accurately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(False, 1, "missing", ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="high",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=_CodexWorkflowBackend(
                    ["example : True := by exact missing\n", None]
                ),
                agent_backend_id="codex-subscription",
            )

            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertFalse(result.ok)
            self.assertEqual(
                manifest["error"],
                "Candidate budgets were exhausted after both Lean candidate validation "
                "failures and Prover output protocol failures.",
            )
            self.assertIsNotNone(manifest["attempts"][0]["candidate_sha256"])
            self.assertEqual(
                manifest["attempts"][1]["failure_stage"],
                "prover_output_protocol",
            )

    def test_codex_mixed_protocol_failure_keep_failed_uses_latest_candidate(self) -> None:
        original = "example : True := by exact original_missing\n"
        candidate = "example : True := by exact candidate_marker\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                diagnostics = (
                    "CANDIDATE_DIAGNOSTIC"
                    if "candidate_marker" in source
                    else "ORIGINAL_DIAGNOSTIC"
                )
                return LeanCheck(False, 1, diagnostics, ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="high",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                keep_failed=True,
                lean_checker=checker,
                agent_backend=_CodexWorkflowBackend([candidate, None]),
                agent_backend_id="codex-subscription",
            )

            self.assertFalse(result.ok)
            self.assertFalse(result.restored)
            self.assertEqual(target.read_text(encoding="utf-8"), candidate)

    def test_codex_pure_protocol_keep_failed_preserves_safe_source(self) -> None:
        original = "example : True := by exact original_missing\n"
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), original)

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(False, 1, "ORIGINAL_DIAGNOSTIC", ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="high",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                keep_failed=True,
                lean_checker=checker,
                agent_backend=_CodexWorkflowBackend([None, None]),
                agent_backend_id="codex-subscription",
            )

            self.assertFalse(result.ok)
            self.assertFalse(result.restored)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_codex_reviewer_and_protocol_failures_are_reported_accurately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact original_missing\n"
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
                reasoning_effort="high",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=_CodexWorkflowBackend(
                    ["example : True := by trivial\n", None],
                    reviewer_verdict="retry",
                ),
                agent_backend_id="codex-subscription",
            )

            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertFalse(result.ok)
            self.assertEqual(manifest["attempts"][0]["check_ok"], True)
            self.assertEqual(manifest["attempts"][0]["review_verdict"], "retry")
            self.assertEqual(
                manifest["error"],
                "Candidate budgets were exhausted after both Reviewer rejection of "
                "Lean-valid candidates and Prover output protocol failures.",
            )
            self.assertNotIn("Lean candidate validation failures", manifest["error"])

    def test_codex_lean_reviewer_and_protocol_failures_are_all_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact original_missing\n"
            )

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            config = ApiConfig(
                api_base="",
                api_key="",
                model="gpt-5.6-sol",
                mode="subscription",
                timeout_seconds=10,
                curl_executable="",
                reasoning_effort="high",
            )
            result = run_structured_workflow(
                project=project,
                target=target,
                task="fix proof",
                plan_config=config,
                prove_config=config,
                review_config=config,
                max_attempts=3,
                max_attempts_per_step=3,
                lean_timeout_seconds=10,
                lake_executable="lake",
                lean_checker=checker,
                agent_backend=_CodexWorkflowBackend(
                    [
                        "example : True := by exact missing\n",
                        "example : True := by trivial\n",
                        None,
                    ],
                    reviewer_verdict="retry",
                ),
                agent_backend_id="codex-subscription",
            )

            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            self.assertFalse(result.ok)
            self.assertEqual(
                manifest["error"],
                "Candidate budgets were exhausted after Lean candidate validation "
                "failures, Reviewer rejection of Lean-valid candidates, and Prover "
                "output protocol failures.",
            )

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
                planning_mode="direct",
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

    def test_exact_hit_rejects_natural_language_geometry_labels(self) -> None:
        retrieval = {
            "hits": [
                {
                    "query": "ABC",
                    "name": "Polynomial.abc",
                    "kind": "theorem",
                    "match": "exact",
                },
                {
                    "query": "M",
                    "name": "Example.M",
                    "kind": "lemma",
                    "match": "exact",
                },
                {
                    "query": "useful_true",
                    "name": "useful_true",
                    "kind": "theorem",
                    "match": "exact",
                },
                {
                    "query": "Real.pi_gt_three",
                    "name": "Real.pi_gt_three",
                    "kind": "theorem",
                    "match": "exact",
                },
            ]
        }

        hits = _exact_theorem_hits(
            retrieval,
            ["ABC", "M", "useful_true", "Real.pi_gt_three"],
        )

        self.assertEqual(
            [row["name"] for row in hits],
            ["useful_true", "Real.pi_gt_three"],
        )

    def test_planner_is_the_default_even_for_a_simple_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact missing\n"
            )
            planner_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": "planned proof",
                        "steps": [{
                            "id": "planned-step",
                            "goal": "prove True",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                            "required_declarations": [],
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

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove True",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                json_model_call=json_model,
                file_model_call=lambda config, prompt, temp: (
                    "example : True := by trivial\n"
                ),
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertEqual(planner_calls, 1)
            plan = json.loads((result.state_dir / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["steps"][0]["id"], "planned-step")

    def test_direct_then_planner_falls_back_after_three_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact original_missing\n"
            )
            planner_calls = 0
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else source.strip(), ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": "fallback plan",
                        "steps": [{
                            "id": "planned-step",
                            "goal": "finish after direct attempts",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                            "required_declarations": [],
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
                if prover_calls <= 3:
                    return f"example : True := by exact missing_{prover_calls}\n"
                return "example : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="prove True directly, then plan if needed",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=6,
                max_attempts_per_step=5,
                lean_timeout_seconds=10,
                lake_executable="lake",
                planning_mode="direct-then-planner",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(result.ok)
            self.assertEqual(planner_calls, 1)
            self.assertEqual(prover_calls, 4)
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["settings"]["planning_mode"], "direct-then-planner")
            self.assertEqual(manifest["direct_fallback"]["failed_attempts"], 3)
            self.assertEqual(manifest["steps"][0]["id"], "planned-step")
            self.assertEqual(manifest["steps"][0]["attempts"], [4])
            self.assertEqual(
                manifest["superseded_steps"][0]["steps"][0]["attempts"],
                [1, 2, 3],
            )
            attempts = manifest["attempts"]
            self.assertEqual([row["step_id"] for row in attempts], [
                "direct-proof", "direct-proof", "direct-proof", "planned-step"
            ])
            self.assertIsNone(attempts[3].get("base_attempt"))
            self.assertTrue((result.state_dir / "direct-plan.json").is_file())
            events = (result.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "direct_plan_fallback_completed"', events)

    def test_direct_mode_never_falls_back_to_planner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact original_missing\n"
            )
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(False, 1, "still missing", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    raise AssertionError("direct mode must not call Planner")
                return {
                    "verdict": "retry",
                    "summary": "retry",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                return f"example : True := by exact missing_{prover_calls}\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="keep trying directly",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=4,
                max_attempts_per_step=4,
                lean_timeout_seconds=10,
                lake_executable="lake",
                planning_mode="direct",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertFalse(result.ok)
            self.assertEqual(prover_calls, 4)
            manifest = json.loads((result.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertNotIn("direct_fallback", manifest)

    def test_hybrid_resume_falls_back_without_a_fourth_direct_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(
                Path(directory), "example : True := by exact original_missing\n"
            )
            planner_calls = 0
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source
                return LeanCheck(ok, 0 if ok else 1, "" if ok else source.strip(), ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    return {
                        "summary": "resume fallback plan",
                        "steps": [{
                            "id": "planned-step",
                            "goal": "finish",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                            "required_declarations": [],
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
                if prover_calls <= 3:
                    return f"example : True := by exact resume_missing_{prover_calls}\n"
                return "example : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="resume hybrid",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=3,
                max_attempts_per_step=5,
                lean_timeout_seconds=10,
                lake_executable="lake",
                planning_mode="direct-then-planner",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertFalse(first.ok)
            self.assertEqual(planner_calls, 0)

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="resume hybrid",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=5,
                max_attempts_per_step=5,
                lean_timeout_seconds=10,
                lake_executable="lake",
                planning_mode="direct-then-planner",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(planner_calls, 1)
            self.assertEqual(prover_calls, 4)
            manifest = json.loads((resumed.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual([row["attempt"] for row in manifest["attempts"]], [1, 2, 3, 4])

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

    def test_replanned_step_uses_a_generation_scoped_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- start\n")
            planner_calls = 0
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                return LeanCheck(True, 0, "", ())

            def json_model(config, system, user, temp):
                nonlocal planner_calls
                if "Planner" in system:
                    planner_calls += 1
                    steps = [
                        {
                            "id": "step-1",
                            "goal": "first generation",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        },
                        {
                            "id": "step-2",
                            "goal": "force a resume",
                            "success_criteria": "Lean passes",
                            "search_terms": [],
                        },
                    ] if planner_calls == 1 else [{
                        "id": "step-1",
                        "goal": "replacement generation",
                        "success_criteria": "Lean passes",
                        "search_terms": [],
                    }]
                    return {
                        "summary": f"plan {planner_calls}",
                        "steps": steps,
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
                return f"example generation{prover_calls} : True := by trivial\n"

            first = run_structured_workflow(
                project=project,
                target=target,
                task="prove across plans",
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

            changed = ApiConfig(
                api_base="http://example.invalid/v1",
                api_key="not-used",
                model="replacement-model",
                mode="responses",
                timeout_seconds=10,
                curl_executable="curl.exe",
                reasoning_effort="low",
            )
            with patch(
                "lean_loop.workflow._persist_step_checkpoint",
                side_effect=OSError("simulated replacement checkpoint crash"),
            ):
                with self.assertRaisesRegex(
                    OSError, "simulated replacement checkpoint crash"
                ):
                    run_structured_workflow(
                        project=project,
                        target=target,
                        task="prove across plans",
                        plan_config=changed,
                        prove_config=changed,
                        review_config=changed,
                        max_attempts=2,
                        max_attempts_per_step=1,
                        lean_timeout_seconds=10,
                        lake_executable="lake",
                        resume_run_id=first.run_id,
                        json_model_call=json_model,
                        file_model_call=file_model,
                        lean_checker=checker,
                    )

            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="prove across plans",
                plan_config=changed,
                prove_config=changed,
                review_config=changed,
                max_attempts=2,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=first.run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(prover_calls, 2)
            checkpoint_names = sorted(
                path.name
                for path in (resumed.state_dir / "checkpoints").iterdir()
                if path.is_dir()
            )
            self.assertEqual(len(checkpoint_names), 2)
            self.assertTrue(checkpoint_names[0].startswith("g000-s001-step-1-a001"))
            self.assertTrue(checkpoint_names[1].startswith("g002-s001-step-1-a002"))

    def test_resume_recovers_accepted_attempt_after_checkpoint_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- start\n")
            prover_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                ok = "by trivial" in target.read_text(encoding="utf-8")
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing proof", ())

            def json_model(config, system, user, temp):
                if "Planner" in system:
                    return {
                        "summary": "one step",
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

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                return "example recovered : True := by trivial\n"

            with patch(
                "lean_loop.workflow._persist_step_checkpoint",
                side_effect=OSError("simulated checkpoint crash"),
            ):
                with self.assertRaisesRegex(OSError, "simulated checkpoint crash"):
                    run_structured_workflow(
                        project=project,
                        target=target,
                        task="recover accepted candidate",
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

            workflow_dirs = list((project / ".lean-agent" / "workflows").iterdir())
            self.assertEqual(len(workflow_dirs), 1)
            run_id = workflow_dirs[0].name
            resumed = run_structured_workflow(
                project=project,
                target=target,
                task="recover accepted candidate",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=1,
                max_attempts_per_step=1,
                lean_timeout_seconds=10,
                lake_executable="lake",
                resume_run_id=run_id,
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )

            self.assertTrue(resumed.ok)
            self.assertEqual(prover_calls, 1)
            manifest = json.loads((resumed.state_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["attempts"]), 1)
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"event": "accepted_attempt_recovered"', events)
            checkpoint_names = [
                path.name
                for path in (resumed.state_dir / "checkpoints").iterdir()
                if path.is_dir()
            ]
            self.assertEqual(checkpoint_names, ["g001-s001-step-1-a001"])

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

    def test_explicit_policy_end_states_and_artifact_scope(self) -> None:
        for policy in ("auto", "precise", "broad"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                result, target, timeline, _ = self._run_terminal_reduction_case(
                    Path(directory), import_policy=policy
                )
                self.assertTrue(result.ok)
                source = target.read_text(encoding="utf-8")
                self.assertEqual(has_broad_import(source), policy == "broad")
                self.assertFalse(
                    (result.state_dir / "final-import-reduction.json").exists()
                )
                manifest = json.loads(
                    (result.state_dir / "run.json").read_text(encoding="utf-8")
                )
                expected_effective = "precise" if policy == "auto" else policy
                self.assertEqual(
                    manifest["settings"]["effective_import_policy"],
                    expected_effective,
                )
                self.assertNotIn("final_import_reduction", manifest)
                reviewer_index = max(
                    index
                    for index, row in enumerate(timeline)
                    if row == ("model", "reviewer")
                )
                audit_index = next(
                    index
                    for index, row in enumerate(timeline)
                    if row == ("phase", "auditing")
                )
                self.assertFalse(
                    any(
                        row[0] == "retrieval"
                        for row in timeline[reviewer_index + 1 : audit_index]
                    )
                )

    def test_terminal_reduction_is_once_transactional_and_budget_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, target, timeline, retrieval_calls = (
                self._run_terminal_reduction_case(Path(directory))
            )
            self.assertTrue(result.ok)
            selected_source = target.read_text(encoding="utf-8")
            self.assertIn("import Mathlib.Logic.Basic\n", selected_source)
            self.assertFalse(has_broad_import(selected_source))

            reviewer_index = max(
                index
                for index, row in enumerate(timeline)
                if row == ("model", "reviewer")
            )
            audit_index = next(
                index
                for index, row in enumerate(timeline)
                if row == ("phase", "auditing")
            )
            terminal_retrievals = [
                row
                for row in timeline[reviewer_index + 1 : audit_index]
                if row[0] == "retrieval"
            ]
            terminal_precise_checks = [
                row
                for row in timeline[reviewer_index + 1 : audit_index]
                if row[0] == "lean"
                and "import Mathlib.Logic.Basic\n" in str(row[1])
                and not has_broad_import(str(row[1]))
            ]
            self.assertEqual(len(terminal_retrievals), 1)
            self.assertEqual(len(terminal_precise_checks), 1)
            self.assertIn(terminal_retrievals[0][1], retrieval_calls)

            artifact = json.loads(
                (result.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            checkpoint = next((result.state_dir / "checkpoints").glob("*"))
            checkpoint_source = (checkpoint / "source.lean").read_text(encoding="utf-8")
            self.assertTrue(has_broad_import(checkpoint_source))
            self.assertEqual(
                artifact["broad_source_sha256"], sha256_text(checkpoint_source)
            )
            self.assertEqual(
                artifact["selected_source_sha256"], sha256_text(selected_source)
            )
            self.assertTrue(artifact["attempted"])
            self.assertTrue(artifact["changed"])
            self.assertEqual(artifact["selected_source"], "precise")
            self.assertIsNone(artifact["fallback_reason"])
            self.assertEqual(result.attempts, 1)
            self.assertEqual(len(manifest["attempts"]), 1)
            self.assertEqual(
                len(list((result.state_dir / "attempts").glob("*"))), 1
            )
            roles = [
                json.loads((path / "request.json").read_text(encoding="utf-8"))[
                    "role"
                ]
                for path in sorted((result.state_dir / "agent-calls").iterdir())
            ]
            self.assertEqual(roles, ["planner", "prover", "reviewer", "auditor"])

    def test_terminal_reduction_fallback_matrix(self) -> None:
        cases = (
            ("no_suggestion", "no_high_confidence_imports"),
            ("source_audit", "source_audit_failed"),
            ("import_validation", "import_validation_failed"),
            ("lean", "lean_probe_failed"),
            ("exception", "reduction_exception"),
        )
        for injected, reason in cases:
            with self.subTest(injected=injected), tempfile.TemporaryDirectory() as directory:
                patches = []
                suggestions = (
                    [] if injected == "no_suggestion" else None
                )

                def failing_audit(original, candidate, **kwargs):
                    if (
                        injected == "source_audit"
                        and kwargs.get("final")
                        and "import Mathlib.Logic.Basic\n" in candidate
                        and not has_broad_import(candidate)
                    ):
                        return {"ok": False, "violations": ["forced reduction audit"]}
                    return real_audit_source(original, candidate, **kwargs)

                def failing_validation(project, candidate):
                    if (
                        injected == "import_validation"
                        and "import Mathlib.Logic.Basic\n" in candidate
                        and not has_broad_import(candidate)
                    ):
                        return {"ok": False, "invalid": [{"module": "forced"}]}
                    return real_validate_mathlib_imports(project, candidate)

                def checker(source: str) -> LeanCheck:
                    if "missing" in source:
                        return LeanCheck(False, 1, "missing", ())
                    if (
                        injected == "lean"
                        and "import Mathlib.Logic.Basic\n" in source
                        and not has_broad_import(source)
                    ):
                        return LeanCheck(False, 1, "forced reduction Lean failure", ())
                    return LeanCheck(True, 0, "", ("lake",))

                if injected == "source_audit":
                    patches.append(
                        patch(
                            "lean_loop.workflow.audit_source",
                            side_effect=failing_audit,
                        )
                    )
                elif injected == "import_validation":
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
                            side_effect=RuntimeError("forced reduction exception"),
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
                self.assertFalse(artifact["changed"])
                self.assertEqual(artifact["selected_source"], "broad")
                self.assertEqual(artifact["fallback_reason"], reason)
                self.assertEqual(
                    artifact["selected_source_sha256"], sha256_text(broad_source)
                )
                final_audit = json.loads(
                    (result.state_dir / "final-audit.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(final_audit["ok"])
                self.assertEqual(
                    final_audit["source_sha256"], sha256_text(broad_source)
                )

    def test_terminal_and_global_cancellation_restore_broad_checkpoint(self) -> None:
        for cancellation_point in ("reduction", "global"):
            run_ids: list[str] = []
            precise_checks = 0

            def checker(source: str) -> LeanCheck:
                nonlocal precise_checks
                if "missing" in source:
                    return LeanCheck(False, 1, "missing", ())
                if (
                    "import Mathlib.Logic.Basic\n" in source
                    and not has_broad_import(source)
                ):
                    precise_checks += 1
                    if (
                        cancellation_point == "reduction"
                        or precise_checks == 2
                    ):
                        raise ProcessCancelled(f"cancel {cancellation_point}")
                return LeanCheck(True, 0, "", ("lake",))

            with self.subTest(cancellation_point=cancellation_point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaises(ProcessCancelled):
                    self._run_terminal_reduction_case(
                        root,
                        checker_override=checker,
                        keep_failed=True,
                        workflow_created_callback=run_ids.append,
                    )
                state_dir = root / ".lean-agent" / "workflows" / run_ids[0]
                target = root / "Main.lean"
                broad_source = target.read_text(encoding="utf-8")
                manifest = json.loads(
                    (state_dir / "run.json").read_text(encoding="utf-8")
                )
                artifact = json.loads(
                    (state_dir / "final-import-reduction.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(has_broad_import(broad_source))
                self.assertEqual(manifest["status"], "cancelled")
                self.assertTrue(manifest["restored"])
                self.assertEqual(
                    manifest["current_sha256"], sha256_text(broad_source)
                )
                self.assertEqual(
                    artifact["broad_source_sha256"], sha256_text(broad_source)
                )
                if cancellation_point == "reduction":
                    self.assertEqual(artifact["fallback_reason"], "cancelled")
                    self.assertEqual(artifact["selected_source"], "broad")
                else:
                    self.assertTrue(artifact["changed"])
                    self.assertEqual(artifact["selected_source"], "precise")

    def test_terminal_reduction_global_rejection_preserves_evidence_sha(self) -> None:
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
                source="-- start\n",
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
                final_audit["source_sha256"],
                artifact["selected_source_sha256"],
            )
            self.assertNotEqual(
                final_audit["source_sha256"], manifest["current_sha256"]
            )
            rejected = Path(manifest["steps"][-1]["rejected_checkpoint"])
            self.assertTrue(
                has_broad_import(
                    (rejected / "source.lean").read_text(encoding="utf-8")
                )
            )
            restored = Path(manifest["restored_to_checkpoint"])
            restored_source = (restored / "source.lean").read_text(encoding="utf-8")
            self.assertTrue(has_broad_import(restored_source))
            self.assertEqual(target.read_text(encoding="utf-8"), restored_source)
            self.assertEqual(
                manifest["current_sha256"], sha256_text(restored_source)
            )

    def test_single_step_proof_first_global_rejection_rechecks_original(self) -> None:
        original = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by exact missing\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            result, target, timeline, _ = self._run_terminal_reduction_case(
                Path(directory),
                source=original,
                global_verdict="retry",
            )

            self.assertFalse(result.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            manifest = json.loads(
                (result.state_dir / "run.json").read_text(encoding="utf-8")
            )
            restore_check = json.loads(
                (result.state_dir / "restore-check.json").read_text(
                    encoding="utf-8"
                )
            )
            initial_check = json.loads(
                (result.state_dir / "initial-check.json").read_text(
                    encoding="utf-8"
                )
            )
            original_sha = sha256_text(original)
            checked_sources = [
                str(value) for kind, value in timeline if kind == "lean"
            ]

            self.assertTrue(has_broad_import(checked_sources[0]))
            self.assertNotEqual(initial_check["source_sha256"], original_sha)
            self.assertEqual(checked_sources[-1], original)
            self.assertFalse(restore_check["reused_safe_check"])
            self.assertFalse(restore_check["reused_initial_check"])
            self.assertIsNone(restore_check["checkpoint"])
            self.assertEqual(restore_check["source_sha256"], original_sha)
            self.assertEqual(restore_check["restored_sha256"], original_sha)
            self.assertEqual(manifest["current_sha256"], original_sha)
            self.assertIsNone(manifest["restored_to_checkpoint"])

    def test_proof_first_retry_reviewer_and_checkpoint_share_broad_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory), "-- start\n")
            planner_prompts: list[str] = []
            prover_prompts: list[str] = []
            reviewer_prompts: list[str] = []
            prover_calls = 0
            reviewer_calls = 0

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                ok = "by trivial" in source or source == "import Mathlib\n-- start\n"
                return LeanCheck(ok, 0 if ok else 1, "" if ok else "missing", ())

            def json_model(config, system, user, temp):
                nonlocal reviewer_calls
                if "Planner" in system:
                    planner_prompts.append(user)
                    return {
                        "summary": "retry once",
                        "steps": [
                            {
                                "id": "step-1",
                                "goal": "prove True",
                                "success_criteria": "Lean passes",
                                "search_terms": [],
                            }
                        ],
                        "preserve": [],
                        "risks": [],
                    }
                if "global-final-audit" not in user:
                    reviewer_calls += 1
                    reviewer_prompts.append(user)
                    verdict = "retry" if reviewer_calls == 1 else "accept"
                else:
                    verdict = "accept"
                return {
                    "verdict": verdict,
                    "summary": verdict,
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }

            def file_model(config, prompt, temp):
                nonlocal prover_calls
                prover_calls += 1
                prover_prompts.append(prompt)
                if prover_calls == 1:
                    return "example first : True := by exact missing\n"
                return "example second : True := by trivial\n"

            result = run_structured_workflow(
                project=project,
                target=target,
                task="retry proof",
                plan_config=_config(),
                prove_config=_config(),
                review_config=_config(),
                max_attempts=2,
                max_attempts_per_step=2,
                lean_timeout_seconds=10,
                lake_executable="lake",
                import_policy="proof-first",
                json_model_call=json_model,
                file_model_call=file_model,
                lean_checker=checker,
            )
            self.assertTrue(result.ok)
            self.assertIn("import Mathlib\n", planner_prompts[0])
            self.assertEqual(len(prover_prompts), 2)
            self.assertTrue(all("import Mathlib\n" in row for row in prover_prompts))
            self.assertIn("example first", prover_prompts[1])
            self.assertEqual(len(reviewer_prompts), 2)
            attempt_sources = [
                (result.state_dir / "attempts" / f"{number:03d}" / "candidate.lean")
                .read_text(encoding="utf-8")
                for number in (1, 2)
            ]
            self.assertTrue(all(has_broad_import(row) for row in attempt_sources))
            self.assertTrue(
                all(source in prompt for source, prompt in zip(attempt_sources, reviewer_prompts))
            )
            checkpoint = next((result.state_dir / "checkpoints").glob("*"))
            checkpoint_source = (checkpoint / "source.lean").read_text(encoding="utf-8")
            self.assertEqual(checkpoint_source, attempt_sources[1])
            checkpoint_metadata = json.loads(
                (checkpoint / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                checkpoint_metadata["candidate_sha256"],
                sha256_text(attempt_sources[1]),
            )

    def test_resume_preserves_saved_historical_effective_policy(self) -> None:
        precise_source = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by exact useful_true\n"
        )
        suggestions = [
            {
                "module": "Mathlib.Logic.Basic",
                "confidence": "high",
                "queries": ["useful_true"],
                "evidence": ["Mathlib/Logic/Basic.lean:1 theorem useful_true"],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
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
            checkpoint_sha = sha256_text(precise_source)
            manifest["status"] = "failed"
            manifest["phase"] = "complete"
            manifest["error"] = "historical interruption"
            manifest["settings"]["import_policy"] = "auto"
            manifest["settings"]["effective_import_policy"] = "proof-first"
            manifest["current_sha256"] = checkpoint_sha
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            target.write_text(precise_source, encoding="utf-8")
            checked_sources: list[str] = []

            def checker(project: Path, target: Path, timeout: int, lake: str) -> LeanCheck:
                source = target.read_text(encoding="utf-8")
                checked_sources.append(source)
                return LeanCheck(True, 0, "", (lake, "env", "lean", "Main.lean"))

            def json_model(config, system, user, temp):
                self.assertIn("global-final-audit", user)
                return {
                    "verdict": "accept",
                    "summary": "accepted",
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
                    import_policy="auto",
                    resume_run_id=first.run_id,
                    json_model_call=json_model,
                    file_model_call=lambda *args: self.fail(
                        "completed historical plan must not call the Prover"
                    ),
                    lean_checker=checker,
                )
            self.assertTrue(resumed.ok)
            self.assertTrue(has_broad_import(checked_sources[0]))
            self.assertEqual(
                (checkpoint / "source.lean").read_bytes(), checkpoint_source_before
            )
            final_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                final_manifest["settings"]["effective_import_policy"],
                "proof-first",
            )
            artifact = json.loads(
                (first.state_dir / "final-import-reduction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                artifact["broad_source_sha256"], sha256_text(checked_sources[0])
            )
            self.assertEqual(target.read_text(encoding="utf-8"), precise_source)

    def test_proof_first_resume_reuses_only_precise_checkpoint_check(self) -> None:
        original = "example : True := by exact missing\n"
        precise_source = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by trivial\n"
        )

        def marker_check(source: str) -> LeanCheck:
            ok = "missing" not in source
            return LeanCheck(
                ok,
                0 if ok else 1,
                f"checked:{sha256_text(source)}",
                ("lake", "env", "lean", "Main.lean"),
            )

        with tempfile.TemporaryDirectory() as directory:
            first, target, _, _ = self._run_terminal_reduction_case(
                Path(directory),
                source=original,
                import_policy="precise",
                candidates=[precise_source],
                checker_override=marker_check,
            )
            self.assertTrue(first.ok)
            manifest_path = first.state_dir / "run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checkpoint = Path(manifest["steps"][0]["checkpoint"])
            checkpoint_sha = sha256_text(precise_source)
            plan_path = first.state_dir / "plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["steps"].append(
                {
                    "id": "step-2",
                    "goal": "finish proof",
                    "success_criteria": "Lean passes",
                    "search_terms": [],
                    "required_declarations": [],
                }
            )
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest["steps"].append(
                {
                    "index": 2,
                    "id": "step-2",
                    "goal": "finish proof",
                    "success_criteria": "Lean passes",
                    "status": "pending",
                    "attempts": [],
                    "checkpoint": None,
                }
            )
            manifest["status"] = "failed"
            manifest["phase"] = "complete"
            manifest["error"] = "historical interruption"
            manifest["settings"]["import_policy"] = "auto"
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
                return marker_check(source)

            def json_model(config, system, user, temp):
                self.assertNotIn("Planner", system)
                self.assertNotIn("global-final-audit", user)
                return {
                    "verdict": "retry",
                    "summary": "candidate still fails",
                    "failure_analysis": ["missing"],
                    "next_actions": [],
                    "search_terms": [],
                }

            with patch(
                "lean_loop.workflow._safe_retrieval",
                return_value={
                    "queries": [],
                    "hits": [],
                    "module_checks": [],
                    "import_suggestions": [],
                },
            ):
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
                    import_policy="auto",
                    resume_run_id=first.run_id,
                    json_model_call=json_model,
                    file_model_call=lambda *args: (
                        "import Mathlib.Logic.Basic\n"
                        "example : True := by exact still_missing\n"
                    ),
                    lean_checker=checker,
                )

            self.assertFalse(resumed.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), precise_source)
            resume_check = json.loads(
                (first.state_dir / "resume-check.json").read_text(encoding="utf-8")
            )
            restore_check = json.loads(
                (first.state_dir / "restore-check.json").read_text(
                    encoding="utf-8"
                )
            )
            checkpoint_check = json.loads(
                (checkpoint / "check.json").read_text(encoding="utf-8")
            )
            final_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )

            self.assertTrue(has_broad_import(checked_sources[0]))
            self.assertNotEqual(resume_check["source_sha256"], checkpoint_sha)
            self.assertEqual(checkpoint_check["source_sha256"], checkpoint_sha)
            self.assertEqual(
                checkpoint_check["output"], f"checked:{checkpoint_sha}"
            )
            self.assertTrue(restore_check["reused_safe_check"])
            self.assertFalse(restore_check["reused_initial_check"])
            self.assertEqual(restore_check["source_sha256"], checkpoint_sha)
            self.assertEqual(restore_check["restored_sha256"], checkpoint_sha)
            self.assertEqual(restore_check["output"], f"checked:{checkpoint_sha}")
            self.assertEqual(restore_check["checkpoint"], str(checkpoint))
            self.assertEqual(final_manifest["current_sha256"], checkpoint_sha)
            self.assertEqual(
                final_manifest["restored_to_checkpoint"], str(checkpoint)
            )

    def test_historical_precise_resume_global_rejection_rechecks_original(self) -> None:
        original = "example : True := by exact missing\n"
        precise_source = (
            "import Mathlib.Logic.Basic\n"
            "example : True := by exact useful_true\n"
        )
        suggestions = [
            {
                "module": "Mathlib.Logic.Basic",
                "confidence": "high",
                "queries": ["useful_true"],
                "evidence": ["Mathlib/Logic/Basic.lean:1 theorem useful_true"],
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            first, target, _, _ = self._run_terminal_reduction_case(
                Path(directory),
                source=original,
                import_policy="precise",
                candidates=[precise_source],
            )
            self.assertTrue(first.ok)
            manifest_path = first.state_dir / "run.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checkpoint = Path(manifest["steps"][0]["checkpoint"])
            checkpoint_sha = sha256_text(precise_source)
            manifest["status"] = "failed"
            manifest["phase"] = "complete"
            manifest["error"] = "historical interruption"
            manifest["settings"]["import_policy"] = "auto"
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
                ok = "missing" not in source
                return LeanCheck(
                    ok,
                    0 if ok else 1,
                    f"checked:{sha256_text(source)}",
                    (lake, "env", "lean", "Main.lean"),
                )

            def json_model(config, system, user, temp):
                self.assertIn("global-final-audit", user)
                return {
                    "verdict": "retry",
                    "summary": "reject resumed final source",
                    "failure_analysis": ["incomplete"],
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
                    import_policy="auto",
                    resume_run_id=first.run_id,
                    json_model_call=json_model,
                    file_model_call=lambda *args: self.fail(
                        "completed historical plan must not call the Prover"
                    ),
                    lean_checker=checker,
                )

            self.assertFalse(resumed.ok)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            resume_check = json.loads(
                (first.state_dir / "resume-check.json").read_text(encoding="utf-8")
            )
            restore_check = json.loads(
                (first.state_dir / "restore-check.json").read_text(
                    encoding="utf-8"
                )
            )
            checkpoint_check = json.loads(
                (checkpoint / "check.json").read_text(encoding="utf-8")
            )
            final_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            original_sha = sha256_text(original)

            self.assertTrue(has_broad_import(checked_sources[0]))
            self.assertEqual(
                resume_check["source_sha256"], sha256_text(checked_sources[0])
            )
            self.assertEqual(checkpoint_check["source_sha256"], checkpoint_sha)
            self.assertEqual(checked_sources[-1], original)
            self.assertFalse(restore_check["reused_safe_check"])
            self.assertFalse(restore_check["reused_initial_check"])
            self.assertEqual(restore_check["source_sha256"], original_sha)
            self.assertEqual(restore_check["restored_sha256"], original_sha)
            self.assertIsNone(restore_check["checkpoint"])
            self.assertEqual(final_manifest["current_sha256"], original_sha)
            self.assertIsNone(final_manifest["restored_to_checkpoint"])

    def test_resume_policy_change_replans_and_original_archive_is_fail_closed(self) -> None:
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
                        "steps": [
                            {
                                "id": "step-1",
                                "goal": "prove True",
                                "success_criteria": "Lean passes",
                                "search_terms": [],
                            }
                        ],
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
            self.assertTrue(has_broad_import(target.read_text(encoding="utf-8")))
            events = (resumed.state_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"reason": "import_policy_changed"', events)

        for mode in ("missing", "tampered"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                project, target = self._project(
                    Path(directory), "example : True := by exact missing\n"
                )
                first = run_structured_workflow(
                    project=project,
                    target=target,
                    task="fail before resume",
                    plan_config=_config(),
                    prove_config=_config(),
                    review_config=_config(),
                    max_attempts=1,
                    lean_timeout_seconds=10,
                    lake_executable="lake",
                    import_policy="auto",
                    json_model_call=lambda config, system, user, temp: (
                        {
                            "summary": "fail",
                            "steps": [
                                {
                                    "id": "step-1",
                                    "goal": "fail",
                                    "success_criteria": "Lean passes",
                                    "search_terms": [],
                                }
                            ],
                            "preserve": [],
                            "risks": [],
                        }
                        if "Planner" in system
                        else {
                            "verdict": "retry",
                            "summary": "failed",
                            "failure_analysis": [],
                            "next_actions": [],
                            "search_terms": [],
                        }
                    ),
                    file_model_call=lambda config, prompt, temp: (
                        "example : True := by exact missing\n"
                    ),
                    lean_checker=lambda project, target, timeout, lake: LeanCheck(
                        False, 1, "missing", ()
                    ),
                )
                original_path = first.state_dir / "original.lean"
                if mode == "missing":
                    original_path.unlink()
                    expected = "original.lean"
                else:
                    original_path.write_text(
                        original_path.read_text(encoding="utf-8") + "-- tampered\n",
                        encoding="utf-8",
                    )
                    expected = "Original workflow source hash no longer matches"
                target_before = target.read_text(encoding="utf-8")
                model_calls = 0

                def unexpected_model(*args, **kwargs):
                    nonlocal model_calls
                    model_calls += 1
                    raise AssertionError("resume archive gate must precede model calls")

                with self.assertRaisesRegex((FileNotFoundError, ValueError), expected):
                    run_structured_workflow(
                        project=project,
                        target=target,
                        task="fail before resume",
                        plan_config=_config(),
                        prove_config=_config(),
                        review_config=_config(),
                        max_attempts=2,
                        lean_timeout_seconds=10,
                        lake_executable="lake",
                        import_policy="auto",
                        resume_run_id=first.run_id,
                        json_model_call=unexpected_model,
                        file_model_call=unexpected_model,
                        lean_checker=lambda project, target, timeout, lake: LeanCheck(
                            False, 1, "missing", ()
                        ),
                    )
                self.assertEqual(model_calls, 0)
                self.assertEqual(target.read_text(encoding="utf-8"), target_before)

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
        broad_source = (
            "/-\n"
            "import Mathlib\n"
            "-/\n"
            "import Mathlib\n"
            "example : True := by exact True.intro\n"
        )
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
            candidate, metadata = optimize_broad_imports(
                broad_source, retrieval
            )
            self.assertTrue(metadata["changed"])
            self.assertIn("/-\nimport Mathlib\n-/\n", candidate)
            self.assertIn("import Mathlib.Logic.Basic\n", candidate)
            target.write_text(candidate, encoding="utf-8")
            reduced_check = check_lean(project, target, 120, "lake")
            self.assertTrue(reduced_check.ok, reduced_check.output)
        finally:
            target.unlink(missing_ok=True)

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
