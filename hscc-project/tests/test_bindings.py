"""Tests for the canonical session→project binding store.

Covers :mod:`flightdeck.core.bindings` (the shared load/save/bind/unbind/list
store), the ``hscc project link / unlink / link --list`` command wiring, and
the CONVERGENCE contract — ``map_sessions --apply`` and manual ``link`` both
write the SAME canonical ``mapping.json`` and neither clobbers the other.

Everything runs against temp files (injectable ``mapping_path``); nothing ever
touches the operator's real ``~/.hermes`` store.
"""

import argparse
import json
import os
from pathlib import Path

import pytest

from flightdeck.commands import project as project_cmd
from flightdeck.commands import map_sessions as map_sessions_cmd
from flightdeck.core import bindings
from flightdeck.core import map_sessions as map_core
from flightdeck.core import registry
from flightdeck.core import session_discovery


# --------------------------------------------------------------------------- #
# Helpers (mirror test_session_discovery's fakes for the discovery integration)
# --------------------------------------------------------------------------- #

class _FakeOrchDB:
    def __init__(self, rows):
        self._rows = {r["id"]: dict(r) for r in rows}
        self._closed = False

    def get_session(self, sid):
        row = self._rows.get(sid)
        return dict(row) if row else None

    def close(self):
        self._closed = True


class _FakeOrchProvider:
    def __init__(self, rows=()):
        self._db = _FakeOrchDB(rows)

    def __call__(self, profile):
        return self._db


class _FakeDefaultDB:
    def __init__(self, rows=()):
        self._rows = [dict(r) for r in rows]
        self._closed = False

    def list_sessions_rich(self, source, limit=5000):
        assert source == "telegram"
        return [dict(r) for r in self._rows]

    def close(self):
        self._closed = True


class _FakeDefaultProvider:
    def __init__(self, rows=()):
        self._db = _FakeDefaultDB(rows)

    def __call__(self, profile):
        return self._db


def _tg(id_, thread, title="t", msgs=None, last=None, started=None):
    return {
        "id": id_, "source": "telegram", "thread_id": thread,
        "chat_id": "-1001", "title": title, "display_name": title,
        "message_count": msgs, "started_at": started, "last_active": last,
    }


def _reg(tmp_path, name="ecofire", topic=8891, session="20260921_abc"):
    path = str(tmp_path / "registry.yaml")
    registry.add_project(name, repo=str(tmp_path / name), board=name,
                         topic=topic, path=path)
    if session:
        registry.set_session(name, session, path=path)
    return path


# --------------------------------------------------------------------------- #
# bindings store — core behaviour
# --------------------------------------------------------------------------- #

def test_bind_is_idempotent_same_project(tmp_path):
    p = str(tmp_path / "mapping.json")
    first = bindings.bind("s1", "ecofire", evidence="op on d1", mapping_path=p)
    second = bindings.bind("s1", "ecofire", evidence="op on d1", mapping_path=p)
    assert first == second
    store = bindings.load(p)
    assert store["s1"]["project"] == "ecofire"
    assert store["s1"]["method"] == "manual"
    assert len(store) == 1


def test_bind_overwrites_different_project_last_wins(tmp_path):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", evidence="a", mapping_path=p)
    bindings.bind("s1", "prime", evidence="b", mapping_path=p)
    store = bindings.load(p)
    assert store["s1"]["project"] == "prime"
    assert len(store) == 1  # replaced, not appended


def test_bind_confidence_is_numeric_not_string(tmp_path):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=p)
    conf = bindings.load(p)["s1"]["confidence"]
    assert isinstance(conf, float)
    assert conf == 1.0


def test_bind_evidence_names_operator_and_date(tmp_path):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", evidence="manual link by Alice on 2026-09-22",
                  mapping_path=p)
    assert bindings.load(p)["s1"]["evidence"] == \
        "manual link by Alice on 2026-09-22"


def test_unbind_removes_entry(tmp_path):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=p)
    bindings.bind("s2", "prime", mapping_path=p)
    assert bindings.unbind("s1", mapping_path=p) is True
    store = bindings.load(p)
    assert "s1" not in store
    assert "s2" in store  # other entries untouched


def test_unbind_missing_binding_is_clean_noop(tmp_path):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=p)
    # unlink a session with no binding -> False, nothing changes, no error
    assert bindings.unbind("zzz_missing", mapping_path=p) is False
    store = bindings.load(p)
    assert "s1" in store
    # and unlink against a nonexistent file entirely is also a clean no-op
    assert bindings.unbind("whatever", mapping_path=str(tmp_path / "nope.json")) is False


