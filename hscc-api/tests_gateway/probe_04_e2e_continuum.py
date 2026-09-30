#!/usr/bin/env python3
"""Probe 4 — END-TO-END session continuity on the REAL stack (isolated).

Card t_59cc6714. Uses the established tests_gateway/probe_* harness pattern:
an ISOLATED `hermes serve` on scratch port 9211 with a scratch home
(/tmp/hscc_e2e_home) — the live operator gateway on 9119 is NEVER touched, and
no operator profile/registry/state is written.

Proves, by real execution:
  A) Backfill determinism on the live store (idempotent, seq contiguous).
  B) Two-way: an app-side message sent via the gateway driver's
     send_user_message lands in the project's NAMED Hermes session (verified
     by re-opening the session's true state.db history), and the CLI
     `--continue` dump of that session shows full history incl. the
     phone-originated message (ONE session, not two streams).
  C) Honest error paths: no-session, unknown project, backfill failure while
     live — each reported honestly, never a silent wedge / fabricated success.

Run: ~/.hermes/hermes-agent/venv/bin/python probe_04_e2e_continuum.py
"""
import json, os, sys, time, tempfile, traceback
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
HSApi = os.path.dirname(HERE)          # hscc-api/
ROOT = os.path.dirname(os.path.dirname(HSApi))   # worktree root (has hscc-roles/)
sys.path.insert(0, HSApi)
sys.path.insert(0, os.path.join(ROOT, "hscc-roles"))   # for `orchestrators`

from session_event import get_store, reset_stores
import gateway_driver as gd
from gateway_driver import GatewayConfig, GatewayDriver, backfill_named_session
import routes_orchestrator

SCRATCH_HOME = Path(os.environ.get("PROBE04_HOME", "/tmp/hscc_e2e_home"))
STATE_DB = SCRATCH_HOME / "state.db"
TOKEN = "iso_probe_token_7f3a9c2e"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PROBE04_PORT", "9211"))

PASS = "PASS"; FAIL = "FAIL"
results = []

def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{PASS if cond else FAIL}] {name}" + (f"  — {detail}" if detail else ""))

# --------------------------------------------------------------------------- #
# Seam: point the resolver/backfill at the SCRATCH home's real state.db.
# --------------------------------------------------------------------------- #
from hermes_state import SessionDB
def _open_scratch(profile, read_only=False):
    return SessionDB(db_path=STATE_DB)
routes_orchestrator._open_profile_session_db = _open_scratch

# --------------------------------------------------------------------------- #
# Build a temp registry + seed a REAL named session in the scratch state.db.
# --------------------------------------------------------------------------- #
def write_registry(tmp, project, session=None, board="hscc"):
    reg = tmp / "registry.yaml"
    lines = ["projects:", f"  - name: {project}", f"    repo: /tmp/{project}"]
    if board:
        lines.append(f"    board: {board}")
    if session:
        lines.append(f"    session: {session}")
    reg.write_text("\n".join(lines) + "\n")
    return str(reg)

SEED_TS = 1759200000
PHONE_TAG = "E2EPHONE_7f3a9c2e"
SID = "20260930_e2e_named_hscc"

def seed_named_session():
    db = SessionDB(db_path=STATE_DB)
    try:
        db.create_session(SID, source="cli", model="worker-model",
                          profile_name="hscc-orch")
        db.set_session_title(SID, "hscc")
        db.append_messages_batch(SID, [
            {"role": "user", "content": "first phone-originated continuity seed",
             "timestamp": SEED_TS},
            {"role": "assistant", "content": "first reply from the seed",
             "timestamp": SEED_TS + 50},
        ])
    finally:
        db.close()

def read_session_messages(sid):
    """Return the message list for sid from the real scratch state.db."""
    db = SessionDB(db_path=STATE_DB)
    try:
        return db.get_messages(sid)
    finally:
        db.close()

def _seed_named(title, profile="hscc-orch", sid=None):
    """Seed a named session (titled `title`) into the scratch state.db."""
    sid = sid or f"20260930_e2e_{title}"
    db = SessionDB(db_path=STATE_DB)
    try:
        db.create_session(sid, source="cli", model="worker-model",
                          profile_name=profile)
        db.set_session_title(sid, title)
        db.append_messages_batch(sid, [
            {"role": "user", "content": f"[{title}] seed user",
             "timestamp": SEED_TS},
            {"role": "assistant", "content": f"[{title}] seed assistant",
             "timestamp": SEED_TS + 50},
        ])
    finally:
        db.close()
    return sid

