"""End-to-end verifier for card t_1b206473 — proves the hard invariants with the
REAL subprocess CLI (not pytest), before the change is merged.

Runs `flightdeck archive-sessions` as a real piped subprocess (stdout = pipe,
not a tty) and asserts, per converted command:
  1. human output contains NO ANSI (\\x1b) bytes — the parser contract;
  2. --json output stays byte-identical to the canonical json.dumps (proved
     directly below for archive-sessions --json), and contains no ANSI.

Among group C sub-e (archive/hygiene/incident/reconcile), `archive-sessions` is
the only one that runs purely on local file I/O (a throwaway state.db + a
throwaway registry + an output dir) with no git, board or network — so it is the
clean real-subprocess demo for BOTH no-ANSI AND --json byte-identity. The
injectable-git/board-backed proofs for reconcile/hygiene (and incident's no-ANSI)
live in the pytest no-ANSI regression classes in the per-command test files.

Uses a throwaway state.db, registry and output dir under /tmp — never the
operator's.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

ROOT = "/Users/desac/.hermes/kanban/boards/hscc/workspaces/t_1b206473"
PY = "/Users/desac/.hermes/hermes-agent/venv/bin/python"
HSCC_PROJECT = os.path.join(ROOT, "hscc-project")
sys.path.insert(0, HSCC_PROJECT)
from flightdeck.core import registry  # noqa: E402

FAIL = []


def run(args, env=None):
    e = dict(os.environ)
    e["PYTHONPATH"] = HSCC_PROJECT + os.pathsep + ROOT
    if env:
        e.update(env)
    p = subprocess.run(
        [PY, "-c", "from flightdeck.cli import main; raise SystemExit(main())"] + args,
        capture_output=True, text=True, cwd=ROOT, env=e)
    return p.returncode, p.stdout, p.stderr


def check(label, cond, detail=""):
    print(f"{label}: {'OK' if cond else 'FAIL'}{('  ' + detail) if (not cond and detail) else ''}")
    if not cond:
        FAIL.append(label)


tmp = tempfile.mkdtemp(prefix="tc-e-")
out_dir = os.path.join(tmp, "out")
regpath = os.path.join(tmp, "registry.yaml")
registry.add_project("hscc", repo="/x/hscc", board="hscc", topic=2046, path=regpath)
registry.add_project("ecofire-app", repo="/x/ecofire", board="hscc", topic=2257, path=regpath)

# Build a minimal state.db mirroring the real schema's columns.
SESSION_COLS = (
    "id TEXT", "source TEXT", "chat_id TEXT", "thread_id TEXT",
    "title TEXT", "display_name TEXT", "started_at REAL", "ended_at REAL",
    "message_count INTEGER",
)
MESSAGE_COLS = (
    "id INTEGER PRIMARY KEY", "session_id TEXT", "role TEXT", "content TEXT",
    "tool_call_id TEXT", "tool_calls TEXT", "tool_name TEXT",
    "timestamp REAL", "reasoning TEXT",
)
db_path = os.path.join(tmp, "state.db")
conn = sqlite3.connect(db_path)
conn.execute("CREATE TABLE sessions (" + ", ".join(SESSION_COLS) + ")")
conn.execute("CREATE TABLE messages (" + ", ".join(MESSAGE_COLS) + ")")
conn.execute(
    "INSERT INTO sessions (id, source, thread_id, title, started_at, message_count) "
    "VALUES ('s_a', 'telegram', '2046', 'HSCC Chat', 1000.0, 2)")
conn.execute(
    "INSERT INTO sessions (id, source, thread_id, title, started_at, message_count) "
    "VALUES ('s_b', 'telegram', '8903', 'Orphan Thread', 1000.0, 1)")
conn.execute(
    "INSERT INTO messages (session_id, role, content, timestamp) "
    "VALUES ('s_a', 'user', 'operator hello', 1001.0)")
conn.execute(
    "INSERT INTO messages (session_id, role, content, timestamp) "
    "VALUES ('s_a', 'assistant', 'assistant reply', 1002.0)")
conn.execute(
    "INSERT INTO messages (session_id, role, content, timestamp) "
    "VALUES ('s_b', 'user', 'orphan words', 1301.0)")
conn.commit()
conn.close()

# ---- archive-sessions human (piped): no ANSI, content preserved ----
rc, out, err = run(["--registry", regpath, "archive-sessions",
                    "--out", out_dir, "--db", db_path])
_ansi = chr(27) in out
check("archive-sessions human rc=0 + plain",
      rc == 0 and not _ansi
      and "archived 2 session(s), 3 message(s)" in out
      and "  hscc" in out and "unmapped" in out,
      f"rc={rc} ansi={_ansi}")

# Build the canonical --json payload from the same data.
from flightdeck.core.archive import archive_sessions  # noqa: E402
result = archive_sessions(out_dir=out_dir, db_path=db_path, registry_path=regpath)
canonical = json.dumps({
    "out_dir": out_dir,
    "sessions": result.sessions,
    "messages": result.messages,
    "bytes_written": result.bytes_written,
    "files": result.files,
    "by_project": result.by_project,
    "by_thread": result.by_thread,
    "unmapped_threads": result.unmapped_threads,
    "index": result.index_path,
}, indent=2) + "\n"

# ---- archive-sessions --json: byte-identical to canonical + no ANSI ----
rc, out, err = run(["--json", "--registry", regpath, "archive-sessions",
                    "--out", out_dir, "--db", db_path])
_eq = out == canonical
_ansi = chr(27) in out
check("archive-sessions --json byte-identical + plain",
      rc == 0 and _eq and not _ansi,
      f"rc={rc} ansi={_ansi} eq={_eq}")

print()
if FAIL:
    print("RESULT: FAILURES ->", FAIL)
    sys.exit(1)
print("RESULT: ALL INVARIANT CHECKS PASSED")
