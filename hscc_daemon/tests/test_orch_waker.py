"""Tests for hscc_daemon/orch_waker.py — the pure W1 core (no delivery).

Offline only: temp sqlite boards with the real task_events column shape, a
tmp state path, an injected clock and stub callables. NEVER the operator's
live ~/.hermes boards or ~/.hscc state.

Debounce shape used throughout: a batch is created by the tick that SEES the
event (ts = that tick's now), and only a LATER tick whose now - oldest >=
DEBOUNCE_S makes the send decision.
"""

import json
import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000

# Mirrors hermes' kanban SCHEMA_SQL for the two tables the waker reads.
SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
"""


def make_board(path, tasks=(), events=()):
    """Create a board DB at `path` seeded with tasks + task_events rows.

    events are (task_id, run_id, kind, payload) tuples; created_at is NOW.
    """
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO tasks (id, title) VALUES (?, ?)", tasks)
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(t, r, k, p, NOW) for (t, r, k, p) in events])
    conn.commit()
    conn.close()
    return path


def add_events(path, events):
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(t, r, k, p, NOW) for (t, r, k, p) in events])
    conn.commit()
    conn.close()


def board_id(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "hscc-state" / "orch_waker.json")


class StubInject:
    """Delivery seam recording calls; `ok` flips the verdict."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok
        self.raise_exc = False

    def __call__(self, board, msg):
        self.calls.append((board, msg))
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.ok


# ---------------------------------------------------------------------------
# describe — stopgap-identical formatting
# ---------------------------------------------------------------------------

def _row(task="t_a", run_id=1, kind="completed", payload=None, title="Some title"):
    return (1, task, run_id, kind, payload, NOW, title)


def test_describe_labels_all_kinds():
    assert ow.describe(_row(kind="completed")) == "t_a done — Some title"
    assert ow.describe(_row(kind="gave_up")) == "t_a GAVE UP — Some title"
    assert ow.describe(_row(kind="crashed")) == "t_a CRASHED — Some title"
    assert ow.describe(_row(kind="timed_out")) == "t_a timed out — Some title"
    assert ow.describe(_row(kind="spawn_failed")) == "t_a spawn failed — Some title"
    assert ow.describe(_row(kind="review_requested")) == "t_a review requested — Some title"
    assert ow.describe(_row(kind="block_loop_detected")) == "t_a block loop — Some title"


def test_describe_truncates_title_to_70():
    line = ow.describe(_row(title="x" * 90))
    assert line == "t_a done — " + "x" * 70


def test_describe_blocked_worker_carries_reason_flattened_and_truncated():
    reason = "line one\nline two " + "r" * 160
    line = ow.describe(_row(kind="blocked", payload=json.dumps({"reason": reason})))
    flat = "line one line two " + "r" * 160
    assert line == f"t_a BLOCKED: {flat[:140]} — Some title"   # [:140] over the WHOLE
    assert "\n" not in line                                     # newline-flattened
    assert "r" * 141 not in line


def test_describe_blocked_bare_without_reason():
    assert ow.describe(_row(kind="blocked", payload=None)) == "t_a BLOCKED — Some title"
    assert ow.describe(_row(kind="blocked", payload="{not json")) == "t_a BLOCKED — Some title"
    assert ow.describe(
        _row(kind="blocked", payload=json.dumps({"note": "no reason key"}))) == \
        "t_a BLOCKED — Some title"


def test_describe_blocked_run_id_null_is_skipped():
    # orch/operator-raised block -> never a wake
    assert ow.describe(_row(kind="blocked", run_id=None)) is None


def test_describe_worker_raised_block_kept():
    assert ow.describe(_row(kind="blocked", run_id=7)) == "t_a BLOCKED — Some title"


# ---------------------------------------------------------------------------
# scan + kind filter + tail-start
# ---------------------------------------------------------------------------

def test_kind_filter_unknown_kinds_invisible(tmp_path, state_path):
    names = list(ow.KINDS) + ["reclaimed", "heartbeated", "some_future_kind"]
    db = make_board(tmp_path / "b" / "kanban.db",
                    tasks=[(f"t_{n}", n) for n in names])
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)   # first sight: tail
    add_events(db, [(f"t_{n}", 5, n, None) for n in names])
    ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path)     # batch
    dec = ow.tick(now=NOW + 200, boards={"b": db}, state_path=state_path)  # debounce ok
    assert len(dec) == 1
    lines = dec[0]["lines"]
    assert len(lines) == len(ow.KINDS)                    # every tracked kind...
    assert not any(l.startswith("t_reclaimed ") for l in lines)
    assert not any(l.startswith("t_heartbeated ") for l in lines)
    assert not any(l.startswith("t_some_future_kind ") for l in lines)


