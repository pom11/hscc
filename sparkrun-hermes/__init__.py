"""sparkrun-hermes: the official Hermes plugin for sparkrun.

Exposes a single guarded `sparkrun_exec` tool (CLI passthrough) so any Hermes
agent can launch/stop/inspect inference workloads on a DGX Spark cluster. The
bundled skills (run/setup/registry) teach the agent how to drive it.

Mirrors the official sparkrun OpenClaw plugin's design. Entry:
register(ctx).
"""
import functools
import json

try:
    from . import execlib
except ImportError:  # direct import (tests) without package parent
    import execlib


def _stringify(handler):
    """Coerce dict/list returns to a JSON string.

    vLLM/OpenAI wire format requires role:"tool" content to be a string; a raw
    dict makes pydantic reject the request (HTTP 400 dict_type). Same guard the
    hscc-cluster toolset uses.
    """
    @functools.wraps(handler)
    def wrapper(args, **kwargs):
        result = handler(args, **kwargs)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)
    return wrapper


SPARKRUN_EXEC_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Full sparkrun CLI command, e.g. 'sparkrun list', "
                           "'sparkrun status', 'sparkrun run qwen3-1.7b-vllm "
                           "--tp 1 --no-follow', 'sparkrun cluster list --json'.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default 120, max 1800).",
            "default": 120,
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

_DESC = (
    "Execute a sparkrun CLI command on the DGX Spark cluster: launch/stop "
    "inference workloads, check status, browse/search recipes, benchmark, "
    "tune, manage clusters + the proxy. The command MUST start with "
    "'sparkrun'. Load the run/setup/registry skills for usage guidance."
)


def _cluster_surface_allowed(ctx) -> bool:
    """May THIS home register ``sparkrun_exec``? (D-B gate, card t_e98c6ad4)

    ``sparkrun_exec`` is an unguarded-by-design CLI passthrough into the fleet,
    so its reach is cluster authority: the main-only ruling puts it on
    ``<main>`` and the operator/gateway root home only (the SOUL already
    described it as "orchestrator-only", and the sibling audit left its reach as
    LEFT-2 because nothing enforced the claim). The decision comes from
    ``hscc-roles/main_orch.plugin_surface_allowed`` — never a name compared
    here — reached by the same sibling-path loader as
    ``hscc-cluster/orch_message.py`` / ``hscc-commands``. Fail-open when the
    resolver cannot be reached or cannot answer: no resolver is no gate.
    """
    import os
    import sys
    roles = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "hscc-roles")
    added = None
    try:
        import main_orch  # noqa: F401  (already importable)
    except ImportError:
        if os.path.isdir(roles) and roles not in sys.path:
            sys.path.insert(0, roles)
            added = 0
        try:
            import main_orch  # noqa: F401
        except ImportError:
            return True                     # no resolver on the path -> no gate
        finally:
            if added is not None and sys.path[:1] == [roles]:
                sys.path.pop(0)
    try:
        return bool(main_orch.plugin_surface_allowed(ctx))
    except Exception:  # noqa: BLE001 — an unusable resolver must not strip the tool
        return True


def register(ctx) -> None:
    # t_e98c6ad4 (audit §6 D-B): the cluster surface is <main> + the operator
    # root home. This plugin registers no hooks, so a denied home registers
    # nothing at all.
    if not _cluster_surface_allowed(ctx):
        return
    ctx.register_tool(
        name="sparkrun_exec",
        toolset="sparkrun",
        schema=SPARKRUN_EXEC_SCHEMA,
        handler=_stringify(execlib.sparkrun_exec),
        emoji="⚡",
        description=_DESC,
    )
