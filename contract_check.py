"""contract check: baseline-diff sentinel for unversioned Hermes contracts (subprocess script, JSON stdin/stdout).

Hermes contracts the fork depends on but does not version — profile.yaml ui_meta shape,
state.db sessions columns, cron/jobs.json layout, .env key names, config.yaml platform_toolsets.
`op=baseline` records the known-good SHAPES (never values) into <hermes-root>/.bot-forge/
contract_baseline.json; `op=check` re-probes and diffs. Everything is read-only except the
baseline store itself. Renamed/missing contracts fail loudly: callers gate on the top-level
`drift: true` + `changed: [...]` summary, not on `ok` (which is execution success only).

Re-baseline after a CONFIRMED Hermes update only — never to silence a drift: baseline
blesses whatever is live, including a shape broken by a Bot Forge bug.
"""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

OPS = ("baseline", "check")
STORE_REL = Path(".bot-forge") / "contract_baseline.json"

# Contract 1: profile.yaml → ui_meta['hermes-bots'] (forge.write_bot_meta is the writer).
BOT_META_KEYS = {"title", "description", "imageKind", "shape", "created"}
# Contract 2: state.db → sessions columns (never select data rows — PRAGMA only).
SESSION_COLUMNS = {"id", "title", "started_at"}
# Contract 4: .env key NAMES at the hermes root (notify.ENV_KEYS); values are never read.
ENV_KEYS = ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_SMTP_HOST", "EMAIL_SMTP_PORT",
            "EMAIL_HOME_ADDRESS")
JOBS_LAYOUTS = ("dict-jobs", "bare-list")


