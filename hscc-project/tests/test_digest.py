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
import json
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


def _digest_args(name="ecofire", archive_dir=None, json_=False, mapping_path=None):
    return argparse.Namespace(
        name=name, archive_dir=archive_dir, mapping_path=mapping_path,
        json=json_, runtime_error_fn=None,
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
    assert d["total_messages"] >= 2


def test_core_digest_is_bounded(tmp_path):
    # A session with a huge tool dump + long transcript must stay bounded.
    _write_big_file(tmp_path, "ecofire")
    d = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
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
    digest = digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path),
        _runtime_error_fn=lambda: None,
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
    args = _digest_args("ecofire", archive_dir=str(tmp_path))
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
    args = _digest_args("ecofire", archive_dir=str(tmp_path), json_=True)
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
    dir, no mapping.json is written, no session DB is touched."""
    d = _write_archive(tmp_path, "ecofire", [("s1", "One")])
    before_files = sorted(p.name for p in d.iterdir())
    snapshot = {p.name: p.read_text(encoding="utf-8") for p in d.iterdir()}

    mapping = tmp_path / "proposals" / "mapping.json"
    digest_core.build_digest(
        "ecofire", archive_dir=str(tmp_path), mapping_path=str(mapping),
        _runtime_error_fn=lambda: None,
    )

    assert sorted(p.name for p in d.iterdir()) == before_files
    for p in d.iterdir():
        assert p.read_text(encoding="utf-8") == snapshot[p.name]
    # The binding store was NOT created by a read-only digest.
    assert not mapping.exists()
