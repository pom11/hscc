"""hscc-cluster mutating self-heal handlers. Shared with the daemon (autonomous path)."""
try:
    from . import clusterlib as cl  # package context (runtime)
except ImportError:
    import clusterlib as cl  # direct import context (tests)


#: Verdicts for :func:`_serving_verdict` — the reaper's "does serving.json still
#: describe the fleet?" question. Deliberately the same tri-state shape as
#: ``hscc_daemon/health.py::_pid_is_tracked_or_descendant`` (t_af7d5486, the proxy
#: reaper's "is this mine?" guard): PROVEN OK, PROVEN NOT OK, or NOT EVALUABLE —
#: and NOT EVALUABLE takes the refusal branch, because an evidence source that
#: could not be read is not evidence for a destructive action. That doctrine is
#: reused here; the ancestry helper is not, because it answers a different
#: question about a different object (a PID, not a JSON file).
SERVING_TRUSTWORTHY = "trustworthy"
SERVING_STALE = "stale"
SERVING_NOT_EVALUABLE = "not_evaluable"


def _running_containers():
    """Live: [{node, recipe}] for sparkrun containers across the cluster."""
    out = []
    for node in cl.NODES + [cl.HEAD]:
        r = cl.ssh_cmd(node, "docker ps --format '{{.Names}}' | grep -i sparkrun", timeout=12)
        for name in [l for l in r["stdout"].splitlines() if l.strip()]:
            out.append({"node": node, "recipe": name.strip()})
    return out


def _serving_nodes(snapshot):
    """Nodes serving.json names, from a :func:`clusterlib.serving_snapshot`."""
    nodes = set()
    for u in snapshot.get("units") or []:
        if not isinstance(u, dict):
            continue
        for n in (u.get("nodes") or []):
            nodes.add(n)
    return nodes


def _serving_verdict(candidate_nodes, snapshot, worker_nodes):
    """Decide whether serving.json may be trusted to define "orphan" right now.

    F13 (docs/audits/2026-11-23-agent-surface.md §4, §9): serving.json is the
    *definition* of an orphan and has >=3 writers (bootstrap ``serving_gen``,
    ``template apply``, ``onboard.py``), so a copy that is behind reality makes a
    LIVE workload reapable — the one tool in the surface where a stale local file
    deletes work. This is the guard the audit asks for. It does NOT take ownership
    of serving.json: it reads it and refuses, it never writes or repairs.

    Two refusals, both chosen because they fire on the *file being wrong* rather
    than on a container being merely unlisted. Note what cannot be a signal here:
    "this orphan's node is absent from serving.json" is true of EVERY orphan by
    construction (that is how the orphan list is built), so coverage of the
    candidate node says nothing and refusing on it would disable the tool.

      * ``not_evaluable`` — the file is absent, unreadable, unparseable, or it
        parsed and declared zero units. Every one of those answers ``[]`` from the
        tolerant reader, which makes *every* sparkrun container on *every* worker
        an orphan; a reaper must not treat "said nothing" as "serves nothing".
        This is the whole-fleet wipe shape, and today it is the DEFAULT shape —
        no serving.json on a host is enough.
      * ``blast_radius`` — the orphan set covers every live worker node. A
        leftover container is local; a file that has fallen behind reality marks
        the whole fleet. This is the second half of the same signature and it
        fires even when the file parses beautifully (e.g. regenerated from a
        different topology, or written for a fleet since grown).
      * ``trustworthy`` — the file is readable, declares something, and the
        candidates are a subset of the fleet rather than all of it.

    A single-worker fleet cannot be split by the blast-radius test (one worker is
    trivially "all workers"), so a non-empty reap there needs ``force=true``.
    That is intended: on a one-worker fleet there is no shape difference between
    "the only worker has a leftover" and "the file describes somewhere else".

    Vacuously ``trustworthy`` when there is nothing to reap: no candidates means
    no destructive branch to protect, and a refusal there would be noise.

    Returns a dict (``verdict``, ``code``, ``reason``, ``age_s``, ``path``,
    ``orphan_nodes``, ``worker_nodes``) so both the refusal and the confirmation
    text can quote WHY, and never mutates anything.
    """
    orphan_nodes = sorted(set(candidate_nodes))
    workers = sorted(set(worker_nodes))
    age_s = snapshot.get("age_s")
    verdict = {
        "verdict": SERVING_TRUSTWORTHY, "code": "covered", "reason": "",
        "age_s": age_s, "path": snapshot.get("path"),
        "orphan_nodes": orphan_nodes, "worker_nodes": workers,
    }
    if not orphan_nodes:
        verdict["code"] = "nothing_to_reap"
        return verdict
    state = snapshot.get("state")
    if state != cl.SERVING_OK:
        verdict.update({
            "verdict": SERVING_NOT_EVALUABLE, "code": f"file_{state}",
            "reason": (f"{snapshot.get('path')} is {state} — serving.json is the "
                       f"definition of 'orphan', and a reaper cannot read a verdict "
                       f"out of a file it cannot parse. Without it every sparkrun "
                       f"container on every worker reads as an orphan"),
        })
        return verdict
    if not _serving_nodes(snapshot):
        verdict.update({
            "verdict": SERVING_NOT_EVALUABLE, "code": "no_units",
            "reason": (f"{snapshot.get('path')} parsed but names no serving node — "
                       f"'nothing serves' and 'the file is behind' answer the same "
                       f"way, and the first one stops every container"),
        })
        return verdict
    if workers and set(orphan_nodes) >= set(workers):
        verdict.update({
            "verdict": SERVING_STALE, "code": "whole_fleet",
            "reason": (f"every live worker node ({', '.join(workers)}) has a container "
                       f"serving.json does not account for (file {_fmt_age(age_s)}). A "
                       f"leftover is local; a file that has fallen behind reality marks "
                       f"the whole fleet, and this tool cannot tell those apart from "
                       f"here — re-check the topology before stopping anything"),
        })
    return verdict


