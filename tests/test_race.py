import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.jsonutil import atomic_write_json
from lean_loop.lean import LeanCheck
from lean_loop.process_control import ProcessCancelled
from lean_loop.race import LaneExecutionResult, run_prover_race


class ProverRaceTests(unittest.TestCase):
    def _project(self, root: Path) -> tuple[Path, Path]:
        target = root / "Main.lean"
        target.write_text("example : True := by exact missing\n", encoding="utf-8")
        (root / ".lean-agent").mkdir()
        return root, target

    def test_first_verified_lane_wins_and_cancels_other_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory))
            worktrees: dict[str, Path] = {}

            def prepare(project: Path, race_id: str, lane_id: str) -> Path:
                worktree = project / "lane-worktrees" / lane_id
                worktree.mkdir(parents=True, exist_ok=True)
                (worktree / "Main.lean").write_text(
                    target.read_text(encoding="utf-8"), encoding="utf-8"
                )
                worktrees[lane_id] = worktree
                return worktree

            def lane_runner(lane_project, lane_target, lane, resume_run_id, controller):
                lane_id = lane["id"]
                if lane_id == "fast":
                    lane_target.write_text(
                        "example : True := by trivial\n", encoding="utf-8"
                    )
                    run_id = "20260715T100000000000Z"
                    workflow = lane_project / ".lean-agent" / "workflows" / run_id
                    workflow.mkdir(parents=True)
                    atomic_write_json(workflow / "run.json", {"status": "succeeded"})
                    return LaneExecutionResult(
                        True, run_id, LeanCheck(True, 0, "", ("lean",))
                    )
                for _ in range(100):
                    if controller.cancel_requested():
                        raise ProcessCancelled("winner selected")
                    time.sleep(0.005)
                return LaneExecutionResult(False, None, None, "slow lane timed out")

            def main_checker(project, target, timeout, lake, controller):
                return LeanCheck(
                    "by trivial" in target.read_text(encoding="utf-8"),
                    0,
                    "",
                    ("lean",),
                )

            with patch("lean_loop.race.prepare_lane_worktree", side_effect=prepare), patch(
                "lean_loop.race.cleanup_lane_worktree"
            ):
                result = run_prover_race(
                    project=project,
                    task_id="task-1",
                    target_file="Main.lean",
                    task_text="prove True",
                    lane_specs=[
                        {"id": "fast", "provider": "default"},
                        {"id": "slow", "provider": "deepseek"},
                    ],
                    lean_timeout_seconds=30,
                    lake_executable="lake",
                    task_cancelled=lambda: False,
                    lane_runner=lane_runner,
                    main_checker=main_checker,
                    race_id="race-first",
                )
            self.assertTrue(result.ok)
            self.assertEqual(result.winner_lane_id, "fast")
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "example : True := by trivial\n",
            )
            state = json.loads(
                (project / ".lean-agent" / "races" / "race-first" / "race.json").read_text(
                    encoding="utf-8"
                )
            )
            statuses = {lane["id"]: lane["status"] for lane in state["lanes"]}
            self.assertEqual(statuses["fast"], "succeeded")
            self.assertEqual(statuses["slow"], "cancelled_by_winner")

    def test_failed_lanes_resume_their_own_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, target = self._project(Path(directory))
            calls: list[tuple[str, str | None]] = []

            def prepare(project: Path, race_id: str, lane_id: str) -> Path:
                worktree = project / "lane-worktrees" / lane_id
                worktree.mkdir(parents=True, exist_ok=True)
                lane_target = worktree / "Main.lean"
                if not lane_target.exists():
                    lane_target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                return worktree

            def failed_runner(lane_project, lane_target, lane, resume_run_id, controller):
                calls.append((lane["id"], resume_run_id))
                return LaneExecutionResult(False, f"run-{lane['id']}", None, "failed")

            with patch("lean_loop.race.prepare_lane_worktree", side_effect=prepare):
                first = run_prover_race(
                    project=project,
                    task_id="task-2",
                    target_file="Main.lean",
                    task_text="prove True",
                    lane_specs=[{"id": "a"}, {"id": "b"}],
                    lean_timeout_seconds=30,
                    lake_executable="lake",
                    task_cancelled=lambda: False,
                    lane_runner=failed_runner,
                    main_checker=lambda *args: LeanCheck(True, 0, "", ()),
                    race_id="race-resume",
                )
                self.assertFalse(first.ok)
                second = run_prover_race(
                    project=project,
                    task_id="task-2",
                    target_file="Main.lean",
                    task_text="prove True",
                    lane_specs=[{"id": "a"}, {"id": "b"}],
                    lean_timeout_seconds=30,
                    lake_executable="lake",
                    task_cancelled=lambda: False,
                    lane_runner=failed_runner,
                    main_checker=lambda *args: LeanCheck(True, 0, "", ()),
                    race_id="race-resume",
                )
            self.assertFalse(second.ok)
            self.assertIn(("a", "run-a"), calls)
            self.assertIn(("b", "run-b"), calls)


if __name__ == "__main__":
    unittest.main()
