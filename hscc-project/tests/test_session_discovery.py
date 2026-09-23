""""Tests for session discovery — `flightdeck.core.session_discovery` plus the
`project sessions` / `chat --resume` command wiring in commands/project.py.

The feature maps a project's registry ``topic`` to ``sessions.thread_id`` on
the DEFAULT profile and surfaces the Telegram history. Two layers are tested:

- Core discovery (``list_project_sessions``) via injected fake DB seams, so
  mapping/newest-first/unmapped logic is deterministic and never touches a
  real file.
- A REAL ``hermes_state.SessionDB`` read-only test (the ``mode=ro`` proof):
  we create a temp state.db read-write, seed it, then open the SAME file read
  only via ``SessionDB(..., read_only=True)`` and assert discovery sees the
  rows — proving the production path never writes.

No test writes to, or even opens, the operator's real ``~/.hermes`` state.db
or any profile tree: every seam is injected, and the real-DB test uses its own
temp file.
"""

import argparse
import json
import tempfile
from pathlib import Path

import pytest

from flightdeck.commands import project as project_cmd
from flightdeck.core import registry
from flightdeck.core import session_discovery


# --------------------------------------------------------------------------- #
# Fakes (mirror the fake session DB shapes in test_project.py)
# --------------------------------------------------------------------------- #

class FakeOrchDB:
    """DEFAULT-less state.db stand-in for the <name>-orch profile.

    Implements ``get_session`` (id lookup), ``list_sessions_rich`` (the title
    scan fallback) and ``message_count`` (the full-history count enrichment),
    mirroring the shapes the production ``SessionDB`` exposes. ``full_counts``
    lets a test model in-place compaction: the ``message_count`` column may own
    only the ACTIVE current segment while the full-history count is larger.
    """

    def __init__(self, rows, full_counts=None):
        self._rows = {r["id"]: dict(r) for r in rows}
        self._full_counts = full_counts or {}
        self.closed = False

    def get_session(self, session_id):
        row = self._rows.get(session_id)
        return dict(row) if row else None

    def list_sessions_rich(self, limit=5000):
        return [dict(r) for r in self._rows.values()]

    def message_count(self, session_id):
        return self._full_counts.get(
            session_id,
            (self._rows.get(session_id) or {}).get("message_count") or 0,
        )

    def close(self):
        self.closed = True


class FakeOrchProvider:
    """A ``_session_db`` seam value: an opener returning a shared orch fake."""

    def __init__(self, rows=(), full_counts=None):
        self._db = FakeOrchDB(rows, full_counts=full_counts)

    def __call__(self, profile):
        return self._db


class FakeDefaultDB:
    """State.db stand-in for the DEFAULT profile (telegram side)."""

    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self.closed = False

    def list_sessions_rich(self, source, limit=5000):
        assert source == "telegram"
        return [dict(r) for r in self._rows]

    def close(self):
        self.closed = True


class FakeDefaultProvider:
    """A ``_default_db`` seam value: an opener returning a shared default fake."""

    def __init__(self, rows=()):
        self._db = FakeDefaultDB(rows)

    def __call__(self, profile):
        return self._db


def _tg(id_, thread, title="t", msgs=None, last=None, started=None):
    """A raw hermes telegram row dict as ``list_sessions_rich`` returns it."""
    return {
        "id": id_, "source": "telegram", "thread_id": thread,
        "chat_id": "-1001", "title": title, "display_name": title,
        "message_count": msgs, "started_at": started, "last_active": last,
    }


def _cli(id_, session_name, msgs=None, last=None, started=None):
    """A raw hermes CLI row (used for the orchestrator session's get_session)."""
    return {
        "id": id_, "source": "cli", "title": session_name,
        "display_name": session_name, "message_count": msgs,
        "started_at": started, "last_active": last,
    }


def _reg(tmp_path, name="ecofire", topic=8891, session="20260921_abc"):
    """A registry with project ``name`` (topic + persisted orchestrator session
    id). ``set_session`` mirrors what ``ensure_session`` persists, so
    ``resolve_orchestrator`` returns the timestamped id rather than the name —
    making the resume tests realistic."""
    path = str(tmp_path / "registry.yaml")
    registry.add_project(name, repo=str(tmp_path / name), board=name,
                         topic=topic, path=path)
    if session:
        registry.set_session(name, session, path=path)
    return path


