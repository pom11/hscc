"""report.py — `flightdeck report <project>` : post a learnable completion summary.

The WHY (from the body of this card): Hermes learns through
``background_review`` — its post-turn self-improvement fork that replays a
session's conversation and decides whether to save a memory or patch a skill.
A kanban worker runs as its own session but only ever sees its own narrow
card, so executing work on boards means the ORCHESTRATOR never learns what
happened. This command closes that loop deliberately: execute on boards
(reliable), then TELL the orchestrator what happened (so it learns).

``flightdeck report <project>`` gathers what actually happened for one project
since a window — from facts flightdeck already has — and renders a SHORT
structured summary shaped for a model to learn from, not a bare changelog.
``--all`` does the same for EVERY registered project in one pass (skipping
those with nothing to report), and ``--backfill DURATION`` is a one-shot
catch-up over a long window that summarises HARDER so a big backlog still fits
one message per project. A single fleet-wide run looks like::

    flightdeck report --all --apply          # every project, per-project windows
    flightdeck report --backfill 72h --apply # one deliberate catch-up of a long gap

The summary layout is:

    SHIPPED      cards that reached done/closed since the window (id + title)
                 and branches merged into main (merge subject lines)
    OPEN         cards still open (status not done/closed, not archived)
    FAILED       what failed and why (the project's verify FAIL, with its hint)
    MILESTONES   roadmap progress deltas (done count change per milestone)
    NOTE         surprising facts worth remembering — facts WITH a cause

The window is ``--since DURATION`` (default 24h on the first run, then the
previously recorded last-reported timestamp). ``--apply`` posts the rendered
summary to the project's topic via :mod:`flightdeck.core.telegram` (the
single-writer MCP path — never a direct Telethon session). Dry-run prints it
and posts nothing.

Hard rules:

  * If there is NOTHING to report, say so and post nothing — never post an
    empty or filler update (that is how a channel gets muted).
  * Never post the same window twice: the last-reported timestamp per project
    is recorded under ``~/.flightdeck/`` and becomes the default ``--since``.
  * The SENT text never exceeds ``telegram.MAX_MESSAGE_LENGTH`` — the renderer
    hard-caps the summary, and the send path asserts the actual length.

All external access is injectable: ``_run`` (git subprocess), ``_now`` (clock),
``_client`` (Telegram MCP), and the board via monkeypatched ``kanban.list_cards``
so tests never touch Telegram, the board, git, or the network in reality.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..core import git_state, kanban, registry, roadmap, telegram, verify

# The hard character cap for the full rendered summary. The summary is the
# body of exactly one Telegram message, so it must always fit under
# telegram.MAX_MESSAGE_LENGTH. Use the constant, never a literal 4096.
MAX_SUMMARY_LENGTH = telegram.MAX_MESSAGE_LENGTH

# The default window on the very first run, before any timestamp is recorded.
DEFAULT_SINCE_SECONDS = 24 * 60 * 60  # 24h

# The per-project report bookkeeping file under ~/.flightdeck/.
DEFAULT_REPORT_STATE = "~/.flightdeck/report-state.yaml"

# Statuses that mean a card's work has landed (closed out). "done"/"closed"
# are Hermes' terminal statuses; "complete"/"archived" tolerated for safety so
# a renamed status never reads as "still open" forever.
_DONE_STATUSES = frozenset({"done", "closed", "complete"})

# Statuses that explicitly mean the work is NOT open; everything left is open.
_ARCHIVED_STATUSES = frozenset({"archived"})

# --------------------------------------------------------------------------- #
# report bookkeeping — ~/.flightdeck/report-state.yaml
# --------------------------------------------------------------------------- #
# Shape:
#   projects:
#     <name>:
#       last_report: 1760000000.0   # epoch, when --apply last posted a summary
#       milestones:                 # snapshot used to compute progress deltas
#         <id>: {done: N, total: M}


def _report_state_path(path: Optional[str]) -> str:
    """Resolve the report-state file path (expand ``~``)."""
    return os.path.expanduser(path if path is not None else DEFAULT_REPORT_STATE)


def _load_report_state(path: Optional[str]) -> dict:
    """Read the report-state file into a dict; a missing/bad file is {}."""
    p = Path(_report_state_path(path))
    if not p.exists():
        return {}
    import yaml

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, IOError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _save_report_state(state: dict, path: Optional[str]) -> None:
    """Persist the whole report-state document, creating the directory."""
    import yaml

    p = Path(_report_state_path(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(state, sort_keys=False), encoding="utf-8")


def last_reported(project: str, path: Optional[str] = None) -> Optional[float]:
    """The epoch when ``project``'s summary was last posted, or None.

    None means never posted — the caller then defaults the window to the full
    ``DEFAULT_SINCE_SECONDS`` rather than to a timestamp that would silently
    skip the whole backlog.
    """
    state = _load_report_state(path)
    row = state.get("projects", {}).get(project)
    if not isinstance(row, dict):
        return None
    value = row.get("last_report")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _recorded_milestones(project: str, path: Optional[str]) -> dict:
    """The milestone snapshot stored for ``project``, keyed by milestone id.

    Each value is ``{done: int, total: int}``. An empty mapping means no
    snapshot was recorded yet (first run) — every current milestone then
    counts as a delta only if it shows progress we can defend.
    """
    state = _load_report_state(path)
    row = state.get("projects", {}).get(project)
    if not isinstance(row, dict):
        return {}
    ms = row.get("milestones")
    if not isinstance(ms, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in ms.items():
        if isinstance(value, dict):
            try:
                out[str(key)] = {"done": int(value.get("done", 0) or 0),
                                 "total": int(value.get("total", 0) or 0)}
            except (TypeError, ValueError):
                continue
    return out


def record_report(
    project: str,
    *,
    _now: Optional[Callable] = None,
    milestones: Optional[dict] = None,
    path: Optional[str] = None,
) -> None:
    """Record that ``project`` was reported now, plus a milestone snapshot.

    Called ONLY after a successful ``--apply`` post, so a recorded timestamp
    is genuine evidence a summary reached the topic — never a preview. The
    milestone snapshot (``{id: {done, total}}`` for current milestones) becomes
    the baseline against which the NEXT run computes its progress deltas.
    """
    state = _load_report_state(path)
    projects = state.setdefault("projects", {})
    row = projects.setdefault(project, {})
    row["last_report"] = _now() if _now is not None else time.time()
    if milestones is not None:
        row["milestones"] = milestones
    _save_report_state(state, path)


# --------------------------------------------------------------------------- #
# milestone progress — current milestone item counts + delta vs snapshot
# --------------------------------------------------------------------------- #


def _current_milestones(project: registry.Project) -> dict:
    """``{id: {title, done, total}}`` for the project's roadmap milestones.

    Uses the shared :mod:`flightdeck.core.roadmap` parser (the same source
    ``roadmap progress`` reads), so the report and the progress command can
    never disagree on what a milestone contains. A project with no roadmap
    yields an empty mapping — those milestones simply have no item data.
    """
    r = roadmap.project_roadmap(project)
    out: dict[str, dict] = {}
    if not r.present:
        return out
    for m in r.milestones:
        out[m.id] = {
            "title": m.title or m.name,
            "done": m.done_count,
            "total": m.total,
        }
    return out


def _milestone_deltas(current: dict, snapshot: dict) -> list[dict]:
    """Milestone progress deltas vs the recorded snapshot.

    ``current`` is the live ``{id: {title, done, total}}`` read from the
    roadmap; ``snapshot`` is what was recorded at the last report. A delta is
    any milestone whose done count increased since the snapshot (or that has
    done items while the snapshot had none — the first-report case). Each
    returned dict carries ``id``/``title`` (new count) and ``added`` (how many
    items are newly done since the snapshot).

    Milestones with no change (done same as snapshot, or 0 done in both) are
    omitted — a report only tells the orchestrator what MOVED. A done-count
    DROP is an edit/shrink, not progress, and is also omitted.
    """
    deltas: list[dict] = []
    for mid, cur in current.items():
        prev_done = snapshot.get(mid, {}).get("done", 0)
        done = cur["done"]
        if done <= prev_done:
            continue
        deltas.append(
            {
                "id": mid,
                "title": cur["title"],
                "done": done,
                "total": cur["total"],
                "added": done - prev_done,
            }
        )
    # Deterministic order by milestone id.
    deltas.sort(key=lambda d: d["id"])
    return deltas


# --------------------------------------------------------------------------- #
# data gathering — the single dict the renderer consumes
# --------------------------------------------------------------------------- #


def _coerce_ts(value) -> float:
    """Coerce a card timestamp to a float epoch, or -inf for unparseable."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return float("-inf")  # never matches the window; treated as stale


