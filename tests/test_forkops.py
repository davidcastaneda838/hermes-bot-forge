"""forkops: the fork maintenance script. Logic-level tests via the fork() entry point;
git-backed ops run against the real checkout only where they are read-only."""
import os
import unittest
from pathlib import Path

from support import ROOT, forkops  # noqa: F401,E402  (support arranges sys.path)


def live_root() -> Path | None:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "hermes"
    return base if (base / "plugins" / "bot-forge" / ".git").is_dir() else None


def fork(spec: dict) -> dict:
    root = live_root()
    if root is None:
        raise unittest.SkipTest("live checkout not present in this environment")
    return forkops.fork({**spec, "hermes_root": str(root)})


def with_home(monkey_home: Path):
    """A hermes root that has no plugins/bot-forge checkout: fork() must refuse."""
    return forkops.fork({"op": "status", "hermes_root": str(monkey_home)})


class OpValidation(unittest.TestCase):
    def test_unknown_op_is_refused_with_the_list(self):
        out = fork({"op": "blast"})
        self.assertFalse(out["ok"])
        self.assertIn("status", out["error"])
        self.assertIn("sync", out["error"])
        self.assertIn("adopt", out["error"])

    def test_missing_op_is_refused(self):
        self.assertFalse(fork({})["ok"])

    def test_no_checkout_is_refused_not_crashing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "plugins").mkdir()
            out = with_home(Path(tmp))
            self.assertFalse(out["ok"])
            self.assertIn("no git checkout", out["error"])


class StatusAgainstRealCheckout(unittest.TestCase):
    """The real checkout exists on this machine; status is read-only."""

    def test_status_reports_shape(self):
        # The practice clone IS a checkout but not under <root>/plugins; use the live one.
        import os
        live = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "hermes"
        out = forkops.fork({"op": "status", "hermes_root": str(live)})
        if not out.get("ok"):
            self.skipTest("live checkout not present in this environment")
        self.assertIn(out["op"], ("status",))
        self.assertIsInstance(out["dirty"], bool)
        self.assertIsInstance(out["ahead"], int)
        self.assertIsInstance(out["behind"], int)
        self.assertIn("/", out["fork"])  # owner/repo


class ProfileScan(unittest.TestCase):
    def test_invalid_profile_dirs_are_skipped_not_adopted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "profiles"
            pdir.mkdir()
            (pdir / "magroot").mkdir()
            (pdir / ".deleted").mkdir()
            (pdir / "Not Valid!").mkdir()
            have, skipped = forkops._wanted_profiles(Path(tmp), [])
            self.assertEqual(have, ["magroot"])
            self.assertEqual(skipped, [".deleted", "Not Valid!"])
            # a missing profile in an explicit list is refused, not half-adopted
            with self.assertRaises(LookupError):
                forkops._wanted_profiles(Path(tmp), ["ghost"])


if __name__ == "__main__":
    unittest.main()
