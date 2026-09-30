#!/usr/bin/env python3
"""Diagnostic: dump the scratch state.db's sessions + the named session msgs."""
import sys
sys.path.insert(0, "/Users/desac/dev/hscc/.worktrees/t_59cc6714/hscc-api")
from hermes_state import SessionDB

from pathlib import Path
db = SessionDB(db_path=Path("/tmp/hscc_e2e_home/state.db"))
try:
    try:
        sess = db.list_sessions()
        print("sessions:", sess)
    except Exception as e:
        print("list_sessions err:", repr(e))
    sid = "20260930_e2e_named_hscc"
    try:
        msgs = db.get_messages(sid)
        print("count in", sid, ":", len(msgs))
        for m in msgs:
            print("  role=", m.get("role"), ":", str(m.get("content", ""))[:80])
    except Exception as e:
        print("get_messages err:", repr(e))
finally:
    db.close()
