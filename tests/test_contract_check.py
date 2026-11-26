"""contract_check: the baseline-diff sentinel. Logic-level tests on a synthetic hermes
root: all five contracts PASS on an intact fixture, each mutation goes CHANGED with the
top-level drift summary, and a missing baseline is a clean refusal."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from support import ROOT, contract_check, tools  # noqa: F401,E402  (support arranges sys.path)

BOT_META = {"title": "Bot", "description": "d", "imageKind": "shape",
            "shape": "blobatar:abc:def", "created": 1700000000000}


def make_contract_root(base: Path) -> Path:
    """A hermes root that satisfies every probed contract."""
    root = base / ".hermes"
    (root / "profiles" / "sample").mkdir(parents=True)
    (root / "cron").mkdir()
    (root / ".env").write_text(
        "EMAIL_ADDRESS=a@b.c\nEMAIL_PASSWORD=pw\nEMAIL_SMTP_HOST=smtp\n"
        "EMAIL_SMTP_PORT=587\nEMAIL_HOME_ADDRESS=h@b.c\n", encoding="utf-8")
    cfg = {"model": {"default": "m", "provider": "p"},
           "platform_toolsets": {"cli": ["terminal", "code_execution"]}}
    (root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (root / "profiles" / "sample" / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    for home in (root, root / "profiles" / "sample"):
        (home / "profile.yaml").write_text(
            yaml.safe_dump({"ui_meta": {"hermes-bots": dict(BOT_META)},
                            "_ui_meta_revisions": {"hermes-bots": 1}}), encoding="utf-8")
        make_state_db(home)
        (home / "cron").mkdir(exist_ok=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    return root


def make_state_db(home: Path, columns=("id", "title", "started_at")):
    con = sqlite3.connect(str(home / "state.db"))
    con.execute(f"create table sessions ({', '.join(columns)})")
    con.commit()
    con.close()


def run(root: Path, op: str) -> dict:
    return contract_check.contract_check({"op": op, "hermes_root": str(root)})


def statuses(out: dict) -> dict:
    return {entry["contract"]: entry["status"] for entry in out["contracts"]}


class IntactFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_contract_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_baseline_records_shapes_and_passes_check(self):
        out = run(self.root, "baseline")
        self.assertTrue(out["ok"])
        self.assertTrue(out["path"].endswith("contract_baseline.json"))
        record = json.loads((self.root / ".bot-forge" / "contract_baseline.json").read_text())
        self.assertEqual(set(record["contracts"]), set(contract_check.PROBES))
        self.assertIn("captured_at", record)
        check = run(self.root, "check")
        self.assertTrue(check["ok"])
        self.assertFalse(check["drift"])
        self.assertEqual(check["changed"], [])
        self.assertEqual(set(statuses(check).values()), {"PASS"})

    def test_jobs_both_accepted_layouts_pass(self):
        run(self.root, "baseline")
        (self.root / "cron" / "jobs.json").write_text(json.dumps([]), encoding="utf-8")
        (self.root / "profiles" / "sample" / "cron" / "jobs.json").write_text(
            json.dumps([]), encoding="utf-8")
        check = run(self.root, "check")
        self.assertFalse(check["drift"])  # bare list is also an accepted layout


class Mutations(IntactFixture):
    """Each contract, mutated: CHANGED, top-level drift summary visible, others still PASS."""

    def setUp(self):
        super().setUp()
        run(self.root, "baseline")
        self.check = None  # mutated per test before checking

    def _check(self):
        self.check = run(self.root, "check")
        return self.check

    def _assert_drift(self, contract):
        out = self._check()
        self.assertTrue(out["ok"])
        self.assertTrue(out["drift"], "top-level drift summary must be true on any CHANGED")
        self.assertIn(contract, out["changed"])
        self.assertEqual(statuses(out)[contract], "CHANGED")
        for other, st in statuses(out).items():
            if other != contract and other != "probe":
                self.assertEqual(st, "PASS", f"{other} should be untouched")

    def test_renamed_ui_meta_key_is_drift(self):
        import os
        os.remove(self.root / "profiles" / "sample" / "profile.yaml")
        mutated = {k: v for k, v in BOT_META.items() if k != "created"}
        mutated["created_x"] = BOT_META["created"]
        data = {"ui_meta": {"hermes-bots": mutated},
                "_ui_meta_revisions": {"hermes-bots": 1}}
        (self.root / "profiles" / "sample" / "profile.yaml").write_text(
            yaml.safe_dump(data), encoding="utf-8")
        self._assert_drift("bot_meta")
        self.assertIn("missing keys: created", self._check()["contracts"][0]["detail"])

    def test_dropped_session_column_is_drift(self):
        import os
        os.remove(self.root / "profiles" / "sample" / "state.db")
        make_state_db(self.root / "profiles" / "sample", ("id", "title"))
        self._assert_drift("sessions_schema")
        self.assertIn("started_at", self._check()["contracts"][1]["detail"])

    def test_jobs_wrong_layout_is_drift(self):
        (self.root / "cron" / "jobs.json").write_text(json.dumps({"queue": []}), encoding="utf-8")
        self._assert_drift("jobs_layout")
        self.assertIn("neither", self._check()["contracts"][2]["detail"])

    def test_jobs_missing_file_is_drift(self):
        (self.root / "cron" / "jobs.json").unlink()
        self._assert_drift("jobs_layout")
        self.assertFalse((self.root / "cron" / "jobs.json").exists())

    def test_env_key_missing_is_drift(self):
        text = (self.root / ".env").read_text(encoding="utf-8")
        (self.root / ".env").write_text(
            "".join(line for line in text.splitlines(keepends=True) if not line.startswith("EMAIL_PASSWORD=")),
            encoding="utf-8")
        self._assert_drift("env_keys")
        self.assertIn("EMAIL_PASSWORD", self._check()["contracts"][3]["detail"])

    def test_toolsets_wrong_type_is_drift(self):
        cfg = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        cfg["platform_toolsets"]["cli"] = "terminal"
        (self.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        self._assert_drift("platform_toolsets_cli")
        self.assertEqual(
            yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
            ["platform_toolsets"]["cli"], "terminal")


class RootIsNotAProfile(unittest.TestCase):
    """Regression (live false-positive drift): the hermes root is not a Bot profile. Per-profile
    contracts (bot_meta, platform_toolsets_cli) must not probe it, or every check emits a
    'profile.yaml unreadable' note that lands in the baseline and repeats forever."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_contract_root(Path(self._tmp.name))
        # A plain install: the root has no profile.yaml — it is not a live profile.
        (self.root / "profile.yaml").unlink()

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_root_note_and_no_drift_after_baseline(self):
        out = run(self.root, "baseline")
        self.assertTrue(out["ok"])
        self.assertEqual(out["notes"], [], "no probe note may exist for the root home")
        record = json.loads((self.root / ".bot-forge" / "contract_baseline.json").read_text())
        self.assertNotIn(self.root.name, record["contracts"]["bot_meta"])
        check = run(self.root, "check")
        self.assertTrue(check["ok"])
        self.assertFalse(check["drift"])
        self.assertEqual(check["changed"], [])

    def test_root_config_change_is_not_a_per_profile_drift(self):
        run(self.root, "baseline")
        cfg = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8"))
        cfg["platform_toolsets"]["cli"] = "terminal"
        (self.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        check = run(self.root, "check")
        self.assertFalse(check["drift"])  # the root is not a live profile here

    def test_root_with_hermes_bots_marker_is_still_probed(self):
        (self.root / "profile.yaml").write_text(
            yaml.safe_dump({"ui_meta": {"hermes-bots": dict(BOT_META)}}), encoding="utf-8")
        run(self.root, "baseline")
        record = json.loads((self.root / ".bot-forge" / "contract_baseline.json").read_text())
        self.assertEqual(record["contracts"]["bot_meta"].get(self.root.name), "present")

    def test_old_baseline_with_root_keys_degrades_cleanly(self):
        run(self.root, "baseline")
        store = self.root / ".bot-forge" / "contract_baseline.json"
        record = json.loads(store.read_text(encoding="utf-8"))
        record["contracts"]["bot_meta"]["<root>"] = "error: profile.yaml unreadable (stale)"
        store.write_text(json.dumps(record), encoding="utf-8")
        check = run(self.root, "check")
        self.assertTrue(check["ok"])  # execution success; drift semantics unchanged
        self.assertTrue(check["drift"])
        self.assertIn("bot_meta", check["changed"])
        self.assertIn("stale", check["contracts"][0]["detail"])


class NoteSetDiffing(unittest.TestCase):
    """Regression round 2 (live): on the real root email is not configured — all 5 keys
    absent. That error note is identical to baseline on every check, so probe CHANGED
    regenerated forever (drift: true, false alarm). The note set itself is diffed: only
    new or disappeared notes are loud."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_contract_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_stable_error_state_is_not_drift(self):
        # The real-root case: no .env at all — baseline records the same error state.
        (self.root / ".env").unlink()
        run(self.root, "baseline")
        check = run(self.root, "check")
        self.assertTrue(check["ok"])
        self.assertFalse(check["drift"])
        self.assertEqual(check["changed"], [])
        self.assertEqual(set(statuses(check).values()), {"PASS"})

    def test_keys_missing_that_baseline_had_present_are_loud(self):
        run(self.root, "baseline")  # .env intact at baseline
        (self.root / ".env").unlink()  # now the whole file is gone
        check = run(self.root, "check")
        self.assertTrue(check["drift"])
        self.assertIn("env_keys", check["changed"])
        self.assertIn("env_keys", " ".join(e["detail"] for e in check["contracts"]))

    def test_new_probe_note_is_loud_with_plus_detail(self):
        run(self.root, "baseline")
        (self.root / "profiles" / "sample" / "config.yaml").write_text(
            "toolsets: [unclosed", encoding="utf-8")  # now unreadable
        check = run(self.root, "check")
        self.assertTrue(check["drift"])
        probe_entries = [e for e in check["contracts"] if e["contract"] == "probe"]
        self.assertTrue(probe_entries)
        self.assertTrue(all(e["detail"].startswith("+ ") for e in probe_entries))
        self.assertIn("platform_toolsets_cli @ sample", probe_entries[0]["detail"])

    def test_disappeared_note_is_loud_with_minus_detail(self):
        (self.root / ".env").unlink()
        run(self.root, "baseline")  # baseline carries the missing-env note
        (self.root / ".env").write_text(
            "EMAIL_ADDRESS=a@b.c\nEMAIL_PASSWORD=pw\nEMAIL_SMTP_HOST=smtp\n"
            "EMAIL_SMTP_PORT=587\nEMAIL_HOME_ADDRESS=h@b.c\n", encoding="utf-8")
        check = run(self.root, "check")
        self.assertTrue(check["drift"])
        self.assertTrue(any(e["contract"] == "probe" and e["detail"].startswith("- ")
                            and "env_keys" in e["detail"] for e in check["contracts"]))

    def test_baseline_records_the_note_set(self):
        (self.root / ".env").unlink()
        run(self.root, "baseline")
        record = json.loads((self.root / ".bot-forge" / "contract_baseline.json").read_text())
        self.assertEqual(len(record["notes"]), 1)
        self.assertIn("env_keys @ .hermes", record["notes"][0])


class GuardRails(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = make_contract_root(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_without_baseline_refuses_cleanly(self):
        out = run(self.root, "check")
        self.assertFalse(out["ok"])
        self.assertIn("run op=baseline first", out["error"])

    def test_unknown_op_refused(self):
        out = contract_check.contract_check({"op": "bless", "hermes_root": str(self.root)})
        self.assertFalse(out["ok"])
        self.assertIn("baseline", out["error"])
        self.assertIn("check", out["error"])

    def test_baseline_never_stores_values(self):
        run(self.root, "baseline")
        record = json.loads((self.root / ".bot-forge" / "contract_baseline.json").read_text())
        blob = json.dumps(record)
        for secret in ("a@b.c", "pw", "blobatar"):
            self.assertNotIn(secret, blob)


class HandlerCallContract(unittest.TestCase):
    """Same class as forkops: input= without stdin=, stripped env, cwd=PLUGIN_DIR."""

    def test_run_gets_input_without_stdin(self):
        from unittest import mock
        with mock.patch.object(tools, "hermes_root", return_value=Path("root")), \
             mock.patch.object(tools.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout='{"ok": true}', stderr="", returncode=0)
            tools.contract_check({"op": "check"})
        kwargs = run.call_args.kwargs
        self.assertIn("input", kwargs)
        self.assertNotIn("stdin", kwargs)
        self.assertEqual(kwargs["cwd"], str(tools.PLUGIN_DIR))
        self.assertIn("PATH", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
