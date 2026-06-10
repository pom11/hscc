"""sparkrun-hermes: the official Hermes plugin for sparkrun.

Exposes a single guarded `sparkrun_exec` tool (CLI passthrough) so any Hermes
agent can launch/stop/inspect inference workloads on a DGX Spark cluster. The
bundled skills (run/setup/registry) teach the agent how to drive it.

Mirrors the official sparkrun OpenClaw + Claude Code plugins' design. Entry:
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


def register(ctx) -> None:
    ctx.register_tool(
        name="sparkrun_exec",
        toolset="sparkrun",
        schema=SPARKRUN_EXEC_SCHEMA,
        handler=_stringify(execlib.sparkrun_exec),
        emoji="⚡",
        description=_DESC,
    )
