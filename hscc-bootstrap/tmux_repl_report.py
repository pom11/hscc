#!/usr/bin/env python3
"""Report running Hermes REPLs split by tmux visibility — one line per fact.

Used by bootstrap.sh ("Install: tmux (iOS chat reachability)"). The iOS app
delivers chat into a tmux PANE (hscc-api/repl_inject), so the operator needs
to know which of their REPLs the phone can actually reach. Reuses the ONE
matcher (flightdeck/core/tmux_chat.chat_processes + pane_ttys — themselves
thin delegations to repl_inject's canonical pane matcher) — bootstrap never
reimplements discovery.

Output (stdout, one per line; exit 0 unless the helper itself broke):
  TMUX_MISSING
  TMUX_UNVERIFIED <error>
  IN_TMUX <profile> <tty>
  OUTSIDE_TMUX <profile> <tty>

Usage: tmux_repl_report.py <repo_or_plugins_root>   (the dir holding hscc-project/)
"""
import importlib.util
import shutil
import sys
from pathlib import Path


def _load_tmux_chat(root: str):
    """Load flightdeck/core/tmux_chat.py by path (stdlib-only module, so no
    package context and no dependency on how hscc was installed)."""
    p = Path(root) / "hscc-project" / "flightdeck" / "core" / "tmux_chat.py"
    if not p.is_file():
        print(f"TMUX_UNVERIFIED tmux_chat.py not found under {root}")
        return None
    spec = importlib.util.spec_from_file_location("hscc_tmux_chat_report", p)
    if spec is None or spec.loader is None:
        print(f"TMUX_UNVERIFIED cannot load {p}")
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    if not shutil.which("tmux"):
        print("TMUX_MISSING")
        return 0
    mod = _load_tmux_chat(root)
    if mod is None:
        return 0
    try:
        ttys = mod.pane_ttys()
        procs = mod.chat_processes()
    except Exception as exc:  # never claim the REPLs are unreachable
        print(f"TMUX_UNVERIFIED {exc}")
        return 0
    for tty, profile in sorted(procs, key=lambda r: (r[1], r[0])):
        print(("IN_TMUX " if tty in ttys else "OUTSIDE_TMUX ")
              + f"{profile} {tty}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
