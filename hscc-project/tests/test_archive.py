"""Tests for flightdeck.core.archive + flightdeck.commands.archive.

The exporter reads a **test-built** state.db (mirroring the real schema's
columns) read-only and writes Markdown under a pytest tmp_path. No test touches
the operator's real ~/.hermes/state.db, a live network, or the cluster.
Registry files are written to tmp_path, never ~/.flightdeck.
"""

import argparse
import sqlite3

import pytest

from flightdeck.commands import archive as archive_cmd
from flightdeck.core import archive as core_archive
from flightdeck.core import registry as _registry_mod

from flightdeck.core.archive import archive_sessions
from flightdeck.commands.archive import run as archive_run


# --------------------------------------------------------------------------- #
# Building a minimal state.db that mirrors the real schema's columns
# --------------------------------------------------------------------------- #

_SESSION_COLS = (
    "id TEXT", "source TEXT", "chat_id TEXT", "thread_id TEXT",
    "title TEXT", "display_name TEXT", "started_at REAL", "ended_at REAL",
    "message_count INTEGER",
)
_MESSAGE_COLS = (
    "id INTEGER PRIMARY KEY", "session_id TEXT", "role TEXT", "content TEXT",
    "tool_call_id TEXT", "tool_calls TEXT", "tool_name TEXT",
    "timestamp REAL", "reasoning TEXT",
)


