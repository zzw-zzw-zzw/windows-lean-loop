from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lean_loop.api import ApiError, call_model
from lean_loop.config import ApiConfig
from lean_loop.lean import LeanCheck, check_lean
from lean_loop.prompts import build_user_prompt


@dataclass(frozen=True)
class RunResult:
    ok: bool
    attempts: int
    backup_path: Path
    final_check: LeanCheck
    restored: bool


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_jsonl(path: Path, event: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def _atomic_write(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".lean-loop.tmp")
    temp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def run_repair_loop(
    *,
    project: Path,
    target: Path,
    task: str,
    config: ApiConfig,
    max_attempts: int,
    lean_timeout_seconds: int,
    lake_executable: str,
    keep_failed: bool,
) -> RunResult:
    state_dir = project / ".lean-agent"
    run_id = _timestamp()
    relative = target.relative_to(project)
    backup_path = state_dir / "backups" / run_id / relative
    log_path = state_dir / "runs" / f"{run_id}.jsonl"
    temp_dir = state_dir / "tmp"

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup_path)
    original_source = target.read_text(encoding="utf-8")

    check = check_lean(project, target, lean_timeout_seconds, lake_executable)
    _write_jsonl(
        log_path,
        {
            "event": "run_start",
            "file": relative.as_posix(),
            "task": task,
            "api_endpoint": config.endpoint,
            "api_mode": config.mode,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "initial_check_ok": check.ok,
            "initial_returncode": check.returncode,
        },
    )

    for attempt in range(1, max_attempts + 1):
        source = target.read_text(encoding="utf-8")
        prompt = build_user_prompt(
            relative_file=relative.as_posix(),
            task=task,
            source=source,
            diagnostics=check.output,
            attempt=attempt,
        )
        _write_jsonl(log_path, {"event": "model_start", "attempt": attempt})
        try:
            replacement = call_model(config, prompt, temp_dir)
        except ApiError as exc:
            _write_jsonl(
                log_path,
                {"event": "model_error", "attempt": attempt, "error": str(exc)},
            )
            if not keep_failed:
                _atomic_write(target, original_source)
            raise

        _atomic_write(target, replacement)
        check = check_lean(project, target, lean_timeout_seconds, lake_executable)
        _write_jsonl(
            log_path,
            {
                "event": "lean_check",
                "attempt": attempt,
                "ok": check.ok,
                "returncode": check.returncode,
                "diagnostics": check.output,
            },
        )
        if check.ok:
            _write_jsonl(log_path, {"event": "run_success", "attempt": attempt})
            return RunResult(True, attempt, backup_path, check, False)

    restored = not keep_failed
    if restored:
        _atomic_write(target, original_source)
        check = check_lean(project, target, lean_timeout_seconds, lake_executable)
    _write_jsonl(
        log_path,
        {"event": "run_failed", "attempts": max_attempts, "restored": restored},
    )
    return RunResult(False, max_attempts, backup_path, check, restored)
