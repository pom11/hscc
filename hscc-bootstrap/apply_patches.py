"""Reapply HSCC's curated upstream patches onto official hermes/sparkrun.

HSCC runs official upstream + a small captured delta (see ../patches/MANIFEST.md).
This script applies (or dry-run checks) the `git format-patch` artifacts onto a
target checkout, so updating = pull upstream + rerun this, instead of
maintaining a fork.

Usage:
  apply_patches.py --check                       # dry-run all sets
  apply_patches.py --target <dir> --set hermes   # apply hermes patches
  apply_patches.py --target <dir> --set sparkrun # apply sparkrun patches
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
    patches = list_patches(set_name)
    if not patches:
        return {"ok": False, "set": set_name, "error": f"no patches in {SETS.get(set_name)}"}
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reapply HSCC upstream patches.")
    ap.add_argument("--check", action="store_true", help="dry-run (git apply --check)")
    ap.add_argument("--target", help="path to the upstream checkout")
    ap.add_argument("--set", dest="set_name", choices=list(SETS),
                    help="which patch set (default: all in --check)")
    args = ap.parse_args(argv)

    import json
    if args.check and not args.target:
        # check all sets against their conventional locations
        targets = {"hermes": Path(os.path.expanduser("~/.hermes/hermes-agent")),
                   "sparkrun": Path(os.path.expanduser("~/sparkrun"))}
        out = {}
        for name, tgt in targets.items():
            out[name] = (apply_set(name, tgt, check=True) if tgt.is_dir()
                         else {"ok": False, "error": f"{tgt} not found"})
        print(json.dumps(out, indent=2))
        return 0 if all(v.get("ok") for v in out.values()) else 1

    if not args.target or not args.set_name:
        ap.error("--target and --set are required unless using --check alone")
    res = apply_set(args.set_name, Path(os.path.expanduser(args.target)), check=args.check)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