def gather_data(
    project: registry.Project,
    since_ts: float,
    *,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
    _state: Optional[str] = None,
    _report_state: Optional[str] = None,
    _cards: Optional[list] = None,
) -> dict:
    """Assemble the report data for one project since ``since_ts``.

    ``project`` is the resolved :class:`registry.Project`. ``since_ts`` is the
    window start (epoch seconds); events older than it are not this report's.

    Reads, all through injectable seams so no test touches the board, git, a
    live repo, or the network:

      * ``_cards`` (default :func:`kanban.list_cards`) — the project's cards
        (attributed by workspace_path, matching the rest of flightdeck);
      * ``git_state.merged_subjects_since`` via ``_run`` — merged branch
        subject lines this window;
      * ``verify.load_state`` via ``_state`` — the project's latest verify;
      * ``roadmap.project_roadmap`` — current milestone item counts, diffed
        against the ``_report_state`` snapshot.

    Returns ``{shipped, open, merged, failed, milestones, notable, anything}``.
    ``shipped``/``open`` are card rows ``{id, title, status}``; ``shipped`` only
    holds done/closed cards whose ``completed_at`` falls inside the window (or
    whose completion time is unknown), so old done cards are never re-reported —
    the guarantee that ``--all`` and repeated runs do not double-post; ``merged``
    is a list of merge subject lines; ``failed`` is ``{what, why}`` rows;
    ``milestones`` is the delta list; ``notable`` is list[str] of cause-carrying
    facts ready to print. ``anything`` is True iff any of those present real
    content.
    """
    cards = _cards if _cards is not None else kanban.list_cards(board=None, include_archived=True)
    my_cards = [c for c in cards if kanban.project_for_card(c, [project]) is project]

    shipped: list[dict] = []
    open_cards: list[dict] = []
    for c in my_cards:
        status = str(c.get("status") or "")
        if status in _ARCHIVED_STATUSES:
            continue
        row = {"id": c.get("id"), "title": c.get("title") or "untitled", "status": status}
        if status in _DONE_STATUSES:
            # A done card is only NEWS when it reached done inside this window.
            # ``completed_at`` is the timestamp Hermes stamps at completion; a
            # card done before ``since_ts`` was already reported (or was never
            # this window's news) and must NOT be re-reported forever — that is
            # the double-post this whole command exists to prevent. A card with
            # NO completed_at is treated as in-window (first-run safe) so we
            # never silently drop a card whose completion time is unknown.
            completed_at = c.get("completed_at")
            in_window = completed_at is None or _coerce_ts(completed_at) >= since_ts
            if in_window:
                shipped.append(row)
        else:
            open_cards.append(row)

    merged = git_state.merged_subjects_since(project.repo, since_ts, _run=_run)

    failed: list[dict] = []
    state = verify.load_state(_state)
    verify_map = state.get("verify", {}) if isinstance(state, dict) else {}
    last = verify_map.get(project.name)
    if isinstance(last, dict) and last.get("status") == verify.FAIL:
        ts = last.get("timestamp")
        if ts is None or (isinstance(ts, (int, float)) and float(ts) >= since_ts):
            why = (last.get("hint") or "").strip()
            failed.append(
                {
                    "what": "verify failed",
                    "why": why or "verify command exited non-zero",
                }
            )

    current = _current_milestones(project)
    snapshot = _recorded_milestones(project.name, _report_state)
    milestones = _milestone_deltas(current, snapshot)

    # --- the "surprising / worth remembering" line: facts WITH a cause.
    # A card closed (done) whose branch never landed in main is the surprising
    # one: closing without evidence the work reached the tree. Each such card is
    # a cause-carrying fact, not a bare list entry.
    notable: list[str] = []
    done_ids = {s["id"] for s in shipped}
    merged_text = "\n".join(merged)
    for c in my_cards:
        status = str(c.get("status") or "")
        if status not in _DONE_STATUSES:
            continue
        card_id = c.get("id")
        if not card_id:
            continue
        branch = c.get("branch") or f"wt/{card_id}"
        landed = git_state.is_merged(project.repo, branch, _run=_run)
        if not landed:
            notable.append(
                f"card {card_id} is marked {status} but its branch {branch} is "
                f"not in main — closing without evidence the work landed"
            )

    anything = bool(
        shipped
        or merged
        or failed
        or milestones
        or notable
    )

    return {
        "project": project.name,
        "since_ts": since_ts,
        "shipped": shipped,
        "open": open_cards,
        "merged": merged,
        "failed": failed,
        "milestones": milestones,
        "notable": notable,
        "anything": anything,
    }


