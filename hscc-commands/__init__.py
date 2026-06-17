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


def _fmt_metrics(m):
    """Format a metrics dict into a compact per-node block."""
    parts = []
    cpu_temp = m.get("cpu_temp_c", "")
    cpu_load = m.get("cpu_load_1m", "")
    cpu_pct = m.get("cpu_usage_pct", "")
    mem_used = m.get("mem_used_pct", "")
    mem_avail = m.get("mem_available_mb", "")
    gpu = m.get("gpu_name", "")
    gpu_util = m.get("gpu_util_pct", "")
    gpu_temp = m.get("gpu_temp_c", "")
    gpu_power = m.get("gpu_power_w", "")

    parts.append(f"  CPU: {cpu_pct}% | temp {cpu_temp}°C | load {cpu_load}")
    parts.append(f"  MEM: {mem_used}% used ({mem_avail} MB free)")
    if gpu:
        parts.append(f"  GPU {gpu}: {gpu_util}% util | {gpu_temp}°C | {gpu_power}W")
    return "\n".join(parts)


def cmd_cluster(raw_args):
    """Read-only cluster resource snapshot. Never mutates."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    metrics = cmdlib.cluster_metrics()
    lines = ["🖥️  *HSCC cluster*", ""]

    if orch:
        node = cmdlib.unit_node(orch)
        live = cmdlib._curl_model(node)
        state = f"✅ serving `{live}`" if live else "❌ DOWN / no /v1/models"
        lines.append(f"*Orchestrator* {node}: {state}")
        lines.append(f"  serving.json: `{orch.get('model')}`")
        m = metrics.get(node, {})
        if m:
            lines.append(_fmt_metrics(m))
        lines.append("")

    lines.append(f"*Workers* ({len(workers)}):")
    if not workers:
        lines.append("  (none)")
    for w in workers:
        node = cmdlib.unit_node(w)
        live = cmdlib._curl_model(node)
        mark = "✅" if live else "❌"
        lines.append(f"  {mark} {node}: {live or 'down'}")
        m = metrics.get(node, {})
        if m:
            lines.append("  " + _fmt_metrics(m).replace("\n", "\n  "))

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
    """Recover the cluster by RE-APPLYING the active template (D14). The template
    is the recovery contract: make reality match the declared state. Falls back
    to restarting serving.json units when no template is recorded. Confirm-first."""
    state = cmdlib.applied_template()
    name = (state or {}).get("template")

    # Template-driven recovery (preferred)
    if name:
        if not _confirmed(raw_args):
            return ("⚠️ *Confirm cluster recovery (re-apply template)*\n"
                    f"  template: `{name}`\n\n"
                    "Re-applies the active template — provisions every declared "
                    "unit to match the template (orchestrator + workers). Models "
                    "reload.\nRun `/cluster-restart confirm` to execute.")
        res = cmdlib.reapply_template(confirm=True)
        if res["ok"]:
            return (f"♻️ Re-applied template `{name}`. Cluster is converging to "
                    f"the declared state — `/cluster` in a few minutes to verify.")
        return (f"❌ Template re-apply FAILED (`{name}`): {res.get('error') or res.get('result')}\n"
                f"Falling back: try `/cluster-restart confirm` again or restart units manually.")

    # Fallback: no template recorded → restart serving.json units
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    targets = ([orch] if orch else []) + workers
    if not targets:
        return "⚠️ No applied template and no units in serving.json — nothing to restart."
    if not _confirmed(raw_args):
        body = []
        if orch:
            body.append(f"  • orchestrator @ {cmdlib.unit_node(orch)} [{orch.get('model')}]")
        for w in workers:
            body.append(f"  • worker @ {cmdlib.unit_node(w)} [{w.get('model')}]")
        return ("⚠️ *Confirm FULL cluster restart* (no template recorded — unit restart)\n"
                + "\n".join(body)
                + f"\n\nRestarts {len(targets)} model(s). Whole cluster down while "
                  f"they reload.\nRun `/cluster-restart confirm` to execute.")
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


def cmd_status(raw_args):
    """Rich one-glance dashboard: topology + free-VRAM + proxy + daemon + template."""
    snap = cmdlib.discovery_snapshot(probe=True)
    lines = ["📊 *HSCC status*", ""]
    if snap.get("ok"):
        lines.append(f"*Topology* (source: {snap.get('source')})")
        o = snap.get("orchestrator") or {}
        lines.append(f"  orchestrator {o.get('ip')} {o.get('name') or ''}".rstrip())
        for w in snap.get("workers") or []:
            vram = w.get("vram_free_gb")
            pw = w.get("power_draw_w")
            idle = w.get("idle")
            extra = []
            if vram is not None:
                extra.append(f"{vram}GB free")
            if pw is not None:
                extra.append(f"{pw}W{' idle' if idle else ''}")
            hp = "✅" if w.get("vllm_healthy") else "❌"
            lines.append(f"  {hp} worker {w.get('ip')}" + (f" — {', '.join(extra)}" if extra else ""))
        if snap.get("nas"):
            lines.append(f"  NAS {snap['nas'].get('ip')}")
    else:
        lines.append(f"*Topology*: ⚠️ discovery unavailable ({snap.get('error', 'n/a')})")
    lines.append("")
    lines.append(f"*Proxy* :{cmdlib.PROXY_PORT}: " + ("✅ up" if cmdlib.proxy_health() else "❌ down"))
    tpl = cmdlib.applied_template()
    lines.append(f"*Applied template*: {tpl.get('template') if tpl else '(none)'}")
    lines.append(f"*Autonomy*: {cmdlib.autonomy_flag() or 'off'}")
    return "\n".join(lines)


def cmd_heal(raw_args):
    """Manual healing pass: report unhealthy workers; confirm-first restart.
    Orchestrator wedge → advise /cluster-restart (template recovery)."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    unhealthy = [w for w in workers if not cmdlib._curl_model(cmdlib.unit_node(w))]
    orch_down = orch and not cmdlib._curl_model(cmdlib.unit_node(orch))

    lines = ["🩺 *HSCC heal*", ""]
    if orch_down:
        lines.append(f"⚠️ Orchestrator @ {cmdlib.unit_node(orch)} looks DOWN/wedged.")
        lines.append("  → run `/cluster-restart confirm` (re-applies the template).")
    if not unhealthy:
        lines.append("Workers: ✅ all healthy." if workers else "No worker units.")
        return "\n".join(lines)
    lines.append(f"Unhealthy workers ({len(unhealthy)}):")
    for w in unhealthy:
        lines.append(f"  ❌ {cmdlib.unit_node(w)} [{w.get('model')}]")
    if not _confirmed(raw_args):
        lines.append("\nRun `/heal confirm` to restart the unhealthy worker(s).")
        return "\n".join(lines)
    results = [cmdlib.restart_one(w) for w in unhealthy]
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        lines.append(f"  {mark} {r['unit']} @ {r.get('node','?')}"
                     + ("" if r["ok"] else f": {r['error']}"))
    lines.append("\nRestarted unhealthy workers — `/cluster` to verify.")
    return "\n".join(lines)


