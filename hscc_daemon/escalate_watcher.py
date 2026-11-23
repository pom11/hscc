"""Failure-escalation watcher for live kanban tasks.

Scans the kanban DB for repeatedly-failing tasks, applies the pure
decision logic from escalate.py, and takes action:

  * "escalate"  — reassign the task to a strong-tier profile.
  * "human"     — notify a human operator.
  * "none"      — no action needed (below failure threshold).

A card the orchestrator has RETIRED is skipped entirely — no announce, no
reassign. Retirement is the exact marker phrase ``RETIRED_MARKER`` appearing in
one of the card's own comments (see ``_retired_comment_times``); the orchestrator
writes it when it dispositions a card out-of-band (superseded and re-cut, or
frozen as a crash artifact of record) and deliberately leaves it ``blocked`` with
a high ``consecutive_failures`` — which is precisely the shape the decision below
reads as "the strong tier is also failing". A retired card is never dispatched
again, so paging a human about it is pure noise. Live incident (t_0c602cd1):
``t_0f214ba0`` paged at 2026-11-23 07:45 — about two minutes BEFORE the
retirement comment was written (last ``crashed`` event 1795412630, marker
comment 1795412832), so nothing keyed on a comment could have suppressed that
first page. What this skip removes is every page AFTER a disposition: the
``~/.hscc/escalated.json`` dedupe holds a card's id only while it keeps
producing a ``human`` action and that file survives, so a reset state file, a
status change that drops the card from the candidate set for one tick, or a
fresh failure each re-page a card the dispatcher will never dispatch again.
Retirement is per-card state, not a lifetime mute: a card that is unblocked,
dispatched again and fails AGAIN re-announces (see ``_failed_after_retirement``).

Best-effort: never raises on missing DB, unreachable hermes CLI, or
absent notification subsystem. The two reads that implement the retirement skip
are advisory — if either fails, nothing is treated as retired and the watcher
behaves exactly as it did before, because weakening escalation is the worse
failure.
"""

import json
import os
import re
import shutil
import subprocess
import sys


