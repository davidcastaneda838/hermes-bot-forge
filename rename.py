"""Rename a Bot: orchestrates `hermes profile rename` and sweeps fork-owned references.

`hermes profile rename old new` (built into Hermes) renames the profile dir AND migrates
state-DB identity (sessions, routing, heartbeats); `hermes profile migrate-identity old new`
is the idempotent retry. This script does NOT reimplement any of that — it runs the CLI once
and then cleans up the references Hermes does not know about:

  - team rosters: create_team wrote member lists into the LEAD's memory
    (team.py/_tell_lead is the writer) as `- @name (Display): description` bullets
  - the renamed Bot's SOUL.md identity lines (forge.ensure_identity wrote them;
    replace-never-stack means a rename leaves a stale name line behind)
  - .botforge.json templates under <root>/profile-exports that reference the old name

The sweep is all-or-nothing: every file is snapshotted before its first edit, and any
failure restores every already-edited file from its snapshot — a rename never half-applies.
"""
import json
import re
import sys
from pathlib import Path

if __package__:  # Hermes imports this as a package; the CLI entry points run it as a script
    from . import forge, journal
else:
    import forge
    import journal

# Same shape forkops._wanted_profiles validates profile names with.
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# A roster bullet as _tell_lead writes it: "- @profile (Display): description"
ROSTER_LINE = re.compile(r"^(?P<indent>\s*)-(?P<at> ?)@(?P<name>[A-Za-z0-9_-]+)(?P<rest>\s*\(.*\): .*)$", re.M)


def rename(s: dict) -> dict:
    root = Path(s.get("hermes_root") or forge.default_root())
    old = (s.get("old_name") or "").strip()
    new = (s.get("new_name") or "").strip()
    confirm = (s.get("confirm") or "").strip()

    # Guards: every one of these returns before anything is touched.
    if not old or not new or confirm != old:
        return {"ok": False, "error": "confirm must equal old_name exactly (delete_agent pattern)"}
    for label, name in (("old_name", old), ("new_name", new)):
        if not SLUG.match(name):
            return {"ok": False, "error": f"{label} is not a valid profile name: {name!r}"}
    if old == new:
        return {"ok": False, "error": "new_name must differ from old_name"}
    src = root / "profiles" / old
    if not forge.is_live_profile(src):
        return {"ok": False, "error": f"no live profile named {old!r} under {root / 'profiles'}"}
    if (root / "profiles" / new).exists() or (root / ".deleted" / new).exists():
        return {"ok": False, "error": f"a profile named {new!r} already exists"}

    # Hermes owns the rename (dir + state DB); a failure here stops everything.
    try:
        forge.run(root, "profile", "rename", old, new, timeout=300)
    except Exception as exc:
        return {"ok": False, "error": f"hermes profile rename failed: {exc}"[-800:]}

    # Fork-owned sweep, all-or-nothing: snapshot each file before its first edit.
    edits: list[tuple[Path, str]] = []

    try:
        swept, skipped = [], []
        profiles = root / "profiles"
        homes = [root] + ([d for d in profiles.iterdir() if forge.is_live_profile(d)]
                          if profiles.is_dir() else [])
        for home in homes:
            mem = home / "memories" / "MEMORY.md"
            if not mem.is_file() or mem.is_symlink():
                continue
            text = mem.read_text(encoding="utf-8")

            def swap(m: re.Match) -> str:
                return f"{m['indent']}-{' ' if m['at'] else ''}@{new}{m['rest']}" if m["name"] == old else m[0]

            updated = ROSTER_LINE.sub(swap, text)
            if updated != text:
                edits.append((mem, text))
                mem.write_text(updated, encoding="utf-8")
                swept.append(str(mem))

        soul_path = root / "profiles" / new / "SOUL.md"
        if soul_path.is_file() and not soul_path.is_symlink():
            # The profile id changed; the display title (profile.yaml) did not. Rebuild the
            # identity around the display name when there is one, the new id otherwise.
            meta = forge.load_yaml(root / "profiles" / new / "profile.yaml")
            bots = (meta.get("ui_meta") or {}).get("hermes-bots") or {}
            display = (bots.get("title") if isinstance(bots, dict) else None) or meta.get("display_name") or new
            soul = soul_path.read_text(encoding="utf-8")
            updated = forge.ensure_identity(soul, str(display), forge.soul_role(soul) or "Bot", new)
            # ensure_identity keeps an already-correct identity as the author's wording — which
            # after a rename still names the OLD profile id. Point it at the new one.
            updated = re.sub(rf"(profile `){re.escape(old)}(`)", rf"\g<1>{new}\g<2>", updated)
            if updated != soul:
                edits.append((soul_path, soul))
                soul_path.write_text(updated, encoding="utf-8")
                swept.append(str(soul_path))
            else:
                skipped.append(str(soul_path))

        folder = root / "profile-exports"
        for path in sorted(folder.glob("*.botforge.json")) if folder.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                skipped.append(str(path))
                continue
            if not isinstance(data, dict):
                skipped.append(str(path))
                continue
            before = json.dumps(data, ensure_ascii=False)
            for key in ("display_name", "name", "profile_id"):
                if data.get(key) == old:
                    data[key] = new
            for key in ("soul_md", "persona"):
                if isinstance(data.get(key), str):
                    data[key] = re.sub(rf"\b{re.escape(old)}\b", new, data[key])
            if json.dumps(data, ensure_ascii=False) != before:
                text = path.read_text(encoding="utf-8")
                edits.append((path, text))
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                swept.append(str(path))
    except Exception as exc:
        for path, snapshot in reversed(edits):  # undo every edit already made
            try:
                path.write_text(snapshot, encoding="utf-8")
            except OSError:
                pass
        return {"ok": False, "renamed": old, "error": f"sweep failed at {exc}; every edit was rolled back"}

    journal_result = _journal(root, old, new)
    return {"ok": True, "renamed": {"from": old, "to": new}, "swept": swept,
            "skipped": skipped, "journal": journal_result}


def _journal(root: Path, old: str, new: str) -> dict:
    """A rename is a material change; record it in the Bot's own journal when enabled."""
    try:
        if not journal.journaling_enabled(root / "profiles" / new):
            return {"written": False, "reason": "journal not enabled"}
        return journal.operate({"action": "add", "name": new, "hermes_root": str(root),
                                "status": "completed", "title": f"renamed from {old}",
                                "summary": f"Profile renamed {old} -> {new} via rename_agent; "
                                           "fork-owned references swept."})
    except Exception as exc:  # a journal failure must not fail the rename
        return {"written": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}


def main():
    try:
        raw = sys.stdin.read() if len(sys.argv) < 2 or sys.argv[1] == "-" else Path(sys.argv[1]).read_text(encoding="utf-8")
        result = rename(json.loads(raw))
    except json.JSONDecodeError as exc:
        result = {"ok": False, "error": f"input is not valid JSON: {exc}"}
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