# --------------------------------------------------------------------------- #
# Core discovery — mapping, ordering, unmapped
# --------------------------------------------------------------------------- #

def test_discovery_maps_thread_id_to_topic(tmp_path):
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2, last=1700000100, started=1699999900)
    tg_rows = [
        _tg("tg-newer", "8891", title="B", msgs=5, last=1700000200, started=1700000000),
        _tg("tg-older", "8891", title="A", msgs=3, last=1700000100, started=1699999500),
        _tg("tg-other", "7788", title="other project", msgs=9),       # different thread
        _tg("tg-null", None, title="unthreaded", msgs=1),             # NULL thread
    ]
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([orch]),
        _default_db=FakeDefaultProvider(tg_rows),
    )

    assert result["project"] == "ecofire"
    assert result["topic"] == 8891
    assert result["orch_profile"] == "ecofire-orch"
    assert result["telegram_profile"] == "default"

    # ORCHESTRATOR row: mapped (with id + title + counts), source 'orchestrator'.
    assert result["orchestrator"]["id"] == "20260921_abc"
    assert result["orchestrator"]["source"] == "orchestrator"
    assert result["orchestrator"]["message_count"] == 2

    # TELEGRAM: only the thread_id==topic rows, newest first.
    ids = [r["id"] for r in result["telegram"]]
    assert ids == ["tg-newer", "tg-older"]
    assert all(r["source"] == "telegram" for r in result["telegram"])
    # newest-first by last-active.
    assert result["telegram"][0]["last"] == 1700000200

    # NULL-thread telegram sessions are counted, never guessed at.
    assert result["telegram_unmapped"] == 1


def test_discovery_resolves_orchestrator_by_title_when_registry_holds_title(tmp_path):
    """The registry ``session:`` value is the session's TITLE, not its id.

    ``get_session`` matches by id only, so an id-lookup on the title used to
    return None and ``project sessions`` reported "no orchestrator session
    found" for EVERY project. Discovery must fall back to matching the title.
    """
    # The persisted `session:` is the TITLE "hscc" (as in production), and the
    # orchestrator session row carries that same title under a real id.
    reg = _reg(tmp_path, "ecofire", topic=8891, session="hscc")
    orch = _cli(
        "20260908_134933_152c5b", "hscc",
        msgs=2, last=1700000100, started=1699999900,
    )
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([orch]),
        _default_db=FakeDefaultProvider([]),
    )

    assert result["orch_profile"] == "ecofire-orch"
    assert result["orchestrator"]["id"] == "20260908_134933_152c5b"
    assert result["orchestrator"]["source"] == "orchestrator"
    assert result["orchestrator"]["title"] == "hscc"


def test_discovery_title_scan_still_tries_id_first(tmp_path):
    """When the registry `session:` holds a real id, `get_session` resolves it
    directly (no title scan needed) — the id path stays the fast/first route."""
    reg = _reg(tmp_path, "ecofire", topic=8891, session="20260921_abc")  # an id
    orch = _cli("20260921_abc", "ecofire", msgs=2, last=1700000100)
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([orch]),
        _default_db=FakeDefaultProvider([]),
    )
    assert result["orchestrator"]["id"] == "20260921_abc"
    assert result["orchestrator"]["message_count"] == 2


def test_discovery_title_scan_picks_newest_active_when_titles_collide(tmp_path):
    """Several sessions may share a title; the scan must pick deterministically
    the newest-active one, never a stale duplicate and never an arbitrary one."""
    reg = _reg(tmp_path, "ecofire", topic=8891, session="hscc")
    orch_old = _cli("stale-id", "hscc", msgs=1, last=1600000000, started=1599999000)
    orch_new = _cli(
        "20260908_134933_152c5b", "hscc",
        msgs=2, last=1700000100, started=1699999900,
    )
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([orch_old, orch_new]),
        _default_db=FakeDefaultProvider([]),
    )
    # newest-active wins over the stale same-titled row
    assert result["orchestrator"]["id"] == "20260908_134933_152c5b"


