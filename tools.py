"""Tool handlers. Bot creation runs forge.py in a subprocess with a clean environment, so the calling
agent's per-session overrides (HOME / HERMES_HOME) never leak into the Bot being built or asked."""

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

if __package__:  # Hermes imports this as a package; the CLI entry points run it as a script
    from . import forge, policy as policy_mod
else:
    import forge
    import policy as policy_mod

if __package__:
    from . import platforms
else:
    import platforms

PLUGIN_DIR = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
NOISE = ("hermes update", "Gateways may", "hermes gateway restart", "tirith security scanner")


def hermes_root() -> Path:
    """Root Hermes dir (``<root>/profiles/<name>`` layout), honoring custom HERMES_HOME roots."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:
        if platforms.is_windows():
            return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "hermes"
        return Path.home() / ".hermes"


def _clean(text):
    return "\n".join(line for line in (text or "").splitlines()
                     if not any(n in line for n in NOISE)).strip()


def launch_profile(session_id=None, root=None) -> str:
    """Profile of the agent calling the tool. Desktop serves every Bot from one backend process, so
    prefer the context-local home override, then the profile whose state.db owns the calling session."""
    root = Path(root or hermes_root())
    try:
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override and Path(override).resolve().parent == (root / "profiles").resolve():
            return Path(override).resolve().name
    except Exception:
        pass
    if session_id:
        profiles = root / "profiles"
        homes = [("default", root)] + (sorted((d.name, d) for d in profiles.iterdir() if d.is_dir()) if profiles.is_dir() else [])
        for name, home in homes:
            db = home / "state.db"
            if not db.exists():
                continue
            try:
                with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)) as c:
                    if c.execute("select 1 from sessions where id=? limit 1", (session_id,)).fetchone():
                        return name
            except sqlite3.Error:
                continue
    return "default"


def _creation_model_error(spec: dict) -> str:
    """Validate only an explicitly supplied creation-tool route, without echoing values."""
    if "model" not in spec:
        return ""
    model = spec["model"]
    if not isinstance(model, dict) or any(
        not isinstance(model.get(k), str) or not model[k].strip() for k in ("default", "provider")
    ):
        return "model must be an object with non-empty default and provider strings"
    if set(model) - {"default", "provider", "base_url", "api_mode"}:
        return "model accepts only default, provider, base_url and api_mode; never supply credentials"
    if any(not isinstance(v, str) for v in model.values()):
        return "model route fields must be strings"
    return ""


def create_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    problem = _creation_model_error(args)
    if problem:
        return json.dumps({"ok": False, "error": problem})
    root = hermes_root()
    spec = {**args, "launch_profile": launch_profile(kwargs.get("session_id"), root),
            "hermes_root": str(root), "settings": settings or {}}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "forge.py"), "-"], input=json.dumps(spec),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "create_agent timed out after 15 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def _manage(op: str, args: dict, settings: dict | None = None) -> str:
    root = hermes_root()
    spec = {**args, "op": op, "hermes_root": str(root), "settings": settings or {}}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "manage.py"), "-"], input=json.dumps(spec),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"{op} timed out after 15 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def fork_ops(args: dict, **kwargs) -> str:
    """Fork maintenance: status / sync / adopt. Runs forkops.py in a subprocess; git
    targets the main checkout, adopt re-installs the fork commit into Bot profiles."""
    spec = {"op": args.get("op"), "profiles": args.get("profiles") or [],
            "hermes_root": str(hermes_root())}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "forkops.py"), "-"],
                           input=json.dumps(spec), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "fork_ops timed out after 15 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def contract_check(args: dict, **kwargs) -> str:
    """Baseline-diff sentinel for unversioned Hermes contracts. Runs contract_check.py in a
    subprocess; strictly read-only except its own baseline store under .bot-forge/."""
    spec = {"op": args.get("op"), "hermes_root": str(hermes_root())}
    env = {key: os.environ[key] for key in _WAITING_ENV_KEYS if os.environ.get(key)}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "contract_check.py"), "-"],
                           input=json.dumps(spec), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           env=env, cwd=str(PLUGIN_DIR))
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "contract_check timed out after 2 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def rename_agent(args: dict, **kwargs) -> str:
    """Rename a Bot: `hermes profile rename` owns the profile dir and state DB; this
    sweeps the fork-owned references (team rosters, SOUL.md identity, .botforge.json
    templates) afterwards. Runs rename.py in a subprocess with a stripped environment."""
    spec = {"old_name": args.get("old_name"), "new_name": args.get("new_name"),
            "confirm": args.get("confirm"), "hermes_root": str(hermes_root())}
    env = {key: os.environ[key] for key in _WAITING_ENV_KEYS if os.environ.get(key)}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "rename.py"), "-"],
                           input=json.dumps(spec), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600,
                           env=env, cwd=str(PLUGIN_DIR))
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "rename_agent timed out after 10 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


_WAITING_ENV_KEYS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "LOCALAPPDATA",
                     "APPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP",
                     # the gateway may resolve site-packages (PyYAML) via PYTHONPATH;
                     # stripping it makes `import forge` fail with ModuleNotFoundError
                     "PYTHONPATH")


def waiting_on(args: dict, **kwargs) -> str:
    """What is waiting on the user, across every Bot. Runs waiting.py in a subprocess;
    strictly read-only, so the child gets a stripped environment and the plugin dir as cwd."""
    spec = {"hermes_root": str(hermes_root())}
    env = {key: os.environ[key] for key in _WAITING_ENV_KEYS if os.environ.get(key)}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "waiting.py"), "-"],
                           input=json.dumps(spec), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120,
                           env=env, cwd=str(PLUGIN_DIR))
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "waiting_on timed out after 2 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def create_team(args: dict, settings: dict | None = None, **kwargs) -> str:
    # Check every explicit route before building the lead or any member.
    specs = [args.get("lead")] + list(args.get("members") or [])
    for spec in specs:
        if isinstance(spec, dict):
            problem = _creation_model_error(spec)
            if problem:
                return json.dumps({"ok": False, "error": problem})
    root = hermes_root()
    spec = {**args, "launch_profile": launch_profile(kwargs.get("session_id"), root),
            "hermes_root": str(root), "settings": settings or {}}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "team.py"), "-"], input=json.dumps(spec),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "create_team timed out after 60 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def check_install(args: dict, settings: dict | None = None, **kwargs) -> str:
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "doctor.py"), "--json",
                            "--settings-stdin"],
                           input=json.dumps(settings or {}), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "check_install timed out"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def check_agents(args: dict, **kwargs) -> str:
    root = hermes_root()
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "health.py"), "-"],
                           input=json.dumps({**args, "hermes_root": str(root)}),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "check_agents timed out"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def _read_text_safe(path) -> str | None:
    """Read a text file, or None if it cannot be read as text.

    Separate from a plain `read_text()` because a UnicodeDecodeError is a ValueError, not an
    OSError, and escapes `except OSError` — which turned a hand-edited file with one latin-1
    apostrophe into a raised exception out of a read-only tool.
    """
    try:
        return path.read_text(errors="replace", encoding="utf-8")
    except (OSError, ValueError):
        return None


def check_policies(args: dict, **kwargs) -> str:
    """Report which Bots' shared operating policy is current, and which have drifted."""
    root = hermes_root()
    relative = (args or {}).get("policy_path") or policy_mod.DEFAULT_RELATIVE
    try:
        canon = policy_mod.policy_path(root, relative)
    except policy_mod.PolicyPathError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    if not canon.exists():
        return json.dumps({"ok": False, "error": f"no shared policy at {canon}",
                          "hint": "create_agent writes one on first use; or create it yourself"})
    # errors="replace": a policy or SOUL.md saved by a non-UTF-8 editor (or containing one
    # latin-1 apostrophe) must degrade to "cannot read", not raise out of a read-only tool.
    # UnicodeDecodeError is a ValueError, so `except OSError` does not catch it.
    canon_text = _read_text_safe(canon)
    if canon_text is None:
        return json.dumps({"ok": False, "error": f"could not read {canon} as text"})
    if not policy_mod.policy_body(canon_text):
        return json.dumps({
            "ok": False,
            "error": f"shared policy at {canon} has no rules in it",
            "hint": "it contains only comments, so every Bot would be built with an empty policy",
        })
    expected = policy_mod.fingerprint(canon_text)

    bots, stale, without, unreadable = [], [], [], []
    candidates = sorted((root / "profiles").glob("*")) if (root / "profiles").is_dir() else []
    # The default profile's SOUL.md sits at the root, not under profiles/, so it is not in the
    # glob above. It is deliberately NOT audited: `_require_bot` refuses it, so `forge` never
    # writes a block there and no documented call can ever fix it. Reporting a permanent
    # `no_shared_policy` for a profile no operation can act on would make the drift report
    # untrustworthy -- the whole point of this tool is that every row it prints is actionable.
    for prof in candidates:
        name = prof.name
        soul = prof / "SOUL.md"
        try:
            text = soul.read_text(errors="replace", encoding="utf-8")
        except OSError as exc:
            bots.append({"profile": name, "error": str(exc)[:120], "current": False,
                         "has_shared_policy": False, "reason": "unreadable",
                         "fingerprint": None, "canonical_fingerprint": expected})
            unreadable.append(name)
            continue
        report = policy_mod.audit_soul(text, expected)
        report["profile"] = name
        bots.append(report)
        if not report["current"]:
            (without if not report["has_shared_policy"] else stale).append(name)

    return json.dumps({
        "ok": True,
        "canonical_policy": str(canon),
        "canonical_fingerprint": expected,
        "bots_checked": len(bots),
        # counted from the rows themselves, not by subtraction: a row that errored belongs to
        # no list, and `checked - stale - without` silently counted it as current.
        "current": sum(1 for b in bots if b.get("current")),
        "stale": stale,
        "no_shared_policy": without,
        "unreadable": unreadable,
        "bots": bots,
        "fix": ("ask the Bot's owner to re-create it, or run update_agent with soul_md to "
                "rewrite its SOUL.md — create_agent refuses a name that is already taken, so "
                "re-running it is not a way to refresh an existing Bot"),
    })


