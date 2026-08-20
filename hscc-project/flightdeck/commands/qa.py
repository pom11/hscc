"""qa.py — `flightdeck qa [project]` — the operator's manual-testing queue.

Answers the question the operator asks every day: WHAT DO I ACTUALLY HAVE TO
TEST BY HAND? For every card genuinely awaiting review it shows the ``VERIFY:``
line, the diff summary, and whether the project's automated verify has run and
passed — so the operator knows what to click or run, and what has *not* been
proven yet.

"Genuinely awaiting review" is the strict rule from DESIGN: a card qualifies
only if it is review-required/blocked AND its branch is **NOT** an ancestor of
``main``. A merged branch is the phantom that made 28 cards look like a backlog
when zero needed attention — a card whose work already landed is never shown
here.

A card with NO ``VERIFY:`` line is flagged prominently as UNVERIFIABLE and
sorted first: that is the operator having to reverse-engineer the test from a
diff, which is exactly the toil this command exists to remove.

Attribution is by workspace_path (via ``kanban.project_for_card``): a card
belongs to the project whose repo its ``workspace_path`` resolves to — the
board slug is at most a narrowing filter on which board(s) we read, never the
attribution rule. A card matching no project's repo is shown as UNATTRIBUTED,
never silently dropped and never guessed into a project.

Ordering: UNVERIFIABLE cards first, then by age (oldest first) within each
group. ``--json`` emits a stable machine-readable shape.

Every external call is injectable: ``_run`` (a ``(cmd_list, repo) -> process``
runner, same contract as core/git_state, used for the per-card git facts) and
``_run_verify`` (a ``(command: str) -> process`` shell runner used for the
project's registry ``verify`` command). Nothing here touches git, the network,
Telegram, or a live board in tests.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import yaml

from ..core import git_state, kanban, registry, telegram
from . import review

# Interval between --watch frames. Mirrors standup's DEFAULT_INTERVAL.
DEFAULT_INTERVAL = 30

# Where the set of already-notified card ids is persisted (transition tracking).
NOTIFY_STATE_DEFAULT = "qa-notified.yaml"

# Where post-merge manual-QA entries are persisted (the "needs manual
# verification" store). Same ~/.flightdeck dir convention as qa-notified.yaml.
MANUAL_QA_DEFAULT = "manual-qa.yaml"

# The default root for qa's persistent state, and the env var that can redirect
# it into a sandbox. When HERMES_HOME is set (the test suite does this in
# conftest.py), ALL default qa state resolves under that sandbox, so no
# default-constructed call can silently write the operator's real ~/.flightdeck.
# When it is unset, the default is ~/.flightdeck — unchanged production
# behaviour. This is the single seam every qa default passes through.
DEFAULT_HOME = "~/.flightdeck"
_HOME_ENV = "HERMES_HOME"


def qa_home() -> str:
    """The resolved root for qa's persistent state files.

    Honours ``HERMES_HOME`` when set, else falls back to ``~/.flightdeck``. A
    leading ``~`` is expanded either way. Callers that persist state should
    resolve their default file under this root (see ``_state_path`` /
    ``_manual_path``) rather than hardcoding the real home, so a test sandbox
    can redirect every default write away from the operator's live store.
    """
    root = os.environ.get(_HOME_ENV) or DEFAULT_HOME
    return os.path.expanduser(root)

# A shell command that fails for any reason (non-zero exit, missing shell,
# OSError) is a *failed* verify — we never claim a project passed when we could
# not confirm it (the "never report a state you have not verified" principle).
DEFAULT_BASE = review.DEFAULT_BASE


class _Proc:
    """Minimal process-shaped object for the injectable runner fallback."""

    def __init__(self, cmd, returncode, stdout="", stderr=""):
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _default_run(cmd, repo):
    """Production runner: run a git command in ``repo`` (subprocess argv form)."""
    try:
        return subprocess.run(
            cmd,
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _Proc(cmd, 128, "", str(exc))


def _default_run_verify(command: str):
    """Production verify runner: run a shell command string.

    The registry's ``verify`` field is a free-form shell command (e.g. ``cd
    ~/dev/hscc && ./scripts/run_tests.sh``), so this runs it through a shell
    and reports pass/fail by exit code. A failure to even start (missing shell)
    degrades to a failed verify.
    """
    try:
        return subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _Proc(command, 128, "", str(exc))


def _dispatch(cmd, repo, runner):
    if runner is not None:
        return runner(cmd, repo)
    return _default_run(cmd, repo)


def _dispatch_verify(command, runner):
    if runner is not None:
        return runner(command)
    return _default_run_verify(command)


# --------------------------------------------------------------------------- #
# Selection: which cards are genuinely awaiting review
# --------------------------------------------------------------------------- #


def _awaiting_review(card: dict, project: registry.Project, *, _run=None) -> bool:
    """True if ``card`` is a card the operator must test by hand.

    A card qualifies only when BOTH hold:
      1. its status is review-required/blocked (kanban.REVIEW_STATUSES), and
      2. its branch is genuinely unmerged — NOT an ancestor of ``main``.

    The merged case is the phantom that inflated the backlog; a merged branch's
    work already shipped and needs no manual testing here. A card whose branch
    cannot be resolved is treated as NOT merged (conservative — we refuse to
    rule out work we cannot verify), so it still qualifies for review.
    """
    if str(card.get("status") or "") not in kanban.REVIEW_STATUSES:
        return False
    branch = card.get("branch")
    if not branch:
        return False
    merged = git_state.is_merged(project.repo, branch, DEFAULT_BASE, _run=_run)
    return not merged


# --------------------------------------------------------------------------- #
# Project-level verify: has the project's automated check run and passed?
# --------------------------------------------------------------------------- #


def _project_verify(project: registry.Project, *, _run_verify=None) -> dict:
    """Run the project's registry ``verify`` command; report run/passed.

    Returns ``{"configured": bool, "run": bool, "passed": bool}``. A project
    with no ``verify`` command is ``configured=False`` (nothing was run). A
    configured command is run once and its exit code decides passed. A command
    that fails to start is ``run=True, passed=False`` — we never fabricate a
    pass.
    """
    command = project.verify
    if not command or not str(command).strip():
        return {"configured": False, "run": False, "passed": False}
    cp = _dispatch_verify(str(command), _run_verify)
    return {"configured": True, "run": True, "passed": cp.returncode == 0}


# --------------------------------------------------------------------------- #
# Collection + ordering
# --------------------------------------------------------------------------- #


def _collect(cards: list[dict], projects: list[registry.Project], *,
             _run=None, _run_verify=None) -> list[dict]:
    """Build the qa rows: one per genuinely-awaiting-review card.

    Each row carries every field the renderer needs:
    ``{project, repo, id, title, status, branch, unattributed, verify_present,
    verify, files, verify_configured, verify_run, verify_passed,
    created_at}``.

    Attribution is by workspace_path via ``kanban.project_for_card`` (a card
    belongs to the project whose repo its workspace_path resolves to) — the
    board slug is at most a narrowing filter on which board(s) we read, never
    the attribution rule. A card matching no project's repo is surfaced as
    UNATTRIBUTED, never silently dropped and never guessed into a project.

    Rows are sorted UNVERIFIABLE first, then by age (oldest ``created_at``
    first) within each group. A row with no usable timestamp sorts last within
    its group. Project verify runs once per project (deduped by project name);
    an UNATTRIBUTED card has no project, so no verify is ever run for it.
    """
    rows: list[dict] = []
    verify_cache: dict[str, dict] = {}

    for card in cards:
        project = kanban.project_for_card(card, projects)
        if project is kanban.UNATTRIBUTED:
            # No repo to check branch/merge facts against. Conservative: an
            # UNATTRIBUTED card in review status still qualifies — we cannot
            # rule out unmerged work, so we show it flagged UNATTRIBUTED. It is
            # never guessed into a project whose repo its workspace_path does
            # not live under.
            if str(card.get("status") or "") not in kanban.REVIEW_STATUSES:
                continue
            proj_name = "UNATTRIBUTED"
            proj_repo = None
            v = {"configured": False, "run": False, "passed": False}
        else:
            if not _awaiting_review(card, project, _run=_run):
                continue
            proj_name = project.name
            proj_repo = project.repo
            if proj_name not in verify_cache:
                verify_cache[proj_name] = _project_verify(project, _run_verify=_run_verify)
            v = verify_cache[proj_name]
        verify_present, verify_text = review._verify_line(card.get("body"))
        branch = card.get("branch")
        files = 0
        if proj_repo and branch:
            facts = review._branch_facts(proj_repo, branch, DEFAULT_BASE, _run=_run)
            files = facts["files"]
        rows.append(
            {
                "project": proj_name,
                "repo": proj_repo,
                "id": card.get("id"),
                "title": card.get("title") or "(untitled)",
                "status": card.get("status"),
                "branch": branch,
                "unattributed": project is kanban.UNATTRIBUTED,
                "verify_present": verify_present,
                "verify": verify_text,
                "files": files,
                "verify_configured": v["configured"],
                "verify_run": v["run"],
                "verify_passed": v["passed"],
                "created_at": card.get("created_at"),
            }
        )

    # Order: UNVERIFIABLE (no VERIFY line) first, then oldest created_at first
    # within each group. Missing/invalid timestamps sort last within a group.
    def _age(row):
        ts = row["created_at"]
        try:
            return int(ts) if ts is not None else None
        except (TypeError, ValueError):
            return None

    def _key(row):
        age = _age(row)
        group = 1 if row["verify_present"] else 0
        return (group, age if age is not None else float("inf"))

    rows.sort(key=_key)
    return rows


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def _render(rows: list[dict], manual: list[dict] | None = None) -> list[str]:
    """Render the pre-merge queue AND the manual-verification section.

    ``rows`` is the pre-merge queue (unchanged semantics). ``manual`` is the
    unchecked manual-QA entries, oldest first. The manual section is printed
    whenever there is anything to show, even if the pre-merge queue is empty —
    a merged item that still needs a human to physically test it must never
    disappear from the operator's view. ``None``/empty manual prints no section.
    """
    manual = manual or []
    lines: list[str] = []

    if rows:
        lines.append("MANUAL-TESTING QUEUE — what you actually have to test by hand:")
        lines.append("")
        for row in rows:
            head = f"{row['project']:<20} {row['id'] or '?':<14} {row['branch'] or '?':<24} {row['title']}"
            if not row["verify_present"]:
                head = f"UNVERIFIABLE  {head}"
            if row.get("unattributed"):
                head = f"UNATTRIBUTED  {head}"
            lines.append(head)
            lines.append(f"{'':22} files changed: {row['files']}")
            if row["verify_present"]:
                if row["verify"]:
                    lines.append(f"{'':22} VERIFY: {row['verify']}")
                else:
                    lines.append(f"{'':22} VERIFY: <present but empty — names a check but no command>")
            else:
                lines.append(f"{'':22} !!! NO VERIFY LINE — reverse-engineer what to test from the diff")
            # Project-level automated verify status.
            if not row["verify_configured"]:
                lines.append(f"{'':22} project verify: not configured (no 'verify' in registry)")
            elif not row["verify_run"]:
                lines.append(f"{'':22} project verify: not run")
            elif row["verify_passed"]:
                lines.append(f"{'':22} project verify: PASSED")
            else:
                lines.append(f"{'':22} project verify: FAILED")
            lines.append("")
    else:
        lines.append("nothing awaiting review — all open work is merged, running, or not review-required.")

    if manual:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("NEEDS MANUAL VERIFICATION — post-merge items a human must check by hand:")
        lines.append("")
        for entry in manual:
            head = f"{entry['project']:<20} {entry['id']:<14} {entry['description']}"
            if entry.get("card_id"):
                head += f"  (card {entry['card_id']})"
            lines.append(head)
            lines.append(f"{'':22} added {entry['added_at']}")
            lines.append("")
    return lines


def _render_json(rows: list[dict], manual: list[dict] | None = None) -> dict:
    """Stable --json shape. Keys never reorder; absent values stay stable.

    Returns ``{"queue": [...pre-merge rows...], "manual_qa": [...entries...]}``
    so the manual-verification section travels with the machine-readable
    output, not just the human rendering. Absent/empty manual_qa is ``[]``.
    """
    queue = []
    for row in rows:
        queue.append(
            {
                "project": row["project"],
                "card_id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "branch": row["branch"],
                "unverifiable": not row["verify_present"],
                "verify": row["verify"],
                "files_changed": row["files"],
                "verify_configured": row["verify_configured"],
                "verify_run": row["verify_run"],
                "verify_passed": row["verify_passed"],
                "created_at": row["created_at"],
            }
        )
    manual_qa = []
    for entry in (manual or []):
        manual_qa.append(
            {
                "id": entry.get("id"),
                "project": entry.get("project"),
                "description": entry.get("description"),
                "card_id": entry.get("card_id"),
                "added_at": entry.get("added_at"),
                "checked": bool(entry.get("checked")),
                "checked_at": entry.get("checked_at"),
            }
        )
    return {"queue": queue, "manual_qa": manual_qa}


# --------------------------------------------------------------------------- #
# notify — one message per card that ENTERS the needs-QA state
# --------------------------------------------------------------------------- #


def _state_path(_state=None) -> str:
    """Where the already-notified set is persisted (under ``qa_home()`` by default).

    An injected ``_state`` overrides the default so tests never touch the file
    under the user's home directory. Without an injection, the default resolves
    under ``qa_home()`` (``~/.flightdeck`` in production, or the ``HERMES_HOME``
    sandbox under test), never hardcoded to a real home. A leading ``~`` is
    expanded either way.
    """
    if _state:
        return os.path.expanduser(str(_state))
    return os.path.join(qa_home(), NOTIFY_STATE_DEFAULT)


def _load_notified(_state=None) -> set:
    """The set of card ids already notified (as currently in the queue).

    The state file is a bare YAML list of card ids. A missing file is an empty
    set; an unreadable/corrupt file degrades to an empty set — never a crash,
    because a transient read failure must not take the watch loop down.
    """
    path = _state_path(_state)
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        return {str(x) for x in data}
    except Exception:
        return set()


def _save_notified(ids, _state=None) -> None:
    """Persist the set of card ids that currently count as notified."""
    path = _state_path(_state)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(sorted(ids), f)


# --------------------------------------------------------------------------- #
# manual-qa store — post-merge items a human must verify by hand
# --------------------------------------------------------------------------- #

def _manual_path(_path=None) -> str:
    """Where the manual-QA store lives (under ``qa_home()`` by default).

    An injected ``_path`` overrides the default so tests never touch the file
    under the user's home directory. Without an injection, the default resolves
    under ``qa_home()`` (``~/.flightdeck`` in production, or the ``HERMES_HOME``
    sandbox under test), never hardcoded to a real home. A leading ``~`` is
    expanded either way.
    """
    if _path:
        return os.path.expanduser(str(_path))
    return os.path.join(qa_home(), MANUAL_QA_DEFAULT)


def _load_manual(_path=None) -> list[dict]:
    """The manual-QA entries currently in the store.

    The store is a bare YAML list of entries. A missing file is an empty list;
    an unreadable/corrupt file degrades to an empty list — never a crash,
    because a transient read failure must not take a command down and must
    never fabricate entries.
    """
    path = _manual_path(_path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        if not isinstance(data, list):
            return []
        return [{k: e.get(k) for k in (
            "id", "project", "description", "card_id", "added_at",
            "checked", "checked_at",
        )} for e in data if isinstance(e, dict)]
    except Exception:
        return []


def _save_manual(entries: list[dict], _path=None) -> None:
    """Persist the manual-QA store.

    Always writes the full current list — the store never deletes entries
    (checked ones stay for history); ``qa`` simply hides checked entries from
    the default view. The ``~/.flightdeck`` dir is created on demand, matching
    ``_save_notified``.
    """
    path = _manual_path(_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(entries, f, sort_keys=False)


def _new_manual_id(existing: list[dict]) -> str:
    """A fresh ``mqa-<8 hex>`` id that does not collide with existing entries."""
    import secrets

    used = {e.get("id") for e in existing}
    while True:
        candidate = f"mqa-{secrets.token_hex(4)}"
        if candidate not in used:
            return candidate


def _get_project(projects: list[registry.Project], project_name: str):
    """Return the Project row for ``project_name``, or None if absent.

    Same lookup message.py uses; kept local so qa.py stays self-contained.
    """
    for proj in projects:
        if proj.name == project_name:
            return proj
    return None


def cmd_qa_add(args: argparse.Namespace, projects: list[registry.Project]) -> int:
    """``qa add <project> "description" [--card ID]`` — append an unchecked entry.

    Validates the project against the registry (never silently create a
    dangling reference). No ``--apply`` gate: this is additive and low-risk,
    matching the ``message send`` precedent rather than ``migrate-card``'s.
    The project comes from the ``add`` subparser's ``add_project`` dest (see
    build_subparser: it must not collide with the top-level ``qa [project]``).
    """
    proj = _get_project(projects, args.add_project)
    if proj is None:
        print(
            f"error: no project named {args.add_project!r} in the registry",
            file=sys.stderr,
        )
        return 2

    entries = _load_manual(args.state)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(args.now()))
    entries.append(
        {
            "id": _new_manual_id(entries),
            "project": proj.name,
            "description": args.description,
            "card_id": getattr(args, "card", None),
            "added_at": now,
            "checked": False,
            "checked_at": None,
        }
    )
    _save_manual(entries, args.state)
    print(entries[-1]["id"])
    return 0


def cmd_qa_done(args: argparse.Namespace) -> int:
    """``qa done <id>`` — mark an entry checked, set checked_at. Refuse unknown."""
    entries = _load_manual(args.state)
    for entry in entries:
        if entry.get("id") == args.id:
            if entry.get("checked"):
                print(f"error: {args.id} is already checked", file=sys.stderr)
                return 2
            entry["checked"] = True
            entry["checked_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(args.now())
            )
            _save_manual(entries, args.state)
            return 0
    print(f"error: no manual-QA entry with id {args.id!r}", file=sys.stderr)
    return 2


def _project_topic(projects: list[registry.Project], project_name: str) -> int:
    """Resolve a project's Telegram topic id through the registry.

    Follows the message.py convention: a project with no configured topic is an
    actionable error (``project repair``), never a silent skip and never a guess.
    """
    for proj in projects:
        if proj.name == project_name:
            if proj.topic is None:
                raise telegram.TelegramError(
                    f"project {project_name} has no topic; "
                    f"run: flightdeck project repair {project_name}"
                )
            return proj.topic
    raise telegram.TelegramError(f"unknown project: {project_name!r}")


def _notify_one(row: dict, projects: list[registry.Project], *, _client=None) -> None:
    """Post one message to the project's topic naming the card, VERIFY, branch."""
    topic = _project_topic(projects, row["project"])
    lines = [
        f"QA needed: {row['title']}",
        f"card: {row['id']}",
        f"branch: {row['branch']}",
    ]
    if row["verify"]:
        lines.append(f"VERIFY: {row['verify']}")
    else:
        lines.append("VERIFY: <none — no VERIFY line on the card>")
    telegram.send_message(topic, "\n".join(lines), _client=_client)