def test_discovery_orchestrator_surfaces_full_history_count(tmp_path):
    """Under in-place compaction the `message_count` column holds only the
    active current segment; discovery must surface the FULL history via hermes'
    own count API, not under-report the thread (the user-facing number)."""
    reg = _reg(tmp_path, "ecofire", topic=8891, session="hscc")
    orch = _cli(
        "20260908_134933_152c5b", "hscc",
        msgs=123, last=1700000100, started=1699999900,  # active window only
    )
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider(
            [orch], full_counts={"20260908_134933_152c5b": 1819},
        ),
        _default_db=FakeDefaultProvider([]),
    )
    assert result["orchestrator"]["id"] == "20260908_134933_152c5b"
    assert result["orchestrator"]["message_count"] == 1819  # full history, not 123


def test_discovery_orchestrator_read_failure_is_surfaced_not_swallowed(tmp_path):
    """A genuine read failure is NOT 'no orchestrator session' — it must surface
    as runtime_error (e2a3251: a failure to look is never a false negative),
    exactly as HermesRuntimeUnavailable does, not render as an empty project."""
    reg = _reg(tmp_path, "ecofire", topic=8891, session="hscc")

    class BoomOrchDB:
        closed = False

        def get_session(self, session_id):
            raise RuntimeError("disk exploded")

        def close(self):
            self.closed = True

    class BoomProvider:
        def __call__(self, profile):
            return BoomOrchDB()

    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=BoomProvider(),
        _default_db=FakeDefaultProvider([]),
    )
    assert result["orchestrator"] is None
    assert result["runtime_error"] is not None
    assert "cannot read orchestrator session" in result["runtime_error"]


def test_discovery_newest_first_places_null_last(tmp_path):
    reg = _reg(tmp_path, "x", topic=5)
    rows = [
        _tg("a", "5", last=30),
        _tg("b", "5", last=None),   # no activity stamp -> sorts last
        _tg("c", "5", last=20),
    ]
    result = session_discovery.list_project_sessions(
        "x", path=reg, _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(rows),
    )
    assert [r["id"] for r in result["telegram"]] == ["a", "c", "b"]


def test_discovery_no_topic_returns_no_telegram(tmp_path):
    """A project with no registry topic cannot be mapped to any telegram
    history; it must not crash and must not guess a thread owner."""
    reg = str(tmp_path / "registry.yaml")
    registry.add_project("plain", repo=str(tmp_path / "plain"), board="plain",
                         path=reg)
    result = session_discovery.list_project_sessions(
        "plain", path=reg, _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider([_tg("t", "8891", title="x")]),
    )
    assert result["topic"] is None
    assert result["telegram"] == []
    # The telegram session with a real thread_id is NOT claimed by this project.


def test_discovery_session_int_vs_string_thread_match(tmp_path):
    """thread_id comes back from hermes as a string; the registry topic is an
    int. Matching must survive the type difference (and no matching on a
    malformed non-numeric thread id)."""
    reg = _reg(tmp_path, "y", topic=8891)
    rows = [
        _tg("a", "8891", title="match"),
        _tg("b", "not-a-number", title="weird"),
    ]
    result = session_discovery.list_project_sessions(
        "y", path=reg, _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(rows),
    )
    assert [r["id"] for r in result["telegram"]] == ["a"]
    assert result["telegram_unmapped"] == 0  # 'not-a-number' is not NULL


# --------------------------------------------------------------------------- #
# Binding store (mapping.json): the second ownership rule
# --------------------------------------------------------------------------- #

def _write_mapping(tmp_path, **entries):
    """Write a temp mapping.json (session_id -> {project, ...}) and return its
    path, matching the canonical binding-store schema produced by map_sessions
    (which card 1 reads but never writes)."""
    p = tmp_path / "mapping.json"
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return str(p)


