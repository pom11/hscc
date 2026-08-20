"""Tests for flightdeck.commands.topics + flightdeck.core.telegram.

Every test drives the command against a STUBBED telegram client (an injectable
``(tool_name, arguments) -> str`` callable). No test touches real Telegram, the
live cluster, or the network. Registry files are written to a pytest tmp_path,
never ~/.flightdeck.
"""

import pytest

from flightdeck.commands import topics as topics_cmd
from flightdeck.core import registry, telegram
from flightdeck.core.telegram import Topic, TopicLockedError
from conftest import TEST_GROUP_ID


# --------------------------------------------------------------------------- #
# A fake telegram MCP client
# --------------------------------------------------------------------------- #

class FakeTG:
    """Stands in for the MCP daemon client: a callable (tool, args) -> str.

    Canned topic state (id -> current name) answers telegram_topic_status.
    Topic mutations are recorded (not applied to ``state`` since the command
    layer, not this stub, owns naming) but callable checks what was requested.
    """

    def __init__(self, topics: dict[int, str], locked: bool = False):
        # id -> name as Telegram currently sees it (the authoritative source).
        self.state = dict(topics)
        self.locked = locked
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if self.locked:
            raise ConnectionError(
                "sqlite3.OperationalError: database is locked"
            )
        if tool_name == "telegram_topic_status":
            lines = [
                f"topic_id={tid} title={name}" for tid, name in sorted(self.state.items())
            ]
            return "\n".join(lines)
        if tool_name == "telegram_topic_create":
            new_id = max(self.state) + 1 if self.state else 1
            self.state[new_id] = arguments["name"]
            return f"created topic_id={new_id} title={arguments['name']}"
        if tool_name == "telegram_topic_rename":
            tid = arguments["topic_id"]
            # Mirror the MCP daemon: renaming a topic that doesn't exist is a no-op
            # that reports the topic back with its (unchanged) name.
            name = self.state.get(tid, "?")
            if tid in self.state:
                name = arguments["name"]
                self.state[tid] = arguments["name"]
            return f"renamed topic_id={tid} title={name}"
        raise AssertionError(f"FakeTG does not know tool {tool_name!r}")


def _ns(**kw):
    """Build an argparse.Namespace with sensible defaults for a topics cmd."""
    import argparse

    defaults = dict(
        client=None, registry=None, json=False, apply=False,
        id=0, name="", bind=None, project="", topic_cmd=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _write_registry(tmp_path, rows, ignored_topics=None):
    """Write a registry yaml with the given project rows; return its path."""
    import yaml

    doc = {"projects": rows}
    if ignored_topics is not None:
        doc["ignored_topics"] = sorted(set(ignored_topics))
    p = tmp_path / "registry.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return str(p)


def _hscc_row(topic=140, topic_name=None):
    row = {"name": "hscc", "repo": "~/dev/hscc", "topic": topic}
    if topic_name is not None:
        row["topic_name"] = topic_name
    return row


# --------------------------------------------------------------------------- #
# core.telegram — parsing + locked handling
# --------------------------------------------------------------------------- #

def test_list_topics_parses_names_from_stub():
    fake = FakeTG({140: "HSCC cluster", 2257: "app.ecofire.ro"})
    topics = telegram.list_topics(_client=fake)
    assert topics == [Topic(140, "HSCC cluster"), Topic(2257, "app.ecofire.ro")]


def test_rename_returns_updated_topic():
    fake = FakeTG({140: "HSCC cluster"})
    updated = telegram.rename_topic(140, "HSCC cluster", _client=fake)
    assert updated == Topic(140, "HSCC cluster")
    assert fake.state[140] == "HSCC cluster"


def test_create_returns_new_topic():
    fake = FakeTG({140: "HSCC cluster"})
    created = telegram.create_topic("ecofire", _client=fake)
    assert created.name == "ecofire"
    assert fake.state[created.id] == "ecofire"


def test_locked_error_is_normalised_to_topiclocked():
    fake = FakeTG({140: "HSCC cluster"}, locked=True)
    with pytest.raises(TopicLockedError):
        telegram.list_topics(_client=fake)


# --------------------------------------------------------------------------- #
# audit — the overwrite detector
# --------------------------------------------------------------------------- #

def test_audit_flags_a_mismatched_name(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "Acknowledged - some bot message text"})  # overwritten
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISMATCH" in out
    assert "140" in out
    assert "hscc" in out


