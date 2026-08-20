"""release.py — `flightdeck release <project> <version>` (dry-run by default).

The command checks every precondition and then prints the ORDERED plan of what
a real release would do — bump, commit, tag, push, gh release, install, verify
installed — and exits 0 WITHOUT doing any of it.

With ``--apply`` it executes the release (guarded by the same preconditions):
bump VERSION, commit the bump, tag the release, push, create the GitHub
release, run the registry-declared install command, and verify the INSTALLED
version matches the released version. Each step is reported as it runs and the
release STOPS at the first failure, reporting exactly which step failed —
nothing is rolled back silently.

The install + post-install verification is the "merged is not live" guard:

* if the registry declares an ``install_cmd``, it is run; the installed
  version is then compared to the released version — a mismatch is a FAILURE
* if the registry declares no ``install_cmd``, install is skipped with a
  plain message but the result is UNVERIFIED (never a clean success)

Anything short of VERIFIED returns a non-zero exit and prints to stderr, so an
unverified or failed release can never be mistaken for a clean one.

All logic lives in :mod:`flightdeck.core.release`; this module is presentation
only, per the commands/core split. It reuses the existing registry reader and
core.verify — never reimplemented.

PRECONDITIONS (in order; any failure refuses the release with a clear reason
and a non-zero exit):

1. the working tree is dirty              -> refuse
2. the current branch is not main         -> refuse
3. the registry verify command fails      -> refuse (NO OVERRIDE FLAG)
4. CHANGELOG.md has no section for version -> refuse + print the skeleton
5. the version is not greater than VERSION -> refuse

No git write happens unless ``--apply`` is given; without it, not one mutation
occurs.
"""

from __future__ import annotations

import argparse
import sys

from ..core import registry, release
from ..core.release import DEFAULT_CHANGELOG

# The ordered steps a real release performs. This command only PRINTS them.
PLAN_STEPS = [
    "bump VERSION",
    "commit the version bump",
    "tag the release (annotated)",
    "push the branch and tag",
    "create the GitHub release",
    "install the release",
    "verify the installed version is live",
]


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "release",
        help="check release preconditions and print the dry-run plan (does nothing)",
        epilog="example: flightdeck release flightdeck 0.6.0 --apply",
    )
    p.add_argument("project", help="project name from the registry")
    p.add_argument("version", help="target version, e.g. 1.9.0 or v1.9.0")
    p.add_argument(
        "--apply",
        action="store_true",
        help="execute the release (this card: bump VERSION), gated by the "
        "same preconditions; without it the command only prints the plan",
    )
    return p


def _print_problems(problems: list[release.Problem], version: str) -> None:
    """Print every refused precondition; print the skeleton if changelog missing."""
    print("release refused:", file=sys.stderr)
    for prob in problems:
        print(f"  - {prob.code}: {prob.message}", file=sys.stderr)
    if any(p.code == "missing-changelog" for p in problems):
        print(f"\nTo release, add this section to {DEFAULT_CHANGELOG}:\n", file=sys.stderr)
        print(release.changelog_skeleton(version), file=sys.stderr)


def _print_plan(project, version: str) -> None:
    """Print the ordered dry-run plan for the real release."""
    print(f"release plan for {project.name} {version} (dry run — nothing executed):")
    for i, step in enumerate(PLAN_STEPS, 1):
        print(f"  {i}. {step}")


def _print_apply(project, completed: list[str], version: str,
                  files_written: tuple[str, ...] = ()) -> None:
    """Print each completed step and the version sources the bump wrote.

    ``files_written`` lists every repo-root file the bump updated (e.g.
    ``("VERSION", "pyproject.toml")``) so the operator sees exactly which
    version sources were kept in lockstep — the v0.2.0 incident was a package
    released against a stale VERSION/pyproject pair.
    """
    for step in completed:
        print(f"  released step: {step}")
    version_file = project.version_file or "VERSION"
    if files_written:
        names = ", ".join(files_written)
        print(f"bumped {project.name} {version_file} to {version.lstrip('v')} "
              f"(wrote {names})")
    else:
        print(f"bumped {project.name} {version_file} to {version.lstrip('v')}")


def _print_verify(project, outcome: release.InstallOutcome) -> None:
    """Report the install + post-install verification outcome.

    Anything short of a VERIFIED outcome is printed to stderr with a non-zero
    exit so an unverified or failed release can never read as a clean success:

    * no install command declared -> plain \"install skipped\" message, but the
      result is UNVERIFIED — never a clean success
    * install failed -> FAILED, loud, non-zero
    * installed version differs -> FAILED (merged is not live), loud, non-zero
    * installed version could not be re-read -> UNVERIFIED, non-zero
    * installed version matches -> VERIFIED, clean, with BOTH versions
    """
    if not outcome.install_ran:
        # no install command declared — say so plainly, then UNVERIFIED
        print(
            f"no install command declared for {project.name} — install skipped",
            file=sys.stderr,
        )
        print(
            f"UNVERIFIED: cannot confirm the installed version is "
            f"{outcome.released_version}",
            file=sys.stderr,
        )
        return

    if outcome.status == release.VERIFIED:
        print(
            f"verified: installed version {outcome.installed_version} matches "
            f"released {outcome.released_version}"
        )
        return

    # FAILED or UNVERIFIED — surface loudly on stderr, never as a clean success.
    marker = "FAILED" if outcome.status == release.FAILED else "UNVERIFIED"
    print(f"{marker}: {outcome.detail}", file=sys.stderr)
    if outcome.installed_version is not None:
        print(
            f"  installed version reported: {outcome.installed_version!r}",
            file=sys.stderr,
        )
    if outcome.status == release.FAILED and outcome.installed_version is not None:
        print(
            f"  merged is not live: {project.name} is running "
            f"{outcome.installed_version!r}, not the released "
            f"{outcome.released_version!r}",
            file=sys.stderr,
        )


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: check preconditions, then plan or execute the release.

    With ``--apply``, after preconditions pass, execute the release steps and
    report each as it runs, then report the install + post-install verification
    outcome. Without it, only the dry-run plan is printed. Any refused
    precondition is non-zero whether or not ``--apply`` is set.

    The command returns 0 only when the release VERIFIED the installed version
    matches the released version. An UNVERIFIED or FAILED verification returns
    non-zero and prints to stderr — an unverified release must not print a
    clean result.

    If a step fails mid-release, the command stops, reports exactly which step
    failed (on stderr), and returns non-zero — no later step is attempted and
    nothing is silently rolled back.
    """
    try:
        proj = registry.get_project(args.project, path=registry_path)
    except registry.ProjectNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _run = getattr(args, "run", None)
    problems = release.preconditions(proj, args.version, _run=_run)

    if problems:
        _print_problems(problems, args.version)
        return 1

    if getattr(args, "apply", False):
        try:
            result = release.execute(proj, args.version, _run=_run)
        except release.ReleaseStepError as exc:
            print(
                f"release stopped at step {exc.step!r}: {exc}",
                file=sys.stderr,
            )
            print(
                "no further release steps were issued; nothing was rolled back.",
                file=sys.stderr,
            )
            return 1
        _print_apply(proj, result.completed, args.version, result.files_written)
        _print_verify(proj, result.outcome)
        return 0 if result.verified else 1
    else:
        _print_plan(proj, args.version)
    return 0