def test_load_empty_and_missing(tmp_path):
    missing = str(tmp_path / "does" / "not" / "exist.json")
    assert bindings.load(missing) == {}
    # not a JSON object -> {} (data fault, not an exception)
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")
    assert bindings.load(str(bad)) == {}


def test_save_atomic_and_valid_json_after_every_op(tmp_path):
    """After every op the file is a valid JSON object, and there is never a
    leftover temp file (atomic write -> intact + clean)."""
    p = tmp_path / "mapping.json"
    bindings.bind("s1", "ecofire", mapping_path=str(p))
    bindings.bind("s2", "prime", mapping_path=str(p))
    bindings.unbind("s1", mapping_path=str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert set(data) == {"s2"}
    # no .mapping-*.json.tmp residue from the atomic save
    residue = [n for n in os.listdir(tmp_path) if ".tmp" in n]
    assert residue == []


def test_list_all_and_by_project(tmp_path):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=str(p))
    bindings.bind("s2", "ecofire", mapping_path=str(p))
    bindings.bind("s3", "prime", mapping_path=str(p))
    assert set(bindings.list(str(p))) == {"s1", "s2", "s3"}
    only = bindings.list(str(p), project="ecofire")
    assert set(only) == {"s1", "s2"}


# --------------------------------------------------------------------------- #
# CLI wiring — link / unlink / link --list
# --------------------------------------------------------------------------- #

def _link_args(project, session_id, mapping_path, list_=False):
    return argparse.Namespace(project=project, session_id=session_id,
                              mapping_path=mapping_path, list=list_,
                              evidence=None)


def _unlink_args(project, session_id, mapping_path):
    return argparse.Namespace(project=project, session_id=session_id,
                              mapping_path=mapping_path)


def test_cmd_link_writes_and_echoes(tmp_path, capsys):
    p = str(tmp_path / "mapping.json")
    rc = project_cmd.cmd_link(_link_args("ecofire", "s1", p))
    assert rc == 0
    out = capsys.readouterr().out
    assert "linked session s1 -> project 'ecofire'" in out
    assert "confidence 1.0" in out
    store = bindings.load(p)
    assert store["s1"]["project"] == "ecofire"
    assert store["s1"]["method"] == "manual"
    assert isinstance(store["s1"]["confidence"], float)


