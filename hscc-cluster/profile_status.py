"""Per-profile kanban task status.

Counts in-progress kanban tasks per profile from ~/.hermes/kanban.db.
Best-effort: returns an empty dict if the database is missing or unreadable,
never raises.
"""
import os
import sqlite3
from typing import Dict


DEFAULT_KANBAN_DB = os.path.expanduser("~/.hermes/kanban.db")


def get_profile_task_counts(kanban_db: str = DEFAULT_KANBAN_DB) -> Dict[str, int]:
    """Return {profile_name: running_task_count} from the kanban DB.

    Handles missing database, missing columns, and corrupt data gracefully.
    """
    if not os.path.exists(kanban_db):
        return {}

    try:
        conn = sqlite3.connect(kanban_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Query running tasks grouped by assignee (profile name).
        # The tasks table has an 'assignee' column that stores the profile name.
        cur.execute(
            "SELECT assignee, COUNT(*) as cnt "
            "FROM tasks "
            "WHERE status = 'running' "
            "AND assignee IS NOT NULL "
            "AND assignee != '' "
            "GROUP BY assignee"
        )

        result: Dict[str, int] = {}
        for row in cur.fetchall():
            name = row["assignee"]
            count = row["cnt"]
            if name and count > 0:
                result[name] = count

        conn.close()
        return result

    except (sqlite3.Error, KeyError, TypeError):
        return {}


def get_profile_status(kanban_db: str = DEFAULT_KANBAN_DB) -> dict:
    """Return a full status dict for hscc-cluster CLI output.

    Structure:
    {
        "counts": {"devops-engineer": 3, "worker": 5},
        "total_running": 8,
        "profiles": ["devops-engineer", "worker"]
    }
    """
    counts = get_profile_task_counts(kanban_db)
    return {
        "counts": counts,
        "total_running": sum(counts.values()),
        "profiles": sorted(counts.keys()),
    }