def test_audit_flags_an_unmapped_topic(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster", 9999: "some orphan topic"})
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNMAPPED" in out
    assert "9999" in out


def test_audit_is_clean_when_names_match(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "hscc"})  # current name equals registry name
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "audit clean" in out


def test_audit_is_clean_when_topic_name_matches(tmp_path, capsys):
    """A topic whose intended name differs from the project key (e.g. topic 140
    being "HSCC cluster" while the project key is "hscc") is NOT flagged when
    topic_name records the correct intended name."""
    reg = _write_registry(tmp_path, [_hscc_row(topic=140, topic_name="HSCC cluster")])
    fake = FakeTG({140: "HSCC cluster"})  # healthy: current == topic_name
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "audit clean" in out


def test_audit_flags_when_topic_name_was_overwritten(tmp_path, capsys):
    """The 2257-shaped disease: topic_name records "app.ecofire.ro" but the live
    topic was overwritten by a bot message -> flagged as mismatched."""
    reg = _write_registry(tmp_path, [_hscc_row(topic=2257, topic_name="app.ecofire.ro")])
    fake = FakeTG({2257: "self-improvement review message"})  # overwritten
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISMATCH" in out
    assert "app.ecofire.ro" in out


# --------------------------------------------------------------------------- #
# audit — the ignored_topics list (the live t_9addaf05 gap)
# --------------------------------------------------------------------------- #
# The live gap this card fixes: topic 140 was explicitly ignored via
# `flightdeck project sync --ignore-topic 140 --apply` (persisting 140 to the
# registry's ignored_topics list) but `topics audit` kept reporting it as
# [UNMAPPED]. audit must honour the same ignore list sync persists: the
# operator's persisted set PLUS the built-in default (topic 1 / General), and
# exclude any of them from [UNMAPPED] entirely.

def test_audit_excludes_topic_explicitly_ignored_via_sync(tmp_path, capsys):
    """topic 140 was explicitly ignored via sync (persisted in ignored_topics)
    but audit still nagged it as [UNMAPPED]. Excluded entirely now."""
    # 140 is NOT bound to any project (so it would otherwise be [UNMAPPED]) but
    # it IS in the operator's persisted ignored_topics list; 9999 is a genuine
    # orphan that must still surface.
    reg = _write_registry(tmp_path, [], ignored_topics=[140])
    fake = FakeTG({140: "HSCC cluster", 9999: "some orphan topic"})
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNMAPPED" in out
    assert "9999" in out
    assert "140" not in out


def test_audit_excludes_general_topic_by_default(tmp_path, capsys):
    """Topic 1 (Telegram's built-in General) is excluded by the built-in default
    even with no registry entries for it and nothing in the ignore list."""
    reg = _write_registry(tmp_path, [])
    fake = FakeTG({1: "General", 9999: "some orphan topic"})
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNMAPPED" in out
    assert "9999" in out
    assert "General" not in out


