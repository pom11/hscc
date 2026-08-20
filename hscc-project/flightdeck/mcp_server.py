"""mcp_server.py — a THIN MCP adapter over flightdeck's read-only commands.

Exposes the read-only flightdeck report commands to any Model Context
Protocol client (Claude Code, Claude Desktop, ...) over stdio, so the fleet
can be inspected directly instead of only from a human at a terminal.

ARCHITECTURE: this is a thin adapter, NOT a reimplementation. Every tool
builds an ``argparse.Namespace`` and calls the SAME command entry point the
CLI uses (the ``run(args, registry_path)`` of ``flightdeck.commands.<module>``),
then returns whatever that command printed. It never shells out to the
``flightdeck`` binary and never reimplements any card/registry/roadmap logic —
the equality between the MCP payload and the CLI output is guaranteed by
construction, because it *is* the same code path.

Every tool is READ-ONLY: it computes a report and returns text. Nothing here
mutates the registry, a board, git, or the filesystem. In particular
``flightdeck_reconcile_preview`` can only ever show reconcile's DRY-RUN plan —
the mutating ``--apply`` path is never reachable from MCP.

Errors are returned as clear plain-text messages (e.g. ``no registry at
~/.flightdeck/registry.yaml``), never a raw traceback: whatever comes back is
what the client's model reads.

Mirrors the shape of ~/.hermes-tg/mcp_server.py: a module-level ``FastMCP``
instance, ``@mcp.tool()``-decorated functions, and a ``main()`` that runs it
over stdio.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os

try:  # mcp < 2
    from mcp.server.fastmcp import FastMCP
except ImportError:  # mcp >= 2.0 renamed it; same tool()/run() surface
    from mcp.server.mcpserver import MCPServer as FastMCP

from .core import registry

mcp = FastMCP("flightdeck")

# ── Injectable handles ──────────────────────────────────────────────────── #
# Each defaults to None → the underlying command's real production behaviour
# (read the live board, run real git subprocesses, etc.). Tests monkeypatch
# these attributes so the adapter stays a thin wrapper around the same command
# code path while no test touches the board, git, the network, or real time.
# They are attached to every ``args`` Namespace before dispatch so whichever
# seam a command reads is covered.
_run = None            # git runner (standup/qa/reconcile/review/start/decompose/ingest)
_run_verify = None     # verify-command runner (qa)
_client = None         # telegram client (doctor/project/decompose/ingest/message)
_kdb = None            # kanban_db provider (reconcile/start)
_now = None            # clock (standup/qa review/decompose)
_now_fn = None         # callable clock (review baseline)
_sleep = None          # loop pause (standup/qa/decompose)
_state = None          # verify state file (standup/qa)
_listdir = None        # worktree inspection (standup)
_config_path = None    # hermes config (standup/start fleet cap)
_cards = None          # injected card list (overrides the board read)
_boards = None         # injected board snapshot (doctor)
_topics = None         # injected topic snapshot (doctor)
_repo_check = None     # injected repo checker (doctor)
_repo_root = None      # lint line-count base (lint)
_close_card = None     # close-card callable (review --apply)
_baseline = None       # test-baseline path (review gate)
_ask = None            # orchestrator ask seam (decompose/ingest)
_locate_refs = None    # repo ref-locator (decompose)
_read_milestone = None # milestone-item reader (decompose)
_list_cards = None     # board read seam (decompose prompt render)
_templates_home = None # template dir (decompose)
_read_refs = None      # skill/context read seam (ingest)
_read = None           # repo file read seam (ingest)
_context_dir = None    # context staging dir (ingest)
_list_archived_cards = None  # archived-board read seam (legacy-cards)
_events = None         # per-card event reader (why/metrics/report)


def _registry_path() -> str:
    """The registry file to read. ``FLIGHTDECK_REGISTRY`` overrides the default.

    The command modules read the registry through ``registry.load_registry``,
    which is itself the injectable seam tests patch — so this only selects which
    default file the real CLI-equivalent path reads when nothing is patched.
    """
    return os.environ.get("FLIGHTDECK_REGISTRY", registry.DEFAULT_REGISTRY)


def _run_cmd(module, args: argparse.Namespace) -> str:
    """Run a command module's CLI entry and return its output as one string.

    Attaches every module-level injectable handle (``_run``, ``_cards``, ...) to
    ``args`` first, so whichever seam the underlying command reads is covered by
    the injected fixture — in production they are all None, meaning the command
    behaves exactly as the CLI does. Captures BOTH stdout and stderr so the
    model sees the report AND anything the command wrote to stderr (e.g.
    ``doctor: all clear`` or an ``error: no project named ...``). Exceptions
    are caught and turned into a clear message — never a raw traceback.
    """
    registry_path = _registry_path()
    args.registry = registry_path
    _attach_handles(args)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            module.run(args, registry_path)
    except Exception as exc:  # noqa: BLE001 - surface as text, not a traceback
        return f"flightdeck error: {type(exc).__name__}: {exc}"
    parts = out.getvalue()
    if err.getvalue():
        parts = parts.rstrip("\n") + ("\n" if parts else "") + err.getvalue()
    return parts.rstrip("\n") or "(no output)"


def _attach_handles(args: argparse.Namespace) -> None:
    """Attach every injectable handle to ``args`` (unless already set).

    Only sets attributes the caller has not already provided on ``args``, so a
    tool's explicit kwargs always win. Each is read from the module-level
    ``_<name>`` attribute, which tests monkeypatch; they default to None (the
    real CLI behaviour).
    """
    for name in ("run", "run_verify", "client", "kdb", "now", "now_fn",
                 "sleep", "state", "listdir", "config_path", "cards", "boards",
                 "topics", "repo_check", "repo_root", "close_card", "baseline",
                 "ask", "locate_refs", "read_milestone", "list_cards",
                 "templates_home", "read_refs", "read", "context_dir",
                 "list_archived_cards", "events"):
        if getattr(args, name, _MISSING) is _MISSING:
            setattr(args, name, globals().get(f"_{name}"))


_MISSING = object()


# --------------------------------------------------------------------------- #
# Tools — each is a thin adapter over the same command the CLI runs.
# --------------------------------------------------------------------------- #

@mcp.tool()
def flightdeck_standup() -> str:
    """Read-only daily fleet digest: NEEDS YOU / FAILING / STALE / RUNNING / DRIFT sections plus the coverage footer and fleet in-flight line. Mutates nothing."""
    from .commands import standup

    return _run_cmd(
        standup,
        _namespace(json=False, watch=False, max_fleet=3),
    )


@mcp.tool()
def flightdeck_qa(project: str | None = None) -> str:
    """Read-only manual-testing queue for one project (or all when omitted). Returns the cards you must test by hand. Mutates nothing."""
    from .commands import qa

    return _run_cmd(
        qa,
        _namespace(project=project, json=False, watch=False, notify=False, func=qa.cmd_qa),
    )


@mcp.tool()
def flightdeck_doctor() -> str:
    """Read-only trust self-check: flags projects it cannot read or verify. Returns per-project check rows. Mutates nothing."""
    from .commands import doctor

    return _run_cmd(doctor, _namespace(json=False))


@mcp.tool()
def flightdeck_roadmap_progress(project: str | None = None) -> str:
    """Read-only per-milestone card progress for one project (or all when omitted): items + cards per milestone. Mutates nothing."""
    from .commands import roadmap

    return _run_cmd(
        roadmap,
        _namespace(project=project, json=False, func=roadmap.cmd_progress),
    )


@mcp.tool()
def flightdeck_roadmap_show(project: str | None = None) -> str:
    """Read-only Now/Next/Later roadmap view for one project (or all when omitted). Mutates nothing."""
    from .commands import roadmap

    return _run_cmd(
        roadmap,
        _namespace(project=project, json=False, func=roadmap.cmd_show),
    )


@mcp.tool()
def flightdeck_lint_cards(board: str | None = None) -> str:
    """Read-only card-quality check: flags cards missing VERIFY/acceptance lines, incl. the CRITICAL main-tree check. Mutates nothing."""
    from .commands import lint

    return _run_cmd(
        lint,
        _namespace(board=board, json=False, repo_root=None),
    )


@mcp.tool()
def flightdeck_reconcile_preview() -> str:
    """Read-only preview of reconcile's DRY-RUN plan (close/archive/stale). The apply path is NOT exposed over MCP. Mutates nothing."""
    from .commands import reconcile
    from .core import kanban

    return _run_cmd(
        reconcile,
        _namespace(json=False, apply=False, days=kanban.DEFAULT_DEAD_DAYS),
    )


