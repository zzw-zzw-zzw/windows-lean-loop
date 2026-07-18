from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from lean_loop.agent_protocol import protocol_capabilities
from lean_loop.jsonutil import atomic_write_text, read_json, utc_now
from lean_loop.lean import resolve_target
from lean_loop.project_config import (
    ProjectConfigError,
    provider_profiles_view,
    project_config_view,
    save_provider_profile,
    save_project_config,
)
from lean_loop.queue import QueueError, QueueStore
from lean_loop.state import list_workflows


ASSET_ROOT = Path(__file__).with_name("dashboard_assets")
MAX_TEXT_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
ACTIVE_STATES = {"planning", "proving", "lean_checking", "reviewing", "auditing", "explaining"}


def _lean_files(project: Path) -> list[str]:
    files: list[str] = []
    for path in project.rglob("*.lean"):
        try:
            relative = path.relative_to(project)
        except ValueError:
            continue
        if any(part in {".git", ".lake", ".lean-agent"} for part in relative.parts):
            continue
        files.append(relative.as_posix())
        if len(files) >= 1000:
            break
    return sorted(files, key=str.casefold)


def _default_queue_settings(project: Path) -> dict[str, Any]:
    configured = project_config_view(project)
    effort = str(configured.get("reasoning_effort") or "high")
    return {
        "model": "",
        "provider": "default",
        "max_attempts": 3,
        "max_attempts_per_step": 3,
        "lean_timeout": 120,
        "api_timeout": int(configured.get("timeout_seconds") or 180),
        "api_retries": int(configured.get("api_timeout_retries") or 0),
        "plan_effort": effort,
        "prove_effort": effort,
        "review_effort": effort,
        "review_model": os.environ.get("LEAN_AGENT_REVIEW_MODEL", ""),
        "lake": str(configured.get("lake") or "lake"),
        "keep_failed": False,
        "formalize_goal": True,
        "import_policy": "auto",
        "protect_existing_statements": True,
        "protected_declarations": [],
        "explain": True,
        "explain_language": "zh-CN",
        "explain_effort": "medium",
        "explain_model": os.environ.get("LEAN_AGENT_EXPLAIN_MODEL", ""),
        "race": None,
    }


