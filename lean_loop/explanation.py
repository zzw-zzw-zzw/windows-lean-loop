from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lean_loop.agent_protocol import AgentRuntime, DirectModelBackend
from lean_loop.api import ApiError, call_model_json
from lean_loop.config import ApiConfig
from lean_loop.jsonutil import atomic_write_json, atomic_write_text, read_json, sha256_text
from lean_loop.prompts import EXPLANATION_SYSTEM_PROMPT, build_explanation_prompt
from lean_loop.process_control import ProcessCancelled
from lean_loop.state import WorkflowStore


JsonModelCall = Callable[[ApiConfig, str, str, Path], dict[str, Any]]


@dataclass(frozen=True)
class ExplanationResult:
    ok: bool
    run_id: str
    json_path: Path | None
    markdown_path: Path | None
    error: str | None = None


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(f"Explanation JSON requires a non-empty {field!r}")
    return value.strip()


def _string_list(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ApiError(f"Explanation JSON field {field!r} must be a list of strings")
    result = [item.strip() for item in value if item.strip()]
    if required and not result:
        raise ApiError(f"Explanation JSON requires a non-empty {field!r}")
    return result


def validate_explanation(value: dict[str, Any]) -> dict[str, Any]:
    correspondence = value.get("lean_correspondence")
    if not isinstance(correspondence, list):
        raise ApiError("Explanation JSON field 'lean_correspondence' must be a list")
    normalized_correspondence: list[dict[str, str]] = []
    for index, row in enumerate(correspondence, 1):
        if not isinstance(row, dict):
            raise ApiError(f"lean_correspondence item {index} must be an object")
        normalized_correspondence.append(
            {
                "lean_fragment": _string(
                    row.get("lean_fragment"),
                    f"lean_correspondence[{index}].lean_fragment",
                ),
                "mathematical_meaning": _string(
                    row.get("mathematical_meaning"),
                    f"lean_correspondence[{index}].mathematical_meaning",
                ),
            }
        )
    return {
        "title": _string(value.get("title"), "title"),
        "statement": _string(value.get("statement"), "statement"),
        "proof_outline": _string_list(
            value.get("proof_outline"), "proof_outline", required=True
        ),
        "detailed_proof": _string(value.get("detailed_proof"), "detailed_proof"),
        "lean_correspondence": normalized_correspondence,
        "assumptions": _string_list(
            value.get("assumptions", []), "assumptions", required=False
        ),
    }


def render_explanation_markdown(explanation: dict[str, Any], language: str) -> str:
    chinese = language.lower().startswith("zh")
    labels = (
        ("定理陈述", "证明思路", "详细证明", "Lean 代码对应", "假设与范围")
        if chinese
        else ("Statement", "Proof Outline", "Detailed Proof", "Lean Correspondence", "Assumptions")
    )
    statement_label, outline_label, proof_label, lean_label, assumptions_label = labels
    lines = [f"# {explanation['title']}", "", f"## {statement_label}", "", explanation["statement"], ""]
    lines.extend([f"## {outline_label}", ""])
    lines.extend(f"{index}. {step}" for index, step in enumerate(explanation["proof_outline"], 1))
    lines.extend(["", f"## {proof_label}", "", explanation["detailed_proof"], ""])
    if explanation["lean_correspondence"]:
        lines.extend([f"## {lean_label}", ""])
        for row in explanation["lean_correspondence"]:
            lines.extend(
                [
                    f"- `{row['lean_fragment']}`",
                    "",
                    f"  {row['mathematical_meaning']}",
                    "",
                ]
            )
    if explanation["assumptions"]:
        lines.extend([f"## {assumptions_label}", ""])
        lines.extend(f"- {item}" for item in explanation["assumptions"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _successful_artifacts(store: WorkflowStore) -> tuple[dict[str, Any], str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = store.read()
    if manifest.get("status") != "succeeded":
        raise ValueError("Natural-language explanation requires a succeeded workflow")
    attempt = manifest.get("completed_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise ValueError("Succeeded workflow has no valid completed_attempt")
    attempt_dir = store.paths.attempt_dir(attempt)
    candidate_path = attempt_dir / "candidate.lean"
    check_path = attempt_dir / "check.json"
    if not candidate_path.is_file() or not check_path.is_file():
        raise FileNotFoundError("Succeeded workflow is missing its candidate or Lean check artifact")
    candidate = candidate_path.read_text(encoding="utf-8")
    check = read_json(check_path)
    if check.get("ok") is not True:
        raise ValueError("Archived completed attempt does not contain a successful Lean check")
    expected_sha = manifest.get("current_sha256")
    if isinstance(expected_sha, str) and expected_sha and sha256_text(candidate) != expected_sha:
        raise ValueError("Archived successful candidate does not match the workflow manifest hash")
    original = store.paths.original.read_text(encoding="utf-8")
    plan = read_json(store.paths.plan)
    review_path = store.paths.reviews / f"{attempt:03d}.json"
    review = read_json(review_path)
    return manifest, original, candidate, check, plan, review


def generate_workflow_explanation(
    *,
    project: Path,
    run_id: str,
    config: ApiConfig,
    language: str = "zh-CN",
    json_model_call: JsonModelCall = call_model_json,
) -> ExplanationResult:
    store = WorkflowStore.open(project, run_id)
    agent_runtime = AgentRuntime(
        workflow_root=store.paths.root,
        run_id=run_id,
        backend=DirectModelBackend(
            json_model_call=json_model_call,
            file_model_call=None,
        ),
    )
    manifest, original, candidate, check, plan, review = _successful_artifacts(store)
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            candidate.splitlines(keepends=True),
            fromfile="original.lean",
            tofile="candidate.lean",
        )
    )
    started = time.perf_counter()
    store.update(explanation_status="running", explanation_error=None)
    store.event("explanation_started", language=language, model=config.model)
    try:
        prompt = build_explanation_prompt(
            language=language,
            relative_file=str(manifest.get("target_file", "")),
            task=str(manifest.get("task", "")),
            original_source=original,
            candidate=candidate,
            source_diff=diff,
            plan=json.dumps(plan, ensure_ascii=False, indent=2),
            review=json.dumps(review, ensure_ascii=False, indent=2),
            check=json.dumps(check, ensure_ascii=False, indent=2),
        )
        explanation = validate_explanation(
            agent_runtime.invoke(
                role="explainer",
                phase="explanation",
                output_type="json",
                config=config,
                system_prompt=EXPLANATION_SYSTEM_PROMPT,
                user_prompt=prompt,
                temp_dir=store.paths.temp,
                attempt=int(manifest["completed_attempt"]),
                step_id="natural-language-explanation",
                context={"language": language},
            )
        )
        explanation["language"] = language
        explanation["source"] = {
            "run_id": run_id,
            "completed_attempt": manifest["completed_attempt"],
            "candidate_sha256": sha256_text(candidate),
            "lean_check_ok": True,
            "duration_seconds": round(time.perf_counter() - started, 6),
        }
        json_path = store.paths.root / "explanation.json"
        markdown_path = store.paths.root / "explanation.md"
        atomic_write_json(json_path, explanation)
        atomic_write_text(
            markdown_path, render_explanation_markdown(explanation, language)
        )
        store.update(
            explanation_status="succeeded",
            explanation_error=None,
            explanation={
                "language": language,
                "model": config.model,
                "json": str(json_path),
                "markdown": str(markdown_path),
                "duration_seconds": round(time.perf_counter() - started, 6),
            },
        )
        store.event("explanation_succeeded", language=language)
        return ExplanationResult(True, run_id, json_path, markdown_path)
    except ProcessCancelled:
        store.update(
            explanation_status="cancelled",
            explanation_error="Explanation was cancelled by the queue worker.",
            explanation_duration_seconds=round(time.perf_counter() - started, 6),
        )
        store.event("explanation_cancelled")
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        store.update(
            explanation_status="failed",
            explanation_error=error,
            explanation_duration_seconds=round(time.perf_counter() - started, 6),
        )
        store.event("explanation_failed", error=error)
        return ExplanationResult(False, run_id, None, None, error)
