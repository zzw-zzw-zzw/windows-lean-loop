from __future__ import annotations

import unittest

from lean_loop.cli import _parser


class SubscriptionCliTests(unittest.TestCase):
    def test_direct_remains_default_for_all_entrypoints(self) -> None:
        parser = _parser()
        cases = (
            ["doctor", "--project", "."],
            ["api-check"],
            [
                "workflow", "run", "--project", ".", "--file", "Main.lean",
                "--task", "prove",
            ],
            ["workflow", "resume", "--project", ".", "--run-id", "run"],
            ["queue", "add", "--project", ".", "--task", "prove"],
        )
        expected = ("direct", "direct", "direct", None, "direct")
        self.assertEqual(
            tuple(parser.parse_args(case).agent_backend for case in cases),
            expected,
        )

    def test_subscription_backends_are_explicit_choices(self) -> None:
        args = _parser().parse_args(
            [
                "workflow", "run", "--project", ".", "--file", "Main.lean",
                "--task", "prove", "--agent-backend", "claude-subscription",
                "--model", "claude-sonnet-5",
            ]
        )
        self.assertEqual(args.agent_backend, "claude-subscription")
        self.assertEqual(args.model, "claude-sonnet-5")

    def test_planner_is_the_default_proof_planning_mode(self) -> None:
        parser = _parser()
        run = parser.parse_args([
            "workflow", "run", "--project", ".", "--file", "Main.lean",
            "--task", "prove",
        ])
        resume = parser.parse_args([
            "workflow", "resume", "--project", ".", "--run-id", "run",
        ])
        queued = parser.parse_args([
            "queue", "add", "--project", ".", "--task", "prove",
        ])

        self.assertEqual(run.planning_mode, "planner")
        self.assertIsNone(resume.planning_mode)
        self.assertEqual(queued.planning_mode, "planner")


if __name__ == "__main__":
    unittest.main()
