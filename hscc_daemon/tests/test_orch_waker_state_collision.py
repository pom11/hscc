"""t_fa9a2faf — the collision the whole W1-W3 suite walked past: ONE path,
TWO owners, and the deployed pairing nobody ever drove.

``DEFAULT_STATE_PATH`` (the waker's ``{last,pending,sent}`` scan state, the
stopgap's schema) and the daemon's stream-state file for ``STREAM_NAME``
(``state.STATE_DIR/<STREAM_NAME>.json``) resolve to the SAME file,
``~/.hscc/state/orch_waker.json``. ``waker_tick`` stamps the stream in its
``finally`` *after* the scan wrote its state, and ``state.write_state`` writes
the whole document — so every tick erased ``last`` and every board
re-first-sighted (tail-started) forever. Nothing ever aged into a nudge, the
audit JSONL stayed empty, ``status`` printed no boards, and both the tick stamp
and the doctor row read healthy. The built-in waker was inert while reporting
itself alive.

WHY THE SUITE MISSED IT: every W3 tick test injects ``write=StreamSpy().write``
(or a stubbed ``run_tick``) AND a separate ``state_path``, so the deployed
combination — the real ``state.write_state`` writing the default path — was
never exercised. These tests drive exactly that pairing: NO injected
``write``/``read``, NO injected ``state_path``, the real ``run_tick`` chain,
under conftest's redirected ``~/.hscc``.

The fix is a MERGE, not a rename (option B): the stamp lands on top of the
scan state instead of replacing it, so one document serves both readers and
the stopgap hand-over file stays the hand-over file. The reverse half was
already safe — ``load_state`` ignores unknown top-level keys and ``save_state``
writes back the dict it loaded, so the stamp's fields survive a scan write.
"""

import json
import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

T0 = 1_800_000_000


def make_tree(tmp_path):
    """A throwaway ~/.hermes: one board with a completed event + its orch.

    Returns ``(home_dir, boards_dir, db_path)``. The board DB is the shape
    ``scan_board`` reads (task_events joined to tasks); the event is inserted
    later by ``add_event`` so the first tick can first-sight an empty board.
    """
    home = tmp_path / "hermes"
    boards = home / "kanban" / "boards"
    (boards / "demo").mkdir(parents=True)
    (home / "profiles" / "demo-orch").mkdir(parents=True)
    db = boards / "demo" / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT,"
        " created_at INTEGER);"
        "INSERT INTO tasks VALUES ('t_demo1', 'Demo card');"
    )
    conn.close()
    return home, boards, str(db)


def add_event(db, kind="completed", run_id=7):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO task_events(task_id,run_id,kind,payload,created_at)"
        " VALUES (?,?,?,?,?)", ("t_demo1", run_id, kind, None, 0))
    conn.commit()
    conn.close()


class FakeRepl:
    """Stands in for the ``repl_inject`` parity module — the documented seam.

    ``make_inject`` still runs for real around it: profile resolution, the
    audit line, the ok/not-ok bookkeeping and the pending-clear rule are all
    exercised; only tmux is replaced.
    """

    def __init__(self):
        self.sent = []

    def inject(self, profile, msg, mode="auto"):
        self.sent.append((profile, msg, mode))


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    """The waker as the daemon actually runs it: zero injected seams.

    Only the three things a hermetic test MUST replace are replaced: the clock
    (determinism), the ``repl_inject`` module (no tmux), and the board/profile
    roots (never read the operator's live boards). The config, the state path
    and the audit path all stay at their DEFAULTS, and ``state.write_state`` /
    ``state.read_state`` are the real ones — that pairing is the defect.
    """
    home, boards, db = make_tree(tmp_path)
    monkeypatch.setattr(ow, "DEFAULT_BOARDS_DIR", str(boards))
    monkeypatch.setattr(ow, "DEFAULT_HERMES_HOME", str(home))
    repl = FakeRepl()
    monkeypatch.setattr(ow, "load_repl_inject", lambda: repl)
    clock = {"now": float(T0)}
    monkeypatch.setattr(ow, "_real_time", lambda: clock["now"])

    def tick_at(offset):
        clock["now"] = float(T0 + offset)
        assert ow.check_orch_waker() is True, "the loop's check-fn must report ok"
        return ow.waker_status(now=clock["now"])

    def deliver():
        """Drive the real cadence to the first send: first-sight, then a tick
        that batches the event, then one past debounce_s (90 s).

        The event must be SCANNED at least one tick before it can fire — a
        batch is only eligible once it has aged ``debounce_s`` — so two ticks
        after the event is not enough. That batching rule is exactly why the
        zero-nudge bug could hide behind "steady" forever.
        """
        add_event(db)
        tick_at(30)
        return tick_at(120)

    return {"db": db, "repl": repl, "tick_at": tick_at, "deliver": deliver,
            "home": home}


