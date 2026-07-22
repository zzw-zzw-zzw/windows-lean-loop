from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator

from lean_loop.api import call_model, call_model_json
from lean_loop.config import ApiConfig
from lean_loop.explanation import generate_workflow_explanation
from lean_loop.jsonutil import read_json, utc_now
from lean_loop.lean import check_lean, resolve_target
from lean_loop.process_control import ProcessCancelled, ProcessControl
from lean_loop.race import LaneExecutionResult, RaceResult, run_prover_race
from lean_loop.workflow import WorkflowResult, run_structured_workflow


ACTIVE_STATES = {"planning", "proving", "lean_checking", "reviewing", "auditing", "explaining"}
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
ALL_STATES = {"queued", "blocked", *ACTIVE_STATES, *TERMINAL_STATES}
ALLOWED_TRANSITIONS = {
    "queued": {"planning", "blocked", "cancelled"},
    "blocked": {"queued", "cancelled"},
    "planning": {"lean_checking", "proving", "failed", "cancelled"},
    "proving": {"lean_checking", "failed", "cancelled"},
    "lean_checking": {"planning", "proving", "reviewing", "auditing", "failed", "cancelled"},
    "reviewing": {"proving", "lean_checking", "auditing", "explaining", "succeeded", "failed", "cancelled"},
    "auditing": {"proving", "explaining", "succeeded", "failed", "cancelled"},
    "explaining": {"succeeded", "failed", "cancelled"},
    "succeeded": set(),
    "failed": {"queued"},
    "cancelled": {"queued"},
}


class QueueError(ValueError):
    pass


@dataclass(frozen=True)
class QueueWorkResult:
    processed: int
    succeeded: int
    failed: int
    cancelled: int


