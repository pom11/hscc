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

    Implements just ``get_session(session_id)`` (an established CLI session
    has a row on its own profile). Returns the whole row dict as hermes does.
    """

    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self.closed = False

    def get_session(self, session_id):
        row = self._rows.get(session_id)
        return dict(row) if row else None

    def close(self):
        self.closed = True


class FakeOrchProvider:
    """A ``_session_db`` seam value: an opener returning a shared orch fake."""

    def __init__(self, rows=()):
        self._db = FakeOrchDB(rows)

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
