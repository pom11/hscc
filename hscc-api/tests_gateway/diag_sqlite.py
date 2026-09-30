#!/usr/bin/env python3
"""Enumerate every session + message in the scratch state.db (sqlite-level)."""
import sqlite3
from pathlib import Path

conn = sqlite3.connect(Path("/tmp/hscc_e2e_home/state.db"))
cur = conn.cursor()
# list tables
try:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    print("tables:", tables)
except Exception as e:
    print("tables err:", repr(e))

# sessions table if present
for tbl in ("sessions", "session", "chat"):
    try:
        cur.execute(f"PRAGMA table_info({tbl})")
        cols = [r[1] for r in cur.fetchall()]
        print(f"--- {tbl} columns: {cols}")
        cur.execute(f"SELECT * FROM {tbl}")
        rows = cur.fetchall()
        print(f"--- {tbl} rows: {len(rows)}")
        for r in rows[:20]:
            print("   ", r)
    except Exception as e:
        print(f"{tbl} err:", repr(e))
conn.close()
