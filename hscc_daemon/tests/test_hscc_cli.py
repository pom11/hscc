"""`hscc project <verb>` aliases flightdeck's `project` SUBGROUP verbs.

The lifecycle/registry verbs (new, list, chat, ...) live in flightdeck's
`project` subgroup, which sits under `hscc project` — so the real invocation
was `hscc project project chat`, with the group name doubled. _handle_project
promotes those verbs, guarded so it can never shadow a flightdeck top-level
command.
"""
import sys
import types

import hscc_daemon.hscc as h


def _fake_flightdeck(seen, top_level=("standup", "doctor", "message")):
    """Stand in for flightdeck.cli: records argv, reports its own commands."""
    def main(argv):
        seen["argv"] = list(argv)
        return 0

    def build_parser():
        action = types.SimpleNamespace(choices={n: None for n in top_level})
        sub = types.SimpleNamespace(_group_actions=[action])
        return types.SimpleNamespace(_subparsers=sub)

    return types.SimpleNamespace(main=main, build_parser=build_parser)


def _run(monkeypatch, tmp_path, argv, seen):
    monkeypatch.setattr(h, "_resolve_project_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "flightdeck", types.ModuleType("flightdeck"))
    monkeypatch.setitem(sys.modules, "flightdeck.cli", _fake_flightdeck(seen))
    monkeypatch.setattr(sys, "argv", argv)
    return h._handle_project()


def test_subgroup_verb_is_promoted(monkeypatch, tmp_path):
    seen = {}
    assert _run(monkeypatch, tmp_path,
                ["hscc", "project", "chat", "hscc"], seen) == 0
    assert seen["argv"] == ["project", "chat", "hscc"]


def test_top_level_command_passes_through(monkeypatch, tmp_path):
    """A flightdeck top-level command must NOT be rewritten."""
    seen = {}
    assert _run(monkeypatch, tmp_path,
                ["hscc", "project", "standup"], seen) == 0
    assert seen["argv"] == ["standup"]


def test_explicit_doubled_form_still_works(monkeypatch, tmp_path):
    seen = {}
    assert _run(monkeypatch, tmp_path,
                ["hscc", "project", "project", "list"], seen) == 0
    assert seen["argv"] == ["project", "list"]


def test_alias_steps_aside_on_future_collision(monkeypatch, tmp_path):
    """If a subgroup verb later becomes a top-level command, the top level wins.

    This is why the guard queries the parser at runtime instead of trusting a
    hardcoded list: `sync` already exists in both namespaces as a MODULE name.
    """
    seen = {}
    monkeypatch.setattr(h, "_resolve_project_dir", lambda: tmp_path)
    monkeypatch.setitem(sys.modules, "flightdeck", types.ModuleType("flightdeck"))
    monkeypatch.setitem(sys.modules, "flightdeck.cli",
                        _fake_flightdeck(seen, top_level=("list", "standup")))
    monkeypatch.setattr(sys, "argv", ["hscc", "project", "list"])
    assert h._handle_project() == 0
    assert seen["argv"] == ["list"]