def _queue_settings(value: object, project: Path) -> dict[str, Any]:
    settings = _default_queue_settings(project)
    supplied = value if isinstance(value, dict) else {}
    for key in settings:
        if key in supplied:
            settings[key] = supplied[key]
    for key in ("max_attempts", "max_attempts_per_step", "lean_timeout"):
        settings[key] = int(settings[key])
        if settings[key] < 1:
            raise ValueError(f"{key} must be positive")
    if settings["api_timeout"] in (None, ""):
        settings["api_timeout"] = None
    else:
        settings["api_timeout"] = int(settings["api_timeout"])
        if settings["api_timeout"] < 1:
            raise ValueError("api_timeout must be positive")
    settings["api_retries"] = int(settings.get("api_retries", 1))
    if settings["api_retries"] < 0:
        raise ValueError("api_retries cannot be negative")
    efforts = {"low", "medium", "high", "xhigh"}
    for key in ("plan_effort", "prove_effort", "review_effort", "explain_effort"):
        settings[key] = str(settings[key])
        if settings[key] not in efforts:
            raise ValueError(f"Unsupported {key}: {settings[key]}")
    for key in (
        "model", "provider", "review_model", "lake", "explain_language", "explain_model",
        "import_policy",
    ):
        settings[key] = str(settings[key]).strip()
    if settings["import_policy"] not in {"auto", "proof-first", "precise", "broad"}:
        raise ValueError(f"Unsupported import_policy: {settings['import_policy']}")
    protected = settings.get("protected_declarations", [])
    if not isinstance(protected, list):
        raise ValueError("protected_declarations must be an array")
    settings["protected_declarations"] = [str(value).strip() for value in protected if str(value).strip()]
    for key in (
        "keep_failed", "explain", "protect_existing_statements", "formalize_goal"
    ):
        settings[key] = bool(settings[key])
    raw_race = supplied.get("race")
    if raw_race:
        if not isinstance(raw_race, dict):
            raise ValueError("race must be an object")
        raw_lanes = raw_race.get("lanes", [])
        if not isinstance(raw_lanes, list) or not 2 <= len(raw_lanes) <= 4:
            raise ValueError("A prover race requires 2-4 lanes")
        lanes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, value in enumerate(raw_lanes, 1):
            if not isinstance(value, dict):
                raise ValueError("Every race lane must be an object")
            lane_id = str(value.get("id") or f"lane-{index}").strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", lane_id):
                raise ValueError("Race lane IDs must use letters, numbers, _ or -")
            if lane_id in seen:
                raise ValueError("Race lane IDs must be unique")
            seen.add(lane_id)
            lane = {
                "id": lane_id,
                "provider": str(value.get("provider") or "default").strip(),
                "model": str(value.get("model") or "").strip(),
                "prompt": str(value.get("prompt") or "").strip(),
                "plan_effort": str(value.get("plan_effort") or settings["plan_effort"]),
                "prove_effort": str(value.get("prove_effort") or settings["prove_effort"]),
                "review_effort": str(value.get("review_effort") or settings["review_effort"]),
            }
            for effort_key in ("plan_effort", "prove_effort", "review_effort"):
                if lane[effort_key] not in efforts:
                    raise ValueError(f"Unsupported lane {effort_key}: {lane[effort_key]}")
            lanes.append(lane)
        settings["race"] = {
            "strategy": "first_verified_wins",
            "lanes": lanes,
            **(
                {"race_id": str(raw_race["race_id"])}
                if raw_race.get("race_id")
                else {}
            ),
        }
    else:
        settings["race"] = None
    return settings


def _resolve_or_create_target(project: Path, target_value: str) -> tuple[Path, bool]:
    clean = target_value.strip()
    if not clean:
        clean = f"GeneratedProof_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.lean"
    target = (project / clean).resolve()
    try:
        target.relative_to(project)
    except ValueError as exc:
        raise ValueError("Target file must stay inside the Lean project") from exc
    if target.suffix.lower() != ".lean":
        raise ValueError("Target file must have a .lean extension")
    if target.is_file():
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        target,
        "-- Created by Lean Agent. The formal goal and imports will be generated below.\n",
    )
    return target, True


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_seconds(value: object) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _read_json_optional(path: Path) -> dict[str, Any] | None:
    try:
        return read_json(path) if path.is_file() else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_text_optional(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _queue_connection(project: Path) -> sqlite3.Connection | None:
    path = project / ".lean-agent" / "queue.sqlite3"
    if not path.is_file():
        return None
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5
    )
    connection.row_factory = sqlite3.Row
    return connection


def _queue_tasks(project: Path, *, include_events: bool = False) -> list[dict[str, Any]]:
    connection = _queue_connection(project)
    if connection is None:
        return []
    try:
        rows = connection.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
        dependencies: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT task_id, depends_on FROM dependencies ORDER BY depends_on"
        ):
            dependencies.setdefault(row["task_id"], []).append(row["depends_on"])
        tasks: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["cancel_requested"] = bool(value.get("cancel_requested"))
            try:
                value["settings"] = json.loads(value.pop("settings_json"))
            except (TypeError, json.JSONDecodeError):
                value["settings"] = {}
            value["dependencies"] = dependencies.get(value["id"], [])
            value["elapsed_since_update_seconds"] = _elapsed_seconds(
                value.get("updated_at")
            )
            if include_events:
                value["events"] = []
            tasks.append(value)
        return tasks
    finally:
        connection.close()


