"""Tests for flightdeck.core.map_sessions + flightdeck.commands.map_sessions.

The proposer reads a **test-built** state.db (mirroring the real schema's
columns) READ-ONLY and writes the proposal under a pytest tmp_path. No test
touches the operator's real ~/.hermes/state.db, a live network, the cluster, or
Telegram. The orchestrator pass is exercised with **injected** ask callbacks —
never a real model — so tests are deterministic and offline.
"""

import argparse
import hashlib
import sqlite3

import pytest

from flightdeck.commands import map_sessions as cmd_mod
from flightdeck.commands.map_sessions import run as map_run
from flightdeck.core import map_sessions as core_mod
from flightdeck.core.map_sessions import (
    bounded_sample,
    propose_owners,
    UnparsedAskError,
)

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
    conn.execute(
        "UPDATE sessions SET message_count = "
        "(SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id)"
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
    conn.execute(
        "UPDATE sessions SET message_count = "
        "(SELECT COUNT(*) FROM messages WHERE messages.session_id = sessions.id)"
    )
    conn.commit()
    conn.close()


def _write_registry(tmp_path, rows):
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


PROJECTS = [
    {"name": "hscc", "repo": "/Users/desac/dev/hscc", "topic": 2046},
    {"name": "ecofire-app", "repo": "/Users/desac/dev/ecofire", "topic": 2257},
    {"name": "ecofire-bc", "repo": "/Users/desac/dev/EcoFire_customizations_bc",
     "topic": 3899},
    {"name": "sphoin", "repo": "/Users/desac/dev/sphoin_engine", "topic": 2},
]


@pytest.fixture
def db_and_registry(tmp_path):
    """A 5-session state.db: 4 resolvable by path, 1 truly unknown."""
    sessions = [
        _session("s_hscc", thread_id=None, title=None),
        _session("s_bc", thread_id=None, title=None),
        _session("s_ambig", thread_id=None, title=None),
        _session("s_sphoin", thread_id=None, title=None),
        _session("s_unknown", thread_id=None, title=None),
        _session("s_threaded", thread_id="2046", title="HSCC Chat"),  # excluded
    ]
    messages = [
        _msg("s_hscc", "user", "working on repo /Users/desac/dev/hscc anyway", 1001.0),
        _msg("s_hscc", "assistant", "hscc daemon at ~/.hermes/plugins/hscc-daemon", 1002.0),
        _msg("s_bc", "user", "use repo ~/dev/EcoFire_customizations_bc (BC 23.4)", 1101.0),
        _msg("s_ambig", "user", "compare /Users/desac/dev/hscc and ~/dev/ecofire", 1201.0),
        _msg("s_sphoin", "assistant", "Create /tmp/sphoin_engine/bin/train.py", 1301.0),
        # s_unknown has user words but NO repo-path signal
        _msg("s_unknown", "user", "hey can you help with a quick thing", 1401.0),
        _msg("s_unknown", "assistant", "sure, what do you need", 1402.0),
        _msg("s_threaded", "user", "threaded words", 1501.0),
    ]
    db = tmp_path / "state.db"
    _make_db(db, sessions, messages)
    registry_path = _write_registry(tmp_path, PROJECTS)
    return str(db), registry_path


# --------------------------------------------------------------------------- #
# Pass 1 — deterministic repo-path
# --------------------------------------------------------------------------- #

def test_unambiguous_repo_path_is_high_confidence(db_and_registry):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    by_id = {p.session_id: p for p in r.proposals}
    p = by_id["s_hscc"]
    assert p.project == "hscc"
    assert p.method == "repo-path"
    assert p.confidence == "high"
    # evidence carries the actual matched path (a strong signal, not a guess)
    assert any("hscc" in e for e in p.evidence)


def test_ambiguous_multi_repo_not_resolved_by_pass1(db_and_registry):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    by_id = {p.session_id: p for p in r.proposals}
    assert by_id["s_ambig"].method != "repo-path"  # went to pass 2 / unknown


def test_default_ask_fails_closed_to_unknown(db_and_registry):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    by_id = {p.session_id: p for p in r.proposals}
    p = by_id["s_unknown"]
    assert p.project is None
    assert p.method == "none"
    assert p.confidence == "medium"


def test_threaded_sessions_excluded(db_and_registry):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    ids = {p.session_id for p in r.proposals}
    assert "s_threaded" not in ids  # only NULL-thread sessions are mapped


def test_real_totals_consistent(db_and_registry):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    # 5 NULL-thread sessions; 3 unambiguously resolvable by path, 2 unknown
    assert r.total == 5
    assert r.resolved_repo_path == 3
    assert r.resolved_model == 0
    assert r.unknown == 2
    assert r.deterministic_by_project["hscc"] == 1
    assert r.deterministic_by_project["ecofire-bc"] == 1
    assert r.deterministic_by_project["sphoin"] == 1


# --------------------------------------------------------------------------- #
# Pass 2 — injectable orchestrator ask
# --------------------------------------------------------------------------- #

def test_model_pass_resolves_unknown_via_injected_ask(db_and_registry):
    db, reg = db_and_registry

    def ask(sample, sid):
        return "ecofire-app — there is app talk in here"

    r = propose_owners(db_path=db, registry_path=reg, ask=ask)
    by_id = {p.session_id: p for p in r.proposals}
    for sid in ("s_ambig", "s_unknown"):
        assert by_id[sid].project == "ecofire-app"
        assert by_id[sid].method == "model"
        assert by_id[sid].confidence == "medium"
        # the one-line reason is preserved as evidence
        assert any("Close" in e or "app talk" in e for e in by_id[sid].evidence)


def test_model_pass_gets_bounded_sample(db_and_registry):
    """The ask receives a bounded sample, never every message."""
    db, reg = db_and_registry
    seen = {}

    def ask(sample, sid):
        seen[sid] = sample
        return None  # unknown

    propose_owners(db_path=db, registry_path=reg, ask=ask)
    sample = seen["s_unknown"]
    # s_unknown has exactly 2 messages, both user/assistant, so both appear
    assert "hey can you help with a quick thing" in sample
    assert "sure, what do you need" in sample
    assert sample.count("\n") <= (core_mod.SAMPLE_HEAD + core_mod.SAMPLE_TAIL + 2)


def test_ask_raising_fails_closed_no_crash(db_and_registry):
    db, reg = db_and_registry

    def ask(sample, sid):
        raise RuntimeError("cluster is down")

    r = propose_owners(db_path=db, registry_path=reg, ask=ask)
    assert r.total == 5
    # every non-deterministic session is unknown, none of them crash
    assert all(p.method == "none" or p.method == "repo-path" for p in r.proposals)
    # s_ambig and s_unknown both asked and both raise -> unknown
    assert r.unknown == 2


def test_rogue_ask_reply_is_rejected(db_and_registry):
    """A made-up project name must be refused, never silently accepted."""
    db, reg = db_and_registry

    def ask(sample, sid):
        return "totally-made-up-project"

    with pytest.raises(UnparsedAskError):
        propose_owners(db_path=db, registry_path=reg, ask=ask)


def test_ask_unknown_reply_is_first_class(db_and_registry):
    db, reg = db_and_registry

    def ask(sample, sid):
        return "unknown"

    r = propose_owners(db_path=db, registry_path=reg, ask=ask)
    assert r.resolved_model == 0
    assert r.unknown == 2  # both the ambiguous and the silent session → unknown


# --------------------------------------------------------------------------- #
# bounded_sample
# --------------------------------------------------------------------------- #

def _rows(roles_contents):
    return [{"role": r, "content": c, "timestamp": i}
            for i, (r, c) in enumerate(roles_contents)]


def test_bounded_sample_head_tail_middle_omitted():
    msgs = _rows([("user", f"msg {i}") for i in range(20)])
    s = bounded_sample(msgs, head=3, tail=2)
    assert "msg 0" in s and "msg 1" in s and "msg 2" in s  # head
    assert "msg 18" in s and "msg 19" in s  # tail
    assert "msg 10" not in s  # middle omitted
    assert "messages omitted" in s


def test_bounded_sample_char_cap():
    big = _rows([("user", "y" * 5000) for _ in range(10)])
    s = bounded_sample(big, head=10, tail=0, max_chars=1000)
    assert len(s) <= 1000 + 64  # truncation marker adds a little
    assert "truncated" in s


def test_bounded_sample_empty():
    assert bounded_sample([]) == ""


# --------------------------------------------------------------------------- #
# Proposal report + reversible apply
# --------------------------------------------------------------------------- #

def test_write_proposal_writes_md_and_json(db_and_registry, tmp_path):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    out = tmp_path / "proposals"
    md, js = core_mod.write_proposal(r, str(out), "20210101-000000")
    md_text = open(md, encoding="utf-8").read()
    assert "total sessions: **5**" in md_text
    assert "s_hscc" in md_text  # every session argued
    js_text = open(js, encoding="utf-8").read()
    assert '"session_id": "s_unknown"' in js_text
    import json as _json
    payload = _json.loads(js_text)
    assert payload["resolved_repo_path"] == 3
    assert payload["unknown"] == 2


def test_apply_is_file_only_and_reversible(db_and_registry, tmp_path):
    db, reg = db_and_registry
    r = propose_owners(db_path=db, registry_path=reg)
    out = tmp_path / "proposals"
    map_path, change_path = core_mod.apply_mapping(r, str(out), "20210101-000000")
    # changelog records what changed (session ids are backtick-wrapped)
    change = open(change_path, encoding="utf-8").read()
    assert "`s_hscc` -> hscc" in change
    assert "reversible" in change
    import json as _json
    mapping = _json.loads(open(map_path, encoding="utf-8").read())
    assert mapping["s_hscc"]["project"] == "hscc"
    assert mapping["s_hscc"]["method"] == "repo-path"
    # only the files we wrote exist — state.db untouched (reversible by rm)
    assert len(list(out.iterdir())) == 2


def test_read_only_state_db_not_modified(db_and_registry, tmp_path):
    db, reg = db_and_registry
    before = hashlib.sha256(open(db, "rb").read()).hexdigest()
    r = propose_owners(db_path=db, registry_path=reg)
    out = tmp_path / "proposals"
    core_mod.write_proposal(r, str(out), "20210101-000000")
    core_mod.apply_mapping(r, str(out), "20210101-000000")
    after = hashlib.sha256(open(db, "rb").read()).hexdigest()
    assert before == after  # we only SELECT; the db bytes never change
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        core_mod.open_readonly(str(missing))


# --------------------------------------------------------------------------- #
# CLI layer
# --------------------------------------------------------------------------- #

def _run_args(registry_path, **kw):
    defaults = dict(out=core_mod.DEFAULT_OUT_DIR, db=core_mod.DEFAULT_STATE_DB,
                    apply=False, ask=None, registry=registry_path,
                    func=cmd_mod.cmd_map_sessions)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_cli_run_reports_real_totals(db_and_registry, tmp_path, capsys):
    db, reg = db_and_registry
    out = tmp_path / "out"
    args = _run_args(reg, out=str(out), db=db)
    rc = map_run(args, reg)
    assert rc == 0
    captured = capsys.readouterr().out
    assert "resolved deterministic (repo-path): 3" in captured
    assert "left unknown: 2" in captured
    assert "map-sessions-" in captured  # proposal file path printed


def test_cli_run_apply_writes_mapping(db_and_registry, tmp_path, capsys):
    db, reg = db_and_registry
    out = tmp_path / "out"
    args = _run_args(reg, out=str(out), db=db, apply=True)
    rc = map_run(args, reg)
    assert rc == 0
    captured = capsys.readouterr().out
    assert "applied (reversible, files-only)" in captured
    names = [p.name for p in out.iterdir()]
    assert any(n.startswith("mapping-") and n.endswith(".json") for n in names)
    assert any(n.startswith("mapping-APPLIED-") for n in names)


def test_discovery_registers_command():
    """map-sessions must be discoverable by flightdeck's CLI."""
    from flightdeck.cli import build_parser

    parser = build_parser()
    sub = parser._subparsers
    names = set()
    for act in (sub._group_actions if sub else []):
        names |= set(getattr(act, "choices", {}) or {})
    assert "map-sessions" in names
