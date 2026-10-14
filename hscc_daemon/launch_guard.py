"""One-vLLM-per-host invariant (A2, HEAL_CASCADE 2026-10-14 follow-up).

WHY THIS EXISTS
---------------
On 2026-10-14 the fleet was destroyed by healing, and the audit (F7 follow-up)
found the class of bug that makes a heal *worse than the fault*: every launch
path asked sparkrun to place a container without ever asking what was already
on that host. A heal that starts a second vLLM on a host whose GPU already
holds one does not restore capacity, it over-commits the GPU — and on this
fleet that is how .244 and .247 hard-hung.

THE INVARIANT
-------------
A host may run a vLLM workload ONLY at a (host, port) covered by the serving
declaration. Concretely: before starting a container for unit U on host H,
look at what sparkrun reports actually running on H. Any vLLM workload there
at a port OUTSIDE U's declared span belongs to someone else -> refuse.

"Outside the declared span" is the operative phrase, and it is what makes this
safe to run against the live fleet: the orchestrator's host legitimately serves
BOTH the
orchestrator (:8000) and an embedding model (:8010). A naive "one vLLM per
host" rule would refuse the second one and break a working deployment. What is
forbidden is *undeclared* co-location, not co-location.

REFUSE + ALERT, NEVER PREEMPT (operator decision, 2026-10-14)
-------------------------------------------------------------
A refusal here is deliberately weak in one direction and hard in the other:
  * It NEVER stops, kills or reconfigures the workload already there. That
    workload may be another tenant's live serving (the fleet is shared); the
    guard has no authority over it and no right to guess it is stale.
  * It DOES hard-refuse its own launch, log ERROR, notify the operator and
    leave a durable events.jsonl row. A silent skip would be the worst
    outcome: the unit stays down, nothing is over-committed, and nobody knows
    why — exactly how the 2026-10-14 incident stayed invisible for an hour.

STATUS UNAVAILABLE IS A REFUSAL
-------------------------------
This is not a conservative flourish. That morning, the thing that was actually
running on :4000 was invisible to every status surface hscc consulted, and
every one of those surfaces reported success. "Could not look" is not evidence
that the host is free, so the guard treats it as a refusal with its own reason
code — and says so, because a guard that refuses for the wrong reason is
indistinguishable from a broken guard otherwise.
"""

from __future__ import annotations

# Reason codes on a refusal. Stable strings: they go into logs, the event row
# and the notification, and an operator greps for them.
REASON_FOREIGN = "foreign_vllm_on_host"
REASON_NO_STATUS = "status_unavailable"

# Sentinel a caller passes when it could not obtain cluster status at all.
# Distinct from {} (which means "we looked, nothing is running").
NO_STATUS = object()

# Workload recipes that occupy the GPU the way a vLLM unit does. Matched
# against the recipe string sparkrun reports in job metadata. Deliberately
# substring-based: recipes are user-named paths (`.../qwen3.8-...-solo-vllm.yaml`)
# and the only reliable signal is that the recipe is a vLLM recipe.
_VLLM_RECIPE_MARKERS = ("vllm", "vllm-", "sglang", "trtllm", "triton")


def _meta_of(entry):
    meta = entry.get("meta")
    return meta if isinstance(meta, dict) else {}


def declared_footprint(serving):
    """{(host, port)} for every unit serving.json declares — the set that is
    ALLOWED to be co-located. A (host, port) not in this set, seen live, is
    "a vLLM outside the declared span" — the exact shape of the 2026-10-14
    incident (heal paths re-issuing workloads with derived cluster_ids, and
    anything an operator or another tenant started that hscc never declared).

    Ports resolve exactly as fleet_up_plan resolves them (unit port, else the
    top-level serving port, else the VLLM_PORT default — via serving.serving_port,
    the same helper the planner uses), so the guard and the launcher cannot
    disagree about where a unit is supposed to live.
    """
    from .serving import serving_port as _serving_port  # deferred: no cycle
    units = (serving or {}).get("units") or []
    default_port = _serving_port(serving)
    out = set()
    for u in units:
        if not isinstance(u, dict):
            continue
        port = u.get("port") or default_port
        if port is None:
            continue
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        for h in (u.get("nodes") or []):
            if h:
                out.add((h, port))
    return out


def _recipe_of(meta):
    return str(meta.get("recipe") or "").lower()


def is_vllm_workload(meta):
    """True if this sparkrun job metadata looks like a GPU model server.

    Non-vLLM workloads (a database, a build, a CPU job sharing the node) are
    NOT this invariant's business — they do not claim the whole GPU, and
    refusing a model server because a postgres container is running would make
    the guard useless and immediately disabled by whoever hit it first.
    """
    recipe = _recipe_of(meta)
    if not recipe:
        # Unknown recipe: assume it is a model server. The guard protects a
        # GPU, and "unlabelled workload" is the shape an orphan proxy / a
        # hand-started container takes — precisely what must not be walked into.
        return True
    return any(m in recipe for m in _VLLM_RECIPE_MARKERS)


