"""
Thin CLI wrapper for cluster template operations.

Called from hscc.py with arguments like:
  cluster-template list
  cluster-template preview <name>
  cluster-template apply <name> --confirm
"""

import json
import sys
from pathlib import Path

# Ensure plugin dir is on path for imports
PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import list_templates, preview_template, apply_template


def cmd_cluster_template(args):
    """Handle hscc-cluster cluster-template <subcommand> [args]"""
    if len(args) < 1:
        return {
            "error": "Missing subcommand",
            "usage": "hscc-cluster cluster-template <list|preview|apply> [args]",
            "subcommands": {
                "list": "List available cluster templates",
                "preview <name>": "Preview what applying a template would change",
                "apply <name> [--confirm]": "Apply a cluster template (use --confirm to execute)",
            },
        }

    subcmd = args[0]

    if subcmd == "list":
        return list_templates()

    elif subcmd == "preview":
        if len(args) < 2:
            return {"error": "Missing template name", "usage": "cluster-template preview <name>"}
        try:
            return preview_template(args[1])
        except FileNotFoundError as e:
            return {"error": str(e)}

    elif subcmd == "apply":
        if len(args) < 2:
            return {"error": "Missing template name", "usage": "cluster-template apply <name> [--confirm]"}
        confirm = "--confirm" in args
        try:
            return apply_template(args[1], confirm=confirm)
        except FileNotFoundError as e:
            return {"error": str(e)}

    else:
        return {
            "error": f"Unknown subcommand: {subcmd}",
            "subcommands": ["list", "preview", "apply"],
        }


if __name__ == "__main__":
    result = cmd_cluster_template(sys.argv)
    print(json.dumps(result, indent=2, default=str))
