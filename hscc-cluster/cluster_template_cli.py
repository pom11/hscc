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

import _theme  # guarded themed render helper (peer cli_theme + neutral fallback)

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


def _template_lines(result) -> list:
    """Build the themed human-view lines for a template result dict.

    ``escape`` is applied to every value-derived line so literal Rich markup in
    template content / file paths renders literally; the generic fallback flattens
    any other shape.
    """
    esc = _theme.escape
    lines = []

    # list
    if isinstance(result, dict) and "templates" in result:
        lines.append(f"[label]templates[/label]  {result.get('count', len(result['templates']))}")
        for t in result["templates"]:
            if isinstance(t, dict):
                ver = t.get("version", "")
                desc = t.get("description") or ""
                lines.append(f"  {esc(str(t.get('name', '?')))}"
                             + (f"  v{esc(str(ver))}" if ver not in (None, "") else "")
                             + (f"  {esc(str(desc))}" if desc else ""))
            else:
                lines.append(f"  - {esc(str(t))}")
        return lines

    # status
    if isinstance(result, dict) and "applied" in result:
        lines.append(f"[label]applied[/label]  {esc(str(result.get('applied')) or '(none)')}")
        if result.get("note"):
            lines.append(f"  {esc(str(result['note']))}")
        return lines

    # generic flatten for preview / validate / apply / anything else
    def _flatten(value, depth=0):
        pad = "  " * depth
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, (dict, list)) and v:
                    lines.append(f"{pad}{esc(str(k))}:")
                    _flatten(v, depth + 1)
                elif isinstance(v, list) and not v:
                    lines.append(f"{pad}{esc(str(k))}: (none)")
                else:
                    lines.append(f"{pad}{esc(str(k))}: {esc(str(v))}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    _flatten(item, depth + 1)
                else:
                    lines.append(f"{pad}- {esc(str(item))}")
        else:
            lines.append(f"{pad}{esc(str(value))}")
    _flatten(result)
    return lines


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    result = cmd_cluster_template(args)
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        console = _theme.make_console()
        title = (args[0] if args else "cluster-template")
        err = result.get("error") if isinstance(result, dict) else None
        if err is not None:
            usage = result.get("usage") if isinstance(result, dict) else None
            body = _theme.escape(str(err))
            if usage:
                body = f"{body}\n\nusage: {_theme.escape(str(usage))}"
            console.print(_theme.status_panel(body, status="error", title=title))
        else:
            console.print(_theme.panel(title, "\n".join(_template_lines(result))))
