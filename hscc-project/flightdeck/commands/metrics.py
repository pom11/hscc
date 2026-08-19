"""metrics.py — `flightdeck metrics [project] [--since DURATION]` : quality metrics.

Turns the fleet's board + git history into MEASURED claims about how cards
actually perform — the number behind what has until now been anecdote (an
abstractly-phrased card once stalled 1h45m with zero commits while the same
task naming exact seams succeeded first try).

Metrics (computed in :mod:`flightdeck.core.metrics`, a pure injectable module —
this command only gathers inputs and presents):

  - first-time-pass rate — cards whose branch merged withOUT being sent back /
    all cards reviewed in the window.
  - stall rate — cards that started and ran past a threshold with zero commits /
    all cards started in the window.
  - review latency — median and p90 of seconds from "blocked for review" to
    merged, over cards whose branch merged in the window.
  - throughput — cards merged per day over the window.
  - rework — cards that needed more than one review round.

The HONESTY rule drives everything: every figure states its window AND its
sample size, and a rate below :data:`metrics.MIN_SAMPLE_SIZE` (or with no data)
prints "insufficient data (n=N)" — NEVER a misleading 0% or 100%. A rate
computed from 3 cards must not be presented like one computed from 300.

Cards are attributed by repo path (:func:`kanban.project_for_card`), not by
board — the shared board holds many projects. ``[project]`` narrows to one
registered project; omitted means the whole fleet. Git facts (merged /
commits) come from :mod:`flightdeck.core.git_state` and per-card event
timelines from :func:`kanban.card_events`; every external call is injectable so
tests never touch a real board, repo, or git.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Callable, Optional

from ..core import git_state, kanban, metrics, registry
from .report import _parse_duration

# Default window when --since is not given: the last 24h.
DEFAULT_SINCE_SECONDS = 24 * 60 * 60


def _resolve_since(arg_since: Optional[str], _now: Callable) -> float:
    """The window start (epoch seconds): ``--since`` parsed, else 24h ago."""
    if arg_since:
        return float(_now()) - _parse_duration(arg_since)
    return float(_now()) - DEFAULT_SINCE_SECONDS


def _default_events(card_id: str) -> list[dict]:
    """Production event reader: the card's timeline via core/kanban."""
    return kanban.card_events(card_id)


def gather(
    projects: list[registry.Project],
    *,
    since_ts: float,
    now: float,
    project: Optional[str] = None,
    _run: Optional[Callable] = None,
    _events: Optional[Callable] = None,
    _cards: Optional[list] = None,
) -> dict:
    """Gather prepared cards and compute metrics, all through injectables.

    ``project`` (a registry name) narrows to one project; ``None`` means the
    whole fleet. Cards are attributed by repo path via :func:`kanban.project_for_card`
    — a card that resolves to no registered repo gets no git facts (conservative:
    we never claim a merged/stalled state we could not verify) and contributes
    nothing.

    Inputs, all injectable so no test touches the real board, git, or network:

      * ``_cards``  — the card source (default :func:`kanban.list_cards`);
      * ``_run``    — the git subprocess runner (git_state facts);
      * ``_events`` — ``(card_id) -> [{kind, created_at}]`` (default reads via
        :func:`kanban.card_events`).

    Returns the :func:`metrics.compute` result dict.
    """
    cards = _cards if _cards is not None else kanban.list_cards(
        board=None, include_archived=True
    )
    events_reader = _events if _events is not None else _default_events

    # Attribute every card to a project by repo path.
    attributed: list[tuple] = []  # (card, project_or_None)
    if project is not None:
        proj = None
        for p in projects:
            if p.name == project:
                proj = p
                break
        if proj is None:
            raise registry.ProjectNotFoundError(project)
        for c in cards:
            if kanban.project_for_card(c, [proj]) is proj:
                attributed.append((c, proj))
    else:
        for c in cards:
            p = kanban.project_for_card(c, projects)
            if p is not kanban.UNATTRIBUTED:
                attributed.append((c, p))

    prepared: list[dict] = []
    for c, p in attributed:
        repo = getattr(p, "repo", None)
        branch = c.get("branch")
        cid = c.get("id")
        if repo and branch and cid:
            is_merged = git_state.is_merged(repo, branch, _run=_run)
            commits_ahead = git_state.commits_ahead(repo, branch, _run=_run)
        else:
            is_merged = False
            commits_ahead = 0
        events = events_reader(cid) if cid else []
        prepared.append(
            {
                "id": cid,
                "title": c.get("title"),
                "status": c.get("status"),
                "board": c.get("board"),
                "started_at": c.get("started_at"),
                "completed_at": c.get("completed_at"),
                "is_merged": is_merged,
                "commits_ahead": commits_ahead,
                "events": events,
            }
        )

    return metrics.compute(prepared, since_ts=since_ts, now=now)


