from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lean_loop.jsonutil import (
    append_jsonl,
    atomic_write_json,
    read_json,
    run_id_now,
    utc_now,
)


SCHEMA_VERSION = 2
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class WorkflowPaths:
    project: Path
    run_id: str

    @property
    def state_root(self) -> Path:
        return self.project / ".lean-agent"

    @property
    def root(self) -> Path:
        return self.state_root / "workflows" / self.run_id

    @property
    def manifest(self) -> Path:
        return self.root / "run.json"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def plan(self) -> Path:
        return self.root / "plan.json"

    @property
    def reviews(self) -> Path:
        return self.root / "reviews"

    @property
    def attempts(self) -> Path:
        return self.root / "attempts"

    @property
    def original(self) -> Path:
        return self.root / "original.lean"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints"

    @property
    def temp(self) -> Path:
        return self.root / "tmp"

    @property
    def timings(self) -> Path:
        return self.root / "timings.json"

    def attempt_dir(self, attempt: int) -> Path:
        return self.attempts / f"{attempt:03d}"

    def checkpoint_dir(
        self,
        step_index: int,
        step_id: str,
        *,
        attempt: int,
        generation: int,
    ) -> Path:
        safe_id = "".join(
            character if character.isalnum() or character in {"-", "_"} else "-"
            for character in step_id
        ).strip("-") or f"step-{step_index}"
        return self.checkpoints / (
            f"g{generation:03d}-s{step_index:03d}-{safe_id}-a{attempt:03d}"
        )


class WorkflowStore:
    def __init__(self, paths: WorkflowPaths) -> None:
        self.paths = paths

    @classmethod
    def create(
        cls,
        *,
        project: Path,
        target_file: str,
        task: str,
        settings: dict[str, Any],
        original_sha256: str,
    ) -> "WorkflowStore":
        paths = WorkflowPaths(project=project, run_id=run_id_now())
        paths.root.mkdir(parents=True, exist_ok=False)
        now = utc_now()
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": paths.run_id,
            "status": "running",
            "phase": "plan",
            "project": str(project),
            "target_file": target_file,
            "task": task,
            "created_at": now,
            "updated_at": now,
            "original_sha256": original_sha256,
            "current_sha256": original_sha256,
            "settings": settings,
            "attempts": [],
            "api_failures": [],
            "steps": [],
            "current_step": None,
            "plan_summary": None,
            "final_review": None,
            "final_audit": None,
            "resume_count": 0,
            "timings": None,
            "explanation_status": "not_requested",
            "explanation": None,
            "explanation_error": None,
            "error": None,
        }
        atomic_write_json(paths.manifest, manifest)
        append_jsonl(paths.events, {"event": "workflow_created", "phase": "plan"})
        return cls(paths)

    @classmethod
    def open(cls, project: Path, run_id: str) -> "WorkflowStore":
        paths = WorkflowPaths(project=project, run_id=run_id)
        if not paths.manifest.is_file():
            raise FileNotFoundError(f"Workflow not found: {run_id}")
        return cls(paths)

    def read(self) -> dict[str, Any]:
        return read_json(self.paths.manifest)

    def update(self, **changes: Any) -> dict[str, Any]:
        manifest = self.read()
        if manifest.get("status") in TERMINAL_STATUSES and changes.get("status") == "running":
            raise ValueError("Cannot move a terminal workflow back to running")
        manifest.update(changes)
        manifest["updated_at"] = utc_now()
        atomic_write_json(self.paths.manifest, manifest)
        return manifest

    def resume(self, **changes: Any) -> dict[str, Any]:
        manifest = self.read()
        if manifest.get("status") == "succeeded":
            raise ValueError("A succeeded workflow cannot be resumed")
        manifest.update(changes)
        manifest["status"] = "running"
        manifest["error"] = None
        manifest["resume_count"] = int(manifest.get("resume_count", 0)) + 1
        manifest["updated_at"] = utc_now()
        atomic_write_json(self.paths.manifest, manifest)
        self.event("workflow_resumed", resume_count=manifest["resume_count"])
        return manifest

    def event(self, event: str, **fields: Any) -> None:
        append_jsonl(self.paths.events, {"event": event, **fields})


def list_workflows(project: Path) -> list[dict[str, Any]]:
    root = project / ".lean-agent" / "workflows"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for manifest in root.glob("*/run.json"):
        try:
            rows.append(read_json(manifest))
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: str(row.get("created_at", "")), reverse=True)
