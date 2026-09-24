"""Tests for the bounded project digest — `flightdeck.core.digest` + the
`project digest <name>` command wiring.

The digest is a READABLE, BOUNDED synthesis of a project's archive history: per
session title / date range / message count / a short ``decision`` extract / the
resume line, sized for a few thousand tokens. These tests build a digest from a
TEMP archive dir with a few fake session ``.md`` files + an optional
``mapping.json``, asserting:

- the digest is bounded (total chars under a cap),
- per-thread fields are present (title / date-range / count / decision / resume),
- raw transcript bytes are NOT dumped wholesale,
- an env fault (unreadable Hermes runtime) renders as ``runtime_error``, never
  as an empty digest implying "no history".

Nothing touches the operator's real ``~/.hermes`` store: every archive dir and
runtime probe is injected.
"""

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from flightdeck.commands import project as project_cmd
from flightdeck.core import digest as digest_core


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #

def _session_md(session_id, title, *, source="telegram", thread_id="8891",
                project="ecofire", started="2026-07-02 10:05:16 UTC",
                ended="2026-07-02 10:30:00 UTC", msgs=None, body=""):
    """One archive .md file in the exact shape archive.py writes."""
    if not body:
        body = (
            "**user** · 2026-07-02 10:05:16\n"
            "Let's fix the fire reporting pipeline.\n\n"
            "**assistant** · 2026-07-02 10:06:00\n"
            "Agreed. We'll deploy the new ingest service.\n"
        )
    count = msgs if msgs is not None else (body.count("**") // 2)
    return (
        f"# {title}\n"
        f"\n"
        f"- **session id**: `{session_id}`\n"
        f"- **source**: {source}\n"
        f"- **thread_id**: {thread_id}\n"
        f"- **project**: {project}\n"
        f"- **started**: {started}\n"
        f"- **ended**: {ended}\n"
        f"- **message count**: {count}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"{body}\n"
    )


def _write_archive(tmp_path, project, files):
    """Write fake session .md files under ``<tmp>/<project>/``; return the dir."""
    d = tmp_path / project
    d.mkdir(parents=True, exist_ok=True)
    for i, (sid, title) in enumerate(files):
        (d / f"{sid}_{title.replace(' ', '_')}.md").write_text(
            _session_md(sid, title, started=f"2026-07-0{2+i} 10:00:00 UTC"),
            encoding="utf-8",
        )
    return d


def _write_big_file(tmp_path, project, sid="big", title="Big session"):
    """A session file with a huge tool-dump body (must NOT leak into the digest)."""
    d = tmp_path / project
    d.mkdir(parents=True, exist_ok=True)
    # 500 KB of tool dump — the digest must never surface this.
    dump = "tool {n} data\n" * 20000  # ~180 KB
    body = (
        "**user** · 2026-07-03 10:00:00\n"
        "Please summarise the deploy.\n\n"
        "**tool** · 2026-07-03 10:00:01\n"
        f"_tool: fs_read_\n{dump}\n\n"
        "**assistant** · 2026-07-03 10:00:02\n"
        "Deploy looks clean, no errors.\n"
    )
    (d / f"{sid}_{title}.md").write_text(_session_md(
        sid, title, started="2026-07-03 10:00:00 UTC",
        ended="2026-07-03 10:01:00 UTC", msgs=3, body=body,
    ), encoding="utf-8")
    return d


class _FakeSessionDB:
    """Minimal SessionDB stand-in: ``get_messages(sid)`` returns that session's
    rows (already structured like Hermes' ``messages`` rows: ``role``, ``content``,
    ``tool_calls`` (list), ``active``). ``close`` is a no-op so the digest's
    try/finally teardown is exercised."""

    def __init__(self, by_sid):
        self._by_sid = by_sid
        self.closed = False

    def get_messages(self, sid):
        # Faithful to real SessionDB.get_messages default: only active rows.
        return [dict(m) for m in self._by_sid.get(sid, []) if m.get("active", 1)]

    def close(self):
        self.closed = True


def _session_db_provider(by_sid):
    """The seam `build_digest(_session_db=...)` expects: a callable mirroring
    ``_open_profile_session_db(profile, read_only=True)`` returning the fake db."""

    def provider(profile, read_only=True):
        assert profile == "default"
        assert read_only is True
        return _FakeSessionDB(by_sid if by_sid is not None else {})

    return provider


def _default_messages():
    # Mirrors the default `_session_md` body: one user + one assistant message.
    return [
        {"id": 1, "role": "user", "active": 1,
         "content": "Let's fix the fire reporting pipeline.",
         "tool_calls": [], "created_at": "2026-07-02 10:05:16"},
        {"id": 2, "role": "assistant", "active": 1,
         "content": "Agreed. We'll deploy the new ingest service.",
         "tool_calls": [], "created_at": "2026-07-02 10:06:00"},
    ]


def _digest_args(name="ecofire", archive_dir=None, json_=False, mapping_path=None,
                 session_db=None):
    return argparse.Namespace(
        name=name, archive_dir=archive_dir, mapping_path=mapping_path,
        json=json_, runtime_error_fn=None, session_db=session_db,
    )


# --------------------------------------------------------------------------- #
# Core digest — bounded, fields present
# --------------------------------------------------------------------------- #

def test_core_digest_fields_present(tmp_path):
    _write_archive(tmp_path, "ecofire", [
        ("s1", "Fire pipeline fix"),
        ("s2", "Deploy ingest service"),
    ])
    d = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
        _session_db=_session_db_provider({"s1": _default_messages(),
                                          "s2": _default_messages()}),
    )
    assert d["project"] == "ecofire"
    assert d["runtime_error"] is None
    assert len(d["sessions"]) == 2
    for e in d["sessions"]:
        assert e["title"]
        assert e["started"]           # date range start present
        assert e["resume"].startswith("hermes -p default --resume ")
        # decision is bounded to a short slice, never the whole body
        assert len(e["decision"]) <= digest_core.SLICE_MAX_CHARS
        # message_count comes from state.db ACTIVE rows, not the archive header
        assert e["message_count"] == 2
    assert d["total_messages"] == 4