# --------------------------------------------------------------------------- #
# presentation — every figure states window + sample size
# --------------------------------------------------------------------------- #


def _fmt_pct(rate: Optional[float]) -> str:
    """A rate as ``42%``, or ``-`` when None (insufficient)."""
    if rate is None:
        return "-"
    return f"{rate * 100:.0f}%"


def _fmt_dur(seconds: Optional[float]) -> str:
    """Seconds as compact human text (e.g. ``8m``, ``1h 30m``), or ``-``."""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def _window_str(since_ts: float, now_ts: float) -> str:
    """Human window header, e.g. ``last 24h`` or ``2026-08-12 09:00 → now``."""
    span = now_ts - since_ts
    days = span / 86400.0
    if abs(days - 1) < 0.01:
        return "last 24h"
    if days.is_integer():
        return f"last {int(days)}d"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(since_ts))


def _figure(value: Optional[str], n: int) -> str:
    """A figure: its value, or ``insufficient data (n=N)`` when None.
    """
    return value if value is not None else f"insufficient data (n={n})"


def render(metrics_dict: dict) -> list[str]:
    """The compact metrics table as human lines.

    Every figure line carries the value AND ``(n=N)`` its sample size, and
    ``insufficient data (n=N)`` stands in for anything below the minimum
    sample. The header states the window.
    """
    w = metrics_dict["window"]
    out: list[str] = []
    out.append(f"metrics: {_window_str(w['since'], w['now'])}  "
               f"({w['days']:.3g} day window)")
    out.append("")

    ftp = metrics_dict["first_time_pass"]
    out.append(
        f"  first-time-pass   {_figure(_fmt_pct(ftp['rate']) if ftp['rate'] is not None else None, ftp['n'])}   "
        f"(n={ftp['n']}, {ftp['count']} passed)"
    )

    stall = metrics_dict["stalled"]
    out.append(
        f"  stall             {_figure(_fmt_pct(stall['rate']) if stall['rate'] is not None else None, stall['n'])}   "
        f"(n={stall['n']}, {stall['count']} stalled)"
    )

    lat = metrics_dict["review_latency"]
    med = _fmt_dur(lat["median"])
    p90 = _fmt_dur(lat["p90"])
    n_lat = lat["n"]
    latency_line = _figure(
        f"{med} median / {p90} p90" if lat["median"] is not None else None,
        n_lat,
    )
    out.append(f"  review latency     {latency_line}   (n={n_lat})")

    thr = metrics_dict["throughput"]
    td = thr["per_day"]
    tp_line = _figure(f"{td:.2f}/day" if td is not None else None, thr["n"])
    out.append(f"  throughput        {tp_line}   (n={thr['n']})")

    rw = metrics_dict["rework"]
    out.append(
        f"  rework            {_figure(_fmt_pct(rw['share']) if rw['share'] is not None else None, rw['n'])}   "
        f"(n={rw['n']}, {rw['count']} >1 round)"
    )
    return out


