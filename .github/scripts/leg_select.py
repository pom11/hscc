#!/usr/bin/env python3
"""Map changed paths to the test legs that have to run (t_23bf61f3).

WHY THIS EXISTS
---------------
``scripts/run_tests.sh`` is the repo's only test gate and, until t_23bf61f3, it
appeared in no workflow — so no PR check could see a red leg and #443/#446
merged with ``hscc-api`` red. Putting the whole 12-leg roster on every PR is not
a free fix either: the suite is ~25 min on a loaded Spark host (measured on
origin/main 540f8a8b: 285 s of that is hscc-bootstrap alone), so a naive
full-matrix job makes every PR slow and every unrelated flake a merge blocker.
This module is the cheap middle: run the legs the diff can actually break.

THE ONE RULE THAT MATTERS: **when in doubt, run more.** Under-selection is what
reproduces the #443/#446 class — a leg that was never run looks exactly like a
leg that passed. So:

  * a path no rule claims selects EVERY leg (fail closed), and the offender is
    printed so a human can extend the map deliberately;
  * every leg directory in the roster selects its own leg — pinned by
    .github/scripts/tests/test_leg_select.py against the live ``DIRS`` array,
    so a leg added to the runner without a map entry fails that test;
  * the map is an OPTIMISATION, not a boundary. The backstop for anything it
    gets wrong is that ``push`` to main runs the FULL suite.

THE CROSS-LEG EDGES ARE GREPPED, NOT IMAGINED (each is a real import or file
read found on origin/main 540f8a8b; if one disappears the pin test still passes
and this table only becomes conservative, which is the safe direction):

  hscc_daemon/ -> + hscc-cluster, hscc-project, hscc-api
      hscc-cluster/tests/test_launch_guard.py:31, test_cluster_up_launch.py:39,
      test_cluster_up_post_boot_hooks.py:35, test_cluster_down_output.py:37
      hscc-project/tests/smoke_dispatch_guard.py:37
      hscc-api/tests/test_cluster_history_parity.py:41,
        test_widget_route_parity_cards.py:21,
        test_cluster_history_cli_parity.py:43, test_profile_root.py:653,
        test_routes_alerts.py:21

  scripts/ -> + hscc_daemon
      hscc_daemon/tests/test_check_repo.py:76 and
      hscc_daemon/tests/test_precommit_address_hook.py:26 both read the real
      ``scripts/address_guard.py`` out of the working tree.

  .github/ and the runner itself -> EVERY leg. A change to the harness can make
      any leg green for the wrong reason, and that is the one class of breakage
      a per-leg map is structurally blind to.

Stdlib only, like the rest of .github/scripts (the runner's own leg runs this
dir's tests under whatever interpreter the host happens to have).

Usage:
  leg_select.py                       # paths on stdin, one per line
  leg_select.py --paths-file FILE     # ...or from a file
  leg_select.py --path P [--path P]   # ...or as argv
  leg_select.py --github-output F     # also append "legs=<space-joined>"

Exit: 0 always (choosing legs is not a verdict); 2 on a usage/harness problem,
because a selector that fails silently would hand CI an empty leg list.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "scripts" / "run_tests.sh"

_DESCRIPTION = "Map changed paths to the test legs that have to run (t_23bf61f3)"

#: The roster, read out of the runner. Duplicating it here is how the two drift.
_DIRS_RE = re.compile(r"^DIRS=\((?P<body>[^)]*)\)", re.MULTILINE)


def roster(runner: Path = RUNNER) -> list[str]:
    """The leg names from scripts/run_tests.sh's DIRS array."""
    text = runner.read_text(encoding="utf-8")
    m = _DIRS_RE.search(text)
    if m is None:
        raise RuntimeError(
            f"{runner} no longer defines the DIRS array this selector reads — "
            "re-read the runner before believing any leg selection is complete"
        )
    legs = m.group("body").split()
    if not legs:
        raise RuntimeError(f"{runner} declares an empty DIRS array")
    return legs


#: Sentinel meaning "every leg in the roster".
ALL = object()