# ── the deployed pairing: default state path + real state.write_state ───────

def test_nudge_is_delivered_across_ticks_at_the_deployed_entry(deployed):
    """THE regression test. Three real ``check_orch_waker()`` calls on the
    DEFAULT state path with the real stream writer: a nudge must be delivered.

    Before the fix this returned zero nudges forever — the tick stamp erased
    ``last`` so the board re-tail-started on every pass and the batch never
    aged into a send.
    """
    db, repl, tick_at = deployed["db"], deployed["repl"], deployed["tick_at"]

    first = tick_at(0)                      # first sight: tail-start, no flood
    assert repl.sent == [], "first sight must not nudge a backlog"
    # The board appears in `status` at all only because the watermark survived
    # the stamp that follows the scan — pre-fix this printed {} forever.
    assert first["boards"] == {"demo": {"last_id": 0, "pending": 0}}

    add_event(db)                           # a worker finishes a card
    mid = tick_at(30)                       # batched; still inside debounce 90s
    assert repl.sent == [], "debounce must hold the batch for 90s"
    assert mid["boards"] == {"demo": {"last_id": 1, "pending": 1}}, (
        "the pending batch must survive the stamp — pre-fix it was erased "
        "with `last`, so nothing ever aged into a send")

    tick_at(120)                            # past debounce, past min_gap from 0
    assert len(repl.sent) == 1, (
        "a nudge MUST be delivered once the batch ages out — zero means the "
        "tick stamp is wiping the scan state again (t_fa9a2faf)")
    profile, msg, mode = repl.sent[0]
    assert profile == "demo-orch" and mode == "auto"
    assert msg == ("[orch-waker] board demo: t_demo1 done — Demo card "
                   "— review/merge/unblock as needed, then carry on.")


def test_watermarks_survive_the_stamp(deployed):
    """The other half of the evidence: the board watermark must move and stay.

    ``status``'s boards list is the operator's retirement gate (docs/ops), and
    it is non-empty only if ``last`` survived the previous tick's stamp.
    """
    tick_at, deliver = deployed["tick_at"], deployed["deliver"]
    tick_at(0)
    deliver()                               # -> batched at +30s, sent at +120s

    st = ow.load_state(None)                # the DEFAULT path, explicitly
    assert st["last"] == {"demo": 1}, "watermark must persist across a stamp"
    assert st["sent"].get("demo") == float(T0 + 120)
    assert st["pending"] == {}, "a delivered batch is cleared, not kept"

    status = ow.waker_status(now=float(T0 + 130))
    assert status["boards"] == {"demo": {"last_id": 1, "pending": 0}}
    assert status["stream"]["last_tick"] == float(T0 + 120), (
        "the stamp and the scan state must coexist in the ONE document")


def test_the_single_document_holds_both_schemas(deployed):
    """One file, both readers: the stamp fields and the stopgap keys coexist.

    Pins the MERGE decision (option B) over option A (rename): the operator's
    stopgap state file IS ``state/orch_waker.json``, and W1's takeover promise
    says the built-in continues from exactly that file. A rename would have
    dropped the pending batches mid-hand-over.
    """
    tick_at, deliver = deployed["tick_at"], deployed["deliver"]
    tick_at(0)
    deliver()                               # -> batched at +30s, sent at +120s

    path = os.path.expanduser(ow.DEFAULT_STATE_PATH)
    from hscc_daemon import state as dstate
    assert os.path.realpath(path) == os.path.realpath(
        os.path.join(dstate.STATE_DIR, ow.STREAM_NAME + ".json")), (
        "the collision is real and by design now: ONE document, merged")
    with open(path) as f:
        raw = json.load(f)
    assert {"last", "pending", "sent"} <= set(raw), "stopgap keys survive"
    assert {"last_tick", "ok", "stream", "timestamp"} <= set(raw), "stamp survives"
    assert raw["nudged"] == 1 and raw["ok"] is True