def agent_journal(args: dict, settings: dict | None = None, **kwargs) -> str:
    """Enable, append to, or read a Bot's work journal."""
    root = hermes_root()
    spec = {**args, "launch_profile": launch_profile(kwargs.get("session_id"), root),
            "hermes_root": str(root), "settings": settings or {}}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "journal.py"), "-"], input=json.dumps(spec),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "agent_journal timed out"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def teach_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    return _manage("teach", args, settings)


def update_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    return _manage("update", args, settings)


def copy_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    return _manage("copy", args, settings)


def share_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    # allow_secrets is an operator setting, never a tool argument
    return _manage("export", {k: v for k, v in args.items() if k != "allow_secrets"}, settings)


def import_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    return _manage("import", {**{k: v for k, v in args.items() if k != "allow_secrets"},
                              "launch_profile": launch_profile(kwargs.get("session_id"))}, settings)


def hide_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    return _manage("hide" if args.get("hidden", True) else "show", args, settings)


def delete_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    return _manage("delete", args, settings)


def request_takeover(args: dict, settings: dict | None = None, **kwargs) -> str:
    """Ask the user onto this Bot's screen. The caller is the profile whose turn this is."""
    root = hermes_root()
    spec = {**args, "from": launch_profile(kwargs.get("session_id"), root),
            "hermes_root": str(root), "settings": settings or {}}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "takeover.py"), "-"],
                           input=json.dumps(spec), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "request_takeover timed out"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def handoff_agent(args: dict, settings: dict | None = None, **kwargs) -> str:
    """Pass a task, with context, to another Bot; it runs in that Bot's own Bot Chat."""
    root = hermes_root()
    spec = {**args, "from": launch_profile(kwargs.get("session_id"), root),
            "hermes_root": str(root), "settings": settings or {}}
    try:
        p = subprocess.run([sys.executable, str(PLUGIN_DIR / "handoff.py"), "-"], input=json.dumps(spec),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=960)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "handoff_agent timed out after 16 min"})
    out = p.stdout.strip()
    try:
        return json.dumps(json.loads(out[out.index("{"):]))
    except ValueError:
        return json.dumps({"ok": False, "error": _clean(p.stderr or out)[-1500:]})


