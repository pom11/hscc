#!/usr/bin/env python3
"""Escalate repeatedly-failing kanban tasks, and alert the human when the
strong tier also fails.

Run on the gateway via Hermes native cron (``--no-agent --deliver telegram``):
the script's stdout is delivered verbatim to the HSCC Telegram group, so an
empty run stays silent and only real escalations produce a message.

For each task with ``consecutive_failures >= fail_limit``
(``hscc_daemon.escalate.decide_escalation``):
  * not yet on the strong tier  → reassign it there (announced once).
  * strong tier also failing     → flag a human (announced once, then deduped).

Cross-run dedup for the human alert lives in ``~/.hscc/escalated.json`` so a
task stuck at the strong tier is not re-announced every tick; a task that
recovers is forgotten, so a later re-failure alerts again.

Run under the Hermes venv python (imports ``hermes_cli`` + ``hscc_daemon``):

    ~/.hermes/hermes-agent/venv/bin/python scripts/escalate_watcher_run.py

Config via env (sensible defaults):
  HSCC_FAIL_LIMIT      consecutive failures before escalating   (default: 3)
  HSCC_STRONG_PROFILE  the "strong tier" profile                (default: architect)
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
STRONG_PROFILE = os.environ.get("HSCC_STRONG_PROFILE", "architect")
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


def main():
    from hscc_daemon import escalate_watcher

    notified = _load_notified()
    before = set(notified)

    # Drive the human alert off stdout (Telegram), not the desktop notifier:
    # the returned actions + the before/after dedup diff decide what we print.
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
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