@mcp.tool()
def flightdeck_legacy_cards(include_archived_boards: bool = False) -> str:
    """Read-only list of unmanaged cards on orphan/archived boards for review (card id, board, status, title, body excerpt, workspace path, and a suggested target project only when workspace_path resolves to one). Mutates nothing."""
    from .commands import legacy

    return _run_cmd(
        legacy,
        _namespace(
            json=False,
            include_archived_boards=include_archived_boards,
            func=legacy.cmd_legacy_cards,
        ),
    )


@mcp.tool()
def flightdeck_list_projects() -> str:
    """Read-only registry listing: name, repo, board, topic (and health). Mutates nothing."""
    from .commands import project

    return _run_cmd(
        project,
        _namespace(json=False, func=project.cmd_list),
    )


@mcp.tool()
def flightdeck_why(card_id: str) -> str:
    """Read-only one-card story: identity, timing, branch, workspace, and a one-line verdict (STARVED / WORKING / AWAITING REVIEW / LANDED / NO BRANCH). Read-only; never mutates."""
    from .commands import why

    return _run_cmd(
        why,
        _namespace(card_id=card_id, json=False, func=why.cmd_why),
    )


@mcp.tool()
def flightdeck_metrics(project: str | None = None, since: str = "24h") -> str:
    """Read-only fleet/project quality metrics over a window: first-time-pass, stall, review latency, throughput, rework (each states its sample size; never a misleading 0%/100%). Read-only; never mutates."""
    from .commands import metrics

    return _run_cmd(
        metrics,
        _namespace(project=project, since=since, json=False),
    )


