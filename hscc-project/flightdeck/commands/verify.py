"""verify.py — `flightdeck verify <project|--all>`.

Runs each project's registry ``verify`` command, reports PASS/FAIL with
duration, and RECORDS the result + timestamp to ``~/.flightdeck/state.yaml``
so other commands (standup) can show "last verified 3 days ago".

A project with no verify command is reported as "no verify configured" —
distinct from both pass and fail, never skipped silently, never counted as
passing.

Presentation only. All logic (running the command, measuring duration,
recording state) lives in :mod:`flightdeck.core.verify`, which is
independently testable against an injected runner.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..core import registry, verify
from ..core.verify import FAIL, NO_VERIFY, PASS


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "verify",
        help="run the registry verify command, record the result",
        epilog="example: flightdeck verify --all",
    )
    p.add_argument(
        "project",
        nargs="?",
        help="project name (omit to run every project that has a verify command)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="verify every project that has a verify command and summarise",
    )
    p.set_defaults(func=_dispatch_verify)
    return p


def _dispatch_verify(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Route to single or --all based on the arguments."""
    if args.all:
        return _cmd_all(args, projects)
    return _cmd_single(args, projects)


def _fmt_duration(d: float) -> str:
    if d < 1:
        return f"{d * 1000:.0f}ms"
    return f"{d:.1f}s"


def _render_error(error: str) -> None:
    """Print a FAIL's hint indented, one line per non-blank line."""
    for line in error.splitlines():
        print(f"  {line}")


def _cmd_single(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Run one named project's verify, record it, and report pass/fail.

    The project comes from the ``project`` positional when given, otherwise it
    is auto-detected from the cwd (the registry project whose repo contains the
    current directory); when neither is available the command says so and
    exits 2, exactly as before. An explicit ``--all`` is routed before this is
    reached, so it always wins over detection.
    """
    project, detected = registry.resolve_project_arg(
        projects, args.project,
        cwd=getattr(args, "cwd", None),
        _print=lambda line: print(line, file=sys.stderr),
    )
    if not project:
        print(
            "verify: specify a project name or use --all to verify every "
            "project that has a verify command.",
            file=sys.stderr,
        )
        return 2
    try:
        proj = registry.get_project(project, path=args.registry)
    except registry.ProjectNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = verify.run_verify(proj, _run=args.run)
    verify.record_result(proj.name, result, path=args.state)

    if args.json:
        print(
            json.dumps(
                {
                    "project": proj.name,
                    "status": result.status,
                    "duration_s": result.duration_s,
                }
            )
        )
        return _single_exit(result.status)

    if result.status == NO_VERIFY:
        print(f"{proj.name}: no verify configured")
        return 0
    if result.status == PASS:
        print(f"{proj.name}: PASS ({_fmt_duration(result.duration_s)})")
        return 0
    # FAIL
    print(f"{proj.name}: FAIL ({_fmt_duration(result.duration_s)})")
    if result.error:
        _render_error(result.error)
    return 1


def _single_exit(status: str) -> int:
    """Exit code for a single-project result: 1 iff FAIL."""
    return 1 if status == FAIL else 0


def _cmd_all(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Run every project's verify (where configured), record, and summarise.

    A project without a verify command is reported as "no verify configured"
    without running anything — never silently skipped, never counted as
    passing. Any FAIL makes the command exit 1.
    """
    if not projects:
        print("no projects in the registry.")
        return 0

    outcomes: list[dict] = []
    for proj in projects:
        result = verify.run_verify(proj, _run=args.run)
        verify.record_result(proj.name, result, path=args.state)
        outcomes.append(
            {"project": proj.name, "status": result.status, "duration_s": result.duration_s}
        )

    if args.json:
        print(json.dumps(outcomes))
        return 1 if any(o["status"] == FAIL for o in outcomes) else 0

    passed = failed = no_verify = 0
    for o in sorted(outcomes, key=lambda x: x["project"]):
        name = o["project"]
        status = o["status"]
        dur = o["duration_s"]
        if status == PASS:
            passed += 1
            print(f"  {name:<24} PASS ({_fmt_duration(dur)})")
        elif status == FAIL:
            failed += 1
            print(f"  {name:<24} FAIL ({_fmt_duration(dur)})")
        else:
            no_verify += 1
            print(f"  {name:<24} no verify configured")

    summary = f"{passed} passed, {failed} failed, {no_verify} no verify configured"
    if failed:
        summary += "  -- some projects are FAILING"
        print(summary)
        return 1
    print(summary)
    return 0


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run the verify command.

    Threads the injectable runner (``args.run``) and the state path
    (``args.state``) so core calls are stubbable in tests.
    """
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.state = getattr(args, "state", None)
    args.cwd = getattr(args, "cwd", None)
    projects = registry.load_registry(registry_path)
    return _dispatch_verify(args, projects)