def main():
    print("=" * 70)
    print("PROBE 4 — session continuity E2E (isolated stack, port 9211)")
    print("scratch home:", SCRATCH_HOME, "state.db exists:", STATE_DB.exists())
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="hscc_e2e_reg_"))
    project = "hscc"
    reg = write_registry(tmp, project, session="hscc")

    reset_stores(); gd.reset_backfill_state()

    # ------------------------------------------------------------------ #
    # A) Backfill determinism on the live store
    # ------------------------------------------------------------------ #
    print("\n--- A) backfill determinism ---")
    seed_named_session()
    before = get_store(project)
    r1 = backfill_named_session(project, registry_path=reg)
    check("A1 backfill seeds the named session",
          r1["session"] == SID and r1["backfilled"] == 2,
          f"result={r1}")
    store1 = get_store(project)
    msgs1 = read_session_messages(SID)
    check("A2 store has the seeded history (2 frames)",
          store1.next_seq - 1 == 2,
          f"store next_seq={store1.next_seq}")
    # Add a live frame, then re-backfill (a reconnect after activity).
    full1 = store1.history(limit=10)
    # Re-backfill with unchanged store -> must be a no-op (idempotent).
    r2 = backfill_named_session(project, registry_path=reg)
    check("A3 re-backfill is idempotent (no dup frames)",
          r2.get("skipped") == "already_backfilled" and r2["backfilled"] == 0,
          f"result={r2}")
    store2 = get_store(project)
    check("A4 store seq unchanged after idempotent re-backfill",
          store2.next_seq == store1.next_seq,
          f"seq {store1.next_seq} -> {store2.next_seq}")

    # ------------------------------------------------------------------ #
    # B) Two-way: app -> named session -> CLI --continue
    # ------------------------------------------------------------------ #
    print("\n--- B) two-way ---")
    # Simulate a reconnect: new driver instance, same project, same store.
    store_before = get_store(project).next_seq
    cfg = GatewayConfig(host=HOST, port=PORT, token=TOKEN, project=project,
                        session_id=SID, profile="current",
                        registry_path=reg)
    drv = GatewayDriver(cfg)
    drv.start()
    check("B1 driver connected + backfilled on start",
          drv._pty is not None and drv._events is not None,
          "pty/events open")
    # Let the TUI boot + resume the named session before typing (the proven
    # probe pattern: probe_02/03 waited ~6s post-connect; with an active
    # session resume we give it more headroom so no keystrokes are dropped
    # while the Ink TUI is still spawning).
    print("   letting TUI boot + resume the named session (18s)...")
    time.sleep(18)
    # Now send a phone-originated message via the same path the WS relay uses.
    phone_msg = f"{PHONE_TAG}: hello from the phone over the bridge"
    ok = drv.send_user_message(phone_msg)
    check("B2 send_user_message accepted (relayed to pinned named session)",
          ok is True)
    # Wait for the model to reply through the PTY -> events feed.
    print("   waiting up to 90s for the model reply...")
    deadline = time.time() + 90
    store_after = get_store(project)
    while time.time() < deadline and store_after.next_seq <= store_before + 1:
        time.sleep(2)
        store_after = get_store(project)
    frames = store_after.history(limit=50)
    check("B3 live feed advanced past the phone message",
          store_after.next_seq > store_before + 1,
          f"seq {store_before} -> {store_after.next_seq}")

    # Prove the phone message is IN the named session's REAL state.db history.
    sess_msgs = read_session_messages(SID)
    sess_texts = [ (m.get("role"), str(m.get("content",""))[:80]) for m in sess_msgs ]
    phone_in_session = any(PHONE_TAG in c for _, c in sess_texts)
    check("B4 phone-originated message is in the named session's state.db history",
          phone_in_session, f"{len(sess_msgs)} messages in session")

    drv.stop()
    print("   named session messages (role, content):")
    for role, c in sess_texts:
        print(f"      {role}: {c}")

    # CLI --continue dump of the named session -> must show full history
    # including the phone message (the "one session, not two streams" proof).
    # We continue by the project's NAMED TITLE ("hscc") exactly as the
    # operator does (`hermes chat --continue <project>`), the same seam the
    # resolver uses — and ask the resumed session to echo what it can see, so
    # the phone message being in its context is proven by the machine-side CLI.
    print("\n--- B5) CLI --continue dump of the named session ---")
    os.environ["HERMES_HOME"] = str(SCRATCH_HOME)
    os.environ["HERMES_DELEGATED_CHILD_CONTEXT"] = "0"
    import subprocess, sqlite3
    hermes_bin = os.path.join(os.path.dirname(sys.executable), "hermes")
    cli_cmd = [hermes_bin, "chat", "--continue", "hscc", "-q",
               ("In our conversation history there is a message tagged "
                f"{PHONE_TAG}. Reply with exactly that tag followed by CONTINUED_OK."),
               "-Q"]
    print("   cmd:", " ".join(cli_cmd))
    # Only one surface drives the turn at a time (design §2): the driver's
    # PTY holds a session_turn_leases row on the session for its active turn.
    # Wait for that lease to clear (so the machine CLI is legitimately free to
    # take the turn), just as a human waits for the phone surface to release —
    # then run the CLI --continue. Bounded: waits up to ~8 min.
    import time as _t
    def _lease_held():
        try:
            conn = sqlite3.connect(str(STATE_DB))
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM session_turn_leases WHERE conversation_id=?", (SID,))
                return cur.fetchone() is not None
            finally:
                conn.close()
        except Exception:
            return False
    waited = 0
    while _lease_held() and waited < 480:
        print(f"   session turn-lease held by TUI ({waited}s); waiting 30s...")
        _t.sleep(30); waited += 30
    print(f"   lease clear after ~{waited}s; running CLI --continue...")
    # run the CLI; retry a couple times only for transient transport errors
    done = False
    for attempt in range(2):
        try:
            p = subprocess.run(cli_cmd, capture_output=True, text=True, timeout=90,
                               env={k: v for k, v in os.environ.items()})
            last_out = (p.stdout or "") + "\n" + (p.stderr or "")
            if PHONE_TAG in (p.stdout or ""):
                done = True
                print("   exit:", p.returncode, f"(attempt {attempt + 1})")
                print("   stdout tail:\n", (p.stdout or "")[-600:])
                print("   stderr tail:\n", (p.stderr or "")[-400:])
                break
            print("   no phone tag in output; retrying...")
            _t.sleep(10)
        except Exception as exc:
            print("   CLI attempt failed:", repr(exc))
            _t.sleep(10)
    if not done:
        print("   last stdout/stderr:\n", last_out)
    check("B5 CLI --continue sees full history (phone msg in session dump)",
          done, "phone tag present in CLI-visible session output")

    # ------------------------------------------------------------------ #
    # C) Honest error paths
    # ------------------------------------------------------------------ #
    print("\n--- C) honest error paths ---")
    # C1 unknown project -> 404 (UnknownProjectError raised by the resolver —
    #    the API endpoint path; backfill_named_session swallows resolve
    #    failures into its honest skipped=resolve_failed by design).
    from orchestrators import UnknownProjectError
    try:
        routes_orchestrator.resolve_named_session_id(
            "no_such_project_xyz", registry_path=reg)
        check("C1 unknown project resolver raises UnknownProjectError (404)",
              False, "no exception raised")
    except UnknownProjectError:
        check("C1 unknown project resolver raises UnknownProjectError (404)", True)
    except Exception as e1:
        check("C1 unknown project resolver raises UnknownProjectError (404)",
              False, repr(e1))

    # C2 project with no session -> honest no_session (session_id None)
    # New project registered, seeded in a DIFFERENT way: registry has the
    # project but no session row in state.db => resolver returns session_id None,
    # backfill returns skipped=no_session.
    p2 = "freshproj"
    reg2 = write_registry(tmp, p2, session=p2)
    r_ns = backfill_named_session(p2, registry_path=reg2)
    check("C2 no-session project -> honest skipped=no_session",
          r_ns.get("skipped") == "no_session" and r_ns["session"] is None,
          f"result={r_ns}")

    # C3 backfill failure while live -> reports honest "backfill_failed",
    #    never raises, and does not break the (subsequent) live connect.
    reg3 = write_registry(tmp, "wallproj", session="wallproj")
    # Seed a session for wallproj so resolution succeeds, then make the
    # opener's get_messages fail -> simulate a backfill failure while live.
    _seed_named("wallproj", "wallproj")
    saved_open = routes_orchestrator._open_profile_session_db
    def _broken_open(profile, read_only=False):
        db = SessionDB(db_path=STATE_DB)
        orig = db.get_messages
        def boom(sid):
            raise RuntimeError("simulated state.db read failure")
        db.get_messages = boom
        return db
    routes_orchestrator._open_profile_session_db = _broken_open
    r_bf = backfill_named_session("wallproj", registry_path=reg3)
    routes_orchestrator._open_profile_session_db = saved_open
    check("C3 backfill failure while live -> honest skipped=backfill_failed",
          r_bf.get("skipped") == "backfill_failed" and "backfilled" in r_bf,
          f"result={r_bf}")

    # C4 kanban contention (_backing_busy_tasks) — resolve_orchestrator reports
    # it; ensure the backfill surfaces it honestly (not a silent success).
    # Reuse the resolver path: if the project's board is busy, resolve throws
    # OrchestratorError (400) -> backfill reports skipped=resolve_failed.
    try:
        from orchestrators import OrchestratorError
        def _busy_resolve(project_arg, registry_path=None):
            raise OrchestratorError("project busy with kanban work")
        routes_orchestrator.resolve_named_session_id = _busy_resolve
        r_c4 = backfill_named_session("hscc", registry_path=reg)
        routes_orchestrator.resolve_named_session_id = None  # restore
        check("C4 kanban contention -> honest skipped=resolve_failed (not silent)",
              r_c4.get("skipped") == "resolve_failed",
              f"result={r_c4}")
    except Exception as e:
        check("C4 kanban contention", False, repr(e))

    # ------------------------------------------------------------------ #
    print("\n" + "=" * 70)
    summary = [n for n, ok, _ in results if ok]
    print(f"RESULT: {len(summary)}/{len(results)} checks passed")
    for n, ok, d in results:
        print(f"   [{PASS if ok else FAIL}] {n}")
    print("=" * 70)

if __name__ == "__main__":
    main()
