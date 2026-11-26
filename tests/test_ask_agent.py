"""ask_agent classification: what a `chat -Q` child actually did. The fixtures are real
captured outputs from live runs (2026-11-26), so the classifier is tested against what
Hermes actually prints, not invented strings."""
import json
import unittest

from support import tools


CONTROL_OK = (
    "\x1b[32m✓ Worktree created:\x1b[0m F:\\Projects\\bot-forge-practice\\.worktrees\\hermes-3871108a\n"
    + "\x1b[32m✓ Worktree cleaned up:\x1b[0m F:\\Projects\\bot-forge-practice\\.worktrees\\hermes-3871108a\n"
    + "\U0001f4ac CONTROL-OK\n")
SESSION_ID_ERR = "\n\nsession_id: local-c4d53bbcafb8224cf916870093efe163fe000588c6ed6d304f48402f6b493bcb"
EXHAUSTED_OUT = (
    "\n".join([
        "- 'echo step1' — not run",
        "- 'echo step2' — not run",
        "",
        "I hit the iteration limit before finding an alternate permitted path.",
        "\u2713 Worktree cleaned up: F:\\Projects\\bot-forge-practice\\.worktrees\\hermes-a2f3e96d",
    ]) + "\n")


class ClassifyOk(unittest.TestCase):
    def test_control_success_is_ok_and_clean(self):
        v = tools._classify_ask(0, CONTROL_OK, SESSION_ID_ERR)
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["reply"], "\U0001f4ac CONTROL-OK")  # scaffolding stripped

    def test_real_error_drops_session_id_junk(self):
        stderr = SESSION_ID_ERR + "\nhermes: error: unrecognized arguments: x"
        v = tools._classify_ask(2, "", stderr)
        self.assertEqual(v["status"], "failed")
        self.assertNotIn("session_id", v["error"])
        self.assertIn("unrecognized arguments", v["error"])

    def test_exhaustion_is_turn_budget_with_partial_reply(self):
        v = tools._classify_ask(1, EXHAUSTED_OUT, SESSION_ID_ERR)
        self.assertEqual(v["status"], "turn_budget")
        self.assertTrue(v["exhausted"])
        self.assertIn("iteration limit", v["partial_reply"])
        self.assertNotIn("Worktree", v["partial_reply"])

    def test_exhaustion_marker_in_success_prose_is_not_turn_budget(self):
        # rc 0: the model merely TALKS about limits — a normal ok, not exhaustion.
        out = "The default max-turns is 30, which suits most asks.\n"
        v = tools._classify_ask(0, out, "")
        self.assertEqual(v["status"], "ok")
        self.assertIn("max-turns", v["reply"])

    def test_zero_exit_but_no_reply_is_empty_not_ok(self):
        v = tools._classify_ask(0, "  \n", "")
        self.assertEqual(v["status"], "empty")

    def test_nonzero_exit_without_markers_is_failed(self):
        v = tools._classify_ask(2, "some stdout", SESSION_ID_ERR)
        self.assertEqual(v["status"], "failed")

    def test_empty_stderr_failure_falls_back_to_stdout(self):
        v = tools._classify_ask(3, "boom: something broke\n", "")
        self.assertEqual(v["status"], "failed")
        self.assertIn("something broke", v["error"])


class MaxTurnsValidation(unittest.TestCase):
    def test_bad_max_turns_is_refused_before_any_spawn(self):
        from unittest import mock
        with mock.patch.object(tools, "hermes_root", return_value=__import__("pathlib").Path(".")):
            out = json.loads(tools.ask_agent({"name": "x", "message": "hi", "max_turns": 0}))
            self.assertFalse(out["ok"])
            self.assertIn("1..300", out["error"])
            out = json.loads(tools.ask_agent({"name": "x", "message": "hi", "max_turns": "ten"}))
            self.assertFalse(out["ok"])
            self.assertIn("1..300", out["error"])


if __name__ == "__main__":
    unittest.main()
