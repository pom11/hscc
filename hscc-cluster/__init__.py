"""hscc-cluster toolset. Entry: register(ctx)."""
import json
import functools
from . import schemas as S
from . import ops
from . import debug
from . import heal
from . import discovery
from . import fleet
from . import template_tools
from . import orch_message
from . import autodown_tools as adt
from . import merge_queue_tools as mqt
from . import node_pause_tools as npt


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
    ("node_paused", npt.NODE_PAUSED_SCHEMA, npt.node_paused, "🅿️",
     "Which nodes are paused, with the units stopped, reason, since and "
     "until. Read-only: reads the pause overlay, never the fleet."),
    # Idle autodown (t_c036808c): the READ half of `hscc autodown`. The write
    # half stays CLI-only — arming or disarming fleet power is operator scope,
    # so there is no autodown_enable/disable/wake/cancel tool to add here.
    ("autodown_status", adt.AUTODOWN_STATUS_SCHEMA, adt.autodown_status, "💤",
     "Idle autodown state: armed or not, the idle window, state (up/down/"
     "waking) reconciled against the live serving layer, last activity, and "
     "which interlock (kanban work, PR/CI, watchdog block) is holding a "
     "teardown off. Read-only: same payload as `hscc autodown status --json`; "
     "it never arms, disarms, wakes or cancels anything."),
    # Local merge queue (t_96134d32): the READ half of `hscc merge-queue`. The
    # write half stays CLI-only — the queue force-pushes and squash-merges, so
    # an agent-writable merge door is a self-merge path (audit D8). There is no
    # merge_queue_add/remove/pause/resume/enable/disable tool to add here.
    ("merge_queue_status", mqt.MERGE_QUEUE_STATUS_SCHEMA,
     mqt.merge_queue_status, "🚦",
     "Per-project local merge-queue state: enabled (or why it is off — no "
     "repo / no GitHub remote / no gates), paused, queue depth and entries, "
     "waiting (non-green head), the in-flight PR and the last outcome. "
     "Read-only: same payload as `hscc merge-queue status --json`; it never "
     "enqueues, drops, pauses, enables or lands anything."),
]

_OPS_TOOLS = [
    ("provision_model", S.PROVISION_MODEL_SCHEMA, ops.provision_model, "🚀",
     "Provision a model on a node via sparkrun. GUARDED: confirm=true to execute."),
    ("stop_model", S.STOP_MODEL_SCHEMA, ops.stop_model, "🛑",
     "Stop a model on a node. GUARDED: confirm=true to execute."),
    ("model_health", S.MODEL_HEALTH_SCHEMA, ops.model_health, "❤️",
     "Check a node's vLLM endpoint health."),
    # Node pause (t_c28f4f14): the agent half of `hscc node pause|resume`.
    # These call the unified CLI's own verb functions — there is no second
    # implementation to fall out of step, and no path by which an agent can
    # mutate something the CLI would refuse (the #478 dry-run rule is the
    # CLI's, not a convention these entries restate).
    ("node_pause", npt.NODE_PAUSE_SCHEMA, npt.node_pause, "⏸️",
     "Park one node: stop its sparkrun units and record a durable pause the "
     "heal paths skip. GUARDED: confirm=true to execute; without confirm "
     "returns the plan (every unit + every node it would touch). Refuses a "
     "node serving the orchestrator model unless force=true, and a "
     "multi-node unit unless span=true."),
    ("node_resume", npt.NODE_RESUME_SCHEMA, npt.node_resume, "▶️",
     "Restart the units a node pause recorded (sparkrun --ensure, same recipe "
     "and flags), then clear the pause and that node's heal strikes. GUARDED: "
     "confirm=true to execute; without confirm returns the commands."),
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
     "Stop sparkrun containers not in serving.json. GUARDED: confirm=true, and "
     "REFUSED outright when serving.json is absent/unparseable/unit-less or does "
     "not name the orphan's node (stale copy = live workload; force=true to "
     "override the guard only)."),
]


