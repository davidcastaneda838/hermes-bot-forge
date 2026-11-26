"""waiting_on: the read-only "what is waiting on you" tool.

The handler shells out to waiting.py, so the failure paths are the same kind the other
subprocess handlers have: a timeout, output that is not JSON. The script side is tested
directly against a synthetic journal — blocked, resolved, and empty all have to come back
as one JSON object, never a traceback.
"""
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from support import make_root, tools, waiting

BLOCKED_DAY = (
    "# Journal\n\n"
    "## 2026-11-25T09:00:00+00:00 · blocked · needs API key\n\n"
    "The render job cannot reach the model without a key.\n"
)


def _bot_with_journal(root: Path, name: str = "quill", body: str = BLOCKED_DAY):
    pdir = root / "profiles" / name
    (pdir / "journal").mkdir(parents=True, exist_ok=True)
    (pdir / "journal" / "2026-11-25.md").write_text(body, encoding="utf-8")


class Handler(unittest.TestCase):
    """tools.waiting_on only wraps the subprocess; mock it, judge the wrapping."""

    def _run(self, **patch):
        with mock.patch.object(tools, "hermes_root", return_value=Path("root")), \
             mock.patch.object(tools.subprocess, "run", **patch) as run:
            out = json.loads(tools.waiting_on({}))
        return out, run

    def test_a_result_from_the_script_is_passed_through(self):
        out, run = self._run(return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "items": [], "scanned": 3}), ""))
        self.assertTrue(out["ok"])
        self.assertEqual(out["scanned"], 3)
        self.assertIn("waiting.py", str(run.call_args.args[0]))
        self.assertEqual(run.call_args.args[0][-1], "-")
        self.assertEqual(run.call_args.kwargs.get("cwd"), str(tools.PLUGIN_DIR))

    def test_the_subprocess_runs_clean_and_read_only(self):
        out, run = self._run(return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"ok": True, "items": [], "scanned": 0}), ""))
        env = run.call_args.kwargs["env"]
        self.assertNotIn("HERMES_ANYTHING", env)
        self.assertEqual(env.get("PYTHONIOENCODING"), "utf-8")

    def test_output_that_is_not_json_comes_back_as_the_error(self):
        out, _ = self._run(return_value=subprocess.CompletedProcess(
            [], 1, "", "Traceback (most recent call last):\n  RuntimeError: boom"))
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])

    def test_a_timeout_is_a_reason_not_a_traceback(self):
        out, _ = self._run(side_effect=subprocess.TimeoutExpired(cmd="waiting.py", timeout=120))
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])

    def test_silence_is_reported_rather_than_read_as_success(self):
        out, _ = self._run(return_value=subprocess.CompletedProcess([], 0, "", ""))
        self.assertFalse(out["ok"])


class Script(unittest.TestCase):
    """waiting.scan/main against a synthetic journal fixture."""

    def test_a_blocked_entry_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), profiles=("quill", "noop"))
            _bot_with_journal(root, "quill")
            out = waiting.scan({"hermes_root": str(root)})
            self.assertTrue(out["ok"])
            self.assertEqual(out["scanned"], 2)
            self.assertEqual(len(out["items"]), 1)
            item = out["items"][0]
            self.assertEqual(item["bot"], "quill")
            self.assertEqual(item["severity"], "blocked")
            self.assertIn("2026-11-25", item["date"])
            self.assertIn("cannot reach the model", item["excerpt"])

    def test_a_completed_entry_resolves_the_blocked_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), profiles=("quill",))
            _bot_with_journal(root, body=BLOCKED_DAY +
                              "## 2026-11-26T08:00:00+00:00 · completed · needs API key\n\nkey set\n")
            out = waiting.scan({"hermes_root": str(root)})
            self.assertEqual(out["items"], [])

    def test_nothing_to_scan_is_ok_with_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            out = waiting.scan({"hermes_root": str(root)})
            self.assertEqual(out, {"ok": True, "items": [], "scanned": 0})

    def test_bad_input_is_an_error_object_not_a_traceback(self):
        buf = io.StringIO()
        with mock.patch.object(waiting.sys, "argv", ["waiting.py", "-"]), \
             mock.patch.object(waiting.sys, "stdin", io.StringIO("{not json")), \
             redirect_stdout(buf):
            code = waiting.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("not valid JSON", out["error"])

    def test_scan_errors_come_back_as_ok_false(self):
        with mock.patch.object(waiting, "scan", side_effect=RuntimeError("disk gone")), \
             mock.patch.object(waiting.sys, "argv", ["waiting.py", "-"]), \
             mock.patch.object(waiting.sys, "stdin", io.StringIO("{}")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = waiting.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("disk gone", out["error"])


if __name__ == "__main__":
    unittest.main()
