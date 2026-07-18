import tempfile
import unittest
from pathlib import Path

from lean_loop.state import WorkflowStore, list_workflows


class WorkflowStateTests(unittest.TestCase):
    def test_create_update_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = WorkflowStore.create(
                project=project,
                target_file="Main.lean",
                task="prove it",
                settings={"max_attempts": 2},
                original_sha256="abc",
            )
            store.update(phase="prove", plan_summary="one step")
            manifest = store.read()
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["phase"], "prove")
            self.assertEqual(list_workflows(project)[0]["run_id"], store.paths.run_id)

    def test_terminal_workflow_cannot_return_to_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            store = WorkflowStore.create(
                project=project,
                target_file="Main.lean",
                task="prove it",
                settings={},
                original_sha256="abc",
            )
            store.update(status="succeeded", phase="complete")
            with self.assertRaises(ValueError):
                store.update(status="running")


if __name__ == "__main__":
    unittest.main()
