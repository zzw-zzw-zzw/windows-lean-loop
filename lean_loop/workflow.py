from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lean_loop.agent_protocol import AgentBackend, AgentRuntime, DirectModelBackend
from lean_loop.api import ApiError, call_model, call_model_json
from lean_loop.audit import audit_source
from lean_loop.config import ApiConfig
from lean_loop.jsonutil import atomic_write_json, atomic_write_text, read_json, sha256_text
from lean_loop.lean import LeanCheck, check_lean
from lean_loop.mathlib_search import (
    collect_retrieval,
    ensure_broad_mathlib_import,
    has_broad_import,
    import_validation_diagnostics,
    optimize_broad_imports,
    repair_invalid_mathlib_imports,
    retrieval_prompt_block,
    source_search_terms,
    validate_mathlib_imports,
)
from lean_loop.prompts import (
    GOAL_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    PROVER_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    build_goal_prompt,
    build_plan_prompt,
    build_review_prompt,
    build_user_prompt,
)
from lean_loop.process_control import ProcessCancelled, ProcessControl
from lean_loop.state import WorkflowStore
from lean_loop.timings import TimingRecorder


@dataclass(frozen=True)
class WorkflowResult:
    ok: bool
    run_id: str
    attempts: int
    state_dir: Path
    final_check: LeanCheck
    restored: bool


JsonModelCall = Callable[[ApiConfig, str, str, Path], dict[str, Any]]
FileModelCall = Callable[[ApiConfig, str, Path], str]
LeanChecker = Callable[[Path, Path, int, str], LeanCheck]
PhaseCallback = Callable[[str, int | None], None]
WorkflowCreatedCallback = Callable[[str], None]
BackendIdentityCallback = Callable[[dict[str, Any]], None]

IMPORT_POLICIES = {"auto", "proof-first", "precise", "broad"}
_TOP_LEVEL_DECLARATION_RE = re.compile(
    r"^\s*(?:theorem|lemma|def|abbrev|structure|class|inductive|example)\b",
    re.MULTILINE,
)
_FORMAL_DECLARATION_RE = re.compile(
    r"^\s*theorem\s+(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)\b"
)
_DECLARATION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*$")


class _FinalImportReductionRestoreError(OSError):
    pass


def _qualify_formal_declaration(declaration: str) -> str:
    replacements = (
        ("IsBounded", "Bornology.IsBounded"),
        ("Tendsto", "Filter.Tendsto"),
        ("atTop", "Filter.atTop"),
    )
    result = declaration
    for short_name, qualified_name in replacements:
        result = re.sub(
            rf"(?<![A-Za-z0-9_.]){re.escape(short_name)}\b",
            qualified_name,
            result,
        )
    return result


def natural_language_search_terms(task: str) -> list[str]:
    lowered = task.casefold()
    groups = [
        (
            ("欧式空间", "euclidean space", "r^n", "ℝ^n"),
            ["EuclideanSpace", "FiniteDimensional.proper", "ProperSpace"],
        ),
        (
            ("有界数列", "bounded sequence"),
            ["tendsto_subseq_of_bounded", "IsBounded", "Set.range"],
        ),
        (
            ("收敛子列", "convergent subsequence", "subsequence"),
            ["tendsto_subseq_of_bounded", "StrictMono", "Tendsto", "atTop"],
        ),
        (("紧致", "compact"), ["IsCompact", "IsCompact.tendsto_subseq"]),
        (("连续", "continuous"), ["Continuous", "Tendsto"]),
        (("多项式", "polynomial"), ["Polynomial", "ring", "nlinarith"]),
        (("下界", "lower bound"), ["lowerBounds", "nlinarith"]),
    ]
    terms: list[str] = []
    for needles, additions in groups:
        if any(needle.casefold() in lowered for needle in needles):
            for value in additions:
                if value not in terms:
                    terms.append(value)
    return terms


def needs_goal_formalization(source: str) -> bool:
    return _TOP_LEVEL_DECLARATION_RE.search(source) is None


def validate_formal_goal(value: dict[str, Any]) -> dict[str, Any]:
    summary = value.get("summary")
    declaration = value.get("declaration")
    if not isinstance(summary, str) or not summary.strip():
        raise ApiError("Goal JSON requires a non-empty summary")
    if isinstance(declaration, str):
        declaration = declaration.strip()
        if declaration.endswith(":="):
            declaration = declaration[:-2].rstrip()
        declaration = _qualify_formal_declaration(declaration)
    if not isinstance(declaration, str) or not _FORMAL_DECLARATION_RE.match(declaration):
        raise ApiError("Goal JSON requires one theorem declaration header")
    lowered = declaration.casefold()
    forbidden = (":=", " by", "sorry", "admit", "axiom", "constant")
    if any(token in lowered for token in forbidden):
        raise ApiError("Formal goal declaration must not contain a proof or axiom")
    return {
        "summary": summary.strip(),
        "declaration": declaration.strip(),
        "search_terms": _validate_string_list(value.get("search_terms"), "search_terms"),
        "assumptions": _validate_string_list(value.get("assumptions"), "assumptions"),
        "ambiguities": _validate_string_list(value.get("ambiguities"), "ambiguities"),
        "validated": False,
        "check": None,
    }


def _formal_goal_prompt_block(goal: dict[str, Any] | None) -> str:
    if not goal:
        return "No separate formal goal contract was created."
    return json.dumps(goal, ensure_ascii=False, indent=2)


def _effective_import_policy(import_policy: str, source: str) -> str:
    if import_policy not in IMPORT_POLICIES:
        raise ValueError(f"Unsupported import policy: {import_policy}")
    if import_policy != "auto":
        return import_policy
    return "proof-first"


def _historical_effective_import_policy(
    previous: dict[str, Any], original_source: str
) -> tuple[str, bool]:
    old_settings = dict(previous.get("settings") or {})
    raw_policy = str(old_settings.get("import_policy") or "auto")
    if raw_policy not in IMPORT_POLICIES:
        raise ValueError(f"Unsupported historical import policy: {raw_policy}")
    historical_effective = old_settings.get("effective_import_policy")
    if historical_effective is not None:
        effective = str(historical_effective)
        if effective not in IMPORT_POLICIES - {"auto"}:
            raise ValueError(
                f"Unsupported historical effective import policy: {effective}"
            )
        return effective, False
    if raw_policy != "auto":
        return raw_policy, False
    legacy_effective = (
        "proof-first" if needs_goal_formalization(original_source) else "precise"
    )
    return legacy_effective, True


def _check_to_json(check: LeanCheck) -> dict[str, Any]:
    return {
        "ok": check.ok,
        "returncode": check.returncode,
        "output": check.output,
        "command": list(check.command),
    }


def _check_from_json(value: dict[str, Any]) -> LeanCheck:
    return LeanCheck(
        bool(value.get("ok")),
        int(value.get("returncode") or 0),
        str(value.get("output") or ""),
        tuple(str(part) for part in value.get("command") or ()),
    )


def _checkpoint_state(
    store: WorkflowStore,
    manifest: dict[str, Any],
    original_source: str,
    fallback_check: LeanCheck,
) -> tuple[str, LeanCheck, str, str | None]:
    for step in reversed(list(manifest.get("steps") or [])):
        if not isinstance(step, dict):
            continue
        checkpoint_value = step.get("checkpoint")
        if step.get("status") != "succeeded" or not checkpoint_value:
            continue
        checkpoint = Path(str(checkpoint_value))
        source_path = checkpoint / "source.lean"
        check_path = checkpoint / "check.json"
        metadata_path = checkpoint / "checkpoint.json"
        if not source_path.is_file() or not check_path.is_file() or not metadata_path.is_file():
            raise ValueError(f"Resume checkpoint is incomplete: {checkpoint}")
        source = source_path.read_text(encoding="utf-8")
        metadata = read_json(metadata_path)
        source_sha = sha256_text(source)
        if source_sha != metadata.get("candidate_sha256"):
            raise ValueError(f"Resume checkpoint hash mismatch: {checkpoint}")
        return source, _check_from_json(read_json(check_path)), source_sha, str(checkpoint)
    return original_source, fallback_check, sha256_text(original_source), None


def _latest_attempt_check(store: WorkflowStore, attempts: list[dict[str, Any]]) -> LeanCheck | None:
    if not attempts:
        return None
    number = int(attempts[-1].get("attempt") or 0)
    path = store.paths.attempt_dir(number) / "check.json"
    return _check_from_json(read_json(path)) if path.is_file() else None