def test_first_sight_tails_and_never_floods_backlog(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db",
                    tasks=[(f"t_{i}", f"title {i}") for i in range(5)],
                    events=[(f"t_{i}", 1, "completed", None) for i in range(5)])
    dec = ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    assert dec == []
    st = ow.load_state(state_path)
    assert st["last"]["b"] == board_id(db)                # tail watermark
    assert not st["pending"].get("b")                     # NO backlog flood
    add_events(db, [("t_new", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)     # batch
    dec = ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path)
    assert dec[0]["lines"] == ["t_new done — "]           # only the new event


def test_watermark_advances_only_with_seen_events(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_x", "X")])
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    before = ow.load_state(state_path)["last"]["b"]
    add_events(db, [("t_x", 1, "completed", None)])
    ow.tick(now=NOW + 5, boards={"b": db}, state_path=state_path)
    assert ow.load_state(state_path)["last"]["b"] == before + 1


# ---------------------------------------------------------------------------
# debounce + min-gap + batching
# ---------------------------------------------------------------------------

def test_debounce_boundary_89_hold_90_send(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)   # tail at 0 events
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)   # batched at +10
    assert len(ow.load_state(state_path)["pending"]["b"]) == 1
    assert ow.tick(now=NOW + 10 + 89, boards={"b": db}, state_path=state_path) == []
    dec = ow.tick(now=NOW + 10 + 90, boards={"b": db}, state_path=state_path)
    assert len(dec) == 1                                   # 90s old -> send
    assert dec[0]["board"] == "b"


def test_min_gap_suppresses_while_pending_keeps_batching(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db",
                    tasks=[("t_1", "One"), ("t_2", "Two"), ("t_3", "Three")])
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path)     # batched +100
    stub = StubInject()
    dec = ow.tick(now=NOW + 200, boards={"b": db}, state_path=state_path, inject=stub)
    assert len(dec) == 1                                   # 100s old, no prior send
    st = ow.load_state(state_path)
    assert st["sent"]["b"] == NOW + 200 and not st["pending"].get("b")
    add_events(db, [("t_2", 1, "completed", None), ("t_3", 1, "completed", None)])
    ow.tick(now=NOW + 210, boards={"b": db}, state_path=state_path, inject=stub)
    # debounce WOULD allow at +300 (batch 90s old); min-gap (sent at +200) does not:
    assert ow.tick(now=NOW + 300, boards={"b": db}, state_path=state_path,
                   inject=stub) == []
    assert stub.calls == [("b", dec[0]["msg"])]            # nothing sent meanwhile
    st = ow.load_state(state_path)
    assert len(st["pending"]["b"]) == 2                    # both kept, nothing lost
    dec = ow.tick(now=NOW + 500, boards={"b": db}, state_path=state_path, inject=stub)
    assert len(dec) == 1                                   # ONE nudge for both
    assert dec[0]["lines"] == ["t_2 done — Two", "t_3 done — Three"]


def test_dedup_six_line_cap_and_plus_more(tmp_path, state_path):
    tasks = [(f"t_{i}", f"Title {i}") for i in range(1, 10)]
    db = make_board(tmp_path / "b" / "kanban.db", tasks=tasks)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [(f"t_{i}", 1, "completed", None) for i in range(1, 10)]
               + [("t_1", 1, "completed", None)])          # duplicate line
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    dec = ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path)
    assert dec[0]["msg"] == ("[orch-waker] board b: "
                             + " · ".join(f"t_{i} done — Title {i}" for i in range(1, 7))
                             + " · +3 more — review/merge/unblock as needed, "
                               "then carry on.")


# ---------------------------------------------------------------------------
# state file: stopgap schema, takeover, injection seams
# ---------------------------------------------------------------------------

