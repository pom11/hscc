"""Executor seam of the node-pause overlay: enumerate, stop and resume units.

What this module owns
---------------------
``plan(host)``      which sparkrun units touch a node, with the FULL span of
                    every unit that spans several nodes (TP across nodes), plus
                    whether that node serves the orchestrator model.
``stop_units(...)`` take those units down.
``resume_units(...)`` bring them back with the SAME recipe and flags that were
                    recorded when the node was paused.

Transport: sparkrun only
------------------------
Every fleet fact here comes from the sanctioned structured status query —
``health._SPARKRUN_STATUS_SCRIPT`` executed under sparkrun's OWN venv python
(``health._sparkrun_venv_python()``, itself a thin delegate to
``sparkrun_bin.sparkrun_venv_python``), i.e. sparkrun's ``resolve_hosts`` +
``api.status`` + ``classify_cluster_status`` and the ``to_dict()`` that
results. That is the same transport ``health._sparkrun_workloads`` and
``recover.fetch_identity_entries`` already use, so this module adds no second
path to the fleet: no raw ``ssh``, no local ``docker`` (a local ``docker ps``
answers *empty*, not *error*, on this fleet — the v1.15.0 failure class), and
no hand-rolled host resolution. Lifecycle commands are the existing builders
(``serving.vllm_stop_cmd`` for the stop, ``serving._unit_run_cmd`` — the
keepalive relaunch command, ``--ensure`` included — for the resume) and are
executed through ``util.run_cmd``, which performs the PATH-independent
``sparkrun`` argv resolution (t_b543e530).

Why span detection is the hard part of ``plan``
-----------------------------------------------
A tensor-parallel unit occupies several nodes at once. Stopping it "on this
node" *is* stopping it on every node it spans, so a pause that enumerated only
the target host would take a workload down somewhere else without ever telling
the operator. ``_live_entries`` therefore builds each unit's node set as the
UNION of the observed placement (``groups[cid].containers[].host``) and the
intent-recorded placement (``meta.hosts``): the observed half under-reports when
a sibling node did not answer the sweep, and the intent half is the record of
what the unit was launched across. ``span_nodes`` in the plan is every node that
would be affected, not just the one named.

Safety (rule from #478)
-----------------------
``stop_units`` and ``resume_units`` are DRY-RUN by default: they print exactly
what they would run and mutate nothing. Only ``apply=True`` executes. A unit
whose launch recipe cannot be derived is REFUSED with a printed reason rather
than restarted from a guessed command — an invented ``sparkrun run`` derives a
different intent id and launches a parallel duplicate of a live unit.
"""

from __future__ import annotations

import json

from . import serving
from . import util

#: Shell-out cap for ONE lifecycle command (a stop, or a --ensure resume).
#: These are single-host sparkrun control calls, not a fleet sweep, so they sit
#: well under health.SPARKRUN_STATUS_TIMEOUT (the sweep budget must stay above
#: its own worst case — t_614f4cac — and a control call must not inherit that).
SPARKRUN_CONTROL_TIMEOUT = 120

#: The alias the orchestrator unit advertises (serving._role_alias). Used only
#: as LIVE corroboration of the serving config, never as the primary source.
_ORCHESTRATOR_ALIAS = "orchestrator-model"


