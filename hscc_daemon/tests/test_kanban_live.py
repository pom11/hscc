"""Tests for hscc_daemon/kanban_live.py — `hscc kanban monitor`.

Offline only: a fake Hermes kanban lib over real in-memory sqlite boards with
the columns the collector reads. NEVER the operator's live boards or ~/.hermes.
"""

import inspect
import io
import json
import sqlite3

import pytest


from hscc_daemon import kanban_live
from hscc_daemon.cli_theme import make_console
from hscc_daemon.kanban_cli import _parse_monitor_args, cmd_kanban

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _no_real_hermes_home(tmp_path, monkeypatch):
    # attach_tokens() defaults to ~/.hermes; never read the operator's DBs here.
    monkeypatch.setattr(kanban_live, "HERMES_HOME", str(tmp_path / "hermes-home"))
    # ...nor write the real orchestrator ledger.
    monkeypatch.setattr(kanban_live, "LEDGER_PATH", str(tmp_path / "ledger.json"))

SCHEMA = """
-- The measured live shape, not an invention: `PRAGMA table_info` on the operator's
-- hscc board, so a fixture cannot drift from what the collector actually reads.
-- Before this card the fixture declared 9 task columns against the live 39 and 6
-- task_runs columns against the live 17 — `outcome`, `ended_at` and `metadata` did
-- not exist in a test board at all. (`task_events` was already the live 6 columns,
-- `run_id` and `payload` included; it stays as measured, not as rewritten.)
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,
  status TEXT, priority INTEGER, created_by TEXT, created_at INTEGER,
  started_at INTEGER, completed_at INTEGER, workspace_kind TEXT,
  workspace_path TEXT, branch_name TEXT, project_id INTEGER, claim_lock TEXT,
  claim_expires INTEGER, tenant TEXT, result TEXT, idempotency_key TEXT,
  consecutive_failures INTEGER, worker_pid INTEGER, last_failure_error TEXT,
  max_runtime_seconds INTEGER, last_heartbeat_at INTEGER, current_run_id INTEGER,
  workflow_template_id INTEGER, current_step_key TEXT, skills TEXT,
  model_override TEXT, provider_override TEXT, max_retries INTEGER,
  goal_mode INTEGER, goal_max_turns INTEGER, session_id TEXT, block_kind TEXT,
  block_recurrences INTEGER, reasoning_effort TEXT, completion_contract TEXT,
  worker_started_at INTEGER);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
  step_key TEXT, status TEXT, claim_lock TEXT, claim_expires INTEGER,
  worker_pid INTEGER, max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
  started_at INTEGER, ended_at INTEGER, outcome TEXT, summary TEXT,
  metadata TEXT, error TEXT, worker_started_at INTEGER);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,
  run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
"""

# Named columns for the fixtures: the tables are now as wide as the live ones, so
# a positional ``INSERT … VALUES (…)`` would be both unreadable and fragile.
_TASK_COLS = ("id", "title", "assignee", "status", "priority", "created_at",
              "completed_at", "last_heartbeat_at", "current_run_id")
_RUN_COLS = ("id", "task_id", "profile", "status", "started_at", "last_heartbeat_at",
             "ended_at", "outcome")


def _task(c, tid, title="t", assignee="backend-engineer", status="ready",
          priority=0, created_at=None, completed_at=None, last_heartbeat_at=None,
          current_run_id=None):
    c.execute(f"INSERT INTO tasks ({','.join(_TASK_COLS)}) VALUES "
              f"({','.join('?' * len(_TASK_COLS))})",
              (tid, title, assignee, status, priority, created_at, completed_at,
               last_heartbeat_at, current_run_id))


def _run(c, run_id, task_id, profile="backend-engineer", status="running",
         started_at=None, last_heartbeat_at=None, ended_at=None, outcome=None):
    c.execute(f"INSERT INTO task_runs ({','.join(_RUN_COLS)}) VALUES "
              f"({','.join('?' * len(_RUN_COLS))})",
              (run_id, task_id, profile, status, started_at, last_heartbeat_at,
               ended_at, outcome))


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *a):
        return False


class _FakeKb:
    def __init__(self, boards, broken=()):
        self.conns = {}
        self.broken = set(broken)
        for slug, build in boards.items():
            c = sqlite3.connect(":memory:")
            c.row_factory = sqlite3.Row
            c.executescript(SCHEMA)
            build(c)
            self.conns[slug] = c

    def list_boards(self):
        return [{"slug": s} for s in list(self.conns) + list(self.broken)]

    def connect_closing(self, board=None):
        if board in self.broken:
            raise RuntimeError("database is locked")
        return _Ctx(self.conns[board])