_FLEET_TOOLS = [
    ("cluster_verify", fleet.CLUSTER_VERIFY_SCHEMA, fleet.cluster_verify, "🔍",
     "Full compatibility/health smoke-test (plugins, multiplex, streams, proxy, config)."),
    ("cluster_throughput", fleet.CLUSTER_THROUGHPUT_SCHEMA, fleet.cluster_throughput, "📊",
     "vLLM token throughput + per-node queue depth across the fleet."),
    ("fleet_stats", fleet.FLEET_STATS_SCHEMA, fleet.fleet_stats, "📈",
     "Fleet activity: task completions + tool usage over N days (default 7)."),
    ("autoscale_advice", fleet.AUTOSCALE_ADVICE_SCHEMA, fleet.autoscale_advice, "🧭",
     "Advisory scale up/down/none decision from current queue depth (does NOT scale)."),
]


_TEMPLATE_TOOLS = [
    ("list_templates", template_tools.LIST_TEMPLATES_SCHEMA, template_tools.list_templates, "📋",
     "List available cluster templates (declared fleet layouts)."),
    ("preview_template", template_tools.PREVIEW_TEMPLATE_SCHEMA, template_tools.preview_template, "🔎",
     "Dry-run: what applying a template would change against the live cluster (no writes)."),
    ("apply_template", template_tools.APPLY_TEMPLATE_SCHEMA, template_tools.apply_template, "⚠️",
     "Apply a cluster template — reshapes the ENTIRE fleet (provisions orchestrator + workers). HIGH-RISK. GUARDED: confirm=true to execute; without confirm returns a preview."),
]


#: Orchestrator-to-orchestrator messaging (t_a15f3047). Queue-only by design:
#: it never interrupts a running turn. Each end must be a `*-orch` profile or
#: the configured main orchestrator; `default` and worker profiles are refused.
#: orch_message.py owns the guard logic — this comment only names the toolset.
_ORCH_TOOLS = [
    ("orch_message", orch_message.ORCH_MESSAGE_SCHEMA, orch_message.orch_message, "✉️",
     "Nudge another orchestrator profile (queue-only, signed '[from <you>]', "
     "audited). Either end may be a *-orch profile or the configured main "
     "orchestrator; workers and 'default' are refused. Rate-limited per pair "
     "per hour. Not `hscc orch inject` (types into a "
     "live pane), not `/orch-restart` (restarts the orchestrator vLLM), not "
     "the `POST /v1/orchestrator/chat` route (one chat turn, by job id)."),
]


def register(ctx) -> None:
    for name, schema, handler, emoji, desc in (_READ_TOOLS + _OPS_TOOLS + _DEBUG_TOOLS
                                                 + _HEAL_TOOLS + _FLEET_TOOLS
                                                 + _TEMPLATE_TOOLS + _ORCH_TOOLS):
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
    # hermes 0.17: `kanban_task_blocked` — surface blocked tasks to ops.
    if hasattr(ctx, "register_hook"):
        try:
            from . import workflow
            ctx.register_hook("kanban_task_blocked", workflow.on_kanban_task_blocked)
        except Exception:
            pass
    # hermes 0.17: `kanban_task_completed` — record completions + auto-unblock.
    if hasattr(ctx, "register_hook"):
        try:
            from . import workflow
            ctx.register_hook("kanban_task_completed", workflow.on_kanban_task_completed)
        except Exception:
            pass
    # hermes 0.17: `pre_tool_call` — stamp profile_name into observability.
    if hasattr(ctx, "register_hook"):
        try:
            from . import workflow
            ctx.register_hook("pre_tool_call", workflow.on_pre_tool_call)
        except Exception:
            pass
    # t_6c4f38f2: worker budget watch — the [budget] note the worker SOUL
    # promises. post_api_request tracks per-session prompt_tokens + calls
    # since last commit (kanban workers only); transform_tool_result appends
    # exactly one [budget] line when a threshold crossed. Fail-open both.
    if hasattr(ctx, "register_hook"):
        try:
            from . import budget_watch
            ctx.register_hook("post_api_request", budget_watch.on_post_api_request)
            ctx.register_hook("transform_tool_result", budget_watch.on_transform_tool_result)
        except Exception:
            pass