# --------------------------------------------------------------------------- #
# JSON presentation — the same figures, machine-readable
# --------------------------------------------------------------------------- #


def render_json(metrics_dict: dict) -> dict:
    """The same figures as a structured dict (for ``--json``)."""
    return {
        "window": {
            "since": metrics_dict["window"]["since"],
            "now": metrics_dict["window"]["now"],
            "days": metrics_dict["window"]["days"],
        },
        "reviewed": metrics_dict["reviewed"],
        "merged_count": metrics_dict["merged_count"],
        "first_time_pass": {
            "n": metrics_dict["first_time_pass"]["n"],
            "count": metrics_dict["first_time_pass"]["count"],
            "rate": metrics_dict["first_time_pass"]["rate"],
        },
        "started": metrics_dict["started"],
        "stalled": {
            "n": metrics_dict["stalled"]["n"],
            "count": metrics_dict["stalled"]["count"],
            "rate": metrics_dict["stalled"]["rate"],
        },
        "review_latency": {
            "n": metrics_dict["review_latency"]["n"],
            "median_s": metrics_dict["review_latency"]["median"],
            "p90_s": metrics_dict["review_latency"]["p90"],
        },
        "throughput": {
            "n": metrics_dict["throughput"]["n"],
            "per_day": metrics_dict["throughput"]["per_day"],
        },
        "rework": {
            "n": metrics_dict["rework"]["n"],
            "count": metrics_dict["rework"]["count"],
            "share": metrics_dict["rework"]["share"],
        },
    }


# --------------------------------------------------------------------------- #
# command entry
# --------------------------------------------------------------------------- #


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "metrics",
        help="fleet/project quality metrics: first-time-pass, stall, review latency",
        epilog="example: flightdeck metrics flightdeck --since 7d",
    )
    p.add_argument(
        "project",
        nargs="?",
        metavar="PROJECT",
        help="registry project to scope to (default: whole fleet)",
    )
    p.add_argument(
        "--since",
        default=None,
        metavar="DURATION",
        help="window, e.g. 24h / 7d / 2h30m (default: 24h)",
    )
    p.set_defaults(func=cmd_metrics)


def cmd_metrics(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """Gather, compute, and render. Returns the process exit code."""
    _now = getattr(args, "now", None) or (lambda: time.time())
    since_ts = _resolve_since(getattr(args, "since", None), _now)
    now_ts = float(_now())

    # Resolve the project: an explicit arg always wins; otherwise it is
    # auto-detected from the cwd (the project whose repo contains the current
    # directory), which prints a visible note to stderr. No match -> whole
    # fleet (today's default), unchanged for anyone running from outside a repo.
    project, _detected = registry.resolve_project_arg(
        projects, getattr(args, "project", None),
        cwd=getattr(args, "cwd", None),
        _print=lambda line: print(line, file=sys.stderr),
    )

    try:
        metrics_dict = gather(
            projects,
            since_ts=since_ts,
            now=now_ts,
            project=project,
            _run=getattr(args, "run", None),
            _events=getattr(args, "events", None),
            _cards=getattr(args, "cards", None),
        )
    except registry.ProjectNotFoundError as exc:
        print(f"metrics: {exc}", file=args.stderr or sys.stderr)
        return 2

    if getattr(args, "json", False):
        import json

        print(json.dumps(render_json(metrics_dict)))
    else:
        for line in render(metrics_dict):
            print(line)
    return 0


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: resolve injectable handles, then dispatch."""
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.events = getattr(args, "events", None)
    args.now = getattr(args, "now", None)
    args.cards = getattr(args, "cards", None)
    args.stderr = getattr(args, "stderr", None) or sys.stderr
    args.json = getattr(args, "json", False)
    args.since = getattr(args, "since", None)
    args.project = getattr(args, "project", None)
    args.cwd = getattr(args, "cwd", None)
    projects = registry.load_registry(registry_path)
    return cmd_metrics(args, projects)