def _alpha(c):
    _run(c, 1, "t_run", started_at=NOW - 5 * 3600, last_heartbeat_at=NOW - 30)
    c.execute("INSERT INTO tasks (id,title,assignee,status,created_at,current_run_id) "
              "VALUES ('t_run','long job','backend-engineer','running',?,1)", (NOW - 9000,))
    c.execute("INSERT INTO tasks (id,title,assignee,status,priority,created_at) "
              "VALUES ('t_rdy','waits','qa','ready',5,?)", (NOW - 7200,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_rdy','promoted',?)",
              (NOW - 2400,))
    c.execute("INSERT INTO tasks (id,title,assignee,status,created_at,completed_at) "
              "VALUES ('t_done','fin','qa','done',?,?)", (NOW - 90000, NOW - 600))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_done','completed',?)",
              (NOW - 600,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_done','heartbeat',?)",
              (NOW - 500,))


def _beta(c):
    c.execute("INSERT INTO tasks (id,title,assignee,status,created_at) "
              "VALUES ('t_blk','stuck [/bold]','ml','blocked',?)", (NOW - 100,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_blk','gave_up',?)",
              (NOW - 120,))
    c.execute("INSERT INTO task_events (task_id,kind,created_at) VALUES ('t_blk','crashed',?)",
              (NOW - 7200,))  # outside the last-hour window


def _kb(**kw):
    return _FakeKb({"alpha": _alpha, "beta": _beta}, **kw)


def _collect(**kw):
    return kanban_live.collect(kanban_db=kw.pop("kb", None) or _kb(), now=NOW,
                               read_caps=lambda: (6, 2), **kw)


def test_collect_running_ready_counts_and_recent_events():
    d = _collect()
    assert [b["name"] for b in d["boards"]] == ["alpha", "beta"]
    (run,) = d["running"]
    assert run["worker"] == "backend-engineer"
    assert run["run_s"] == 5 * 3600 and run["hb_s"] == 30
    (rdy,) = d["ready"]
    assert rdy["wait_s"] == 2400  # since its last event, not since creation
    alpha = d["boards"][0]
    assert alpha["counts"] == {"running": 1, "ready": 1, "done": 1}
    assert alpha["done_24h"] == 1
    # heartbeats are not outcomes; events older than an hour drop out
    assert [(e["id"], e["kind"]) for e in d["events"]] == [
        ("t_blk", "gave_up"), ("t_done", "completed")]
    assert d["caps"] == (6, 2)


def test_board_filter_and_unreadable_board_does_not_stop_the_pass():
    d = _collect(kb=_kb(broken=["gamma"]))
    assert any("gamma" in e and "unreadable" in e for e in d["errors"])
    assert len(d["boards"]) == 2
    d = _collect(board="beta")
    assert [b["name"] for b in d["boards"]] == ["beta"] and not d["running"]


def test_render_escapes_titles_and_flags_full_cap():
    d = _collect()
    d["caps"] = (1, 2)
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(kanban_live.render(d, snapshot=True))
    out = buf.getvalue()
    assert "1/1 slots (≤2/profile)" in out and "global cap full" in out
    assert "running (" not in out
    assert "slots in use" not in out
    assert "t_run" in out and "5h00m" in out and "stuck [/bold]" in out


def test_short_pane_drops_lower_sections_first():
    d = _collect()
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(
        kanban_live.render(d, height=12))
    out = buf.getvalue()
    assert "t_run" in out and "ready, waiting" in out
    assert "last hour" not in out


def test_fmt_age():
    assert [kanban_live.fmt_age(x) for x in (None, 5, 125, 3700, 90000)] == [
        "-", "5s", "2m", "1h01m", "1d1h"]


def test_parse_monitor_args():
    assert _parse_monitor_args(["--live", "--interval", "3", "--board", "x"]) == (
        {"live": True, "interval": 3, "board": "x"}, None)
    assert _parse_monitor_args(["--interval", "0"])[1]
    assert _parse_monitor_args(["--board"])[1]
    assert "unknown argument" in _parse_monitor_args(["--nope"])[1]


def test_cli_json_snapshot_and_live_json_refused(monkeypatch, capsys):
    monkeypatch.setattr(kanban_live, "collect",
                        lambda board=None: {"boards": [], "running": [], "ready": [],
                                            "events": [], "caps": (6, 2), "errors": [],
                                            "at": NOW})
    assert cmd_kanban(["monitor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["caps"] == [6, 2]
    assert cmd_kanban(["monitor", "--json", "--live"]) == 1


def test_cli_help_executes_nothing(monkeypatch, capsys):
    monkeypatch.setattr(kanban_live, "collect", lambda **k: (_ for _ in ()).throw(AssertionError))
    assert cmd_kanban(["monitor", "--help"]) == 0
    assert "hscc kanban monitor" in capsys.readouterr().out


def test_hscc_kanban_dispatcher_keeps_json_for_delegated_verbs(monkeypatch, capsys):
    # `hscc kanban` enters through kanban_blocked.cmd_kanban, which strips
    # --json before delegating stale/monitor to kanban_cli — the flag must be
    # handed back or the "JSON" output is the human table.
    from hscc_daemon import kanban_blocked
    monkeypatch.setattr(kanban_live, "collect",
                        lambda board=None: {"boards": [], "running": [], "ready": [],
                                            "events": [], "caps": (6, 2), "errors": [],
                                            "at": NOW})
    assert kanban_blocked.cmd_kanban(["monitor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["caps"] == [6, 2]


_SESSION_COLS = ("id", "source", "started_at", "input_tokens", "output_tokens",
                 "ended_at", "title", "last_activity_at", "last_activity_description")
                                 # the reader-facing subset of the live 65 columns;
                                 # legacy rows are the first 5 (see the CREATE TABLE)


def _state_db(path, rows, leases=(), *, lease_table=True, nullable_source=False):
    """One profile store: the ``sessions`` table plus (v0.23) the
    ``session_turn_leases`` table hermes writes for every in-flight turn.

    ``rows`` keeps the historical ``(id, source, started_at, input_tokens,
    output_tokens)`` shape — the columns the token tests read — padded with
    NULLs for the v0.23 columns they do not care about. Pass the full 9-tuple
    ``(id, source, started_at, input, output, ended_at, title,
    last_activity_at, last_activity_description)`` when a test needs them.

    ``nullable_source=True`` drops the live ``NOT NULL`` on ``source``. A real
    store cannot hand the readers a NULL source (measured: ``source IS NULL`` = 0
    of 7 223 session rows over 51 stores), so this builds the legacy-shaped store
    whose ONLY purpose is to keep the two readers' NULL-defensive clauses pinned
    (``_LIVE_TURNS``' ``s.source IS NULL``, the token table's complement, where a
    bare ``NOT IN`` silently drops the row).

    ``leases`` are ``(conversation_id, acquired_at, expires_at)``: one row per
    turn that is running RIGHT NOW. ``lease_table=False`` builds a pre-v0.23
    store with no lease table at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    # The reader-facing columns of the live `sessions` table (65 columns measured
    # by PRAGMA table_info on this host's root store) plus every NOT NULL column
    # with its measured default — so a fixture row has to satisfy the same insert
    # contract the kernel's does, instead of a schema this file invented.
    c.execute("CREATE TABLE sessions (id TEXT, "
              + ("source TEXT NOT NULL" if not nullable_source else "source TEXT")
              + ", "
              "started_at REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER, "
              "ended_at REAL, title TEXT, last_activity_at REAL, "
              "last_activity_description TEXT, "
              "rewind_count INTEGER NOT NULL DEFAULT 0, "
              "archived INTEGER NOT NULL DEFAULT 0, "
              "hidden INTEGER NOT NULL DEFAULT 0, "
              "pinned INTEGER NOT NULL DEFAULT 0, "
              "compression_fallback_streak INTEGER NOT NULL DEFAULT 0, "
              "compression_ineffective_count INTEGER NOT NULL DEFAULT 0, "
              "git_metadata_generation INTEGER NOT NULL DEFAULT 0, "
              "runtime_revision INTEGER NOT NULL DEFAULT 0, "
              "runtime_generation INTEGER NOT NULL DEFAULT 0, "
              "transcript_revision INTEGER NOT NULL DEFAULT 0)")
    c.executemany(f"INSERT INTO sessions ({','.join(_SESSION_COLS)}) VALUES "
                  f"({','.join('?' * len(_SESSION_COLS))})",
                  [tuple(r) + (None,) * (len(_SESSION_COLS) - len(r)) for r in rows])
    if lease_table:
        c.execute("CREATE TABLE session_turn_leases (conversation_id TEXT PRIMARY KEY, "
                  "holder TEXT, acquired_at REAL, expires_at REAL)")
        c.executemany("INSERT INTO session_turn_leases VALUES (?,?,?,?)",
                      [(cid, f"pid=20411:turn={cid}:platform=cli", acq, exp)
                       for cid, acq, exp in leases])
    c.commit()
    c.close()


def test_attach_tokens_matches_worker_session_and_sums_24h(tmp_path):
    run_start = NOW - 5 * 3600
    _state_db(tmp_path / "profiles" / "backend-engineer" / "state.db", [
        ("old", "kanban", run_start - 3000, 9, 9),            # earlier card, same worker
        ("mine", "kanban", run_start + 2, 18_000_000, 202_000),
        ("chat", "cli", run_start + 1, 5, 5),                  # not a worker session
        ("stale", "kanban", NOW - 2 * 86400, 7, 7),            # outside 24h
    ])
    _state_db(tmp_path / "profiles" / "qa" / "state.db", [
        ("q", "kanban", NOW - 60, 1_000, 50)])
    (tmp_path / "profiles" / "no-db").mkdir()
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    run = d["running"][0]
    assert (run["tok_in"], run["tok_out"]) == (18_000_000, 202_000)
    # 24h: 'mine' (5h ago) + 'old' (5h50m ago) + qa's, never cli or >24h
    assert d["tokens_24h"] == [18_000_000 + 9 + 1_000, 202_000 + 9 + 50]


def test_attach_tokens_without_state_db_leaves_dashes(tmp_path):
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    assert d["running"][0]["tok_in"] is None and d["tokens_24h"] is None
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(kanban_live.render(d))
    assert "Tokens" in buf.getvalue()


# ── per-run session identity (MON-23-SESSION-ID) ──────────────────────────
#
# Under Hermes v0.23 the dispatcher dials the profile owner, which admits each
# run into ONE gateway session and records it in the board as the
# `owner_admitted` task_event for `tasks.current_run_id`
# (payload keys: birth, db, pid, request_id, session_id). `task_runs` has no
# session column, so that event is the only board-side run -> session link.
# Cards claimed in the same second — what the operator hit on ecofire-bc with
# t_a727e3b8 / t_2e818869 / t_48c90037 — are indistinguishable to a
# started_at window, so identity is the only thing that can attribute tokens.


def _tri_board(db_path, run_started_at):
    """Builder for three cards claimed in the SAME second by one profile, each
    with its own `owner_admitted` event naming `<db_path>`. Mirrors the real
    `task_events` schema (run_id + payload are already in SCHEMA)."""
    cards = (("t_a727e3b8", 392, "local-aaa"),
             ("t_2e818869", 393, "local-bbb"),
             ("t_48c90037", 394, "local-ccc"))

    def build(c):
        for i, (tid, run_id, sid) in enumerate(cards):
            _run(c, run_id, tid, profile="ecofire-bc-engineer",
                 started_at=run_started_at, last_heartbeat_at=NOW - 30)
            c.execute("INSERT INTO tasks (id,title,assignee,status,created_at,current_run_id) "
                      "VALUES (?,?,?,'running',?,?)",
                      (tid, f"job {i}", "ecofire-bc-engineer", run_started_at, run_id))
            c.execute("INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
                      "VALUES (?,?,?,?,?)",
                      (tid, run_id, "owner_admitted",
                       json.dumps({"birth": 1.5, "db": db_path, "pid": 20411,
                                   "request_id": f'kanban:["tri","{tid}",{run_id}]',
                                   "session_id": sid}),
                       run_started_at))
    return build


def test_collect_links_each_running_card_to_its_run_session(tmp_path):
    """collect() puts the run's session_id and the owner's own db on the row."""
    db = tmp_path / "owner" / "state.db"
    _state_db(db, [])                             # the owner's own store
    d = _collect(kb=_FakeKb({"tri": _tri_board(str(db), NOW - 600)}))
    rows = {r["id"]: r for r in d["running"]}
    assert [rows[t]["session_id"] for t in
            ("t_a727e3b8", "t_2e818869", "t_48c90037")] == ["local-aaa", "local-bbb", "local-ccc"]
    assert all(r["session_db"] == str(db) for r in rows.values())
    assert all(r["tok_match"] is None for r in rows.values())   # not attached yet

    # A payload naming a file that is not there is not a path this module will
    # invent from board data: session_db stays None, so attach_tokens uses
    # _state_db_path(home, worker) — and identity still travels on the row.
    d = _collect(kb=_FakeKb({"tri": _tri_board(str(tmp_path / "gone" / "state.db"), NOW - 600)}))
    assert all(r["session_db"] is None and r["session_id"] for r in d["running"])


def test_three_cards_started_in_one_second_get_three_distinct_token_pairs(tmp_path):
    """The reported bug: all three printed `1.4M/22k` because every window
    matched the same `ORDER BY started_at LIMIT 1` session row."""
    started = NOW - 600                      # identical claim second for all three
    db = tmp_path / "profiles" / "ecofire-bc-engineer" / "state.db"
    _state_db(db, [
        ("local-aaa", "kanban", started + 2, 1_000_000, 12_000),
        ("local-bbb", "kanban", started + 2, 2_738_286, 34_510),
        ("local-ccc", "kanban", started + 2, 2_386_590, 33_859),
    ])
    d = _collect(kb=_FakeKb({"tri": _tri_board(str(db), started)}))
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    by_card = {r["id"]: (r["tok_in"], r["tok_out"]) for r in d["running"]}
    assert by_card == {"t_a727e3b8": (1_000_000, 12_000),
                       "t_2e818869": (2_738_286, 34_510),
                       "t_48c90037": (2_386_590, 33_859)}
    assert len(set(by_card.values())) == 3
    assert {r["tok_match"] for r in d["running"]} == {"session"}


def test_known_session_id_never_falls_back_to_the_window(tmp_path):
    """A run WITH an `owner_admitted` event must not be matched by time: the
    window would happily hand back the neighbouring card's session."""
    started = NOW - 600
    db = tmp_path / "profiles" / "ecofire-bc-engineer" / "state.db"
    _state_db(db, [("local-neighbour", "kanban", started + 1, 999, 9)])   # inside the window
    d = _collect(kb=_FakeKb({"tri": _tri_board(str(db), started)}))
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    r = next(r for r in d["running"] if r["id"] == "t_a727e3b8")
    assert (r["tok_in"], r["tok_out"], r["tok_match"]) == (None, None, None)


def test_run_without_owner_admitted_event_uses_the_window(tmp_path):
    """A brand-new run is admitted to its session ~2 s after the claim, so it
    legitimately has no event yet — the window stays as the labelled fallback."""
    db = tmp_path / "profiles" / "backend-engineer" / "state.db"
    _state_db(db, [("mine", "kanban", NOW - 5 * 3600 + 2, 18_000_000, 202_000)])
    d = _collect()                                    # `_alpha`: no owner_admitted
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    r = d["running"][0]
    assert (r["session_id"], r["session_db"]) == (None, None)
    assert (r["tok_in"], r["tok_out"], r["tok_match"]) == (18_000_000, 202_000, "window")


def test_owner_admitted_payload_db_wins_over_the_profile_path(tmp_path):
    """The owner's `db` is authoritative — the profile dir may hold a copy."""
    started = NOW - 600
    owner = tmp_path / "elsewhere" / "state.db"
    _state_db(owner, [("local-aaa", "kanban", started + 2, 111, 1)])
    _state_db(tmp_path / "profiles" / "ecofire-bc-engineer" / "state.db",
              [("local-aaa", "kanban", started + 2, 777, 7)])   # stale copy
    d = _collect(kb=_FakeKb({"tri": _tri_board(str(owner), started)}))
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    r = next(r for r in d["running"] if r["id"] == "t_a727e3b8")
    assert (r["tok_in"], r["tok_out"]) == (111, 1)


def test_unreadable_or_unparsable_store_leaves_dashes(tmp_path):
    started = NOW - 600
    garbage = tmp_path / "elsewhere" / "state.db"
    garbage.parent.mkdir(parents=True)
    garbage.write_bytes(b"not a database at all")
    d = _collect(kb=_FakeKb({"tri": _tri_board(str(garbage), started)}))
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    assert all((r["tok_in"], r["tok_out"]) == (None, None) for r in d["running"])

    # a payload that is not JSON (or absent) must not break the board either
    def _junk(c):
        _tri_board("/nonexistent/state.db", started)(c)
        c.execute("INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
                  "VALUES ('t_a727e3b8',392,'owner_admitted','{broken',?)", (NOW,))
    d = _collect(kb=_FakeKb({"tri": _junk}))
    assert not d["errors"]
    r = next(r for r in d["running"] if r["id"] == "t_a727e3b8")
    assert r["session_id"] == "local-aaa"             # the good event still wins


def test_render_shows_card_tokens_and_the_token_table_once():
    """One token surface, not two that have to be reconciled.

    The old renderer could print a `tokens 24h in … · out …` line *beside* the
    table — same window, different query, different source set — and `tokens_24h`
    in the payload was that other measurement. Now the table is the only token
    output and `tokens_24h` is one of its own cells (pinned by
    ``test_tokens_24h_is_the_tables_own_cell_not_a_second_measurement``).
    """
    d = _collect()
    d["running"][0].update(tok_in=18_000_000, tok_out=202_000)
    d["tokens"] = {"workers": {"24h": [100_000_000, 3_000_000], "7d": [200_000_000, 6_000_000]},
                   "scheduled": {"24h": [10_000_000, 1_000_000], "7d": [20_000_000, 2_000_000]},
                   "orchestrators": {"24h": [20_000_000, 200_000], "7d": [70_000_000, 700_000],
                                     "since": None}}
    d["tokens_24h"] = d["tokens"]["workers"]["24h"]
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(kanban_live.render(d))
    lines = buf.getvalue().splitlines()
    assert "18.0M/202k" in "\n".join(lines)          # the running card's own session
    body = {}
    for line in lines:
        parts = line.split()
        # `parts[1] != "="` skips the note line, which also starts with "workers"
        if len(parts) > 1 and parts[0] in kanban_live.GROUPS + ("total",) and parts[1] != "=":
            body[parts[0]] = parts[1:]
    assert body["workers"] == ["100.0M", "3.0M", "200.0M", "6.0M"]
    assert body["scheduled"] == ["10.0M", "1.0M", "20.0M", "2.0M"]
    assert body["orchestrators"] == ["20.0M", "200k", "70.0M", "700k"]
    assert body["total"] == ["130.0M", "4.2M", "290.0M", "8.7M"]     # the three added
    assert "tokens 24h in" not in "\n".join(lines)   # the fallback line is gone
    # and the two notes say what the rows are made of
    assert "workers = kanban+subagent+oneshot" in "\n".join(lines)


def test_the_partition_bills_every_measured_source_exactly_once():
    """`group_of` is a partition of the sources measured on this host.

    Census over the 51 stores: cron 3620, kanban 1895, cli 1526, telegram 103,
    subagent 54, oneshot 3, local 2, NULL 0. The old code had two rows and one
    catch-all (`source NOT IN ('kanban','cron')`), so `subagent` and `telegram`
    were billed as orchestrator use and `total` absorbed them without saying so;
    a NULL source vanished from every row, because SQL `NOT IN` is NULL — not
    TRUE — for NULL. An unrecognised source must land in `orchestrators`, where
    the operator can see it, not in `workers` and not nowhere.
    """
    measured = ("kanban", "subagent", "oneshot",          # machine work a card asked for
                "cron",                                   # the scheduler
                "cli", "local", "telegram", None)         # an operator somewhere
    assert set(map(kanban_live.group_of, measured)) == set(kanban_live.GROUPS)
    for src in ("kanban", "subagent", "oneshot"):
        assert kanban_live.group_of(src) == "workers", src
    assert kanban_live.group_of("cron") == "scheduled"
    for src in ("cli", "local", "telegram", None, "whatsapp-invented-next-release"):
        assert kanban_live.group_of(src) == "orchestrators", src
    # disjoint, or one session would be counted in two rows and `total` would lie
    assert not set(kanban_live.WORKER_SOURCES) & set(kanban_live.SCHEDULED_SOURCES)
    assert tuple(kanban_live.GROUPS) == ("workers", "scheduled", "orchestrators")
    # the pane says the same thing out loud, so the mapping is not a code secret
    notes = " ".join(kanban_live.TOKEN_NOTES)
    for src in ("kanban", "subagent", "oneshot", "cron", "cli", "telegram"):
        assert src in notes, src
    for g in kanban_live.GROUPS:
        assert g in notes, g


def test_the_orchestrator_panel_and_the_token_table_agree_on_what_a_worker_is():
    """A hotspot guard for the two partitions of `sessions.source` in this file.

    `attach_orchestrators` (t_1f14c019) excludes a list of worker-family sources
    from the live-turn panel; the token table partitions the same column into
    workers / scheduled / orchestrators. They must stay the same cut of the world:
    if one grows a source and the other does not, a dispatched run is either an
    orchestrator in the panel and a worker in the table, or invisible in both. This
    test does not touch their definitions — it only refuses the drift.
    """
    assert set(kanban_live.NON_ORCHESTRATOR_SOURCES) == set(
        kanban_live.WORKER_SOURCES + kanban_live.SCHEDULED_SOURCES)
    # and the panel's catch-all side is the token table's orchestrator group
    for src in ("cli", "local", "telegram", None, "unknown-next-release"):
        assert kanban_live.group_of(src) == "orchestrators", src
        assert (src is None or src not in kanban_live.NON_ORCHESTRATOR_SOURCES), src


def test_a_card_started_before_the_window_is_charged_to_the_window_it_worked_in(tmp_path):
    """The attribution case, pinned (D1).

    The old predicate was `started_at >= now-span`: a card that ran 25 h was
    invisible to 24 h and charged wholly to 7 d. Measured on this host the 7 d
    column was missing 7 kanban sessions — 58,501,630 in / 684,357 out — that
    started 7.06 d ago and were active inside 7 d. Nothing is *split*: with
    `messages.token_count` NULL in all 595,776 message rows there is no per-call
    time to split by, so the whole lifetime total is charged to each window the
    session was active in, and the table and `--json` both say that.
    """
    _state_db(tmp_path / "profiles" / "backend-engineer" / "state.db", [
        # started 26 h ago, still moving now: inside the 24 h window
        ("long", "kanban", NOW - 26 * 3600, 1_000, 100, None, None, NOW - 60, "turn"),
        # started 30 h ago, last moved 26 h ago: 7 d only
        ("done", "kanban", NOW - 30 * 3600, 2_000, 200, NOW - 26 * 3600, None,
         NOW - 26 * 3600, None),
        # finished 9 d ago: in neither window
        ("gone", "kanban", NOW - 9 * 86400, 5, 5, NOW - 9 * 86400, None,
         NOW - 9 * 86400, None),
    ])
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    t = d["tokens"]
    assert t["workers"]["24h"] == [1_000, 100]      # `long`, because it was active now
    assert t["workers"]["7d"] == [3_000, 300]       # `long` + `done`, never `gone`


def test_subagent_and_telegram_land_in_the_group_the_table_names(tmp_path):
    """D2, with a v0.23-shaped fixture: the two sources the old catch-all swallowed.

    `subagent`/`oneshot` are machine work a card asked for, so they join `workers`;
    `telegram` is an operator talking to an agent, so it is orchestrator-side — and
    it is charged as *ledger growth*, never as its lifetime total, because these
    sessions live for months (longest measured activity span: 148 days). `total` is
    then the sum of the three rows, which is what the card means by "no silent
    absorption".
    """
    _state_db(tmp_path / "profiles" / "backend-engineer" / "state.db", [
        ("k", "kanban", NOW - 3600, 100, 10),
        ("sub", "subagent", NOW - 3600, 30, 3),         # was: "orchestrator"
        ("one", "oneshot", NOW - 3600, 1, 1),           # was: "orchestrator"
    ])
    _state_db(tmp_path / "profiles" / "cron-box" / "state.db", [
        ("c", "cron", NOW - 600, 40, 4),                # was: "orchestrator"
    ])
    _state_db(tmp_path / "profiles" / "hscc-orch" / "state.db", [
        # lifetime totals summing to exactly 5000/500, i.e. the ledger's last sample
        ("repl", "cli", NOW - 60 * 86400, 4_000, 400, None, None, NOW - 50, "idle"),
        ("tg", "telegram", NOW - 40 * 86400, 900, 90, None, None, None, None),
        ("nul", None, NOW - 40 * 86400, 100, 10),       # SQL NOT IN dropped this one
    ], nullable_source=True)   # a live store cannot write this (0 of 7 223 rows);
                               # the row exists only to keep the complement NULL-safe
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps([[NOW - 2 * 86400, 1_000, 100],
                                  [NOW - 3600, 5_000, 500]]))
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW, ledger_path=str(ledger))
    t = d["tokens"]
    assert t["workers"]["24h"] == [131, 14]             # kanban + subagent + oneshot
    assert t["scheduled"]["24h"] == [40, 4]             # cron, on its own row
    assert t["orchestrators"]["24h"] == [4_000, 400]    # 1000 → 5000 growth, nothing else
    assert t["orchestrators"]["since"] == NOW - 2 * 86400
    # the sum the operator is allowed to read as "everything on this host"
    rows = {g: cells for g, cells in kanban_live.token_rows(t)}
    assert rows["total"][0] == [131 + 40 + 4_000, 14 + 4 + 400]
    # and the numbers that would have been there before the partition are not:
    # neither telegram's lifetime 900/90 nor the NULL row's 100/10 in workers.
    assert rows["workers"][0] == [131, 14]


def test_a_workers_growth_can_never_make_the_orchestrator_row_grow(tmp_path):
    """The ledger's cumulative is the orchestrator group only (D3).

    `cum` used to be `source NOT IN ('kanban','cron')`, which put every `subagent`,
    `oneshot` and `telegram` session into the number the ledger differences — so the
    "orchestrators" row rose when a *card's* delegated subagent did its work. Here a
    card session and its subagent each grow by 500 in / 50 out between two ticks:
    `workers` follows both, `orchestrators` does not move at all. (Red at the base
    commit: there the orchestrator row grows by exactly the subagent's 500/50.)
    """
    db = tmp_path / "profiles" / "backend-engineer" / "state.db"
    _state_db(db, [("k", "kanban", NOW - 3600, 1_000, 100),
                   ("s", "subagent", NOW - 3600, 500, 50)])
    ledger = tmp_path / "ledger.json"
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW, ledger_path=str(ledger))
    assert d["tokens"]["workers"]["24h"] == [1_500, 150]        # card + its subagent
    assert d["tokens"]["orchestrators"]["24h"] == [0, 0]        # first sample: no history
    assert json.loads(ledger.read_text()) == [[NOW, 0, 0]]      # worker tokens are not in it

    c = sqlite3.connect(db)                                     # the card keeps working
    c.execute("UPDATE sessions SET input_tokens=input_tokens+500, "
              "output_tokens=output_tokens+50")
    c.commit()
    c.close()
    d2 = _collect()
    kanban_live.attach_tokens(d2, hermes_home=str(tmp_path), now=NOW + 400,
                              ledger_path=str(ledger))
    assert d2["tokens"]["workers"]["24h"] == [2_500, 250]       # workers follow the work
    assert d2["tokens"]["orchestrators"]["24h"] == [0, 0]       # orchestrators do not move


def test_a_narrower_ledger_step_costs_nothing_and_stays_non_negative(tmp_path):
    """Migrating the ledger to the orchestrator group must not make a window negative.

    The cumulative drops by the `subagent`/`oneshot` total (62.7M in on this host)
    at the first sample after this change, exactly like a deleted session. The
    per-step floor in ``_ledger_delta`` turns that step into 0 and the growth after
    it still counts — the guarantee the deleted-session test has always pinned.
    """
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps([[NOW - 7200, 60_000, 6_000],     # still included
                                  [NOW - 3600, 55_000, 5_500]]))   # narrowed: a drop
    _state_db(tmp_path / "profiles" / "hscc-orch" / "state.db",
              [("r", "cli", NOW - 30 * 86400, 56_000, 5_600)])
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW, ledger_path=str(ledger))
    o = d["tokens"]["orchestrators"]
    assert o["24h"] == [1_000, 100]          # only 55_000 → 56_000 counts; the drop is 0
    assert all(v >= 0 for pair in o.values() if isinstance(pair, list) for v in pair)


def test_tokens_24h_is_the_tables_own_cell_not_a_second_measurement(tmp_path):
    """D4: `--json` must not carry two totals for one window.

    `tokens_24h` predates the table (2.14.0) and summed `source='kanban'` by
    `started_at` on its own, so it disagreed with the table beside it — different
    source set, different predicate. It stays as a mirror of the workers/24 h cell,
    which costs one fewer scan of every store and cannot disagree by construction.
    """
    _state_db(tmp_path / "profiles" / "backend-engineer" / "state.db", [
        ("mine", "kanban", NOW - 5 * 3600, 18_000_000, 202_000),
        ("sub", "subagent", NOW - 5 * 3600, 4_000, 40),
    ])
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    assert d["tokens_24h"] == d["tokens"]["workers"]["24h"] == [18_004_000, 202_040]


def test_no_store_at_all_is_a_dash_in_every_row(tmp_path):
    """Existing contract, now across all three rows: nothing readable → `-`, no raise."""
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW)
    assert d["tokens_24h"] is None
    for group, cells in kanban_live.token_rows(d["tokens"]):
        assert all(c is None for c in cells), group      # never a silent 0
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(kanban_live.render(d))
    out = buf.getvalue()
    assert out.count("-") >= 8 and "workers" in out and "scheduled" in out


