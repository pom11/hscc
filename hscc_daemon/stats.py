"""Fleet analytics — aggregate HSCC jsonl activity logs into stats.

Best-effort: never raises on missing files or malformed lines.
"""

import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone


def _parse_iso(ts):
    """Parse an ISO timestamp string, tolerating trailing Z. Returns datetime or None."""
    if not isinstance(ts, str):
        return None
    try:
        ts = ts.rstrip("Z")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _read_jsonl(path):
    """Yield parsed JSON objects from a JSONL file, skipping bad lines."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        yield obj
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        return
    except OSError:
        return


def compute_stats(since_days=7, hscc_dir="~/.hscc"):
    """Aggregate jsonl activity logs into fleet stats.

    Returns a dict:
        {
            "since_days": since_days,
            "completions": {"total": int, "by_profile": {...}, "by_day": {...}},
            "activity": {"tool_calls_by_profile": {...}, "top_tools": [[name, count], ...]}
        }
    """
    hscc_dir = os.path.expanduser(hscc_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    # --- task_completions ---
    total = 0
    by_profile = Counter()
    by_day = Counter()

    for record in _read_jsonl(os.path.join(hscc_dir, "task_completions.jsonl")):
        ts = _parse_iso(record.get("completed_at"))
        if ts is None:
            continue
        # Make naive datetimes UTC-aware for comparison
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        total += 1
        profile = record.get("profile_name")
        if profile is not None:
            by_profile[profile] += 1
        day = ts.strftime("%Y-%m-%d")
        by_day[day] += 1

    # --- tool_events ---
    tool_calls_by_profile = Counter()
    tools_counter = Counter()

    for record in _read_jsonl(os.path.join(hscc_dir, "tool_events.jsonl")):
        ts = _parse_iso(record.get("timestamp"))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        profile = record.get("profile_name")
        tool = record.get("tool_name")
        if profile is not None:
            tool_calls_by_profile[profile] += 1
        if tool is not None:
            tools_counter[tool] += 1

    top_tools = [[name, count] for name, count in tools_counter.most_common(10)]

    return {
        "since_days": since_days,
        "completions": {
            "total": total,
            "by_profile": dict(by_profile),
            "by_day": dict(by_day),
        },
        "activity": {
            "tool_calls_by_profile": dict(tool_calls_by_profile),
            "top_tools": top_tools,
        },
    }


def format_stats(stats):
    """Return a compact human-readable summary of stats."""
    lines = []
    cd = stats.get("completions", {})
    ac = stats.get("activity", {})

    lines.append(f"Fleet stats (last {stats['since_days']} days)")
    lines.append(f"  Completions: {cd['total']}")

    if cd.get("by_profile"):
        top_profiles = sorted(cd["by_profile"].items(), key=lambda x: -x[1])[:5]
        lines.append("  Top profiles:")
        for profile, count in top_profiles:
            lines.append(f"    {profile}: {count}")

    if cd.get("by_day"):
        lines.append("  Completions by day:")
        for day in sorted(cd["by_day"]):
            lines.append(f"    {day}: {cd['by_day'][day]}")

    if ac.get("top_tools"):
        lines.append("  Top tools:")
        for tool, count in ac["top_tools"]:
            lines.append(f"    {tool}: {count}")

    if ac.get("tool_calls_by_profile"):
        top_profiles_activity = sorted(
            ac["tool_calls_by_profile"].items(), key=lambda x: -x[1]
        )[:5]
        lines.append("  Tool calls by profile:")
        for profile, count in top_profiles_activity:
            lines.append(f"    {profile}: {count}")

    return "\n".join(lines)
