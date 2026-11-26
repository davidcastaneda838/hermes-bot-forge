"""rename_agent: orchestrate `hermes profile rename` and sweep the fork-owned references.

Logic-level tests via rename.rename() against a synthetic root (the hermes CLI call is
mocked — the sweep runs against real temp files), plus the handler's subprocess contract.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import rename as rename_mod, tools, yaml


def fake_rename(root: Path, old: str, new: str):
    """What `hermes profile rename` really does: move the profile dir (state-DB identity
    migration is Hermes-internal and out of scope for the sweep)."""
    src = root / "profiles" / old
    src.rename(root / "profiles" / new)


def rename(root: Path, **spec) -> dict:
    with mock.patch.object(rename_mod.forge, "run",
                           side_effect=lambda r, *args, **kw: fake_rename(Path(r), args[2], args[3])):
        return rename_mod.rename({"hermes_root": str(root), **spec})


def bot(root: Path, name: str, soul: str | None = None) -> Path:
    pdir = root / "profiles" / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "config.yaml").write_text(yaml.safe_dump({"model": {"default": "m", "provider": "p"}}), encoding="utf-8")
    if soul:
        (pdir / "SOUL.md").write_text(soul, encoding="utf-8")
    return pdir


class Guards(unittest.TestCase):
    """Every guard returns before anything is touched — zero side effects."""

    def test_confirm_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            bot(root, "quill")
            out = rename(root, old_name="quill", new_name="scribble", confirm="scribble")
            self.assertFalse(out["ok"])
            self.assertIn("confirm", out["error"])
            self.assertTrue((root / "profiles" / "quill" / "config.yaml").exists())

    def test_unknown_profile_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = rename(Path(tmp) / ".hermes", old_name="ghost", new_name="newname", confirm="ghost")
            self.assertFalse(out["ok"])
            self.assertIn("no live profile", out["error"])

    def test_existing_target_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            bot(root, "quill")
            bot(root, "taken")
            out = rename(root, old_name="quill", new_name="taken", confirm="quill")
            self.assertFalse(out["ok"])
            self.assertIn("already exists", out["error"])
            self.assertTrue((root / "profiles" / "quill" / "config.yaml").exists())

    def test_invalid_slug_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            bot(root, "quill")
            out = rename(root, old_name="quill", new_name="Not A Slug", confirm="quill")
            self.assertFalse(out["ok"])
            self.assertIn("not a valid profile name", out["error"])

    def test_same_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".hermes"
            bot(root, "quill")
            out = rename(root, old_name="quill", new_name="quill", confirm="quill")
            self.assertFalse(out["ok"])
            self.assertIn("differ", out["error"])


class RenameFlow(unittest.TestCase):
    """The happy path against a synthetic root: mocked hermes CLI, real sweep files."""

    def _scene(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        root = Path(tmp) / ".hermes"
        lead = bot(root, "lead")
        (lead / "memories").mkdir()
        (lead / "memories" / "MEMORY.md").write_text(
            "I lead the crew team. My team and what each one does:\n"
            "- @quill (Quill): writes the drafts\n"
            "- @scout (Scout): research\n"
            "I delegate to them by name instead of doing their work myself.\n", encoding="utf-8")
        soul = bot(root, "quill", soul="# Quill — writer\n\nYou are **Quill**, the writer of this "
                                        "Hermes deployment (profile `quill`). Always introduce yourself as Quill.\n"
                                        "\nWrites terse prose.\n")
        (soul / "profile.yaml").write_text(
            yaml.safe_dump({"ui_meta": {"hermes-bots": {"title": "Quill"}}}), encoding="utf-8")
        exports = root / "profile-exports"
        exports.mkdir()
        template = exports / "quill.botforge.json"
        template.write_text(json.dumps({"display_name": "Quill", "role": "writer",
                                        "soul_md": "You are **Quill**, the writer (profile `quill`).",
                                        "persona": "Terse prose by Quill."}), encoding="utf-8")
        return root, lead, soul, template

    def test_end_to_end_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, lead, soul, template = self._scene(Path(tmp))
            out = rename(root, old_name="quill", new_name="scribble", confirm="quill")
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["renamed"], {"from": "quill", "to": "scribble"})
            self.assertIn(str(template), out["swept"])
            self.assertFalse((root / "profiles" / "quill").exists())
            self.assertTrue((root / "profiles" / "scribble" / "config.yaml").exists())
            # Roster bullet: the member's name changes, the other one does not.
            mem = (lead / "memories" / "MEMORY.md").read_text(encoding="utf-8")
            self.assertIn("- @scribble (Quill): writes the drafts", mem)
            self.assertIn("- @scout (Scout): research", mem)
            self.assertNotIn("@quill", mem)
            # SOUL.md identity: display title kept, profile id updated, no stacking.
            text = (root / "profiles" / "scribble" / "SOUL.md").read_text(encoding="utf-8")
            self.assertIn("# Quill — writer", text)
            self.assertIn("You are **Quill**", text)
            self.assertIn("profile `scribble`", text)
            self.assertEqual(text.count("You are **"), 1)
            # Template: profile-id references follow the rename; the display title stays.
            tpl = json.loads(template.read_text(encoding="utf-8"))
            self.assertEqual(tpl["display_name"], "Quill")
            self.assertNotIn("`quill`", json.dumps(tpl))
            self.assertIn("Terse prose by Quill.", tpl["persona"])

    def test_a_sweep_failure_rolls_back_every_edit_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, lead, _, _ = self._scene(Path(tmp))
            before = (lead / "memories" / "MEMORY.md").read_text(encoding="utf-8")
            real = rename_mod.forge.ensure_identity
            with mock.patch.object(rename_mod.forge, "ensure_identity",
                                   side_effect=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))), \
                 mock.patch.object(rename_mod.forge, "run",
                                   side_effect=lambda r, *args, **kw: fake_rename(Path(r), args[2], args[3])):
                out = rename_mod.rename({"hermes_root": str(root), "old_name": "quill",
                                         "new_name": "scribble", "confirm": "quill"})
            self.assertFalse(out["ok"])
            self.assertIn("sweep failed", out["error"])
            self.assertIn("boom", out["error"])
            self.assertEqual((lead / "memories" / "MEMORY.md").read_text(encoding="utf-8"), before)
            self.assertEqual(real, rename_mod.forge.ensure_identity)

    def test_journal_failure_does_not_fail_the_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = self._scene(Path(tmp))
            with mock.patch.object(rename_mod.journal, "journaling_enabled", side_effect=OSError("disk")):
                out = rename(root, old_name="quill", new_name="scribble", confirm="quill")
            self.assertTrue(out["ok"])
            self.assertFalse(out["journal"]["written"])

    def test_journal_entry_is_written_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _, _ = self._scene(Path(tmp))
            rename_mod.journal.enable_journal(root / "profiles" / "quill")
            out = rename(root, old_name="quill", new_name="scribble", confirm="quill")
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["journal"].get("written", out["journal"].get("ok", False)))
            folder = root / "profiles" / "scribble" / "journal"
            self.assertTrue(folder.is_dir() and any(folder.glob("*.md")))


class HandlerContract(unittest.TestCase):
    """tools.rename_agent only wraps the subprocess. Regression class: the fork_ops bug
    was passing both input= and stdin= (a ValueError); the child env must keep PYTHONPATH
    so `import forge` works under the gateway."""

    def _run(self, env_patch=None, **patch):
        env = dict(os.environ)
        env.update(env_patch or {})
        with mock.patch.dict(tools.os.environ, env, clear=True), \
             mock.patch.object(tools, "hermes_root", return_value=Path("root")), \
             mock.patch.object(tools.subprocess, "run", **patch) as run:
            if not {"stdout", "stderr", "return_value", "side_effect"} & set(patch):
                run.return_value = mock.Mock(stdout='{"ok": true}', stderr="", returncode=0)
            out = json.loads(tools.rename_agent({"old_name": "a", "new_name": "b", "confirm": "a"}))
        return out, run

    def test_run_gets_input_without_stdin_and_the_env_allowslist(self):
        out, run = self._run(env_patch={"PYTHONPATH": "F:/site-packages"})
        self.assertTrue(out["ok"])
        kwargs = run.call_args.kwargs
        self.assertIn("input", kwargs)
        self.assertNotIn("stdin", kwargs)
        # Regression (77815b4): the gateway resolves PyYAML via PYTHONPATH; stripping it
        # breaks `import forge` in the child. Whatever the parent env carries passes through.
        self.assertEqual(kwargs["env"].get("PYTHONPATH"), "F:/site-packages")
        self.assertEqual(kwargs["cwd"], str(tools.PLUGIN_DIR))
        self.assertIn("rename.py", str(run.call_args.args[0][1]))

    def test_a_timeout_is_a_reason_not_a_traceback(self):
        out, _ = self._run(side_effect=subprocess.TimeoutExpired(cmd="rename.py", timeout=600))
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["error"])

    def test_output_that_is_not_json_comes_back_as_the_error(self):
        out, _ = self._run(return_value=mock.Mock(stdout="", stderr="Traceback: RuntimeError", returncode=1))
        self.assertFalse(out["ok"])
        self.assertIn("RuntimeError", out["error"])


if __name__ == "__main__":
    unittest.main()
