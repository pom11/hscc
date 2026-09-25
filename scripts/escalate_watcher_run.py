#!/usr/bin/env python3
"""Escalate repeatedly-failing kanban tasks, and alert the human when the
strong tier also fails.

Run on the gateway via Hermes native cron (``--no-agent``). Alerts are sent
directly as native desktop notifications through ``hscc_daemon.desktop.
send_desktop_notification()`` (NOT via ``--deliver desktop`` — hermes cron
silently resolves that to no target). The script prints to stdout only as a
run-history record; the notification is what actually reaches the operator.
An idle run sends nothing, so only real escalations notify.

For each task with ``consecutive_failures >= fail_limit``
(``hscc_daemon.escalate.decide_escalation``):
  * acting reassign is OPT-IN — it happens only when ``HSCC_STRONG_PROFILE``
    is explicitly set to a profile that can execute the card. With no
    explicit strong profile (the default), at-threshold failures are reported
    to a human instead of silently reassigning the card.
  * strong tier also failing     → flag a human (announced once, then deduped).

Cross-run dedup for the human alert lives in ``~/.hscc/escalated.json`` so a
task stuck at the strong tier is not re-announced every tick; a task that
recovers is forgotten, so a later re-failure alerts again.

Run under the Hermes venv python (imports ``hermes_cli`` + ``hscc_daemon``):

    ~/.hermes/hermes-agent/venv/bin/python scripts/escalate_watcher_run.py

Config via env (sensible defaults):
  HSCC_FAIL_LIMIT      consecutive failures before escalating   (default: 3)
  HSCC_STRONG_PROFILE  the "strong tier" profile                (default: none —
                       unset ⇒ at-threshold failures are REPORTED to a human,
                       never silently reassigned)
  HSCC_ESCALATED_STATE dedup state file                         (default: ~/.hscc/escalated.json)
"""

import json
import os
import sys

# Make both the runtime (hermes_cli.kanban_db) and the installed HSCC plugins
# (hscc_daemon.escalate_watcher) importable however this is launched.
for _p in (os.path.expanduser("~/.hermes/hermes-agent"),
           os.path.expanduser("~/.hermes/plugins")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FAIL_LIMIT = int(os.environ.get("HSCC_FAIL_LIMIT", "3"))
# Acting reassign is OPT-IN: only set when the operator explicitly configured a
# strong profile. Default None (not "architect") — silently reassigning cards to
# a profile that could not execute them corrupted the assignment and forced
# manual assign+unblock recovery. Unset here, at-threshold failures are REPORTED
# to a human (see main()).
STRONG_PROFILE = os.environ.get("HSCC_STRONG_PROFILE") or None
STATE_PATH = os.path.expanduser(
    os.environ.get("HSCC_ESCALATED_STATE", "~/.hscc/escalated.json")
)


def _load_notified():
    """Load the set of already-human-alerted task ids. Best-effort → empty."""
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(data)
    except (OSError, ValueError):
        pass
    return set()


def _save_notified(notified):
    """Persist the dedup set. Best-effort — never raise into the cron run."""
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sorted(notified), f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def _deliver(body):
    """Send the alert as a native desktop notification.

    hermes cron ``--deliver desktop`` resolves to no target (silent no-op,
    verified in cron/scheduler_delivery.py), so the watcher notifies directly.
    Best-effort — never raise into the cron run.
    """
    try:
        from hscc_daemon import desktop

        desktop.send_desktop_notification(
            "HSCC escalation", body, priority="critical",
            app_id="com.hermes.hscc_daemon",
        )
    except Exception:
        pass


def main():
    from hscc_daemon import escalate_watcher

    notified = _load_notified()
    before = set(notified)

    # Drive the human alert via a native desktop notification (not --deliver
    # desktop): the returned actions + the before/after dedup diff decide what
    # we notify.
    actions = escalate_watcher.scan_and_escalate(
        fail_limit=FAIL_LIMIT,
        strong_profile=STRONG_PROFILE,
        _notified=notified,
        _notify=lambda title, body: None,
    )

    human_now = {a["task"] for a in actions if a["action"] == "human"}
    # Forget tasks no longer stuck-at-strong so a future re-failure re-alerts.
    _save_notified(notified & human_now)

    lines = []
    for a in actions:
        act = a["action"]
        cat = a.get("category", "?")
        if act == "escalate":
            lines.append(
                f"⚠️ HSCC escalation: task {a['task']} → strong tier "
                f"({a['to']}) [category: {cat}]"
            )
        elif act == "escalate_failed":
            lines.append(
                f"❗ HSCC escalation FAILED to reassign task {a['task']} → "
                f"{a['to']} — reassign it by hand [category: {cat}]"
            )
        elif act == "human" and a["task"] in (human_now - before):
            # Only the newly-flagged human alerts; deduped across runs.
            lines.append(
                f"🚨 HSCC task {a['task']} needs a HUMAN: strong tier is also "
                f"failing [category: {cat}]"
            )

    if lines:
        body = "\n".join(lines)
        print(body)  # run-history record (hermes cron runs)
        _deliver(body)  # the notification that reaches the operator
    return 0


if __name__ == "__main__":
    # This runs as its own process from the `hscc-escalate-watcher` cron, so it
    # never passes through run_daemon_loop and does not inherit the daemon's
    # private umask. Without this it recreates ~/.hscc/escalated.json 0644 —
    # which is exactly what happened after the daemon-side fix looked complete.
    os.umask(0o077)
    sys.exit(main())
