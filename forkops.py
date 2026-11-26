"""fork ops: status/sync/adopt for the Bot Forge fork (subprocess script, JSON stdin/stdout).

The fork lives in the main profile's plugin directory (a git checkout with two remotes:
upstream = BkashJEE/hermes-bot-forge, origin = the fork). Bot profiles hold plain copies
without .git, so git operations always target the main checkout; `adopt` is how the
fork's commit reaches the Bot profiles (catalog pins block `plugins update`).
"""

import json
import subprocess
import sys
from pathlib import Path

OPS = ("status", "sync", "adopt")
REMOTE = "upstream"
BRANCH = "main"
UPSTREAM_REF = "upstream/main"


def _git(repo: Path, *args: str, timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def _repo_dir(root: Path) -> Path | None:
    """The main checkout: <hermes root>/plugins/bot-forge when it is a git repo."""
    candidate = Path(root) / "plugins" / "bot-forge"
    if (candidate / ".git").is_dir():
        return candidate
    return None


def _owner_repo(repo: Path) -> str:
    rc, url, _ = _git(repo, "remote", "get-url", "origin")
    if rc != 0:
        return ""
    url = url.removesuffix(".git").rstrip("/")
    for marker in ("github.com/", "github.com:"):
        if marker in url:
            parts = url.split(marker, 1)[1].split("/")
            return "/".join(parts[:2])  # owner/repo
    tail = url.rsplit("/", 2)
    return "/".join(tail[-2:]) if len(tail) >= 2 else url


def _status(repo: Path) -> dict:
    rc, head, err = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        return {"ok": False, "error": f"not a usable git checkout: {err}"}
    branch_rc, branch, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    rc_a, ahead, _ = _git(repo, "rev-list", "--count", f"{UPSTREAM_REF}..HEAD")
    rc_b, behind, _ = _git(repo, "rev-list", "--count", f"HEAD..{UPSTREAM_REF}")
    rc_u, upstream_head, _ = _git(repo, "rev-parse", "--verify", UPSTREAM_REF)
    rc_d, dirty, _ = _git(repo, "status", "--porcelain")
    rc_l, last_log, _ = _git(repo, "log", "-1", "--format=%s", UPSTREAM_REF) if rc_u == 0 else (1, "", "")
    return {"ok": True, "op": "status",
            "branch": branch if branch_rc == 0 else "?",
            "head": head[:12],
            "upstream_available": rc_u == 0,
            "upstream_head": (upstream_head or "")[:12] if rc_u == 0 else None,
            "upstream_latest": last_log or None,
            "ahead": int(ahead) if rc_a == 0 else None,
            "behind": int(behind) if rc_b == 0 else None,
            "dirty": bool(dirty.strip()) if rc_d == 0 else None,
            "fork": _owner_repo(repo)}


def op_status(s: dict, repo: Path, settings: dict) -> dict:
    return _status(repo)


def op_sync(s: dict, repo: Path, settings: dict) -> dict:
    before = _status(repo)
    rc, _, err = _git(repo, "fetch", REMOTE, BRANCH, timeout=300)
    if rc != 0:
        return {"ok": False, "op": "sync", "error": f"git fetch {REMOTE} {BRANCH} failed: {err}"}
    rc, out, err = _git(repo, "rebase", UPSTREAM_REF, timeout=600)
    if rc != 0:
        if "uncommitted changes" in (out + err) or "cannot rebase" in (out + err):
            return {"ok": False, "op": "sync", "rebased": False, "aborted": False,
                    "conflicts": [], "error": err or out,
                    "hint": "The checkout has uncommitted changes. Commit them or stash "
                            "before syncing (never sync a dirty checkout).",
                    "before": before}
        rc_c, conflicts, _ = _git(repo, "diff", "--name-only", "--diff-filter", "U")
        conflicted = [line for line in conflicts.splitlines() if line.strip()]
        _git(repo, "rebase", "--abort")  # leave the checkout as it was; conflicts need a human
        return {"ok": False, "op": "sync", "rebased": False, "aborted": True,
                "conflicts": conflicted, "error": err or out,
                "hint": "Resolve by hand (see vault rebase runbook): rebase, edit each "
                        "file, grep for '<<<<<<<', git add, git rebase --continue.",
                "before": before}
    after = _status(repo)
    return {"ok": True, "op": "sync", "rebased": True,
            "moved": before.get("head") != after.get("head"),
            "ahead": after.get("ahead"), "behind": after.get("behind"),
            "upstream_latest": after.get("upstream_latest"),
            "next": "run the test suite (hermes verify or unittest discover) before promoting",
            "before": before}


def _wanted_profiles(root: Path, wanted: list[str]) -> "tuple[list[str], list[str]]":
    """(profiles to adopt, skipped) — a directory in profiles/ is only a profile when its
    name is; leftovers (e.g. '.deleted' tombstones) are skipped, not adopted."""
    import re
    profiles_dir = Path(root) / "profiles"
    valid = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    if not profiles_dir.is_dir():
        return [], []
    have, skipped = [], []
    for d in sorted(profiles_dir.iterdir()):
        if not d.is_dir():
            continue
        (have if valid.match(d.name) else skipped).append(d.name)
    if wanted:
        missing = [w for w in wanted if w not in have]
        if missing:
            raise LookupError(f"no such profile(s): {', '.join(missing)}")
        return wanted, []
    return have, skipped


def op_adopt(s: dict, repo: Path, settings: dict) -> dict:
    sha_rc, sha, err = _git(repo, "rev-parse", "HEAD")
    if sha_rc != 0:
        return {"ok": False, "op": "adopt", "error": f"cannot resolve HEAD: {err}"}
    fork = _owner_repo(repo)
    if not fork:
        return {"ok": False, "op": "adopt", "error": "origin remote has no usable GitHub URL"}
    try:
        wanted, skipped = _wanted_profiles(Path(s.get("hermes_root") or Path.home() / ".hermes"),
                                           [p.strip().lower() for p in (s.get("profiles") or [])])
    except LookupError as exc:
        return {"ok": False, "op": "adopt", "error": str(exc)}
    results = []
    for name in wanted:
        p = subprocess.run(
            ["hermes", "-p", name, "plugins", "install", fork, "--ref", sha,
             "--force", "--yes-deps", "--enable"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, stdin=subprocess.DEVNULL)
        tail = (p.stdout or "").strip().splitlines()
        results.append({"profile": name, "returncode": p.returncode,
                        "detail": " | ".join(tail[-2:]) if tail else (p.stderr or "")[-300:]})
    return {"ok": all(r["returncode"] == 0 for r in results), "op": "adopt",
            "ref": sha[:12], "fork": fork, "profiles": results, "skipped": skipped,
            "next": "restart gateways, then doctor: gateway and version should go green"}


HANDLERS = {"status": op_status, "sync": op_sync, "adopt": op_adopt}


def fork(s: dict) -> dict:
    root = s.get("hermes_root") or str(Path.home() / ".hermes")
    repo = _repo_dir(Path(root))
    if repo is None:
        return {"ok": False, "error": "no git checkout at <hermes root>/plugins/bot-forge"}
    op = HANDLERS.get((s.get("op") or "").strip().lower())
    if not op:
        return {"ok": False, "error": f"unknown op; use one of {', '.join(OPS)}"}
    try:
        return op(s, repo, s.get("settings") or {})
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{s.get('op')} timed out"}


def main():
    try:
        raw = sys.stdin.read() if len(sys.argv) < 2 or sys.argv[1] == "-" else Path(sys.argv[1]).read_text(encoding="utf-8")
        result = fork(json.loads(raw))
    except json.JSONDecodeError as exc:
        result = {"ok": False, "error": f"input is not valid JSON: {exc}"}
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