def test_token_windows_are_three_groups_that_add_up(tmp_path):
    """`workers` + `scheduled` + `orchestrators` = `total`, by construction.

    Rewritten for the partition: `cron` used to be billed as orchestrator use
    inside the same row as the REPL growth, which is how `subagent` and
    `telegram` got in there too. A weeks-old `cli` session is still ledger
    growth only — its lifetime total must never appear in a window cell.
    """
    _state_db(tmp_path / "profiles" / "backend-engineer" / "state.db", [
        ("w1", "kanban", NOW - 3600, 100, 10),                 # in 24h and 7d
        ("w2", "kanban", NOW - 3 * 86400, 1_000, 100),         # 7d only
        ("w3", "kanban", NOW - 9 * 86400, 5, 5)])              # neither
    _state_db(tmp_path / "profiles" / "sphoin-orch" / "state.db", [
        ("c1", "cron", NOW - 600, 40, 4),                      # scheduler: its own row
        ("repl", "cli", NOW - 60 * 86400, 9_000, 900)])        # weeks-old REPL: ledger only
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps([[NOW - 8 * 86400, 1_000, 100],   # before the 7d window
                                  [NOW - 2 * 86400, 5_000, 500],
                                  [NOW - 3600, 8_000, 800]]))
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW, ledger_path=str(ledger))
    t = d["tokens"]
    assert t["workers"]["24h"] == [100, 10]
    assert t["workers"]["7d"] == [1_100, 110]
    assert t["scheduled"]["24h"] == [40, 4] == t["scheduled"]["7d"]
    # growth since the sample at/before NOW-24h (5000 → 8000 → 9000), cron not in it
    assert t["orchestrators"]["24h"] == [4_000, 400]
    # 7d: growth from the NOW-8d sample (1000 → 9000)
    assert t["orchestrators"]["7d"] == [8_000, 800]
    assert t["orchestrators"]["since"] is None      # ledger already covers 7 days
    assert json.loads(ledger.read_text())[-1] == [NOW, 9_000, 900]
    # the three rows add up to the row the operator reads as "everything"
    rows = {g: cells for g, cells in kanban_live.token_rows(t)}
    for i, (name, _span) in enumerate(kanban_live.WINDOWS):
        assert rows["total"][i] == [rows["workers"][i][0] + rows["scheduled"][i][0]
                                    + rows["orchestrators"][i][0],
                                    rows["workers"][i][1] + rows["scheduled"][i][1]
                                    + rows["orchestrators"][i][1]], name
    # --json says what the numbers are, instead of the operator guessing
    assert t["meta"]["groups"]["workers"] == ["kanban", "subagent", "oneshot"]
    assert t["meta"]["groups"]["scheduled"] == ["cron"]
    assert "active in" in t["meta"]["attribution"]
    assert "reasoning" in t["meta"]["columns"]


