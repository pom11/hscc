"""HSCC HTTP API — read-only cron / scheduled-jobs roster.

Exposes ``GET /v1/cron/list``, the single read-only endpoint the iOS cron
view is blocked on (see ios-app/docs/cron-view-gap.md). It returns ALL Hermes
cron jobs — active AND paused — each mapped 1:1 onto the on-disk
``~/.hermes/cron/jobs.json`` fields the contract names:

  { id, name, schedule_display, enabled, state, next_run_at, last_run_at,
    last_status, last_error }

This is a pure read of existing state: no new backend collection, no writes,
no confirm gate, and it performs no side effects. The backing reader lives in
``hscc_daemon.autodown`` (``list_all_cron_jobs``), a sibling of the existing
``list_active_cron_jobs`` that the autodown status handler already uses — so
there is one code path that knows how to read jobs.json, not two.

Conventions (design §A/§B, shared — mirror routes_commands / routes_autodown):
  * handler is ``(server, ctx, query, body) -> (status, payload_dict)``;
  * ``speak`` is ALWAYS present on a read response (design §B);
  * every backing call goes through a ``_backing_*`` module function so tests
    can monkeypatch them without ever touching the operator's live jobs.json;
  * if the store can't be read, degrade to a 200 with an honest ``speak``
    (never fabricate a job list, never crash).
"""

from __future__ import annotations

import re
from pathlib import Path

from api_server import ROUTES  # noqa: E402

# Sentinel meaning "could not determine" — mirrors hscc_daemon.autodown's
# CRON_UNREADABLE (which is what the backing returns on a bad/absent store).
CRON_UNREADABLE = "<unreadable>"

# Make hscc_daemon importable (sibling of hscc-api/) — same seam as
# routes_cluster.py::_ensure_repo_root_on_path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(_REPO_ROOT))


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_list_all_cron_jobs():
    """Return ALL cron jobs (active + paused), or ``CRON_UNREADABLE``.

    Delegates to ``hscc_daemon.autodown.list_all_cron_jobs``. The sentinel is
    ``CRON_UNREADABLE`` when jobs.json cannot be read/malformed — the handler
    degrades honestly on that, never fabricating a list.
    """
    from hscc_daemon import autodown
    return autodown.list_all_cron_jobs()


# --------------------------------------------------------------------------- #
# speak helper (design §B) — pure
# --------------------------------------------------------------------------- #

def _speak_cron_list(jobs):
    """§B: summarise the roster, e.g. "11 scheduled jobs (2 active)."."""
    if jobs is None:
        return "Scheduled-jobs list unavailable."
    n = len(jobs)
    active = sum(1 for j in jobs if j.get("enabled"))
    plural = "job" if n == 1 else "jobs"
    if n == 0:
        return "No scheduled jobs."
    return f"{n} scheduled {plural} ({active} active, {n - active} paused)."


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_cron_list(server, ctx, query, body):
    """GET /v1/cron/list — read-only roster of all scheduled jobs.

    Returns an array of jobs under ``jobs``, each mapped 1:1 from the on-disk
    jobs.json contract (id, name, schedule_display, enabled, state,
    next_run_at, last_run_at, last_status, last_error), active and paused
    alike. A single ``speak`` field summarises. If the store cannot be read,
    degrades to a 200 with an honest ``speak`` rather than a fabricated list.
    """
    try:
        jobs = _backing_list_all_cron_jobs()
    except Exception:
        # Any unexpected backing failure degrades to an honest speak, never a
        # crash and never a fabricated list.
        jobs = CRON_UNREADABLE
    if not isinstance(jobs, list):
        return 200, {"speak": _speak_cron_list(None)}
    return 200, {"jobs": jobs, "speak": _speak_cron_list(jobs)}


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/cron/list$"), handle_cron_list))
