#!/usr/bin/env python3
"""Inspect session_turn_leases in the scratch state.db (ownership gate)."""
import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path("/tmp/hscc_e2e_home/state.db"))
cur = conn.cursor()
try:
    cur.execute("PRAGMA table_info(session_turn_leases)")
    print("cols:", [r[1] for r in cur.fetchall()])
    cur.execute("SELECT * FROM session_turn_leases")
    for r in cur.fetchall():
        print("row:", r)
except Exception as e:
    print("err:", repr(e))
conn.close()