# ── structured status read ─────────────────────────────────────────────────
def fetch_cluster_status():
    """Sparkrun's structured cluster status: ``(data, error)``.

    Runs ``health._SPARKRUN_STATUS_SCRIPT`` under sparkrun's own venv python —
    the same two lines ``recover.fetch_identity_entries`` uses — and parses the
    printed ``ClusterStatusResult.to_dict()``.

    Unlike that caller, this one does NOT collapse failure into an empty list:
    "the fleet is empty" and "we could not look" are different facts, and a
    pause that read the second as the first would stop nothing and report
    success. The error string says which leg failed.
    """
    from . import health as _health

    venv_py = _health._sparkrun_venv_python()
    if not venv_py:
        return None, ("sparkrun venv python unresolvable — cannot read cluster "
                      "status (is the sparkrun CLI installed?)")
    try:
        res = util.run_cmd([venv_py, "-c", _health._SPARKRUN_STATUS_SCRIPT],
                           timeout=_health.SPARKRUN_STATUS_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — report, never propagate a transport error
        return None, f"sparkrun status query raised: {e}"
    if not res or not res.get("ok"):
        return None, (f"sparkrun status query failed: "
                      f"{(res or {}).get('stderr') or (res or {}).get('output') or 'no output'}")
    try:
        data = json.loads(res.get("output") or "")
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        return None, f"sparkrun status JSON unparsable: {e}"
    if not isinstance(data, dict) or (
            "groups" not in data and "solo_entries" not in data):
        # The script's no-hosts early exit answers {"host_list": []}: a reply,
        # but not a fleet observation (health treats it the same way).
        return None, ("sparkrun status answered without fleet data "
                      "(host_list-only payload) — host resolution is broken?")
    return data, None


def _hosts_of(values):
    """Clean, de-duplicated, order-preserving host list."""
    out = []
    for h in (values or []):
        h = str(h).strip()
        if h and h not in out:
            out.append(h)
    return out


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _live_entries(data):
    """``to_dict()`` -> one entry per live workload, with its FULL span.

    Covers both shapes ``classify_cluster_status`` emits: ``groups`` (a job with
    one or more containers, keyed by cluster id — the multi-node/TP shape) and
    ``solo_entries`` (a standalone container). ``cluster_id`` is read from the
    group key for groups; for solo entries sparkrun's current ``to_dict()``
    spells the field ``workload_id``, while older readers in this repo (and
    older sparkrun) use ``cluster_id`` — both are accepted so a sparkrun rename
    cannot silently make every unit anonymous.

    ``nodes`` is the union described in the module docstring. ``tp`` prefers the
    recorded parallelism so a resume can re-emit the declared ``--tp``.
    """
    entries = []
    groups = data.get("groups")
    if isinstance(groups, dict):
        for cid, group in groups.items():
            if not isinstance(group, dict):
                continue
            meta = group.get("meta") or {}
            observed = _hosts_of([c.get("host") for c in
                                  (group.get("containers") or [])
                                  if isinstance(c, dict)])
            entries.append({
                "cluster_id": str(cid or ""),
                "nodes": _hosts_of(_hosts_of(meta.get("hosts")) + observed),
                "observed_nodes": observed,
                "port": _as_int((meta.get("overrides") or {}).get("port")
                                or meta.get("port")),
                "recipe": meta.get("recipe") or "",
                "model": meta.get("model") or "",
                "served_model_name": meta.get("served_model_name") or "",
                "tp": _as_int(meta.get("tensor_parallel") or meta.get("tp")),
            })
    for entry in data.get("solo_entries") or []:
        if not isinstance(entry, dict):
            continue
        meta = entry.get("meta") or {}
        observed = _hosts_of([entry.get("host")])
        cid = str(entry.get("workload_id") or entry.get("cluster_id") or "")
        entries.append({
            "cluster_id": cid,
            "nodes": _hosts_of(_hosts_of(meta.get("hosts")) + observed),
            "observed_nodes": observed,
            "port": _as_int((meta.get("overrides") or {}).get("port")
                            or meta.get("port")),
            "recipe": meta.get("recipe") or "",
            "model": meta.get("model") or "",
            "served_model_name": meta.get("served_model_name") or "",
            "tp": _as_int(meta.get("tensor_parallel") or meta.get("tp")),
        })
    return entries


# ── serving.json correlation ───────────────────────────────────────────────
def _serving_units(serving_dict):
    """Declared units of a parsed serving.json (empty on missing/malformed)."""
    if not isinstance(serving_dict, dict):
        return []
    out = []
    for u in (serving_dict.get("units") or []):
        if isinstance(u, dict) and (u.get("nodes") or []):
            out.append(u)
    return out


def _find_declared_unit(declared, live):
    """Declared serving unit for a live entry: unique node+port match.

    Identity is node + port, the same rule ``recover._match_by_identity`` uses:
    in a uniform-pool fleet every unit shares recipe and model, so recipe
    matching is not identity. A UNIQUE match is required — zero matches returns a
    reason (a foreign/unconfigured container), two matches returns a reason too
    (ambiguous), and a unit without a unique match is never handed a resume
    command.

    Returns ``(unit_or_None, reason_or_None)``.
    """
    nodes = _hosts_of(live.get("nodes"))
    port = live.get("port")
    by_node = [u for u in declared
               if set(nodes) & set(_hosts_of(u.get("nodes")))]
    if not by_node:
        return None, "no serving.json unit declares these nodes"
    if port not in (None, ""):
        by_port = [u for u in by_node if _as_int(u.get("port")) == port]
        if len(by_port) == 1:
            return by_port[0], None
        if len(by_port) > 1:
            return None, (f"{len(by_port)} serving.json units declare port "
                          f"{port} on these nodes")
        return None, f"no serving.json unit on these nodes uses port {port}"
    if len(by_node) == 1:
        return by_node[0], None
    return None, (f"{len(by_node)} serving.json units share these nodes and "
                  "the sweep reported no port")


# ── orchestrator-model detection ───────────────────────────────────────────
def orchestrator_flag(host, serving_dict, live_entries=None):
    """Whether ``host`` serves the orchestrator model, and the evidence.

    SOURCE: the served-model config — serving.json's ``role == "orchestrator"``
    units, through ``serving.orchestrator_nodes``. The live status is consulted
    only as corroboration: a workload advertising the ``orchestrator-model``
    alias on this host raises the flag even when the config does not.

    Fail-closed by design. The CLI refuses to pause an orchestrator host without
    ``--force``, so the direction of a wrong answer matters: an unreadable
    serving config returns ``True`` ("cannot rule it out") rather than ``False``,
    because the expensive mistake is pausing the node every agent depends on.
    """
    if not isinstance(serving_dict, dict):
        return True, ("serving config unavailable — cannot rule out this host "
                      "serving the orchestrator model")
    nodes = serving.orchestrator_nodes(serving_dict)
    if host in nodes:
        ids = [u.get("id") or "?" for u in _serving_units(serving_dict)
               if u.get("role") == "orchestrator"
               and host in _hosts_of(u.get("nodes"))]
        return True, f"serving config: orchestrator unit(s) {ids} declare {host}"
    for entry in (live_entries or []):
        if host in _hosts_of(entry.get("nodes")):
            served = (_ORCHESTRATOR_ALIAS in
                      str(entry.get("served_model_name") or "").split())
            if served:
                return True, (f"live status: workload {entry.get('cluster_id')} "
                              f"advertises {_ORCHESTRATOR_ALIAS} on {host}")
    return False, f"serving config declares no orchestrator unit on {host}"


# ── the plan ───────────────────────────────────────────────────────────────
def _unit_record(live, declared, host, serving_dict):
    """Assemble one enumerable unit: identity, span, and its two commands."""
    nodes = _hosts_of(live.get("nodes"))
    declared_node = declared if declared else {}
    declared_nodes = _hosts_of(declared_node.get("nodes"))
    # The declared span joins the observed one: a node that did not answer the
    # sweep still hosts part of this unit, and the operator has to see it.
    nodes = _hosts_of(nodes + declared_nodes)
    recipe = live.get("recipe") or (declared_node.get("recipe") or "")
    port = live.get("port") if live.get("port") is not None \
        else _as_int(declared_node.get("port"))
    tp = live.get("tp")
    if tp is None:
        tp = _as_int(declared_node.get("tp"))
    role = declared_node.get("role") or ("orchestrator"
                                         if host in serving.orchestrator_nodes(
                                             serving_dict) else "worker")
    unit_id = declared_node.get("id") or live.get("cluster_id") or ",".join(nodes)

    stop_cmd = None
    if recipe and nodes:
        stop_cmd = serving.vllm_stop_cmd(node=",".join(nodes), recipe=recipe,
                                         port=port)
    resume_cmd = None
    resume_blocked = None
    if declared and declared.get("recipe"):
        # The keepalive relaunch command, verbatim: recipe, --cluster, --hosts,
        # --port, --no-follow, --ensure, --served-model-name, --tp and the
        # recorded -o overrides. The declared unit is passed to the SAME builder
        # health's keepalive relaunch uses, with only the placement overridden
        # (nodes/port/tp), so a resume from one node of a span re-issues the
        # whole declared workload. Swapping in the live recipe here instead would
        # change sparkrun's intent hash and launch a PARALLEL DUPLICATE of the
        # unit rather than re-issuing it (t_e1ff0e8e).
        resume_cmd = serving._unit_run_cmd(dict(declared, nodes=nodes,
                                                port=port, tp=tp))
    elif declared:
        resume_blocked = ("the serving.json unit declares no recipe — the "
                          "launch command cannot be reproduced")
    else:
        resume_blocked = ("no serving.json unit matches this workload — the "
                          "launch recipe and flags are not recorded, so it "
                          "cannot be resumed safely")
    return {
        "unit_id": unit_id,
        "cluster_id": live.get("cluster_id") or "",
        "kind": "orchestrator" if role == "orchestrator" else "worker",
        "recipe": recipe,
        "model": live.get("model") or declared_node.get("model") or "",
        "served_model_name": (live.get("served_model_name")
                              or declared_node.get("served_model_name") or ""),
        "nodes": nodes,
        "port": port,
        "tp": tp,
        "span": len(nodes) > 1,
        "observed": bool(live.get("observed_nodes")),
        "unverified_nodes": _hosts_of(
            [n for n in nodes if n not in _hosts_of(live.get("observed_nodes"))]),
        "serving_match": bool(declared),
        "stop_cmd": stop_cmd,
        "resume_cmd": resume_cmd,
        "resume_blocked": resume_blocked,
    }


def plan(host, serving_dict=None, status_reader=None):
    """Every unit on ``host``, span-aware, plus the orchestrator-model flag.

    ``serving_dict`` defaults to ``serving.load_serving()``; ``status_reader``
    defaults to :func:`fetch_cluster_status` and exists so tests (and any
    caller that already swept) can hand in a snapshot instead of shelling out.

    Returns::

        {"host", "ok", "error", "units", "span_units", "span_nodes",
         "serves_orchestrator_model", "orchestrator_evidence",
         "unreachable_hosts"}

    ``ok`` is False — with an ``error`` and NO units — when the node or the
    cluster could not be read. That is deliberate: an empty ``units`` from a
    failed read must never be readable as "this node runs nothing".
    """
    if serving_dict is None:
        serving_dict = serving.load_serving()
    data, error = (status_reader or fetch_cluster_status)()
    result = {
        "host": host,
        "ok": False,
        "error": error,
        "units": [],
        "span_units": [],
        "span_nodes": [],
        "span_siblings": [],
        "unreachable_hosts": [],
        "serves_orchestrator_model": False,
        "orchestrator_evidence": "",
    }
    if error:
        # Fail closed on the orchestrator flag too: no read means no proof.
        result["serves_orchestrator_model"], result["orchestrator_evidence"] = \
            orchestrator_flag(host, serving_dict)
        return result

    errors = dict((data or {}).get("errors") or {})
    unreachable = _hosts_of(sorted(errors.keys()))
    result["unreachable_hosts"] = unreachable
    if host in unreachable:
        result["error"] = (f"{host} did not answer the sparkrun status sweep "
                           f"({errors.get(host)}) — its units cannot be "
                           "enumerated")
        result["serves_orchestrator_model"], result["orchestrator_evidence"] = \
            orchestrator_flag(host, serving_dict)
        return result

    declared = _serving_units(serving_dict)
    entries = _live_entries(data)
    units = []
    for live in entries:
        if host not in _hosts_of(live.get("nodes")):
            continue
        match, why = _find_declared_unit(declared, live)
        record = _unit_record(live, match, host, serving_dict)
        if why:
            record["serving_match"] = False
            record["resume_blocked"] = (
                f"{why}; the launch recipe and flags are not recorded, so it "
                "cannot be resumed safely")
        units.append(record)

    # A declared unit on this host that the sweep did not show (still loading,
    # or already down) is still part of what a pause owns — enumerated, but
    # honestly marked as not observed so nothing downstream claims it stopped.
    seen = {u["unit_id"] for u in units}
    for u in declared:
        if host not in _hosts_of(u.get("nodes")):
            continue
        nodes = _hosts_of(u.get("nodes"))
        unit_id = u.get("id") or ",".join(nodes)
        if unit_id in seen:
            continue
        live_stub = {"cluster_id": "", "nodes": nodes,
                     "observed_nodes": [], "port": _as_int(u.get("port")),
                     "recipe": u.get("recipe") or "", "model": u.get("model") or "",
                     "served_model_name": "", "tp": _as_int(u.get("tp"))}
        record = _unit_record(live_stub, u, host, serving_dict)
        record["observed"] = False
        units.append(record)
        seen.add(unit_id)

    affected = _hosts_of([n for u in units for n in u["nodes"]])
    flag, evidence = orchestrator_flag(host, serving_dict, entries)
    result.update({
        "ok": True,
        "error": None,
        "units": units,
        "span_units": [u["unit_id"] for u in units if u["span"]],
        # EVERY node a stop of these units would touch, the target included —
        # "affected nodes", not "the other ones", so a caller can never print a
        # shorter list than the blast radius.
        "span_nodes": affected,
        "span_siblings": [n for n in affected if n != host],
        "serves_orchestrator_model": (any(u["kind"] == "orchestrator"
                                          for u in units) or flag),
        "orchestrator_evidence": evidence,
    })
    return result


# ── stop / resume ──────────────────────────────────────────────────────────
def _run_units(units, apply, key, verb, run, echo):
    """Shared dry-run/apply driver for stop_units and resume_units.

    ``key`` is the per-unit argv (``stop_cmd`` / ``resume_cmd``). Dry-run is the
    default and prints the command it WOULD run; ``apply=True`` executes it
    through ``run`` (default ``util.run_cmd``, which resolves the sparkrun
    argv). A unit with no command, or one whose node set is unknown, is refused
    and counted as a failure — never skipped silently, and never guessed.
    """
    results = []
    for unit in (units or []):
        unit_id = unit.get("unit_id") or unit.get("cluster_id") or "?"
        nodes = _hosts_of(unit.get("nodes"))
        cmd = list(unit.get(key) or [])
        if not cmd:
            why = unit.get("resume_blocked") or f"no {verb} command recorded"
            echo(f"REFUSED {verb} {unit_id}: {why}")
            results.append({"unit_id": unit_id, "cmd": None, "applied": bool(apply),
                            "ok": False, "skipped": True, "error": why})
            continue
        if apply:
            echo(f"{verb.upper()} {unit_id} on {', '.join(nodes)}: {' '.join(cmd)}")
            try:
                res = run(cmd, timeout=SPARKRUN_CONTROL_TIMEOUT)
            except Exception as e:  # noqa: BLE001 — a shell-out must not abort the batch
                res = {"ok": False, "output": "", "stderr": str(e)}
            ok = bool(res and res.get("ok"))
            results.append({"unit_id": unit_id, "cmd": cmd, "nodes": nodes,
                            "applied": True, "ok": ok,
                            "error": None if ok else (
                                (res or {}).get("stderr")
                                or (res or {}).get("output") or "command failed")})
        else:
            echo(f"WOULD {verb.upper()} {unit_id} on {', '.join(nodes)}: "
                 f"{' '.join(cmd)}")
            if not unit.get("observed", True):
                echo(f"  (not observed in the latest sweep — {verb} is a no-op "
                     "if it is not running)")
            results.append({"unit_id": unit_id, "cmd": cmd, "nodes": nodes,
                            "applied": False, "ok": True, "error": None})
    return {
        "applied": bool(apply),
        "ok": all(r["ok"] for r in results),
        "units": results,
    }


def stop_units(units, apply=False, run=None, echo=print):
    """Take ``units`` down. Dry-run by default (rule from #478).

    ``units`` are the records :func:`plan` returns (JSON-serialisable, so a
    pause record written by the CLI round-trips through the overlay). With
    ``apply=False`` nothing runs and every line is prefixed ``WOULD``; the
    printed line names EVERY node the unit spans, because stopping a span unit
    affects all of them. ``apply=True`` executes each unit's
    ``sparkrun stop`` through ``run`` (default ``util.run_cmd``).
    """
    return _run_units(units, apply, "stop_cmd", "stop",
                      run or util.run_cmd, echo)


def resume_units(units, apply=False, run=None, echo=print):
    """Restart the recorded units via sparkrun's ``--ensure`` path.

    Same recipe, same flags, same ``--cluster`` as recorded at pause time: the
    commands are ``serving._unit_run_cmd``'s — the exact argv the keepalive
    relaunch issues — so ``--ensure`` re-issues the declared workload instead of
    deriving a different intent id and launching a duplicate. Dry-run by
    default; ``apply=True`` executes. A unit whose recipe was never recorded is
    refused with the reason printed.
    """
    return _run_units(units, apply, "resume_cmd", "resume",
                      run or util.run_cmd, echo)
