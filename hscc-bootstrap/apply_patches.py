"""Reapply HSCC's curated upstream patches onto official hermes/sparkrun.

HSCC runs official upstream + a small captured delta (see ../patches/MANIFEST.md).
This script applies (or dry-run checks) the `git format-patch` artifacts onto a
target checkout, so updating = pull upstream + rerun this, instead of
maintaining a fork.

Usage:
  apply_patches.py --check                       # dry-run all sets
  apply_patches.py --target <dir> --set hermes   # apply hermes patches
  apply_patches.py --target <dir> --set sparkrun # apply sparkrun patches

The `--check` default for the hermes set resolves the `hermes-agent` checkout
through the shared homes ladder (`hscc_daemon.hermes_python.hermes_agent_dir`),
NOT through a `$HERMES_HOME/hermes-agent` join: Hermes exports HERMES_HOME as
`<root>/profiles/<name>` for every profile session, so the raw join resolved a
path that cannot exist in the fleet's normal run mode. `--check` then reported
the checkout "not found", `bootstrap.sh` misread that as "already patched /
upstream drifted" and skipped the `--target` apply — the hermes patch set
silently never landed (t_8579fba4). The chosen candidate is printed as
`target` in the JSON so the resolution is auditable.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = REPO_ROOT / "patches"
SETS = {"hermes": PATCH_DIR / "hermes", "sparkrun": PATCH_DIR / "sparkrun"}
#: Conventional checkout dir for the sparkrun set (root-level, not Hermes-owned).
SPARKRUN_DIR = Path(os.path.expanduser("~/sparkrun"))


def list_patches(set_name: str) -> list[Path]:
    d = SETS.get(set_name)
    if not d or not d.is_dir():
        return []
    return sorted(d.glob("*.patch"))


def _git(target: Path, *args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(target), *args],
                          capture_output=True, text=True, timeout=timeout)


def apply_set(set_name: str, target: Path, *, check: bool) -> dict:
    """Apply (or --check) a patch set onto target. Returns a result dict.

    check=True uses `git apply --check` (no mutation) per patch and reports
    which would fail. check=False uses `git am` to apply with commit metadata;
    on failure it aborts the am and reports the offending patch."""
    patch_dir = SETS.get(set_name)
    if not patch_dir:
        return {"ok": False, "set": set_name, "mode": "check",
                "error": f"unknown patch set: {set_name!r}"}
    if not patch_dir.is_dir():
        # A MISSING patch directory is an environment/repo fault, never the same
        # as a legitimately-empty set. Reporting ok:true here is a silent-failure
        # footgun: bootstrap.sh:236-241 greps for '"ok": true' then applies and
        # prints 'hermes patches applied' — a fresh checkout would silently never
        # receive the kanban review/resume patches. Fail loud instead.
        return {"ok": False, "set": set_name, "mode": "check",
                "error": f"patch directory not found: {patch_dir}"}
    patches = list_patches(set_name)
    if not patches:
        # Intentionally empty patch set (patches landed upstream) — quiet success.
        return {"ok": True, "set": set_name, "mode": "check", "patches": []}
    # A normal checkout has a .git dir; a worktree/submodule has a .git file.
    if not (target / ".git").exists():
        return {"ok": False, "set": set_name, "error": f"{target} is not a git checkout"}

    results = []
    if check:
        for p in patches:
            r = _git(target, "apply", "--check", str(p))
            ok = r.returncode == 0
            results.append({"patch": p.name, "applies": ok,
                            "error": (r.stderr.strip() or None) if not ok else None})
        all_ok = all(x["applies"] for x in results)
        return {"ok": all_ok, "set": set_name, "mode": "check", "patches": results}

    # apply for real via git am (preserves author/message); abort cleanly on fail
    for p in patches:
        r = _git(target, "am", str(p))
        if r.returncode != 0:
            _git(target, "am", "--abort")
            results.append({"patch": p.name, "applied": False,
                            "error": (r.stderr or r.stdout).strip()})
            return {"ok": False, "set": set_name, "mode": "apply",
                    "applied_count": len(results) - 1, "failed_on": p.name,
                    "patches": results}
        results.append({"patch": p.name, "applied": True})
    return {"ok": True, "set": set_name, "mode": "apply",
            "applied_count": len(results), "patches": results}


def _load_hermes_python():
    """Import ``hscc_daemon/hermes_python.py`` for the runtime-checkout ladder.

    Deferred + guarded, same pattern as ``ensure_review_feature._load_hermes_python``:
    the resolution rule lives with the shared ladder (t_314eb13c) so this script
    cannot drift from how the rest of HSCC finds the Hermes install. Returns the
    module, or None when hscc_daemon is not deployed next to this plugin (a
    bootstrap-only install), in which case :func:`_fallback_agent_dirs` covers us.
    """
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import hermes_python
        return hermes_python
    except Exception:
        return None


def _fallback_agent_dirs(env=None, *, home=None) -> list[str]:
    """Candidate ``hermes-agent`` checkouts when ``hscc_daemon`` is unavailable.

    Deliberately the SAME three steps as ``hermes_python.hermes_homes`` —
    exported ``$HERMES_HOME``, its ``profiles/`` grandparent, then
    ``~/.hermes`` — because a shorter list here is exactly the bug being fixed:
    any resolver that looks only at the exported home reports "no checkout" from
    every profile session. ``test_ladder_drift`` pins the two against each other.
    """
    env = os.environ if env is None else env
    home = home or os.path.expanduser("~")
    homes: list[str] = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in homes:
            homes.append(path)

    _push(env.get("HERMES_HOME", "").strip())
    if homes:
        parent = os.path.dirname(homes[0])
        if parent and os.path.basename(parent) == "profiles":
            _push(os.path.dirname(parent))
    _push(os.path.join(os.path.expanduser(str(home)), ".hermes"))
    return [os.path.join(h, "hermes-agent") for h in homes]


def agent_dir_candidates(env=None, *, home=None) -> list[str]:
    """Candidate ``hermes-agent`` checkout paths, in the order they are tried.

    Shared-module ladder when available, :func:`_fallback_agent_dirs` otherwise.
    Non-existent candidates are kept so callers can report what was ruled out.
    """
    hp = _load_hermes_python()
    if hp is not None:
        return hp.agent_dir_candidates(env, home=home)
    return _fallback_agent_dirs(env, home=home)


def resolve_agent_dir(env=None, *, home=None) -> tuple[str | None, str]:
    """Resolve the hermes-agent checkout for the --check default.

    Returns ``(path_or_None, searched_detail)`` — same contract as
    ``ensure_review_feature.resolve_hermes_dir``. ``None`` means no candidate
    exists anywhere searched: the caller must report that as "no runtime here",
    never let it read as "already patched" (t_8579fba4).
    """
    cands = agent_dir_candidates(env, home=home)
    for cand in cands:
        try:
            if os.path.isdir(cand):
                return cand, ""
        except OSError:  # pragma: no cover - unreadable path is not a hit
            continue
    return None, ", ".join(cands) or "no candidate paths"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reapply HSCC upstream patches.")
    ap.add_argument("--check", action="store_true", help="dry-run (git apply --check)")
    ap.add_argument("--target", help="path to the upstream checkout")
    ap.add_argument("--set", dest="set_name", choices=list(SETS),
                    help="which patch set (default: all in --check)")
    args = ap.parse_args(argv)

    import json
    if args.check and not args.target:
        # check all sets against their conventional locations. The hermes
        # checkout comes from the shared homes ladder, NOT a $HERMES_HOME join
        # (raw join = "not found" from every profile session = the apply step
        # skipped forever; see the module docstring). Each set's resolved target
        # is echoed in the JSON so a caller can audit which candidate won.
        hermes_dir, searched = resolve_agent_dir()
        targets = {"hermes": Path(hermes_dir) if hermes_dir else None,
                   "sparkrun": SPARKRUN_DIR}
        out = {}
        for name, tgt in targets.items():
            if tgt is None:
                out[name] = {"ok": False,
                             "error": f"hermes-agent checkout not found (looked: {searched})"}
                continue
            res = apply_set(name, tgt, check=True) if tgt.is_dir() \
                else {"ok": False, "error": f"{tgt} not found"}
            res["target"] = str(tgt)
            out[name] = res
        print(json.dumps(out, indent=2))
        return 0 if all(v.get("ok") for v in out.values()) else 1

    if not args.target or not args.set_name:
        ap.error("--target and --set are required unless using --check alone")
    res = apply_set(args.set_name, Path(os.path.expanduser(args.target)), check=args.check)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
