"""map_sessions.py — `flightdeck map-sessions` — propose owners for unmapped history.

Presents the ``map-sessions`` command: PROPOSE a project owner for every
Telegram session with ``thread_id IS NULL`` (the ones :mod:`flightdeck.core.archive`
files under ``unmapped/``). A proposal is argued per session — session id, msgs,
dates, proposed project, **method** (``repo-path`` / ``model`` / ``none``),
confidence, and the EVIDENCE that drove it — and written as both Markdown (human
review) and JSON (tooling) under ``~/.hermes/archive/telegram/proposals``.

Two passes (see :mod:`flightdeck.core.map_sessions`): deterministic repo-path
first (free, high-confidence), then a bounded-sample ask to the orchestrator for
the remainder that FAILS CLOSED to ``unknown``/``none`` when no transport is
reachable — a wrong attribution is worse than none.

The default run is READ-ONLY on ``~/.hermes/state.db`` and writes NO state: it
only writes the proposal report files (the deliverable). ``--apply`` is a
separate, explicit step that persists the machine mapping + a changelog and is
reversible (files only). This module is argparse + the human/JSON report of the
real totals; all logic lives in core.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time

from ..core import map_sessions

_DEFAULT_OUT = map_sessions.DEFAULT_OUT_DIR
_DEFAULT_DB = map_sessions.DEFAULT_STATE_DB


def _not_implemented() -> int:
    print(
        "map-sessions: command not recognised. "
        "Try `flightdeck map-sessions --help`.",
        file=sys.stderr,
    )
    return 2


def _resolve_ask_module(spec: str) -> map_sessions.AskFn:
    """Load ``module:function`` as the orchestrator ask callback.

    ``module`` is imported (relative to ``flightdeck.commands`` when it is a
    bare name, else ``package.module``), and ``function`` must be callable with
    ``(sample_text, session_id)``. A broken spec is a usage error reported up
    front — never a silent fallback to ``none``, so an operator who asked for a
    model pass knows it did not run.
    """
    mod_path, _, func_name = spec.partition(":")
    if not func_name:
        raise ValueError(
            f"--ask-module must be 'module:function', got {spec!r}"
        )
    module = importlib.import_module(mod_path)
    fn = getattr(module, func_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f"{mod_path}:{func_name} is not callable")
    return fn


def cmd_map_sessions(args: argparse.Namespace) -> int:
    result = map_sessions.propose_owners(
        db_path=args.db,
        registry_path=args.registry,
        ask=getattr(args, "ask", None),
    )

    out_dir = args.out
    ts = time.strftime("%Y%m%d-%H%M%S")
    md_path, json_path = map_sessions.write_proposal(result, out_dir, ts)

    if getattr(args, "json", False):
        payload = {
            "out_dir": out_dir,
            "proposal_md": md_path,
            "proposal_json": json_path,
            "total": result.total,
            "resolved_repo_path": result.resolved_repo_path,
            "resolved_model": result.resolved_model,
            "unknown": result.unknown,
            "deterministic_by_project": result.deterministic_by_project,
            "model_by_project": result.model_by_project,
            "proposals": [
                {
                    "session_id": p.session_id,
                    "msgs": p.msgs,
                    "started_at": p.started_at,
                    "ended_at": p.ended_at,
                    "project": p.project,
                    "method": p.method,
                    "confidence": p.confidence,
                    "evidence": p.evidence,
                }
                for p in result.proposals
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0

    print(f"proposed owners for {result.total} unmapped session(s)")
    print(f"  resolved deterministic (repo-path): {result.resolved_repo_path}")
    for proj, n in sorted(result.deterministic_by_project.items()):
        print(f"    {proj:<12} {n}")
    print(f"  resolved by model: {result.resolved_model}")
    for proj, n in sorted(result.model_by_project.items()):
        print(f"    {proj:<12} {n}")
    print(f"  left unknown: {result.unknown}")
    print(f"proposal: {md_path}")
    print(f"json:     {json_path}")

    if args.apply:
        map_path, change_path = map_sessions.apply_mapping(result, out_dir, ts)
        print(f"applied (reversible, files-only): {map_path}")
        print(f"changelog: {change_path}")

    return 0


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "map-sessions",
        help="propose a project owner for each unmapped Telegram session (deterministic first, orchestrator for the rest)",
        epilog=(
            "example: flightdeck map-sessions\n"
            "         flightdeck map-sessions --json\n"
            "         flightdeck map-sessions --apply\n"
            "         flightdeck map-sessions --ask-module mypkg.ask:call"
        ),
    )
    p.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        metavar="DIR",
        help=f"proposal output root (default: {_DEFAULT_OUT})",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="EXPLICIT, reversible apply: persist the mapping + a changelog "
        "(files only, never state.db). Omit for a read-only proposal. "
        "--json is the top-level global flag.",
    )
    p.add_argument(
        "--ask-module",
        default=None,
        metavar="module:function",
        help="orchestrator ask callback 'module:function' taking "
        "(bounded_sample, session_id) -> a project name, 'unknown', or None. "
        "Default fails closed to unknown (method none) when no transport is set.",
    )
    p.add_argument(
        "--db",
        default=_DEFAULT_DB,
        metavar="PATH",
        help=argparse.SUPPRESS,  # hidden seam for tests; default ~/.hermes/state.db
    )
    p.set_defaults(func=cmd_map_sessions)


def run(args: argparse.Namespace, registry_path: str) -> int:
    args.registry = registry_path
    args.ask = getattr(args, "ask", None)
    ask_module = getattr(args, "ask_module", None) or getattr(args, "ask-module", None)
    if args.ask is None and ask_module:
        try:
            args.ask = _resolve_ask_module(ask_module)  # type: ignore[assignment]
        except (ImportError, ValueError, AttributeError) as exc:
            print(
                f"map-sessions: could not load --ask-module {ask_module!r}: {exc}",
                file=sys.stderr,
            )
            return 2
    func = getattr(args, "func", None)
    if func is None:
        return _not_implemented()
    return func(args)