def _run_notify(rows: list[dict], projects: list[registry.Project], *,
                _client=None, _state=None):
    """Notify only the cards that just ENTERED the needs-QA queue.

    The persisted set holds the cards we have already notified that are STILL in
    the queue. A card that stays across ticks stays in the set and is never
    re-notified (every-tick pings get muted). When a card leaves the queue it
    drops out of the set, so re-entering counts as a fresh transition and
    notifies again.

    A card that fails to notify (telegram error, no configured topic) is NOT
    added to the persisted set, so the next tick retries it; the failure is
    returned for reporting, never raised — the loop must not crash.

    Returns ``(newly_notified: list[str], errors: list[(card_id, message)])``.
    """
    current = {r["id"] for r in rows if r["id"] and not r["unattributed"]}
    notified = _load_notified(_state)
    entering = sorted(current - notified)
    newly: list[str] = []
    errors: list[tuple[str, str]] = []
    for rid in entering:
        row = next((r for r in rows if r["id"] == rid), None)
        if row is None:
            continue
        try:
            _notify_one(row, projects, _client=_client)
            newly.append(rid)
        except Exception as exc:  # never crash the loop on a notification failure
            errors.append((rid, str(exc)))
    keep = (notified & current) | set(newly)
    _save_notified(keep, _state)
    return newly, errors