def _pid_exists(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class QueueStore:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.path = project / ".lean-agent" / "queue.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    target_file TEXT NOT NULL,
                    task_text TEXT NOT NULL,
                    state TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    workflow_run_id TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    worker_pid INTEGER,
                    active_pid INTEGER,
                    active_kind TEXT,
                    attempt INTEGER,
                    run_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    depends_on TEXT NOT NULL REFERENCES tasks(id),
                    PRIMARY KEY (task_id, depends_on)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tasks_state_index ON tasks(state, created_at);
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "activity_text" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN activity_text TEXT")
            if "activity_at" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN activity_at TEXT")

    def _event(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO task_events(task_id, timestamp, event, details_json) VALUES (?, ?, ?, ?)",
            (task_id, utc_now(), event, json.dumps(details or {}, ensure_ascii=False)),
        )

    def add_task(
        self,
        *,
        target_file: str,
        task_text: str,
        settings: dict[str, Any],
        dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        dependencies = dependencies or []
        task_id = uuid.uuid4().hex[:12]
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for dependency in dependencies:
                if connection.execute(
                    "SELECT 1 FROM tasks WHERE id = ?", (dependency,)
                ).fetchone() is None:
                    raise QueueError(f"Dependency task not found: {dependency}")
            connection.execute(
                """INSERT INTO tasks(
                    id, target_file, task_text, state, settings_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
                (task_id, target_file, task_text, json.dumps(settings), now, now),
            )
            connection.executemany(
                "INSERT INTO dependencies(task_id, depends_on) VALUES (?, ?)",
                [(task_id, dependency) for dependency in dependencies],
            )
            self._event(connection, task_id, "task_added", {"dependencies": dependencies})
        return self.get_task(task_id)

    def _row_to_dict(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["settings"] = json.loads(value.pop("settings_json"))
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["dependencies"] = [
            item["depends_on"]
            for item in connection.execute(
                "SELECT depends_on FROM dependencies WHERE task_id = ? ORDER BY depends_on",
                (value["id"],),
            )
        ]
        return value

    def get_task(self, task_id: str, *, include_events: bool = False) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise QueueError(f"Queue task not found: {task_id}")
            value = self._row_to_dict(connection, row)
            if include_events:
                value["events"] = [
                    {
                        **dict(event),
                        "details": json.loads(event["details_json"]),
                    }
                    for event in connection.execute(
                        "SELECT * FROM task_events WHERE task_id = ? ORDER BY sequence",
                        (task_id,),
                    )
                ]
                for event in value["events"]:
                    event.pop("details_json", None)
            return value

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            return [self._row_to_dict(connection, row) for row in rows]

    def transition(
        self,
        task_id: str,
        new_state: str,
        **changes: Any,
    ) -> dict[str, Any]:
        if new_state not in ALL_STATES:
            raise QueueError(f"Unknown task state: {new_state}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise QueueError(f"Queue task not found: {task_id}")
            old_state = row["state"]
            if new_state != old_state and new_state not in ALLOWED_TRANSITIONS[old_state]:
                raise QueueError(f"Illegal task transition: {old_state} -> {new_state}")
            allowed_columns = {
                "workflow_run_id", "error", "cancel_requested", "worker_pid",
                "active_pid", "active_kind", "attempt", "run_count",
            }
            unknown = set(changes) - allowed_columns
            if unknown:
                raise QueueError(f"Unsupported task fields: {', '.join(sorted(unknown))}")
            assignments = ["state = ?", "updated_at = ?"]
            values: list[Any] = [new_state, utc_now()]
            for key, value in changes.items():
                assignments.append(f"{key} = ?")
                values.append(int(value) if key == "cancel_requested" else value)
            values.append(task_id)
            connection.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", values
            )
            self._event(
                connection,
                task_id,
                "state_changed" if new_state != old_state else "task_updated",
                {"from": old_state, "to": new_state, **changes},
            )
        return self.get_task(task_id)

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["state"] in TERMINAL_STATES:
            return task
        if task["state"] in {"queued", "blocked"}:
            return self.transition(task_id, "cancelled", cancel_requested=True)
        return self.transition(task_id, task["state"], cancel_requested=True)

    def retry(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["state"] not in {"failed", "cancelled"}:
            raise QueueError("Only failed or cancelled tasks can be retried")
        settings = dict(task.get("settings") or {})
        if settings.get("race"):
            settings["max_attempts"] = int(settings.get("max_attempts") or 3) * 2
            settings["max_attempts_per_step"] = int(
                settings.get("max_attempts_per_step") or 3
            ) * 2
        run_id = task.get("workflow_run_id")
        if run_id:
            manifest_path = (
                self.project / ".lean-agent" / "workflows" / str(run_id) / "run.json"
            )
            if manifest_path.is_file():
                manifest = read_json(manifest_path)
                attempts_used = len(manifest.get("attempts") or [])
                total = int(settings.get("max_attempts") or 3)
                if attempts_used >= total:
                    settings["max_attempts"] = attempts_used + total
                failed_steps = [
                    row
                    for row in manifest.get("steps") or []
                    if isinstance(row, dict) and row.get("status") in {"failed", "stopped"}
                ]
                if failed_steps:
                    step_used = len(failed_steps[0].get("attempts") or [])
                    per_step = int(settings.get("max_attempts_per_step") or total)
                    if step_used >= per_step:
                        settings["max_attempts_per_step"] = step_used + per_step
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET settings_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(settings, ensure_ascii=False), utc_now(), task_id),
            )
        return self.transition(
            task_id,
            "queued",
            cancel_requested=False,
            worker_pid=None,
            active_pid=None,
            active_kind=None,
            attempt=None,
            error=None,
        )

    def update_settings(self, task_id: str, settings: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET settings_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(settings, ensure_ascii=False), utc_now(), task_id),
            )
            self._event(connection, task_id, "settings_updated")
        return self.get_task(task_id)

    def _refresh_blocked(self, connection: sqlite3.Connection) -> None:
        blocked = connection.execute("SELECT id FROM tasks WHERE state = 'blocked'").fetchall()
        for row in blocked:
            pending = connection.execute(
                """SELECT 1 FROM dependencies d JOIN tasks t ON t.id = d.depends_on
                   WHERE d.task_id = ? AND t.state != 'succeeded' LIMIT 1""",
                (row["id"],),
            ).fetchone()
            if pending is None:
                connection.execute(
                    "UPDATE tasks SET state = 'queued', updated_at = ? WHERE id = ?",
                    (utc_now(), row["id"]),
                )
                self._event(connection, row["id"], "dependencies_satisfied")

    def recover_orphans(self) -> int:
        recovered = 0
        for task in self.list_tasks():
            if task["state"] in ACTIVE_STATES and not _pid_exists(task["worker_pid"]):
                self.transition(
                    task["id"],
                    "failed",
                    error="Worker process ended before the task reached a terminal state.",
                    worker_pid=None,
                    active_pid=None,
                    active_kind=None,
                )
                recovered += 1
        return recovered

    def claim_next(self, worker_pid: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._refresh_blocked(connection)
            active = connection.execute(
                f"SELECT 1 FROM tasks WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)}) LIMIT 1",
                tuple(ACTIVE_STATES),
            ).fetchone()
            if active is not None:
                return None
            row = connection.execute(
                """SELECT q.* FROM tasks q
                   WHERE q.state = 'queued' AND q.cancel_requested = 0
                     AND NOT EXISTS (
                       SELECT 1 FROM dependencies d JOIN tasks dep ON dep.id = d.depends_on
                       WHERE d.task_id = q.id AND dep.state != 'succeeded'
                     )
                   ORDER BY q.created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                failed_dependencies = connection.execute(
                    """SELECT DISTINCT q.id FROM tasks q
                       JOIN dependencies d ON d.task_id = q.id
                       JOIN tasks dep ON dep.id = d.depends_on
                       WHERE q.state = 'queued' AND dep.state IN ('failed', 'cancelled')"""
                ).fetchall()
                for blocked in failed_dependencies:
                    connection.execute(
                        "UPDATE tasks SET state = 'blocked', updated_at = ? WHERE id = ?",
                        (utc_now(), blocked["id"]),
                    )
                    self._event(connection, blocked["id"], "dependency_blocked")
                return None
            now = utc_now()
            connection.execute(
                """UPDATE tasks SET state = 'planning', updated_at = ?, worker_pid = ?,
                   run_count = run_count + 1, error = NULL WHERE id = ?""",
                (now, worker_pid, row["id"]),
            )
            self._event(connection, row["id"], "task_claimed", {"worker_pid": worker_pid})
            claimed = connection.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
            return self._row_to_dict(connection, claimed)

    def cancel_requested(self, task_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            return row is None or bool(row["cancel_requested"])

    def set_active_process(self, task_id: str, pid: int | None, kind: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET active_pid = ?, active_kind = ?, updated_at = ? WHERE id = ?",
                (pid, kind, utc_now(), task_id),
            )
            self._event(
                connection,
                task_id,
                "process_started" if pid else "process_finished",
                {"pid": pid, "kind": kind},
            )

    def set_activity(self, task_id: str, text: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET activity_text = ?, activity_at = ?, updated_at = ? WHERE id = ?",
                (text, utc_now() if text else None, utc_now(), task_id),
            )

    def attach_workflow(self, task_id: str, run_id: str) -> None:
        task = self.get_task(task_id)
        self.transition(task_id, task["state"], workflow_run_id=run_id)


class QueueProcessController(ProcessControl):
    def __init__(self, store: QueueStore, task_id: str) -> None:
        self.store = store
        self.task_id = task_id
        self._last_progress_at = 0.0
        self._last_activity = ""

    def cancel_requested(self) -> bool:
        return self.store.cancel_requested(self.task_id)

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested():
            raise ProcessCancelled(f"Queue task {self.task_id} was cancelled")

    def process_started(self, pid: int, kind: str) -> None:
        self.store.set_active_process(self.task_id, pid, kind)

    def process_finished(self, pid: int) -> None:
        task = self.store.get_task(self.task_id)
        if task.get("active_pid") == pid:
            self.store.set_active_process(self.task_id, None, None)

    def process_progress(self, kind: str, details: dict[str, Any]) -> None:
        event = str(details.get("event", "activity"))
        now = time.monotonic()
        if "reasoning" in event:
            text = f"Model reasoning · {details.get('reasoning_events', 0)} events"
        elif event == "response.output_text.delta":
            text = f"Model output · {details.get('output_characters', 0)} characters"
        elif event == "response.retry":
            text = (
                f"API timeout retry {details.get('retry')}/{details.get('retries')} · "
                f"effort {details.get('reasoning_effort') or 'default'}"
            )
        elif event == "transport.selected":
            text = f"API transport · {details.get('transport') or 'auto'}"
        elif event == "response.completed":
            text = f"Model response complete · {details.get('output_characters', 0)} characters"
        elif event in {"response.created", "response.in_progress"}:
            text = "Model request active"
        else:
            text = event
        if text == self._last_activity and now - self._last_progress_at < 1.0:
            return
        if event in {"response.output_text.delta"} and now - self._last_progress_at < 1.0:
            return
        self._last_progress_at = now
        self._last_activity = text
        self.store.set_activity(self.task_id, text)

    def enter_phase(self, phase: str, attempt: int | None = None) -> None:
        self.raise_if_cancelled()
        task = self.store.get_task(self.task_id)
        changes: dict[str, Any] = {"attempt": attempt} if attempt is not None else {}
        self.store.transition(self.task_id, phase, **changes)
        self.store.set_activity(
            self.task_id,
            f"{phase} · candidate {attempt}" if attempt is not None else phase,
        )


def run_queue_task(
    *,
    store: QueueStore,
    task_row: dict[str, Any],
    base_config: ApiConfig | None = None,
) -> WorkflowResult | None:
    task_id = task_row["id"]
    settings = task_row["settings"]
    agent_backend_id = str(settings.get("agent_backend") or "direct")
    if agent_backend_id != "direct" and settings.get("explain"):
        raise QueueError(
            "Subscription queue tasks cannot silently use a direct API for explanation"
        )
    race_settings = settings.get("race") if isinstance(settings.get("race"), dict) else None
    if agent_backend_id != "direct" and race_settings:
        raise QueueError("Subscription backends do not support provider races")
    if race_settings and len(race_settings.get("lanes") or []) >= 2:
        return _run_queue_race(store=store, task_row=task_row, race_settings=race_settings)

    controller = QueueProcessController(store, task_id)
    target = resolve_target(store.project, task_row["target_file"])
    provider_id = str(settings.get("provider") or "default")
    selected_model = str(settings.get("model") or "").strip()
    if agent_backend_id == "direct":
        if base_config is None or provider_id != "default":
            base_config = ApiConfig.for_backend(
                store.project, "direct", provider_id=provider_id, model=selected_model
            )
    else:
        base_config = ApiConfig.for_backend(
            store.project,
            agent_backend_id,
            provider_id=provider_id,
            model=selected_model,
        )
    api_timeout = settings.get("api_timeout")
    if api_timeout is not None:
        base_config = replace(base_config, timeout_seconds=int(api_timeout))
    api_retries = settings.get("api_retries")
    if api_retries is not None:
        base_config = replace(base_config, api_timeout_retries=int(api_retries))
    if selected_model:
        base_config = replace(base_config, model=selected_model)
    plan_config = replace(base_config, reasoning_effort=settings["plan_effort"])
    prove_config = replace(base_config, reasoning_effort=settings["prove_effort"])
    review_config = replace(
        base_config,
        model=settings.get("review_model") or base_config.model,
        reasoning_effort=settings["review_effort"],
    )

    def json_call(config: ApiConfig, system: str, user: str, temp: Path) -> dict[str, Any]:
        return call_model_json(config, system, user, temp, process_control=controller)

    def file_call(config: ApiConfig, prompt: str, temp: Path) -> str:
        return call_model(config, prompt, temp, process_control=controller)

    def lean_checker(project: Path, target: Path, timeout: int, lake: str):
        return check_lean(
            project, target, timeout, lake, process_control=controller
        )

    def persist_backend_identity(identity: dict[str, Any]) -> None:
        settings["backend_identity"] = dict(identity)
        store.update_settings(task_id, settings)

    try:
        controller.raise_if_cancelled()
        result = run_structured_workflow(
            project=store.project,
            target=target,
            task=task_row["task_text"],
            plan_config=plan_config,
            prove_config=prove_config,
            review_config=review_config,
            max_attempts=int(settings["max_attempts"]),
            max_attempts_per_step=int(
                settings.get("max_attempts_per_step") or settings["max_attempts"]
            ),
            lean_timeout_seconds=int(settings["lean_timeout"]),
            lake_executable=settings["lake"],
            keep_failed=bool(settings.get("keep_failed", False)),
            formalize_goal=bool(settings.get("formalize_goal", True)),
            import_policy=str(settings.get("import_policy") or "auto"),
            protect_existing_statements=bool(
                settings.get("protect_existing_statements", True)
            ),
            protected_declarations=list(settings.get("protected_declarations") or []),
            resume_run_id=(
                str(task_row["workflow_run_id"])
                if task_row.get("workflow_run_id")
                else None
            ),
            json_model_call=json_call,
            file_model_call=file_call,
            lean_checker=lean_checker,
            phase_callback=controller.enter_phase,
            workflow_created_callback=lambda run_id: store.attach_workflow(task_id, run_id),
            backend_identity_callback=persist_backend_identity,
            process_control=controller,
            agent_backend_id=agent_backend_id,
        )
        controller.raise_if_cancelled()
        if result.ok and settings.get("explain"):
            controller.enter_phase("explaining")
            explain_config = replace(
                base_config,
                model=settings.get("explain_model") or base_config.model,
                reasoning_effort=settings.get("explain_effort", "medium"),
            )
            generate_workflow_explanation(
                project=store.project,
                run_id=result.run_id,
                config=explain_config,
                language=settings.get("explain_language", "zh-CN"),
                json_model_call=json_call,
            )
            controller.raise_if_cancelled()
        final_state = "succeeded" if result.ok else "failed"
        store.transition(
            task_id,
            final_state,
            error=None if result.ok else "Lean workflow failed.",
            worker_pid=None,
            active_pid=None,
            active_kind=None,
        )
        return result
    except ProcessCancelled as exc:
        current = store.get_task(task_id)
        if current["state"] not in TERMINAL_STATES:
            store.transition(
                task_id,
                "cancelled",
                error=str(exc),
                worker_pid=None,
                active_pid=None,
                active_kind=None,
            )
        return None
    except KeyboardInterrupt:
        store.request_cancel(task_id)
        current = store.get_task(task_id)
        if current["state"] not in TERMINAL_STATES:
            store.transition(
                task_id,
                "cancelled",
                error="Worker interrupted from the console.",
                worker_pid=None,
                active_pid=None,
                active_kind=None,
            )
        raise
    except Exception as exc:
        current = store.get_task(task_id)
        if current["state"] not in TERMINAL_STATES:
            store.transition(
                task_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                worker_pid=None,
                active_pid=None,
                active_kind=None,
            )
        raise


def _run_queue_race(
    *,
    store: QueueStore,
    task_row: dict[str, Any],
    race_settings: dict[str, Any],
) -> WorkflowResult | None:
    task_id = task_row["id"]
    settings = task_row["settings"]
    lane_specs = list(race_settings.get("lanes") or [])
    store.transition(task_id, "proving")
    store.set_activity(task_id, f"Starting {len(lane_specs)} prover lanes")

    def task_cancelled() -> bool:
        return store.cancel_requested(task_id)

    def activity(text: str) -> None:
        store.set_activity(task_id, text)

    def lane_runner(
        lane_project: Path,
        lane_target: Path,
        lane: dict[str, Any],
        resume_run_id: str | None,
        controller: ProcessControl,
    ) -> LaneExecutionResult:
        provider_id = str(lane.get("provider") or "default")
        config = ApiConfig.from_environment(store.project, provider_id)
        api_timeout = lane.get("api_timeout", settings.get("api_timeout"))
        if api_timeout not in (None, ""):
            config = replace(config, timeout_seconds=int(api_timeout))
        api_retries = lane.get("api_retries", settings.get("api_retries"))
        if api_retries is not None:
            config = replace(config, api_timeout_retries=int(api_retries))
        model = str(lane.get("model") or "").strip()
        if model:
            config = replace(config, model=model)
        plan_config = replace(
            config,
            reasoning_effort=str(lane.get("plan_effort") or settings["plan_effort"]),
        )
        prove_config = replace(
            config,
            reasoning_effort=str(lane.get("prove_effort") or settings["prove_effort"]),
        )
        review_config = replace(
            config,
            model=str(lane.get("review_model") or config.model),
            reasoning_effort=str(
                lane.get("review_effort") or settings["review_effort"]
            ),
        )
        lane_prompt = str(lane.get("prompt") or "").strip()
        lane_task = task_row["task_text"]
        if lane_prompt:
            lane_task += (
                "\n\nIndependent prover lane instructions (do not change the original task):\n"
                + lane_prompt
            )

        def json_call(cfg: ApiConfig, system: str, user: str, temp: Path) -> dict[str, Any]:
            return call_model_json(cfg, system, user, temp, process_control=controller)

        def file_call(cfg: ApiConfig, prompt: str, temp: Path) -> str:
            return call_model(cfg, prompt, temp, process_control=controller)

        def lane_check(project: Path, target: Path, timeout: int, lake: str):
            return check_lean(project, target, timeout, lake, process_control=controller)

        try:
            result = run_structured_workflow(
                project=lane_project,
                target=lane_target,
                task=lane_task,
                plan_config=plan_config,
                prove_config=prove_config,
                review_config=review_config,
                max_attempts=int(settings["max_attempts"]),
                max_attempts_per_step=int(
                    settings.get("max_attempts_per_step") or settings["max_attempts"]
                ),
                lean_timeout_seconds=int(settings["lean_timeout"]),
                lake_executable=settings["lake"],
                keep_failed=False,
                formalize_goal=bool(settings.get("formalize_goal", True)),
                import_policy=str(settings.get("import_policy") or "auto"),
                protect_existing_statements=bool(
                    settings.get("protect_existing_statements", True)
                ),
                protected_declarations=list(
                    settings.get("protected_declarations") or []
                ),
                resume_run_id=resume_run_id,
                json_model_call=json_call,
                file_model_call=file_call,
                lean_checker=lane_check,
                phase_callback=getattr(controller, "enter_phase"),
                workflow_created_callback=getattr(controller, "attach_workflow"),
                process_control=controller,
            )
            return LaneExecutionResult(
                result.ok,
                result.run_id,
                result.final_check,
                None if result.ok else "Lane workflow failed",
            )
        except ProcessCancelled:
            raise
        except Exception as exc:
            return LaneExecutionResult(
                False, resume_run_id, None, f"{type(exc).__name__}: {exc}"
            )

    def main_checker(
        project: Path,
        target: Path,
        timeout: int,
        lake: str,
        controller: ProcessControl,
    ):
        return check_lean(project, target, timeout, lake, process_control=controller)

    try:
        race_id = str(race_settings.get("race_id") or "") or uuid.uuid4().hex[:12]
        if race_settings.get("race_id") != race_id:
            race_settings["race_id"] = race_id
            settings["race"] = race_settings
            store.update_settings(task_id, settings)
        result: RaceResult = run_prover_race(
            project=store.project,
            task_id=task_id,
            target_file=task_row["target_file"],
            task_text=task_row["task_text"],
            lane_specs=lane_specs,
            lean_timeout_seconds=int(settings["lean_timeout"]),
            lake_executable=settings["lake"],
            task_cancelled=task_cancelled,
            lane_runner=lane_runner,
            main_checker=main_checker,
            activity_callback=activity,
            race_id=race_id,
        )
        if not result.ok:
            final_state = "cancelled" if task_cancelled() else "failed"
            store.transition(
                task_id,
                final_state,
                error=("Prover race cancelled" if final_state == "cancelled" else "All prover lanes failed"),
                worker_pid=None,
                active_pid=None,
                active_kind=None,
            )
            return None
        if result.winner_run_id:
            store.attach_workflow(task_id, result.winner_run_id)
        store.transition(task_id, "lean_checking")
        store.transition(task_id, "auditing")
        if settings.get("explain") and result.winner_run_id:
            winner = next(
                lane for lane in lane_specs
                if str(lane.get("id")) == str(result.winner_lane_id)
            )
            explain_config = ApiConfig.from_environment(
                store.project, str(winner.get("provider") or "default")
            )
            if winner.get("model"):
                explain_config = replace(explain_config, model=str(winner["model"]))
            explain_config = replace(
                explain_config,
                reasoning_effort=settings.get("explain_effort", "medium"),
            )
            store.transition(task_id, "explaining")
            generate_workflow_explanation(
                project=store.project,
                run_id=result.winner_run_id,
                config=explain_config,
                language=settings.get("explain_language", "zh-CN"),
            )
        store.transition(
            task_id,
            "succeeded",
            error=None,
            worker_pid=None,
            active_pid=None,
            active_kind=None,
        )
        return WorkflowResult(
            True,
            result.winner_run_id or result.race_id,
            0,
            store.project / ".lean-agent" / "races" / result.race_id,
            result.final_check or LeanCheck(True, 0, "", ()),
            False,
        )
    except ProcessCancelled as exc:
        current = store.get_task(task_id)
        if current["state"] not in TERMINAL_STATES:
            store.transition(
                task_id,
                "cancelled",
                error=str(exc),
                worker_pid=None,
                active_pid=None,
                active_kind=None,
            )
        return None
    except Exception as exc:
        current = store.get_task(task_id)
        if current["state"] not in TERMINAL_STATES:
            store.transition(
                task_id,
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                worker_pid=None,
                active_pid=None,
                active_kind=None,
            )
        raise


def work_queue(
    *,
    project: Path,
    once: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> QueueWorkResult:
    store = QueueStore(project)
    store.recover_orphans()
    processed = succeeded = failed = cancelled = 0
    while True:
        task = store.claim_next(os.getpid())
        if task is None:
            break
        if progress:
            progress(task)
        try:
            run_queue_task(
                store=store,
                task_row=task,
            )
        except KeyboardInterrupt:
            cancelled += 1
            processed += 1
            break
        except Exception:
            failed += 1
            processed += 1
        else:
            state = store.get_task(task["id"])["state"]
            succeeded += state == "succeeded"
            failed += state == "failed"
            cancelled += state == "cancelled"
            processed += 1
        if once:
            break
    return QueueWorkResult(processed, succeeded, failed, cancelled)
