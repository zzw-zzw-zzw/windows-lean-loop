from __future__ import annotations

import unittest

from lean_loop.audit import audit_source


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


if __name__ == "__main__":
    unittest.main()