def _make_db(path, sessions, messages):
    """Create a state.db at `path` with the given sessions/messages rows."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE sessions (" + ", ".join(_SESSION_COLS) + ")")
    conn.execute("CREATE TABLE messages (" + ", ".join(_MESSAGE_COLS) + ")")
    for s in sessions:
        cols = ", ".join(k for k in s if s[k] is not None)
        vals = {k: v for k, v in s.items() if v is not None}
        if cols:
            conn.execute(
                f"INSERT INTO sessions ({cols}) VALUES ("
                + ", ".join("?" for _ in vals) + ")",
                list(vals.values()),
            )
    for m in messages:
        cols = ", ".join(k for k in m if m[k] is not None)
        vals = {k: v for k, v in m.items() if v is not None}
        if cols:
            conn.execute(
                f"INSERT INTO messages ({cols}) VALUES ("
                + ", ".join("?" for _ in vals) + ")",
                list(vals.values()),
            )
    conn.commit()
    conn.close()


def _write_registry(tmp_path, rows):
    """Write a registry yaml with the given project rows; return its path."""
    import yaml

    doc = {"projects": rows}
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(path)


def _session(id_, thread_id=None, title=None, **kw):
    row = {"id": id_, "source": "telegram", "thread_id": thread_id,
           "started_at": 1000.0, "title": title}
    row.update(kw)
    return row


def _msg(session_id, role, content, ts, **kw):
    row = {"session_id": session_id, "role": role, "content": content,
           "timestamp": ts}
    row.update(kw)
    return row


@pytest.fixture
def db_and_registry(tmp_path):
    """Build a small state.db + registry; return (db_path, registry_path)."""
    sessions = [
        _session("s_hscc_1", thread_id="2046", title="HSCC Chat"),
        _session("s_app_1", thread_id="2257", title="App Chat"),
        _session("s_null_1", thread_id=None, title=None),
        _session("s_unmapped_1", thread_id="8903", title="Orphan Thread"),
    ]
    messages = [
        _msg("s_hscc_1", "user", "operator hello", 1001.0),
        _msg("s_hscc_1", "assistant", "assistant reply", 1002.0),
        _msg("s_hscc_1", "tool", "huge tool dump " + "x" * 5000, 1003.0,
             tool_name="bash"),
        _msg("s_app_1", "user", "app user words", 1101.0),
        _msg("s_null_1", "user", "unmapped words", 1201.0),
        _msg("s_unmapped_1", "user", "orphan words", 1301.0),
    ]
    db = tmp_path / "state.db"
    _make_db(db, sessions, messages)
    registry_path = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/x/hscc", "topic": 2046},
        {"name": "ecofire-app", "repo": "/x/ecofire", "topic": 2257},
    ])
    return str(db), registry_path


def test_exports_group_by_project_and_unmapped(db_and_registry, tmp_path):
    db, reg = db_and_registry
    out = tmp_path / "out"
    result = archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    assert result.sessions == 4
    assert result.messages == 6
    assert result.files == 4
    # grouped by project where thread maps to a registry topic
    assert result.by_project == {"hscc": 1, "ecofire-app": 1, "unmapped": 2}
    # unmapped threads reported distinctly (8903 present, NULL is not a thread)
    assert result.unmapped_threads == ["8903"]

    # files exist under the right folders
    assert (out / "hscc" / "s_hscc_1_HSCC-Chat.md").exists()
    assert (out / "ecofire-app" / "s_app_1_App-Chat.md").exists()
    assert (out / "unmapped" / "s_null_1_session.md").exists()
    # unmapped non-null thread also lands under unmapped/
    assert (out / "unmapped").exists()
    # index written
    assert (out / "INDEX.md").exists()


def test_index_lists_every_session(db_and_registry, tmp_path):
    db, reg = db_and_registry
    out = tmp_path / "out"
    result = archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    index = (out / "INDEX.md").read_text(encoding="utf-8")
    for sid in ("s_hscc_1", "s_app_1", "s_null_1", "s_unmapped_1"):
        assert sid in index
    assert "## hscc" in index
    assert "## unmapped" in index
    assert "4 sessions" in index and "6 messages" in index


def test_project_filter(db_and_registry, tmp_path):
    db, reg = db_and_registry
    out = tmp_path / "out"
    result = archive_sessions(out_dir=str(out), db_path=db, registry_path=reg,
                              project="hscc")
    assert result.sessions == 1
    assert result.messages == 3
    assert result.by_project == {"hscc": 1}
    # only hscc file present
    assert (out / "hscc" / "s_hscc_1_HSCC-Chat.md").exists()
    assert not (out / "ecofire-app").exists()

    out2 = tmp_path / "out2"
    result2 = archive_sessions(out_dir=str(out2), db_path=db, registry_path=reg,
                               project="ecofire-app")
    assert result2.sessions == 1


def test_operator_words_not_truncated_tool_dump_is(db_and_registry, tmp_path):
    db, reg = db_and_registry
    out = tmp_path / "out"
    archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    body = (out / "hscc" / "s_hscc_1_HSCC-Chat.md").read_text(encoding="utf-8")
    # operator + assistant words intact
    assert "operator hello" in body
    assert "assistant reply" in body
    # the 5000-char tool dump is truncated below TOOL_MAX_CHARS (4000)
    assert "truncated" in body
    assert "x" * 5000 not in body


def test_idempotent_re_run_overwrites_no_duplication(db_and_registry, tmp_path):
    db, reg = db_and_registry
    out = tmp_path / "out"
    r1 = archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    r2 = archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    assert r1.sessions == r2.sessions == 4
    assert r1.messages == r2.messages == 6
    # file count unchanged after re-run
    files = [p for p in out.rglob("*") if p.is_file()]
    assert len(files) == 5  # 4 session files + INDEX.md
    # content identical (no accumulation)
    hscc1 = (out / "hscc" / "s_hscc_1_HSCC-Chat.md").read_text(encoding="utf-8")
    assert hscc1.count("operator hello") == 1


def test_read_only_state_db_not_modified(db_and_registry, tmp_path):
    import hashlib

    db, reg = db_and_registry
    before = hashlib.sha256(open(db, "rb").read()).hexdigest()
    out = tmp_path / "out"
    archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    after = hashlib.sha256(open(db, "rb").read()).hexdigest()
    assert before == after  # we only SELECT; the db bytes never change
    # mode=ro refuses to create a missing db (never silently create/write)
    missing = tmp_path / "does_not_exist.db"
    with pytest.raises(sqlite3.OperationalError):
        core_archive.open_readonly(str(missing))


def test_session_header_fields(db_and_registry, tmp_path):
    db, reg = db_and_registry
    out = tmp_path / "out"
    archive_sessions(out_dir=str(out), db_path=db, registry_path=reg)
    body = (out / "hscc" / "s_hscc_1_HSCC-Chat.md").read_text(encoding="utf-8")
    assert "- **session id**: `s_hscc_1`" in body
    assert "- **source**: telegram" in body
    assert "- **thread_id**: 2046" in body
    assert "- **project**: hscc" in body
    assert "- **message count**: 3" in body


# --------------------------------------------------------------------------- #
# CLI layer
# --------------------------------------------------------------------------- #

def _run_args(registry_path, **kw):
    defaults = dict(out=core_archive.DEFAULT_OUT_DIR, db=core_archive.DEFAULT_STATE_DB,
                    project=None, json=False, registry=registry_path,
                    func=archive_cmd.cmd_archive_sessions)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_cli_run_with_fake_db(db_and_registry, tmp_path, capsys):
    db, reg = db_and_registry
    out = tmp_path / "out"
    args = _run_args(reg, out=str(out), db=db)
    rc = archive_run(args, reg)
    assert rc == 0
    captured = capsys.readouterr().out
    assert "archived 4 session(s), 6 message(s)" in captured
    assert (out / "INDEX.md").exists()


def test_discovery_registers_command():
    """archive-sessions must be discoverable by flightdeck's CLI."""
    from flightdeck.cli import build_parser

    parser = build_parser()
    sub = parser._subparsers
    names = set()
    for act in (sub._group_actions if sub else []):
        names |= set(getattr(act, "choices", {}) or {})
    assert "archive-sessions" in names


def test_default_registry_path_still_groups_by_project(db_and_registry, tmp_path,
                                                       monkeypatch):
    """Omitting ``registry_path`` must NOT silently drop all attribution.

    Every other test passes ``registry_path`` explicitly, so a guard of the form
    ``_topic_owner_map(registry_path) if registry_path else {}`` passed them all
    while filing 100% of real sessions under ``unmapped/`` — the default call is
    the one operators actually make. Pin the default at the registry the fixture
    built and assert grouping still happens.
    """
    db, reg = db_and_registry
    monkeypatch.setattr(_registry_mod, "DEFAULT_REGISTRY", str(reg))

    result = archive_sessions(out_dir=str(tmp_path / "out"), db_path=db)

    assert result.by_project != {"unmapped": result.sessions}, (
        "default registry_path lost every project mapping")
    assert any(p != "unmapped" for p in result.by_project)
