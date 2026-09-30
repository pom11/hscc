#!/usr/bin/env python3
"""Print now and the lease expiry to see how long until the TUI lease clears."""
import sqlite3, time
from pathlib import Path
conn = sqlite3.connect(Path("/tmp/hscc_e2e_home/state.db"))
cur = conn.cursor()
cur.execute("SELECT expires_at FROM session_turn_leases")
rows = cur.fetchall()
now = time.time()
print("now:", now)
for (exp,) in rows:
    print("expires_at:", exp, "seconds until free:", round(exp - now, 1))
conn.close()
