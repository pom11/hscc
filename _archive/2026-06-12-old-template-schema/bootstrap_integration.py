"""
Bootstrap integration: auto-apply cluster template on first HSCC install.

Detects cluster size from ~/.hscc/cluster.json and applies the matching
default template.
"""

from __future__ import annotations

import json
from pathlib import Path

HSCC_DIR = Path.home() / ".hscc"
SERVING_JSON = HSCC_DIR / "serving.json"
CLUSTER_JSON = HSCC_DIR / "cluster.json"


def should_apply_template() -> bool:
    """Check if template should be auto-applied on bootstrap.
    
    Returns True if:
    - serving.json doesn't exist yet (first install)
    - serving.json has fewer than 2 units (minimal/default config)
    """
    if not SERVING_JSON.exists():
        return True
    
    try:
        with open(SERVING_JSON) as f:
            data = json.load(f)
        units = data.get("units", [])
        return len(units) < 2
    except (json.JSONDecodeError, IOError):
        return True


def bootstrap_default_template() -> str:
    """Detect cluster size and return the default template name.
    
    Reads ~/.hscc/cluster.json to determine number of workers,
    then returns the matching basic-N-node template.
    
    Returns:
        Template name string (e.g., "basic-4-node")
    """
    if not CLUSTER_JSON.exists():
        return "basic-1-node"
    
    try:
        with open(CLUSTER_JSON) as f:
            data = json.load(f)
        workers = len(data.get("workers", []))
        size = 1 + workers
        
        if size == 1:
            return "basic-1-node"
        elif size == 2:
            return "basic-2-node"
        elif size == 3:
            return "basic-3-node"
        else:
            return "basic-4-node"
    except (json.JSONDecodeError, IOError, KeyError):
        return "basic-1-node"


if __name__ == "__main__":
    print(f"Should apply: {should_apply_template()}")
    print(f"Default template: {bootstrap_default_template()}")