def test_ledger_survives_a_deleted_session_and_reports_since(tmp_path):
    ledger = tmp_path / "ledger.json"
    samples = [[NOW - 7200, 5_000, 500], [NOW - 3600, 1_000, 100]]   # a session got deleted
    ledger.write_text(json.dumps(samples))
    _state_db(tmp_path / "profiles" / "x-orch" / "state.db", [("r", "cli", NOW - 86400 * 30, 1_600, 160)])
    d = _collect()
    kanban_live.attach_tokens(d, hermes_home=str(tmp_path), now=NOW, ledger_path=str(ledger))
    o = d["tokens"]["orchestrators"]
    assert o["24h"] == [600, 60]                    # the drop costs 0, never negative
    assert o["since"] == NOW - 7200                 # less than 7 days of history


def test_ledger_samples_at_most_every_step(tmp_path):
    ledger = tmp_path / "ledger.json"
    kanban_live._update_ledger(str(ledger), NOW, [1, 1])
    kanban_live._update_ledger(str(ledger), NOW + 60, [2, 2])        # too soon: not stored
    kanban_live._update_ledger(str(ledger), NOW + 400, [3, 3])
    assert json.loads(ledger.read_text()) == [[NOW, 1, 1], [NOW + 400, 3, 3]]


# ── orchestrators: live turns from the session store (t_1f14c019) ─────────
#
# Hermes v0.23 starts a REPL as `python3 -I -c '<~10 KB bootstrap>' -p <profile>
# chat ...`, so argv[0] is the interpreter and argv[1] is `-I`: the old
# `_chat_profiles()` gate (an argv[:2] entry ending in `/hermes`) matched nothing
# and the section silently disappeared — measured 2026-11-09 with seven REPL
# processes alive. Turn liveness is a store fact now: `session_turn_leases` holds
# one row per in-flight turn, renewed while the turn runs and self-expiring, so
# `ps` is gone rather than merely demoted.