def test_cmd_link_list_all(tmp_path, capsys):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=str(p))
    bindings.bind("s2", "prime", mapping_path=str(p))
    rc = project_cmd.cmd_link(_link_args(None, None, p, list_=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ecofire" in out and "prime" in out
    assert "s1" in out and "s2" in out


def test_cmd_link_list_filtered_by_project(tmp_path, capsys):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=str(p))
    bindings.bind("s2", "prime", mapping_path=str(p))
    rc = project_cmd.cmd_link(_link_args("ecofire", None, p, list_=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "s1" in out          # ecofire's binding listed
    assert "s2" not in out and "prime" not in out  # other project filtered out


def test_cmd_link_requires_project_and_session(tmp_path, capsys):
    rc = project_cmd.cmd_link(_link_args(None, None, str(tmp_path / "m.json")))
    assert rc == 2
    assert not (tmp_path / "m.json").exists()  # nothing written


def test_cmd_unlink_removes(tmp_path, capsys):
    p = str(tmp_path / "mapping.json")
    bindings.bind("s1", "ecofire", mapping_path=str(p))
    rc = project_cmd.cmd_unlink(_unlink_args("ecofire", "s1", p))
    assert rc == 0
    out = capsys.readouterr().out
    assert "unlinked session s1" in out
    assert "s1" not in bindings.load(p)


def test_cmd_unlink_missing_noop_says_so(tmp_path, capsys):
    p = str(tmp_path / "mapping.json")
    rc = project_cmd.cmd_unlink(_unlink_args("ecofire", "zzz", p))
    assert rc == 0  # not an error
    out = capsys.readouterr().out
    assert "had no binding" in out


def test_subcommands_registered_under_project():
    """`project link` / `project unlink` must be discoverable by the CLI."""
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
    assert {"link", "unlink"} <= set(projsub.keys())


# --------------------------------------------------------------------------- #
# Discovery integration — link, then discovery sees it (card 1 code on dev)
# --------------------------------------------------------------------------- #

def test_link_then_discovery_sees_it(tmp_path):
    """After `bind`, `list_project_sessions` discovers the session for the
    project — the exact contract card 1 made discovery read the store for."""
    reg = _reg(tmp_path, "ecofire", topic=8891)
    p = str(tmp_path / "mapping.json")
    tg_rows = [
        _tg("tg-by-thread", "8891", title="thread"),
        _tg("tg-hand", "7788", title="hand-linked"),   # NULL-to-topic thread
    ]
    # manual link: bind tg-hand to ecofire (no thread match, so only the store
    # can own it) and ALSO link one already-threaded session to the SAME project
    # to prove the union is de-duplicated.
    bindings.bind("tg-hand", "ecofire", evidence="op 2026-09-22", mapping_path=p)

    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=_FakeOrchProvider(),
        _default_db=_FakeDefaultProvider(tg_rows),
        _mapping_path=p,
    )
    ids = {r["id"] for r in result["telegram"]}
    assert "tg-hand" in ids          # via binding store
    assert "tg-by-thread" in ids     # via thread_id == topic (unchanged)
    assert result["telegram_unmapped"] == 0  # tg-hand is owned, not unmapped


def test_unlink_then_discovery_no_longer_sees_it(tmp_path):
    reg = _reg(tmp_path, "ecofire", topic=8891)
    p = str(tmp_path / "mapping.json")
    # NULL-thread session: only the binding store can own it (no thread match).
    tg_rows = [_tg("tg-hand", None, title="hand")]
    bindings.bind("tg-hand", "ecofire", mapping_path=p)
    # while bound, discovery owns it (not unmapped)
    before = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=_FakeOrchProvider(),
        _default_db=_FakeDefaultProvider(tg_rows),
        _mapping_path=p,
    )
    assert [r["id"] for r in before["telegram"]] == ["tg-hand"]
    assert before["telegram_unmapped"] == 0

    assert bindings.unbind("tg-hand", mapping_path=p) is True
    result = session_discovery.list_project_sessions(
        "ecofire", path=reg,
        _session_db=_FakeOrchProvider(),
        _default_db=_FakeDefaultProvider(tg_rows),
        _mapping_path=p,
    )
    assert result["telegram"] == []          # no binding, no thread match
    assert result["telegram_unmapped"] == 1  # back to unmapped


# --------------------------------------------------------------------------- #
# Convergence — --apply and link agree on ONE file, no clobbering
# --------------------------------------------------------------------------- #

def test_apply_and_link_converge_on_one_file(tmp_path):
    """map-sessions --apply and a manual `link` write the same canonical
    mapping.json; a later --apply must NOT clobber the manual link, and vice
    versa."""
    out = tmp_path / "proposals"
    out.mkdir()
    mapping_json = out / "mapping.json"

    # --apply persists resolved proposals into the canonical mapping.json.
    from flightdeck.core.map_sessions import Proposal, MapResult
    r = MapResult(total=1, proposals=[
        Proposal(session_id="s_auto", msgs=2, started_at=None, ended_at=None,
                 project="ecofire", method="repo-path", confidence="high",
                 evidence=["matched repo path"]),
    ])
    map_path, _change = map_core.apply_mapping(r, str(out), "20210101-000000")
    assert Path(map_path) == mapping_json
    assert mapping_json.exists()

    # Now a human MANUALLY links another session — same file.
    bindings.bind("s_manual", "prime", evidence="human", mapping_path=str(mapping_json))

    # A SECOND --apply (for the same/different sessions) must merge, not clobber.
    r2 = MapResult(total=1, proposals=[
        Proposal(session_id="s_auto2", msgs=1, started_at=None, ended_at=None,
                 project="ecofire", method="model", confidence="medium",
                 evidence=["model decided"]),
    ])
    map_core.apply_mapping(r2, str(out), "20210101-000001")

    store = bindings.load(str(mapping_json))
    # the manual link survived the second --apply
    assert store["s_manual"]["project"] == "prime"
    # the first --apply entry survived too (--apply only sets what it proposes)
    assert store["s_auto"]["project"] == "ecofire"
    assert store["s_auto"]["method"] == "repo-path"
    # the second --apply's own entry is present
    assert store["s_auto2"]["project"] == "ecofire"
    assert store["s_auto2"]["method"] == "model"


def test_apply_skips_unknown_proposals(tmp_path):
    """A proposal with project=None (unknown) must NOT be written to the
    canonical store — a binding with no owner is meaningless."""
    out = tmp_path / "proposals"
    out.mkdir()
    from flightdeck.core.map_sessions import Proposal, MapResult
    r = MapResult(total=1, proposals=[
        Proposal(session_id="s_unknown", msgs=1, started_at=None, ended_at=None,
                 project=None, method="none", confidence="medium",
                 evidence=["no signal"]),
    ])
    map_core.apply_mapping(r, str(out), "20210101-000000")
    # unknown proposals are never written as bindings — the store has no
    # project= entries for them (a binding with no owner is meaningless).
    store = bindings.load(str(out / "mapping.json"))
    assert store == {}