def _hermes_version() -> str:
    try:
        p = subprocess.run(["hermes", "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           stdin=subprocess.DEVNULL)
        return (p.stdout or p.stderr or "").strip().splitlines()[-1][:40] if p.stdout or p.stderr else ""
    except Exception:
        return ""


def _homes(root: Path) -> list:
    """Root profile plus every live profile (forge.is_live_profile semantics, inlined:
    this is a standalone script without a plugin import)."""
    homes = [root]
    profiles = root / "profiles"
    if profiles.is_dir():
        for d in sorted(profiles.iterdir()):
            if (d.is_dir() and not d.name.startswith(".") and (d / "config.yaml").exists()
                    and not (profiles.parent / ".deleted" / d.name).exists()):
                homes.append(d)
    return homes


# ── probes: each returns (shape, detail) where shape is what the baseline stores ─────

def _probe_bot_meta(home: Path) -> tuple[str | None, str]:
    try:
        import yaml
        data = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return None, f"profile.yaml unreadable: {exc}"
    bots = (data.get("ui_meta") or {}).get("hermes-bots") if isinstance(data.get("ui_meta"), dict) else None
    if not isinstance(bots, dict):
        return None, ""
    missing = BOT_META_KEYS - set(bots)
    if missing:
        return None, f"missing keys: {', '.join(sorted(missing))}"
    revs = data.get("_ui_meta_revisions")
    if revs is not None and not (isinstance(revs, dict) and isinstance(revs.get("hermes-bots"), int)):
        return None, "_ui_meta_revisions['hermes-bots'] is not an int counter"
    return "present", ""


def _probe_sessions(home: Path) -> tuple[str | None, str]:
    db = home / "state.db"
    if not db.exists():
        return None, ""
    try:
        # Read-only URI, like forge.py; column NAMES only, never data rows.
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error as exc:
        return None, f"state.db not readable: {exc}"
    try:
        cols = {row[1] for row in con.execute("pragma table_info(sessions)")}
    finally:
        con.close()
    missing = SESSION_COLUMNS - cols
    if missing:
        return None, f"sessions columns missing: {', '.join(sorted(missing))}"
    return "present", ""


def _probe_jobs(home: Path) -> tuple[str | None, str]:
    path = home / "cron" / "jobs.json"
    if not path.exists():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"jobs.json not parseable: {exc}"
    # Both accepted layouts (dict with "jobs", bare list) normalize to one shape: either = PASS.
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return "accepted", ""
    if isinstance(data, list):
        return "accepted", ""
    return None, "jobs.json is neither a dict with 'jobs' nor a bare list"


def _probe_env(root: Path, home: Path) -> tuple[str | None, str]:
    """The .env contract lives at the hermes root only — skip every profile home."""
    if home != root:
        return None, ""
    path = root / ".env"
    if not path.exists():
        return None, ".env missing at the hermes root"
    import re
    try:
        text = path.read_text(errors="ignore", encoding="utf-8")
    except OSError as exc:
        return None, f".env unreadable: {exc}"
    names = {m.group(1) for m in re.finditer(r"^\s*(?:export\s+)?([A-Z_]+)\s*=", text, re.M)}
    missing = [k for k in ENV_KEYS if k not in names]
    if missing:
        return None, f".env key(s) missing: {', '.join(sorted(missing))}"
    return "present", ""


def _probe_toolsets(home: Path) -> tuple[str | None, str]:
    try:
        import yaml
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return None, f"config.yaml unreadable: {exc}"
    cli = (cfg.get("platform_toolsets") or {}).get("cli")
    if not isinstance(cli, list) or not all(isinstance(t, str) for t in cli):
        return None, "platform_toolsets.cli is not a list of toolset names"
    return "present", ""


PROBES = {
    "bot_meta": lambda root, home: _probe_bot_meta(home),
    "sessions_schema": lambda root, home: _probe_sessions(home),
    "jobs_layout": lambda root, home: _probe_jobs(home),
    "env_keys": lambda root, home: _probe_env(root, home),
    "platform_toolsets_cli": lambda root, home: _probe_toolsets(home),
}

# Per-profile contracts: they are about Bot profiles, and the hermes root is not one —
# probing it only ever yields a "profile.yaml unreadable" error note that repeats on
# every check (a permanent probe CHANGED, drift: true forever). The root home counts
# only when it is itself a Bot profile (forge.bot_mode_install semantics: it carries
# the ui_meta['hermes-bots'] marker). Root-only contracts (env_keys) keep probing it.
PER_PROFILE = ("bot_meta", "platform_toolsets_cli")


def _root_is_bot_profile(root: Path) -> bool:
    try:
        import yaml
        data = yaml.safe_load((root / "profile.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return isinstance((data.get("ui_meta") or {}).get("hermes-bots"), dict)


def probe(root: Path) -> "tuple[dict, dict]":
    """({contract: {home: shape}}, notes) — per-home shapes (never values), so a single
    drifted profile is visible even where others are intact. A probe error is captured
    as an 'error: ...' shape so it diffs loudly instead of disappearing."""
    shapes, notes = {}, []
    root_bot = _root_is_bot_profile(root)
    for name, probe_fn in PROBES.items():
        per_home = {}
        for home in _homes(root):
            if name in PER_PROFILE and home == root and not root_bot:
                continue
            key = home.name or "<root>"
            shape, detail = probe_fn(root, home)
            if detail:
                notes.append(f"{name} @ {key}: {detail}")
            if shape is not None or detail:
                per_home[key] = shape if shape is not None else f"error: {detail}"
        shapes[name] = per_home
    return shapes, notes


def _store(root: Path) -> Path:
    return root / STORE_REL


def op_baseline(s: dict, root: Path) -> dict:
    shapes, notes = probe(root)
    store = _store(root)
    store.parent.mkdir(parents=True, exist_ok=True)
    import datetime
    record = {"captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
              "hermes_version": _hermes_version(), "contracts": shapes}
    store.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {"ok": True, "op": "baseline", "path": str(store),
            "contracts": record["contracts"],
            "notes": notes,
            "next": "commit the baseline as part of the Hermes update; re-baseline only on a confirmed update"}


def op_check(s: dict, root: Path) -> dict:
    store = _store(root)
    if not store.exists():
        return {"ok": False, "op": "check", "error": "no baseline — run op=baseline first"}
    try:
        baseline = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "op": "check", "error": f"baseline store unreadable: {exc}"}
    expected = baseline.get("contracts") or {}
    shapes, notes = probe(root)
    results = []
    for name in PROBES:
        want, got = expected.get(name), shapes.get(name)
        detail = ""
        if want != got:
            diff = {k: {"baseline": (want or {}).get(k), "live": (got or {}).get(k)}
                    for k in set(want or {}) | set(got or {}) if (want or {}).get(k) != (got or {}).get(k)}
            detail = json.dumps(diff)
        results.append({"contract": name, "status": "PASS" if want == got else "CHANGED",
                        "detail": detail})
    results.extend({"contract": "probe", "status": "CHANGED", "detail": n} for n in notes)
    changed = [r["contract"] for r in results if r["status"] == "CHANGED"]
    return {"ok": True, "op": "check",
            "captured_at": baseline.get("captured_at"),
            "hermes_version": baseline.get("hermes_version"),
            "contracts": results, "drift": bool(changed), "changed": changed,
            "hint": "re-baseline only after a confirmed Hermes update; a drift without one is a bug" if changed else ""}


def contract_check(s: dict) -> dict:
    root = Path(s.get("hermes_root") or Path.home() / ".hermes")
    op = (s.get("op") or "").strip().lower()
    if op not in OPS:
        return {"ok": False, "error": f"unknown op; use one of {', '.join(OPS)}"}
    try:
        return op_baseline(s, root) if op == "baseline" else op_check(s, root)
    except OSError as exc:
        return {"ok": False, "op": op, "error": str(exc)}


def main():
    try:
        raw = sys.stdin.read() if len(sys.argv) < 2 or sys.argv[1] == "-" else Path(sys.argv[1]).read_text(encoding="utf-8")
        result = contract_check(json.loads(raw))
    except json.JSONDecodeError as exc:
        result = {"ok": False, "error": f"input is not valid JSON: {exc}"}
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
