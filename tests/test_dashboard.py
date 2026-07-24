import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from lean_loop.dashboard import (
    create_dashboard_server,
    dashboard_snapshot,
    dashboard_task_detail,
)
from lean_loop.jsonutil import atomic_write_json, atomic_write_text
from lean_loop.queue import QueueStore
from lean_loop.state import WorkflowStore


class DashboardTests(unittest.TestCase):
    def _post(
        self,
        base: str,
        path: str,
        value: dict[str, object],
        token: str | None,
    ) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Lean-Agent-Token"] = token
        request = urllib.request.Request(
            base + path,
            data=json.dumps(value).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def _project(self, root: Path) -> tuple[Path, str, str]:
        (root / "lean-toolchain").write_text("fake\n", encoding="utf-8")
        (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
        (root / "Main.lean").write_text(
            "example : True := by trivial\n", encoding="utf-8"
        )
        workflow = WorkflowStore.create(
            project=root,
            target_file="Main.lean",
            task="prove True",
            settings={},
            original_sha256="abc",
        )
        atomic_write_text(workflow.paths.original, "example : True := by trivial\n")
        atomic_write_json(workflow.paths.plan, {"summary": "direct", "steps": []})
        attempt = workflow.paths.attempt_dir(1)
        atomic_write_text(attempt / "candidate.lean", "example : True := by trivial\n")
        atomic_write_json(attempt / "check.json", {"ok": True, "output": "", "returncode": 0})
        atomic_write_json(attempt / "retrieval.json", {"hits": [], "cache": {"hit": True}})
        atomic_write_json(workflow.paths.reviews / "001.json", {"verdict": "accept"})
        atomic_write_json(
            workflow.paths.timings,
            {
                "status": "succeeded",
                "total_seconds": 1.25,
                "phase_seconds": {"lean_check": 0.5},
                "phase_counts": {"lean_check": 1},
                "spans": [],
            },
        )
        workflow.update(
            status="succeeded",
            phase="complete",
            completed_attempt=1,
            attempts=[{"attempt": 1, "check_ok": True, "review_verdict": "accept"}],
            timings={"total_seconds": 1.25},
        )

        queue = QueueStore(root)
        task = queue.add_task(
            target_file="Main.lean", task_text="prove True", settings={}
        )
        queue.claim_next(4242)
        queue.attach_workflow(task["id"], workflow.paths.run_id)
        queue.set_active_process(task["id"], 9090, "lean")
        return root, task["id"], workflow.paths.run_id

    def test_snapshot_and_task_detail_include_live_process_and_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, task_id, run_id = self._project(Path(directory))
            snapshot = dashboard_snapshot(project)
            self.assertEqual(snapshot["active"][0]["active_pid"], 9090)
            self.assertEqual(snapshot["workflows"][0]["run_id"], run_id)
            detail = dashboard_task_detail(project, task_id)
            self.assertEqual(detail["task"]["active_kind"], "lean")
            self.assertIn("by trivial", detail["workflow"]["attempts"][0]["candidate"])
            self.assertEqual(detail["workflow"]["timings"]["total_seconds"], 1.25)

    def test_task_detail_enriches_prover_race_lanes_with_workflow_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, task_id, run_id = self._project(Path(directory))
            queue = QueueStore(project)
            task = queue.get_task(task_id)
            task["settings"]["race"] = {
                "strategy": "first_verified_wins",
                "race_id": "race123",
                "lanes": [
                    {"id": "gpt", "provider": "default"},
                    {"id": "ds", "provider": "deepseek"},
                ],
            }
            queue.update_settings(task_id, task["settings"])
            race_root = project / ".lean-agent" / "races" / "race123"
            lane_workflow = race_root / "lanes" / "gpt" / "workflows" / run_id
            lane_workflow.parent.mkdir(parents=True)
            shutil.copytree(project / ".lean-agent" / "workflows" / run_id, lane_workflow)
            atomic_write_json(
                race_root / "race.json",
                {
                    "race_id": "race123",
                    "status": "running",
                    "updated_at": "2026-07-16T12:00:00+00:00",
                    "winner_lane_id": None,
                    "lanes": [
                        {"id": "gpt", "provider": "default", "status": "running", "phase": "reviewing", "run_id": run_id, "attempt": 1},
                        {"id": "ds", "provider": "deepseek", "status": "queued", "phase": "queued", "run_id": None},
                    ],
                },
            )
            detail = dashboard_task_detail(project, task_id)
            lane = detail["race"]["lanes"][0]
            self.assertEqual(lane["workflow"]["attempt_count"], 1)
            self.assertTrue(lane["workflow"]["latest_check"]["ok"])
            snapshot_task = next(
                row for row in dashboard_snapshot(project)["tasks"] if row["id"] == task_id
            )
            self.assertEqual(
                snapshot_task["race_updated_at"], "2026-07-16T12:00:00+00:00"
            )

    def test_http_api_assets_and_sse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project, task_id, _ = self._project(Path(directory))
            server = create_dashboard_server(project, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    page = response.read()
                    self.assertIn(b"Lean Agent", page)
                    self.assertIn(b'id="taskModel"', page)
                    self.assertIn(b'id="planningMode"', page)
                    self.assertIn(b'id="multiProver"', page)
                    self.assertIn(b'id="configProviderSelect"', page)
                    self.assertIn(b'id="configApiTransport"', page)
                    self.assertIn(b'id="configButton"', page)
                with urllib.request.urlopen(base + "/assets/dashboard.js", timeout=5) as response:
                    script = response.read()
                    self.assertIn(b"EventSource", script)
                    self.assertIn(b'taskModel', script)
                    self.assertIn(b'planning_mode', script)
                    self.assertIn(b'first_verified_wins', script)
                    self.assertIn(b'renderRaceLane', script)
                with urllib.request.urlopen(base + "/api/snapshot", timeout=5) as response:
                    snapshot = json.loads(response.read())
                self.assertEqual(snapshot["active"][0]["active_pid"], 9090)
                self.assertEqual(snapshot["agent_protocol"]["protocol_version"], 1)
                with urllib.request.urlopen(base + "/api/capabilities", timeout=5) as response:
                    capabilities = json.loads(response.read())
                self.assertIn("prover", capabilities["agent_protocol"]["roles"])
                with urllib.request.urlopen(base + f"/api/tasks/{task_id}", timeout=5) as response:
                    detail = json.loads(response.read())
                self.assertEqual(detail["task"]["id"], task_id)
                with urllib.request.urlopen(base + "/api/events", timeout=5) as response:
                    event = response.readline().decode("utf-8").strip()
                    data = response.readline().decode("utf-8").strip()
                self.assertEqual(event, "event: snapshot")
                self.assertIn('"active_pid": 9090', data)
                try:
                    urllib.request.urlopen(base + "/api/workflows/not-valid", timeout=5)
                    self.fail("invalid workflow id should return 404")
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, 404)
                    error.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_control_api_add_cancel_retry_and_start_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "lean-toolchain").write_text("fake\n", encoding="utf-8")
            (project / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
            (project / "Main.lean").write_text(
                "example : True := by trivial\n", encoding="utf-8"
            )
            server = create_dashboard_server(project, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/snapshot", timeout=5) as response:
                    snapshot = json.loads(response.read())
                token = snapshot["control_token"]
                self.assertIn("Main.lean", snapshot["lean_files"])

                configured = self._post(
                    base,
                    "/api/config",
                    {
                        "configuration": {
                            "api_base": "https://relay.example",
                            "model": "relay-gpt-5.6-sol",
                            "api_mode": "responses",
                            "api_transport": "python",
                            "reasoning_effort": "medium",
                            "disable_response_storage": True,
                            "lake": "D:/tools/lake.exe",
                            "timeout_seconds": 600,
                            "max_output_tokens": 8192,
                            "api_timeout_retries": 1,
                            "stream_responses": True,
                            "lsp_local_repair": True,
                            "lsp_local_max_rounds": 3,
                            "lsp_local_max_candidates": 8,
                            "lsp_rg_path": "D:/tools/rg.exe",
                            "lsp_local_validation_timeout_seconds": 90,
                            "lsp_local_total_budget_seconds": 300,
                            "lsp_local_reasoning_effort": "low",
                            "lsp_evidence_budget_seconds": 60,
                        },
                        "api_key": "dashboard-secret",
                    },
                    token,
                )
                self.assertTrue(configured["configuration"]["api_key_configured"])
                self.assertEqual(
                    configured["configuration"]["api_transport"], "python"
                )
                self.assertTrue(
                    configured["configuration"]["lsp_local_repair"]
                )
                self.assertEqual(
                    configured["configuration"]["lsp_local_max_rounds"], 3
                )
                self.assertEqual(
                    configured["configuration"]["lsp_local_max_candidates"], 8
                )
                self.assertEqual(
                    configured["configuration"]["lsp_local_validation_timeout_seconds"], 90
                )
                self.assertEqual(
                    configured["configuration"]["lsp_local_total_budget_seconds"], 300
                )
                self.assertNotIn("dashboard-secret", json.dumps(configured))
                deepseek = self._post(
                    base,
                    "/api/config",
                    {
                        "provider_id": "deepseek",
                        "configuration": {
                            "provider_kind": "deepseek",
                            "api_base": "https://api.deepseek.com",
                            "model": "deepseek-reasoner",
                            "api_mode": "chat-completions",
                            "api_transport": "curl",
                            "reasoning_effort": "high",
                            "timeout_seconds": 600,
                            "max_output_tokens": 8192,
                            "api_timeout_retries": 1,
                            "stream_responses": False,
                        },
                        "api_key": "deepseek-dashboard-secret",
                    },
                    token,
                )
                self.assertIn("deepseek", deepseek["providers"])
                self.assertNotIn("deepseek-dashboard-secret", json.dumps(deepseek))

                with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                    self._post(
                        base,
                        "/api/tasks",
                        {"target_file": "Main.lean", "task_text": "prove True"},
                        None,
                    )
                self.assertEqual(unauthorized.exception.code, 403)
                unauthorized.exception.close()

                added = self._post(
                    base,
                    "/api/tasks",
                    {
                        "target_file": "Main.lean",
                        "task_text": "prove True",
                        "settings": {
                            "model": "relay-gpt-5.6",
                            "max_attempts": 2,
                            "planning_mode": "direct-then-planner",
                            "explain": True,
                        },
                    },
                    token,
                )
                task_id = added["task"]["id"]
                self.assertEqual(added["task"]["state"], "queued")
                stored_settings = QueueStore(project).get_task(task_id)["settings"]
                self.assertTrue(stored_settings["explain"])
                self.assertEqual(stored_settings["model"], "relay-gpt-5.6")
                self.assertEqual(
                    stored_settings["planning_mode"], "direct-then-planner"
                )

                with self.assertRaises(urllib.error.HTTPError) as invalid_mode:
                    self._post(
                        base,
                        "/api/tasks",
                        {
                            "target_file": "Main.lean",
                            "task_text": "prove True",
                            "settings": {"planning_mode": "automatic"},
                        },
                        token,
                    )
                self.assertEqual(invalid_mode.exception.code, 400)
                invalid_mode.exception.close()

                generated = self._post(
                    base,
                    "/api/tasks",
                    {
                        "target_file": "",
                        "task_text": "create and prove a new theorem",
                        "settings": {
                            "max_attempts": 2,
                            "race": {
                                "strategy": "first_verified_wins",
                                "lanes": [
                                    {
                                        "id": "gpt",
                                        "provider": "default",
                                        "model": "relay-gpt-5.6",
                                        "prompt": "use algebra",
                                    },
                                    {
                                        "id": "deepseek",
                                        "provider": "deepseek",
                                        "model": "deepseek-reasoner",
                                        "prompt": "search existing lemmas",
                                    },
                                ],
                            },
                        },
                    },
                    token,
                )
                self.assertTrue(generated["target_created"])
                generated_path = project / generated["task"]["target_file"]
                self.assertTrue(generated_path.is_file())
                generated_settings = QueueStore(project).get_task(
                    generated["task"]["id"]
                )["settings"]
                self.assertEqual(
                    generated_settings["race"]["strategy"],
                    "first_verified_wins",
                )
                self.assertEqual(len(generated_settings["race"]["lanes"]), 2)

                cancelled = self._post(
                    base, f"/api/tasks/{task_id}/cancel", {}, token
                )
                self.assertEqual(cancelled["task"]["state"], "cancelled")
                retried = self._post(
                    base, f"/api/tasks/{task_id}/retry", {}, token
                )
                self.assertEqual(retried["task"]["state"], "queued")

                process = MagicMock()
                process.pid = 777
                process.poll.return_value = None
                with patch("lean_loop.dashboard.subprocess.Popen", return_value=process) as popen:
                    started = self._post(base, "/api/queue/start", {}, token)
                self.assertTrue(started["worker"]["running"])
                self.assertEqual(started["worker"]["pid"], 777)
                self.assertEqual(
                    popen.call_args.kwargs["cwd"],
                    Path(__file__).resolve().parents[1],
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
