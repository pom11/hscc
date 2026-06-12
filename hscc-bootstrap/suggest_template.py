"""Suggest a cluster template matching the detected cluster size.

Bootstrap calls this to print a *suggestion* only — it never applies anything.
The operator reviews and runs ``hscc-cluster cluster-template apply <name>
--confirm`` when ready.

Cluster size is the host count from the live sparkrun cluster (via detect.py),
not a guess — so the suggestion matches reality.
"""

import os
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "hscc-cluster" / "templates"


def pick_template(host_count, *, template_dir=_TEMPLATE_DIR):
    """Pick the best built-in template name for ``host_count`` hosts.

    Maps 1..4 hosts to basic-N-node; anything larger falls back to the biggest
    built-in. Returns the name only if the template file exists, else None.
    """
    n = max(1, int(host_count or 0))
    name = f"basic-{n}-node" if 1 <= n <= 4 else "basic-4-node"
    if (Path(template_dir) / f"{name}.yaml").is_file():
        return name
    return None


def suggest(cluster, *, template_dir=_TEMPLATE_DIR):
    """Return a suggestion dict from a detect.py cluster dict (or None).

    {"hosts": int, "template": str|None, "note": str}
    """
    hosts = list((cluster or {}).get("hosts") or [])
    name = pick_template(len(hosts), template_dir=template_dir)
    if name:
        note = (f"Suggested template: {name} ({len(hosts)} host(s)). "
                f"Apply with: hscc-cluster cluster-template apply {name} --confirm")
    else:
        note = "No matching built-in template found."
    return {"hosts": len(hosts), "template": name, "note": note}


if __name__ == "__main__":
    import json
    import sys

    boot_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(boot_dir))
    import detect

    result = suggest(detect.detect_cluster())
    print(json.dumps(result))
