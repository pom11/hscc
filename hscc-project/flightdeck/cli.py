"""Flightdeck command line entry point.

Entry point (see pyproject.toml): ``flightdeck = flightdeck.cli:main``.

The skeleton shipped subcommand stubs that exit 2 until each card lands its
module. This card implements ``topics``; ``topics`` is now a real subcommand
built by :func:`~flightdeck.commands.topics.build_subparser` and dispatched to
:func:`~flightdeck.commands.topics.run`. The remaining subcommands keep their
not-implemented stub.
"""

from __future__ import annotations

import argparse
import sys

from .core import registry

# Subcommands implemented so far. A name in BOTH sets is real; a name only here
# is a not-implemented stub that exits 2 with a clear message (see _dispatch).

_NOT_IMPLEMENTED = (
    "flightdeck: command {cmd!r} is not implemented yet. "
    "Implementations land with later cards -- see docs/DESIGN.md."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flightdeck",
        description="Mission control for a multi-project agent fleet.",
        epilog=(
            "examples:\n"
            "  flightdeck standup                  one-shot fleet digest\n"
            "  flightdeck report flightdeck --all  post completion summaries\n"
            "  flightdeck qa                       show the manual-testing queue\n"
            "Try `flightdeck <COMMAND> --help` for per-command usage examples."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of human text",
    )
    parser.add_argument(
        "--registry",
        default=registry.DEFAULT_REGISTRY,
        metavar="PATH",
        help="path to registry.yaml (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    parser.set_defaults(_command_modules=_install_commands(sub))
    return parser


def _install_commands(sub: argparse._SubParsersAction) -> "dict[str, object]":
    """Register every discovered module and map the NAMES it actually claimed.

    A module's file name is not its command name: ``lint.py`` registers
    ``lint-cards``. Dispatching on the module name therefore sent ``lint-cards``
    to the not-implemented stub even though the command was fully written and
    merged. The mapping is built by observing which subparser choices appear
    after each module runs, so a module may register any name — or several —
    and dispatch still finds it.
    """
    mapping: dict[str, object] = {}
    for name, module in _discover_commands().items():
        before = set(sub.choices)
        module.build_subparser(sub)
        claimed = set(sub.choices) - before
        for command_name in claimed or {name}:
            mapping[command_name] = module
    return mapping


def _discover_commands() -> "dict[str, object]":
    """Auto-discover command modules in ``flightdeck/commands/``.

    A command module is any module there exposing BOTH ``build_subparser(sub)``
    and ``run(args, registry_path)``. Discovery means a new command is added by
    dropping in one file — nothing edits this module, so command work never
    collides here. A module missing either hook is skipped silently: it is a
    helper, not a command.
    """
    import importlib
    import pkgutil

    from . import commands as _pkg

    found: dict[str, object] = {}
    for info in pkgutil.iter_modules(_pkg.__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{_pkg.__name__}.{info.name}")
        except Exception as exc:
            # A module that fails to import must never simply vanish: it would
            # then report "not implemented yet", turning a real breakage into a
            # benign-looking message. Say so and carry on with the rest.
            print(
                f"flightdeck: command module {info.name!r} failed to import: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        if hasattr(mod, "build_subparser") and hasattr(mod, "run"):
            found[info.name] = mod
    return dict(sorted(found.items()))


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help(sys.stderr)
        return 2

    return _dispatch(args)


def _dispatch(args: argparse.Namespace) -> int:
    modules = getattr(args, "_command_modules", None)
    if modules is None:  # direct _dispatch call in a test
        modules = _install_commands(
            argparse.ArgumentParser().add_subparsers(dest="command")
        )
    module = modules.get(args.command)
    if module is None:
        print(_NOT_IMPLEMENTED.format(cmd=args.command), file=sys.stderr)
        return 2
    return module.run(args, args.registry)


if __name__ == "__main__":
    sys.exit(main())
