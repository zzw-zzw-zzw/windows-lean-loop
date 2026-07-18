from __future__ import annotations

import os
import stat
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lean_loop.jsonutil import atomic_write_json, atomic_write_text, read_json, sha256_text, utc_now
from lean_loop.lean import LeanCheck
from lean_loop.process_control import ProcessCancelled, ProcessControl


LANE_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "cancelled_by_winner"}
_WORKTREE_SETUP_LOCK = threading.Lock()


class RaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class LaneExecutionResult:
    ok: bool
    run_id: str | None
    final_check: LeanCheck | None
    error: str | None = None


@dataclass(frozen=True)
class RaceResult:
    ok: bool
    race_id: str
    winner_lane_id: str | None
    winner_run_id: str | None
    final_check: LeanCheck | None


LaneRunner = Callable[[Path, Path, dict[str, Any], str | None, ProcessControl], LaneExecutionResult]
MainChecker = Callable[[Path, Path, int, str, ProcessControl], LeanCheck]


def _race_root(project: Path, race_id: str) -> Path:
    return project / ".lean-agent" / "races" / race_id


def _worktree_root(project: Path, race_id: str) -> Path:
    return project.parent / f".{project.name}-lean-agent-worktrees" / race_id


def _safe_lane_id(value: object, index: int) -> str:
    raw = "".join(
        character.lower() if character.isalnum() else "-"
        for character in str(value or f"lane-{index}")
    ).strip("-")
    return (raw or f"lane-{index}")[:32]