def _turn(cid, acquired_at, expires_at=NOW + 300):
    """A `session_turn_leases` row `(conversation_id, acquired_at, expires_at)`.

    hermes renews `expires_at` while the turn runs — measured a 6238 s-old lease
    still 254 s from expiry — so a live turn is simply `expires_at > now`, however
    long it has been going. Pass a past `expires_at` for a turn that died.
    """
    return (cid, acquired_at, expires_at)


def test_orchestrator_detection_keeps_no_process_list_dependency():
    """D3 was "the store over `ps`", and the card forbids leaving a dead argv
    heuristic behind as a fallback that can only produce false negatives."""
    assert not hasattr(kanban_live, "_chat_profiles")
    assert not hasattr(kanban_live, "_ps_lines")
    assert "subprocess" not in vars(kanban_live)
    assert "ps_lines" not in inspect.signature(
        kanban_live.attach_orchestrators).parameters


def test_a_live_turn_lease_is_the_only_thing_that_makes_a_working_orchestrator(tmp_path):
    """One row per live lease, read from the store that owns it: profile, session
    id, turn age, lifetime tokens, and `last_activity_description` as state."""
    _state_db(tmp_path / "profiles" / "hscc-orch" / "state.db",
              [("local-41d4ee6a", "local", NOW - 7200, 1_685_225, 10_065,
                None, "hscc", NOW - 40, "receiving stream response")],
              leases=[_turn("local-41d4ee6a", NOW - 323)])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert d["orchestrators"] == [{
        "profile": "hscc-orch", "session": "local-41d4ee6a", "turn_s": 323,
        "tok_in": 1_685_225, "tok_out": 10_065, "state": "receiving stream response"}]