def test_state_file_is_exactly_the_stopgap_schema(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject()
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "crashed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path, inject=stub)
    with open(state_path) as f:
        raw = json.load(f)
    assert set(raw) == {"last", "pending", "sent"}         # stopgap keys, nothing else
    assert raw["last"]["b"] == board_id(db)
    assert raw["sent"]["b"] == NOW + 100
    assert "b" not in raw["pending"]                       # cleared after send
    assert stub.calls == [("b", "[orch-waker] board b: t_1 CRASHED — One "
                                 "— review/merge/unblock as needed, then carry on.")]


def test_takeover_continues_from_hand_written_stopgap_state(tmp_path):
    """The seamless-retirement test: adopt the operator's live stopgap file."""
    state_file = tmp_path / "orch_waker.json"
    state_file.write_text(json.dumps({
        "last": {"b": 2},                                  # mid-board watermark
        "pending": {"b": [[NOW - 10, "t_old done — Old pending line"]]},
        "sent": {"b": NOW - 1000},
    }))
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_9", "Fresh")],
                    events=[("t_a", 1, "completed", None),
                            ("t_b", 1, "blocked", json.dumps({"reason": "wait"})),
                            ("t_9", 1, "review_requested", None)])
    # ids 1-2 are already consumed (id <= last=2); id 3 is new.
    dec = ow.tick(now=NOW + 95, boards={"b": str(db)}, state_path=str(state_file),
                  inject=StubInject(ok=False))
    assert len(dec) == 1
    assert dec[0]["ok"] is False                           # failed inject
    assert dec[0]["lines"] == ["t_old done — Old pending line",    # inherited batch...
                               "t_9 review requested — Fresh"]     # ...plus the new one
    st = json.loads(state_file.read_text())
    assert st["last"]["b"] == 3                            # watermark advanced
    assert st["sent"]["b"] == NOW - 1000                   # failure: sent NOT stamped
    assert [p[1] for p in st["pending"]["b"]] == dec[0]["lines"]
    assert all(isinstance(p, list) and len(p) == 2 for p in st["pending"]["b"])


def test_failed_inject_keeps_pending_for_retry(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject(ok=False)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "gave_up", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    dec = ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path, inject=stub)
    assert dec[0]["ok"] is False
    st = ow.load_state(state_path)
    assert len(st["pending"]["b"]) == 1 and "b" not in st["sent"]
    stub.ok = True                                         # retry succeeds next tick
    dec = ow.tick(now=NOW + 250, boards={"b": db}, state_path=state_path, inject=stub)
    assert dec[0]["ok"] is True
    st = ow.load_state(state_path)
    assert not st["pending"].get("b") and st["sent"]["b"] == NOW + 250
    assert len(stub.calls) == 2                            # retried the SAME batch


def test_inject_raising_is_a_failed_delivery_not_a_crash(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject()
    stub.raise_exc = True
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "timed_out", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    dec = ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path, inject=stub)
    assert dec[0]["ok"] is False
    assert len(ow.load_state(state_path)["pending"]["b"]) == 1


def test_inject_none_reports_decision_and_keeps_pending(tmp_path, state_path):
    """W1 default (delivery is W2's): the core decides, nothing is cleared."""
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    dec = ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path)
    assert len(dec) == 1 and dec[0]["ok"] is None
    assert len(ow.load_state(state_path)["pending"]["b"]) == 1


def test_corrupt_state_file_start_fresh_and_tails(tmp_path, state_path):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as f:
        f.write("{ not json")
    db = make_board(tmp_path / "b" / "kanban.db",
                    tasks=[("t_1", "One")], events=[("t_1", 1, "completed", None)])
    dec = ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    assert dec == []                                       # fresh -> tail start
    assert ow.load_state(state_path)["last"]["b"] == board_id(db)