def _node_claimed(node, snapshot):
    """True when a FRESH serving.json now names ``node`` for some unit.

    The action-time half of the guard, and the only half that runs with the
    decision already made. serving.json has >=3 writers and none of them take a
    lock against this reaper, so the gap between "planned the stop" and "ran the
    stop" is exactly when a bootstrap / ``template apply`` / ``onboard.py`` pass
    can hand a node to the serving layer. If the file says that node serves
    something *now*, the container on it is a workload that was live when we
    looked and is a workload when we act — so the stop is dropped.

    Deliberately one-directional: an unreadable or vanished re-read is NOT new
    evidence of anything (it is the state the plan-time verdict already judged,
    and possibly judged under ``force``), so it never aborts on its own. Only
    positive evidence of a claim aborts.
    """
    if snapshot.get("state") != cl.SERVING_OK:
        return False
    return node in _serving_nodes(snapshot)


def _fmt_age(age_s):
    """Human age for guard text. None means we could not stat the file."""
    if age_s is None:
        return "age unknown"
    if age_s < 90:
        return f"{int(age_s)} s old"
    if age_s < 7200:
        return f"{int(age_s // 60)} min old"
    return f"{age_s / 3600:.1f} h old"


def _guard_suffix(snapshot, verdict):
    """What the confirmation text quotes: the age and reach of the 'orphan' file."""
    return (f" [definition of orphan: {snapshot.get('path')}, "
            f"{_fmt_age(snapshot.get('age_s'))}, names "
            f"{len(_serving_nodes(snapshot))} node(s) vs "
            f"{len(verdict['worker_nodes'])} live worker(s); "
            f"guard: {verdict['verdict']}]")


def restart_model(args, **kwargs):
    """Shared restart primitive for both the agent toolset and the daemon.

    Agent/worker path: restart_model({recipe, node}) -> hard `stop` then `run`,
    HEAD-refused unless force. (No optional flags passed -> behavior unchanged.)

    Daemon/orchestrator path: the head vLLM needs its sparkrun launch flags
    (--cluster for the NAS cache_dir, --port, --no-follow, --ensure). Passing
    ensure=True selects launch-only semantics (NO hard stop) so a transient
    health blip can't thrash a model that is still loading.
    """
    recipe, node = args["recipe"], args["node"]
    if node == cl.HEAD and not args.get("force", False):
        return {"refused": True, "executed": False,
                "reason": f"{node} is the orchestrator/head node; refusing to restart it. Pass force=true to override."}
    cluster = args.get("cluster")
    port = args.get("port")
    ensure = bool(args.get("ensure", False))
    no_follow = bool(args.get("no_follow", False))
    mode = "ensure-up" if ensure else "stop then run"
    action = f"restart_model {recipe} on {node} ({mode} via sparkrun)"
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    run = [cl.SPARKRUN, "run", recipe, "--hosts", node]
    if cluster:
        run += ["--cluster", cluster]
    if port:
        run += ["--port", str(port)]
    if no_follow:
        run.append("--no-follow")
    if ensure:
        run.append("--ensure")  # launch-only: skip the stop, no thrash
    else:
        cl.run_cmd([cl.SPARKRUN, "stop", recipe, "--hosts", node], timeout=120)
    r = cl.run_cmd(run, timeout=900)
    base_port = int(port) if port else 8000
    return {"ok": r["ok"], "executed": True, "node": node,
            "base_url": f"http://{node}:{base_port}/v1", "stdout_tail": r["stdout"][-2000:]}


def remount_nas(args, **kwargs):
    node = args["node"]
    action = f"remount_nas on {node} (umount -l /mnt/nas then mount)"
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    r = cl.ssh_cmd(node, "sudo umount -l /mnt/nas; sudo mount /mnt/nas && ls /mnt/nas | head",
                   timeout=40)
    return {"ok": r["ok"], "executed": True, "node": node, "out": r["stdout"][-1000:]}


