#!/usr/bin/env python3
"""Wait for the session turn-lease to clear, then run B5 CLI --continue."""
import os, subprocess, sys, time, sqlite3
from pathlib import Path

HOME = Path("/tmp/hscc_e2e_home")
SID = "20260930_e2e_named_hscc"
PHONE_TAG = "E2EPHONE_7f3a9c2e"
STATE = HOME / "state.db"

def lease_held():
    try:
        conn = sqlite3.connect(str(STATE))
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM session_turn_leases WHERE conversation_id=?", (SID,))
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False

waited = 0
while lease_held() and waited < 540:
    print(f"lease held ({waited}s); waiting 30s...")
    time.sleep(30); waited += 30
print("lease clear after", waited, "s")

env = dict(os.environ)
env["HERMES_HOME"] = str(HOME)
env["HERMES_DELEGATED_CHILD_CONTEXT"] = "0"
hermes_bin = os.path.join(os.path.dirname(sys.executable), "hermes")
cmd = [hermes_bin, "chat", "--continue", "hscc", "-q",
       f"In our conversation history there is a message tagged {PHONE_TAG}. "
       "Reply with exactly that tag followed by CONTINUED_OK.", "-Q"]
print("cmd:", " ".join(cmd))
p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
print("exit:", p.returncode)
print("--- stdout ---")
print((p.stdout or "")[-1000:])
print("--- stderr ---")
print((p.stderr or "")[-600:])
ok = PHONE_TAG in (p.stdout or "")
print("\nB5_FINAL", "PASS" if ok else "FAIL")
