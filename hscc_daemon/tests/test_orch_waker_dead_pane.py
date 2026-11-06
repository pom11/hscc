"""t_f1122b7a — the dead-pane class: an orch profile that EXISTS but has no
tmux REPL pane made the waker hammer the same failing delivery every 30 s
forever.

Measured on the live daemon (2026-11-03): board ``pontaje`` has a
``pontaje-orch`` profile directory but no REPL pane, so ``resolve_orch`` — which
only checked the profile dir — approved delivery on EVERY tick. 125/125 audit
lines in ``~/.hscc/state/orch_waker.jsonl`` were byte-identical
``failed: no tmux pane running a Hermes REPL for profile 'pontaje-orch'``,
``pending`` never drained, and the "evidence trail" grew one line per tick.

The two fixes this suite pins:

  * FIX 1 — ``orch_map`` (config, hot-reloaded): ``{board: profile}`` consulted
    by ``resolve_orch`` BEFORE the ``<board>-orch`` convention. A map entry
    naming a profile that does not exist on disk falls back to the convention —
    the operator's typo must not silence a board the convention could reach.
  * FIX 2 — dead-pane backoff: after ``fail_threshold`` (3) CONSECUTIVE failed
    attempts to one board, the board is skipped for ``backoff_s`` (900 s): no
    inject attempt, therefore NO audit line (the audit is evidence, not a spool),
    one LOG line on entering the window, pending keeps batching. A success or an
    ``orch_map`` edit resets the counters; window expiry retires them so the
    board gets a fresh set of attempts.

Offline like the rest of the suite: tmp boards/profiles/audit, injected clock,
stub repl — NEVER tmux, never the operator's live ~/.hscc or ~/.hermes.
"""

import json
import os
import sqlite3

import pytest

from hscc_daemon import orch_waker as ow

NOW = 1_800_000_000

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
"""


@pytest.fixture
def state_path(tmp_path):
    """Same shape as the W1 suite's: a tmp waker state file (not the default)."""
    return str(tmp_path / "hscc-state" / "orch_waker.json")


def make_board(path, tasks=(), events=()):
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


class StubInject:
    """Core-level delivery seam; ``ok`` and the failure bookkeeping are ours."""

    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def __call__(self, board, msg):
        self.calls.append((board, msg))
        return self.ok


class FakeRepl:
    """Stands in for the repl_inject parity module (raises => failed attempt)."""

    def __init__(self, raises=None):
        self.calls = []                  # (profile, text, mode)
        self.raises = raises

    def inject(self, profile, text, mode="auto"):
        self.calls.append((profile, text, mode))
        if self.raises is not None:
            raise self.raises