def _report_notify_errors(errors: list[tuple[str, str]]) -> None:
    for rid, message in errors:
        print(f"notify failed for {rid}: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# watch — re-render the queue on an interval, kept alive by mandate
# --------------------------------------------------------------------------- #


def _sleep_seconds(seconds: float) -> None:
    """Production sleep; injectable so tests never really pause."""
    time.sleep(seconds)


def qa_frames(gather: Callable[[], list[dict]], *, interval: int,
              _sleep: Callable):
    """Yield one qa frame dict per refresh in watch mode.

    Each frame is ``{"rows": list, "lines": list[str], "error": str|None}``
    where ``lines`` is :func:`_render` of this frame's fresh ``rows`` — or,
    when the gather itself raised, the lines of the LAST good frame with
    ``error`` set so the caller keeps rendering a stale-but-real queue instead
    of crashing. ``rows`` always holds the frame's (possibly last-good) rows so
    the caller can run notify against fresh state. ``interval`` is passed to
    ``_sleep`` so the injected sleep is respected.

    Infinite by design; the shell below stops on ``KeyboardInterrupt``.
    """
    last_rows: list[dict] = []
    last_lines: list[str] = []
    while True:
        error = None
        try:
            rows = gather()
            lines = _render(rows)
            last_rows, last_lines = rows, lines
        except Exception as exc:
            rows, lines, error = last_rows, last_lines, f"refresh failed: {exc}"
        _sleep(interval)
        yield {"rows": rows, "lines": lines, "error": error}


def _clear() -> None:
    """Clear the screen between frames (ANSI erase + home)."""
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def _write_frame(frame: dict, *, timestamp: str, interval: int) -> None:
    """Print one watch frame: header (time + interval), then the queue body.

    The body is exactly what the one-shot command prints — the same renderer,
    byte-identical, only the header and clear differ. An error line, when a
    refresh failed, is appended under the stale-but-real body.
    """
    header = f"flightdeck qa --watch  [{timestamp}]  every {interval}s"
    sep = "-" * len(header)
    out = [header, sep]
    out.extend(frame["lines"])
    if frame["error"]:
        out.append("")
        out.append(frame["error"])
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def _watch(args: argparse.Namespace, projects: list[registry.Project],
           registry_path: str) -> int:
    """The --watch loop: redraw the queue every ``interval`` seconds.

    Same renderer as one-shot. Clears between frames, prints a timestamp +
    interval header, keeps the last good frame when a refresh fails, runs
    ``--notify`` on fresh data each pass, and exits 0 with no traceback on
    Ctrl-C.
    """
    interval = max(1, int(args.interval))

    def gather() -> list[dict]:
        return _gather_rows(args, projects)

    try:
        for frame in qa_frames(gather, interval=interval, _sleep=args.sleep):
            if frame["error"] is None:
                newly, errors = _run_notify(
                    frame["rows"], projects,
                    _client=getattr(args, "client", None),
                    _state=getattr(args, "state", None),
                )
                _report_notify_errors(errors)
                if newly:
                    print(
                        f"notified {len(newly)} card(s) entering the QA queue",
                        file=sys.stderr,
                    )
            _clear()
            _write_frame(
                frame,
                timestamp=time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(args.now())
                ),
                interval=interval,
            )
    except KeyboardInterrupt:
        return 0
    return 0