@mcp.tool()
def flightdeck_topics_list() -> str:
    """Read-only list of every Telegram topic: id, name, mapped project. Read-only; never mutates."""
    from .commands import topics

    return _run_cmd(
        topics,
        _namespace(json=False, func=topics.cmd_list),
    )


@mcp.tool()
def flightdeck_topics_audit() -> str:
    """Read-only audit: flags topics whose CURRENT name differs from their registry name, and topics with no project mapping. Read-only; never mutates."""
    from .commands import topics

    return _run_cmd(
        topics,
        _namespace(json=False, func=topics.cmd_audit),
    )


# --------------------------------------------------------------------------- #
# Mutating tools — every one defaults to apply=False and mutates nothing then.
# --------------------------------------------------------------------------- #


@mcp.tool()
def flightdeck_review(card: str, apply: bool = False) -> str:
    """Mutates only when apply=True: review one card — diff/verify/merge summary by default, merge into main AND close the card when apply=True. Mutates nothing when apply=False."""
    from .commands import review

    return _run_gated(
        review,
        _namespace(card=card, apply=apply, json=False, queue=False,
                   func=review.cmd_dispatch),
        apply=apply,
    )


@mcp.tool()
def flightdeck_reconcile(apply: bool = False) -> str:
    """Mutates only when apply=True: close/archive cards per the reconcile plan (dead/stale/merged); reports the plan by default. Mutates nothing when apply=False."""
    from .commands import reconcile
    from .core import kanban

    return _run_gated(
        reconcile,
        _namespace(json=False, apply=apply, days=kanban.DEFAULT_DEAD_DAYS,
                   func=reconcile.cmd_reconcile),
        apply=apply,
    )


