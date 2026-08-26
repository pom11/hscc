#!/usr/bin/env python3
"""Real-CLI smoke test for the dispatch cluster-readiness guard (tmp HOME).

Runs the REAL flightdeck CLI ('hscc project message dispatch') with the real
hscc_daemon.autodown on sys.path (as it is in the production 'hscc project'
process), pointed at a TMP HOME so it never touches the live ~/.hscc or
~/.hermes. Fakes + tmp state only per the operator constraint:

  1. `flightdeck message dispatch <project> <task>` DRY-RUN (no --apply)
     against a tmp-HOME registry -> proves the real parser + command wiring
     are intact and no card is created (dry-run) and no wake is triggered.
  2. The real `_ensure_cluster_ready` guard invoked against the real
     hscc_daemon.autodown module with a fake autodown.json at tmp HOME, and
     autoup monkeypatched to a fake, to prove the `state=up` (no wake) and
     `state=down` (would wake + wait) DECISIONS without a real wake.

No real teardown/wake, no Telegram, no live board, no real card.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent  # …/hscc (repo root)
sys.path.insert(0, str(REPO / "hscc-project"))
sys.path.insert(0, str(REPO))

# Set HOME BEFORE importing hscc_daemon / flightdeck: many module-level
# constants (autodown.AUTODOWN_FILE, registry.DEFAULT_REGISTRY, ...) expand
# "~" at import time. Setting HOME first pins every such path under tmp so the
# smoke test can only ever touch the sandbox, never the live ~/.hscc/~/.hermes.
tmp_home = tempfile.mkdtemp(prefix="hscc-smoke-")
os.environ["HOME"] = tmp_home

from hscc_daemon import autodown  # noqa: E402
from flightdeck.cli import main as flightdeck_main  # noqa: E402
from flightdeck.commands import message as msg_cmd  # noqa: E402

os.makedirs(f"{tmp_home}/.hscc", exist_ok=True)
autodown_path = os.path.expanduser("~/.hscc/autodown.json")
assert autodown_path.startswith(tmp_home), autodown_path  # safety: tmp only

reg_path = f"{tmp_home}/registry.yaml"
Path(reg_path).write_text(
    "projects:\n"
    "  - name: smoke\n"
    "    repo: ~/dev/hscc\n"
    "    topic: 999\n"
    "    board: smoke\n"
)

print(f"### tmp HOME: {tmp_home}")
print()

# Real lazy importer resolves the REAL hscc_daemon.autodown module here.
real_ad = msg_cmd._load_autodown()
print(f"### _load_autodown() resolved to: {real_ad.__name__ if real_ad else None}")

# --- 1. Real CLI dry-run dispatch (no card, no wake) ---------------------
print()
print("===== [1] REAL CLI dry-run dispatch (tmp HOME; no --apply) =====")
argv = ["--registry", reg_path, "message", "dispatch", "smoke", "build the widget"]
print(f"$ flightdeck {' '.join(argv)}")
rc = flightdeck_main(argv)
print(f"### dry-run exit code: {rc}")
print()

# --- 2. Real guard decisions against a fake autodown.json ----------------
print("===== [2] REAL guard function, real autodown module, tmp-HOME state =====")
for state in ("up", "down"):
    cfg = {"enabled": True, "state": state, "reason": ""}
    with open(autodown_path, "w") as f:
        json.dump(cfg, f, indent=2)

    calls = {"n": 0}

    def fake_autoup():
        calls["n"] += 1
        return {"result": "up"}

    orig_autoup = autodown.autoup
    autodown.autoup = fake_autoup
    try:
        ns = argparse.Namespace(no_wait=False)
        err = msg_cmd._ensure_cluster_ready(
            real_ad, ns, out=lambda s: print("    " + s))
        print(f"  state={state}: guard_error={err!r}  autoup_calls={calls['n']}")
    finally:
        autodown.autoup = orig_autoup
    print()