def test_msg_count_reconciles_with_project_sessions(tmp_path):
    """message_count = COUNT(active=1 rows) — the exact number `project sessions`
    shows. Archival `** message count **` headers are IGNORED in favour of the
    state.db count, even when they disagree."""
    d = tmp_path / "ecofire"
    d.mkdir(parents=True, exist_ok=True)
    # Archive header claims 999; state.db has 3 active rows → digest must say 3.
    body = ("**user** · 2026-07-02 10:05:16\nFirst.\n\n"
            "**assistant** · 2026-07-02 10:06:00\nSecond.\n\n"
            "**user** · 2026-07-02 10:07:00\nThird.\n")
    (d / "s1_Mismatch.md").write_text(_session_md(
        "s1", "Mismatch", msgs=999, body=body,
        started="2026-07-02 10:00:00 UTC", ended="2026-07-02 10:07:00 UTC",
    ), encoding="utf-8")

    msgs = [
        {"id": 1, "role": "user", "active": 1, "content": "First.", "tool_calls": [],
         "created_at": "2026-07-02 10:05:16"},
        {"id": 2, "role": "assistant", "active": 1, "content": "Second.", "tool_calls": [],
         "created_at": "2026-07-02 10:06:00"},
        {"id": 3, "role": "tool", "active": 1, "content": "tool payload",
         "tool_calls": [], "created_at": "2026-07-02 10:06:01"},
        # an INACTIVE row must NOT count (matches sessions' active-where)
        {"id": 4, "role": "user", "active": 0, "content": "rewound/old", "tool_calls": [],
         "created_at": "2026-07-02 10:04:00"},
    ]
    dg = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
        _session_db=_session_db_provider({"s1": msgs}),
    )
    entry = dg["sessions"][0]
    assert entry["message_count"] == 3   # 3 active rows; inactive one excluded
    # the tool message is excluded from the decision (role not user/assistant)
    assert "tool payload" not in entry["decision"]
    # the inactive user row is excluded from the decision too
    assert "rewound/old" not in entry["decision"]


