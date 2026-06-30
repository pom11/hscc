"""hscc-cluster toolset. Entry: register(ctx)."""
import json
import functools
from . import schemas as S
from . import ops
from . import debug
from . import heal
from . import discovery


def _stringify(handler):
    """Coerce dict/list handler returns to a JSON string.

    The OpenAI/vLLM wire format requires ``role:"tool"`` message content to be a
    string; a raw dict makes pydantic iterate keys and reject the request
    (HTTP 400 dict_type). Every other Hermes tool returns a string already.
    """
    @functools.wraps(handler)
    def wrapper(args, **kwargs):
        result = handler(args, **kwargs)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)
    return wrapper

_READ_TOOLS = [
    ("cluster_status", S.CLUSTER_STATUS_SCHEMA, ops.cluster_status, "🖥️",
     "Live cluster status (running models, idle nodes, serving units)."),
    ("list_recipes", S.LIST_RECIPES_SCHEMA, ops.list_recipes, "📜",
     "List available sparkrun recipes."),
    ("pick_node", S.PICK_NODE_SCHEMA, ops.pick_node, "🎯",
     "Pick an idle worker node for provisioning."),
    ("discovery_status", S.DISCOVERY_STATUS_SCHEMA, discovery.discovery_status, "🛰️",
     "Live cluster topology map (orchestrator/workers/NAS, source); probe=true for VRAM/power/health."),
    ("nas_status", S.NAS_STATUS_SCHEMA, discovery.nas_status, "🗄️",
     "NAS health: discovery's NAS node + a single mount probe (no fan-out)."),
]

_OPS_TOOLS = [
    ("provision_model", S.PROVISION_MODEL_SCHEMA, ops.provision_model, "🚀",
     "Provision a model on a node via sparkrun. GUARDED: confirm=true to execute."),
    ("stop_model", S.STOP_MODEL_SCHEMA, ops.stop_model, "🛑",
     "Stop a model on a node. GUARDED: confirm=true to execute."),
    ("model_health", S.MODEL_HEALTH_SCHEMA, ops.model_health, "❤️",
     "Check a node's vLLM endpoint health."),
]

_DEBUG_TOOLS = [
    ("vllm_logs", S.VLLM_LOGS_SCHEMA, debug.vllm_logs, "📄",
     "Pull vLLM serve log + docker logs from a node's sparkrun container."),
    ("node_diagnostics", S.NODE_DIAGNOSTICS_SCHEMA, debug.node_diagnostics, "🩺",
     "Node health: dmesg/OOM, fd count, disk, docker, GPU temp/util/ECC."),
    ("nas_diagnose", S.NAS_DIAGNOSE_SCHEMA, debug.nas_diagnose, "🗄️",
     "Probe NAS mount (healthy/stale/unreachable) + QNAP exports."),
]

_HEAL_TOOLS = [
    ("restart_model", S.RESTART_MODEL_SCHEMA, heal.restart_model, "♻️",
     "Restart a model on a node (stop+run). GUARDED: confirm=true to execute."),
    ("remount_nas", S.REMOUNT_NAS_SCHEMA, heal.remount_nas, "🔌",
     "Lazy-umount + remount /mnt/nas on a node. GUARDED: confirm=true."),
    ("repair_nas_export", S.REPAIR_NAS_EXPORT_SCHEMA, heal.repair_nas_export, "🛠️",
     "Re-publish QNAP NFS exports (backup first). HIGH-RISK. GUARDED: confirm=true."),
    ("reap_orphans", S.REAP_ORPHANS_SCHEMA, heal.reap_orphans, "🧹",
     "Stop sparkrun containers not in serving.json. GUARDED: confirm=true."),
]


def register(ctx) -> None:
    for name, schema, handler, emoji, desc in _READ_TOOLS + _OPS_TOOLS + _DEBUG_TOOLS + _HEAL_TOOLS:
        ctx.register_tool(name=name, toolset="hscc-cluster", schema=schema,
                          handler=_stringify(handler), emoji=emoji, description=desc)
    # WS4: on kanban re-dispatch, post an idempotent-resume note so the worker
    # continues from prior branch state instead of redoing finished work.
    if hasattr(ctx, "register_hook"):
        try:
            from . import workflow
            ctx.register_hook("kanban_task_claimed", workflow.on_kanban_task_claimed)
        except Exception:
            pass
