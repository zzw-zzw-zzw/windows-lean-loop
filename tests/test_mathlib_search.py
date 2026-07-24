import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lean_loop.mathlib_index import build_mathlib_index, index_status, search_index
from lean_loop import mathlib_search
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

    def test_ensure_broad_mathlib_import_preserves_header_and_crlf(self) -> None:
        self.assertTrue(hasattr(mathlib_search, "ensure_broad_mathlib_import"))
        if not hasattr(mathlib_search, "ensure_broad_mathlib_import"):
            return
        source = (
            "prelude\r\n"
            "-- imports stay in their original order\r\n"
            "import Demo.Basic\r\n"
            "import Mathlib.Algebra.Group.Basic\r\n"
            "\r\n"
            "theorem kept : True := by trivial\r\n"
        )
        expected = (
            "prelude\r\n"
            "-- imports stay in their original order\r\n"
            "import Demo.Basic\r\n"
            "import Mathlib.Algebra.Group.Basic\r\n"
            "import Mathlib\r\n"
            "\r\n"
            "theorem kept : True := by trivial\r\n"
        )

        broadened = mathlib_search.ensure_broad_mathlib_import(source)

        self.assertEqual(broadened, expected)
        self.assertEqual(mathlib_search.ensure_broad_mathlib_import(broadened), expected)

    def test_ensure_broad_mathlib_import_follows_prelude_without_imports(self) -> None:
        self.assertTrue(hasattr(mathlib_search, "ensure_broad_mathlib_import"))
        if not hasattr(mathlib_search, "ensure_broad_mathlib_import"):
            return
        source = "prelude\n-- keep this comment\ntheorem kept : True := by trivial\n"

        self.assertEqual(
            mathlib_search.ensure_broad_mathlib_import(source),
            "prelude\nimport Mathlib\n-- keep this comment\n"
            "theorem kept : True := by trivial\n",
        )

    def test_block_comment_import_is_not_a_real_broad_import(self) -> None:
        source = (
            "/-\n"
            "import Mathlib\n"
            "-/\n"
            "\n"
            "import Mathlib.Logic.Basic\n"
            "\n"
            "example : True := by trivial\n"
        )

        self.assertFalse(mathlib_search.has_broad_import(source))
        broadened = mathlib_search.ensure_broad_mathlib_import(source)
        self.assertEqual(
            broadened,
            (
                "/-\n"
                "import Mathlib\n"
                "-/\n"
                "\n"
                "import Mathlib.Logic.Basic\n"
                "import Mathlib\n"
                "\n"
                "example : True := by trivial\n"
            ),
        )
        self.assertTrue(mathlib_search.has_broad_import(broadened))

    def test_reduction_preserves_import_text_inside_nested_block_comments(self) -> None:
        comment = (
            "/- outer\n"
            "  /- nested\n"
            "  import Mathlib\n"
            "  -/\n"
            "-/\n"
        )
        source = comment + "example : True := by trivial\n"
        broadened = mathlib_search.ensure_broad_mathlib_import(source)

        optimized, metadata = optimize_broad_imports(
            broadened,
            {
                "import_suggestions": [
                    {"module": "Mathlib.Logic.Basic", "confidence": "high"}
                ]
            },
        )

        self.assertTrue(metadata["changed"])
        self.assertTrue(optimized.startswith(comment))
        self.assertIn("  import Mathlib\n", optimized)
        self.assertIn("import Mathlib.Logic.Basic\n", optimized)
        self.assertFalse(mathlib_search.has_broad_import(optimized))

    def test_unterminated_header_comment_fails_closed(self) -> None:
        source = "/-\nimport Mathlib\n"

        self.assertFalse(mathlib_search.has_broad_import(source))
        with self.assertRaisesRegex(ValueError, "Cannot safely classify"):
            mathlib_search.ensure_broad_mathlib_import(source)
        optimized, metadata = optimize_broad_imports(
            source,
            {
                "import_suggestions": [
                    {"module": "Mathlib.Logic.Basic", "confidence": "high"}
                ]
            },
        )
        self.assertEqual(optimized, source)
        self.assertEqual(metadata["reason"], "unterminated_import_header_comment")

    def test_optimize_broad_imports_reports_selected_and_only_adds_missing(self) -> None:
        source = (
            "import Demo.Basic\r\n"
            "import Mathlib.Logic.Basic\r\n"
            "import Mathlib\r\n"
            "-- declaration comment\r\n"
            "example : True := by trivial\r\n"
        )
        retrieval = {
            "import_suggestions": [
                {"module": "Mathlib.Logic.Basic", "confidence": "high"},
                {"module": "Mathlib.Analysis.Bounds", "confidence": "high"},
                {"module": "Mathlib.Logic.Basic", "confidence": "high"},
            ]
        }

        optimized, metadata = optimize_broad_imports(source, retrieval)

        self.assertIn("selected_modules", metadata)
        self.assertIn("added_modules", metadata)
        if "selected_modules" not in metadata or "added_modules" not in metadata:
            return
        self.assertEqual(
            metadata["selected_modules"],
            ["Mathlib.Logic.Basic", "Mathlib.Analysis.Bounds"],
        )
        self.assertEqual(metadata["added_modules"], ["Mathlib.Analysis.Bounds"])
        self.assertEqual(optimized.count("import Mathlib.Logic.Basic"), 1)
        self.assertEqual(optimized.count("import Mathlib.Analysis.Bounds"), 1)
        self.assertNotIn("import Mathlib\r\n", optimized)
        self.assertIn("import Demo.Basic\r\n", optimized)
        self.assertNotIn("\n", optimized.replace("\r\n", ""))

    def test_optimize_broad_imports_supports_remove_only_and_no_suggestion(self) -> None:
        source = (
            "import Mathlib.Logic.Basic\n"
            "import Mathlib\n"
            "example : True := by trivial\n"
        )
        remove_only, remove_metadata = optimize_broad_imports(
            source,
            {"import_suggestions": [
                {"module": "Mathlib.Logic.Basic", "confidence": "high"}
            ]},
        )
        unchanged, empty_metadata = optimize_broad_imports(
            source,
            {"import_suggestions": [
                {"module": "Mathlib.Analysis.Bounds", "confidence": "candidate"}
            ]},
        )

        self.assertEqual(
            remove_only,
            "import Mathlib.Logic.Basic\nexample : True := by trivial\n",
        )
        self.assertEqual(remove_metadata["selected_modules"], ["Mathlib.Logic.Basic"])
        self.assertEqual(remove_metadata["added_modules"], [])
        self.assertTrue(remove_metadata["changed"])
        self.assertEqual(unchanged, source)
        self.assertEqual(empty_metadata["selected_modules"], [])
        self.assertEqual(empty_metadata["added_modules"], [])

    def test_optimize_broad_imports_truncates_stably_to_first_twelve(self) -> None:
        modules = [f"Mathlib.Test.Module{index:02d}" for index in range(1, 14)]
        source = "import Mathlib\nexample : True := by trivial\n"

        optimized, metadata = optimize_broad_imports(
            source,
            {
                "import_suggestions": [
                    {"module": module, "confidence": "high"}
                    for module in modules
                ]
            },
        )

        self.assertEqual(metadata["selected_modules"], modules[:12])
        self.assertEqual(metadata["added_modules"], modules[:12])
        self.assertEqual(
            optimized,
            "".join(f"import {module}\n" for module in modules[:12])
            + "example : True := by trivial\n",
        )
        self.assertNotIn(modules[12], optimized)

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
                "import Mathlib\n"
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
