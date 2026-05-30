"""HSCC tool functions — typed facades over the CLI plugins.

Each returns either parsed JSON (when the plugin emits JSON) or raw stdout text.
Risky tools require ``confirm=True``.
"""
from hscc_mcp.runner import run_hscc

COORD = "hscc-agent-coordinator"
PROJECTS = "hscc-projects"
CLUSTER = "hscc-cluster"


def _result(res: dict):
    """Prefer parsed JSON; fall back to raw stdout; surface errors."""
    if res.get("error"):
        return {"error": res["error"]}
    if res.get("json") is not None:
        return res["json"]
    return res.get("stdout", "")


def cluster_status():
    return _result(run_hscc(CLUSTER, "cluster-status"))


def fleet_activity():
    return _result(run_hscc(COORD, "fleet-activity", "--json"))


def projects_show():
    return _result(run_hscc(PROJECTS, "show"))


def task_status(task_id: str):
    return _result(run_hscc(COORD, "task-status", task_id))


def project_create(name: str, description: str = ""):
    return _result(run_hscc(PROJECTS, "create", name, description))


def task_add(roadmap: str, subproject: str, title: str, description: str = ""):
    return _result(run_hscc(PROJECTS, "add-task", roadmap, subproject, title, description))


def dispatch_task(task_id: str):
    """Pre-create the worktree + a BLOCKED kanban card. Nothing runs until release."""
    return _result(run_hscc(COORD, "dispatch-task", task_id))


def _require_confirm(action: str, confirm: bool):
    if not confirm:
        return {
            "needs_confirmation": True,
            "error": (
                f"Refused: '{action}' is a live fleet operation. Ask the user to "
                f"approve, then call again with confirm=true."
            ),
        }
    return None


def release_task(task_id: str, confirm: bool = False):
    """Unblock a dispatched task → gateway spawns a live worker. GATED."""
    gate = _require_confirm("release_task", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "release-task", task_id))


def cancel_task(task_id: str, confirm: bool = False):
    """Cancel a live task/worker. GATED."""
    gate = _require_confirm("cancel_task", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "cancel-task", task_id))


def merge_worktree(task_id: str, confirm: bool = False):
    """Merge a task's worktree branch into the project default branch. GATED."""
    gate = _require_confirm("merge_worktree", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "merge-worktree", task_id))


def remove_worktree(task_id: str, confirm: bool = False):
    """Remove a task's worktree. GATED."""
    gate = _require_confirm("remove_worktree", confirm)
    if gate:
        return gate
    return _result(run_hscc(COORD, "remove-worktree", task_id))


def green_check(task_id: str):
    """Read-only readiness check before merge. Not gated."""
    return _result(run_hscc(COORD, "green-check", task_id))
