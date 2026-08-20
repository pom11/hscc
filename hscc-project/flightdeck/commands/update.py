"""update.py — `flightdeck update` — explicitly update the tool itself.

Gap 2 step 1 from ``docs/BRAINSTORM-what-is-missing.md``. The brainstorm
settled the self-update question explicitly: an explicit, on-demand command,
``--apply``-gated, no background daemon. This command is the natural sibling of
``project pull`` / ``project push`` — same read-only-by-default rule, same
honest reporting, same reuse of :mod:`flightdeck.core.git_state`.

Dry-run by default (``flightdeck update``): it detects HOW flightdeck is
installed, compares the installed commit/version to the upstream, and prints:

    flightdeck update
      installed  0.6.0  (commit 7d9b83d)
      upstream   0.7.0  (commit a1b2c3d)   # or "already up to date"
      would update this install by fast-forwarding the installed source clone
      (nothing performed; pass --apply to update)

``--apply`` actually performs the update, then VERIFIES the running source now
matches the intended upstream commit — the "merged is not live" guard from
``release``. A failed or unverified update is reported loudly with a non-zero
exit, never as a clean success.

Install mechanisms handled:

* **editable** (``pip install -e`` from a local clone) — update by
  ``git pull --ff-only`` on that clone, using the same safe path ``project
  pull`` uses (:func:`git_state.pull_project`). Refuses to touch a dirty tree,
  a non-default branch, or diverged history.
* **git+ non-editable** (``pip install git+...``) — no local source to pull, so
  update by ``pip install --upgrade --force-reinstall git+<url>``.
* **non-git / not-installed / unknown** — cannot be self-updated; reported
  honestly, and nothing is guessed.

All logic lives in :mod:`flightdeck.core.self_update`; this module is argparse +
presentation only, per the commands/core split.
"""

from __future__ import annotations

import argparse

from ..core import self_update


def _short(sha: str | None) -> str:
    """Abbreviate a full sha to 7 chars, or '?' when unknown."""
    return (sha or "?")[:7]


def _render_plan(plan: dict) -> str:
    """Human plan for the editable case."""
    status = plan["status"]
    installed_v = plan.get("installed_version") or "?"
    installed_s = _short(plan.get("installed_sha"))
    up_v = plan.get("upstream_version") or "?"
    up_s = _short(plan.get("upstream_sha")) if plan.get("upstream_sha") else "?"

    lines = []
    lines.append(f"  installed  {installed_v}  (commit {installed_s})")

    if status == self_update.UP_TO_DATE:
        lines.append("  already up to date")
        lines.append("  (nothing performed)")
        return "\n".join(lines)

    if status == self_update.NO_REMOTE:
        lines.append(f"  UNKNOWN: {plan['detail']}")
        lines.append("  cannot compare to an upstream; not claiming up to date")
        return "\n".join(lines)

    if status == self_update.CANNOT_REACH:
        lines.append(f"  UNKNOWN: {plan['detail']}")
        lines.append("  cannot confirm an update is (or isn't) available")
        return "\n".join(lines)

    if status == self_update.NOT_GIT:
        lines.append(f"  UNKNOWN: {plan['detail']}")
        lines.append("  nothing to update against")
        return "\n".join(lines)

    # update_available
    lines.append(f"  upstream   {up_v}  (commit {up_s})")
    lines.append("  would update this install by fast-forwarding the installed")
    lines.append("  source clone (nothing performed; pass --apply to update)")
    return "\n".join(lines)


def _render_apply_editable(plan: dict, result: dict) -> str:
    """Report an attempted editable update + its verification."""
    lines = []
    if result["status"] in (self_update.APPLIED, self_update.UP_TO_DATE):
        verb = "updated to" if result["status"] == self_update.APPLIED else "already at"
        target = plan.get("upstream_sha") or "?"
        lines.append(f"  installed source {verb} commit {_short(target)}")
    else:
        lines.append(f"  update did NOT happen: {result['detail']}")

    if result.get("verified"):
        lines.append(
            f"  verified: running source now at commit {_short(result.get('installed_sha'))}"
        )
    else:
        lines.append(f"  UNVERIFIED: {result['detail']}")
    return "\n".join(lines)


def _render_apply_git(result: dict, url: str) -> str:
    """Report an attempted non-editable pip update."""
    lines = []
    if result["status"] == "applied":
        lines.append(f"  installed from {url} (pip exit 0)")
        lines.append("  UNVERIFIED: cannot re-read installed version from a git+")
        lines.append("  pip path; run `flightdeck` again to confirm it is live")
    else:
        lines.append(f"  FAILED: {result['detail']}")
    return "\n".join(lines)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: plan the update, or apply it behind ``--apply``."""
    _run = getattr(args, "run", None)
    src = self_update.installed_source()
    mechanism = src["mechanism"]
    source = src["source"]

    print("flightdeck update")
    print(f"  install mechanism: {mechanism}" + (f" ({source})" if source else ""))

    if mechanism in ("non-git", "not-installed", "unknown"):
        print("  cannot self-update: flightdeck was not installed from a git")
        print("  source this tool can pull (pip install a package from an index,")
        print("  or the distribution could not be found in this environment).")
        print("  to update, reinstall manually from the flightdeck git repo.")
        return 1 if mechanism in ("not-installed", "unknown") else 0

    if mechanism == "git":
        # Non-editable git+ install: no local clone to pull. Report the source
        # and, on --apply, reinstall from the recorded git URL.
        if getattr(args, "apply", False):
            result = self_update.apply_git(source, _run=_run)
            print(_render_apply_git(result, source))
            return 0 if result["status"] == "applied" else 1
        print("  installed via pip git+; update mechanism:")
        print(f"    pip install --upgrade --force-reinstall git+{source}")
        print("  (nothing performed; pass --apply to update)")
        return 0

    # mechanism == "editable": local clone we can pull --ff-only.
    plan = self_update.plan(source, _run=_run)
    if getattr(args, "apply", False):
        # Only ever run the mutation when the dry-run says there is an actual
        # update to apply — never when up to date or uncheckable.
        if plan["status"] != self_update.UPDATE_AVAILABLE:
            print(_render_plan(plan))
            return 0
        result = self_update.apply_source(source, _run=_run)
        print(_render_apply_editable(plan, result))
        return 0 if result.get("verified") else 1

    print(_render_plan(plan))
    return 0 if plan["status"] in (
        self_update.UP_TO_DATE, self_update.UPDATE_AVAILABLE,
    ) else 1


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "update",
        help="check the installed flightdeck against its git upstream, or update it (--apply-gated, no daemon)",
        epilog="example: flightdeck update   (dry-run plan)   |   flightdeck update --apply   (actually update)",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="perform the update (mutating; without it the command only prints the plan)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and touch nothing (alias of simply omitting --apply)",
    )
    return p