def test_a_repl_without_a_lease_is_idle_and_not_listed(tmp_path):
    """CHOSEN AND PINNED: an idle REPL is not listed at all. The section header
    says "(N running)", and `ended_at IS NULL` proves nothing — 871 `cli`
    sessions on the measured host are NULL there purely because the process died
    before anything stamped it."""
    _state_db(tmp_path / "profiles" / "sphoin-orch" / "state.db",
              [("idle-repl", "local", NOW - 9 * 86400, 9_000, 900, None, "sphoin",
                NOW - 4000, "awaiting input")])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert d["orchestrators"] == []


def test_an_expired_lease_is_not_listed(tmp_path):
    """The lease self-expires, so a REPL killed mid-turn drops out within one TTL
    instead of haunting the view forever."""
    _state_db(tmp_path / "profiles" / "sphoin-orch" / "state.db",
              [("dead", "local", NOW - 900, 9_000, 900, None, "sphoin", NOW - 900,
                "receiving stream response")],
              leases=[_turn("dead", NOW - 900, expires_at=NOW - 180)])   # expired 180 s ago
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert d["orchestrators"] == []


def test_a_worker_turn_never_renders_as_an_orchestrator(tmp_path):
    """A dispatched card holds a live lease too — that is what makes it a running
    card. Only interactive sources may show in this section. `oneshot` is a
    single non-interactive invocation, so it is excluded alongside the three the
    card named; `local` is excluded nowhere, because a v0.23 REPL turn's source
    measures `local` with `created_source='cli'`, NOT `cli`."""
    worker = (
        ("k-1", "kanban", 60),        # a running card's turn
        ("c-1", "cron", 120),         # a scheduled job
        ("s-1", "subagent", 30),      # a delegated child
        ("o-1", "oneshot", 10),       # `hermes -p X -q ...`, one query and done
        ("n-1", None, 15),           # a source the exclude list does not name
    )
    # (MON-23-FIELDS) `source` is `TEXT NOT NULL` in every live store (measured:
    # 7 223 session rows over 51 stores, `source IS NULL` = 0), so this store is
    # built with `nullable_source=True` — the row exists only to keep
    # `_LIVE_TURNS`' `s.source IS NULL` branch pinned, which a v0.23-shaped store
    # can no longer reach. The rule it guards is unchanged: an unknown source
    # renders rather than vanishing from the section.
    _state_db(tmp_path / "profiles" / "backend-engineer" / "state.db",
              [(cid, src, NOW - age, 1000, 10, None, "card", NOW - age,
                "receiving stream response") for cid, src, age in worker],
              leases=[_turn(cid, NOW - age) for cid, src, age in worker],
              nullable_source=True)
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert [r["session"] for r in d["orchestrators"]] == ["n-1"]


def test_two_live_turns_in_one_store_are_two_rows_oldest_turn_first(tmp_path):
    _state_db(tmp_path / "profiles" / "hscc-orch" / "state.db",
              [("a", "local", NOW - 900, 10, 1, None, "a", NOW - 10, "a"),
               ("b", "local", NOW - 3600, 20, 2, None, "b", NOW - 20, "b")],
              leases=[_turn("a", NOW - 900), _turn("b", NOW - 3600)])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert [r["session"] for r in d["orchestrators"]] == ["b", "a"]   # like running_table
    assert [r["turn_s"] for r in d["orchestrators"]] == [3600, 900]


def test_the_root_store_is_profile_default(tmp_path):
    _state_db(tmp_path / "state.db",
              [("r-1", "cli", NOW - 60, 5, 5, None, "root", NOW - 5, "thinking")],
              leases=[_turn("r-1", NOW - 60)])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert [(r["profile"], r["session"]) for r in d["orchestrators"]] == [("default", "r-1")]


def test_a_profile_dir_named_default_is_its_own_store(tmp_path):
    """`_state_db_path` maps the NAME `default` onto the root db, so resolving a
    scanned directory through it opened the root store twice and never opened
    `profiles/default/state.db` — a doubled row and a lost one, from one bug.
    This host really has a `profiles/default/` directory, so it is not academic."""
    _state_db(tmp_path / "state.db",
              [("root-sid", "cli", NOW - 60, 1, 1)], leases=[_turn("root-sid", NOW - 60)])
    _state_db(tmp_path / "profiles" / "default" / "state.db",
              [("dir-sid", "cli", NOW - 30, 2, 2)], leases=[_turn("dir-sid", NOW - 30)])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert sorted(r["session"] for r in d["orchestrators"]) == ["dir-sid", "root-sid"]


def test_orchestrators_survive_a_missing_unreadable_or_old_store(tmp_path):
    """The reported failure mode is a section that vanishes; one bad store must
    never take the good ones, or the view, down with it."""
    _state_db(tmp_path / "profiles" / "good-orch" / "state.db",
              [("g", "local", NOW - 60, 7, 7, None, "good", NOW - 10, "thinking")],
              leases=[_turn("g", NOW - 60)])
    (tmp_path / "profiles" / "no-db-orch").mkdir(parents=True)             # no state.db
    garbage = tmp_path / "profiles" / "garbage-orch"
    garbage.mkdir()
    (garbage / "state.db").write_bytes(b"not a database at all")           # unreadable
    _state_db(tmp_path / "profiles" / "old-orch" / "state.db",             # pre-v0.23
              [("x", "cli", NOW - 60, 3, 3)], lease_table=False)
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert [(r["profile"], r["session"]) for r in d["orchestrators"]] == [("good-orch", "g")]


def test_a_store_with_no_lease_table_at_all_is_skipped_without_a_crash(tmp_path):
    """Every store here predates `session_turn_leases`: the probe misses and the
    join never runs, so nothing raises out of the view."""
    _state_db(tmp_path / "profiles" / "ancient-orch" / "state.db",
              [("x", "cli", NOW - 60, 3, 3)], lease_table=False)
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert d["orchestrators"] == []