# --------------------------------------------------------------------------- #
# renderer — the single source of truth for the summary text
# --------------------------------------------------------------------------- #


def render_summary(data: dict) -> str:
    """Render the SHORT structured summary for ``data``.

    Deterministic given ``data``. Layout (each section present only when it
    has rows, so an update with nothing to say stays short):

        <project> since <window>
        SHIPPED: <n> card(s) — <title> [<id>], ...
                + <m> branch(es) merged into main:
                  - merge: ...
        OPEN: <title> [<id>] (<status>), ...
        FAILED: <what> — <why>
        MILESTONES: <title> +<added> now <done>/<total>
        NOTE: <surprising fact>

    The total length is hard-capped at ``MAX_SUMMARY_LENGTH`` characters: the
    summary is the body of ONE Telegram message and must always fit. Sections
    are emitted most-informative-first (shipped, failed, milestones, note),
    so if the cap bites we truncate the low-value OPEN tail, never the learnable
    head. Returns an empty string for a ``data`` with ``anything`` False — the
    caller then prints "nothing to report" and posts nothing.
    """
    if not data.get("anything"):
        return ""

    project = data.get("project") or "?"
    since = _window_str(data.get("since_ts"))
    lines: list[str] = [f"summary for {project} since {since}"]

    shipped = data.get("shipped") or []
    merged = data.get("merged") or []
    if shipped or merged:
        lines.append("SHIPPED:")
        if shipped:
            bits = [f"{s['title']} [{s['id']}]" for s in shipped]
            noun = "card" if len(bits) == 1 else "cards"
            lines.append(f"  {len(bits)} {noun}: {', '.join(bits)}")
        if merged:
            noun = "branch" if len(merged) == 1 else "branches"
            lines.append(f"  {len(merged)} {noun} merged into main:")
            for m in merged:
                lines.append(f"    - {m}")

    open_cards = data.get("open") or []
    if open_cards:
        bits = [f"{o['title']} [{o['id']}] ({o['status']})" for o in open_cards]
        noun = "card" if len(bits) == 1 else "cards"
        lines.append(f"OPEN: {len(bits)} {noun} — {', '.join(bits)}")

    failed = data.get("failed") or []
    for f in failed:
        lines.append(f"FAILED: {f['what']} — {f['why']}")

    milestones = data.get("milestones") or []
    if milestones:
        for ms in milestones:
            lines.append(
                f"MILESTONE: {ms['title']} +{ms['added']} item(s) "
                f"now {ms['done']}/{ms['total']}"
            )

    notable = data.get("notable") or []
    for n in notable:
        lines.append(f"NOTE: {n}")

    return _cap("\n".join(lines), MAX_SUMMARY_LENGTH)


