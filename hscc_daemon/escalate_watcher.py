"""Failure-escalation watcher for live kanban tasks.

Scans the kanban DB for repeatedly-failing tasks, applies the pure
decision logic from escalate.py, and takes action:

  * "escalate"  — reassign the task to a strong-tier profile.
  * "human"     — notify a human operator.
  * "none"      — no action needed (below failure threshold).

Best-effort: never raises on missing DB, unreachable hermes CLI, or
absent notification subsystem.
"""

import json
import os
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


def scan_and_escalate(*, fail_limit=3, strong_profile="architect",
                       _kb=None, _reassign=None, _notify=None,
                       _notified=None):
    """Scan live kanban tasks and escalate repeated failures.

    Args:
        fail_limit: consecutive failures before escalation kicks in.
        strong_profile: profile name considered the "strong tier".
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

    if _kb is None:
        try:
            from hermes_cli import kanban_db as _kb
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