def repair_nas_export(args, **kwargs):
    action = ("repair_nas_export on QNAP .249: backup /etc/exports, restore export line, "
              "exportfs -ra (HIGH-RISK)")
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    bk = cl.ssh_cmd(cl.NAS_HOST,
                    "sudo cp /etc/exports /etc/exports.bak.$(date +%s) 2>/dev/null; "
                    "sudo exportfs -ra 2>&1; sudo exportfs -v 2>&1", timeout=30)
    return {"ok": bk["ok"], "executed": True, "out": bk["stdout"][-2000:]}


def reap_orphans(args, **kwargs):
    """Stop sparkrun containers that serving.json does not account for.

    GUARDED twice over, and deliberately so: this is the one mutating tool whose
    notion of "what may I delete" comes from a local file with >=3 writers
    (F13 — see :func:`_serving_verdict`).

      1. **Staleness guard** (:func:`_serving_verdict`), evaluated against the live
         containers and BEFORE the confirm gate, so a preview reports the refusal
         rather than promising a stop it would not perform. An absent, unreadable,
         unparseable or unit-less serving.json refuses, and so does an orphan set
         that covers every live worker node — the shape of a file that has fallen
         behind reality rather than of one leftover container. Both return
         ``executed: False`` and name why. Only ``force=true`` overrides them, and
         it overrides the guard alone: the head exemption below is not negotiable.
      2. **Action-time re-read** (:func:`_node_claimed`), immediately before EACH
         stop. A writer that claimed the node while this call was deciding drops
         that one stop (reported under ``skipped``) and cannot be overridden.
      3. **Confirm gate** (:func:`clusterlib.confirm_gate`), whose preview text now
         quotes the age and reach of the file the stop is based on.

    serving.json stays the definition of "orphan" — this tool neither writes nor
    repairs it (the daemon owns that file); the guard only decides whether today's
    copy is fit to act on.
    """
    snap = cl.serving_snapshot()
    serving = _serving_nodes(snap)
    # cl.NODES is discovery's worker_ips list (the PEP 562 lazy attr is typed as a
    # union; it is a list of node identifiers here, always).
    workers = sorted({str(n) for n in (cl.NODES or [])})
    # never target the head/orchestrator node, regardless of serving.json state
    orphans = [c for c in _running_containers()
               if c["node"] not in serving and c["node"] != cl.HEAD]
    guard = _serving_verdict([o["node"] for o in orphans], snap, workers)
    force = bool(args.get("force", False))
    if guard["verdict"] != SERVING_TRUSTWORTHY and not force:
        return {"refused": True, "executed": False, "reaped": [], "failed": None,
                "guard": guard["verdict"], "guard_code": guard["code"],
                "orphan_nodes": guard["orphan_nodes"], "worker_nodes": guard["worker_nodes"],
                "serving_json": guard["path"], "serving_age_s": guard["age_s"],
                "reason": (guard["reason"] + " — nothing was stopped; re-call with "
                           "force=true only if you have confirmed by hand that these "
                           "containers are not serving"),
                "would_have_reaped": [(o["node"], o["recipe"]) for o in orphans] or None}
    action = (f"reap_orphans: stop {[ (o['node'],o['recipe']) for o in orphans]}"
              + _guard_suffix(snap, guard)
              + (" — GUARD OVERRIDDEN by force=true"
                 if guard["verdict"] != SERVING_TRUSTWORTHY and force else ""))
    gate = cl.confirm_gate(args.get("confirm", False), action)
    if gate:
        return gate
    reaped = []
    failed = []
    skipped = []
    for o in orphans:
        # Re-read at the moment of harm: serving.json has >=3 unlocked writers, and
        # a node the file has claimed since we planned the stop is a live workload.
        fresh = cl.serving_snapshot()
        if _node_claimed(o["node"], fresh):
            skipped.append({"node": o["node"], "recipe": o["recipe"],
                            "reason": f"serving.json now names {o['node']} for a unit "
                                      f"(claimed after the stop was planned)"})
            continue
        r = cl.run_cmd([cl.SPARKRUN, "stop", o["recipe"], "--hosts", o["node"]], timeout=120)
        if r["ok"]:
            reaped.append({"node": o["node"], "recipe": o["recipe"]})
        else:
            failed.append({"node": o["node"], "recipe": o["recipe"],
                           "error": r.get("stderr", "unknown")[:200]})
    out = {"ok": len(failed) == 0, "executed": True, "reaped": reaped,
           "guard": guard["verdict"], "guard_overridden": bool(force) and
           guard["verdict"] != SERVING_TRUSTWORTHY,
           "failed": failed if failed else None}
    if skipped:
        out["skipped"] = skipped
        out["ok"] = out["ok"] and False  # an unverified claim is not a clean reap
    return out