def test_decision_whitelist_excludes_all_leak_classes(tmp_path):
    """The decision whitelist must keep only genuinely human user/assistant
    words: tool/system rows, assistant-with-tool_calls (JSON leak), the active
    compaction notice, and inactive rows never surface in the digest."""
    d = tmp_path / "ecofire"
    d.mkdir(parents=True, exist_ok=True)
    (d / "s1_Whitelist.md").write_text(_session_md(
        "s1", "Whitelist", started="2026-07-02 10:00:00 UTC",
        ended="2026-07-02 10:10:00 UTC", msgs=6,
        body="**user** · 2026-07-02 10:00:00\nok\n",
    ), encoding="utf-8")

    msgs = [
        # INCLUDE: clean user / clean assistant
        {"id": 1, "role": "user", "active": 1, "content": "Hello.",
         "tool_calls": [], "created_at": "2026-07-02 10:00:00"},
        {"id": 2, "role": "assistant", "active": 1, "content": "Hi there.",
         "tool_calls": [], "created_at": "2026-07-02 10:00:01"},
        # EXCLUDE: assistant that carries tool_calls JSON (the leak vector)
        {"id": 3, "role": "assistant", "active": 1, "content": "",
         "tool_calls": [{"id": "c1", "name": "run_shell"}], "created_at": "2026-07-02 10:00:02"},
        # EXCLUDE: tool / system rows
        {"id": 4, "role": "tool", "active": 1, "content": "secret tool dump",
         "tool_calls": [], "created_at": "2026-07-02 10:00:03"},
        {"id": 5, "role": "system", "active": 1, "content": "system prompt pump",
         "tool_calls": [], "created_at": "2026-07-02 10:00:04"},
        # EXCLUDE: the active compaction notice (no flag marks it; match text)
        {"id": 6, "role": "assistant", "active": 1,
         "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns...",
         "tool_calls": [], "created_at": "2026-07-02 10:00:05"},
        # EXCLUDE: inactive row
        {"id": 7, "role": "user", "active": 0, "content": "stale turn",
         "tool_calls": [], "created_at": "2026-07-02 10:00:00"},
    ]
    dg = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
        _session_db=_session_db_provider({"s1": msgs}),
    )
    decision = dg["sessions"][0]["decision"]
    assert "Hello." in decision
    assert "Hi there." in decision
    for leak in ("secret tool dump", "system prompt pump",
                 "[CONTEXT COMPACTION", "stale turn"):
        assert leak not in decision
    # count counts ALL active rows (incl. tool/system), matching `project sessions`
    assert dg["sessions"][0]["message_count"] == 6


def test_core_digest_is_bounded(tmp_path):
    # A session with a huge tool dump + long transcript must stay bounded.
    _write_big_file(tmp_path, "ecofire")
    # The leak scenario: an assistant row with tool_calls=set carries the huge
    # dump JSON. Whitelist must exclude it from the decision entirely.
    dump = "tool {n} data\n" * 20000
    big_msgs = [
        {"id": 1, "role": "user", "active": 1, "content": "Please summarise the deploy.",
         "tool_calls": [], "created_at": "2026-07-03 10:00:00"},
        {"id": 2, "role": "assistant", "active": 1, "content": "",
         "tool_calls": [{"id": "call_1", "name": "fs_read"}], "created_at": "2026-07-03 10:00:01"},
        {"id": 3, "role": "tool", "active": 1, "content": "_tool: fs_read_\n" + dump,
         "tool_calls": [], "created_at": "2026-07-03 10:00:01"},
        {"id": 4, "role": "assistant", "active": 1, "content": "Deploy looks clean, no errors.",
         "tool_calls": [], "created_at": "2026-07-03 10:00:02"},
    ]
    d = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
        _session_db=_session_db_provider({"big": big_msgs}),
    )
    text = digest_core.format_digest(d)
    assert len(text) <= digest_core.DIGEST_MAX_CHARS
    # The raw tool dump must NOT appear in the digest text at all.
    assert "tool {n} data" not in text
    assert "tool 0 data" not in text


