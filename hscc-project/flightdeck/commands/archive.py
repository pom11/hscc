"""archive.py — `flightdeck archive-sessions` — durable Telegram history export.

Presents the ``archive-sessions`` command: export every Telegram-originated
Hermes session to human-readable Markdown files under an output directory
(``--out``, default ``~/.hermes/archive/telegram``), grouped by project for
sessions whose thread maps to a registry topic, and under ``unmapped/`` for the
rest. All logic lives in :mod:`flightdeck.core.archive`; this module is
argparse + a human/JSON printout of the real totals.

This command is READ-ONLY on the session store: it never writes to, VACUUMs,
or deletes from ``state.db``. Re-running OVERWRITES files deterministically
(full regenerate, keyed by session id) — it never duplicates or corrupts.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..core import archive
from ._theme import escape, make_console, panel

_DEFAULT_OUT = archive.DEFAULT_OUT_DIR
_DEFAULT_DB = archive.DEFAULT_STATE_DB


def _not_implemented() -> int:
    print(
        "archive-sessions: command not recognised. "
        "Try `flightdeck archive-sessions --help`.",
        file=sys.stderr,
    )
    return 2


def cmd_archive_sessions(args: argparse.Namespace) -> int:
    result = archive.archive_sessions(
        out_dir=args.out,
        db_path=args.db,
        registry_path=args.registry,
        project=args.project,
    )

    if args.json:
        payload = {
            "out_dir": args.out,
            "sessions": result.sessions,
            "messages": result.messages,
            "bytes_written": result.bytes_written,
            "files": result.files,
            "by_project": result.by_project,
            "by_thread": result.by_thread,
            "unmapped_threads": result.unmapped_threads,
            "index": result.index_path,
        }
        print(json.dumps(payload, indent=2))
        return 0

    lines = [f"archived {result.sessions} session(s), {result.messages} message(s)",
             f"bytes:  {result.bytes_written:,} across {result.files} file(s)"]
    for proj, n in sorted(result.by_project.items()):
        lines.append(f"  {escape(proj):<12} {n} session(s)")
    if result.unmapped_threads:
        lines.append("non-null unmapped thread ids (no registry owner; reported, not guessed):")
        for t in result.unmapped_threads:
            lines.append(f"  thread {escape(t)}")
    lines.append(f"index: {escape(result.index_path or '')}")
    make_console().print(panel("archive-sessions", "\n".join(lines)))
    return 0


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "archive-sessions",
        help="export Telegram session history to durable readable Markdown",
        epilog=(
            "example: flightdeck archive-sessions\n"
            "         flightdeck archive-sessions --out /path/to/archive\n"
            "         flightdeck archive-sessions --project hscc"
        ),
    )
    p.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        metavar="DIR",
        help=f"output root (default: {_DEFAULT_OUT})",
    )
    p.add_argument(
        "--project",
        default=None,
        metavar="NAME",
        help="only export sessions whose thread maps to this project",
    )
    p.add_argument(
        "--db",
        default=_DEFAULT_DB,
        metavar="PATH",
        help=argparse.SUPPRESS,  # hidden seam for tests; default ~/.hermes/state.db
    )
    p.set_defaults(func=cmd_archive_sessions)


def run(args: argparse.Namespace, registry_path: str) -> int:
    args.registry = registry_path
    func = getattr(args, "func", None)
    if func is None:
        return _not_implemented()
    return func(args)
