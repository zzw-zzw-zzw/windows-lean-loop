from __future__ import annotations

import unittest

from lean_loop.audit import audit_source, declarations


class SourceAuditTests(unittest.TestCase):
    def test_allows_proof_rewrite_but_freezes_existing_statement(self) -> None:
        baseline = "theorem kept (x : Nat) : x = x := by rfl\n"
        rewritten = "theorem kept (x : Nat) : x = x := by simp\n"
        changed = "theorem kept (x : Nat) : x + 0 = x := by simp\n"
        self.assertTrue(audit_source(baseline, rewritten)["ok"])
        result = audit_source(baseline, changed)
        self.assertFalse(result["ok"])
        self.assertIn("statement changed", result["violations"][0])

    def test_rejects_new_axioms_and_final_placeholders(self) -> None:
        baseline = "theorem goal : True := by trivial\n"
        axiom_candidate = baseline + "axiom shortcut : False\n"
        self.assertFalse(audit_source(baseline, axiom_candidate)["ok"])
        sorry_candidate = "theorem goal : True := by sorry\n"
        self.assertFalse(audit_source(baseline, sorry_candidate, final=True)["ok"])
        sorry_ax_candidate = "theorem goal : True := by exact sorryAx True true\n"
        self.assertFalse(audit_source(baseline, sorry_ax_candidate, final=True)["ok"])

    def test_ignores_forbidden_words_in_comments_and_strings(self) -> None:
        source = (
            '-- sorry axiom hidden\n'
            'def message : String := "admit"\n'
            'theorem goal : True := by trivial\n'
        )
        self.assertTrue(audit_source(source, source, final=True)["ok"])

    def test_requires_exact_formal_goal_declaration(self) -> None:
        required = "theorem generated_goal (n : ℕ) : n = n"
        accepted = audit_source(
            "-- empty\n",
            "theorem generated_goal (n : ℕ) : n = n := by rfl\n",
            required_declaration=required,
        )
        changed = audit_source(
            "-- empty\n",
            "theorem generated_goal (n : ℕ) : True := by trivial\n",
            required_declaration=required,
        )
        self.assertTrue(accepted["ok"])
        self.assertFalse(changed["ok"])

    def test_requires_plan_step_declarations_by_name(self) -> None:
        missing = audit_source(
            "-- empty\n",
            "lemma other : True := by trivial\n",
            required_declaration_names=["helper"],
        )
        accepted = audit_source(
            "-- empty\n",
            "lemma helper : True := by trivial\n",
            required_declaration_names=["helper"],
        )
        self.assertFalse(missing["ok"])
        self.assertIn("Required Plan declaration is missing: helper", missing["violations"])
        self.assertTrue(accepted["ok"])

    def test_declarations_qualify_single_and_nested_namespaces(self) -> None:
        source = (
            "namespace Outer\n"
            "theorem first : True := by trivial\n"
            "namespace Inner\n"
            "lemma second : True := by trivial\n"
            "end Inner\n"
            "end Outer\n"
        )
        self.assertEqual(
            [row.name for row in declarations(source)],
            ["Outer.first", "Outer.Inner.second"],
        )

    def test_namespace_scan_ignores_comments_and_strings(self) -> None:
        source = (
            "-- namespace LineComment\n"
            "/- namespace BlockComment\n"
            "end BlockComment -/\n"
            'def message : String := "namespace StringValue\\nend StringValue"\n'
            "namespace Real\n"
            "theorem goal : True := by trivial\n"
            "end Real\n"
        )
        self.assertEqual(
            [row.name for row in declarations(source)],
            ["message", "Real.goal"],
        )

    def test_qualified_required_name_rejects_wrong_namespace(self) -> None:
        accepted = audit_source(
            "-- empty\n",
            "namespace Expected\ntheorem goal : True := by trivial\nend Expected\n",
            required_declaration_names=["Expected.goal"],
        )
        rejected = audit_source(
            "-- empty\n",
            "namespace Other\ntheorem goal : True := by trivial\nend Other\n",
            required_declaration_names=["Expected.goal"],
        )
        self.assertTrue(accepted["ok"])
        self.assertFalse(rejected["ok"])
        self.assertIn(
            "Required Plan declaration is missing: Expected.goal",
            rejected["violations"],
        )

    def test_unqualified_required_name_requires_unique_local_name(self) -> None:
        duplicated_local_name = (
            "namespace One\nlemma helper : True := by trivial\nend One\n"
            "namespace Two\nlemma helper : True := by trivial\nend Two\n"
        )
        unique = audit_source(
            "-- empty\n",
            "namespace Only\nlemma helper : True := by trivial\nend Only\n",
            required_declaration_names=["helper"],
        )
        ambiguous = audit_source(
            "-- empty\n",
            duplicated_local_name,
            required_declaration_names=["helper"],
        )
        qualified = audit_source(
            "-- empty\n",
            duplicated_local_name,
            required_declaration_names=["Two.helper"],
        )
        root = audit_source(
            "-- empty\n",
            "lemma helper : True := by trivial\n",
            required_declaration_names=["helper"],
        )
        self.assertTrue(unique["ok"])
        self.assertFalse(ambiguous["ok"])
        self.assertTrue(qualified["ok"])
        self.assertTrue(root["ok"])

    def test_issue_5_namespace_qualified_required_declaration(self) -> None:
        required = ["Stage0Calibration.mathd_algebra_109"]
        accepted = audit_source(
            "-- empty\n",
            "namespace Stage0Calibration\n"
            "theorem mathd_algebra_109 : True := by trivial\n"
            "end Stage0Calibration\n",
            required_declaration_names=required,
        )
        wrong_namespace = audit_source(
            "-- empty\n",
            "namespace Decoy\n"
            "theorem mathd_algebra_109 : True := by trivial\n"
            "end Decoy\n",
            required_declaration_names=required,
        )
        self.assertTrue(accepted["ok"])
        self.assertFalse(wrong_namespace["ok"])


if __name__ == "__main__":
    unittest.main()