def _profile_info(name, home):
    import yaml
    cfg, meta = {}, {}
    for fname, target in (("config.yaml", cfg), ("profile.yaml", meta)):
        try:
            target.update(yaml.safe_load((home / fname).read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    bots = (meta.get("ui_meta") or {}).get("hermes-bots") or {}
    bots = bots if isinstance(bots, dict) else {}
    info = {"name": name, "display_name": bots.get("title") or meta.get("display_name") or name,
            "description": (meta.get("description") or "").strip(),
            "model": (cfg.get("model") or {}).get("default") or ""}
    if bots.get("hidden"):
        info["hidden"] = True
    try:
        jobs = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
        count = len(jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs)
        if count:
            info["routines"] = count
    except Exception:
        pass
    return info


def list_agents(args: dict, **kwargs) -> str:
    root = hermes_root()
    agents = [_profile_info("default", root)]
    profiles = root / "profiles"
    if profiles.is_dir():
        agents += [_profile_info(d.name, d) for d in sorted(profiles.iterdir()) if (d / "config.yaml").exists()]
    return json.dumps({"agents": agents})


def delegate_outside(args: dict, settings: dict | None = None, **kwargs) -> str:
    """Spawn Claude Code. The caller is resolved the way every other tool resolves it — from the
    running profile, never from a tool argument."""
    if __package__:
        from . import outside
    else:
        import outside
    spec = {**args, "hermes_root": str(hermes_root()), "settings": settings or {},
            "from": launch_profile(kwargs.get("session_id"), hermes_root())}
    return json.dumps(outside.delegate(spec))


def _strip_noise(text: str) -> str:
    """Ask-agent reply cleanup: ANSI color codes, NOISE lines, and CLI scaffolding
    (worktree bookkeeping, session_id) that is never part of a Bot's answer."""
    text = ANSI.sub("", text or "")
    lines = []
    for line in (text or "").splitlines():
        s = line.strip()
        if any(n in line for n in NOISE):
            continue
        if s.startswith(("✓ Worktree", "Worktree", "session_id:", "Branch: hermes/", "Base:  ", "Base: origin")):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


TURN_LIMIT_MARKERS = ("iteration limit", "turn limit", "max-turns", "iteration budget")

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _classify_ask(rc: int, stdout: str, stderr: str) -> dict:
    """One place for deciding what a `chat -Q` child actually did (pure, unit-tested).

    Returns {status, reply?, error?, exhausted?, error_type?}:
    - status "ok": rc 0 and a reply exists
    - status "turn_budget": rc != 0 and the output says the iteration/turn limit was hit —
      not a crash; the partial reply is real and more turns (or a smaller task) fix it
    - status "empty": rc 0 but no reply text survived cleaning
    - status "failed": anything else — a real error; the session_id (if stderr had
      one) is captured for `--resume` even though its line stays out of the error text
    """
    reply = _strip_noise(stdout)
    # Capture the session_id from stderr BEFORE it is stripped, so a turn-budget
    # exhaustion (or any failure with a live session) can be resumed later.
    m = re.search(r"^session_id:\s*(\S+)", stderr or "", re.MULTILINE)
    session_id = m.group(1) if m else None
    if rc == 0 and reply:
        return {"status": "ok", "reply": reply}
    if rc != 0 and any(m2 in stdout for m2 in TURN_LIMIT_MARKERS):
        return {"status": "turn_budget", "exhausted": True, "partial_reply": reply,
                "session_id": session_id,
                "error": "the Bot hit its turn budget before finishing"}
    if rc == 0:
        return {"status": "empty", "error": "no reply text came back"}
    err = "\n".join(line for line in (stderr or "").splitlines()
                    if not line.strip().startswith("session_id:")).strip()
    verdict = {"status": "failed", "error": (err or reply or "unknown failure")[-1500:]}
    if session_id:
        verdict["session_id"] = session_id
    return verdict


def ask_agent(args: dict, **kwargs) -> str:
    root = hermes_root()
    name = (args.get("name") or "").strip().lower()
    message = (args.get("message") or "").strip()
    if not NAME_RE.match(name) or not message:
        return json.dumps({"ok": False, "error": "need a valid profile name and a message"})
    max_turns = args.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or not 1 <= max_turns <= 300):
        return json.dumps({"ok": False, "error": "max_turns must be an integer 1..300"})
    if name != "default" and not (root / "profiles" / name / "config.yaml").exists():
        return json.dumps({"ok": False, "error": f"no Bot named '{name}' (see list_agents)"})
    turns = max_turns if isinstance(max_turns, int) and max_turns >= 1 else 30
    # Never forward the caller's process environment to another Bot — it may hold the main profile's
    # whole .env. forge.safe_env is the one allowlist for running another Bot's turn.
    env = forge.safe_env(root)
    try:
        limit = forge.reading_only_args(args.get("reading_only"))
        wait = forge.within_deadline(900, root)
        p = subprocess.run(["hermes", "-p", name, "chat", "-Q", "--max-turns", str(turns),
                            *limit, "-q", message],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL, timeout=wait, env=env,
                           cwd=str(PLUGIN_DIR))
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": f"{name} did not answer within {wait}s. "
                                                 + forge.deadline_hint(root)})
    except FileNotFoundError:
        return json.dumps({"ok": False, "error": "hermes CLI not found on PATH"})
    verdict = _classify_ask(p.returncode, p.stdout or "", p.stderr or "")
    if verdict["status"] == "ok":
        return json.dumps({"ok": True, "name": name, "reply": verdict["reply"][-6000:]})
    if verdict["status"] == "turn_budget":
        sid = verdict.get("session_id")
        return json.dumps({"ok": False, "name": name, "error_type": "turn_budget",
                           "exhausted": True, "turns": turns,
                           "session_id": sid,
                           "partial_reply": (verdict.get("partial_reply") or "")[-3000:] or None,
                           "error": (f"{name} hit its {turns}-turn budget before finishing. "
                                     "The partial reply is real work, not garbage. Retry with "
                                     "a higher max_turns, split the task into smaller asks, "
                                     + (f"or resume the session with: hermes --resume {sid} -p {name}."
                                        if sid else "")).strip()})
    failed = {"ok": False, "name": name, "error": verdict["error"]}
    if verdict.get("session_id"):
        failed["session_id"] = verdict["session_id"]
    return json.dumps(failed)