class InjectErrorLike(Exception):
    """Carries `.kind` like repl_inject.InjectError (no import coupling)."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def make_env(tmp_path, profiles=(), orch_map=None):
    """A hermetic W2 environment: profiles dir, paused file, audit path."""
    pdir = tmp_path / "profiles"
    for p in profiles:
        (pdir / p).mkdir(parents=True, exist_ok=True)
    paused_file = tmp_path / "kanban_paused.json"
    paused_file.write_text(json.dumps({}))
    return {"profiles_dir": str(pdir), "paused_path": str(paused_file),
            "audit_path": str(tmp_path / "orch_waker.jsonl"),
            "orch_map": orch_map or {}}


def audit_lines(path):
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def fail_n(state_path, board):
    st = ow.load_state(state_path)
    return (ow.fail_state(st).get(board) or {}).get("n", 0)


# ── FIX 1: resolution order (map hit / map-to-missing falls back) ───────────

def test_resolve_orch_prefers_the_configured_map(tmp_path):
    (tmp_path / "profiles" / "b-orch").mkdir(parents=True)
    (tmp_path / "profiles" / "custom-orch").mkdir(parents=True)
    # The map wins over the convention when BOTH exist — that is the override:
    assert ow.resolve_orch("b", profiles_dir=str(tmp_path / "profiles"),
                           orch_map={"b": "custom-orch"}) == "custom-orch"
    # ...and the convention still answers when the map does not mention the board
    assert ow.resolve_orch("b", profiles_dir=str(tmp_path / "profiles"),
                           orch_map={"other": "custom-orch"}) == "b-orch"


def test_resolve_orch_map_to_missing_profile_falls_back(tmp_path):
    (tmp_path / "profiles" / "b-orch").mkdir(parents=True)
    # The mapped profile does NOT exist on disk: fall back to <board>-orch —
    # a typo in a map value must not be able to silence a reachable board.
    assert ow.resolve_orch("b", profiles_dir=str(tmp_path / "profiles"),
                           orch_map={"b": "ghost-orch"}) == "b-orch"
    # ...and when NEITHER exists there is no target at all (never nudged,
    # never audited — the pre-existing rule, unchanged by the map):
    assert ow.resolve_orch("z", profiles_dir=str(tmp_path / "profiles"),
                           orch_map={"z": "ghost-orch"}) is None


def test_resolve_orch_map_only_profile_works(tmp_path):
    (tmp_path / "profiles" / "ecofire-pontaje-orch").mkdir(parents=True)
    # The operator's real case: no `pontaje-orch` dir, the REPL runs under a
    # differently named profile the map points at:
    assert ow.resolve_orch("pontaje", profiles_dir=str(tmp_path / "profiles"),
                           orch_map={"pontaje": "ecofire-pontaje-orch"}) \
        == "ecofire-pontaje-orch"
    # Junk map shapes are never trusted (no crash, no route):
    for junk in (None, "b-orch", [], {"b": 5}, {"b": "  "}, {5: "x-orch"}):
        assert ow.mapped_orch("b", junk,
                              profiles_dir=str(tmp_path / "profiles")) is None


def _arm_failure(db, state_path, env, repl, until, orch_map=None):
    """Tail-start, one event, then one decision tick with the map wired."""
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    kw = dict(env)
    if orch_map is not None:
        kw["orch_map"] = orch_map
    return ow.run_tick(now=until, boards={"b": db}, state_path=state_path,
                       repl=repl, **kw)


def test_map_hit_routes_the_real_delivery_and_the_audit(tmp_path, state_path):
    """FIX 1 end to end through run_tick + make_inject: the nudge lands on the
    MAPPED profile (no <board>-orch exists at all), and so does the audit line."""
    env = make_env(tmp_path, profiles=("custom-orch",),
                   orch_map={"b": "custom-orch"})
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    repl = FakeRepl()
    dec = _arm_failure(db, state_path, env, repl, NOW + 100,
                       orch_map=env["orch_map"])
    assert dec[0]["ok"] is True
    assert repl.calls == [("custom-orch", dec[0]["msg"], "auto")]
    line, = audit_lines(env["audit_path"])
    assert line["profile"] == "custom-orch" and line["ok"] is True


def test_map_to_missing_profile_delivers_via_convention(tmp_path, state_path):
    env = make_env(tmp_path, profiles=("b-orch",), orch_map={"b": "ghost-orch"})
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    repl = FakeRepl()
    dec = _arm_failure(db, state_path, env, repl, NOW + 100,
                       orch_map=env["orch_map"])
    assert dec[0]["ok"] is True
    assert repl.calls == [("b-orch", dec[0]["msg"], "auto")]   # fell back, not dark


# ── FIX 2: the backoff (arms at N, silent inside, resumes after K) ──────────

def test_backoff_arms_at_exactly_n_then_goes_silent(tmp_path, state_path,
                                                   caplog):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject(ok=False)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)

    for i, t in enumerate((100, 130, 160), start=1):           # attempts 1..3
        dec = ow.tick(now=NOW + t, boards={"b": db}, state_path=state_path,
                      inject=stub)
        assert len(dec) == 1 and dec[0]["ok"] is False          # normal retry
        assert len(stub.calls) == i
        assert fail_n(state_path, "b") == i

    # The 4th tick sits inside the armed window: NO decision, NO inject call,
    # and the counters say the window is armed with the right board.
    report = {}
    dec = ow.tick(now=NOW + 190, boards={"b": db}, state_path=state_path,
                  inject=stub, report=report)
    assert dec == [] and len(stub.calls) == 3                   # nothing attempted
    assert report.get("backoff") == [{"board": "b", "retry_in_s": 870.0,
                                      "failures": 3}]
    assert fail_n(state_path, "b") == 3                         # not reset by skipping
    # ONE log line on entering the window (armed on attempt #3), not one per skip:
    armed = [r for r in caplog.records if "backing off" in r.getMessage()]
    assert len(armed) == 1 and "backing off 900s" in armed[0].getMessage()


def test_zero_audit_lines_inside_the_window(tmp_path, state_path):
    """THE measured defect, pinned at the W2 level: attempts write audit lines,
    so zero new lines is the proof nothing was attempted — the JSONL stops
    being a 30 s spool of one identical failure."""
    env = make_env(tmp_path, profiles=("b-orch",))
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    repl = FakeRepl(raises=InjectErrorLike(
        "no_panes", "no tmux pane running a Hermes REPL for profile 'b-orch'"))
    _arm_failure(db, state_path, env, repl, NOW + 100)          # attempt #1
    ow.run_tick(now=NOW + 130, boards={"b": db},
                state_path=state_path, repl=repl, **env)        # attempt #2
    ow.run_tick(now=NOW + 160, boards={"b": db},
                state_path=state_path, repl=repl, **env)        # attempt #3 arms
    before = len(audit_lines(env["audit_path"]))
    assert before == 3 and [l["ok"] for l in audit_lines(env["audit_path"])] == \
        [False, False, False]
    repl.calls.clear()
    for t in (190, 220, 250):                                    # deep inside
        assert ow.run_tick(now=NOW + t, boards={"b": db},
                           state_path=state_path, repl=repl, **env) == []
    assert repl.calls == []                                      # zero attempts
    assert len(audit_lines(env["audit_path"])) == before         # zero audit lines


def test_pending_keeps_batching_and_resumes_after_k(tmp_path, state_path):
    """Window expiry resumes: the WHOLE accumulated batch (the original line
    plus everything that arrived while skipped) goes out as ONE nudge."""
    db = make_board(tmp_path / "b" / "kanban.db",
                    tasks=[("t_1", "One"), ("t_2", "Two")])
    stub = StubInject(ok=False)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    for t in (100, 130, 160):                                    # arm at #3
        ow.tick(now=NOW + t, boards={"b": db}, state_path=state_path, inject=stub)
    add_events(db, [("t_2", 1, "completed", None)])              # arrives inside
    ow.tick(now=NOW + 190, boards={"b": db}, state_path=state_path,
           inject=stub)                                          # skipped, but scanned
    assert len(ow.load_state(state_path)["pending"]["b"]) == 2

    stub.ok = True                                               # pane came back
    dec = ow.tick(now=NOW + 160 + 900, boards={"b": db}, state_path=state_path,
                  inject=stub)                                   # exactly at K
    assert len(dec) == 1 and dec[0]["ok"] is True
    assert dec[0]["lines"] == ["t_1 done — One", "t_2 done — Two"]
    st = ow.load_state(state_path)
    assert "b" not in st["pending"] and "b" not in ow.fail_state(st)  # success clears


def test_window_expiry_then_three_fresh_attempts_rearm(tmp_path, state_path):
    """After a window expires the counter retires: the board gets a fresh set of
    `fail_threshold` attempts before the NEXT window — `n` always means
    consecutive failures since the last window, never a growing number."""
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject(ok=False)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    for t in (100, 130, 160):
        ow.tick(now=NOW + t, boards={"b": db}, state_path=state_path, inject=stub)
    # window 1: NOW+160 .. NOW+1060. The attempt at expiry restarts n at 1, and
    # the board then needs THREE fresh attempts before a second window arms.
    ow.tick(now=NOW + 1060, boards={"b": db}, state_path=state_path, inject=stub)
    assert len(stub.calls) == 4 and fail_n(state_path, "b") == 1
    ow.tick(now=NOW + 1090, boards={"b": db}, state_path=state_path, inject=stub)
    ow.tick(now=NOW + 1120, boards={"b": db}, state_path=state_path, inject=stub)
    assert len(stub.calls) == 6 and fail_n(state_path, "b") == 3   # rearmed
    report = {}
    assert ow.tick(now=NOW + 1150, boards={"b": db}, state_path=state_path,
                   inject=stub, report=report) == []              # skipped
    assert len(stub.calls) == 6                                    # nothing attempted
    assert report["backoff"][0]["failures"] == 3                   # second window


def test_success_resets_the_counter(tmp_path, state_path):
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject(ok=False)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    ow.tick(now=NOW + 100, boards={"b": db}, state_path=state_path, inject=stub)
    ow.tick(now=NOW + 130, boards={"b": db}, state_path=state_path, inject=stub)
    assert fail_n(state_path, "b") == 2                          # below threshold
    stub.ok = True
    dec = ow.tick(now=NOW + 160, boards={"b": db}, state_path=state_path,
                  inject=stub)
    assert dec[0]["ok"] is True
    st = ow.load_state(state_path)
    assert "fail" not in st                                      # key gone entirely
    # ...and the state file on disk is back to exactly the stopgap schema:
    assert set(json.loads(open(state_path).read())) == {"last", "pending", "sent"}


def test_orch_map_edit_resets_and_retries_immediately(tmp_path, state_path):
    """An operator who sets/edits orch_map must NOT have to wait out a window
    that was armed against the old, wrong target: the fingerprint change resets
    every counter mid-window and the very next tick attempts again."""
    db = make_board(tmp_path / "b" / "kanban.db", tasks=[("t_1", "One")])
    stub = StubInject(ok=False)
    ow.tick(now=NOW, boards={"b": db}, state_path=state_path)
    add_events(db, [("t_1", 1, "completed", None)])
    ow.tick(now=NOW + 10, boards={"b": db}, state_path=state_path)
    for t in (100, 130, 160):
        ow.tick(now=NOW + t, boards={"b": db}, state_path=state_path, inject=stub)
    report = {}
    assert ow.tick(now=NOW + 190, boards={"b": db}, state_path=state_path,
                   inject=stub, report=report) == []              # skipped
    assert report["backoff"]                                      # armed
    dec = ow.tick(now=NOW + 220, boards={"b": db}, state_path=state_path,
                  inject=stub, orch_map={"b": "ecofire-pontaje-orch"})
    assert len(dec) == 1 and len(stub.calls) == 4                 # retried at once
    assert fail_n(state_path, "b") == 1                           # fresh count
    st = json.loads(open(state_path).read())
    assert isinstance(st.get(ow.MAP_STATE_KEY), str)              # fingerprint stored


# ── config surface (hot-reloaded knobs) ─────────────────────────────────────

def cfg_path(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    return str(d / "orch_waker.json")


def write_cfg(tmp_path, doc):
    p = cfg_path(tmp_path)
    with open(p, "w") as f:
        json.dump(doc, f)
    return p


def test_config_new_knob_defaults(tmp_path):
    cfg = ow.load_config(cfg_path(tmp_path))
    assert cfg["orch_map"] == {}
    assert cfg["fail_threshold"] == ow.DEFAULT_FAIL_THRESHOLD == 3
    assert cfg["backoff_s"] == ow.DEFAULT_BACKOFF_S == 900


def test_config_orch_map_roundtrip_and_junk(tmp_path):
    p = write_cfg(tmp_path, {"orch_map": {"pontaje": "ecofire-pontaje-orch"}})
    assert ow.load_config(p)["orch_map"] == {"pontaje": "ecofire-pontaje-orch"}
    # junk whole-value => empty map (no overrides), never a crash or a guess:
    assert ow.load_config(write_cfg(tmp_path, {"orch_map": "pontaje=ghost"}))\
        ["orch_map"] == {}
    # junk ENTRIES are dropped one by one; a valid sibling survives (the value
    # is whitespace-stripped, a non-profile value can never route a board):
    p = write_cfg(tmp_path, {"orch_map": {"ok": " x-orch ", "bad": 5,
                                          "blank": "  ", "": "y-orch"}})
    assert ow.load_config(p)["orch_map"] == {"ok": "x-orch"}


def test_config_map_default_is_not_a_shared_mutable(tmp_path):
    """load_config hands out a FRESH dict every call — a caller (or a bug)
    mutating its config can never poison CONFIG_DEFAULTS for the next tick."""
    p = write_cfg(tmp_path, {"orch_map": {"a": "p"}})
    cfg = ow.load_config(p)
    cfg["orch_map"]["a"] = "poisoned"
    cfg["orch_map"]["injected"] = "also"
    assert ow.CONFIG_DEFAULTS["orch_map"] == {}
    assert ow.load_config(p)["orch_map"] == {"a": "p"}


@pytest.mark.parametrize("bad,floor,expected", [
    (0, "fail_threshold", 3),        # 0 would skip on the FIRST failure
    (True, "fail_threshold", 3),     # bool in a count field is a typo
    ("3", "fail_threshold", 3),
    (10, "fail_threshold", 10),      # a real value is honoured
    (5, "backoff_s", 900),           # below the 30 s stream cadence floor
    (-1, "backoff_s", 900),
    (120, "backoff_s", 120),
])
def test_config_backoff_knob_coercion(tmp_path, bad, floor, expected):
    p = write_cfg(tmp_path, {floor: bad})
    assert ow.load_config(p)[floor] == expected


def test_waker_tick_threads_config_into_run_tick(tmp_path, monkeypatch):
    """Hot-reload path: the daemon reads the config EVERY tick and hands all
    five tuning knobs to run_tick — a mid-run config edit is live next tick."""
    seen = {}

    def spy_run(**kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(ow, "run_tick", spy_run)
    p = write_cfg(tmp_path, {"orch_map": {"b": "x-orch"},
                             "fail_threshold": 5, "backoff_s": 120})
    ow.waker_tick(now=NOW, config_path=p, write=StreamlessWrite(),
                  read=lambda name: None)
    assert seen["orch_map"] == {"b": "x-orch"}
    assert seen["fail_threshold"] == 5 and seen["backoff_s"] == 120


class StreamlessWrite:
    def write(self, name, data):
        return data


def test_stream_message_names_the_backed_off_boards():
    assert ow._tick_message(0, 0, 0) == "steady"
    assert ow._tick_message(1, 0, 1) == "1 nudged, 0 failed, 1 decided"
    msg = ow._tick_message(0, 0, 0, [{"board": "pontaje", "retry_in_s": 870.0,
                                      "failures": 3}])
    assert msg.startswith("steady") and "backing off: pontaje (870.0s)" in msg
    many = [{"board": f"b{i}", "retry_in_s": 1.0, "failures": 3}
            for i in range(5)]
    assert ow._tick_message(0, 0, 0, many).endswith("+2 more")


# ── the deployed pairing: the per-tick stamp must never erase the counters ──

def make_tree(tmp_path, boards):
    """A throwaway ~/.hermes + one board under the pinned kanban root."""
    home = tmp_path / "hermes"
    boards = boards
    (boards / "demo").mkdir(parents=True)
    # v0.23.0 requires the ``board.json`` identity marker for discovery
    # (hermes_cli/kanban_db_boards.py::_dir_holds_board).
    (boards / "demo" / "board.json").write_text('{"name": "Demo"}')
    (home / "profiles" / "demo-orch").mkdir(parents=True)
    db = boards / "demo" / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT,"
        " created_at INTEGER);"
        "INSERT INTO tasks VALUES ('t_demo1', 'Demo card');")
    conn.close()
    return home, str(db)


def add_event(db):
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO task_events(task_id,run_id,kind,payload,created_at)"
                 " VALUES (?,?,?,?,?)", ("t_demo1", 7, "completed", None, 0))
    conn.commit()
    conn.close()


@pytest.fixture
def deployed(tmp_path, monkeypatch, hermes_kanban_root):
    """The waker exactly as the daemon runs it: default paths, the REAL
    state.write_state stamp, only clock + repl + roots replaced (no tmux)."""
    _, boards = hermes_kanban_root
    home, db = make_tree(tmp_path, boards)
    monkeypatch.setattr(ow, "DEFAULT_HERMES_HOME", str(home))
    repl = FakeRepl(raises=InjectErrorLike(
        "no_panes", "no tmux pane running a Hermes REPL for profile 'demo-orch'"))
    monkeypatch.setattr(ow, "load_repl_inject", lambda: repl)
    clock = {"now": float(T0 := 1_800_000_000)}
    monkeypatch.setattr(ow, "_real_time", lambda: clock["now"])

    def tick_at(offset):
        clock["now"] = float(T0 + offset)
        assert ow.check_orch_waker() is True
        return ow.waker_status(now=clock["now"])

    return {"db": db, "repl": repl, "tick_at": tick_at, "T0": float(T0)}


def test_backoff_survives_the_stamp_at_the_deployed_entry(deployed):
    """The t_fa9a2faf trap, re-armed against the new keys: the stream stamp
    rewrites the waker's own document every tick, so a `fail` counter that is
    not in the merge would be erased every tick and the backoff could NEVER
    arm (n would never exceed 1) — while the audit still showed retries and
    everything else read healthy."""
    tick_at, repl, db, T0 = (deployed["tick_at"], deployed["repl"],
                             deployed["db"], deployed["T0"])
    tick_at(0)
    add_event(db)
    tick_at(30)                                   # batched
    tick_at(120); tick_at(150); tick_at(180)      # 3 failed attempts -> armed
    assert len(repl.calls) == 3
    st = ow.load_state(None)                      # DEFAULT path, post-stamp
    assert ow.fail_state(st)["demo"]["n"] == 3, "counters survive the stamp"

    status = tick_at(210)                         # inside the window
    assert len(repl.calls) == 3, "no attempt inside the window"
    assert "backing off" in status["stream"]["message"]
    audit = os.path.expanduser(ow.DEFAULT_AUDIT_PATH)
    assert len([l for l in open(audit) if l.strip()]) == 3, "no audit inside window"
    assert status["boards"]["demo"]["pending"] == 1  # still batching

    tick_at(180 + 900)                            # window expired -> one attempt
    assert len(repl.calls) == 4
