"""hscc-commands: operator slash commands for cluster incident response.

Entry: register(ctx). Registers /orch-restart, /cluster, /cluster-restart as
in-session slash commands. Handlers run directly in the gateway (not through the
LLM), so they work even when the orchestrator model is wedged.

Confirm-first: a bare command shows a preview; re-run with the word ``confirm``
(or ``yes``/``y``) in the args to execute. Read-only /cluster never gates.
"""
try:
    from . import cmdlib
except ImportError:  # direct import (tests) without package parent
    import cmdlib


_CONFIRM_WORDS = {"confirm", "yes", "y", "true", "1"}


def _confirmed(raw_args):
    return any(w in _CONFIRM_WORDS for w in (raw_args or "").lower().split())


def _fmt_units(units, label):
    if not units:
        return f"  ({label}: none in serving.json)"
    return "\n".join(
        f"  • {u.get('id') or u.get('role')} @ {cmdlib.unit_node(u)} "
        f"[{u.get('model') or '?'}]"
        for u in units
    )


def cmd_cluster(raw_args):
    """Read-only cluster resource snapshot. Never mutates."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    lines = ["🖥️  *HSCC cluster*", ""]

    if orch:
        node = cmdlib.unit_node(orch)
        live = cmdlib._curl_model(node)
        state = f"✅ serving `{live}`" if live else "❌ DOWN / no /v1/models"
        lines.append(f"*Orchestrator* {node}: {state}")
        lines.append(f"  serving.json: `{orch.get('model')}`")
    else:
        lines.append("*Orchestrator*: ⚠️ none defined in serving.json")

    lines.append("")
    lines.append(f"*Workers* ({len(workers)}):")
    if not workers:
        lines.append("  (none)")
    for w in workers:
        node = cmdlib.unit_node(w)
        live = cmdlib._curl_model(node)
        mark = "✅" if live else "❌"
        lines.append(f"  {mark} {node}: {live or 'down'}")

    return "\n".join(lines)


def cmd_orch_restart(raw_args):
    """Restart the orchestrator vLLM on its node. Confirm-first."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    if not orch:
        return "⚠️ No orchestrator unit in serving.json — nothing to restart."
    node = cmdlib.unit_node(orch)
    if not _confirmed(raw_args):
        return (f"⚠️ *Confirm orchestrator restart*\n"
                f"  node: {node}\n"
                f"  model: `{orch.get('model')}`\n"
                f"  recipe: `{orch.get('recipe')}`\n\n"
                f"This stops + relaunches the orchestrator (~5–7 min downtime).\n"
                f"Run `/orch-restart confirm` to execute.")
    res = cmdlib.restart_one(orch)
    if res["ok"]:
        return (f"♻️ Orchestrator restart launched on {node} "
                f"(`{orch.get('model')}`). Model is loading — give it a few "
                f"minutes, then `/cluster` to verify.")
    return f"❌ Orchestrator restart FAILED on {node}: {res['error']}"


def cmd_cluster_restart(raw_args):
    """Restart orchestrator + all worker models. Confirm-first."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    targets = ([orch] if orch else []) + workers
    if not targets:
        return "⚠️ No units in serving.json — nothing to restart."

    if not _confirmed(raw_args):
        body = []
        if orch:
            body.append(f"  • orchestrator @ {cmdlib.unit_node(orch)} "
                        f"[{orch.get('model')}]")
        for w in workers:
            body.append(f"  • worker @ {cmdlib.unit_node(w)} [{w.get('model')}]")
        return ("⚠️ *Confirm FULL cluster restart*\n"
                + "\n".join(body)
                + f"\n\nRestarts {len(targets)} model(s) "
                  f"(orchestrator + {len(workers)} worker(s)). Whole cluster is "
                  f"down while they reload.\n"
                  f"Run `/cluster-restart confirm` to execute.")

    results = [cmdlib.restart_one(u) for u in targets]
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    lines = [f"♻️ Cluster restart: {len(ok)}/{len(results)} launched."]
    for r in ok:
        lines.append(f"  ✅ {r['unit']} @ {r['node']} (loading)")
    for r in bad:
        lines.append(f"  ❌ {r['unit']} @ {r.get('node','?')}: {r['error']}")
    lines.append("\nModels are loading — `/cluster` in a few minutes to verify.")
    return "\n".join(lines)


def register(ctx) -> None:
    ctx.register_command(
        name="cluster", handler=cmd_cluster,
        description="Show HSCC cluster status (orchestrator + workers, live health).",
    )
    ctx.register_command(
        name="orch-restart", handler=cmd_orch_restart,
        description="Restart the orchestrator vLLM (confirm-first).",
        args_hint="confirm",
    )
    ctx.register_command(
        name="cluster-restart", handler=cmd_cluster_restart,
        description="Restart ALL cluster models — orchestrator + workers (confirm-first).",
        args_hint="confirm",
    )
