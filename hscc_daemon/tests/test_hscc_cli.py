"""`hscc project <verb>` aliases flightdeck's `project` SUBGROUP verbs.

The lifecycle/registry verbs (new, list, chat, ...) live in flightdeck's
`project` subgroup, which sits under `hscc project` — so the real invocation
was `hscc project project chat`, with the group name doubled. _handle_project
promotes those verbs, guarded so it can never shadow a flightdeck top-level
command. The short form is the canonical user-facing spelling (epilogs,
README); the doubled form keeps working and is pinned below, as is the rule
that the alias table mirrors flightdeck's REAL subgroup (drift here silently
demoted verbs back to the doubled form — sessions/digest/link/unlink).
"""
import sys
import types
from pathlib import Path

import pytest

import hscc_daemon.hscc as h

# The pins below probe flightdeck's REAL parser, and the hscc_daemon leg does
# not otherwise put hscc-project on sys.path (each plugin dir is its own
# pytest process). Same insert the audit engine uses (scripts/
# hscc_cli_help_audit.py); harmless if already present.
_HSCC_PROJECT_DIR = Path(__file__).resolve().parents[2] / "hscc-project"
if str(_HSCC_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_HSCC_PROJECT_DIR))


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


# ── the alias table must mirror flightdeck's REAL subgroup ─────────────── #
# Drift here is silent and user-visible: a subgroup verb missing from the
# table still WORKS (doubled form) but keeps reading
# `hscc project project <verb>`. sessions/digest/link/unlink were added to
# the subgroup and quietly missed the table until t_36580090.

def _real_subgroup_verbs():
    import argparse
    import flightdeck.cli as fd_cli
    parser = fd_cli.build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            project = action.choices["project"]
            for a2 in project._actions:
                if isinstance(a2, argparse._SubParsersAction):
                    return set(a2.choices)
    raise AssertionError("flightdeck parser has no `project` subgroup")


def test_project_alias_covers_whole_subgroup():
    subgroup = _real_subgroup_verbs()
    missing = subgroup - set(h._PROJECT_SUBGROUP_VERBS)
    assert not missing, (
        "flightdeck project-subgroup verbs missing from "
        "_PROJECT_SUBGROUP_VERBS — they keep the doubled "
        f"`hscc project project <verb>` form: {sorted(missing)}")
    # Reverse direction: the table must not claim verbs the subgroup lacks.
    extra = set(h._PROJECT_SUBGROUP_VERBS) - subgroup
    assert not extra, f"_PROJECT_SUBGROUP_VERBS names non-subgroup verbs: {sorted(extra)}"


def test_alias_never_shadows_a_top_level_verb():
    """Every aliased verb must be disjoint from flightdeck's top-level names."""
    import argparse
    import flightdeck.cli as fd_cli
    top = set()
    for action in fd_cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            top |= set(action.choices)
    clash = set(h._PROJECT_SUBGROUP_VERBS) & top
    assert not clash, f"alias would shadow top-level commands: {sorted(clash)}"


@pytest.mark.parametrize("verb", sorted(h._PROJECT_SUBGROUP_VERBS))
def test_every_subgroup_verb_short_form_is_promoted(verb, monkeypatch, tmp_path):
    """The short form is THE documented spelling for every subgroup verb."""
    seen = {}
    assert _run(monkeypatch, tmp_path, ["hscc", "project", verb, "hscc"], seen) == 0
    assert seen["argv"] == ["project", verb, "hscc"]


@pytest.mark.parametrize("verb", sorted(h._PROJECT_SUBGROUP_VERBS))
def test_every_subgroup_verb_doubled_form_still_works(verb, monkeypatch, tmp_path):
    """Back-compat: the old doubled invocation is untouched."""
    seen = {}
    assert _run(monkeypatch, tmp_path,
                ["hscc", "project", "project", verb, "hscc"], seen) == 0
    assert seen["argv"] == ["project", verb, "hscc"]


def test_epilogs_advertise_the_short_form_only():
    """Help/examples must teach `hscc project <verb>`, never the doubled form.

    The doubled form stays PARSEABLE (pinned above) but is no longer
    advertised; F1 called it 'canonical' only because the audit's parse
    oracle did not yet model the alias (fixed in hscc_cli_help_audit.py).
    """
    import argparse
    import flightdeck.cli as fd_cli
    parser = fd_cli.build_parser()
    offenders = []

    def walk(p, path):
        if p.epilog and "project project" in p.epilog:
            offenders.append((path, p.epilog.splitlines()[0]))
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, path + [name])

    walk(parser, [])
    assert not offenders, f"doubled form advertised in epilogs: {offenders}"
