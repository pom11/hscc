#!/usr/bin/env python3
"""B5 standalone: CLI `hermes chat --continue <project-title>` shows the
phone-originated message from the named session — run AFTER the driver/TUI has
fully released the session, on the persisted session (no live PTY owner)."""
import os, subprocess, sys

HOME = "/tmp/hscc_e2e_home"
PHONE_TAG = "E2EPHONE_7f3a9c2e"
env = dict(os.environ)
env["HERMES_HOME"] = HOME
env["HERMES_DELEGATED_CHILD_CONTEXT"] = "0"
hermes_bin = os.path.join(os.path.dirname(sys.executable), "hermes")

cmd = [hermes_bin, "chat", "--continue", "hscc", "-q",
       f"In our conversation history there is a message tagged {PHONE_TAG}. "
       "Reply with exactly that tag followed by CONTINUED_OK.", "-Q"]
print("cmd:", " ".join(cmd))
p = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
print("exit:", p.returncode)
print("--- stdout ---")
print((p.stdout or "")[-900:])
print("--- stderr ---")
print((p.stderr or "")[-600:])
ok = PHONE_TAG in (p.stdout or "")
print("\nB5_STANDALONE", "PASS" if ok else "FAIL")