def cmd_cluster_down(raw_args):
    """Stop all vLLM units across the cluster — leaves hosts up. Confirm-first.

    Uses sparkrun stop per node (parallel). Pairs with /cluster-docker-prune +
    /cluster-restart for a clean recycle without a full reboot."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    nodes = []
    if orch:
        n = cmdlib.unit_node(orch)
        if n:
            nodes.append(("orchestrator", n))
    for w in workers:
        n = cmdlib.unit_node(w)
        if n:
            nodes.append(("worker", n))

    if not nodes:
        return "⚠️ No nodes in serving.json — nothing to stop."

    if not _confirmed(raw_args):
        lines = ["⚠️ *Confirm cluster down*", ""]
        for role, n in nodes:
            lines.append(f"  • {role} {n}")
        lines.append("")
        lines.append("Stops all vLLM units (sparkrun stop --all) on each node, "
                     "parallel. Hosts stay up. Workflow: /cluster-down → "
                     "/cluster-docker-prune → /cluster-restart.")
        lines.append("Run `/cluster-down confirm` to execute.")
        return "\n".join(lines)

    # Parallel sparkrun stop per node
    from concurrent.futures import ThreadPoolExecutor
    lines = ["🛑 *HSCC cluster down*", ""]
    node_list = [n for _r, n in nodes]

    def _stop(node):
        return node, cmdlib._run(
            [cmdlib.SPARKRUN, "stop", "--all", "--hosts", node], timeout=90,
        )

    with ThreadPoolExecutor(max_workers=max(1, len(node_list))) as pool:
        results = list(pool.map(_stop, node_list))

    for node, (ok, _o, err) in results:
        mark = "✅" if ok else "❌"
        tail = "" if ok else f" — {err[:140]}"
        lines.append(f"  {mark} {node}: stopped{tail}")
    lines.append("")
    lines.append("All units stopped. Next: `/cluster-docker-prune confirm` then "
                 "`/cluster-restart confirm`.")
    return "\n".join(lines)


def cmd_cluster_docker_prune(raw_args):
    """Run `docker system prune -af` across all cluster nodes (parallel).
    Reclaims images/containers/networks. Volumes preserved (model cache safe).
    Confirm-first."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    nodes = []
    if orch:
        n = cmdlib.unit_node(orch)
        if n:
            nodes.append(("orchestrator", n))
    for w in workers:
        n = cmdlib.unit_node(w)
        if n:
            nodes.append(("worker", n))

    if not nodes:
        return "⚠️ No nodes in serving.json — nothing to prune."

    if not _confirmed(raw_args):
        lines = ["⚠️ *Confirm cluster docker prune*", ""]
        for role, n in nodes:
            lines.append(f"  • {role} {n}")
        lines.append("")
        lines.append("Runs `docker system prune -af` on each node (parallel). "
                     "Removes dangling images, stopped containers, unused networks. "
                     "Volumes preserved (model cache safe).")
        lines.append("Best run AFTER `/cluster-down confirm`.")
        lines.append("Run `/cluster-docker-prune confirm` to execute.")
        return "\n".join(lines)

    node_list = [n for _r, n in nodes]
    results = cmdlib.ssh_exec_parallel(
        node_list, "docker system prune -af 2>&1 | tail -3", timeout=120,
    )

    lines = ["🧹 *HSCC docker prune*", ""]
    for node in node_list:
        ok, out, err = results.get(node, (False, "", "no result"))
        mark = "✅" if ok else "❌"
        # Pull the "Total reclaimed space" line if present
        space_line = ""
        for ln in (out or "").splitlines():
            if "reclaimed" in ln.lower():
                space_line = " — " + ln.strip()
                break
        tail = space_line if ok else f" — {(err or out)[:140]}"
        lines.append(f"  {mark} {node}{tail}")
    lines.append("")
    lines.append("Done. Next: `/cluster-restart confirm` to re-apply the template.")
    return "\n".join(lines)


