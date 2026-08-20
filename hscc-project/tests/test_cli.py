"""CLI entry point: argument parsing and command auto-discovery.

Pure argparse behaviour — no network, no Telegram, no git. Commands are
discovered from ``flightdeck/commands/``, so this module never enumerates them
and adding a command touches no shared file.
"""
from __future__ import annotations

import argparse

import pytest

from flightdeck.cli import build_parser, main


def test_build_parser_help_works():
    """argparse help for the top-level command exits 0 and prints usage."""
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--help"])
    assert exc.value.code == 0


def test_main_help_exits_zero():
    """`flightdeck --help` is handled by argparse and exits 0."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_global_flags_parsed():
    """Global flags are available alongside a discovered subcommand."""
    args = build_parser().parse_args(
        ["--json", "--registry", "/tmp/reg.yaml", "topics", "list"]
    )
    assert args.json is True
    assert args.registry == "/tmp/reg.yaml"
    assert args.command == "topics"


@pytest.mark.parametrize("cmd", ["projects"])
def test_undiscovered_command_is_rejected_with_exit_2(capsys, cmd):
    """A command with no module in flightdeck/commands/ is simply unknown.

    Because cli.py auto-discovers command modules, an unimplemented command is
    never registered. argparse rejects it, names it, and exits 2 — the same
    contract the old hardcoded stub list provided, without cli.py having to know
    every command in advance. That is what lets a new command be added by
    dropping in one file, so parallel command work never collides here.
    """
    with pytest.raises(SystemExit) as exc:
        main([cmd])
    assert exc.value.code == 2
    assert cmd in capsys.readouterr().err


def test_discovered_command_is_registered():
    """`topics` ships as a module, so discovery must expose it."""
    args = build_parser().parse_args(["topics", "list"])
    assert args.command == "topics"


def test_topics_without_subcommand_exits_2(capsys):
    """`flightdeck topics` with no action must not silently run; exit 2 + hint."""
    rc = main(["topics"])
    assert rc == 2
    assert "topics" in capsys.readouterr().err


def test_no_command_exits_2_and_prints_help(capsys):
    """Bare `flightdeck` prints help to stderr and exits 2."""
    rc = main([])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_command_name_may_differ_from_module_name():
    """`lint.py` registers `lint-cards`; dispatch must find it.

    Keying dispatch on the module name sent `lint-cards` to the
    not-implemented stub even though the command was written and merged.
    """
    from flightdeck.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["lint-cards"])
    assert args.command == "lint-cards"
    modules = args._command_modules
    assert modules["lint-cards"].__name__.endswith("commands.lint")


def test_every_registered_subcommand_is_dispatchable():
    """No advertised command may fall through to the not-implemented stub."""
    from flightdeck.cli import build_parser

    parser = build_parser()
    choices = [a.choices for a in parser._actions if getattr(a, "choices", None)][0]
    modules = parser.get_default("_command_modules")
    unroutable = [c for c in choices if c not in modules]
    assert not unroutable, f"registered but not dispatchable: {unroutable}"


def _walk_parsers(parser: argparse.ArgumentParser):
    """Yield every ArgumentParser reachable from ``parser``.

    Argparse stores nested subcommands (``roadmap add``, ``topics bind``,
    ``project sync``, ``message dispatch``) as ``_SubParsersAction`` choices.
    Walking the tree finds every leaf parser, so each one's ``--help`` epilog
    is checked exactly as a first-time user would see it.
    """
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _walk_parsers(sub)


def test_every_command_help_has_a_usage_example():
    """Every registered command's --help shows a concrete usage example.

    Acceptance test: fails the moment any command's usage example is removed.
    Each top-level AND nested subparser (e.g. ``roadmap add``, ``topics bind``,
    ``project sync``, ``message dispatch``) must carry a non-empty ``epilog``
    starting with ``example``, and the usage example must end with an actual
    invocation (so a bare, content-free epilog cannot pass).
    """
    parser = build_parser()
    parsers = list(_walk_parsers(parser))

    missing = [
        p.prog
        for p in parsers
        if not (p.epilog or "").strip()  # non-empty
        or not (p.epilog or "").splitlines()[0].strip().lower().startswith("example")
    ]
    assert not missing, f"commands without a usage-example epilog: {missing}"

    # The whole tree is covered: every leaf parser has an example, so the
    # number of parsed leaves is a lower bound we can sanity-check.
    assert len(parsers) >= 26, "expected at least the root + 25 commands"