def live_vllm_on_host(status, host):
    """Return [{port, recipe, cluster_id, host}] for vLLM workloads on `host`.

    Reads sparkrun's own structured status (`ClusterStatusResult.to_dict()`):
    ``groups[*]`` (multi-host/TP spans: containers list one entry per member
    host, the port/recipe live on the group's meta) and ``solo_entries[*]``
    (single-host units: host and meta on the entry itself). This is the same
    payload shape `_sparkrun_created_map` already consumes — one transport,
    one shape, no second way of asking.
    """
    if not isinstance(status, dict):
        return []
    found = []
    for cid, group in (status.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        meta = _meta_of(group)
        if not is_vllm_workload(meta):
            continue
        port = meta.get("port")
        if port is None:
            continue
        for c in group.get("containers") or []:
            if not isinstance(c, dict) or c.get("host") != host:
                continue
            found.append({"port": int(port), "recipe": str(meta.get("recipe") or ""),
                          "cluster_id": cid, "host": host})
    for entry in status.get("solo_entries") or []:
        if not isinstance(entry, dict) or entry.get("host") != host:
            continue
        meta = _meta_of(entry)
        if not is_vllm_workload(meta):
            continue
        port = meta.get("port")
        if port is None:
            continue
        found.append({"port": int(port), "recipe": str(meta.get("recipe") or ""),
                      "cluster_id": entry.get("cluster_id"), "host": host})
    return found


def evaluate(status, host, port, span, declared=None):
    """May we start a container for this unit on `host`?

    Args:
        status: sparkrun's structured status dict, or NO_STATUS when the
            lookup failed.
        host: the host the launch would land on.
        port: the port the unit serves on.
        span: the unit's declared host list (`serving.json` `nodes`), i.e. the
            hosts this unit ALREADY owns. A workload on one of these hosts at
            this unit's own port is the unit's own previous container, not a
            stranger — the launch paths stop-then-start precisely that, and the
            guard must not refuse a heal for finding the thing it is replacing.
        declared: the FLEET-WIDE declared footprint, a set of (host, port)
            pairs covered by serving.json. This is what "outside the declared
            span" means concretely: one host may legitimately serve BOTH the
            orchestrator (:8000) and an embedding model (:8010) on one host —
            a workload at a DECLARED (host, port) is co-location the operator
            chose, and refusing it would make the guard false-positive on a
            working deployment until someone disabled it. An UNdeclared
            (host, port) is the incident shape: something holding a GPU that
            no one declared. None/empty => only the launching unit's own
            footprint is declared (the strictest reading — fail closed).

    Returns (ok: bool, reason_code: str|None, detail: dict).
    """
    detail = {"host": host, "port": port, "span": list(span or [])}
    if status is NO_STATUS or not isinstance(status, dict):
        detail["error"] = ("cluster status unavailable — refusing because "
                           "'could not look' is not evidence the host is free")
        return False, REASON_NO_STATUS, detail
    # sparkrun's per-host errors map: the fleet answered, but THIS host did
    # not. Same rule as a total failure, narrowed — and it must be checked
    # before the "no workloads found" pass, because an unreachable host
    # trivially has no visible workloads and would otherwise read as empty.
    host_err = (status.get("errors") or {}).get(host)
    if host_err:
        detail["error"] = (f"sparkrun status for {host} failed ({host_err}) — "
                           "refusing because 'could not look' is not evidence "
                           "the host is free")
        return False, REASON_NO_STATUS, detail

    try:
        own_port = int(port)
    except (TypeError, ValueError):
        detail["error"] = f"unparseable port {port!r}"
        return False, REASON_NO_STATUS, detail

    # Ports on THIS host that the fleet declares: the launching unit's own
    # slot (its span at its port — including the container this launch
    # replaces) plus every serving.json-declared (host, port) on the host.
    allowed = {own_port} if host in {h for h in (span or [])} else set()
    for (d_host, d_port) in (declared or ()):
        try:
            if d_host == host:
                allowed.add(int(d_port))
        except (TypeError, ValueError):
            continue

    foreign = []
    for w in live_vllm_on_host(status, host):
        if w["port"] in allowed:
            continue          # declared co-location (or the unit's own slot)
        foreign.append(w)
    detail["foreign"] = foreign
    if foreign:
        detail["error"] = (f"{len(foreign)} vLLM workload(s) already on {host} "
                           f"outside the declared span: "
                           + ", ".join(f":{w['port']} ({w['recipe'] or 'unknown recipe'})"
                                       for w in foreign))
        return False, REASON_FOREIGN, detail
    return True, None, detail


def alert_refusal(actor, unit, reason, detail, notify=None, emit=None):
    """Log ERROR + notify + events.jsonl row for a refusal. Never raises.

    notify/emit are injectable so tests capture alerts without touching the
    live desktop or the real event ledger — and so that a failure in the alert
    path can never turn a refusal into a crash of the heal loop.
    """
    if notify is None or emit is None:
        from .desktop import emit_event as _emit, send_macos_notification as _notify
        emit = emit or _emit
        notify = notify or _notify

    host = detail.get("host")
    msg = (f"Refused to start {unit or '?'} on {host}:{detail.get('port')} — "
           f"{detail.get('error')} (actor={actor}). hscc will NOT stop the "
           f"workload already there; it may belong to another tenant. Move the "
           f"unit or free the host, then retry.")
    from .daemon_ops import log
    log(msg, "ERROR")
    try:
        notify("HSCC: launch refused — GPU already claimed", msg, priority="high")
    except Exception:
        pass
    try:
        emit("launch_guard.refused", {
            "actor": actor, "unit": unit, "host": host, "port": detail.get("port"),
            "span": detail.get("span"), "reason": reason,
            "foreign": detail.get("foreign"), "severity": "critical",
        })
    except Exception:
        pass
    return msg
