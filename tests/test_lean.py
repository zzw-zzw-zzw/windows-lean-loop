import tempfile
import unittest
from pathlib import Path

from lean_loop.lean import ProjectError, resolve_project, resolve_target


class ProjectPathTests(unittest.TestCase):
    def test_resolve_project_and_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lean-toolchain").write_text("leanprover/lean4:v4.0.0\n", encoding="utf-8")
            (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
            (root / "Main.lean").write_text("example : True := by trivial\n", encoding="utf-8")
            project = resolve_project(root)
            self.assertEqual(resolve_target(project, "Main.lean"), root / "Main.lean")

    def test_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lean-toolchain").write_text("x\n", encoding="utf-8")
            (root / "lakefile.toml").write_text("x\n", encoding="utf-8")
            project = resolve_project(root)
            with self.assertRaises(ProjectError):
                resolve_target(project, "../outside.lean")


if __name__ == "__main__":
    unittest.main()