# --------------------------------------------------------------------------- #
# command entry
# --------------------------------------------------------------------------- #


def _gather_rows(args: argparse.Namespace, projects: list[registry.Project]) -> list[dict]:
    """The shared rows pipeline: read cards, collect, apply the project filter.

    One code path serves both the one-shot command and every ``--watch`` frame,
    so they cannot drift. Card reads come from ``args.cards`` (set by ``run``
    through core/kanban) and every git/verify call flows through the injectable
    ``args.run`` / ``args.run_verify`` seams.
    """
    cards = getattr(args, "cards", None)
    if cards is None:
        cards = kanban.list_cards()
    rows = _collect(cards, projects, _run=args.run, _run_verify=args.run_verify)
    want = getattr(args, "project", None)
    if want:
        rows = [r for r in rows if r["project"] == want]
    return rows


def cmd_qa(args: argparse.Namespace) -> int:
    """Compute and present the manual-testing queue. Returns exit code."""
    import json

    json_out = bool(getattr(args, "json", False))

    projects = registry.load_registry(args.registry)

    # Optional project filter: restrict the queue to one named project.
    # The filter narrows WHICH project's rows are shown — it does NOT change
    # attribution, which is always resolved against the full registry (a card
    # whose workspace_path resolves to a registered repo belongs to that
    # project; filtering out that project would otherwise relabel it
    # UNATTRIBUTED, which is wrong).
    #
    # When no project is given explicitly, auto-detect it from the cwd (the
    # project whose repo contains the current directory). This is a strictly
    # additive convenience: from outside any registered repo there is no match,
    # so `qa` with no argument still renders the whole fleet exactly as before.
    # `add`/`done` are routed before this in run() and always carry an explicit
    # project/id, so they are unaffected.
    want = getattr(args, "project", None)
    if want:
        wanted = {p.name for p in projects}
        if want not in wanted:
            print(f"error: no project named {want!r} in the registry", file=sys.stderr)
            return 2
    else:
        want, _detected = registry.resolve_project_arg(
            projects, None,
            cwd=getattr(args, "cwd", None),
            _print=lambda line: print(line, file=sys.stderr),
        )
        # Normalise the resolved selection back onto args so the shared
        # pipeline (_gather_rows, the manual filter) applies the SAME filter.
        args.project = want

    if getattr(args, "watch", False):
        return _watch(args, projects, args.registry)

    rows = _gather_rows(args, projects)

    # Manual-QA store, filtered by the same [project] filter as the queue.
    # Checked entries stay in the store but drop out of this default view.
    # Unchecked entries are shown oldest first, matching the queue's ordering.
    manual = _load_manual(getattr(args, "state", None))
    if want:
        manual = [e for e in manual if e.get("project") == want]
    unchecked = [e for e in manual if not e.get("checked")]
    unchecked.sort(key=lambda e: e.get("added_at") or "")

    if getattr(args, "notify", False):
        newly, errors = _run_notify(
            rows, projects,
            _client=getattr(args, "client", None),
            _state=getattr(args, "state", None),
        )
        _report_notify_errors(errors)
        if newly:
            print(
                f"notified {len(newly)} card(s) entering the QA queue",
                file=sys.stderr,
            )

    if json_out:
        print(json.dumps(_render_json(rows, unchecked)))
        return 0
    for line in _render(rows, unchecked):
        print(line)
    return 0


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "qa",
        help="the manual-testing queue: what you actually have to test by hand",
        epilog='example: flightdeck qa add flightdeck "verify the new dashboard loads"',
    )
    # `qa` takes ONE variadic positional (``project``) that serves every form:
    # the bare render ``qa`` / ``qa <project>``, ``qa add <project> \"desc\"``,
    # and ``qa done <id>``. We deliberately DO NOT register ``add``/``done`` as
    # argparse subparsers: argparse cannot mix a sibling positional (the
    # render's ``<project>``) with nested subparsers on one parser — the
    # subparsers action greedily swallows the first positional token and
    # rejects any project name that isn't literally a subcommand as an
    # "invalid choice", breaking ``qa <project>``. Instead, all tokens land in
    # ``project`` and ``run()`` reads the leading token to decide which form
    # was requested (a real subcommand name is unambiguous — no project is
    # named ``add`` or ``done``). See ``run()`` for the dispatch.
    p.add_argument(
        "project",
        nargs="*",
        default=[],
        help=(
            "render the queue, or restrict it to one named project; "
            "or `add <project> \"what to check\" [--card CARD_ID]` to file a "
            "post-merge item a human must verify by hand, "
            "or `done <id>` to mark one checked"
        ),
    )
    p.add_argument(
        "--card",
        metavar="CARD_ID",
        default=None,
        help="optional traceability back to an archived kanban card (with `add`)",
    )
    p.add_argument(
        "--watch",
        action="store_true",
        help="redraw the queue every --interval seconds until interrupted",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        metavar="N",
        help=f"seconds between --watch frames (default: {DEFAULT_INTERVAL})",
    )
    p.add_argument(
        "--notify",
        action="store_true",
        help=(
            "when a card enters the needs-QA queue, post one message to its "
            "project's Telegram topic (once per card, on the transition)"
        ),
    )
    p.set_defaults(func=cmd_qa)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py. Attaches injectable seams and reads the board.

    ``cards`` is loaded here through core/kanban (which is itself injectable
    inside core) for the render path; ``qa add``/``qa done`` touch only the
    manual-QA store and never read the board. The command's git/verify runners
    default to the real subprocess implementations unless supplied on ``args``.
    ``--watch`` adds ``args.now``/``args.sleep`` (clock + pause, both
    injectable) and ``--notify`` adds ``args.client`` (the Telegram MCP client)
    and ``args.state`` (the notified-set file); the ``add``/``done`` forms
    reuse the same ``args.state`` for the manual-QA store, each defaulting to
    the real production behaviour when a test does not inject a fake.

    Dispatch: because argparse cannot nest real subcommands under a parser that
    also has a sibling positional, the parser (see ``build_subparser``) always
    puts every token into ``args.project`` (a list). This method reads the
    leading token to tell the three forms apart. ``add``/``done`` return
    BEFORE any board read keeps them board-independent — a deliberate property.
    """
    args.registry = registry_path
    args.run = getattr(args, "run", None)
    args.run_verify = getattr(args, "run_verify", None)
    args.json = getattr(args, "json", False)
    args.now = getattr(args, "now", None) or time.time
    args.sleep = getattr(args, "sleep", None) or _sleep_seconds
    args.client = getattr(args, "client", None)
    args.state = getattr(args, "state", None)
    args.cwd = getattr(args, "cwd", None)

    projects = registry.load_registry(registry_path)

    # The parser always provides ``project`` as a list. A synthetic namespace
    # built directly in a test may pass a bare string instead — normalize so
    # dispatch reads tokens uniformly either way.
    raw_project = getattr(args, "project", None)
    if isinstance(raw_project, str):
        toks = [raw_project]
    elif raw_project:
        toks = list(raw_project)
    else:
        toks = []
    args.project = toks  # normalize for downstream/render

    if toks and toks[0] == "add":
        # `qa add <project> "description" [--card CARD_ID]`
        if len(toks) < 3:
            print(
                'usage: flightdeck qa add <project> "description" [--card CARD_ID]',
                file=sys.stderr,
            )
            return 2
        args.add_project = toks[1]
        args.description = " ".join(toks[2:])
        # Board-independent: only validates the registry and writes the store.
        return cmd_qa_add(args, projects)

    if toks and toks[0] == "done":
        # `qa done <id>`
        if len(toks) < 2:
            print("usage: flightdeck qa done <id>", file=sys.stderr)
            return 2
        args.id = toks[1]
        # Board-independent: only edits the store.
        return cmd_qa_done(args)

    # Render path: `qa` (all projects) or `qa <project>` (filtered).
    args.project = toks[0] if toks else None

    if getattr(args, "cards", None) is None:
        try:
            args.cards = kanban.list_cards()
        except kanban.KanbanError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    return cmd_qa(args)