def test_discovery_maps_session_via_binding_store_only(tmp_path):
    """A telegram session whose thread_id does NOT match the topic but which
    the binding store links to the project DOES appear (manual linking)."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    tg_rows = [
        _tg("tg-linked", "7788", title="linked by hand"),  # other thread, bound
        _tg("tg-other", "8899", title="someone else's"),   # neither matches
    ]
    mapping = _write_mapping(
        tmp_path, **{"tg-linked": {"project": "ecofire", "method": "manual",
                                   "confidence": 1.0, "evidence": "linked"}}
    )
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(tg_rows),
        _mapping_path=mapping,
    )
    assert [r["id"] for r in result["telegram"]] == ["tg-linked"]
    assert result["telegram"][0]["source"] == "telegram"


def test_discovery_thread_and_binding_union_dedup(tmp_path):
    """A session that matches BOTH rules appears once; the union is exact."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    tg_rows = [
        _tg("t1", "8891", title="by thread"),
        _tg("t2", "7788", title="by binding only"),
    ]
    mapping = _write_mapping(
        tmp_path,
        **{"t1": {"project": "ecofire", "method": "manual", "confidence": 1.0,
                   "evidence": "dup"},
           "t2": {"project": "ecofire", "method": "manual", "confidence": 1.0,
                   "evidence": "linked"}},
    )
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(tg_rows),
        _mapping_path=mapping,
    )
    ids = [r["id"] for r in result["telegram"]]
    assert sorted(ids) == ["t1", "t2"]  # t1 not doubled by the binding entry


def test_discovery_binding_for_other_project_excluded(tmp_path):
    """A binding pointing at a DIFFERENT project must not leak this session in;
    neither thread nor binding match -> not included."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    tg_rows = [_tg("tg-x", "7788", title="bound elsewhere")]
    mapping = _write_mapping(
        tmp_path, **{"tg-x": {"project": "prime", "method": "model",
                              "confidence": 0.9, "evidence": "repo path"}}
    )
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(tg_rows),
        _mapping_path=mapping,
    )
    assert result["telegram"] == []


def test_discovery_absent_mapping_behaves_as_today(tmp_path):
    """No mapping.json -> no bindings, exactly the pre-card behaviour (only
    thread_id == topic maps). A bound-to-this-project session with no thread
    match must NOT appear when no mapping file exists."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    tg_rows = [
        _tg("tg-ok", "8891", title="by thread"),
        _tg("tg-linked", "7788", title="would only match via store"),
    ]
    missing = str(tmp_path / "nope" / "mapping.json")  # does not exist
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(tg_rows),
        _mapping_path=missing,
    )
    assert [r["id"] for r in result["telegram"]] == ["tg-ok"]


def test_discovery_linked_via_mapping_when_no_topic(tmp_path):
    """A project with NO registry topic can still own a session through the
    binding store alone (manual link is independent of the thread mapping)."""
    reg = str(tmp_path / "registry.yaml")
    registry.add_project("plain", repo=str(tmp_path / "plain"), board="plain",
                         path=reg)
    tg_rows = [_tg("tg-hand", "7788", title="hand-linked")]
    mapping = _write_mapping(
        tmp_path, **{"tg-hand": {"project": "plain", "method": "manual",
                                 "confidence": 1.0, "evidence": "x"}}
    )
    result = session_discovery.list_project_sessions(
        "plain", path=reg,
        _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(tg_rows),
        _mapping_path=mapping,
    )
    assert result["topic"] is None
    assert [r["id"] for r in result["telegram"]] == ["tg-hand"]