class RaceStore:
    def __init__(self, project: Path, race_id: str) -> None:
        self.project = project
        self.race_id = race_id
        self.root = _race_root(project, race_id)
        self.path = self.root / "race.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(
        self,
        *,
        task_id: str,
        target_file: str,
        original_sha256: str,
        lanes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        value = {
            "schema_version": 1,
            "race_id": self.race_id,
            "task_id": task_id,
            "target_file": target_file,
            "strategy": "first_verified_wins",
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "original_sha256": original_sha256,
            "winner_lane_id": None,
            "winner_run_id": None,
            "lanes": lanes,
        }
        atomic_write_json(self.path, value)
        return value

    def read(self) -> dict[str, Any]:
        return read_json(self.path)

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            value = self.read()
            value.update(changes)
            value["updated_at"] = utc_now()
            atomic_write_json(self.path, value)
            return value

    def update_lane(self, lane_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            value = self.read()
            for lane in value.get("lanes", []):
                if lane.get("id") == lane_id:
                    lane.update(changes)
                    lane["updated_at"] = utc_now()
                    break
            else:
                raise RaceError(f"Race lane not found: {lane_id}")
            value["updated_at"] = utc_now()
            atomic_write_json(self.path, value)
            return value


def _run_git(project: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(project), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        raise RaceError((completed.stderr or completed.stdout).strip())


def _prepare_internal_repository(project: Path, race_id: str) -> Path:
    race_worktrees = _worktree_root(project, race_id)
    baseline = race_worktrees / "_baseline"
    with _WORKTREE_SETUP_LOCK:
        if (baseline / ".git").is_dir():
            return baseline
        baseline.mkdir(parents=True, exist_ok=True)
        _overlay_working_tree(project, baseline)
        _run_git(baseline, "init")
        _run_git(baseline, "config", "user.name", "Lean Agent")
        _run_git(baseline, "config", "user.email", "lean-agent@local.invalid")
        _run_git(baseline, "add", "--all")
        _run_git(baseline, "commit", "-m", f"Lean Agent race baseline {race_id}")
    return baseline


def _create_junction(link: Path, target: Path) -> None:
    if link.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0:
        raise RaceError(f"Could not create junction {link}: {completed.stdout or completed.stderr}")


def _overlay_working_tree(project: Path, worktree: Path) -> None:
    excluded = {".git", ".lake", ".lean-agent", ".lean-agent-tmp"}
    for source in project.iterdir():
        if source.name in excluded:
            continue
        destination = worktree / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def prepare_lane_worktree(
    project: Path,
    race_id: str,
    lane_id: str,
) -> Path:
    destination = _worktree_root(project, race_id) / lane_id
    if destination.is_dir():
        return destination
    baseline = _prepare_internal_repository(project, race_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_git(baseline, "worktree", "add", "--detach", str(destination), "HEAD")
    try:
        _create_junction(destination / ".lake", project / ".lake")
        lane_agent = destination / ".lean-agent"
        lane_agent.mkdir(parents=True, exist_ok=True)
        for name in ("indexes", "cache"):
            source = project / ".lean-agent" / name
            source.mkdir(parents=True, exist_ok=True)
            _create_junction(lane_agent / name, source)
    except Exception:
        try:
            _run_git(baseline, "worktree", "remove", "--force", str(destination))
        except Exception:
            pass
        raise
    return destination


def _remove_link(path: Path) -> None:
    if not path.exists():
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


def cleanup_lane_worktree(project: Path, worktree: Path) -> None:
    _remove_link(worktree / ".lake")
    _remove_link(worktree / ".lean-agent" / "indexes")
    _remove_link(worktree / ".lean-agent" / "cache")
    baseline = worktree.parent / "_baseline"
    _run_git(baseline, "worktree", "remove", "--force", str(worktree))
    try:
        _run_git(baseline, "worktree", "prune")
    except RaceError:
        pass


def cleanup_race_repository(project: Path, race_id: str) -> None:
    root = _worktree_root(project, race_id)
    baseline = root / "_baseline"
    if baseline.is_dir():
        def make_writable(function: Callable[..., object], path: str, error: BaseException) -> None:
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(baseline, onexc=make_writable)
    try:
        root.rmdir()
    except OSError:
        pass


def archive_lane_workflows(
    main_project: Path,
    race_id: str,
    lane_id: str,
    worktree: Path,
    *,
    winner_run_id: str | None = None,
) -> None:
    source = worktree / ".lean-agent" / "workflows"
    if not source.is_dir():
        return
    destination = _race_root(main_project, race_id) / "lanes" / lane_id / "workflows"
    shutil.copytree(source, destination, dirs_exist_ok=True)
    if winner_run_id:
        winner_source = source / winner_run_id
        if winner_source.is_dir():
            shutil.copytree(
                winner_source,
                main_project / ".lean-agent" / "workflows" / winner_run_id,
                dirs_exist_ok=True,
            )


class RaceLaneController(ProcessControl):
    def __init__(
        self,
        *,
        race_store: RaceStore,
        lane_id: str,
        task_cancelled: Callable[[], bool],
        winner_event: threading.Event,
        activity_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.race_store = race_store
        self.lane_id = lane_id
        self.task_cancelled = task_cancelled
        self.winner_event = winner_event
        self.activity_callback = activity_callback

    def cancel_requested(self) -> bool:
        return self.task_cancelled() or self.winner_event.is_set()

    def process_started(self, pid: int, kind: str) -> None:
        self.race_store.update_lane(self.lane_id, active_pid=pid, active_kind=kind)

    def process_finished(self, pid: int) -> None:
        lane = next(
            row for row in self.race_store.read()["lanes"] if row["id"] == self.lane_id
        )
        if lane.get("active_pid") == pid:
            self.race_store.update_lane(self.lane_id, active_pid=None, active_kind=None)

    def process_progress(self, kind: str, details: dict[str, Any]) -> None:
        event = str(details.get("event") or kind)
        text = f"{self.lane_id}: {event}"
        self.race_store.update_lane(self.lane_id, activity=text)
        if self.activity_callback:
            self.activity_callback(text)

    def enter_phase(self, phase: str, attempt: int | None = None) -> None:
        if self.cancel_requested():
            raise ProcessCancelled(f"Race lane {self.lane_id} was cancelled")
        self.race_store.update_lane(
            self.lane_id,
            phase=phase,
            attempt=attempt,
            activity=f"{phase} · candidate {attempt}" if attempt is not None else phase,
        )

    def attach_workflow(self, run_id: str) -> None:
        self.race_store.update_lane(self.lane_id, run_id=run_id)


def run_prover_race(
    *,
    project: Path,
    task_id: str,
    target_file: str,
    task_text: str,
    lane_specs: list[dict[str, Any]],
    lean_timeout_seconds: int,
    lake_executable: str,
    task_cancelled: Callable[[], bool],
    lane_runner: LaneRunner,
    main_checker: MainChecker,
    activity_callback: Callable[[str], None] | None = None,
    race_id: str | None = None,
) -> RaceResult:
    if len(lane_specs) < 2:
        raise RaceError("A prover race requires at least two lanes")
    target = (project / target_file).resolve()
    original_source = target.read_text(encoding="utf-8")
    original_sha = sha256_text(original_source)
    race_id = race_id or uuid.uuid4().hex[:12]
    race_store = RaceStore(project, race_id)
    if race_store.path.is_file():
        state = race_store.read()
        if state.get("original_sha256") != original_sha:
            raise RaceError("Main target changed since this prover race was created")
        lanes = state["lanes"]
        race_store.update(status="running", winner_lane_id=None, winner_run_id=None)
    else:
        lanes = []
        seen: set[str] = set()
        for index, spec in enumerate(lane_specs, 1):
            lane_id = _safe_lane_id(spec.get("id"), index)
            if lane_id in seen:
                lane_id = f"{lane_id}-{index}"
            seen.add(lane_id)
            lanes.append(
                {
                    **spec,
                    "id": lane_id,
                    "status": "queued",
                    "phase": "queued",
                    "run_id": None,
                    "worktree": None,
                    "active_pid": None,
                    "active_kind": None,
                    "score": None,
                    "error": None,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
        race_store.create(
            task_id=task_id,
            target_file=target_file,
            original_sha256=original_sha,
            lanes=lanes,
        )

    winner_event = threading.Event()
    winner_lock = threading.Lock()
    winner_lane_id: str | None = None
    winner_run_id: str | None = None
    winner_check: LeanCheck | None = None

    def run_lane(lane: dict[str, Any]) -> None:
        nonlocal winner_lane_id, winner_run_id, winner_check
        lane_id = str(lane["id"])
        if task_cancelled():
            race_store.update_lane(lane_id, status="cancelled", phase="complete")
            return
        worktree: Path | None = None
        try:
            worktree = prepare_lane_worktree(project, race_id, lane_id)
            race_store.update_lane(
                lane_id,
                status="running",
                phase="planning",
                worktree=str(worktree),
                error=None,
            )
            lane_target = (worktree / target_file).resolve()
            controller = RaceLaneController(
                race_store=race_store,
                lane_id=lane_id,
                task_cancelled=task_cancelled,
                winner_event=winner_event,
                activity_callback=activity_callback,
            )
            resume_run_id = str(lane.get("run_id") or "") or None
            result = lane_runner(worktree, lane_target, lane, resume_run_id, controller)
            if result.run_id:
                race_store.update_lane(lane_id, run_id=result.run_id)
            if not result.ok:
                race_store.update_lane(
                    lane_id,
                    status="failed",
                    phase="complete",
                    score=0,
                    error=result.error or "Lane workflow failed",
                )
                archive_lane_workflows(project, race_id, lane_id, worktree)
                return
            with winner_lock:
                if winner_event.is_set():
                    race_store.update_lane(
                        lane_id,
                        status="cancelled_by_winner",
                        phase="complete",
                        score=None,
                    )
                    archive_lane_workflows(project, race_id, lane_id, worktree)
                    return
                if sha256_text(target.read_text(encoding="utf-8")) != original_sha:
                    raise RaceError("Main target changed while prover lanes were running")
                candidate = lane_target.read_text(encoding="utf-8")
                atomic_write_text(target, candidate)
                try:
                    merge_check = main_checker(
                        project,
                        target,
                        lean_timeout_seconds,
                        lake_executable,
                        controller,
                    )
                finally:
                    if 'merge_check' not in locals() or not merge_check.ok:
                        atomic_write_text(target, original_source)
                if not merge_check.ok:
                    race_store.update_lane(
                        lane_id,
                        status="failed",
                        phase="merge_check",
                        score=0,
                        error=merge_check.output or "Main-project Lean check failed",
                    )
                    archive_lane_workflows(project, race_id, lane_id, worktree)
                    return
                winner_lane_id = lane_id
                winner_run_id = result.run_id
                winner_check = merge_check
                winner_event.set()
                archive_lane_workflows(
                    project,
                    race_id,
                    lane_id,
                    worktree,
                    winner_run_id=result.run_id,
                )
                race_store.update_lane(
                    lane_id,
                    status="succeeded",
                    phase="complete",
                    score=1,
                    selection_reason="first_verified_wins",
                )
                race_store.update(
                    status="succeeded",
                    winner_lane_id=lane_id,
                    winner_run_id=result.run_id,
                )
        except ProcessCancelled as exc:
            status = "cancelled_by_winner" if winner_event.is_set() else "cancelled"
            race_store.update_lane(lane_id, status=status, phase="complete", error=str(exc))
            if worktree is not None:
                archive_lane_workflows(project, race_id, lane_id, worktree)
        except Exception as exc:
            race_store.update_lane(
                lane_id,
                status="failed",
                phase="complete",
                score=0,
                error=f"{type(exc).__name__}: {exc}",
            )
            if worktree is not None:
                archive_lane_workflows(project, race_id, lane_id, worktree)

    threads = [
        threading.Thread(target=run_lane, args=(lane,), daemon=False, name=f"prover-{lane['id']}")
        for lane in lanes
        if lane.get("status") != "succeeded"
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    final_state = race_store.read()
    if winner_lane_id:
        # Successful races no longer need isolated worktrees after artifacts
        # have been archived into the main project.
        for lane in final_state.get("lanes", []):
            worktree_value = lane.get("worktree")
            if worktree_value:
                try:
                    cleanup_lane_worktree(project, Path(str(worktree_value)))
                except Exception as exc:
                    race_store.update_lane(
                        str(lane["id"]), cleanup_error=f"{type(exc).__name__}: {exc}"
                    )
        try:
            cleanup_race_repository(project, race_id)
        except Exception as exc:
            race_store.update(cleanup_error=f"{type(exc).__name__}: {exc}")
        return RaceResult(True, race_id, winner_lane_id, winner_run_id, winner_check)

    if task_cancelled():
        race_store.update(status="cancelled")
    else:
        race_store.update(status="failed")
    return RaceResult(False, race_id, None, None, None)