@mcp.tool()
def flightdeck_decompose(
    project: str,
    milestone: str | None = None,
    goal: str | None = None,
    apply: bool = False,
) -> str:
    """Mutates only when apply=True: create the ACCEPTED decomposed cards for a project (from a milestone or free-text goal); quality-gated proposal by default. Mutates nothing when apply=False."""
    from .commands import decompose

    return _run_gated(
        decompose,
        _namespace(project=project, milestone=milestone, goal=goal,
                   apply=apply, timeout=decompose._DEFAULT_ASK_TIMEOUT,
                   func=decompose.cmd_decompose),
        apply=apply,
    )


@mcp.tool()
def flightdeck_start(
    project: str,
    milestone: str,
    apply: bool = False,
    max_concurrent: int = 3,
) -> str:
    """Mutates only when apply=True: release a milestone's cards to the fleet (concurrency-aware, respects the fleet ceiling); dry-run release order by default. Mutates nothing when apply=False."""
    from .commands import start

    return _run_gated(
        start,
        _namespace(project=project, milestone=milestone, apply=apply,
                   max_concurrent=max_concurrent, func=start.cmd_start),
        apply=apply,
    )


@mcp.tool()
def flightdeck_ingest(project: str, limit: int = 25, apply: bool = False) -> str:
    """Mutates only when apply=True: write the drafted ROADMAP.md for a project (stages context by default); dry-run draft by default. Mutates nothing when apply=False."""
    from .commands import ingest

    return _run_gated(
        ingest,
        _namespace(project=project, limit=limit, apply=apply,
                   timeout=ingest._DEFAULT_TIMEOUT, func=ingest.cmd_ingest),
        apply=apply,
    )


@mcp.tool()
def flightdeck_release(project: str, version: str, apply: bool = False) -> str:
    """Mutates only when apply=True: run the release (bump→commit→tag→push→gh→install→verify) for a project's version; prints the dry-run plan by default. Gated/destructive op defaulting to dry-run — mutates nothing when apply=False."""
    from .commands import release

    return _run_gated(
        release,
        _namespace(project=project, version=version, apply=apply,
                   func=release.run),
        apply=apply,
    )


@mcp.tool()
def flightdeck_roadmap_adopt(project: str, apply: bool = False) -> str:
    """Mutates only when apply=True: promote the project's ROADMAP.draft.md to ROADMAP.md (backing up the existing one); diff by default. Mutates nothing when apply=False."""
    from .commands import roadmap

    return _run_gated(
        roadmap,
        _namespace(project=project, apply=apply, func=roadmap.cmd_adopt),
        apply=apply,
    )


@mcp.tool()
def flightdeck_migrate_card(card_id: str, project: str, apply: bool = False) -> str:
    """Mutates only when apply=True: re-home one card to a project's board (creates a [migrated] copy there, archives the original with a pointer). Refuses running/claimed cards and unknown projects; dry-runs the plan by default. Mutates nothing when apply=False."""
    from .commands import legacy

    return _run_gated(
        legacy,
        _namespace(card_id=card_id, to=project, apply=apply,
                   func=legacy.cmd_migrate_card),
        apply=apply,
    )


@mcp.tool()
def flightdeck_message_send(project: str, text: str) -> str:
    """Post a message to a project's topic IMMEDIATELY — by design, exactly like the CLI. This is the one tool with no apply gate: sending happens right away."""
    from .commands import message

    return _run_cmd(
        message,
        _namespace(project=project, message=text, func=message.cmd_send),
    )


