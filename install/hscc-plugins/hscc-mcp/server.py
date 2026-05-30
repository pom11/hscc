"""HSCC MCP server — registers HSCC operations as typed MCP tools over stdio.

Run by Hermes via config ``mcp_servers.hscc`` with the hermes venv python.
Tools are thin facades over the hscc-* CLI plugins (see tools.py / runner.py).

Tool function names are intentionally bare verbs (e.g. ``cluster_status``).
Hermes prefixes every MCP tool ``mcp_<server>_<tool>`` at registration, so the
callable names the model sees are ``mcp_hscc_cluster_status`` etc. Naming the
functions ``hscc_*`` here would double-stamp to ``mcp_hscc_hscc_*``.
"""
import importlib.util
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent  # .../hscc-mcp


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(
        f"hscc_mcp.{mod_name}", _PKG_DIR / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[f"hscc_mcp.{mod_name}"] = module
    return module


# Synthetic package so intra-package imports resolve despite the hyphen dir name.
if "hscc_mcp" not in sys.modules:
    import types
    _pkg = types.ModuleType("hscc_mcp")
    _pkg.__path__ = [str(_PKG_DIR)]
    sys.modules["hscc_mcp"] = _pkg

_load("runner", "runner.py")
tools = _load("tools", "tools.py")

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hscc")


@mcp.tool()
def cluster_status() -> object:
    """DGX Spark cluster status: running workloads and idle hosts."""
    return tools.cluster_status()


@mcp.tool()
def fleet_activity() -> object:
    """Per-agent live fleet activity (agent -> task -> kanban -> node)."""
    return tools.fleet_activity()


@mcp.tool()
def projects_show() -> object:
    """Active project roadmaps and tasks."""
    return tools.projects_show()


@mcp.tool()
def task_status(task_id: str) -> object:
    """Status of a dispatched task by id."""
    return tools.task_status(task_id)


@mcp.tool()
def project_create(name: str, description: str = "") -> object:
    """Create an HSCC project (auto-provisions a git repo + kanban board)."""
    return tools.project_create(name, description)


@mcp.tool()
def task_add(roadmap: str, subproject: str, title: str, description: str = "") -> object:
    """Add a task under a roadmap/subproject."""
    return tools.task_add(roadmap, subproject, title, description)


@mcp.tool()
def dispatch_task(task_id: str) -> object:
    """Pre-create a git worktree + a BLOCKED kanban card. Nothing runs until release."""
    return tools.dispatch_task(task_id)


@mcp.tool()
def release_task(task_id: str, confirm: bool = False) -> object:
    """Unblock a dispatched task so a worker runs it on its node. Ask the user
    first, then call with confirm=true."""
    return tools.release_task(task_id, confirm)


@mcp.tool()
def cancel_task(task_id: str, confirm: bool = False) -> object:
    """Cancel a live task/worker. Ask the user first, then confirm=true."""
    return tools.cancel_task(task_id, confirm)


@mcp.tool()
def green_check(task_id: str) -> object:
    """Read-only readiness check before merging a task's worktree."""
    return tools.green_check(task_id)


@mcp.tool()
def merge_worktree(task_id: str, confirm: bool = False) -> object:
    """Merge a task's worktree branch into the project default branch. Ask the
    user first, then confirm=true."""
    return tools.merge_worktree(task_id, confirm)


@mcp.tool()
def remove_worktree(task_id: str, confirm: bool = False) -> object:
    """Remove a task's worktree. Ask the user first, then confirm=true."""
    return tools.remove_worktree(task_id, confirm)


if __name__ == "__main__":
    mcp.run()