# (prefix, legs). Prefixes are repo-relative and matched on the raw path string,
# so a directory rule is written with its trailing slash and a file rule is the
# whole path. Order does not matter: matches UNION, and any ALL widens to all.
_RULES: tuple[tuple[tuple[str, ...], object], ...] = (
    # The harness. See the docstring: this is the widening that must never be
    # "optimised" away, because it is the only thing covering harness breakage.
    (
        (
            ".github/",
            "scripts/run_tests.sh",
            "scripts/leg_heartbeat.sh",
            "requirements",          # requirements.txt and any requirements-*.txt
            "install/",              # the deployed payload layout
            "conftest.py",
            ".githooks/",            # the commit-time guard wiring
        ),
        ALL,
    ),
    # The daemon library is imported by three other legs (provenance above).
    (("hscc_daemon/",), ("hscc_daemon", "hscc-cluster", "hscc-project", "hscc-api")),
    # scripts/ is read by two hscc_daemon tests (provenance above).
    (("scripts/",), ("scripts", "hscc_daemon")),
    # Repo prose. The legs that merely CITE a fragment in a docstring are not
    # consumers — checked on 540f8a8b: every hscc-api / hscc-roles / hscc_daemon
    # hit for "changelog.d" is a comment or docstring, and
    # hscc-bootstrap/tests/test_unit_name_drift.py:64 skips the dir outright.
    # The leg that actually parses fragments is scripts/tests.
    (
        (
            "changelog.d/",
            "docs/",
            "site/",
            "README",
            "CLAUDE.md",
            "AGENTS.md",
        ),
        ("scripts",),
    ),
    # The Apple clients are Swift and are compiled by app-build-checks.yml, not
    # here — so the naive answer would be "no leg", which is under-selection this
    # map is not allowed to do: scripts/tests reads real files out of both trees
    # (scripts/tests/test_site_landing.py:46 the ios AppIcon png, :49-50
    # ios-app/scripts/publish_pages.sh and macos-app/scripts/publish_macos.sh,
    # :305-347 which EXECUTES the mac publisher). Grepping those two dirs across
    # every leg's tests on 540f8a8b found scripts/tests as the only Python reader,
    # so an Apple-client change runs the repo/publisher leg and nothing else.
    # That is the whole point of the map: a Swift-only PR costs seconds, not the
    # 25-minute roster it would otherwise pay.
    (
        ("macos-app/", "ios-app/"),
        ("scripts",),
    ),
)


def _per_leg_rules(legs: list[str]) -> list[tuple[tuple[str, ...], object]]:
    """Every leg dir selects its own leg. Generated from the roster, so a new leg
    is covered the moment it is added to DIRS (and pinned by the test)."""
    return [((f"{leg}/",), (leg,)) for leg in legs]


def select_legs(paths: list[str], legs: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Return (legs to run, paths that forced the full suite).

    ``legs`` defaults to the runner's roster; the tests pass their own roster so
    the widening behaviour can be exercised without touching the real array.
    """
    all_legs = list(legs if legs is not None else roster())
    rules = list(_RULES) + _per_leg_rules(all_legs)

    chosen: set[str] = set()
    widen: list[str] = []
    full = False

    for raw in paths:
        path = raw.strip().replace("\\", "/")
        if not path:
            continue
        claimed = False
        for prefixes, target in rules:
            for prefix in prefixes:
                if path == prefix or path.startswith(prefix):
                    claimed = True
                    if target is ALL:
                        full = True
                    elif isinstance(target, tuple):
                        chosen.update(t for t in target if t in all_legs)
                    break
        if not claimed:
            # Unattributable change: we cannot say what it breaks, so run all.
            widen.append(path)
            full = True

    result = all_legs if full or not chosen else sorted(chosen, key=all_legs.index)
    # `not chosen` is NOT the widening mechanism — it is the empty-diff case: a
    # caller that claimed nothing (an empty path list, a diff of blank lines) must
    # run everything rather than report green on zero tests. An unclaimed path
    # among claimed ones is handled by `full` above, and
    # test_a_single_unattributable_path_among_known_ones_widens_everything pins
    # that pair — the empty case alone hides a missing `full=True`, which is how
    # this line read as covered while nothing claimed the path.
    return result, widen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=_DESCRIPTION)
    ap.add_argument("--paths-file", help="file of changed paths, one per line")
    ap.add_argument("--path", action="append", default=[], help="a changed path (repeatable)")
    ap.add_argument("--github-output", help="append legs=<...> to this GITHUB_OUTPUT file")
    ap.add_argument("--leg", action="append", default=[], help="override the roster (tests only)")
    args = ap.parse_args(argv)

    try:
        all_legs = args.leg or roster()
    except (OSError, RuntimeError) as exc:
        print(f"leg_select: {exc}", file=sys.stderr)
        return 2

    paths: list[str] = list(args.path)
    if args.paths_file:
        try:
            paths.extend(Path(args.paths_file).read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            print(f"leg_select: cannot read {args.paths_file}: {exc}", file=sys.stderr)
            return 2
    if not args.paths_file and not args.path:
        if sys.stdin.isatty():
            print("leg_select: no paths on stdin and none on argv", file=sys.stderr)
            return 2
        paths.extend(sys.stdin.read().splitlines())

    legs, widen = select_legs(paths, all_legs)

    # Human-readable, always: a job log has to answer "why only these legs?"
    print(f"changed paths considered: {len([p for p in paths if p.strip()])}")
    for path in widen:
        print(f"  no map rule for {path!r} -> widening to the FULL suite")
    if len(legs) == len(all_legs):
        print(f"legs: FULL SUITE ({len(legs)})" + (" — widened" if widen else ""))
    else:
        print(f"legs: SELECTED {len(legs)}/{len(all_legs)} -> {' '.join(legs)}")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"legs={' '.join(legs)}\n")
            fh.write(f"leg_count={len(legs)}\n")
            fh.write(f"full_suite={'true' if len(legs) == len(all_legs) else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