@mcp.tool()
def flightdeck_message_dispatch(
    project: str,
    task: str,
    body: str | None = None,
    assignee: str | None = None,
    message: str | None = None,
    apply: bool = False,
) -> str:
    """Mutates only when apply=True: create a card on a project's board AND announce it in its topic (dry-runs the board/title/body/announcement by default). ``body`` is the card body string; ``message`` is the (optional, short) Telegram announcement text, which may differ from ``body``. Mutates nothing when apply=False."""
    from .commands import message as message_mod

    return _run_gated(
        message_mod,
        _namespace(project=project, task=task, body=body, assignee=assignee,
                   message=message, apply=apply,
                   func=message_mod.cmd_dispatch),
        apply=apply,
    )


@mcp.tool()
def flightdeck_report(
    project: str | None = None,
    since: str = "24h",
    all: bool = False,
    backfill: str | None = None,
    apply: bool = False,
) -> str:
    """Mutates only when apply=True: post a learnable completion summary (SHIPPED/OPEN/FAILED/MILESTONES/NOTE) for a project — or every project with --all / one catch-up window with --backfill.
    ``since`` is the reporting window (e.g. 24h/7d/2h30m). The CLI's own dry-run-by-default --apply gate maps 1:1 onto this tool's apply param. Mutates nothing when apply=False."""
    from .commands import report

    return _run_gated(
        report,
        _namespace(project=project, since=since, all=all, backfill=backfill,
                   apply=apply, func=report.cmd_report),
        apply=apply,
    )


@mcp.tool()
def flightdeck_incident(
    symptom: str,
    project: str | None = None,
    fix: str | None = None,
    cause: str | None = None,
    lesson: str | None = None,
    apply: bool = False,
) -> str:
    """Mutates only when apply=True: append a dated, five-field lesson to a project's docs/INCIDENTS.md (newest first; existing entries are never rewritten). The CLI's own dry-run-by-default --apply gate maps 1:1 onto this tool's apply param. Mutates nothing when apply=False."""
    from .commands import incident

    return _run_gated(
        incident,
        _namespace(symptom=symptom, project=project, fix=fix, cause=cause,
                   lesson=lesson, apply=apply, func=incident.cmd_incident),
        apply=apply,
    )


@mcp.tool()
def flightdeck_hygiene(apply: bool = False, similarity: float = 0.88) -> str:
    """Mutates only when apply=True: fix board decay — duplicate cards, triage traps, and stale worktrees (one item at a time, refusing to drop anything it could not act on). The CLI's own dry-run-by-default --apply gate maps 1:1 onto this tool's apply param. Mutates nothing when apply=False."""
    from .commands import hygiene

    return _run_gated(
        hygiene,
        _namespace(apply=apply, similarity=similarity, json=False),
        apply=apply,
    )


def _namespace(**kw) -> argparse.Namespace:
    """Build the argparse Namespace for a command. ``module.run`` sets the
    injectable seams and ``args.registry`` itself, so we only set the user-facing
    parses here."""
    return argparse.Namespace(**kw)


def _run_gated(module, args: argparse.Namespace, *, apply: bool) -> str:
    """Run a MUTATING command through the same entry the CLI uses, honouring the apply gate.

    Thin adapter only — delegates to :func:`_run_cmd` (the same
    ``module.run(args, registry_path)`` path the CLI dispatches to). When
    ``apply=False`` (always the default), the underlying command is itself in
    dry-run mode and never reaches its write branch, so the injected kdb/git
    seams are never asked to write. A plain gate footer is appended so the
    returned text unambiguously states that nothing was changed — the
    hard contract every mutating tool must honour.
    """
    out = _run_cmd(module, args)
    if not apply:
        footer = "APPLY GATE: apply=False — nothing was changed. "\
            "Re-run with apply=True to perform the mutation."
        return (out + "\n\n" + footer) if out else footer
    return out


def main() -> None:
    """Console-script entry: run the stdio MCP server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
