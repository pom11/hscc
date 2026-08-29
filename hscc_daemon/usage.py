"""Fleet usage accounting — per-bot and per-project token + cost.

Reads each Hermes profile's ``state.db`` ``sessions`` table (the SAME store
Hermes writes on every turn) and aggregates tokens/cost per bot (profile) and
per project. Best-effort, exactly like ``stats.py``: never raises on a missing
or unreadable profile DB.

Attribution model
-----------------
A *bot* is a Hermes profile. A *project* is attributed from the project
orchestrator profiles that follow the ``<project>-orch`` naming convention
(the documented shape HSCC uses for per-project orchestrators — see
``hscc_daemon/replay.py`` and the ``-orch`` profiles on the operator host). A
``<project>-orch`` profile's sessions count toward ``<project>``. Profiles that
do not match ``-orch`` are project-less bots and appear under ``per_bot`` only.

Cost honesty
------------
The DB records ``estimated_cost_usd`` / ``actual_cost_usd`` and a
``cost_source`` per session. When the operator's Hermes prices usage, those
columns carry real values and we surface them verbatim. When cost is not
tracked (``cost_source`` = 'none' / cost columns are 0), we report that state
honestly rather than inventing a price: ``spent_usd`` is the sum of whatever
the DB records, and the response carries ``cost_tracked: false`` so the client
can say "cost not tracked" instead of implying a real spend. Token counts are
always real (Hermes records them regardless).

A budget warning is derived from ``~/.hscc/budget.json`` (``{"budget_usd": N}``)
or a default; ``exceeded`` is true only when a real tracked spend exceeds it —
never fabricated from zero cost.
"""

import json
import os
import sqlite3

# Default budget in USD when ~/.hscc/budget.json is absent.
DEFAULT_BUDGET_USD = 500.0
BUDGET_FILE = os.path.expanduser("~/.hscc/budget.json")

# Profiles that match /-orch$/ are project orchestrators; strip the suffix to
# get the project name. Anything else is a project-less bot.
_ORCH_SUFFIX = "-orch"


def _sum_sessions(db_path):
    """Sum the sessions rows of one profile DB.

    Returns an (empty-tolerant) dict of token/cost totals plus the count of
    session rows read, or None if the DB is missing/unreadable/has no sessions
    table. Never raises.
    """
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT "
            "COUNT(*) , "
            "COALESCE(SUM(input_tokens),0) , "
            "COALESCE(SUM(output_tokens),0) , "
            "COALESCE(SUM(cache_read_tokens),0) , "
            "COALESCE(SUM(cache_write_tokens),0) , "
            "COALESCE(SUM(reasoning_tokens),0) , "
            "COALESCE(SUM(estimated_cost_usd),0) , "
            "COALESCE(SUM(actual_cost_usd),0) "
            "FROM sessions"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    sessions = int(row[0] or 0)
    input_tokens = int(row[1] or 0)
    output_tokens = int(row[2] or 0)
    cache_read_tokens = int(row[3] or 0)
    cache_write_tokens = int(row[4] or 0)
    reasoning_tokens = int(row[5] or 0)
    estimated_cost = float(row[6] or 0.0)
    actual_cost = float(row[7] or 0.0)

    spent = estimated_cost if estimated_cost else actual_cost
    return {
        "sessions": sessions,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": (input_tokens + output_tokens
                         + cache_read_tokens + cache_write_tokens
                         + reasoning_tokens),
        "cost_usd": spent,
    }


def _read_cost_columns(db_path):
    """Read the richest per-session cost status for a profile DB.

    Returns a dict {cost_status, cost_source, cost_tracked} or None when the
    DB is unreadable. cost_tracked is True when any session reports a real
    non-none cost_source (i.e. Hermes actually priced usage).
    """
    if not os.path.isfile(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        status = conn.execute(
            "SELECT cost_status, cost_source FROM sessions "
            "WHERE cost_source IS NOT NULL AND cost_source != 'none' "
            "AND cost_source != '' LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if status is None:
        return {"cost_status": None, "cost_source": None, "cost_tracked": False}
    return {
        "cost_status": status[0],
        "cost_source": status[1],
        "cost_tracked": True,
    }


def _aggregate(rows):
    """Sum a list of per-session-total dicts (produced by _sum_sessions)."""
    total = {
        "sessions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
    for r in rows:
        if r is None:
            continue
        for key in ("sessions", "input_tokens", "output_tokens",
                    "cache_read_tokens", "cache_write_tokens",
                    "reasoning_tokens", "total_tokens"):
            total[key] += r.get(key, 0)
        total["cost_usd"] += r.get("cost_usd", 0.0)
    return total


def _load_budget(budget_file=BUDGET_FILE, default=DEFAULT_BUDGET_USD):
    """Read budget_usd from budget.json, falling back to the default.

    Returns (budget_usd, configured: bool). A malformed/missing file yields
    the default with configured=False. Never raises.
    """
    try:
        with open(budget_file, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("budget_usd")
        budget = float(raw) if isinstance(raw, (int, float)) else default
    except (OSError, ValueError, TypeError):
        return default, False
    return budget, True


def compute_usage(profiles_home=None, budget_file=BUDGET_FILE,
                  default_budget=DEFAULT_BUDGET_USD):
    """Aggregate per-bot / per-project token + cost across all profiles.

    Args:
        profiles_home: dir containing per-profile state.db files. Defaults to
            ~/.hermes/profiles.
        budget_file: path to budget.json (test seam).
        default_budget: fallback budget when budget_file is absent.

    Returns a dict:
        {
            "per_bot": {profile: totals...},
            "per_project": {project: totals...},
            "total": totals...,
            "cost_tracked": bool,
            "budget": {budget_usd, spent_usd, pct, exceeded, remaining_usd,
                       configured},
        }
    Best-effort: skips profiles whose DB is missing/unreadable.
    """
    if profiles_home is None:
        profiles_home = os.path.expanduser("~/.hermes/profiles")

    per_bot = {}
    per_project = {}

    entries = []
    try:
        entries = sorted(os.listdir(profiles_home))
    except OSError:
        entries = []

    for name in entries:
        # Skip the resource files that live beside profile dirs (e.g. *.db).
        db_path = os.path.join(profiles_home, name, "state.db")
        subtotal = _sum_sessions(db_path)
        if subtotal is None:
            continue  # no state.db / unreadable — skip, don't raise

        per_bot[name] = subtotal

        if name.endswith(_ORCH_SUFFIX):
            project = name[: -len(_ORCH_SUFFIX)]
            existing = per_project.get(project)
            per_project[project] = (
                _aggregate([existing, subtotal]) if existing else subtotal
            )

    # Fleet total across every tracked bot.
    total = _aggregate(list(per_bot.values()))

    # Cost honesty: True only when at least one profile actually priced usage.
    cost_tracked = False
    for name in per_bot:
        cols = _read_cost_columns(os.path.join(profiles_home, name, "state.db"))
        if cols and cols.get("cost_tracked"):
            cost_tracked = True
            break

    budget, configured = _load_budget(budget_file, default_budget)
    spent = total["cost_usd"]
    pct = round((spent / budget * 100.0), 1) if budget else 0.0
    exceeded = bool(budget and spent > budget)
    remaining = round(budget - spent, 2) if budget else 0.0

    return {
        "per_bot": per_bot,
        "per_project": per_project,
        "total": total,
        "cost_tracked": cost_tracked,
        "budget": {
            "budget_usd": budget,
            "spent_usd": round(spent, 2),
            "pct": pct,
            "exceeded": exceeded,
            "remaining_usd": remaining,
            "configured": configured,
        },
    }