def _resume_attempt_number(
    store: WorkflowStore, attempts: list[dict[str, Any]]
) -> tuple[int, list[int]]:
    recorded = {
        int(row.get("attempt") or 0)
        for row in attempts
        if isinstance(row, dict) and int(row.get("attempt") or 0) > 0
    }
    on_disk = {
        int(path.name)
        for path in store.paths.attempts.iterdir()
        if path.is_dir() and path.name.isdigit() and int(path.name) > 0
    } if store.paths.attempts.is_dir() else set()
    occupied = recorded | on_disk
    return max(occupied, default=0), sorted(on_disk - recorded)


def _validate_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ApiError(f"Model JSON field {field!r} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def validate_plan(value: dict[str, Any]) -> dict[str, Any]:
    summary = value.get("summary")
    steps = value.get("steps")
    if not isinstance(summary, str) or not summary.strip():
        raise ApiError("Plan JSON requires a non-empty 'summary'")
    if not isinstance(steps, list) or not steps:
        raise ApiError("Plan JSON requires a non-empty 'steps' list")

    normalized_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ApiError("Every plan step must be a JSON object")
        goal = step.get("goal")
        criteria = step.get("success_criteria")
        if not isinstance(goal, str) or not goal.strip():
            raise ApiError(f"Plan step {index} requires a goal")
        if not isinstance(criteria, str) or not criteria.strip():
            raise ApiError(f"Plan step {index} requires success_criteria")
        required_declarations = _validate_string_list(
            step.get("required_declarations"),
            f"steps[{index}].required_declarations",
        )
        if any(not _DECLARATION_NAME_RE.fullmatch(name) for name in required_declarations):
            raise ApiError(
                f"Plan step {index} required_declarations must contain Lean names only"
            )
        normalized_steps.append(
            {
                "id": str(step.get("id") or f"step-{index}"),
                "goal": goal.strip(),
                "success_criteria": criteria.strip(),
                "search_terms": _validate_string_list(
                    step.get("search_terms"), f"steps[{index}].search_terms"
                ),
                "required_declarations": required_declarations,
            }
        )
    return {
        "summary": summary.strip(),
        "steps": normalized_steps,
        "preserve": _validate_string_list(value.get("preserve"), "preserve"),
        "risks": _validate_string_list(value.get("risks"), "risks"),
    }


def _formal_goal_name(goal: dict[str, Any] | None) -> str | None:
    if not goal or not goal.get("validated"):
        return None
    match = _FORMAL_DECLARATION_RE.match(str(goal.get("declaration") or ""))
    return match.group("name") if match else None


def _apply_plan_contract(
    plan: dict[str, Any], formal_goal: dict[str, Any] | None
) -> dict[str, Any]:
    goal_name = _formal_goal_name(formal_goal)
    if not goal_name:
        return plan
    steps = plan["steps"]
    for step in steps[:-1]:
        step["required_declarations"] = [
            name for name in step.get("required_declarations", []) if name != goal_name
        ]
    final_required = list(steps[-1].get("required_declarations", []))
    if goal_name not in final_required:
        final_required.append(goal_name)
    steps[-1]["required_declarations"] = final_required
    return plan


def _exact_theorem_hits(
    retrieval: dict[str, object], preferred_terms: list[str]
) -> list[dict[str, object]]:
    preferred = {term.casefold() for term in preferred_terms}
    hits: list[dict[str, object]] = []
    for row in retrieval.get("hits", []):
        if not isinstance(row, dict):
            continue
        query = str(row.get("query") or "")
        if (
            str(row.get("match") or "") == "exact"
            and str(row.get("kind") or "") in {"theorem", "lemma"}
            and query.casefold() in preferred
        ):
            hits.append(row)
    return hits


def _direct_proof_plan(
    hits: list[dict[str, object]], formal_goal: dict[str, Any] | None
) -> dict[str, Any]:
    names = list(
        dict.fromkeys(str(row.get("name") or row.get("query")) for row in hits)
    )
    plan = validate_plan(
        {
            "summary": (
                "Use exact local Mathlib declarations directly: " + ", ".join(names)
            ),
            "steps": [
                {
                    "id": "direct-proof",
                    "goal": (
                        "Prove the complete requested theorem by applying or specializing "
                        "the exact local declaration(s): " + ", ".join(names)
                    ),
                    "success_criteria": (
                        "The complete file passes Lean and the final source audit."
                    ),
                    "search_terms": names,
                    "required_declarations": [],
                }
            ],
            "preserve": [],
            "risks": ["Lean must verify the exact arguments and witness order."],
        }
    )
    return _apply_plan_contract(plan, formal_goal)


def _resume_replan_reason(
    store: WorkflowStore,
    previous: dict[str, Any],
    current_settings: dict[str, Any],
) -> str | None:
    old_settings = dict(previous.get("settings") or {})
    old_backend = str(old_settings.get("agent_backend") or "direct")
    if old_backend != str(current_settings.get("agent_backend") or "direct"):
        return "backend_changed"
    old_backend_identity = old_settings.get("backend_identity")
    if (
        old_backend_identity is not None
        and old_backend_identity != current_settings.get("backend_identity")
    ):
        return "backend_identity_changed"
    old_import_policy = str(old_settings.get("import_policy") or "auto")
    if old_import_policy != current_settings.get("import_policy"):
        return "import_policy_changed"
    old_models = old_settings.get("models")
    if old_models and old_models != current_settings.get("models"):
        return "model_changed"
    old_providers = old_settings.get("provider_signatures")
    if old_providers and old_providers != current_settings.get("provider_signatures"):
        return "provider_changed"

    attempts = list(previous.get("attempts") or [])
    if len(attempts) < 2:
        return None
    diagnostics: list[str] = []
    for row in attempts[-2:]:
        if not isinstance(row, dict) or row.get("check_ok"):
            return None
        number = int(row.get("attempt") or 0)
        check_path = store.paths.attempt_dir(number) / "check.json"
        if not check_path.is_file():
            return None
        output = str(read_json(check_path).get("output") or "")
        diagnostics.append(" ".join(output.split()))
    if diagnostics[0] and diagnostics[0] == diagnostics[1]:
        return "repeated_diagnostics"
    return None


def validate_review(
    value: dict[str, Any], check_ok: bool, *, allow_stop: bool = False
) -> dict[str, Any]:
    verdict = value.get("verdict")
    if verdict not in {"accept", "retry", "stop"}:
        raise ApiError("Review verdict must be accept, retry, or stop")
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ApiError("Review JSON requires a non-empty summary")
    if verdict == "accept" and not check_ok:
        verdict = "retry"
        summary = "Reviewer claimed acceptance, but Lean failed. " + summary
    stop_rejected = verdict == "stop" and not allow_stop
    if stop_rejected:
        verdict = "retry"
        summary = (
            "Reviewer requested stop without a deterministic external blocker; "
            "the remaining candidate budget stays active. " + summary
        )
    result = {
        "verdict": verdict,
        "summary": summary.strip(),
        "failure_analysis": _validate_string_list(
            value.get("failure_analysis"), "failure_analysis"
        ),
        "next_actions": _validate_string_list(value.get("next_actions"), "next_actions"),
        "search_terms": _validate_string_list(value.get("search_terms"), "search_terms"),
    }
    if stop_rejected:
        result["stop_rejected"] = True
    return result


def _fallback_review(check: LeanCheck, error: str) -> dict[str, Any]:
    return {
        "verdict": "retry",
        "summary": "Deterministic fallback review because the reviewer API failed.",
        "failure_analysis": [
            "Lean passed, but the active Plan step could not be semantically reviewed."
            if check.ok
            else check.output or "Lean exited non-zero."
        ],
        "next_actions": [
            "Retry the active Plan step and obtain an explicit reviewer decision."
            if check.ok
            else "Use the exact Lean diagnostics in the next attempt."
        ],
        "search_terms": [],
        "reviewer_error": error,
    }


def _fallback_final_audit_review(
    check: LeanCheck, source_audit_ok: bool, error: str
) -> dict[str, Any]:
    if check.ok and source_audit_ok:
        return {
            "verdict": "accept",
            "summary": (
                "The semantic reviewer was unavailable, but Lean and every "
                "deterministic final source audit passed."
            ),
            "failure_analysis": [],
            "next_actions": [],
            "search_terms": [],
            "reviewer_error": error,
            "review_status": "unavailable",
            "accepted_by": "deterministic_final_audit",
        }
    return _fallback_review(check, error)


def _plan_search_terms(plan: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    for step in plan.get("steps", []):
        if isinstance(step, dict):
            for term in step.get("search_terms", []):
                if isinstance(term, str) and term not in terms:
                    terms.append(term)
    return terms


def _safe_retrieval(
    project: Path,
    *,
    diagnostics: str,
    requested_terms: list[str],
    process_control: ProcessControl | None = None,
) -> dict[str, object]:
    try:
        return collect_retrieval(
            project,
            diagnostics=diagnostics,
            requested_terms=requested_terms,
            process_control=process_control,
        )
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError) as exc:
        return {
            "queries": requested_terms,
            "hits": [],
            "module_checks": [],
            "error": str(exc),
        }


def _final_import_reduction_summary(
    artifact: dict[str, Any],
) -> dict[str, Any]:
    return {
        "artifact": "final-import-reduction.json",
        "attempted": artifact["attempted"],
        "changed": artifact["changed"],
        "effective_import_policy": artifact["effective_import_policy"],
        "broad_source_sha256": artifact["broad_source_sha256"],
        "candidate_source_sha256": artifact["candidate_source_sha256"],
        "selected_source_sha256": artifact["selected_source_sha256"],
        "selected_source": artifact["selected_source"],
        "fallback_reason": artifact["fallback_reason"],
    }


def _restore_final_broad_source(
    target: Path,
    broad_source: str,
    broad_source_sha256: str,
) -> None:
    try:
        atomic_write_text(target, broad_source)
    except Exception as exc:
        raise _FinalImportReductionRestoreError(
            f"Final import reduction could not restore the broad source: {exc}"
        ) from exc
    restored_sha = sha256_text(target.read_text(encoding="utf-8"))
    if restored_sha != broad_source_sha256:
        raise _FinalImportReductionRestoreError(
            "Final import reduction restored a source with the wrong SHA-256"
        )


def _write_final_import_reduction(
    store: WorkflowStore,
    artifact: dict[str, Any],
) -> None:
    atomic_write_json(store.paths.root / "final-import-reduction.json", artifact)
    store.update(
        final_import_reduction=_final_import_reduction_summary(artifact),
        current_sha256=artifact["selected_source_sha256"],
    )


def run_structured_workflow(
    *,
    project: Path,
    target: Path,
    task: str,
    plan_config: ApiConfig,
    prove_config: ApiConfig,
    review_config: ApiConfig,
    max_attempts: int,
    max_attempts_per_step: int | None = None,
    lean_timeout_seconds: int,
    lake_executable: str,
    keep_failed: bool = False,
    formalize_goal: bool = False,
    import_policy: str = "auto",
    protect_existing_statements: bool = True,
    protected_declarations: list[str] | None = None,
    resume_run_id: str | None = None,
    json_model_call: JsonModelCall = call_model_json,
    file_model_call: FileModelCall = call_model,
    lean_checker: LeanChecker = check_lean,
    phase_callback: PhaseCallback | None = None,
    workflow_created_callback: WorkflowCreatedCallback | None = None,
    backend_identity_callback: BackendIdentityCallback | None = None,
    process_control: ProcessControl | None = None,
    agent_backend: AgentBackend | None = None,
    agent_backend_id: str = "direct",
) -> WorkflowResult:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if max_attempts_per_step is None:
        max_attempts_per_step = max_attempts
    if max_attempts_per_step < 1:
        raise ValueError("max_attempts_per_step must be positive")
    protected_declarations = list(protected_declarations or [])
    if agent_backend_id not in {
        "direct",
        "codex-subscription",
        "claude-subscription",
    }:
        raise ValueError(f"Unsupported Agent backend: {agent_backend_id}")
    relative = target.relative_to(project)
    selected_backend = agent_backend
    backend_identity: dict[str, Any] | None = None
    if agent_backend_id != "direct":
        from lean_loop.subscription_backend import (
            build_subscription_identity_summary,
            create_subscription_backend,
        )

        if selected_backend is None:
            selected_backend = create_subscription_backend(
                agent_backend_id,
                protected_root=project,
                protected_target=target,
                process_control=process_control,
            )
        backend_identity = build_subscription_identity_summary(
            selected_backend,  # type: ignore[arg-type]
            {
                "plan": plan_config,
                "prove": prove_config,
                "review": review_config,
            },
        )
    settings = {
        "agent_backend": agent_backend_id,
        "max_attempts": max_attempts,
        "max_attempts_total": max_attempts,
        "max_attempts_per_step": max_attempts_per_step,
        "lean_timeout_seconds": lean_timeout_seconds,
        "lake_executable": lake_executable,
        "keep_failed": keep_failed,
        "formalize_goal": formalize_goal,
        "import_policy": import_policy,
        "protect_existing_statements": protect_existing_statements,
        "protected_declarations": protected_declarations,
        "models": {
            "plan": plan_config.model,
            "prove": prove_config.model,
            "review": review_config.model,
        },
        "provider_signatures": {
            "plan": {
                "backend": agent_backend_id,
                "model": plan_config.model,
                "mode": plan_config.mode,
                "endpoint": plan_config.endpoint if agent_backend_id == "direct" else None,
            },
            "prove": {
                "backend": agent_backend_id,
                "model": prove_config.model,
                "mode": prove_config.mode,
                "endpoint": prove_config.endpoint if agent_backend_id == "direct" else None,
            },
            "review": {
                "backend": agent_backend_id,
                "model": review_config.model,
                "mode": review_config.mode,
                "endpoint": review_config.endpoint if agent_backend_id == "direct" else None,
            },
        },
        "reasoning_effort": {
            "plan": plan_config.reasoning_effort,
            "prove": prove_config.reasoning_effort,
            "review": review_config.reasoning_effort,
        },
    }
    if backend_identity is not None:
        settings["backend_identity"] = backend_identity
    resuming = resume_run_id is not None
    resume_safe_state: tuple[str, LeanCheck, str, str | None] | None = None
    resume_replan_reason: str | None = None
    legacy_policy_resolved = False
    if resuming:
        store = WorkflowStore.open(project, str(resume_run_id))
        previous = store.read()
        if str(previous.get("target_file")) != relative.as_posix():
            raise ValueError("Resume target does not match the original workflow target")
        if str(previous.get("task")) != task:
            raise ValueError("Resume task does not match the original workflow task")
        original_source = store.paths.original.read_text(encoding="utf-8")
        original_sha = sha256_text(original_source)
        if original_sha != previous.get("original_sha256"):
            raise ValueError("Original workflow source hash no longer matches its manifest")
        previous_initial = _check_from_json(dict(previous.get("initial_check") or {}))
        resume_safe_state = _checkpoint_state(
            store, previous, original_source, previous_initial
        )
        if sha256_text(target.read_text(encoding="utf-8")) != resume_safe_state[2]:
            raise ValueError(
                "Target file changed after the last verified checkpoint; "
                "resume refuses to overwrite external edits"
            )
        old_settings = dict(previous.get("settings") or {})
        old_import_policy = str(old_settings.get("import_policy") or "auto")
        if import_policy == old_import_policy:
            effective_import_policy, legacy_policy_resolved = (
                _historical_effective_import_policy(previous, original_source)
            )
        else:
            effective_import_policy = _effective_import_policy(
                import_policy, original_source
            )
        settings["effective_import_policy"] = effective_import_policy
        resume_replan_reason = _resume_replan_reason(store, previous, settings)
        store.resume(phase="lean_checking", settings=settings)
        if legacy_policy_resolved:
            store.event(
                "legacy_import_policy_resolved",
                import_policy=old_import_policy,
                effective_import_policy=effective_import_policy,
            )
    else:
        original_source = target.read_text(encoding="utf-8")
        original_sha = sha256_text(original_source)
        effective_import_policy = _effective_import_policy(
            import_policy, original_source
        )
        settings["effective_import_policy"] = effective_import_policy
        store = WorkflowStore.create(
            project=project,
            target_file=relative.as_posix(),
            task=task,
            settings=settings,
            original_sha256=original_sha,
        )
        atomic_write_text(store.paths.original, original_source)
    timings = TimingRecorder(store.paths.timings, resume=resuming)
    agent_runtime = AgentRuntime(
        workflow_root=store.paths.root,
        run_id=store.paths.run_id,
        backend=selected_backend
        or DirectModelBackend(
            json_model_call=json_model_call,
            file_model_call=file_model_call,
        ),
    )

    target_changed = False
    transaction_touched = False
    try:
        if backend_identity is not None and backend_identity_callback is not None:
            backend_identity_callback(dict(backend_identity))
        if workflow_created_callback is not None:
            workflow_created_callback(store.paths.run_id)
        if phase_callback is not None:
            phase_callback("lean_checking", None)
        preflight_source = target.read_text(encoding="utf-8")
        preflight_terms = natural_language_search_terms(task)
        include_source_terms = not (
            formalize_goal and needs_goal_formalization(preflight_source)
        )
        source_terms = (
            source_search_terms(preflight_source, task) if include_source_terms else []
        )
        for term in source_terms:
            if term not in preflight_terms:
                preflight_terms.append(term)
        with timings.measure("import_optimization"):
            seed_retrieval = _safe_retrieval(
                project,
                diagnostics="",
                requested_terms=preflight_terms,
                process_control=process_control,
            )
            if effective_import_policy == "precise":
                optimized_source, import_optimization = optimize_broad_imports(
                    preflight_source, seed_retrieval
                )
            else:
                optimized_source = ensure_broad_mathlib_import(preflight_source)
                import_optimization = {
                    "changed": optimized_source != preflight_source,
                    "reason": (
                        "broad_import_ensured"
                        if optimized_source != preflight_source
                        else "broad_import_present"
                    ),
                    "policy": effective_import_policy,
                }
        if import_optimization.get("changed"):
            # Probe exact imports before the first expensive Lean check. Keep
            # this source in the working tree so the Planner and Prover see the
            # same fast import set; normal failure/cancellation restores the
            # original source through the transaction path below.
            atomic_write_text(target, optimized_source)
            target_changed = True
            transaction_touched = True
        initial_import_validation = validate_mathlib_imports(
            project, target.read_text(encoding="utf-8")
        )
        if initial_import_validation["ok"]:
            with timings.measure("resume_lean_check" if resuming else "initial_lean_check"):
                initial_check = lean_checker(
                    project, target, lean_timeout_seconds, lake_executable
                )
        else:
            initial_check = LeanCheck(
                False,
                1,
                import_validation_diagnostics(initial_import_validation),
                ("import-validation", relative.as_posix()),
            )
        import_optimization["import_validation"] = initial_import_validation
        import_optimization["probe_ok"] = initial_check.ok
        import_optimization["probe_returncode"] = initial_check.returncode
        with timings.measure("resume_retrieval" if resuming else "initial_retrieval"):
            initial_retrieval = _safe_retrieval(
                project,
                diagnostics=initial_check.output,
                requested_terms=preflight_terms,
                process_control=process_control,
            )
        initial_retrieval["import_optimization"] = import_optimization
    except ProcessCancelled as exc:
        restored = False
        if target_changed and not keep_failed:
            atomic_write_text(target, preflight_source)
            restored = True
        current_sha = sha256_text(target.read_text(encoding="utf-8"))
        restored_checkpoint = None
        if (
            restored
            and resume_safe_state is not None
            and current_sha == resume_safe_state[2]
        ):
            restored_checkpoint = resume_safe_state[3]
        timing_summary = timings.finish("cancelled")
        store.update(
            status="cancelled",
            phase="complete",
            current_sha256=current_sha,
            restored=restored,
            restored_to_checkpoint=restored_checkpoint,
            error=str(exc),
            timings=timing_summary,
        )
        store.event("workflow_cancelled", restored=restored)
        raise
    except Exception as exc:
        restored = False
        if target_changed and not keep_failed:
            atomic_write_text(target, preflight_source)
            restored = True
        current_sha = sha256_text(target.read_text(encoding="utf-8"))
        restored_checkpoint = None
        if (
            restored
            and resume_safe_state is not None
            and current_sha == resume_safe_state[2]
        ):
            restored_checkpoint = resume_safe_state[3]
        timing_summary = timings.finish("failed")
        store.update(
            status="failed",
            phase="complete",
            current_sha256=current_sha,
            restored=restored,
            restored_to_checkpoint=restored_checkpoint,
            error=f"{type(exc).__name__}: {exc}",
            timings=timing_summary,
        )
        store.event(
            "workflow_crashed",
            error=f"{type(exc).__name__}: {exc}",
            restored=restored,
        )
        raise
    check_name = "resume-check.json" if resuming else "initial-check.json"
    retrieval_name = "resume-retrieval.json" if resuming else "initial-retrieval.json"
    atomic_write_json(store.paths.root / check_name, _check_to_json(initial_check))
    atomic_write_json(store.paths.root / retrieval_name, initial_retrieval)
    preflight_current_sha = sha256_text(target.read_text(encoding="utf-8"))
    store.update(
        initial_check=_check_to_json(initial_check),
        current_sha256=preflight_current_sha,
    )

    formal_goal: dict[str, Any] | None = None
    formal_goal_created = False
    planning_retrieval = initial_retrieval
    goal_path = store.paths.root / "goal.json"
    if resuming and goal_path.is_file():
        formal_goal = validate_formal_goal(read_json(goal_path))
        saved_goal = read_json(goal_path)
        formal_goal["validated"] = bool(saved_goal.get("validated"))
        formal_goal["check"] = saved_goal.get("check")
    elif formalize_goal and needs_goal_formalization(original_source):
        if phase_callback is not None:
            phase_callback("planning", None)
        store.event("phase_started", phase="formalize_goal")
        try:
            with timings.measure("formalize_goal_api"):
                formal_goal = validate_formal_goal(
                    agent_runtime.invoke(
                        role="formalizer",
                        phase="formalize_goal",
                        output_type="json",
                        config=plan_config,
                        system_prompt=GOAL_SYSTEM_PROMPT,
                        user_prompt=build_goal_prompt(
                            relative_file=relative.as_posix(),
                            task=task,
                            retrieval=retrieval_prompt_block(initial_retrieval),
                        ),
                        temp_dir=store.paths.temp,
                        context={"target_file": relative.as_posix()},
                    )
                )
            store.paths.temp.mkdir(parents=True, exist_ok=True)
            goal_probe = store.paths.temp / "FormalGoalCheck.lean"
            def write_goal_probe(goal: dict[str, Any]) -> None:
                atomic_write_text(
                    goal_probe,
                    "import Mathlib\n\nopen scoped Topology\n\n"
                    + goal["declaration"]
                    + " := by\n  sorry\n",
                )

            write_goal_probe(formal_goal)
            with timings.measure("formalize_goal_lean_check"):
                goal_check = lean_checker(
                    project, goal_probe, lean_timeout_seconds, lake_executable
                )
            atomic_write_json(
                store.paths.root / "formal-goal-check-001.json",
                _check_to_json(goal_check),
            )
            if not goal_check.ok:
                repair_prompt = build_goal_prompt(
                    relative_file=relative.as_posix(),
                    task=task,
                    retrieval=retrieval_prompt_block(initial_retrieval),
                ) + (
                    "\n\nThe previous declaration failed Lean elaboration. "
                    "Return a corrected declaration using these exact diagnostics:\n"
                    + goal_check.output
                    + "\n\nPrevious declaration:\n"
                    + formal_goal["declaration"]
                )
                with timings.measure("formalize_goal_repair_api"):
                    formal_goal = validate_formal_goal(
                        agent_runtime.invoke(
                            role="formalizer",
                            phase="formalize_goal_repair",
                            output_type="json",
                            config=plan_config,
                            system_prompt=GOAL_SYSTEM_PROMPT,
                            user_prompt=repair_prompt,
                            temp_dir=store.paths.temp,
                            context={"target_file": relative.as_posix(), "repair": True},
                        )
                    )
                write_goal_probe(formal_goal)
                with timings.measure("formalize_goal_repair_lean_check"):
                    goal_check = lean_checker(
                        project, goal_probe, lean_timeout_seconds, lake_executable
                    )
            formal_goal["validated"] = goal_check.ok
            formal_goal["check"] = _check_to_json(goal_check)
            atomic_write_json(store.paths.root / "formal-goal-check.json", _check_to_json(goal_check))
            goal_terms = list(preflight_terms)
            for term in formal_goal["search_terms"]:
                if term not in goal_terms:
                    goal_terms.append(term)
            for term in source_search_terms(formal_goal["declaration"], task):
                if term not in goal_terms:
                    goal_terms.append(term)
            with timings.measure("planning_retrieval"):
                planning_retrieval = _safe_retrieval(
                    project,
                    diagnostics=goal_check.output if not goal_check.ok else initial_check.output,
                    requested_terms=goal_terms,
                    process_control=process_control,
                )
            atomic_write_json(store.paths.root / "planning-retrieval.json", planning_retrieval)
            atomic_write_json(goal_path, formal_goal)
            formal_goal_created = True
            store.update(formal_goal=formal_goal)
            store.event(
                "phase_completed",
                phase="formalize_goal",
                validated=goal_check.ok,
                declaration=formal_goal["declaration"],
            )
        except ApiError as exc:
            store.update(formal_goal=None, formal_goal_error=str(exc))
            store.event("phase_failed", phase="formalize_goal", error=str(exc))

    required_formal_declaration = (
        str(formal_goal["declaration"])
        if formal_goal and formal_goal.get("validated")
        else None
    )

    last_check = initial_check
    last_review: dict[str, Any] = {
        "verdict": "retry",
        "summary": "No proof attempt has run yet.",
        "failure_analysis": [],
        "next_actions": [],
        "search_terms": [],
    }
    if resume_safe_state is not None:
        safe_source, _, safe_sha, safe_checkpoint = resume_safe_state
        safe_check = initial_check
    else:
        safe_source = original_source
        safe_check = initial_check
        safe_sha = original_sha
        safe_checkpoint = None
    working_sha = preflight_current_sha if initial_check.ok else safe_sha
    terminal_broad_source: str | None = None
    terminal_broad_sha: str | None = None

    try:
        previous_manifest = store.read()
        saved_steps = list(previous_manifest.get("steps") or [])
        has_successful_checkpoint = any(
            isinstance(row, dict) and row.get("status") == "succeeded"
            for row in saved_steps
        )
        reuse_saved_plan = (
            resuming
            and store.paths.plan.is_file()
            and not (formal_goal_created and not has_successful_checkpoint)
            and resume_replan_reason is None
        )
        if reuse_saved_plan:
            plan = _apply_plan_contract(
                validate_plan(read_json(store.paths.plan)), formal_goal
            )
            atomic_write_json(store.paths.plan, plan)
            attempt_rows = list(previous_manifest.get("attempts") or [])
            step_rows = list(previous_manifest.get("steps") or [])
            if len(step_rows) != len(plan["steps"]):
                raise ValueError("Saved Plan and workflow step state disagree")
            for row in step_rows:
                if row.get("status") in {"running", "failed", "stopped"}:
                    row["status"] = "pending"
            attempt, orphaned_attempts = _resume_attempt_number(store, attempt_rows)
            latest = _latest_attempt_check(store, attempt_rows)
            if latest is not None:
                last_check = latest
            store.update(
                phase="prove",
                plan_summary=plan["summary"],
                steps=step_rows,
                attempts=attempt_rows,
            )
            store.event("plan_reused", steps=len(step_rows), attempts=attempt)
            if orphaned_attempts:
                store.event(
                    "partial_attempts_recovered",
                    attempts=orphaned_attempts,
                    next_attempt=attempt + 1,
                )
        else:
            if phase_callback is not None:
                phase_callback("planning", None)
            store.event("phase_started", phase="plan")
            plan_prompt = build_plan_prompt(
                relative_file=relative.as_posix(),
                task=task,
                source=target.read_text(encoding="utf-8"),
                diagnostics=initial_check.output,
                retrieval=retrieval_prompt_block(planning_retrieval),
                formal_goal=_formal_goal_prompt_block(formal_goal),
            )
            preferred_terms = natural_language_search_terms(task)
            for term in source_search_terms("", task):
                if term not in preferred_terms:
                    preferred_terms.append(term)
            exact_hits = _exact_theorem_hits(planning_retrieval, preferred_terms)
            if exact_hits:
                plan = _direct_proof_plan(exact_hits, formal_goal)
                store.event(
                    "direct_plan_selected",
                    declarations=[
                        str(row.get("name") or row.get("query")) for row in exact_hits
                    ],
                )
            else:
                try:
                    with timings.measure("plan_api"):
                        plan = _apply_plan_contract(
                            validate_plan(
                                agent_runtime.invoke(
                                    role="planner",
                                    phase="plan",
                                    output_type="json",
                                    config=plan_config,
                                    system_prompt=PLAN_SYSTEM_PROMPT,
                                    user_prompt=plan_prompt,
                                    temp_dir=store.paths.temp,
                                    context={"target_file": relative.as_posix()},
                                )
                            ),
                            formal_goal,
                        )
                except ApiError as exc:
                    fallback_terms = natural_language_search_terms(task)
                    plan = _apply_plan_contract(
                        validate_plan(
                            {
                                "summary": "Deterministic one-step fallback because Planner output was invalid.",
                                "steps": [
                                    {
                                        "id": "step-1",
                                        "goal": "Produce the complete requested Lean theorem and make the file compile.",
                                        "success_criteria": "The complete file passes Lean and source audit.",
                                        "search_terms": fallback_terms,
                                        "required_declarations": [],
                                    }
                                ],
                                "preserve": [],
                                "risks": [str(exc)],
                            }
                        ),
                        formal_goal,
                    )
                    plan["planner_error"] = str(exc)
            atomic_write_json(store.paths.plan, plan)
            store.update(phase="prove", plan_summary=plan["summary"])
            store.event("phase_completed", phase="plan", summary=plan["summary"])
            attempt_rows = (
                list(previous_manifest.get("attempts") or []) if resuming else []
            )
            step_rows = [
                {
                    "index": index,
                    "id": step["id"],
                    "goal": step["goal"],
                    "success_criteria": step["success_criteria"],
                    "status": "pending",
                    "attempts": [],
                    "checkpoint": None,
                }
                for index, step in enumerate(plan["steps"], 1)
            ]
            store.update(steps=step_rows)
            attempt, orphaned_attempts = _resume_attempt_number(store, attempt_rows)
            if orphaned_attempts:
                store.event(
                    "partial_attempts_recovered",
                    attempts=orphaned_attempts,
                    next_attempt=attempt + 1,
                )
            if resuming and formal_goal_created:
                store.event(
                    "plan_replaced_for_formal_goal",
                    attempts=attempt,
                    declaration=(formal_goal or {}).get("declaration"),
                )
            elif resuming and resume_replan_reason:
                store.event(
                    "plan_replaced_on_resume",
                    reason=resume_replan_reason,
                    attempts=attempt,
                )
        workflow_stopped = False
        api_failure_rows = list(previous_manifest.get("api_failures") or [])
        max_consecutive_prover_api_failures = max(
            2, int(prove_config.api_timeout_retries) + 1
        )

        for step_index, active_step in enumerate(plan["steps"], 1):
            step_row = step_rows[step_index - 1]
            if step_row.get("status") == "succeeded":
                continue
            if attempt >= max_attempts:
                break
            step_row["status"] = "running"
            store.update(
                phase="prove",
                current_step={
                    "index": step_index,
                    "id": active_step["id"],
                    "goal": active_step["goal"],
                },
                steps=step_rows,
            )
            store.event(
                "plan_step_started",
                step_index=step_index,
                step_id=active_step["id"],
            )
            prior_attempts = list(step_row.get("attempts") or [])
            prior_global_review = (
                previous_manifest.get("final_audit", {}).get("review")
                if isinstance(previous_manifest.get("final_audit"), dict)
                else None
            )
            if step_row.get("rejected_checkpoint") and isinstance(prior_global_review, dict):
                last_review = prior_global_review
            elif prior_attempts:
                review_path = store.paths.reviews / f"{int(prior_attempts[-1]):03d}.json"
                last_review = read_json(review_path) if review_path.is_file() else last_review
            else:
                last_review = {
                    "verdict": "retry",
                    "summary": "No candidate has run for this Plan step.",
                    "failure_analysis": [],
                    "next_actions": [],
                    "search_terms": [],
                }
            step_completed = False
            step_attempt = len(prior_attempts)
            consecutive_prover_api_failures = 0
            is_final_plan_step = step_index == len(plan["steps"])
            step_required_declaration = (
                required_formal_declaration if is_final_plan_step else None
            )
            step_required_names = list(
                dict.fromkeys(
                    name
                    for completed_or_active in plan["steps"][:step_index]
                    for name in completed_or_active.get("required_declarations", [])
                )
            )

            while attempt < max_attempts and step_attempt < max_attempts_per_step:
                candidate_attempt = attempt + 1
                candidate_step_attempt = step_attempt + 1
                store.event(
                    "phase_started",
                    phase="prove",
                    attempt=candidate_attempt,
                    step_index=step_index,
                    step_id=active_step["id"],
                )
                if phase_callback is not None:
                    phase_callback("proving", candidate_attempt)

                requested_terms = natural_language_search_terms(task)
                for term in active_step.get("search_terms", []):
                    if term not in requested_terms:
                        requested_terms.append(term)
                if formal_goal:
                    for term in formal_goal.get("search_terms", []):
                        if term not in requested_terms:
                            requested_terms.append(term)
                for term in last_review.get("search_terms", []):
                    if term not in requested_terms:
                        requested_terms.append(term)
                for term in source_search_terms(target.read_text(encoding="utf-8"), task):
                    if term not in requested_terms:
                        requested_terms.append(term)
                with timings.measure("attempt_retrieval", candidate_attempt):
                    retrieval = _safe_retrieval(
                        project,
                        diagnostics=last_check.output,
                        requested_terms=requested_terms,
                        process_control=process_control,
                    )

                current_source = target.read_text(encoding="utf-8")
                completed_steps = [
                    {"id": row["id"], "goal": row["goal"], "checkpoint": row["checkpoint"]}
                    for row in step_rows
                    if row["status"] == "succeeded"
                ]
                active_step_prompt = dict(active_step)
                active_step_prompt["formal_goal_required_now"] = is_final_plan_step
                active_step_json = json.dumps(
                    active_step_prompt, ensure_ascii=False, indent=2
                )
                prover_prompt = build_user_prompt(
                    relative_file=relative.as_posix(),
                    task=task,
                    source=current_source,
                    diagnostics=last_check.output,
                    attempt=candidate_attempt,
                    plan=json.dumps(plan, ensure_ascii=False, indent=2),
                    retrieval=retrieval_prompt_block(retrieval),
                    review_guidance=json.dumps(last_review, ensure_ascii=False, indent=2),
                    active_step=active_step_json,
                    completed_steps=json.dumps(
                        completed_steps, ensure_ascii=False, indent=2
                    ),
                    formal_goal=_formal_goal_prompt_block(formal_goal),
                    import_policy=effective_import_policy,
                )
                prover_error: ApiError | None = None
                try:
                    with timings.measure("prove_api", candidate_attempt):
                        candidate_value = agent_runtime.invoke(
                            role="prover",
                            phase="prove",
                            output_type="lean_file",
                            config=prove_config,
                            system_prompt=PROVER_SYSTEM_PROMPT,
                            user_prompt=prover_prompt,
                            temp_dir=store.paths.temp,
                            attempt=candidate_attempt,
                            step_id=str(active_step["id"]),
                            context={
                                "step_index": step_index,
                                "step_attempt": candidate_step_attempt,
                                "target_file": relative.as_posix(),
                            },
                        )
                        if not isinstance(candidate_value, str):
                            raise ApiError("Prover Agent did not return Lean source text")
                        candidate = candidate_value
                except ApiError as exc:
                    prover_error = exc
                if prover_error is not None:
                    consecutive_prover_api_failures += 1
                    failure_root = store.paths.root / "api-failures"
                    failure_root.mkdir(parents=True, exist_ok=True)
                    occupied_failures = [
                        int(path.name)
                        for path in failure_root.iterdir()
                        if path.is_dir() and path.name.isdigit()
                    ]
                    failure_number = max(
                        [
                            int(row.get("failure") or 0)
                            for row in api_failure_rows
                            if isinstance(row, dict)
                        ]
                        + occupied_failures,
                        default=0,
                    ) + 1
                    failure_dir = failure_root / f"{failure_number:03d}"
                    failure_dir.mkdir(exist_ok=False)
                    atomic_write_json(failure_dir / "retrieval.json", retrieval)
                    failure_row = {
                        "failure": failure_number,
                        "step_index": step_index,
                        "step_id": active_step["id"],
                        "candidate_attempt": candidate_attempt,
                        "error": str(prover_error),
                    }
                    atomic_write_json(failure_dir / "error.json", failure_row)
                    api_failure_rows.append(failure_row)
                    store.update(api_failures=api_failure_rows, phase="prove")
                    store.event(
                        "prover_api_failed",
                        step_index=step_index,
                        step_id=active_step["id"],
                        candidate_attempt=candidate_attempt,
                        consecutive=consecutive_prover_api_failures,
                        error=str(prover_error),
                    )
                    if (
                        consecutive_prover_api_failures
                        >= max_consecutive_prover_api_failures
                    ):
                        raise ApiError(
                            "Prover API failed repeatedly without producing a Lean candidate: "
                            + str(prover_error)
                        ) from prover_error
                    continue

                consecutive_prover_api_failures = 0
                attempt = candidate_attempt
                step_attempt = candidate_step_attempt
                attempt_dir = store.paths.attempt_dir(attempt)
                attempt_dir.mkdir(parents=True, exist_ok=False)
                atomic_write_json(attempt_dir / "retrieval.json", retrieval)
                candidate, deterministic_import_repair = repair_invalid_mathlib_imports(
                    project, candidate
                )
                if effective_import_policy in {"proof-first", "broad"}:
                    candidate = ensure_broad_mathlib_import(candidate)
                # Search names introduced by the candidate before making an
                # import decision. Under proof-first, broad Mathlib is checked
                # first and minimized only after the proof already passes.
                if has_broad_import(candidate):
                    with timings.measure("candidate_retrieval", attempt):
                        candidate_retrieval = _safe_retrieval(
                            project,
                            diagnostics=last_check.output,
                            requested_terms=[
                                *requested_terms,
                                *[
                                    term
                                    for term in source_search_terms(candidate, task)
                                    if term not in requested_terms
                                ],
                            ],
                            process_control=process_control,
                        )
                    if effective_import_policy == "precise":
                        candidate, candidate_import_optimization = optimize_broad_imports(
                            candidate, candidate_retrieval
                        )
                    else:
                        candidate_import_optimization = {
                            "changed": False,
                            "reason": "broad_import_allowed_for_proof",
                            "policy": effective_import_policy,
                        }
                else:
                    candidate_retrieval = retrieval
                    candidate_import_optimization = {
                        "changed": False,
                        "reason": "candidate_has_no_broad_import",
                    }
                retrieval = candidate_retrieval
                source_audit = audit_source(
                    original_source,
                    candidate,
                    protect_existing_statements=protect_existing_statements,
                    protected_declarations=protected_declarations,
                    required_declaration=step_required_declaration,
                    required_declaration_names=step_required_names,
                )
                if phase_callback is not None:
                    phase_callback("lean_checking", attempt)
                candidate_import_validation = validate_mathlib_imports(project, candidate)
                if prover_error is not None:
                    last_check = LeanCheck(
                        False,
                        1,
                        f"Prover API output error: {prover_error}",
                        ("prover-api", relative.as_posix()),
                    )
                elif not source_audit["ok"]:
                    last_check = LeanCheck(
                        False,
                        1,
                        "Source audit failed:\n" + "\n".join(source_audit["violations"]),
                        ("source-audit", relative.as_posix()),
                    )
                elif not candidate_import_validation["ok"]:
                    last_check = LeanCheck(
                        False,
                        1,
                        import_validation_diagnostics(candidate_import_validation),
                        ("import-validation", relative.as_posix()),
                    )
                else:
                    atomic_write_text(target, candidate)
                    transaction_touched = True
                    try:
                        with timings.measure("lean_check", attempt):
                            last_check = lean_checker(
                                project, target, lean_timeout_seconds, lake_executable
                            )
                    finally:
                        atomic_write_text(target, current_source)

                candidate_sha = sha256_text(candidate)
                atomic_write_text(attempt_dir / "candidate.lean", candidate)
                atomic_write_json(attempt_dir / "audit.json", source_audit)
                candidate_import_optimization["probe_ok"] = last_check.ok
                candidate_import_optimization["probe_returncode"] = last_check.returncode
                candidate_import_optimization["policy"] = effective_import_policy
                if not source_audit["ok"]:
                    candidate_import_optimization["probe_skipped"] = "source_audit_failed"
                candidate_terms = list(requested_terms)
                for term in source_search_terms(candidate, task):
                    if term not in candidate_terms:
                        candidate_terms.append(term)
                with timings.measure("post_check_retrieval", attempt):
                    retrieval = _safe_retrieval(
                        project,
                        diagnostics=last_check.output,
                        requested_terms=candidate_terms,
                        process_control=process_control,
                    )
                retrieval["import_optimization"] = candidate_import_optimization
                retrieval["import_validation"] = candidate_import_validation
                retrieval["deterministic_import_repair"] = deterministic_import_repair
                atomic_write_json(attempt_dir / "retrieval.json", retrieval)
                atomic_write_json(attempt_dir / "check.json", _check_to_json(last_check))
                store.event(
                    "phase_completed",
                    phase="prove",
                    attempt=attempt,
                    step_index=step_index,
                    step_id=active_step["id"],
                    check_ok=last_check.ok,
                    candidate_sha256=candidate_sha,
                )

                store.update(phase="review")
                store.event("phase_started", phase="review", attempt=attempt)
                if phase_callback is not None:
                    phase_callback("reviewing", attempt)
                review_prompt = build_review_prompt(
                    relative_file=relative.as_posix(),
                    task=task,
                    attempt=attempt,
                    plan=json.dumps(plan, ensure_ascii=False, indent=2),
                    candidate=candidate,
                    check_ok=last_check.ok,
                    diagnostics=last_check.output,
                    retrieval=retrieval_prompt_block(retrieval),
                    active_step=active_step_json,
                )
                try:
                    with timings.measure("review_api", attempt):
                        last_review = validate_review(
                            agent_runtime.invoke(
                                role="reviewer",
                                phase="review",
                                output_type="json",
                                config=review_config,
                                system_prompt=REVIEW_SYSTEM_PROMPT,
                                user_prompt=review_prompt,
                                temp_dir=store.paths.temp,
                                attempt=attempt,
                                step_id=str(active_step["id"]),
                                context={
                                    "step_index": step_index,
                                    "check_ok": last_check.ok,
                                },
                            ),
                            last_check.ok,
                        )
                except ApiError as exc:
                    last_review = _fallback_review(last_check, str(exc))

                review_path = store.paths.reviews / f"{attempt:03d}.json"
                atomic_write_json(review_path, last_review)
                attempt_row = {
                    "attempt": attempt,
                    "step_index": step_index,
                    "step_id": active_step["id"],
                    "step_attempt": step_attempt,
                    "candidate_sha256": candidate_sha,
                    "check_ok": last_check.ok,
                    "returncode": last_check.returncode,
                    "review_verdict": last_review["verdict"],
                    "prover_error": str(prover_error) if prover_error else None,
                    "artifacts": {
                        "candidate": str(attempt_dir / "candidate.lean"),
                        "check": str(attempt_dir / "check.json"),
                        "retrieval": str(attempt_dir / "retrieval.json"),
                        "audit": str(attempt_dir / "audit.json"),
                        "review": str(review_path),
                    },
                }
                attempt_rows.append(attempt_row)
                step_row["attempts"].append(attempt)
                store.update(
                    attempts=attempt_rows,
                    steps=step_rows,
                    final_review=last_review,
                )
                store.event(
                    "phase_completed",
                    phase="review",
                    attempt=attempt,
                    step_index=step_index,
                    step_id=active_step["id"],
                    verdict=last_review["verdict"],
                )

                if last_check.ok and last_review["verdict"] == "accept":
                    atomic_write_text(target, candidate)
                    target_changed = True
                    checkpoint_dir = store.paths.checkpoint_dir(
                        step_index, active_step["id"]
                    )
                    checkpoint_dir.mkdir(parents=True, exist_ok=False)
                    atomic_write_text(checkpoint_dir / "source.lean", candidate)
                    atomic_write_json(
                        checkpoint_dir / "check.json", _check_to_json(last_check)
                    )
                    atomic_write_json(checkpoint_dir / "review.json", last_review)
                    atomic_write_json(checkpoint_dir / "retrieval.json", retrieval)
                    checkpoint_meta = {
                        "step_index": step_index,
                        "step_id": active_step["id"],
                        "goal": active_step["goal"],
                        "success_criteria": active_step["success_criteria"],
                        "attempt": attempt,
                        "candidate_sha256": candidate_sha,
                    }
                    atomic_write_json(checkpoint_dir / "checkpoint.json", checkpoint_meta)
                    step_row["status"] = "succeeded"
                    step_row["checkpoint"] = str(checkpoint_dir)
                    safe_source = candidate
                    safe_check = last_check
                    safe_sha = candidate_sha
                    safe_checkpoint = str(checkpoint_dir)
                    working_sha = candidate_sha
                    step_completed = True
                    store.update(steps=step_rows, current_sha256=candidate_sha)
                    store.event(
                        "plan_step_succeeded",
                        step_index=step_index,
                        step_id=active_step["id"],
                        attempt=attempt,
                        checkpoint=str(checkpoint_dir),
                    )
                    break
                if last_review["verdict"] == "stop":
                    step_row["status"] = "stopped"
                    workflow_stopped = True
                    break
                store.update(phase="prove")

            if not step_completed:
                if step_row["status"] == "running":
                    step_row["status"] = "failed"
                step_row["budget_exhausted"] = (
                    "global"
                    if attempt >= max_attempts
                    else "step"
                    if step_attempt >= max_attempts_per_step
                    else None
                )
                store.update(steps=step_rows)
                break

        all_steps_succeeded = bool(step_rows) and all(
            row["status"] == "succeeded" for row in step_rows
        )
        if all_steps_succeeded:
            broad_source = target.read_text(encoding="utf-8")
            broad_source_sha = sha256_text(broad_source)
            if broad_source_sha != working_sha:
                raise OSError(
                    "Final broad source does not match the latest verified working source"
                )
            terminal_broad_source = broad_source
            terminal_broad_sha = broad_source_sha
            selected_source = broad_source
            selected_source_kind = (
                "broad" if has_broad_import(broad_source) else "precise"
            )
            final_import_reduction: dict[str, Any] = {
                "attempted": effective_import_policy == "proof-first",
                "changed": False,
                "effective_import_policy": effective_import_policy,
                "broad_source_sha256": broad_source_sha,
                "candidate_source_sha256": None,
                "selected_source_sha256": broad_source_sha,
                "retrieval": {
                    "queries": [],
                    "import_suggestions": [],
                },
                "selected_modules": [],
                "added_modules": [],
                "optimization": {},
                "source_audit": None,
                "import_validation": None,
                "lean_probe": {
                    "ok": None,
                    "returncode": None,
                    "diagnostics": "",
                    "command": [],
                },
                "selected_source": selected_source_kind,
                "fallback_reason": (
                    None
                    if effective_import_policy == "proof-first"
                    else "policy_not_applicable"
                ),
            }
            if effective_import_policy == "proof-first":
                try:
                    reduction_terms = _plan_search_terms(plan)
                    for term in source_search_terms(broad_source, task):
                        if term not in reduction_terms:
                            reduction_terms.append(term)
                    with timings.measure("final_import_reduction_retrieval"):
                        reduction_retrieval = _safe_retrieval(
                            project,
                            diagnostics="",
                            requested_terms=reduction_terms,
                            process_control=process_control,
                        )
                    retrieval_queries = reduction_retrieval.get("queries", [])
                    retrieval_suggestions = reduction_retrieval.get(
                        "import_suggestions", []
                    )
                    final_import_reduction["retrieval"] = {
                        "queries": (
                            list(retrieval_queries)
                            if isinstance(retrieval_queries, list)
                            else []
                        ),
                        "import_suggestions": (
                            list(retrieval_suggestions)
                            if isinstance(retrieval_suggestions, list)
                            else []
                        ),
                    }
                    reduction_candidate, reduction_optimization = (
                        optimize_broad_imports(broad_source, reduction_retrieval)
                    )
                    final_import_reduction["optimization"] = reduction_optimization
                    final_import_reduction["selected_modules"] = list(
                        reduction_optimization.get("selected_modules") or []
                    )
                    final_import_reduction["added_modules"] = list(
                        reduction_optimization.get("added_modules") or []
                    )
                    if not reduction_optimization.get("changed"):
                        final_import_reduction["fallback_reason"] = str(
                            reduction_optimization.get("reason") or "no_candidate"
                        )
                    else:
                        reduction_candidate_sha = sha256_text(reduction_candidate)
                        final_import_reduction[
                            "candidate_source_sha256"
                        ] = reduction_candidate_sha
                        reduction_source_audit = audit_source(
                            original_source,
                            reduction_candidate,
                            final=True,
                            protect_existing_statements=protect_existing_statements,
                            protected_declarations=protected_declarations,
                            required_declaration=required_formal_declaration,
                            required_declaration_names=list(
                                dict.fromkeys(
                                    name
                                    for step in plan["steps"]
                                    for name in step.get("required_declarations", [])
                                )
                            ),
                        )
                        final_import_reduction["source_audit"] = reduction_source_audit
                        if not reduction_source_audit["ok"]:
                            final_import_reduction[
                                "fallback_reason"
                            ] = "source_audit_failed"
                        else:
                            reduction_import_validation = validate_mathlib_imports(
                                project, reduction_candidate
                            )
                            final_import_reduction[
                                "import_validation"
                            ] = reduction_import_validation
                            if not reduction_import_validation["ok"]:
                                final_import_reduction[
                                    "fallback_reason"
                                ] = "import_validation_failed"
                            else:
                                atomic_write_text(target, reduction_candidate)
                                transaction_touched = True
                                try:
                                    with timings.measure(
                                        "final_import_reduction_lean_check"
                                    ):
                                        reduction_check = lean_checker(
                                            project,
                                            target,
                                            lean_timeout_seconds,
                                            lake_executable,
                                        )
                                finally:
                                    _restore_final_broad_source(
                                        target, broad_source, broad_source_sha
                                    )
                                final_import_reduction["lean_probe"] = {
                                    "ok": reduction_check.ok,
                                    "returncode": reduction_check.returncode,
                                    "diagnostics": reduction_check.output,
                                    "command": list(reduction_check.command),
                                }
                                if reduction_check.ok:
                                    selected_source = reduction_candidate
                                    final_import_reduction["changed"] = True
                                    final_import_reduction[
                                        "selected_source_sha256"
                                    ] = reduction_candidate_sha
                                    final_import_reduction[
                                        "selected_source"
                                    ] = "precise"
                                    final_import_reduction["fallback_reason"] = None
                                else:
                                    final_import_reduction[
                                        "fallback_reason"
                                    ] = "lean_probe_failed"
                except ProcessCancelled:
                    _restore_final_broad_source(
                        target, broad_source, broad_source_sha
                    )
                    final_import_reduction["changed"] = False
                    final_import_reduction[
                        "selected_source_sha256"
                    ] = broad_source_sha
                    final_import_reduction["selected_source"] = "broad"
                    final_import_reduction["fallback_reason"] = "cancelled"
                    _write_final_import_reduction(store, final_import_reduction)
                    raise
                except _FinalImportReductionRestoreError:
                    raise
                except Exception:
                    _restore_final_broad_source(
                        target, broad_source, broad_source_sha
                    )
                    selected_source = broad_source
                    final_import_reduction["changed"] = False
                    final_import_reduction[
                        "selected_source_sha256"
                    ] = broad_source_sha
                    final_import_reduction["selected_source"] = "broad"
                    final_import_reduction[
                        "fallback_reason"
                    ] = "reduction_exception"

            atomic_write_text(target, selected_source)
            target_changed = target_changed or selected_source != broad_source
            try:
                _write_final_import_reduction(store, final_import_reduction)
            except Exception:
                _restore_final_broad_source(
                    target, broad_source, broad_source_sha
                )
                raise
            store.update(phase="audit", current_step=None)
            store.event("phase_started", phase="global_audit")
            if phase_callback is not None:
                phase_callback("auditing", None)
            final_source = target.read_text(encoding="utf-8")
            final_source_audit = audit_source(
                original_source,
                final_source,
                final=True,
                protect_existing_statements=protect_existing_statements,
                protected_declarations=protected_declarations,
                required_declaration=required_formal_declaration,
                required_declaration_names=list(
                    dict.fromkeys(
                        name
                        for step in plan["steps"]
                        for name in step.get("required_declarations", [])
                    )
                ),
            )
            with timings.measure("final_lean_check"):
                final_check = lean_checker(
                    project, target, lean_timeout_seconds, lake_executable
                )
            final_retrieval = _safe_retrieval(
                project,
                diagnostics=final_check.output,
                requested_terms=[
                    *_plan_search_terms(plan),
                    *[
                        term
                        for term in source_search_terms(final_source, task)
                        if term not in _plan_search_terms(plan)
                    ],
                ],
                process_control=process_control,
            )
            audit_ok = final_check.ok and bool(final_source_audit["ok"])
            audit_diagnostics = final_check.output
            if not final_source_audit["ok"]:
                audit_diagnostics = (
                    audit_diagnostics
                    + "\nGlobal source audit failed:\n"
                    + "\n".join(final_source_audit["violations"])
                ).strip()
            global_step = json.dumps(
                {
                    "id": "global-final-audit",
                    "goal": "Verify the complete file satisfies the user's original task and every Plan step together.",
                    "success_criteria": "The full Lean file passes, all protected declarations remain valid, no forbidden proof placeholders remain, and the final requested theorem is present with the intended statement.",
                },
                ensure_ascii=False,
                indent=2,
            )
            audit_prompt = build_review_prompt(
                relative_file=relative.as_posix(),
                task=task,
                attempt=attempt,
                plan=json.dumps(plan, ensure_ascii=False, indent=2),
                candidate=final_source,
                check_ok=audit_ok,
                diagnostics=audit_diagnostics,
                retrieval=retrieval_prompt_block(final_retrieval),
                active_step=global_step,
            )
            try:
                with timings.measure("final_review_api"):
                    global_review = validate_review(
                        agent_runtime.invoke(
                            role="auditor",
                            phase="global_audit",
                            output_type="json",
                            config=review_config,
                            system_prompt=REVIEW_SYSTEM_PROMPT,
                            user_prompt=audit_prompt,
                            temp_dir=store.paths.temp,
                            attempt=attempt,
                            step_id="global-final-audit",
                            context={
                                "check_ok": final_check.ok,
                                "source_audit_ok": final_source_audit["ok"],
                            },
                        ),
                        audit_ok,
                    )
            except ApiError as exc:
                global_review = _fallback_final_audit_review(
                    final_check, bool(final_source_audit["ok"]), str(exc)
                )
            final_audit = {
                "ok": audit_ok and global_review["verdict"] == "accept",
                "source_sha256": sha256_text(final_source),
                "source_audit": final_source_audit,
                "lean_check": _check_to_json(final_check),
                "review": global_review,
            }
            atomic_write_json(store.paths.root / "final-audit.json", final_audit)
            store.update(final_audit=final_audit, final_review=global_review)
            store.event(
                "phase_completed",
                phase="global_audit",
                ok=final_audit["ok"],
                verdict=global_review["verdict"],
            )
            if final_audit["ok"]:
                timing_summary = timings.finish("succeeded")
                store.update(
                    status="succeeded",
                    phase="complete",
                    current_step=None,
                    current_sha256=final_audit["source_sha256"],
                    completed_attempt=attempt,
                    timings=timing_summary,
                )
                store.event("workflow_succeeded", attempt=attempt)
                return WorkflowResult(
                    True, store.paths.run_id, attempt, store.paths.root, final_check, False
                )

            last_step = step_rows[-1]
            last_step["status"] = "failed"
            last_step["rejected_checkpoint"] = last_step.get("checkpoint")
            last_step["checkpoint"] = None
            store.update(steps=step_rows)
            safe_source, safe_check, safe_sha, safe_checkpoint = _checkpoint_state(
                store,
                {"steps": step_rows},
                original_source,
                initial_check,
            )
            atomic_write_text(target, safe_source)
            last_check = final_check

        restored = not keep_failed or all_steps_succeeded
        failed_check = last_check
        if restored:
            atomic_write_text(target, safe_source)
            restored_sha = sha256_text(target.read_text(encoding="utf-8"))
            if restored_sha != safe_sha:
                raise OSError("Restored Lean file does not match the latest safe checkpoint")
            restored_check = {
                **_check_to_json(safe_check),
                "reused_safe_check": True,
                "reused_initial_check": safe_checkpoint is None,
                "restored_sha256": restored_sha,
                "checkpoint": safe_checkpoint,
            }
            atomic_write_json(
                store.paths.root / "restore-check.json", restored_check
            )
            store.event(
                "safe_checkpoint_restored",
                checkpoint=safe_checkpoint,
                reused_safe_check=True,
            )
            current_sha = safe_sha
        else:
            if attempt_rows:
                failed_candidate = store.paths.attempt_dir(
                    int(attempt_rows[-1]["attempt"])
                ) / "candidate.lean"
                if failed_candidate.is_file():
                    atomic_write_text(
                        target, failed_candidate.read_text(encoding="utf-8")
                    )
            current_sha = sha256_text(target.read_text(encoding="utf-8"))
        timing_summary = timings.finish("failed")
        store.update(
            status="failed",
            phase="complete",
            current_sha256=current_sha,
            restored=restored,
            restored_to_checkpoint=safe_checkpoint,
            error=(
                "Global final audit did not accept the complete proof."
                if all_steps_succeeded
                else "Lean did not pass within the configured candidate budgets."
            ),
            timings=timing_summary,
        )
        store.event(
            "workflow_failed",
            restored=restored,
            checkpoint=safe_checkpoint,
            stopped=workflow_stopped,
        )
        return WorkflowResult(
            False,
            store.paths.run_id,
            len(store.read().get("attempts", [])),
            store.paths.root,
            failed_check,
            restored,
        )
    except ProcessCancelled as exc:
        if terminal_broad_source is not None and terminal_broad_sha is not None:
            _restore_final_broad_source(
                target, terminal_broad_source, terminal_broad_sha
            )
            restored = True
        elif target_changed and not keep_failed:
            atomic_write_text(target, safe_source)
            restored = True
        else:
            restored = False
        current_sha = sha256_text(target.read_text(encoding="utf-8"))
        restored_checkpoint = (
            safe_checkpoint
            if restored and current_sha == safe_sha
            else None
        )
        timing_summary = timings.finish("cancelled")
        store.update(
            status="cancelled",
            phase="complete",
            current_sha256=current_sha,
            restored=restored,
            restored_to_checkpoint=restored_checkpoint,
            error=str(exc),
            timings=timing_summary,
        )
        store.event("workflow_cancelled", restored=restored)
        raise
    except Exception as exc:
        if target_changed and not keep_failed:
            atomic_write_text(target, safe_source)
        current_sha = sha256_text(target.read_text(encoding="utf-8"))
        restored = (target_changed or transaction_touched) and not keep_failed
        restored_checkpoint = (
            safe_checkpoint
            if restored and current_sha == safe_sha
            else None
        )
        timing_summary = timings.finish("failed")
        store.update(
            status="failed",
            phase="complete",
            current_sha256=current_sha,
            restored=restored,
            restored_to_checkpoint=restored_checkpoint,
            error=f"{type(exc).__name__}: {exc}",
            timings=timing_summary,
        )
        store.event("workflow_crashed", error=f"{type(exc).__name__}: {exc}")
        raise
    build_goal_prompt,