def test_tick_mutates_no_board_and_leaves_no_tmp_litter(tmp_path, state_path):
    bdir = tmp_path / "boards" / "b"
    db = make_board(bdir / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW, boards={"b": str(db)}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": str(db)}, state_path=state_path)
    ow.tick(now=NOW + 100, boards={"b": str(db)}, state_path=state_path,
            inject=StubInject())
    assert os.listdir(tmp_path / "hscc-state") == ["orch_waker.json"]   # no .tmp litter
    assert sorted(os.listdir(bdir)) == ["kanban.db"]       # boards dir untouched
    conn = sqlite3.connect(str(db))                        # still a healthy rw db
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


# ---------------------------------------------------------------------------
# board-level skips: unreadable / paused / no orch
# ---------------------------------------------------------------------------

def test_unreadable_board_skipped_not_fatal_and_warned_once(tmp_path, state_path, caplog):
    good = make_board(tmp_path / "boards" / "good" / "kanban.db", tasks=[("t_1", "One")])
    bad = tmp_path / "boards" / "bad" / "kanban.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("this is not a sqlite database")
    warned = set()
    boards = {"bad": str(bad), "good": good}
    with caplog.at_level("WARNING"):
        assert ow.tick(now=NOW, boards=boards, state_path=state_path,
                       warned=warned) == []
        assert ow.tick(now=NOW + 30, boards=boards, state_path=state_path,
                       warned=warned) == []                 # second tick, same run
    st = ow.load_state(state_path)
    assert "good" in st["last"] and "bad" not in st["last"]  # healthy board survives
    warns = [r for r in caplog.records if "unreadable" in r.getMessage()]
    assert len(warns) == 1                                  # once per board per run


def test_paused_board_drops_pending_but_watermark_advances(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "blocked", json.dumps({"reason": "busy"}))])
    dec = ow.tick(now=NOW + 100, boards={"b": db}, paused={"b"},
                  state_path=state_path, inject=StubInject())
    assert dec == []
    st = ow.load_state(state_path)
    assert st["last"]["b"] == board_id(db)                  # watermark still advanced
    assert not st["pending"].get("b")                       # batch dropped
    assert "b" not in st["sent"]                            # nothing was delivered


def test_no_orch_profile_suppresses_delivery(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject()
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    dec = ow.tick(now=NOW + 100, boards={"b": db}, has_orch=lambda b: None,
                  state_path=state_path, inject=stub)
    assert dec == [] and stub.calls == []
    assert not ow.load_state(state_path)["pending"].get("b")  # batch dropped, not queued
    # orch appears later: new events wake normally (the dropped batch is gone)
    add_events(db, [("t_1", 2, "completed", None)])
    ow.tick(now=NOW + 110, boards={"b": db},
            has_orch=lambda b: f"{b}-orch", state_path=state_path, inject=stub)
    dec = ow.tick(now=NOW + 210, boards={"b": db},
                  has_orch=lambda b: f"{b}-orch", state_path=state_path, inject=stub)
    assert len(dec) == 1 and len(stub.calls) == 1


# ---------------------------------------------------------------------------
# enumeration (legacy default board + named boards)
# ---------------------------------------------------------------------------

def test_enumerate_includes_legacy_default_and_named_boards(tmp_path):
    home = tmp_path / "hermes"
    (home / "kanban" / "boards" / "beta").mkdir(parents=True)
    (home / "kanban" / "boards" / "beta" / "kanban.db").write_text("")
    (home / "kanban" / "boards" / "notaboard").mkdir()      # no kanban.db -> skipped
    (home / "kanban" / "boards" / "default").mkdir()
    (home / "kanban" / "boards" / "default" / "kanban.db").write_text("")  # never wins
    # the legacy default DB holds a real board (created here, not write_text)
    legacy = make_board(home / "kanban.db", tasks=[("t_1", "One")])
    boards = ow.enumerate_boards(boards_dir=str(home / "kanban" / "boards"),
                                 hermes_home=str(home))
    assert boards["beta"] == str(home / "kanban" / "boards" / "beta" / "kanban.db")
    assert boards["default"] == str(home / "kanban.db")     # legacy path wins
    assert "notaboard" not in boards
    # default keeps its OWN last-id key and tail-starts like any board:
    ow.tick(now=NOW, boards={"default": legacy}, state_path=str(tmp_path / "st.json"))
    assert ow.load_state(str(tmp_path / "st.json"))["last"]["default"] == 0


def test_boards_list_restricts_scan(tmp_path, state_path):
    a = make_board(tmp_path / "a" / "kanban.db", tasks=[("t_a", "A")])
    b = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_b", "B")])
    ow.tick(now=NOW, boards=["a"], state_path=state_path,
            boards_dir=str(tmp_path / "unused"))            # slugs, no matching dir
    assert ow.load_state(state_path)["last"] == {}
    ow.tick(now=NOW, boards={"a": a, "b": b}, state_path=state_path)
    assert set(ow.load_state(state_path)["last"]) == {"a", "b"}