def test_core_digest_decision_bounded_head_tail(tmp_path):
    """The decision extract keeps first+last user/assistant words, never all."""
    d = tmp_path / "ecofire"
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"**user** · 2026-07-02 10:{i:02d}:00\nmessage {i}\n\n"
        for i in range(12)
    )
    (d / "s1_Many.md").write_text(_session_md(
        "s1", "Many messages", started="2026-07-02 10:00:00 UTC",
        ended="2026-07-02 10:12:00 UTC", msgs=12, body=body,
    ), encoding="utf-8")
    msgs = [
        {"id": i + 1, "role": "user", "active": 1, "content": f"message {i}",
         "tool_calls": [], "created_at": f"2026-07-02 10:{i:02d}:00"}
        for i in range(12)
    ]
    digest = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
        _session_db=_session_db_provider({"s1": msgs}),
    )
    decision = digest["sessions"][0]["decision"]
    assert "message 0" in decision          # head present
    assert "message 11" in decision         # tail present
    # A middle message is omitted (the head+tail window), so the digest never
    # carries the full transcript — explicitly: not every message appears.
    assert "message 5" not in decision or "omitted" in decision


def test_core_digest_no_archive_dir_empty(tmp_path):
    """A project with no archive dir is genuine 'no history', NOT an error."""
    d = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path / "missing"),
        _runtime_error_fn=lambda: None,
        _session_db=_session_db_provider({}),
    )
    assert d["sessions"] == []
    assert d["runtime_error"] is None


# --------------------------------------------------------------------------- #
# Env fault vs data — an unreadable runtime is NOT empty history
# --------------------------------------------------------------------------- #

def test_core_digest_runtime_error_is_surfaced(tmp_path):
    """An unreadable Hermes runtime must render as runtime_error, never as a
    silent empty digest implying the project has no history."""
    d = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: "cannot import the Hermes runtime (test)",
    )
    assert d["runtime_error"]
    assert "cannot import" in d["runtime_error"]
    # The digest still carries the (empty) sessions list alongside the error,
    # but the caller distinguishes: error present == env fault, not no-data.


def test_cmd_digest_runtime_error_renders_and_exit_3(tmp_path, capsys):
    """The CLI renders an env fault distinctly and exits 3 — not 0 ('nothing')."""
    args = _digest_args("ecofire", archive_dir=str(tmp_path))
    # Command wiring forwards the injected runtime probe (here faked).
    args.runtime_error_fn = lambda: "cannot import the Hermes runtime (test)"
    rc = project_cmd.cmd_digest(args)
    out = capsys.readouterr().out
    assert rc == 3
    assert "cannot read session history" in out
    assert "cannot import the Hermes runtime" in out


def test_cmd_digest_runtime_error_json_exit_3(tmp_path, capsys):
    args = _digest_args("ecofire", archive_dir=str(tmp_path), json_=True)
    args.runtime_error_fn = lambda: "cannot import the Hermes runtime (test)"
    rc = project_cmd.cmd_digest(args)
    out = capsys.readouterr().out
    assert rc == 3
    payload = json.loads(out)
    assert payload["runtime_error"] and "cannot import" in payload["runtime_error"]


# --------------------------------------------------------------------------- #
# Command wiring — rendering + exit codes
# --------------------------------------------------------------------------- #