def test_audit_trail_lands_on_the_default_path(deployed):
    """The JSONL audit is the operator's evidence trail; it was never created
    at all while the waker was inert. Default path (redirected ``~/.hscc``).
    """
    tick_at, deliver = deployed["tick_at"], deployed["deliver"]
    tick_at(0)
    deliver()                               # -> batched at +30s, sent at +120s
    tail = ow.read_audit_tail(None, 5)
    assert [e["board"] for e in tail] == ["demo"]
    assert tail[0]["ok"] is True and tail[0]["profile"] == "demo-orch"


def test_stale_counters_are_not_resurrected_by_the_merge(tmp_path, monkeypatch):
    """A ``poll_gate``/``disabled`` stamp must not carry the PREVIOUS scan's
    ``nudged``/``decisions`` forward: the merge carries only {last,pending,
    sent}, so the stamp section is rewritten clean every tick.
    """
    home, boards, db = make_tree(tmp_path)
    monkeypatch.setattr(ow, "DEFAULT_BOARDS_DIR", str(boards))
    monkeypatch.setattr(ow, "DEFAULT_HERMES_HOME", str(home))
    repl = FakeRepl()
    monkeypatch.setattr(ow, "load_repl_inject", lambda: repl)
    clock = {"now": float(T0)}
    monkeypatch.setattr(ow, "_real_time", lambda: clock["now"])
    monkeypatch.setattr(ow, "stream_interval", lambda *a, **k: 120)  # poll gate

    clock["now"] = float(T0)
    assert ow.check_orch_waker() is True
    add_event(db)
    clock["now"] = float(T0 + 60)           # inside the 120s poll window
    assert ow.check_orch_waker() is True

    from hscc_daemon import state as dstate
    entry = dstate.read_state(ow.STREAM_NAME)
    assert entry is not None, "the skipped tick still stamped the stream"
    assert entry["skipped"] == "poll_gate"
    assert "nudged" not in entry and "decisions" not in entry, (
        "a skipped tick must not advertise the last real scan's counters")
    assert entry["last_tick"] == float(T0 + 60)
    assert entry["last"] == {"demo": 0}, "the scan watermark still survives a skip"


def test_stamp_stays_alone_when_the_paths_are_different(tmp_path, monkeypatch):
    """The guard: merge ONLY when the two paths really are the same file.

    A test (or a future operator) that points ``state_path`` elsewhere must
    not get the scan state duplicated into the stream file — the stream
    document stays exactly the stamp, as it was before this fix.
    """
    scan = tmp_path / "elsewhere" / "orch_waker.json"
    scan.parent.mkdir()
    scan.write_text(json.dumps({"last": {"b": 42}, "pending": {}, "sent": {}}))
    entry = ow.stamp_entry(str(scan), {"last_tick": T0, "ok": True})
    assert entry == {"last_tick": T0, "ok": True}, (
        "no collision => no merge => the stamp document is unchanged")
    assert ow.stamp_entry(None, {"ok": True})["ok"] is True
    assert ow._owned_state_keys(str(tmp_path / "missing.json")) == {}


def test_corrupt_document_does_not_break_the_stamp(tmp_path, monkeypatch):
    """A torn/hand-edited file must not stop the tick from stamping: the
    merge degrades to a bare stamp, and the stream stays non-stale.
    """
    monkeypatch.setattr(ow, "stream_interval", lambda *a, **k: 30)
    path = os.path.expanduser(ow.DEFAULT_STATE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")
    wrote = []
    entry = ow.waker_tick(now=float(T0), config_path=str(tmp_path / "cfg.json"),
                          write=lambda name, data: wrote.append((name, data)) or data,
                          read=lambda name: None)
    assert entry["ok"] is True
    assert wrote and wrote[0][0] == ow.STREAM_NAME
    assert wrote[0][1]["last_tick"] == float(T0)