def test_audit_ignored_topic_still_reports_mismatch_when_bound(tmp_path, capsys):
    """Ignore only suppresses 'no project mapping'. An ignored topic that is
    bound to a real project and whose live name differs from the registry's
    expected name still reports [MISMATCH] — a genuine finding is not hidden."""
    reg = _write_registry(
        tmp_path,
        [_hscc_row(topic=140, topic_name="HSCC cluster")],
        ignored_topics=[140],
    )
    fake = FakeTG({140: "overwritten by a bot message"})  # bound but mismatched
    rc = topics_cmd.cmd_audit(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 1
    assert "MISMATCH" in out
    assert "HSCC cluster" in out
    assert "140" in out


# --------------------------------------------------------------------------- #
# rename — --apply gating
# --------------------------------------------------------------------------- #

def test_rename_without_apply_mutates_nothing(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster"})
    # create a tasks-side record of calls: dry-run must not call the rename tool
    rc = topics_cmd.cmd_rename(_ns(client=fake, registry=reg, apply=False, id=140, name="NEW"), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "--apply" in out
    # No mutating MCP call happened.
    assert all(tool != "telegram_topic_rename" for (tool, _) in fake.calls)
    assert fake.state[140] == "HSCC cluster"  # unchanged


def test_rename_with_apply_calls_client_and_mutates(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster"})
    rc = topics_cmd.cmd_rename(_ns(client=fake, registry=reg, apply=True, id=140, name="HSCC cluster"), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "renamed topic 140" in out
    assert ("telegram_topic_rename", {"group": TEST_GROUP_ID, "topic_id": 140, "name": "HSCC cluster"}) in fake.calls


# --------------------------------------------------------------------------- #
# create — --apply gating (mirrors rename)
# --------------------------------------------------------------------------- #

def test_create_without_apply_mutates_nothing(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster"})
    rc = topics_cmd.cmd_create(_ns(client=fake, registry=reg, apply=False, name="neofetch"), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "--apply" in out
    assert all(tool != "telegram_topic_create" for (tool, _) in fake.calls)
    assert 140 in fake.state and "neofetch" not in fake.state.values()


# --------------------------------------------------------------------------- #
# bind / unbind — registry mutations gated on --apply
# --------------------------------------------------------------------------- #

def test_bind_without_apply_mutates_nothing(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    before = registry.load_registry(reg)
    rc = topics_cmd.cmd_bind(_ns(client=FakeTG({}), registry=reg, apply=False, id=9999, project="sphoin"), before)
    out = capsys.readouterr().out
    assert rc == 0
    assert "--apply" in out
    assert registry.load_registry(reg) == before  # registry unchanged


def test_bind_with_apply_updates_registry(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    rc = topics_cmd.cmd_bind(_ns(client=FakeTG({}), registry=reg, apply=True, id=9999, project="hscc"), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "bound" in out
    assert registry.get_project("hscc", reg).topic == 9999


def test_unbind_without_apply_mutates_nothing(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    before = registry.load_registry(reg)
    rc = topics_cmd.cmd_unbind(_ns(client=FakeTG({}), registry=reg, apply=False, id=140), before)
    out = capsys.readouterr().out
    assert rc == 0
    assert "--apply" in out
    assert registry.load_registry(reg) == before


def test_unbind_with_apply_clears_registry(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    rc = topics_cmd.cmd_unbind(_ns(client=FakeTG({}), registry=reg, apply=True, id=140), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unbound" in out
    assert registry.get_project("hscc", reg).topic is None


# --------------------------------------------------------------------------- #
# locked-database: surfaces as a clear message, never a traceback
# --------------------------------------------------------------------------- #

def test_rename_locked_surfaces_clear_message(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster"}, locked=True)
    rc = topics_cmd.cmd_rename(_ns(client=fake, registry=reg, apply=True, id=140, name="X"), registry.load_registry(reg))
    err = capsys.readouterr().err
    assert rc == 3
    assert "locked" in err
    assert "retry" in err
    assert "Traceback" not in err


def test_create_locked_surfaces_clear_message(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster"}, locked=True)
    rc = topics_cmd.cmd_create(_ns(client=fake, registry=reg, apply=True, name="x"), registry.load_registry(reg))
    err = capsys.readouterr().err
    assert rc == 3
    assert "locked" in err
    assert "Traceback" not in err


# --------------------------------------------------------------------------- #
# list — read-only presentation
# --------------------------------------------------------------------------- #

def test_list_shows_id_name_and_mapped_project(tmp_path, capsys):
    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster", 2257: "app.ecofire.ro"})
    rc = topics_cmd.cmd_list(_ns(client=fake, registry=reg, json=False), registry.load_registry(reg))
    out = capsys.readouterr().out
    assert rc == 0
    assert "140" in out and "HSCC cluster" in out
    assert "hscc" in out          # mapped project shown
    assert "unknown" in out       # 2257 has no mapping -> unknown


def test_list_json_shape(tmp_path, capsys):
    import json

    reg = _write_registry(tmp_path, [_hscc_row(topic=140)])
    fake = FakeTG({140: "HSCC cluster"})
    rc = topics_cmd.cmd_list(_ns(client=fake, registry=reg, json=True), registry.load_registry(reg))
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == [{"id": 140, "name": "HSCC cluster", "project": "hscc"}]