def _default_reassign(task_id, to_profile):
    """Reassign a kanban task via the hermes CLI.

    Uses the venv-local ``hermes`` binary if discoverable, otherwise
    falls back to ``hermes`` on PATH.  Silently swallows errors — the
    caller still records the action attempt.
    """
    hermes = None
    # Try venv-local bin first
    if hasattr(sys, "real_prefix"):
        bin_dir = os.path.join(sys.real_prefix, "bin")
    elif hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix:
        bin_dir = os.path.join(sys.prefix, "bin")
    else:
        bin_dir = None

    if bin_dir:
        candidate = os.path.join(bin_dir, "hermes")
        if os.path.isfile(candidate):
            hermes = candidate

    if hermes is None:
        hermes = shutil.which("hermes")
    if hermes is None:
        return False

    try:
        result = subprocess.run(
            [hermes, "kanban", "reassign", task_id, to_profile],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def _default_notify(title, body):
    """Send a desktop notification, best-effort."""
    try:
        from hscc_daemon.desktop import send_macos_notification
        send_macos_notification(title, body)
    except Exception:
        pass


# The orchestrator's retirement marker (t_0c602cd1). Written as the first words
# of a comment by @hscc-orch on a card it has dispositioned out-of-band —
# superseded and re-cut under a new id, or frozen as the crash artifact of
# record — and left ``blocked`` on purpose. ``kanban_comment`` bodies are
# free text, so this is a phrase convention, not a schema field; it is matched
# below with whitespace/case/dash folding so that the WORDS decide, because the
# cost of missing one is the exact false page this code exists to stop (the
# operator types the dash by hand, and a hyphen is one keystroke from an em
# dash). The words themselves stay exact and in order: no partial-match slop.
RETIRED_MARKER = "SUPERSEDED \u2014 DO NOT DISPATCH"

# Dash-ish codepoints folded to "-" before comparing (em, en, minus, horizontal
# bar, hyphen, non-breaking hyphen).
_DASH_FOLD = {0x2014: "-", 0x2013: "-", 0x2212: "-", 0x2015: "-",
              0x2010: "-", 0x2011: "-"}

# task_runs.outcome/status values that mean "this attempt did not succeed".
# `blocked` is deliberately absent: a block is a disposition, not a failure, and
# treating one as a failure would un-mute a retired card on the very next tick.
_FAILED_RUN = ("crashed", "timed_out", "spawn_failed", "failed", "gave_up",
               "reclaimed")


def _fold(text):
    """Normalise a comment body for marker matching.

    Collapse whitespace (a marker wrapped across a line break is still the
    marker), fold the dash variants, casefold. Applied to BOTH sides so the
    comparison stays honest.
    """
    return " ".join((text or "").translate(_DASH_FOLD).split()).casefold()


_MARKER_FOLDED = _fold(RETIRED_MARKER)

# Word-boundary test on the folded phrase, so the marker has to appear as ITSELF
# and not as the middle of a longer word ("…DO NOT DISPATCHING that worker…" is
# prose about dispatching, not a disposition).
_MARKER_RE = re.compile(r"(?<!\w)" + re.escape(_MARKER_FOLDED) + r"(?!\w)")


def _is_retirement_comment(body):
    """True when `body` carries the retirement marker as a phrase."""
    return _MARKER_RE.search(_fold(body)) is not None


def _retired_comment_times(conn, task_ids):
    """Newest retirement-marker comment per candidate task: {task_id: created_at}.

    The comment text is read for the CANDIDATE set only (tasks that are already
    failing at least once and still dispatchable — a handful per tick), not for
    the whole board, so the extra read stays proportional to the work the tick
    can actually do. Comments are matched in Python rather than with SQL LIKE so
    the fold above is the single definition of "is the marker".

    Advisory: any error yields {} — "nothing is retired", which is the behaviour
    before this existed. A comment read that breaks must never mute a real
    escalation.
    """
    if not task_ids:
        return {}
    try:
        marks = ",".join("?" for _ in task_ids)
        rows = conn.execute(
            f"SELECT task_id, created_at, body FROM task_comments "
            f"WHERE task_id IN ({marks})",
            tuple(task_ids),
        ).fetchall()
    except Exception:
        return {}
    newest = {}
    for row in rows:
        try:
            if not _is_retirement_comment(row["body"]):
                continue
            task_id, created_at = row["task_id"], row["created_at"] or 0
        except Exception:
            continue  # an unreadable row is not evidence of retirement
        if created_at >= newest.get(task_id, 0):
            newest[task_id] = created_at
    return newest


def _failed_after_retirement(conn, retired):
    """Ids in `retired` that have a failed attempt ending AFTER their marker.

    Retirement is per-card state, not a lifetime mute: if the card is unblocked,
    dispatched again and fails again, a human must be paged about the new
    failure. The marker comment's own timestamp is the boundary — the crash the
    orchestrator retired the card FOR is by definition older than the comment
    that records the retirement, so only a failure that ENDED later counts as
    evidence the card came back.

    Advisory in the same direction as the read above: on error, nothing is
    revived (a card is only ever un-muted by positive evidence of a new failure).
    """
    if not retired:
        return set()
    try:
        marks = ",".join("?" for _ in retired)
        outcomes = ",".join("?" for _ in _FAILED_RUN)
        rows = conn.execute(
            f"SELECT task_id, MAX(ended_at) FROM task_runs "
            f"WHERE task_id IN ({marks}) "
            f"  AND COALESCE(outcome, status) IN ({outcomes}) "
            f"GROUP BY task_id",
            (*retired, *_FAILED_RUN),
        ).fetchall()
    except Exception:
        return set()
    revived = set()
    for row in rows:
        try:
            task_id, last_failed = row[0], row[1]
        except Exception:
            continue
        if last_failed and task_id in retired and last_failed > retired[task_id]:
            revived.add(task_id)
    return revived


def scan_and_escalate(*, fail_limit=3, strong_profile=None,
                       _kb=None, _reassign=None, _notify=None,
                       _notified=None):
    """Scan live kanban tasks and escalate repeated failures.

    Acting escalation (reassign) is OPT-IN: ``strong_profile`` defaults to
    None, so by default at-threshold failures route to the "human" alert
    instead of silently rewriting the card's assignee. Pass an explicit
    ``strong_profile`` (e.g. from ``HSCC_STRONG_PROFILE`` env) to turn the
    acting reassign back on.

    A card the orchestrator retired — one of its comments carries
    ``RETIRED_MARKER`` — is skipped outright: no entry in the returned list, no
    notify, no reassign. Unless a run has FAILED since that comment, which means
    the card was unblocked and is failing for real again.

    Args:
        fail_limit: consecutive failures before escalation kicks in.
        strong_profile: profile name considered the "strong tier". None/empty
            disables acting reassign (repeated failures alert a human).
        _kb: kanban_db module (injected for testing).
        _reassign: callable(task_id, to_profile) — injected for testing.
        _notify: callable(title, body) — injected for testing.
        _notified: set of already-notified task ids (injectable for
            testing; callers persist a set to ~/.hscc/escalated.json
            between runs for durability).

    Returns:
        list[dict] — one entry per action taken.  Empty list when no
        escalation was needed or the DB was unreachable.
    """
    if _notified is None:
        _notified = set()
    from hscc_daemon import escalate
    from hscc_daemon import hermes_import

    if _kb is None:
        try:
            # The whole resolve runs inside the agent-tree window (t_8eafef82):
            # a bare ``hermes_cli`` import here used to work only when some
            # other module in the process had left ~/.hermes/hermes-agent on
            # sys.path, so whether this watcher could see the board at all
            # depended on call order. Inside the window also covers the
            # ``kanban_db_connect`` import below, which is a second fresh
            # top-level import.
            with hermes_import.agent_tree_on_path():
                from hermes_cli import kanban_db as _kb
                # ``connect`` moved to ``hermes_cli.kanban_db_connect``; ``kanban_db``
                # retains it only as a removal-scheduled compat shim that emits
                # HermesPluginCompatWarning. Bind the canonical version from the new
                # module unless a test already set it explicitly on the module (only
                # ``connect_closing`` is used here, but bind both for consistency).
                if "connect" not in _kb.__dict__:
                    try:
                        from hermes_cli import kanban_db_connect as _kbc  # noqa: PLC0415
                        _kb.connect = _kbc.connect
                        _kb.connect_closing = _kbc.connect_closing
                    except Exception:
                        pass
        except Exception:
            return []

    if _reassign is None:
        _reassign = _default_reassign

    if _notify is None:
        _notify = _default_notify

    # ── Query failing tasks ──────────────────────────────────────────
    try:
        with _kb.connect_closing() as conn:
            rows = conn.execute(
                "SELECT id, assignee, consecutive_failures, "
                "status, last_failure_error "
                "FROM tasks "
                "WHERE status IN ('running', 'ready', 'blocked') "
                "  AND consecutive_failures >= 1"
            ).fetchall()
            # Advisory reads on the SAME connection (no second connect per tick).
            # Both are best-effort by construction; the outer guard is here so a
            # malformed row shape can never turn this pass into no-escalation-at-
            # all, which would be strictly worse than the false page it prevents.
            try:
                retired = _retired_comment_times(conn, [row["id"] for row in rows])
                revived = _failed_after_retirement(conn, retired)
            except Exception:
                retired, revived = {}, set()
    except Exception:
        return []

    actions = []

    for row in rows:
        task = {
            "id": row["id"],
            "assignee": row["assignee"],
            "consecutive_failures": row["consecutive_failures"],
            "status": row["status"],
            "last_failure_error": row["last_failure_error"],
        }

        # Retired by the orchestrator (its comment carries RETIRED_MARKER and
        # nothing has failed on the card since): skip BEFORE the decision, so a
        # retired card gets no announce AND no reassign. t_0c602cd1.
        if task["id"] in retired and task["id"] not in revived:
            continue

        decision = escalate.decide_escalation(
            task, fail_limit=fail_limit, strong_profile=strong_profile
        )

        action = decision["action"]

        if action == "escalate":
            ok = _reassign(task["id"], decision["reassign_to"])
            entry = {
                "task": task["id"],
                "action": "escalate",
                "to": decision["reassign_to"],
                "category": decision["category"],
            }
            if not ok:
                entry["ok"] = False
                entry["action"] = "escalate_failed"
            actions.append(entry)

        elif action == "human":
            if task["id"] not in _notified:
                title = f"Kanban task needs human attention: {task['id']}"
                body = f"{decision.get('reason', 'repeated failures')} " \
                       f"[category: {decision['category']}]"
                _notify(title, body)
                _notified.add(task["id"])
            actions.append({
                "task": task["id"],
                "action": "human",
                "category": decision["category"],
            })

        # "none" → skip

    return actions


if __name__ == "__main__":
    results = scan_and_escalate()
    print(json.dumps(results, indent=2))