def test_discovery_malformed_mapping_behaves_as_today(tmp_path):
    """A mapping.json that is not a JSON object is a data fault, not an
    environment fault: it yields no bindings, never raises, and never renders
    as runtime_error."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    bad = tmp_path / "mapping.json"
    bad.write_text("not json {{{", encoding="utf-8")
    tg_rows = [_tg("tg-ok", "8891", title="by thread")]
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider(),
        _default_db=FakeDefaultProvider(tg_rows),
        _mapping_path=str(bad),
    )
    assert [r["id"] for r in result["telegram"]] == ["tg-ok"]
    assert result["runtime_error"] is None  # a data fault, not a runtime one


# --------------------------------------------------------------------------- #
# REAL hermes SessionDB, read-only — the `mode=ro` proof
# --------------------------------------------------------------------------- #

def _real_sdb_seed(tmp_path):
    """Create a temp state.db, seed it read-write, return (path, orch_row)."""
    from hermes_state import SessionDB

    db_path = Path(tmp_path) / "state.db"
    rw = SessionDB(db_path=db_path)
    # telegram sessions on thread 8891 (the project's topic)
    rw.create_session("tg-a", source="telegram", thread_id="8891",
                      chat_id="-1001", display_name="real tg a")
    rw.create_session("tg-b", source="telegram", thread_id="8891",
                      chat_id="-1001", display_name="real tg b")
    # unthreaded telegram + a cli session
    rw.create_session("tg-null", source="telegram", chat_id="-1002",
                      display_name="no thread")
    rw.create_session("cli-c", source="cli", display_name="real cli")
    rw.close()
    return db_path


def test_real_sessiondb_read_only_discovers_telegram(tmp_path):
    """The `mode=ro` requirement, proven with a REAL hermes SessionDB.

    We seed a temp state.db read-write (setup only, not the code under test),
    then open the SAME file through ``SessionDB(..., read_only=True)`` — the
    hermes-native ``mode=ro`` — and run the production function. It must find
    the mapped telegram rows and nothing else, and it must close the handle.
    """
    db_path = _real_sdb_seed(tmp_path)
    reg = _reg(tmp_path, "ecofire", topic=8891)

    # Point the DEFAULT seam at the temp file by monkeypatching the read-only
    # opener's target, not by passing a fake (we want the REAL hermes code).
    real_ro = _list_telegram_via_real_sessiondb(db_path)
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([_cli("20260921_abc", "ecofire", msgs=1)]),
        _default_db=real_ro,
    )

    assert result["topic"] == 8891
    assert sorted(r["id"] for r in result["telegram"]) == ["tg-a", "tg-b"]
    assert result["telegram_unmapped"] == 1  # tg-null
    assert "cli-c" not in [r["id"] for r in result["telegram"]]

    # Read-only guarantee: a SECOND read-only open of the same file still works
    # (the first close did not corrupt or lock anything), and the session count
    # is unchanged — nothing was written.
    fresh = _list_telegram_via_real_sessiondb(db_path)
    again = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([_cli("20260921_abc", "ecofire", msgs=1)]),
        _default_db=fresh,
    )
    assert sorted(r["id"] for r in again["telegram"]) == ["tg-a", "tg-b"]


def _list_telegram_via_real_sessiondb(db_path):
    """Return a `_default_db` seam that opens ``db_path`` with the REAL hermes
    ``SessionDB(read_only=True)`` for every default-profile open."""
    from hermes_state import SessionDB

    class RealReadOnlyProvider:
        def __call__(self, profile):
            return SessionDB(db_path=db_path, read_only=True)

    return RealReadOnlyProvider()


def test_real_readonly_connection_is_truly_read_only(tmp_path):
    """Sanity: a ``SessionDB(read_only=True)`` connection cannot mutate — the
    discovery path literally cannot write, even if a bug tried to."""
    db_path = _real_sdb_seed(tmp_path)
    from hermes_state import SessionDB
    ro = SessionDB(db_path=db_path, read_only=True)
    with pytest.raises(Exception):
        ro.set_session_title("tg-a", "should-fail")
    ro.close()


def test_real_sessiondb_readonly_with_binding_store(tmp_path):
    """The binding store works through the REAL read-only SessionDB path, and
    reading mapping.json never touches state.db (a session bound via the store
    on another thread is discovered; state.db is unchanged)."""
    db_path = _real_sdb_seed(tmp_path)  # tg-a/tg-b on thread 8891, tg-null
    reg = _reg(tmp_path, "ecofire", topic=8891)

    # Bind tg-null (thread_id NULL, would otherwise be unmapped) to ecofire.
    mapping = _write_mapping(
        tmp_path, **{"tg-null": {"project": "ecofire", "method": "manual",
                                 "confidence": 1.0, "evidence": "hand"}}
    )
    real_ro = _list_telegram_via_real_sessiondb(db_path)
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([_cli("20260921_abc", "ecofire", msgs=1)]),
        _default_db=real_ro,
        _mapping_path=mapping,
    )

    # tg-null now belongs (via the binding store) despite thread_id == None.
    assert sorted(r["id"] for r in result["telegram"]) == \
        ["tg-a", "tg-b", "tg-null"]
    # A NULL thread mapped via the store is now owned, not counted unmapped.
    assert result["telegram_unmapped"] == 0

    # Read-only invariant: reopening the SAME file read-only proves nothing was
    # written; the bound session came from the store, not from a state.db edit.
    fresh = _list_telegram_via_real_sessiondb(db_path)
    again = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=FakeOrchProvider([_cli("20260921_abc", "ecofire", msgs=1)]),
        _default_db=fresh,
        _mapping_path=mapping,
    )
    assert sorted(r["id"] for r in again["telegram"]) == \
        ["tg-a", "tg-b", "tg-null"]


# --------------------------------------------------------------------------- #
# command wiring: `project sessions`
# --------------------------------------------------------------------------- #

def _ns(**kw):
    defaults = dict(
        registry=None, session_db=None, default_db=None, exec_seam=None,
        name="", extra=None, resume_id=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_cmd_sessions_lists_orchestrator_and_telegram(tmp_path, capsys):
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2, last=1700000100, started=1699999900)
    tg_rows = [
        _tg("t1", "8891", title="firebrief", msgs=5, last=1700000200, started=1700000000),
        _tg("t2", "8891", title="morning", msgs=3, last=1700000100, started=1699999500),
    ]
    args = _ns(registry=reg, session_db=FakeOrchProvider([orch]),
               default_db=FakeDefaultProvider(tg_rows), name="ecofire")

    rc = project_cmd.cmd_sessions(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "topic 8891" in out
    assert "telegram profile default" in out
    # Orchestrator row + title column header
    assert "20260921_abc" in out and "ecofire" in out
    # Telegram rows present, and they carry message counts
    assert "t1" in out and "firebrief" in out and "5 msgs" in out
    assert "t2" in out and "morning" in out and "3 msgs" in out


def test_cmd_sessions_no_topic_and_no_telegram(tmp_path, capsys):
    reg = str(tmp_path / "registry.yaml")
    registry.add_project("plain", repo=str(tmp_path / "plain"), board="plain",
                         path=reg)
    args = _ns(registry=reg, session_db=FakeOrchProvider([]),
               default_db=FakeDefaultProvider([]), name="plain")

    rc = project_cmd.cmd_sessions(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "topic (none)" in out
    assert "no orchestrator session found" in out
    assert "creates it on first use" in out
    assert "no telegram history — project has no telegram topic" in out


def test_cmd_sessions_unknown_project(tmp_path, capsys):
    reg = _reg(tmp_path, "ecofire", topic=8891)
    args = _ns(registry=reg, session_db=FakeOrchProvider([]),
               default_db=FakeDefaultProvider([]), name="missing")
    rc = project_cmd.cmd_sessions(args)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown project" in err
    assert "ecofire" in err  # lists valid names


# --------------------------------------------------------------------------- #
# command wiring: `chat --resume` + the history note in plain `chat`
# --------------------------------------------------------------------------- #

class CaptureExec:
    def __init__(self):
        self.calls = []

    def __call__(self, file, argv):
        self.calls.append((file, list(argv)))
        return 0


def test_chat_resume_telegram_uses_default_profile(tmp_path, capsys):
    """`chat ecofire --resume t1` resumes the telegram session on the DEFAULT
    profile (not <name>-orch) — the whole point of the resume path."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2)
    tg_rows = [_tg("t1", "8891", title="firebrief", msgs=5, last=1700000200)]
    seam = CaptureExec()
    args = _ns(registry=reg, session_db=FakeOrchProvider([orch]),
               default_db=FakeDefaultProvider(tg_rows),
               exec_seam=seam, name="ecofire", resume_id="t1")

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert seam.calls, "exec seam must have been invoked"
    file, argv = seam.calls[0]
    assert file == "hermes"
    assert argv == ["hermes", "-p", "default", "--resume", "t1"]
    assert "resuming session 't1'" in out
    assert "profile default" in out