def test_orchestrators_state_is_cleaned_and_dashes_stay_honest(tmp_path):
    """`last_activity_description` is a db string; it can carry control chars."""
    _state_db(tmp_path / "profiles" / "ok-orch" / "state.db",
              [("s", "local", NOW - 60, 5, 5, None, "t", NOW - 1, "\x1b[2Jboom\x07")],
              leases=[_turn("s", NOW - 60)])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert d["orchestrators"][0]["state"] == "[2Jboom"


def test_a_profile_dir_that_is_not_a_plain_token_never_becomes_a_path(tmp_path):
    """The guard moved from argv to the directory scan: a name is only ever a
    path if it is a plain token naming an existing profiles/<name> directory."""
    _state_db(tmp_path / "state.db", [("s", "local", NOW - 60, 1, 1)],
              leases=[_turn("s", NOW - 60)])
    weird = tmp_path / "profiles" / ".. hidden .."
    _state_db(weird / "state.db",
              [("s", "local", NOW - 60, 1, 1, None, None, NOW, "leak")],
              leases=[_turn("s", NOW - 60)])
    d = _collect()
    kanban_live.attach_orchestrators(d, hermes_home=str(tmp_path), now=NOW)
    assert [r["profile"] for r in d["orchestrators"]] == ["default"]


def test_render_shows_orchestrators_and_7d_tokens():
    d = _collect()
    d["tokens"] = {"workers": {"24h": [400_000_000, 6_000_000], "7d": [2_000_000_000, 30_000_000]},
                   "orchestrators": {"24h": [20_000_000, 300_000], "7d": [90_000_000, 1_000_000],
                                     "since": NOW - 3600}}
    d["orchestrators"] = [{"profile": "sphoin-orch", "session": "local-41d4ee6a",
                           "turn_s": 323, "tok_in": 97_000_000, "tok_out": 990_000,
                           "state": "receiving stream response"}]
    buf = io.StringIO()
    make_console(file=buf, width=200, color_system=None).print(kanban_live.render(d))
    out = buf.getvalue()
    lines = out.splitlines()
    head = next(l for l in lines if l.startswith("Tokens"))
    assert head.split() == ["Tokens", "24h", "in", "24h", "out", "7d", "in", "7d", "out"]
    assert next(l for l in lines if l.startswith("workers")).split()[1:] == ["400.0M", "6.0M", "2.00G", "30.0M"]
    assert next(l for l in lines if l.startswith("orchestrators ")).split()[1:] == ["20.0M", "300k", "90.0M", "1.0M"]
    assert next(l for l in lines if l.startswith("total")).split()[1:] == ["420.0M", "6.3M", "2.09G", "31.0M"]
    assert "orchestrators (1 running)" in out
    orch = next(l for l in lines if l.startswith("sphoin-orch")).split()
    assert orch == ["sphoin-orch", "local-41d4ee6a", "5m", "97.0M/990k",
                    "receiving", "stream", "response"]
    assert "orchestrator history since" not in out


# ── MON-23-FIELDS: every printed field re-verified against the v0.23 schema ──
#
# Each case below is the pin for one row of the card's verdict table, and every
# constant it asserts against is a measurement from the operator's live boards
# (read-only `mode=ro` over 24 boards) or a file:line in the Hermes kernel, both
# quoted in the case. Fixtures use the real column sets declared at the top of
# this file, so `run_id`, `payload`, `outcome` and `ended_at` all exist.

def _ev(c, task_id, kind, at, run_id=None, payload=None):
    c.execute("INSERT INTO task_events (task_id,run_id,kind,payload,created_at) "
              "VALUES (?,?,?,?,?)", (task_id, run_id, kind, payload, at))


# ── 1. `scheduled` is a status the view must count ──────────────────────────

def _parked(c):
    """The shape `kanban_db.schedule_task` leaves behind (hermes_cli/kanban_db.py
    :3869-3890 — `SET status='scheduled'` plus a `scheduled` event). `scheduled` is
    a column of the default workflow (`hermes_cli/kanban_workflow.py`
    :113-124 `DEFAULT_WORKFLOW.columns`: triage, todo, scheduled, ready, running,
    blocked, review, done), so a card parked on a time or an external event was
    counted nowhere and a board whose only open cards were `scheduled` was skipped
    as idle by both `boards_table` and `render`'s `live_boards`."""
    _task(c, "t_park", title="parked until the release", assignee="qa",
          status="scheduled", created_at=NOW - 4000)
    _ev(c, "t_park", "scheduled", NOW - 3000,
        payload=json.dumps({"reason": "waiting on the release tag"}))


def test_scheduled_cards_are_counted_and_their_board_is_not_idle():
    d = _collect(kb=_FakeKb({"alpha": _alpha, "parked": _parked}))
    parked = next(b for b in d["boards"] if b["name"] == "parked")
    assert parked["counts"]["scheduled"] == 1
    assert "scheduled" in kanban_live.STATUS_COLS        # counted ...
    assert "archived" not in kanban_live.STATUS_COLS     # ... terminal states are not
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(
        kanban_live.render(d, snapshot=True))
    out = buf.getvalue()
    assert "Scheduled" in out, out          # the boards table has the column
    assert "parked" in out, out             # and a scheduled-only board is on screen


# ── 2. `ready` must be aged from the event that queued the card ─────────────

def _queue(c):
    """Three cards promoted in one second, then the bookkeeping v0.23 really
    writes: `commented` 4 510, `attached` 330, `edited` 104, `reprioritized` 41 and
    `heartbeat` 189 650 rows all-time on the live boards. Measured there: 607 tasks
    whose newest event is not their own `promoted`, the furthest 168 747 s after it.
    `t_requeue` came back to the queue 5 min ago — the kernel re-queues a reclaimed
    run with a `reclaimed` event carrying `retry_status` (kanban_db.py:2394-2420)."""
    for tid in ("t_quiet", "t_talked", "t_requeue"):
        _task(c, tid, title=f"waiting on a slot {tid}", assignee="qa",
              status="ready", priority=5, created_at=NOW - 3 * 86400)
        _ev(c, tid, "promoted", NOW - 6 * 3600)
    for kind, at in (("commented", NOW - 900), ("edited", NOW - 300),
                     ("attached", NOW - 60), ("heartbeat", NOW - 30),
                     ("reprioritized", NOW - 10)):
        _ev(c, "t_talked", kind, at)
    _ev(c, "t_requeue", "reclaimed", NOW - 300, run_id=77,
        payload=json.dumps({"retry_status": "ready"}))
    # a card created straight into `ready` has no queue event: created_at is the clock
    _task(c, "t_fresh", title="just created", assignee="qa", status="ready",
          created_at=NOW - 100)


def test_ready_waits_are_aged_from_the_event_that_queued_the_card():
    d = _collect(kb=_FakeKb({"queue": _queue}))
    waits = {r["id"]: r["wait_s"] for r in d["ready"]}
    # the comment/edit/attach/reprioritise/heartbeat storm did NOT reset t_talked
    assert waits == {"t_quiet": 6 * 3600, "t_talked": 6 * 3600,
                     "t_requeue": 300, "t_fresh": 100}, waits
    # and the panel's Waiting column shows the true age, not ~0
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(
        kanban_live.render(d, snapshot=True))
    assert "6h00m" in buf.getvalue()


# ── 3. the kernel's own alarms belong in the last-hour panel ────────────────

ALARMS = ("protocol_violation", "suspected_hallucinated_references",
          "completion_blocked_empty_result", "completion_blocked_hallucination",
          "changes_requested", "review_requested", "terminal_worker_reaped",
          "rate_limited")

NOISE = ("heartbeat", "commented", "edited", "attached", "respawn_guarded",
         "archived", "archive_worker_termination", "workspace_cleanup_deferred_shared",
         "claim_extended", "worker_result", "dependency_wait", "scheduled",
         "specified", "owner_admitted", "worker_bound")


