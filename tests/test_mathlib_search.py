import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.mathlib_index import build_mathlib_index, index_status, search_index
from lean_loop.mathlib_search import (
    collect_retrieval,
    diagnostic_queries,
    import_validation_diagnostics,
    optimize_broad_imports,
    repair_invalid_mathlib_imports,
    search_mathlib,
    source_search_terms,
    suggest_imports,
    validate_mathlib_imports,
)


class MathlibSearchTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        source = root / ".lake" / "packages" / "mathlib" / "Mathlib" / "Analysis"
        source.mkdir(parents=True)
        (source / "Bounds.lean").write_text(
            "namespace Real\n\ntheorem pi_gt_three : 3 < pi := by sorry\n",
            encoding="utf-8",
        )
        init = root / ".lake" / "packages" / "mathlib" / "Mathlib" / "Init"
        init.mkdir(parents=True)
        (init / "Internal.lean").write_text(
            "theorem internal_only : True := by trivial\n", encoding="utf-8"
        )
        tactic = root / ".lake" / "packages" / "mathlib" / "Mathlib" / "Tactic" / "Demo"
        tactic.mkdir(parents=True)
        (tactic / "Core.lean").write_text(
            'syntax (name := demo_tac) "demo_tac" : tactic\n', encoding="utf-8"
        )
        (tactic.parent / "Demo.lean").write_text(
            "import Mathlib.Tactic.Demo.Core\n", encoding="utf-8"
        )
        (root / "lean-toolchain").write_text("fake-toolchain\n", encoding="utf-8")
        return root

    def test_python_fallback_returns_module_and_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            with patch("lean_loop.mathlib_search.shutil.which", return_value=None):
                hits = search_mathlib(project, "pi_gt_three", 5)
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0].module, "Mathlib.Analysis.Bounds")
            self.assertEqual(hits[0].line, 3)

    def test_diagnostics_drive_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            diagnostics = "Unknown constant `Real.pi_gt_three`"
            self.assertEqual(diagnostic_queries(diagnostics), ["pi_gt_three"])
            with patch("lean_loop.mathlib_search.shutil.which", return_value=None):
                result = collect_retrieval(project, diagnostics=diagnostics)
            self.assertEqual(result["queries"], ["pi_gt_three"])
            self.assertEqual(result["hits"][0]["module"], "Mathlib.Analysis.Bounds")

    def test_sqlite_index_cache_and_import_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            built = build_mathlib_index(project)
            self.assertGreaterEqual(built.symbols, 2)
            self.assertTrue(index_status(project)["valid"])

            indexed = search_index(project, "Real.pi_gt_three", 5)
            self.assertEqual(indexed[0].module, "Mathlib.Analysis.Bounds")
            with patch(
                "lean_loop.mathlib_search._search_with_python",
                side_effect=AssertionError("index should avoid Python scan"),
            ):
                first = collect_retrieval(
                    project, diagnostics="Unknown constant `Real.pi_gt_three`"
                )
                second = collect_retrieval(
                    project, diagnostics="Unknown constant `Real.pi_gt_three`"
                )
            self.assertFalse(first["cache"]["hit"])
            self.assertTrue(second["cache"]["hit"])
            self.assertEqual(first["search_backend"], "sqlite-index")
            self.assertEqual(
                first["import_suggestions"][0]["module"],
                "Mathlib.Analysis.Bounds",
            )

            suggestions = suggest_imports(
                [
                    {
                        "query": "internal_only",
                        "module": "Mathlib.Init.Internal",
                        "path": "x",
                        "line": 1,
                        "snippet": "theorem internal_only",
                        "match": "exact",
                    }
                ]
            )
            self.assertEqual(suggestions, [])

            tactic_hits = search_mathlib(project, "demo_tac", 5)
            tactic_suggestions = suggest_imports(tactic_hits)
            self.assertEqual(
                tactic_suggestions[0]["module"], "Mathlib.Tactic.Demo"
            )

    def test_source_terms_optimize_broad_import_without_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            build_mathlib_index(project)
            source = "import Mathlib\nexample : 3 < Real.pi := by exact Real.pi_gt_three\n"
            terms = source_search_terms(source, "repair the existing sorry proof")
            self.assertIn("Real.pi_gt_three", terms)
            result = collect_retrieval(project, diagnostics="", requested_terms=terms)
            optimized, metadata = optimize_broad_imports(source, result)
            self.assertTrue(metadata["changed"])
            self.assertIn("import Mathlib.Analysis.Bounds", optimized)
            self.assertNotIn("import Mathlib\n", optimized)

    def test_invalid_mathlib_import_reports_similar_local_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = self._project(Path(directory))
            module = (
                project
                / ".lake"
                / "packages"
                / "mathlib"
                / "Mathlib"
                / "Analysis"
                / "Normed"
                / "Module"
                / "FiniteDimension.lean"
            )
            module.parent.mkdir(parents=True)
            module.write_text("theorem marker : True := by trivial\n", encoding="utf-8")
            build_mathlib_index(project, force=True)

            validation = validate_mathlib_imports(
                project,
                "import Mathlib.Analysis.NormedSpace.FiniteDimension\n",
            )

            self.assertFalse(validation["ok"])
            suggestions = validation["invalid"][0]["suggestions"]
            self.assertEqual(
                suggestions[0]["module"],
                "Mathlib.Analysis.Normed.Module.FiniteDimension",
            )
            self.assertIn(
                "Do not run lake build",
                import_validation_diagnostics(validation),
            )
            repaired, metadata = repair_invalid_mathlib_imports(
                project,
                "import Mathlib.Analysis.NormedSpace.FiniteDimension\n",
            )
            self.assertTrue(metadata["changed"])
            self.assertIn(
                "import Mathlib.Analysis.Normed.Module.FiniteDimension",
                repaired,
            )


if __name__ == "__main__":
    unittest.main()
