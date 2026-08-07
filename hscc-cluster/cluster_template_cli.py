"""
Thin CLI wrapper for cluster template operations.

Called from hscc.py with arguments like:
  cluster-template list
  cluster-template preview <name>
  cluster-template validate <name> [--structural-only] [--json]
  cluster-template apply <name> --confirm
"""

import json
import sys
from pathlib import Path

# Ensure plugin dir is on path for imports
PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))

from cluster_template import (
    list_templates, preview_template, apply_template,
    validate_template, applied_status,
)


def cmd_cluster_template(args):
    """Handle hscc-cluster cluster-template <subcommand> [args]"""
    if len(args) < 1:
        return {
            "error": "Missing subcommand",
            "usage": "hscc-cluster cluster-template <list|status|validate|preview|apply> [args]",
            "subcommands": {
                "list": "List available cluster templates",
                "status": "Show which template is currently applied",
                "validate <name>": "Preflight-check a template (is it deployable?)",
                "preview <name>": "Preview what applying a template would change",
                "apply <name> [--confirm] [--force-recreate]": "Apply a cluster template (use --confirm to execute; --force-recreate to stop+rerun units so changed serve flags reach vLLM)",
            },
        }

    subcmd = args[0]

    if subcmd == "list":
        return list_templates()

    elif subcmd == "status":
        return applied_status()

    elif subcmd == "validate":
        if len(args) < 2:
            return {"error": "Missing template name",
                    "usage": "cluster-template validate <name> [--structural-only] [--json]"}

        def _first_token(toks, *candidates):
            for c in candidates:
                if c in toks:
                    return True
            return False

        structural_only = _first_token(args, "--structural-only")
        # `--json` is accepted for interface compatibility; the validate result
        # is already returned as the spec's machine-readable JSON shape, which
        # the caller (hscc _emit) prints as JSON. It is deliberately not treated
        # as the template name.
        # Name is the first arg after 'validate' that is not a flag.
        name = next((a for a in args[1:] if not a.startswith("--")),
                    args[1] if len(args) > 1 else None)
        if not name:
            return {"error": "Missing template name",
                    "usage": "cluster-template validate <name> [--structural-only] [--json]"}
        return validate_template(name, structural_only=structural_only)

    elif subcmd == "preview":
        if len(args) < 2:
            return {"error": "Missing template name", "usage": "cluster-template preview <name>"}
        try:
            return preview_template(args[1])
        except FileNotFoundError as e:
            return {"error": str(e)}

    elif subcmd == "apply":
        if len(args) < 2:
            return {"error": "Missing template name",
                    "usage": "cluster-template apply <name> [--confirm] [--force-recreate]"}
        confirm = "--confirm" in args
        recreate = ("--force-recreate" in args) or ("--recreate-on-change" in args)
        try:
            return apply_template(args[1], confirm=confirm, recreate=recreate)
        except FileNotFoundError as e:
            return {"error": str(e)}

    else:
        return {
            "error": f"Unknown subcommand: {subcmd}",
            "subcommands": ["list", "status", "validate", "preview", "apply"],
        }


if __name__ == "__main__":
    result = cmd_cluster_template(sys.argv[1:])
    print(json.dumps(result, indent=2, default=str))