def _queue_task(project: Path, task_id: str) -> dict[str, Any] | None:
    connection = _queue_connection(project)
    if connection is None:
        return None
    try:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["cancel_requested"] = bool(value.get("cancel_requested"))
        try:
            value["settings"] = json.loads(value.pop("settings_json"))
        except (TypeError, json.JSONDecodeError):
            value["settings"] = {}
        value["dependencies"] = [
            item["depends_on"]
            for item in connection.execute(
                "SELECT depends_on FROM dependencies WHERE task_id = ? ORDER BY depends_on",
                (task_id,),
            )
        ]
        events = []
        for event in connection.execute(
            "SELECT sequence, timestamp, event, details_json FROM task_events "
            "WHERE task_id = ? ORDER BY sequence DESC LIMIT 200",
            (task_id,),
        ):
            row_value = dict(event)
            try:
                row_value["details"] = json.loads(row_value.pop("details_json"))
            except (TypeError, json.JSONDecodeError):
                row_value["details"] = {}
            events.append(row_value)
        value["events"] = events
        return value
    finally:
        connection.close()


def _workflow_summary(row: dict[str, Any]) -> dict[str, Any]:
    timings = row.get("timings") if isinstance(row.get("timings"), dict) else {}
    return {
        "run_id": row.get("run_id"),
        "status": row.get("status"),
        "phase": row.get("phase"),
        "target_file": row.get("target_file"),
        "task": row.get("task"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_attempt": row.get("completed_attempt"),
        "attempt_count": len(row.get("attempts", [])),
        "current_step": row.get("current_step"),
        "step_count": len(row.get("steps", [])),
        "error": row.get("error"),
        "explanation_status": row.get("explanation_status"),
        "total_seconds": timings.get("total_seconds"),
    }


def dashboard_snapshot(
    project: Path,
    *,
    worker: dict[str, Any] | None = None,
    control_token: str | None = None,
) -> dict[str, Any]:
    try:
        tasks = _queue_tasks(project)
    except sqlite3.Error:
        tasks = []
    for task in tasks:
        race_settings = (task.get("settings") or {}).get("race")
        if not isinstance(race_settings, dict) or not race_settings.get("race_id"):
            continue
        race = _read_json_optional(
            project
            / ".lean-agent"
            / "races"
            / str(race_settings["race_id"])
            / "race.json"
        )
        if race:
            task["race_status"] = race.get("status")
            task["race_updated_at"] = race.get("updated_at")
            task["race_winner_lane_id"] = race.get("winner_lane_id")
    workflows = [_workflow_summary(row) for row in list_workflows(project)[:500]]
    counts: dict[str, int] = {}
    for task in tasks:
        state = str(task.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    active = [task for task in tasks if task.get("state") in ACTIVE_STATES]
    snapshot = {
        "generated_at": utc_now(),
        "project": str(project),
        "counts": counts,
        "active": active,
        "tasks": tasks,
        "workflows": workflows,
        "lean_files": _lean_files(project),
        "worker": worker or {"running": False, "pid": None, "exit_code": None},
        "configuration": project_config_view(project),
        "providers": provider_profiles_view(project),
        "agent_protocol": protocol_capabilities(),
    }
    if control_token is not None:
        snapshot["control_token"] = control_token
    return snapshot


def _workflow_detail_from_root(root: Path) -> dict[str, Any] | None:
    manifest = _read_json_optional(root / "run.json")
    if manifest is None:
        return None
    attempts: list[dict[str, Any]] = []
    for row in manifest.get("attempts", []):
        if not isinstance(row, dict) or not isinstance(row.get("attempt"), int):
            continue
        attempt_number = int(row["attempt"])
        attempt_dir = root / "attempts" / f"{attempt_number:03d}"
        attempts.append(
            {
                **row,
                "candidate": _read_text_optional(attempt_dir / "candidate.lean"),
                "check": _read_json_optional(attempt_dir / "check.json"),
                "retrieval": _read_json_optional(attempt_dir / "retrieval.json"),
                "review": _read_json_optional(
                    root / "reviews" / f"{attempt_number:03d}.json"
                ),
            }
        )
    checkpoints: list[dict[str, Any]] = []
    checkpoint_root = root / "checkpoints"
    if checkpoint_root.is_dir():
        for checkpoint_dir in sorted(checkpoint_root.iterdir()):
            if not checkpoint_dir.is_dir():
                continue
            metadata = _read_json_optional(checkpoint_dir / "checkpoint.json")
            if metadata is None:
                continue
            checkpoints.append(
                {
                    **metadata,
                    "source": _read_text_optional(checkpoint_dir / "source.lean"),
                    "check": _read_json_optional(checkpoint_dir / "check.json"),
                    "review": _read_json_optional(checkpoint_dir / "review.json"),
                }
            )
    agent_calls: list[dict[str, Any]] = []
    agent_call_root = root / "agent-calls"
    if agent_call_root.is_dir():
        for call_dir in sorted(agent_call_root.iterdir()):
            if call_dir.is_dir():
                agent_calls.append(
                    {
                        "request": _read_json_optional(call_dir / "request.json"),
                        "response": _read_json_optional(call_dir / "response.json"),
                        "raw_output": _read_text_optional(call_dir / "raw-output.txt"),
                    }
                )
    return {
        "manifest": manifest,
        "plan": _read_json_optional(root / "plan.json"),
        "goal": _read_json_optional(root / "goal.json"),
        "formal_goal_check": _read_json_optional(root / "formal-goal-check.json"),
        "planning_retrieval": _read_json_optional(root / "planning-retrieval.json"),
        "final_audit": _read_json_optional(root / "final-audit.json"),
        "timings": _read_json_optional(root / "timings.json"),
        "initial_check": _read_json_optional(root / "initial-check.json"),
        "initial_retrieval": _read_json_optional(root / "initial-retrieval.json"),
        "original": _read_text_optional(root / "original.lean"),
        "explanation": _read_text_optional(root / "explanation.md"),
        "attempts": attempts,
        "checkpoints": checkpoints,
        "agent_calls": agent_calls,
    }


def _workflow_detail(project: Path, run_id: str) -> dict[str, Any] | None:
    if not run_id or any(character not in "0123456789TZ" for character in run_id):
        return None
    return _workflow_detail_from_root(project / ".lean-agent" / "workflows" / run_id)


def _race_detail(project: Path, race_id: str) -> dict[str, Any] | None:
    race_root = project / ".lean-agent" / "races" / race_id
    race = _read_json_optional(race_root / "race.json")
    if race is None:
        return None
    lanes: list[dict[str, Any]] = []
    for lane in race.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        value = dict(lane)
        run_id = str(lane.get("run_id") or "")
        workflow = None
        if run_id:
            roots = [
                race_root / "lanes" / str(lane.get("id")) / "workflows" / run_id,
                Path(str(lane.get("worktree") or ""))
                / ".lean-agent"
                / "workflows"
                / run_id,
                project / ".lean-agent" / "workflows" / run_id,
            ]
            for root in roots:
                workflow = _workflow_detail_from_root(root)
                if workflow is not None:
                    break
        if workflow:
            manifest = workflow["manifest"]
            attempts = workflow.get("attempts") or []
            value["workflow"] = {
                "run_id": run_id,
                "status": manifest.get("status"),
                "phase": manifest.get("phase"),
                "current_step": manifest.get("current_step"),
                "steps": manifest.get("steps", []),
                "attempt_count": len(manifest.get("attempts", [])),
                "max_attempts": (manifest.get("settings") or {}).get("max_attempts"),
                "models": (manifest.get("settings") or {}).get("models", {}),
                "total_seconds": (workflow.get("timings") or {}).get("total_seconds"),
                "error": manifest.get("error"),
                "final_audit": workflow.get("final_audit"),
                "latest_check": attempts[-1].get("check") if attempts else None,
            }
        lanes.append(value)
    return {**race, "lanes": lanes}


def dashboard_task_detail(project: Path, task_id: str) -> dict[str, Any] | None:
    if not task_id or any(character not in "0123456789abcdef" for character in task_id):
        return None
    try:
        task = _queue_task(project, task_id)
    except sqlite3.Error:
        return None
    if task is None:
        return None
    run_id = task.get("workflow_run_id")
    race = None
    race_settings = (task.get("settings") or {}).get("race")
    if isinstance(race_settings, dict) and race_settings.get("race_id"):
        race = _race_detail(project, str(race_settings["race_id"]))
    return {
        "task": task,
        "workflow": _workflow_detail(project, str(run_id)) if run_id else None,
        "race": race,
    }


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: "DashboardServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _headers(self, content_type: str, length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'",
        )
        if length is not None:
            self.send_header("Content-Length", str(length))

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _request_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def _control_authorized(self) -> bool:
        client = self.client_address[0]
        if client not in {"127.0.0.1", "::1"}:
            return False
        supplied = self.headers.get("X-Lean-Agent-Token", "")
        return secrets.compare_digest(supplied, self.server.control_token)

    def _asset(self, relative: str) -> None:
        candidate = (ASSET_ROOT / relative).resolve()
        try:
            candidate.relative_to(ASSET_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self._headers("text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for _ in range(3600):
                payload = json.dumps(
                    self.server.snapshot(), ensure_ascii=False
                )
                self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self._asset("index.html")
            return
        if path.startswith("/assets/"):
            self._asset(path.removeprefix("/assets/"))
            return
        if path == "/api/snapshot":
            self._json(self.server.snapshot())
            return
        if path == "/api/capabilities":
            self._json({"agent_protocol": protocol_capabilities()})
            return
        if path == "/api/events":
            self._events()
            return
        if path.startswith("/api/tasks/"):
            detail = dashboard_task_detail(
                self.server.project, path.removeprefix("/api/tasks/")
            )
            self._json(detail or {"error": "Task not found"}, HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND)
            return
        if path.startswith("/api/workflows/"):
            detail = _workflow_detail(
                self.server.project, path.removeprefix("/api/workflows/")
            )
            self._json(detail or {"error": "Workflow not found"}, HTTPStatus.OK if detail else HTTPStatus.NOT_FOUND)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._control_authorized():
            self._json({"error": "Control request was not authorized"}, HTTPStatus.FORBIDDEN)
            return
        path = unquote(urlparse(self.path).path)
        try:
            body = self._request_json()
            if path == "/api/tasks":
                target_value = str(body.get("target_file", "")).strip()
                task_text = str(body.get("task_text", "")).strip()
                if not task_text:
                    raise ValueError("task_text is required")
                if len(task_text) > 20_000:
                    raise ValueError("task_text is too long")
                target, created = _resolve_or_create_target(
                    self.server.project, target_value
                )
                raw_dependencies = body.get("dependencies", [])
                if not isinstance(raw_dependencies, list):
                    raise ValueError("dependencies must be an array")
                dependencies = [str(value).strip() for value in raw_dependencies if str(value).strip()]
                row = QueueStore(self.server.project).add_task(
                    target_file=target.relative_to(self.server.project).as_posix(),
                    task_text=task_text,
                    settings=_queue_settings(body.get("settings"), self.server.project),
                    dependencies=dependencies,
                )
                self._json({"task": row, "target_created": created}, HTTPStatus.CREATED)
                return
            if path == "/api/config":
                raw_config = body.get("configuration", {})
                if not isinstance(raw_config, dict):
                    raise ValueError("configuration must be an object")
                raw_api_key = body.get("api_key")
                api_key = None if raw_api_key is None else str(raw_api_key)
                if api_key is not None and len(api_key) > 20_000:
                    raise ValueError("API key is too long")
                provider_id = str(body.get("provider_id") or "default").strip()
                if provider_id == "default":
                    view = save_project_config(
                        self.server.project,
                        raw_config,
                        api_key=api_key,
                        clear_api_key=bool(body.get("clear_api_key", False)),
                    )
                else:
                    view = save_provider_profile(
                        self.server.project,
                        provider_id,
                        raw_config,
                        api_key=api_key,
                        clear_api_key=bool(body.get("clear_api_key", False)),
                    )
                self._json(
                    {
                        "configuration": project_config_view(self.server.project),
                        "providers": provider_profiles_view(self.server.project),
                        "saved_provider": provider_id,
                    }
                )
                return
            if path == "/api/queue/start":
                self._json({"worker": self.server.start_worker()})
                return
            if path.startswith("/api/tasks/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("Unknown task action")
                task_id, action = parts[2], parts[3]
                store = QueueStore(self.server.project)
                if action == "cancel":
                    row = store.request_cancel(task_id)
                elif action == "retry":
                    row = store.retry(task_id)
                else:
                    raise ValueError("Unknown task action")
                self._json({"task": row})
                return
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except QueueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        except (FileNotFoundError, ValueError, ProjectConfigError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], project: Path) -> None:
        QueueStore(project)
        super().__init__(address, DashboardRequestHandler)
        self.project = project
        self.control_token = secrets.token_urlsafe(32)
        self._worker: subprocess.Popen[bytes] | None = None
        self._worker_lock = threading.Lock()
        self._worker_exit_code: int | None = None
        self._worker_started_at: str | None = None

    def worker_status(self) -> dict[str, Any]:
        with self._worker_lock:
            worker = self._worker
            if worker is None:
                return {
                    "running": False,
                    "pid": None,
                    "exit_code": self._worker_exit_code,
                    "started_at": self._worker_started_at,
                }
            exit_code = worker.poll()
            if exit_code is not None:
                self._worker_exit_code = exit_code
            return {
                "running": exit_code is None,
                "pid": worker.pid,
                "exit_code": exit_code,
                "started_at": self._worker_started_at,
            }

    def snapshot(self) -> dict[str, Any]:
        return dashboard_snapshot(
            self.project,
            worker=self.worker_status(),
            control_token=self.control_token,
        )

    def start_worker(self) -> dict[str, Any]:
        with self._worker_lock:
            if self._worker is not None and self._worker.poll() is None:
                raise QueueError(f"Queue worker is already running (PID {self._worker.pid})")
            store = QueueStore(self.project)
            active = [row for row in store.list_tasks() if row["state"] in ACTIVE_STATES]
            if active:
                pid = active[0].get("worker_pid")
                raise QueueError(f"A queue worker is already processing task {active[0]['id']} (PID {pid or 'unknown'})")
            ready = [row for row in store.list_tasks() if row["state"] == "queued"]
            if not ready:
                raise QueueError("No queued tasks are ready to start")
            log_path = self.project / ".lean-agent" / "dashboard-worker.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with log_path.open("ab") as log_file:
                package_root = Path(__file__).resolve().parent.parent
                self._worker = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "lean_loop",
                        "queue",
                        "work",
                        "--project",
                        str(self.project),
                    ],
                    cwd=package_root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
            self._worker_exit_code = None
            self._worker_started_at = utc_now()
            return {
                "running": True,
                "pid": self._worker.pid,
                "exit_code": None,
                "started_at": self._worker_started_at,
                "log_path": str(log_path),
            }

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_dashboard_server(project: Path, port: int = 8765) -> DashboardServer:
    if not 1 <= port <= 65535 and port != 0:
        raise ValueError("Dashboard port must be between 1 and 65535")
    return DashboardServer(("127.0.0.1", port), project)


def serve_dashboard(project: Path, port: int = 8765) -> None:
    server = create_dashboard_server(project, port)
    actual_port = int(server.server_address[1])
    print(f"Lean Agent Dashboard: http://127.0.0.1:{actual_port}")
    print(f"Project: {project}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Dashboard stopped.")
    finally:
        server.server_close()