def _window_str(since_ts) -> str:
    """Human rendering of a since-window, e.g. ``24h`` or ``2026-08-11 09:00``.

    Compact by design so the summary header stays small. Uses local wall time.
    """
    try:
        ts = float(since_ts)
    except (TypeError, ValueError):
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _cap(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters, never splitting mid-char.

    The summary is the body of ONE Telegram message, so it must fit under the
    per-message cap. A (pathological) over-long build is cut hard at the cap;
    a plain build never reaches it. Truncation always keeps the learnable HEAD
    (the section renderer already orders most-informative-first).
    """
    if len(text) <= limit:
        return text
    return text[:limit]


def _fit_lines(lines: list[str], limit: int) -> str:
    """Keep WHOLE lines from the top until the joined text fits ``limit``.

    Used by the backfill renderer, where a long window can legitimately carry
    a huge amount of material. Lines are already ordered most-informative-first
    (header, SHIPPED counts, FAILED, MILESTONE, NOTE, then the low-value OPEN
    count). Greedily keep each line whole as long as appending it stays under
    ``limit``; drop trailing lines whole once the cap bites. A line is NEVER
    split, so the result never truncates mid-sentence and always ends on a
    complete line. Returns "" only if not even the first line fits.
    """
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join(kept + [line])
        if len(candidate) > limit:
            break
        kept.append(line)
    return "\n".join(kept)


def render_backfill(data: dict) -> str:
    """Render a HARDER-summarised summary for a ``--backfill`` long window.

    A backfill rehashes a deliberately long window (e.g. the 46-merge gap this
    card closes), so it must never balloon into several messages or a
    mid-sentence truncation. Unlike :func:`render_summary` — which lists every
    shipped card and merge subject — backfill AGGREGATES those into high-level
    count lines (``SHIPPED: 5 cards; 12 branches merged``), keeps the genuinely
    learnable facts (FAILED, MILESTONE deltas, NOTE) verbatim, and puts a bare
    OPEN count last. If the joined text still exceeds ``MAX_SUMMARY_LENGTH``,
    whole lines are dropped tail-first via :func:`_fit_lines` — never split.

    Returns an empty string for ``data`` with ``anything`` False.
    """
    if not data.get("anything"):
        return ""

    project = data.get("project") or "?"
    since = _window_str(data.get("since_ts"))
    lines: list[str] = [f"backfill summary for {project} since {since}"]

    shipped = data.get("shipped") or []
    merged = data.get("merged") or []
    if shipped or merged:
        parts: list[str] = []
        if shipped:
            parts.append(f"{len(shipped)} card(s) reached done/closed")
        if merged:
            parts.append(f"{len(merged)} branch(es) merged into main")
        lines.append("SHIPPED: " + "; ".join(parts))

    for f in data.get("failed") or []:
        lines.append(f"FAILED: {f['what']} — {f['why']}")

    for ms in data.get("milestones") or []:
        lines.append(
            f"MILESTONE: {ms['title']} +{ms['added']} item(s) "
            f"now {ms['done']}/{ms['total']}"
        )

    for n in data.get("notable") or []:
        lines.append(f"NOTE: {n}")

    # OPEN is the least learnable section: shrink it to a bare count and put it
    # LAST so it is the first whole line dropped when the cap bites.
    open_cards = data.get("open") or []
    if open_cards:
        noun = "card" if len(open_cards) == 1 else "cards"
        lines.append(f"OPEN: {len(open_cards)} {noun}")

    return _fit_lines(lines, MAX_SUMMARY_LENGTH)



# --------------------------------------------------------------------------- #
# command handler
# --------------------------------------------------------------------------- #


def _parse_duration(s: str) -> float:
    """Parse a ``--since`` duration like ``24h``, ``90m``, ``7d``, ``2h30m``.

    Returns SECONDS. A bare integer with no unit is seconds. The first token
    wins for each unit, so ``2h30m`` = 2h + 30m. An unparseable string raises
    :class:`ValueError` with a clear message.
    """
    s = (s or "").strip().lower()
    if not s:
        return float(DEFAULT_SINCE_SECONDS)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0.0
    num = ""
    matched = False
    for ch in s:
        if ch.isdigit() or ch == ".":
            num += ch
            continue
        if ch in units:
            if not num:
                raise ValueError(f"invalid --since {s!r}: number missing before {ch!r}")
            total += float(num) * units[ch]
            num = ""
            matched = True
            continue
        raise ValueError(
            f"invalid --since {s!r}: unknown unit {ch!r} (use s/m/h/d)"
        )
    if num:
        total += float(num)  # trailing bare number = seconds
        matched = True
    if not matched:
        raise ValueError(f"invalid --since {s!r}: no duration units found")
    return total


def _resolve_since(project: str, arg_since, _report_state, _now) -> float:
    """The window start for this run.

    ``--since`` given -> parse it. Otherwise default to the last recorded
    report timestamp for the project (so repeated runs never repost the same
    window), falling back to ``DEFAULT_SINCE_SECONDS`` ago on the very first
    run.
    """
    if arg_since:
        return float(_now()) - _parse_duration(arg_since)
    recorded = last_reported(project, _report_state)
    if recorded is not None:
        return recorded
    return float(_now()) - DEFAULT_SINCE_SECONDS


def _not_found(name: str) -> int:
    print(f"no project named {name!r} in the registry", file=sys.stderr)
    return 2


def _report_one(
    args: argparse.Namespace,
    project: registry.Project,
    renderer: Callable[[dict], str],
    use_since: Optional[float] = None,
) -> tuple[str, int]:
    """Gather, render, and optionally post a report for ONE project.

    The single shared reporting core used by the single-project command,
    ``--all``, and ``--backfill`` — so one project's summary is always built
    the same way no matter how it was invoked (there is exactly ONE reporting
    implementation, not one per flag). ``renderer`` picks the layout
    (:func:`render_summary` detailed, or :func:`render_backfill` condensed).
    ``use_since`` forces the window start and is used by backfill, which wants
    one deliberately long window for every project.

    Returns ``(kind, rc)`` where ``kind`` is one of ``posted`` | ``dry`` |
    ``nothing`` | ``error`` and ``rc`` is the process exit code. ``apply`` posts
    and records the timestamp; ``dry`` prints and posts nothing; ``nothing``
    means there was nothing worth reporting (never posts a filler update).
    """
    since_ts = (
        use_since
        if use_since is not None
        else _resolve_since(project.name, args.since, args.report_state, args.now)
    )

    data = gather_data(
        project,
        since_ts,
        _run=args.run,
        _now=args.now,
        _state=args.state,
        _report_state=args.report_state,
        _cards=args.cards,
    )

    if not data["anything"]:
        print(f"nothing to report for {project.name} since {_window_str(since_ts)}.")
        return "nothing", 0

    summary = renderer(data)
    if not summary:
        print(f"nothing to report for {project.name} since {_window_str(since_ts)}.")
        return "nothing", 0

    if args.apply:
        rc = _post(project, summary, args)
        return ("posted" if rc == 0 else "error"), rc

    # Dry-run: print the summary and post nothing.
    print(summary)
    print()
    print("[dry-run] summary not posted. pass --apply to send it to the project topic.")
    return "dry", 0


def cmd_report(args: argparse.Namespace, project: registry.Project) -> int:
    """Dry-run (print) or ``--apply`` (post to the topic) for ONE project."""
    _kind, rc = _report_one(args, project, render_summary)
    return rc


def cmd_report_all(args: argparse.Namespace, projects: list) -> int:
    """Report EVERY registered project in one pass; skip those with nothing.

    ``--all`` is how a single command covers the fleet. Each project still uses
    its own resolve-since rule (per-project last-reported timestamp, else 24h),
    so ``--all`` never double-posts a window; a project with nothing to report
    is skipped, never given a filler update. ``--apply`` posts once per project
    that has content; dry-run prints every summary and posts nothing.
    """
    reported = 0
    nothing = 0
    rc = 0
    for project in projects:
        kind, one_rc = _report_one(args, project, render_summary)
        if kind == "nothing":
            nothing += 1
        elif kind in ("posted", "dry"):
            reported += 1
        if one_rc != 0:
            rc = one_rc
    action = "posted" if args.apply else "rendered for review (dry-run)"
    print(f"report --all: {reported} project(s) {action}, {nothing} with nothing to report.")
    return rc


def cmd_backfill(
    args: argparse.Namespace, projects: list, duration: str
) -> int:
    """One-shot catch-up over a deliberately long window, across the fleet.

    ``--backfill <DURATION>`` (e.g. ``72h``) rehashes the whole fleet since an
    explicit long window so a big gap can be closed in one deliberate run — the
    current 46-merge gap. Unlike ``--all`` it pins the SAME wide window for
    every project (it does not trust the per-project last-reported timestamp,
    because those were never set for the gap) and renders with the condensed
    :func:`render_backfill`, which aggregates instead of listing. It still
    posts once per project via ``--apply`` and never truncates mid-sentence.
    """
    try:
        span = _parse_duration(duration)
    except ValueError as exc:
        print(f"report --backfill: {exc}", file=sys.stderr)
        return 2
    since_ts = float(args.now()) - span

    reported = 0
    nothing = 0
    rc = 0
    for project in projects:
        kind, one_rc = _report_one(args, project, render_backfill, use_since=since_ts)
        if kind == "nothing":
            nothing += 1
        elif kind in ("posted", "dry"):
            reported += 1
        if one_rc != 0:
            rc = one_rc
    action = "posted" if args.apply else "rendered for review (dry-run)"
    print(f"report --backfill {duration}: {reported} project(s) {action}, {nothing} with nothing to report.")
    return rc


def _post(project: registry.Project, summary: str, args: argparse.Namespace) -> int:
    """Post ``summary`` to the project's topic; record the timestamp on success.

    ``--apply`` is the only path that touches Telegram and the only path that
    records a reported timestamp. A failure is reported clearly and does not
    crash (returns non-zero). The SENT text length is asserted against
    ``telegram.MAX_MESSAGE_LENGTH`` before dispatch — an over-long body is never
    silently sent, and a recorded timestamp is only written after the send
    succeeded, so a failed post does not suppress the next window.
    """
    if project.topic is None:
        print(
            f"error: project {project.name!r} has no topic bound — cannot post. "
            f"Bind one with `flightdeck topics bind <id> {project.name}`.",
            file=sys.stderr,
        )
        return 2

    try:
        telegram.send_message(project.topic, summary, _client=args.client)
    except telegram.MessageTooLongError as exc:
        # The renderer caps, but belt-and-braces: an oversize body is named and
        # refused BEFORE dispatch — it must never be silently truncated by a send.
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except telegram.TopicLockedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except telegram.TelegramError as exc:
        print(f"error: failed to post summary to topic {project.topic}: {exc}", file=sys.stderr)
        return 2

    record_report(
        project.name,
        _now=args.now,
        milestones=_current_milestones(project),
        path=args.report_state,
    )
    print(f"posted {len(summary)}-char summary to topic {project.topic} ({project.name}).")
    return 0


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "report", help="post a learnable completion summary to a project's topic",
        epilog="example: flightdeck report flightdeck --all",
    )
    p.add_argument(
        "project",
        nargs="?",
        help="project name in the registry (omit with --all/--backfill)",
    )
    p.add_argument(
        "--since",
        default=None,
        metavar="DURATION",
        help="window to report over, e.g. 24h/90m/7d/2h30m "
        "(default: last reported timestamp, else 24h)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="report every registered project in one pass, skipping those with "
        "nothing to report",
    )
    p.add_argument(
        "--backfill",
        default=None,
        metavar="DURATION",
        help="one-shot catch-up over a deliberately long window (e.g. 72h) for "
        "every project, summarised harder so it fits the message cap",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="post the summary to the project's topic (dry-run by default)",
    )
    p.set_defaults(func=cmd_report)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: run a report.

    Attaches the injectable handles (``run``, ``now``, ``state``,
    ``report_state``, ``client``, ``cards``) so core calls are stubbable in
    tests and nothing here touches git, a board, or the network in reality.

    Routes three ways: ``--all`` reports every project; ``--backfill`` reports
    every project over one pinned long window with the condensed renderer; and
    a bare ``report <project>`` reports that single project. The auto-discovered
    ``func`` (``cmd_report``) is only reached for the single-project path —
    ``--all``/``--backfill`` are handled here directly because they need the
    whole registry, not one resolved project.
    """
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.now = getattr(args, "now", None) or time.time
    args.state = getattr(args, "state", None)
    args.report_state = getattr(args, "report_state", None)
    args.client = getattr(args, "client", None)
    args.cards = getattr(args, "cards", None)
    args.cwd = getattr(args, "cwd", None)

    projects = registry.load_registry(registry_path)

    if getattr(args, "all", False):
        return cmd_report_all(args, projects)

    backfill = getattr(args, "backfill", None)
    if backfill:
        return cmd_backfill(args, projects, backfill)

    if not args.project:
        # No explicit project and no --all/--backfill: auto-detect it from the
        # cwd (the project whose repo contains the current directory). When the
        # cwd is inside no registered repo there is no match and we fall
        # through to the existing error — a strictly additive convenience.
        args.project, _detected = registry.resolve_project_arg(
            projects, None,
            cwd=getattr(args, "cwd", None),
            _print=lambda line: print(line, file=sys.stderr),
        )

    if not args.project:
        print("report: specify a project, or use --all / --backfill.", file=sys.stderr)
        return 2

    project = next((p for p in projects if p.name == args.project), None)
    if project is None:
        return _not_found(args.project)

    func = getattr(args, "func", None)
    if func is None:
        print("report: command not recognised. Try `flightdeck report --help`.", file=sys.stderr)
        return 2
    return func(args, project)