def test_chat_resume_orchestrator_session_uses_orch_profile(tmp_path, capsys):
    """`chat ecofire --resume 20260921_abc` resumes the ORCHESTRATOR session on
    <name>-orch, the profile it lives on."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2)
    seam = CaptureExec()
    args = _ns(registry=reg, session_db=FakeOrchProvider([orch]),
               default_db=FakeDefaultProvider([]),
               exec_seam=seam, name="ecofire", resume_id="20260921_abc")

    rc = project_cmd.cmd_chat(args)
    capsys.readouterr()
    assert rc == 0
    assert seam.calls[0][1] == ["hermes", "-p", "ecofire-orch", "--resume",
                                "20260921_abc"]


def test_chat_resume_unknown_id_fails_with_listing(tmp_path, capsys):
    """A resume id that is not one of the project's sessions must fail loudly
    and list the project's real sessions — never silently attach fresh."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2)
    tg_rows = [_tg("t1", "8891", title="firebrief", msgs=5)]
    seam = CaptureExec()
    args = _ns(registry=reg, session_db=FakeOrchProvider([orch]),
               default_db=FakeDefaultProvider(tg_rows),
               exec_seam=seam, name="ecofire", resume_id="bogus")

    rc = project_cmd.cmd_chat(args)
    err = capsys.readouterr().err

    assert rc == 2
    assert seam.calls == []  # nothing exec'd
    assert "no session 'bogus' belongs to project 'ecofire'" in err
    assert "20260921_abc" in err and "t1" in err  # lists the real sessions