def cmd_cluster_reboot(raw_args):
    """Reboot all cluster nodes. Workers in parallel; orchestrator last.

    Confirm-first. Orchestrator reboot kills the gateway mid-command, so the
    final completion message comes from the workers; user runs `/cluster` after
    ~2-3 min to verify everything came back. Skips docker prune (reboot recycles
    all containers anyway)."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    worker_nodes = [cmdlib.unit_node(w) for w in workers if cmdlib.unit_node(w)]
    orch_node = cmdlib.unit_node(orch) if orch else None
    if not orch_node and not worker_nodes:
        return "⚠️ No nodes in serving.json — nothing to reboot."

    if not _confirmed(raw_args):
        lines = ["⚠️ *Confirm full cluster reboot*", ""]
        if worker_nodes:
            lines.append(f"  workers (parallel): {', '.join(worker_nodes)}")
        if orch_node:
            lines.append(f"  orchestrator (last): {orch_node}")
        lines.append("")
        lines.append("Workers reboot in parallel (~3 min). Orchestrator reboots last "
                     "and kills the gateway — no completion message after that.")
        lines.append("Run `/cluster-reboot confirm` to execute.")
        return "\n".join(lines)

    lines = ["♻️ *HSCC cluster reboot launched*", ""]

    # Phase 1: parallel worker reboot
    if worker_nodes:
        results = cmdlib.ssh_exec_parallel(
            worker_nodes, "sudo nohup shutdown -r now >/dev/null 2>&1 &", timeout=30,
        )
        for node, (ok, _o, err) in results.items():
            mark = "✅" if ok else "❌"
            tail = "" if ok else f" — {err[:120]}"
            lines.append(f"  {mark} worker {node}: reboot triggered{tail}")
        lines.append("")
        lines.append("Workers rebooting (~2-3 min). Run `/cluster` after to verify.")
    else:
        lines.append("(no workers in serving.json)")

    # Phase 2: orchestrator reboot last
    if orch_node:
        # Delay 5s so this message lands before gateway dies
        lines.append("")
        lines.append(f"  ⏳ orchestrator {orch_node}: reboot in 5s (kills this gateway)")
        cmdlib.ssh_exec(
            orch_node,
            "sudo nohup bash -c 'sleep 5 && shutdown -r now' >/dev/null 2>&1 &",
            timeout=15,
        )
        lines.append("")
        lines.append("Run `/cluster` in ~3 min to verify all 4 units are back.")
    return "\n".join(lines)


def cmd_cluster_apt_upgrade(raw_args):
    """Run apt-get update + upgrade across all cluster nodes. Confirm-first.
    Auto-chains to /cluster-reboot if /var/run/reboot-required appears on any
    node (kernel/critical lib update). Sequential per node so each apt
    finishes cleanly; parallel risks dpkg lock contention."""
    units = cmdlib.read_units()
    orch = cmdlib.orchestrator_unit(units)
    workers = cmdlib.worker_units(units)
    nodes = []
    if orch:
        n = cmdlib.unit_node(orch)
        if n:
            nodes.append(("orchestrator", n))
    for w in workers:
        n = cmdlib.unit_node(w)
        if n:
            nodes.append(("worker", n))

    if not nodes:
        return "⚠️ No nodes in serving.json — nothing to upgrade."

    if not _confirmed(raw_args):
        lines = ["⚠️ *Confirm cluster apt-upgrade*", ""]
        for role, n in nodes:
            lines.append(f"  • {role} {n}")
        lines.append("")
        lines.append("Runs `sudo apt-get update && sudo apt-get -y upgrade` on each "
                     "node (sequential — apt lock-safe). If `/var/run/reboot-required` "
                     "appears on any node afterwards, will chain into /cluster-reboot "
                     "automatically.")
        lines.append("Run `/cluster-apt-upgrade confirm` to execute.")
        return "\n".join(lines)

    lines = ["📦 *HSCC apt-upgrade*", ""]
    reboot_needed = []
    cmd = ("sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
           "sudo DEBIAN_FRONTEND=noninteractive apt-get -y -qq upgrade && "
           f"test -f {cmdlib.REBOOT_REQUIRED_FILE} && echo REBOOT_NEEDED || true")
    for role, n in nodes:
        ok, out, err = cmdlib.ssh_exec(n, cmd, timeout=600)
        mark = "✅" if ok else "❌"
        rb_marker = "REBOOT_NEEDED" in (out or "")
        line = f"  {mark} {role} {n}"
        if rb_marker:
            line += " — *reboot required*"
            reboot_needed.append(n)
        if not ok:
            line += f" — {(err or out)[:140]}"
        lines.append(line)

    if reboot_needed:
        lines.append("")
        lines.append(f"🔁 reboot-required on: {', '.join(reboot_needed)}")
        lines.append("Chaining → `/cluster-reboot confirm` …")
        chain = cmd_cluster_reboot("confirm")
        lines.append("")
        lines.append(chain)
    else:
        lines.append("")
        lines.append("No reboot required. Done.")
    return "\n".join(lines)


def cmd_template(raw_args):
    """List / preview / validate / apply cluster templates from chat.
    Usage: /template [list|status|validate <name>|preview <name>|apply <name> [confirm]]"""
    import json as _json
    parts = (raw_args or "").split()
    sub = parts[0] if parts else "list"
    if sub == "apply":
        if len(parts) < 2:
            return "Usage: /template apply <name> [confirm]"
        argv = ["x", "apply", parts[1]] + (["--confirm"] if _confirmed(raw_args) else [])
    elif sub in ("preview", "validate"):
        if len(parts) < 2:
            return f"Usage: /template {sub} <name>"
        argv = ["x", sub, parts[1]]
    elif sub in ("list", "status"):
        argv = ["x", sub]
    else:
        return ("Usage: /template [list|status|validate <name>|preview <name>|"
                "apply <name> [confirm]]")
    res = cmdlib.template_cli(argv)
    return "📦 *HSCC template*\n```\n" + _json.dumps(res, indent=2, default=str)[:3000] + "\n```"


def register(ctx) -> None:
    ctx.register_command(
        name="cluster", handler=cmd_cluster,
        description="Show HSCC cluster status (orchestrator + workers, live health).",
    )
    ctx.register_command(
        name="status", handler=cmd_status,
        description="Rich HSCC dashboard: topology, free-VRAM, proxy, daemon, template.",
    )
    ctx.register_command(
        name="orch-restart", handler=cmd_orch_restart,
        description="Restart the orchestrator vLLM (confirm-first).",
        args_hint="confirm",
    )
    ctx.register_command(
        name="cluster-restart", handler=cmd_cluster_restart,
        description="Recover the cluster by re-applying the active template (confirm-first).",
        args_hint="confirm",
    )
    ctx.register_command(
        name="cluster-reboot", handler=cmd_cluster_reboot,
        description="Reboot all nodes (workers parallel, orchestrator last) — confirm-first.",
        args_hint="confirm",
    )
    ctx.register_command(
        name="cluster-down", handler=cmd_cluster_down,
        description="Stop all vLLM units cluster-wide (hosts stay up) — confirm-first.",
        args_hint="confirm",
    )
    ctx.register_command(
        name="cluster-docker-prune", handler=cmd_cluster_docker_prune,
        description="docker system prune -af on every node (volumes preserved) — confirm-first.",
        args_hint="confirm",
    )
    ctx.register_command(
        name="cluster-apt-upgrade", handler=cmd_cluster_apt_upgrade,
        description="apt update+upgrade across cluster; chains to /cluster-reboot if needed.",
        args_hint="confirm",
    )
    ctx.register_command(
        name="heal", handler=cmd_heal,
        description="Heal unhealthy workers; advise on orchestrator wedge (confirm-first).",
        args_hint="confirm",
    )
    ctx.register_command(
        name="template", handler=cmd_template,
        description="List/preview/validate/apply cluster templates.",
        args_hint="list|preview <name>|apply <name> [confirm]",
    )