def _alarms(c):
    """One card per alarm kind. Measured all-time over the live boards:
    protocol_violation 354, terminal_worker_reaped 206, review_requested 121,
    rate_limited 92, suspected_hallucinated_references 35, changes_requested 21,
    completion_blocked_empty_result 4, completion_blocked_hallucination 2 — every
    one of them invisible in this view before MON-23-FIELDS (measured live: 3
    `terminal_worker_reaped` in the last hour, and the panel showed only the
    `completed` of the same three cards)."""
    for i, kind in enumerate(ALARMS):
        _task(c, f"t_{kind}", title=kind, assignee="qa", status="blocked",
              created_at=NOW - 4000)
        _ev(c, f"t_{kind}", kind, NOW - 600 - i)
    _task(c, "t_noise", title="volume, not outcomes", assignee="qa",
          status="ready", created_at=NOW - 4000)
    for i, kind in enumerate(NOISE):
        _ev(c, "t_noise", kind, NOW - 500 - i)


def test_the_kernel_alarms_reach_the_panel_and_the_volume_kinds_do_not():
    d = _collect(kb=_FakeKb({"alarms": _alarms}))
    kinds = {e["kind"] for e in d["events"]}
    assert set(ALARMS) <= kinds, sorted(kinds)
    assert kinds == set(ALARMS), sorted(kinds)      # nothing else leaks in
    assert not set(NOISE) & kinds
    # every kind the panel can print has a colour of its own
    assert set(kanban_live.OUTCOME_KINDS) <= set(kanban_live._EVENT_STYLE)
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(
        kanban_live.render(d, snapshot=True))
    out = buf.getvalue()
    assert "terminal_worker_reaped" in out and "protocol_violation" in out
    assert "suspected_hallucinated_references" in out


# ── 4. Beat: the column, the bar, and an unknown beat ───────────────────────

def _beat(hb_age=None, hb_task=None, started_age=1200):
    def build(c):
        started = None if started_age is None else NOW - started_age
        _run(c, 900, "t_beat", started_at=started,
             last_heartbeat_at=None if hb_age is None else NOW - hb_age)
        _task(c, "t_beat", title="beat me", status="running",
              created_at=NOW - 60, current_run_id=900,
              last_heartbeat_at=None if hb_task is None else NOW - hb_task)
    return build


def _running_line(row):
    buf = io.StringIO()
    make_console(file=buf, width=120, color_system=None).print(
        kanban_live.running_table([row]))
    return next(l for l in buf.getvalue().splitlines() if row["id"] in l)


def test_beat_is_task_runs_last_heartbeat_at_and_600s_is_the_stale_bar():
    """`task_runs.last_heartbeat_at` is still the column the kernel writes: live
    running runs were 0-60 s old when measured, and `task_events.kind='heartbeat'`
    gaps over 24 h were n=1102, p50 60 s, p95 67 s, max 102 s, ZERO above 600 s.
    So 600 s stays the bar — about ten missed beats — and this is where it is
    pinned, against a v0.23-shaped `task_runs` row instead of nowhere."""
    (row,) = _collect(kb=_FakeKb({"alpha": _beat(hb_age=47)}))["running"]
    assert (row["hb_s"], row["run_s"]) == (47, 1200)
    assert "47s" in _running_line(row)
    assert kanban_live.beat_style(row["hb_s"]) == "dim"

    (row,) = _collect(kb=_FakeKb({"alpha": _beat(
        hb_age=kanban_live.STALE_HEARTBEAT_S + 100)}))["running"]
    assert row["hb_s"] == kanban_live.STALE_HEARTBEAT_S + 100
    assert "11m" in _running_line(row)
    assert kanban_live.beat_style(row["hb_s"]) == "error"
    # the bar is exclusive: a beat exactly at it is still "dim"
    assert kanban_live.beat_style(kanban_live.STALE_HEARTBEAT_S) == "dim"

    # a task-level beat still counts when the run row has none
    (row,) = _collect(kb=_FakeKb({"alpha": _beat(hb_task=90)}))["running"]
    assert row["hb_s"] == 90 and kanban_live.beat_style(row["hb_s"]) == "dim"
    # and a row with NO beat and no start is unknown, not healthy
    (row,) = _collect(kb=_FakeKb({"alpha": _beat(started_age=None)}))["running"]
    assert (row["hb_s"], row["run_s"]) == (None, None)
    assert kanban_live.beat_style(row["hb_s"]) == "warn"


# ── 5. a running card whose run row is gone must still render ───────────────

def test_a_running_card_with_no_run_row_still_renders():
    """`tasks.current_run_id` naming a `task_runs` row that is not there (spawn
    failed, reclaimed mid-flight) must not delete the card from the Running table.
    The `LEFT JOIN` is what keeps it; measured over 24 live boards there was no
    dangling id today, so this pins the construction, not a live instance."""
    def build(c):
        _task(c, "t_ghost", title="spawn failed", assignee="qa", status="running",
              created_at=NOW - 300, current_run_id=999)

    d = _collect(kb=_FakeKb({"ghost": build}))
    (row,) = d["running"]
    assert row["worker"] == "qa"               # falls back to the assignee
    assert (row["run_s"], row["hb_s"]) == (None, None)
    line = _running_line(row)
    assert "t_ghost" in line and "spawn failed" in line, line   # the card is on screen
    assert kanban_live.beat_style(row["hb_s"]) == "warn"        # and not styled healthy


# ── 6. `Done 24h` must survive the card being archived ──────────────────────

def _archived_done(c):
    _task(c, "t_arch", title="finished then archived", assignee="qa",
          status="archived", created_at=NOW - 90000, completed_at=NOW - 600)


def _archived_never_done(c):
    _task(c, "t_no", title="archived unfinished", assignee="qa", status="archived",
          created_at=NOW - 90000, completed_at=None)


def test_done_24h_counts_a_completion_that_was_archived():
    """The archive path keeps `completed_at` (measured: 54 archived rows across the
    live boards still carry it — hscc 25 of 110, ecofire-app 10 of 24, flightdeck
    5 of 19), so a card that finished today and was archived today used to leave
    the `Done 24h` column, and could take its board out of the view with it. A card
    that never completed (measured: 212 archived rows with completed_at NULL) stays
    out."""
    d = _collect(kb=_FakeKb({"arch": _archived_done, "never": _archived_never_done}))
    assert next(b for b in d["boards"] if b["name"] == "arch")["done_24h"] == 1
    assert next(b for b in d["boards"] if b["name"] == "never")["done_24h"] == 0
    buf = io.StringIO()
    make_console(file=buf, width=160, color_system=None).print(
        kanban_live.render(d, snapshot=True))
    out = buf.getvalue()
    assert "arch" in out and "never" not in out, out


# ── 7. the outcome lookup must not be the panel's 12-row slice ──────────────

def _many(c):
    """16 cards finishing in the window: the panel keeps the newest
    `RECENT_LIMIT` rows across every board, `outcomes` must answer for all 16,
    newest kind per card."""
    for i in range(15):
        _task(c, f"t_{i:02d}", title=f"card {i}", assignee="qa", status="done",
              created_at=NOW - 9000, completed_at=NOW - 600 + i)
        _ev(c, f"t_{i:02d}", "completed", NOW - 600 + i)
    _task(c, "t_both", title="reaped then completed", assignee="qa", status="done",
          created_at=NOW - 9000, completed_at=NOW - 60)
    _ev(c, "t_both", "completed", NOW - 700)
    _ev(c, "t_both", "terminal_worker_reaped", NOW - 60)


def test_outcomes_answer_for_every_card_not_just_the_panels_slice():
    d = _collect(kb=_FakeKb({"many": _many}))
    assert len(d["events"]) == kanban_live.RECENT_LIMIT      # the panel is capped
    assert len(d["outcomes"]["many"]) == 16                   # the lookup is not
    assert d["outcomes"]["many"]["t_both"] == "terminal_worker_reaped"
    assert d["outcomes"]["many"]["t_00"] == "completed"
    # board-keyed, string keys: `hscc kanban monitor --json` must still serialise
    assert json.loads(json.dumps(d))["outcomes"]["many"]["t_01"] == "completed"

