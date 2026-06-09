"""Detect the configured sparkrun cluster (facts, no assumptions)."""
import json
import subprocess


def parse_clusters(raw):
    """Parse `sparkrun cluster list --json` output into a normalized dict.

    Returns {name, hosts, user, nas} for the default cluster (or the only one),
    or None if no clusters / unparseable. ``nas`` is the cache_dir or None.
    """
    try:
        clusters = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(clusters, list) or not clusters:
        return None
    chosen = next((c for c in clusters if c.get("default")), clusters[0])
    cache = (chosen.get("cache_dir") or "").strip()
    return {
        "name": chosen.get("name", ""),
        "hosts": list(chosen.get("hosts") or []),
        "user": chosen.get("user", ""),
        "nas": cache or None,
    }


def detect_cluster(timeout=10):
    """Run sparkrun + parse. Returns the normalized dict or None."""
    try:
        r = subprocess.run(
            ["sparkrun", "cluster", "list", "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return parse_clusters(r.stdout)


if __name__ == "__main__":
    c = detect_cluster()
    print(json.dumps(c) if c else "null")