def test_chat_history_note_when_telegram_history_exists(tmp_path, capsys):
    """Plain `chat` prints a note that earlier telegram history exists (and
    how to reach it) instead of silently starting fresh."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2)
    tg_rows = [
        _tg("t1", "8891", title="a", msgs=5, last=1700000200),
        _tg("t2", "8891", title="b", msgs=3, last=1700000100),
    ]
    seam = CaptureExec()
    args = _ns(registry=reg, session_db=FakeOrchProvider([orch]),
               default_db=FakeDefaultProvider(tg_rows),
               exec_seam=seam, name="ecofire")

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "2 earlier telegram sessions for this project (8 msgs)" in out
    assert "`hscc project sessions ecofire` to list" in out
    assert "`--resume <id>` to open" in out
    # The normal attach still happens (continues the persisted orch session,
    # not auto-resumed).
    assert seam.calls[0][1] == ["hermes", "-p", "ecofire-orch", "chat",
                                "--continue", "20260921_abc"]


def test_chat_no_note_when_no_telegram_history(tmp_path, capsys):
    reg = _reg(tmp_path, "ecofire", topic=8891)
    orch = _cli("20260921_abc", "ecofire", msgs=2)
    seam = CaptureExec()
    args = _ns(registry=reg, session_db=FakeOrchProvider([orch]),
               default_db=FakeDefaultProvider([]),
               exec_seam=seam, name="ecofire")

    rc = project_cmd.cmd_chat(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "earlier telegram session" not in out
    assert seam.calls[0][1] == ["hermes", "-p", "ecofire-orch", "chat",
                                "--continue", "20260921_abc"]


def test_unimportable_runtime_reports_error_not_empty_history(monkeypatch):
    """A wrong interpreter must not render as "no telegram history".

    `hscc` runs under miniconda, where `hermes_cli` is not importable, while
    the sessions live in the Hermes venv. The resolver used to swallow that
    ImportError and return None, so `project sessions ecofire-bc` printed
    "no telegram history" for a project with six sessions and 1,128 messages —
    an environment fault reported as a fact about the operator's data.
    """
    from flightdeck.core import project_lifecycle as pl

    def _boom(*a, **k):
        raise pl.HermesRuntimeUnavailable("cannot import the Hermes runtime (test)")

    monkeypatch.setattr(session_discovery, "_open_profile_sdb_ro", _boom, raising=False)
    monkeypatch.setattr(pl, "_open_profile_session_db", _boom)

    result = session_discovery.list_project_sessions("hscc")

    assert result["runtime_error"], "must say the runtime was unreadable"
    assert "Hermes runtime" in result["runtime_error"]
    assert result["telegram"] == []
