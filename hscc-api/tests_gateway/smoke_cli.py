#!/usr/bin/env python3
"""Smoke test: seed a named session + run `hermes chat --continue <title>`
against the scratch home to confirm the corrected provider config answers."""
import os, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/Users/desac/dev/hscc/.worktrees/t_59cc6714/hscc-api")
from hermes_state import SessionDB

HOME = Path("/tmp/hscc_e2e_home")
SID = "20260930_e2e_named_hscc"
db = SessionDB(db_path=HOME / "state.db")
try:
    db.create_session(SID, source="cli", model="worker-model", profile_name="hscc-orch")
    db.set_session_title(SID, "hscc")
    db.append_messages_batch(SID, [
        {"role": "user", "content": "seed user", "timestamp": 1759200000},
        {"role": "assistant", "content": "seed assistant", "timestamp": 1759200050},
    ])
finally:
    db.close()

env = dict(os.environ)
env["HERMES_HOME"] = str(HOME)
env["HERMES_DELEGATED_CHILD_CONTEXT"] = "0"
hermes_bin = os.path.join(os.path.dirname(sys.executable), "hermes")
cmd = [hermes_bin, "chat", "--continue", "hscc", "-q",
       "Reply with exactly the single word CONTINUED_OK and nothing else.", "-Q"]
print("cmd:", " ".join(cmd))
p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
print("exit:", p.returncode)
print("--- stdout ---")
print((p.stdout or "")[-800:])
print("--- stderr ---")
print((p.stderr or "")[-600:])
ok = "CONTINUED_OK" in (p.stdout or "")
print("SMOKE", "PASS" if ok else "FAIL")