def test_cmd_digest_renders_all_fields(tmp_path, capsys):
    _write_archive(tmp_path, "ecofire", [
        ("s1", "Fire pipeline fix"),
        ("s2", "Deploy ingest service"),
    ])
    args = _digest_args("ecofire", archive_dir=str(tmp_path),
                        session_db=_session_db_provider(
                            {"s1": _default_messages(), "s2": _default_messages()}))
    # In normalized command execution build_digest runs the real probe; here we
    # inject a clean runtime so the test is deterministic.
    args.runtime_error_fn = lambda: None
    rc = project_cmd.cmd_digest(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "project ecofire — digest" in out
    assert "session(s)" in out
    # per-thread: title, dates, count, decision, resume
    assert "## Fire pipeline fix" in out or "## Deploy ingest service" in out
    assert "dates:" in out
    assert "messages:" in out
    assert "decided / outcome:" in out
    assert "hermes -p default --resume s1" in out
    assert "hermes -p default --resume s2" in out


def test_cmd_digest_json_is_clean(tmp_path, capsys):
    _write_archive(tmp_path, "ecofire", [("s1", "One session")])
    args = _digest_args("ecofire", archive_dir=str(tmp_path), json_=True,
                        session_db=_session_db_provider({"s1": _default_messages()}))
    args.runtime_error_fn = lambda: None
    rc = project_cmd.cmd_digest(args)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["project"] == "ecofire"
    assert payload["runtime_error"] is None
    assert payload["sessions"][0]["resume"] == "hermes -p default --resume s1"


def test_cmd_digest_subcommand_registered():
    """`project digest` must be discoverable by the CLI."""
    from flightdeck.cli import build_parser
    import argparse as _argparse

    parser = build_parser()
    proj = None
    for a in parser._actions:
        if isinstance(a, _argparse._SubParsersAction):
            proj = a.choices.get("project")
            break
    assert proj is not None
    projsub = {}
    for a in proj._actions:
        if isinstance(a, _argparse._SubParsersAction):
            projsub = a.choices
    assert "digest" in projsub


# --------------------------------------------------------------------------- #
# Never a physical merge — the digest doesn't touch session state
# --------------------------------------------------------------------------- #

def test_digest_never_writes(tmp_path):
    """The digest is read-only: no files are created or mutated in the archive
    dir, no mapping.json is written, no real session DB is touched."""
    d = _write_archive(tmp_path, "ecofire", [("s1", "One")])
    before_files = sorted(p.name for p in d.iterdir())
    snapshot = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir()}

    mapping = tmp_path / "proposals" / "mapping.json"
    provider = _session_db_provider({"s1": _default_messages()})
    digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path), mapping_path=str(mapping),
        _runtime_error_fn=lambda: None, _session_db=provider,
    )

    assert sorted(p.name for p in d.iterdir()) == before_files
    for p in d.iterdir():
        assert p.read_text(encoding="utf-8") == snapshot[p.name]
    # The binding store was NOT created by a read-only digest.
    assert not mapping.exists()


# --------------------------------------------------------------------------- #
# No-ANSI regression — the converted `project digest` must degrade to plain
# --------------------------------------------------------------------------- #

def _no_ansi(fn):
    """Run ``fn()`` with stdout redirected to a non-tty StringIO (a pipe) and
    assert no ANSI escape sequence leaks into the captured output."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    out = buf.getvalue()
    assert "\x1b[" not in out, f"ANSI escape found in piped output: {out!r}"
    assert "\x1b" not in out, f"ANSI escape found in piped output: {out!r}"
    return out


class TestNoAnsiDigest:
    def test_human_view_is_plain(self, tmp_path):
        _write_archive(tmp_path, "ecofire", [("s1", "Fire pipeline fix")])
        args = _digest_args("ecofire", archive_dir=str(tmp_path),
                            session_db=_session_db_provider(
                                {"s1": _default_messages()}))
        args.runtime_error_fn = lambda: None
        out = _no_ansi(lambda: project_cmd.cmd_digest(args))
        # the digest content is intact under the themed panel, plain, no ANSI
        assert "project ecofire — digest" in out
        assert "## Fire pipeline fix" in out

    def test_json_stays_clean(self, tmp_path):
        _write_archive(tmp_path, "ecofire", [("s1", "One session")])
        args = _digest_args("ecofire", archive_dir=str(tmp_path), json_=True,
                            session_db=_session_db_provider(
                                {"s1": _default_messages()}))
        args.runtime_error_fn = lambda: None
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = project_cmd.cmd_digest(args)
        out = buf.getvalue()
        assert rc == 0
        assert "\x1b[" not in out
        payload = json.loads(out)
        assert payload["project"] == "ecofire"
        assert payload["sessions"][0]["resume"] == "hermes -p default --resume s1"
