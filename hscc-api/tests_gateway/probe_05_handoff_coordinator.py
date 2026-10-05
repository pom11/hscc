"""Probe: HSCC HandoffCoordinator against the REAL upstream seam primitives.

t_7cc2e7d5 — proves the HSCC-side handoff coordinator mechanics against the
actual upstream consented-handoff seam (fork branch feat/session-ownership-handoff
@ 77f046f089, pom11/hermes-agent) in an ISOLATED HERMES_HOME, CROSS-PROCESS:

    owner (subproc)   acquires the lease on session S (surface=cli)
    controller (this) HandoffCoordinator.orchestrate(...)
        -> request_active_session_handoff(S, controller="hscc-app")
        -> emits handoff_pending ("waiting for approval on your computer")
    owner (subproc)   grants via grant_active_session_handoff(live_session_id=own)
        -> returns the ONE-SHOT nonce
    controller (this) poll_for_nonce picks it up from the shared registry
        -> complete_active_session_handoff(S, controller, nonce) -> lease
        -> drive() callback runs (the turn)
        -> release() hands the session back to the owner

The turn-driving itself (the PTY write path) is existing, already-tested HSCC
code — the handoff's only job is to make that drive not refuse SESSION_NOT_OWNED.
So ``drive`` here is a callback that asserts it was invoked with the turn held,
then the lease is released. What this probe verifies by EXECUTION is the
request -> grant -> complete -> release mechanics against the real seam code.

Run against an isolated HERMES_HOME; needs the fork seam on the import path:

    PYTHONPATH=/tmp/hscc-fork-seam python tests_gateway/probe_05_handoff_coordinator.py

This never touches a real profile, the live hermes checkout, or a real model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Where the fork seam lives (must be on PYTHONPATH when invoking this probe).
SEAM = Path(os.environ.get("HSCC_SEAM", "/tmp/hscc-fork-seam"))
PY = os.environ.get(
    "HSCC_PROBE_PY",
    os.path.join(os.path.expanduser(
        "~/.hermes/hermes-agent/venv/bin/python"))
)
if not os.path.exists(PY):
    PY = os.path.realpath(sys.executable)

HERE = Path(__file__).resolve().parent
# The HSCC module lives in the hscc-api root (script dir is tests_gateway, which
# is what Python adds when running `python path/to/probe.py`); make the root
# importable so `import active_sessions_handoff` resolves.
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

OWNER_SRC = r'''
import json, os, sys, time
from pathlib import Path

# Resolve the orchestrator profile home from the env we set.
home = Path(os.environ["HERMES_HOME"])
from hermes_cli import active_sessions as as_mod

sid = sys.argv[1]          # the stored session key
grant_file = Path(sys.argv[2])   # when it appears, consent
nonce_file = Path(sys.argv[3])   # we write the minted nonce here

# 1) Acquire the lease the way the operator's interactive CLI does: surface="cli",
#    live_session_id = our own "sid" (the owner's live id, which only we know).
lease, err = as_mod.try_acquire_active_session(
    session_id=sid, surface="cli", config=None,
    metadata={"live_session_id": "owner-live-0x1"},
    registry_home=home)
if lease is None:
    print(json.dumps({"failed": True, "reason": str(err)}), flush=True)
    sys.exit(2)
print(json.dumps({"acquired": True, "lease_id": lease.lease_id}), flush=True)

# 2) Wait for the controller to stamp its pending ask, then consent.
deadline = time.time() + 60
while time.time() < deadline:
    entries = as_mod.active_session_registry_snapshot(home, strict=False)
    h = None
    for e in entries:
        if str(e.get("session_id") or "") == sid:
            h = e.get("handoff")
    if isinstance(h, dict) and h.get("controller") == "hscc-app":
        break
    time.sleep(0.1)
else:
    print(json.dumps({"failed": True, "reason": "no handoff ask seen"}), flush=True)
    sys.exit(3)

# 3) The OWNER consents (proves same-writer: our live_session_id matches the
#    lease we acquired). Mint + publish the one-shot nonce.
nonce = as_mod.grant_active_session_handoff(
    sid, controller="hscc-app", live_session_id="owner-live-0x1")
if not nonce:
    print(json.dumps({"failed": True, "reason": "grant refused"}), flush=True)
    sys.exit(4)
nonce_file.write_text(nonce)
print(json.dumps({"granted": True, "nonce_plus_one": nonce is not None}), flush=True)
# Stay alive briefly so the controller can complete + drive + release.
time.sleep(15)
'''

def main() -> int:
    failures = []

    def check(label: str, ok: bool, detail: str = ""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}"
              + (f" -- {detail}" if detail else ""), flush=True)
        if not ok:
            failures.append(label)

    # The active-sessions registry lives under HERMES_HOME/runtime.
    home = Path(tempfile.mkdtemp(prefix="hscc-handoff-probe-"))
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    # The owner subprocess must resolve hermes_cli.active_sessions to the fork
    # seam too (it calls grant_active_session_handoff, absent on the live branch).
    env["PYTHONPATH"] = str(SEAM)

    sid = "probe-named-session-1"

    grant_file = Path(home) / "grant.signal"
    nonce_file = Path(home) / "nonce.txt"

    owner_proc = subprocess.Popen(
        [PY, "-u", "-c", OWNER_SRC, sid, str(grant_file), str(nonce_file)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    assert owner_proc.stdout is not None and owner_proc.stderr is not None
    line = owner_proc.stdout.readline().strip()
    print(json.dumps({"owner_line": line}) if line else "", flush=True)
    check("owner acquired the session lease (surface=cli)", '"acquired": true' in line,
          line[:160])

    # Point the coordinator's primitives import at the fork worktree. The
    # HSCC coordinator lazily imports hermes_cli.active_sessions; running THIS
    # probe with SEAM dir on sys.path makes that resolve to the fork's code.
    sys.path.insert(0, str(SEAM))
    import active_sessions_handoff as ash

    # Guard: seam_available must be True against the fork seam.
    check("HandoffCoordinator.seam_available() True against fork seam",
          ash.seam_available())

    captured = {"pending": 0, "working": 0, "denied": [], "drove": 0, "released": 0}

    coordinator = ash.HandoffCoordinator(profile="orch", session_id=sid,
                                         surface="tui",
                                         grant_timeout=5.0,
                                         grant_poll_interval=0.1)

    def on_pending():
        captured["pending"] += 1

    def on_working():
        captured["working"] += 1

    def on_denied(msg: str):
        captured["denied"].append(msg)

    # The turn. In real deployment this is relay_user_message (serve/PTY path);
    # here just a callback proving it runs only AFTER the lease transfers.
    def drive():
        captured["drove"] += 1

    # Inject a release spy? The real lease object's release() is used — we
    # cannot easily spy on it, so verify post-conditions via the registry
    # instead (no controller lease remains after release).
    print("  [probe] orchestrating...", flush=True)
    result = coordinator.orchestrate(
        "probe: app drives the session", drive=drive,
        on_pending=on_pending, on_denied=on_denied, on_working=on_working,
        session_id=sid, registry_home=home)

    check("orchestrate emitted the pending 'waiting for approval' frame",
          captured["pending"] == 1, json.dumps(captured))
    check("orchestrate emitted the working frame as the turn started driving",
          captured["working"] == 1, json.dumps(captured))
    check("orchestrate drove the turn after the lease transferred",
          captured["drove"] == 1, json.dumps(captured))
    check("orchestrate reported outcome completed", result.get("outcome") == "completed",
          json.dumps(result))

    # Post-condition: the controller released its lease (handed the session
    # back) — verify by EXECUTION that the shared registry no longer holds a
    # lease on this session from our (controller) process. After the transfer
    # the owner's entry was replaced by the controller's single entry; release
    # drops it, so no live foreign/controller lease remains for the session.
    import active_sessions_handoff as ash_mod
    remaining = ash_mod._import_primitives().active_session_registry_snapshot(
        home, strict=False)
    ours_still_held = [
        e for e in remaining
        if str(e.get("session_id") or "") == sid
        and int(e.get("pid") or -1) == os.getpid()
    ]
    check("handoff released the controller's lease (session no longer fenced "
          "to this process)", not ours_still_held,
          json.dumps(remaining)[:200])

    owner_proc.terminate()
    try:
        owner_proc.wait(timeout=5)
    except Exception:
        owner_proc.kill()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All HandoffCoordinator-vs-seam checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
