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
import os
import re
import sys
from datetime import datetime

from . import node_pauses, serving, util

#: Shell-out cap for ONE lifecycle command (a stop). These are single-host
#: sparkrun control calls, not a fleet sweep, so they sit well under
#: health.SPARKRUN_STATUS_TIMEOUT (the sweep budget must stay above
#: its own worst case — t_614f4cac — and a control call must not inherit that).
SPARKRUN_CONTROL_TIMEOUT = 120

#: Bounded wait for ONE relaunch client (t_09708c17, DEFECT 3). Longer than a
#: control call because it covers a real launch, and deliberately well under
#: verify's 600 s stream-staleness floor: the expiry tick runs this in a
#: supervised stream thread and cannot hand the stream to a wedged client. The
#: wait usually ends far earlier than this — the moment the unit answers its own
#: /v1/models the client is cut loose and the restart is reported as verified by
#: the health probe (which is the ONLY honest success signal for a
#: ``sparkrun run`` client that hangs on its child ssh session after the
#: container is already serving: the operator's live launch sat 40 min on a
#: machine whose model answered in 10). The endpoint is re-asked every couple of
#: seconds (``util.run_cmd``'s poll cadence); a unit takes minutes to load, so a
#: faster poll would only buy requests.
SPARKRUN_LAUNCH_WAIT_TIMEOUT = 240

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


# ── who serves the orchestrator alias, fleet-wide ──────────────────────────
def _live_serves_alias(live, declared_match):
    """Whether one swept workload serves the orchestrator-model alias.

    Two independent proofs, either sufficient: the live workload advertises the
    alias token, or the serving.json unit it matched is declared
    ``role == "orchestrator"`` (a unit may serve the orchestrator under its
    concrete model name only, and it may advertise the alias without the config
    having been re-applied — the two sources corroborate each other, they do
    not gate each other).
    """
    if _ORCHESTRATOR_ALIAS in str(live.get("served_model_name") or "").split():
        return True
    return bool(declared_match) and declared_match.get("role") == "orchestrator"


def orchestrator_alias_units(serving_dict, live_entries):
    """Every LIVE unit serving the orchestrator alias, wherever it runs.

    This is the fleet-wide view the guard needs, and it is deliberately NOT the
    same question as :func:`orchestrator_flag` ("does THIS host serve it?").
    Under a ``uniform_pool`` template every replica advertises the alias
    (``serving._served_model_name`` stamps both aliases), so a per-unit flag
    cannot tell the orchestrator node from a interchangeable replica — that
    confusion is what made `hscc node pause` refuse every node in the fleet
    (#567, replaced by :func:`orchestrator_pause_guard`).

    Only swept workloads are listed: a declared unit the sweep did not show is
    not running, so it can only ever subtract from the healthy count, never
    add to it, and pretending otherwise would count a stopped server as a live
    one. ``observed`` is kept visible so a caller can see the difference
    between "launched across these nodes" and "the sweep saw containers here".
    """
    declared = _serving_units(serving_dict)
    out = []
    for live in (live_entries or []):
        match, _why = _find_declared_unit(declared, live)
        if not _live_serves_alias(live, match):
            continue
        nodes = _hosts_of(live.get("nodes"))
        out.append({
            "unit_id": (match or {}).get("id") or live.get("cluster_id") \
                or ",".join(nodes),
            "role": "orchestrator" if (match or {}).get("role") == "orchestrator" \
                else "alias",
            "nodes": nodes,
            "observed_nodes": _hosts_of(live.get("observed_nodes")),
            "observed": bool(live.get("observed_nodes")),
        })
    return out


# ── THE orchestrator guard (the only one) ──────────────────────────────────
#: The verdicts :func:`orchestrator_pause_guard` can reach, and the reason
#: strings behind a refusal. Both entry points print ``verdict`` + ``lines``
#: verbatim, so the wording is pinned here and nowhere else.
GUARD_ALLOW = "allow"
GUARD_REFUSE = "refuse"

REASON_DECLARED_NODE = "declared_orchestrator_node"
REASON_NO_HEALTHY_SERVER = "no_healthy_orchestrator_server"
REASON_UNREADABLE = "cannot_rule_out"

#: The verdicts :func:`pause_eligibility` can reach, and the reason strings
#: behind a refusal. Same vocabulary as the orchestrator guard above: both
#: entry points print ``verdict`` + ``lines`` verbatim and compose (eligibility
#: first, then the orchestrator guard), so neither the CLI nor the agent tool
#: may word a refusal of its own.
ELIG_ALLOW = "allow"
ELIG_REFUSE = "refuse"

REASON_ZERO_UNITS = "nothing_to_pause"
REASON_MULTI_NODE = "multi_node_unit"
REASON_PLAN_UNREADABLE = "plan_unreadable"


def _declared_orchestrator_node(applied_state=None):
    """The applied template's declared orchestrator node, or None.

    Read through the SANCTIONED reader — ``declaration_agreement.load_applied_state``
    — which is the one function that knows the record's path (``~/.hscc/applied_template.json``),
    its v1/v2 shape and the top-level ``orchestrator_node`` key (the same key
    doctor's ``template drift`` row and verify's ``--deployed`` read). This
    module does not open, parse or glob that file itself.

    Returns ``(node_or_None, note)``. An absent record is NOT an error: a fleet
    whose serving.json was hand-written (``hscc-bootstrap``) legitimately has no
    applied-template state, and treating that as "everything is the orchestrator
    node" is exactly the failure this guard replaces. It is reported in ``note``
    so the caller can say out loud that the declaration could not be consulted.
    """
    state, err = (applied_state if applied_state is not None
                  else _load_applied_state())
    if err or not isinstance(state, dict):
        return None, ("the applied-template record could not be consulted (%s)"
                      " — the declared orchestrator node was not checked"
                      % (err,))
    node = state.get("orchestrator_node")
    node = node.strip() if isinstance(node, str) else None
    if not node:
        return None, "the applied template declares no orchestrator_node"
    template = state.get("template")
    where = ("applied template %r declares orchestrator_node=%s"
             % (template, node) if template else
             "the applied record declares orchestrator_node=%s" % node)
    return node, where


def _load_applied_state():
    """Call the sanctioned applied-state reader (lazy: it pulls health/proxy_router)."""
    try:
        from . import declaration_agreement as _da
    except Exception as exc:  # pragma: no cover - import-time only
        return None, "declaration_agreement unavailable (%s)" % (exc,)
    try:
        return _da.load_applied_state()
    except Exception as exc:  # pragma: no cover - defensive
        return None, "applied-template record unreadable (%s)" % (exc,)


def orchestrator_pause_guard(host, plan=None, serving_dict=None,
                             status_reader=None, paused=None, force=False,
                             applied_state=None):
    """THE orchestrator-model pause guard. One function, both entry points.

    It answers one question — *if this host (and its span) goes down, does
    anything still serve the orchestrator alias?* — and refuses on exactly two
    grounds:

    ``declared_orchestrator_node``
        the host is the applied template's declared ``orchestrator_node``.
        That node is pinned by the fleet's own template; the other replicas are
        interchangeable behind the proxy and it is not.
    ``no_healthy_orchestrator_server``
        every healthy unit serving the alias is on a node this stop takes down,
        so pausing would leave ZERO. A replica the pause overlay is holding does
        NOT count as healthy (it is deliberately parked, and would come back via
        the resume path, not by being ignored here), and neither does a unit the
        sweep did not observe running.

    Anything else is an ALLOW, with a note naming how many other nodes keep
    serving the alias. The host merely *advertising* the alias is not a refusal:
    under a ``uniform_pool`` template every replica advertises it, so a
    per-unit flag would refuse the whole fleet and make the feature's first real
    use require ``--force`` — which defeats the guard (#567, the defect this
    replaces).

    ``--force`` overrides either refusal; the override is announced in the same
    ``note: --force given, …`` shape the verb has always used.

    Fail-closed: an unreadable fleet read or serving config yields a REFUSE
    (``cannot_rule_out``), never an allow — the same posture as
    :func:`orchestrator_flag`, and the reason says it could not rule the host
    out rather than claiming it is the orchestrator.

    Args:
        host: the host about to be paused.
        plan: the caller's :func:`plan` result (re-used, so one pause never
            sweeps the fleet twice). Computed when not given.
        paused: node set held by the pause overlay. Default: one read of the
            overlay, so the caller cannot get a stale answer by reading it
            earlier.
        applied_state: injectable ``(dict_or_None, err_or_None)`` for the
            applied-template record, for tests.

    Returns a dict: ``verdict``, ``reason``, ``forced``, ``lines`` (the exact
    lines to print — the callers print them, they do not compose their own),
    ``others`` (nodes still serving the alias), ``healthy_units`` /
    ``remaining_units``, plus ``declared_node`` / ``note`` for the record.
    """
    host = (host or "").strip()
    if plan is None:
        # Only this path reads anything: with a plan handed in, the guard works
        # from the sweep and the config verdict the caller already has, so one
        # pause never sweeps the fleet (or re-reads serving.json) twice.
        if serving_dict is None:
            serving_dict = serving.load_serving()
        plan = _plan_enumerate(host, serving_dict=serving_dict,
                               status_reader=status_reader)
    if paused is None:
        paused = node_pauses.paused_nodes()
    paused = {str(n).strip() for n in (paused or set()) if str(n).strip()}

    out = {"host": host, "verdict": GUARD_ALLOW, "reason": None, "forced": False,
           "lines": [], "others": [], "healthy_units": 0, "remaining_units": 0,
           "declared_node": None, "note": None}

    def _refuse(reason, why, headline):
        out["verdict"] = GUARD_REFUSE
        out["reason"] = reason
        out["lines"] = [
            "REFUSED: %s %s" % (host, headline),
            "  why: %s" % why,
            "  Pausing it parks the model every agent in this fleet talks to.",
            "  Pass --force to pause it anyway.",
            "Nothing stopped and nothing recorded.",
        ]

    # ── cannot rule it out → refuse (fail-closed, like orchestrator_flag) ──
    if not plan.get("ok"):
        _refuse(REASON_UNREADABLE,
                plan.get("error") or "the fleet status read failed",
                "serves the orchestrator model.")
        out["lines"][1] = ("  why: %s — pausing now could take the only "
                           "orchestrator server down, and the fleet could not be "
                           "read to rule that out" % (plan.get("error"),))
        return out
    if not plan.get("serving_config_ok", serving_dict is not None):
        # The plan already answered this fail-closed (its flag is True from
        # "the config was unreadable"); the guard repeats the verdict rather than
        # re-reading the config and possibly disagreeing with the plan above it.
        _refuse(REASON_UNREADABLE, plan.get("orchestrator_evidence")
                or "serving config unavailable", "serves the orchestrator model.")
        return out

    # ── 1. the host is the template's declared orchestrator node ───────────
    notes = []
    declared, note = _declared_orchestrator_node(applied_state)
    out["declared_node"] = declared
    out["note"] = None if declared else note
    if declared and host == declared:
        if not force:
            _refuse(REASON_DECLARED_NODE,
                    "%s; this stop takes down %d unit(s) on it (span: %s)"
                    % (note, len(plan.get("units") or []),
                       ", ".join(plan.get("span_nodes") or [host])),
                    "is the applied template's declared orchestrator node.")
            return out
        out["forced"] = True
        notes.append("note: --force given, but this host is the applied "
                     "template's declared orchestrator node.")

    # ── 2. would this leave no healthy server of the alias? ────────────────
    affected = {str(n).strip() for n in (plan.get("span_nodes") or [])
                if str(n).strip()}
    affected.add(host)
    alias_units = plan.get("orchestrator_alias_units")
    if alias_units is None:
        # Only a caller that built the plan dict by hand lands here; the normal
        # path got the fleet-wide view out of the one sweep the plan made.
        if serving_dict is None:
            serving_dict = serving.load_serving()
        alias_units = orchestrator_alias_units(
            serving_dict, _live_entries((status_reader or
                                         fetch_cluster_status)()))
    healthy = [u for u in alias_units
               if u.get("observed")
               and not (set(u.get("nodes") or []) & paused)]
    taken = [u for u in healthy
             if set(u.get("nodes") or []) & affected]
    remaining = [u for u in healthy if u not in taken]
    others = sorted({n for u in remaining for n in (u.get("observed_nodes")
                                                    or u.get("nodes") or [])}
                    - affected)
    # Nodes that hold the alias but are being held by the pause overlay: named in
    # the refusal so "why is this refused when the replica is right there" is
    # answerable from the terminal, not from the overlay file.
    parked = sorted({n for u in alias_units for n in (u.get("nodes") or [])}
                    & paused)
    out["healthy_units"] = len(healthy)
    out["remaining_units"] = len(remaining)
    out["others"] = others

    if healthy and taken and not remaining:
        why = ("orchestrator-model served by %d healthy unit(s) — %s — and all "
               "of them are on node(s) this stop takes down. Pausing %s%s would "
               "leave no healthy unit serving %s%s"
               % (len(taken),
                  ", ".join(sorted(u["unit_id"] for u in taken)),
                  host,
                  ("" if len(affected) <= 1
                   else " (and its span %s)"
                        % ", ".join(sorted(affected - {host}))),
                  _ORCHESTRATOR_ALIAS,
                  ("; paused and therefore not counted as a healthy server: %s"
                   % ", ".join(parked)) if parked else ""))
        if not force:
            _refuse(REASON_NO_HEALTHY_SERVER, why,
                    "serves the orchestrator model.")
            return out
        out["forced"] = True
        notes.append("note: --force given, but pausing %s would leave no "
                     "healthy unit serving %s." % (host, _ORCHESTRATOR_ALIAS))

    # ── 3. allowed ─────────────────────────────────────────────────────────
    if taken and remaining:
        # The host does serve the alias and something else still will — say so,
        # so an ALLOW never reads like an all-clear about the orchestrator.
        notes.append("note: %s stays served by %d other node(s): %s"
                     % (_ORCHESTRATOR_ALIAS, len(others), ", ".join(others)))
    elif not taken and force:
        notes.append("note: --force given, but this host is not flagged as "
                     "serving the orchestrator model.")
    if out["note"] and taken:
        # Only worth saying when this host actually serves the alias: a pause of
        # a node that serves nothing must not print a warning about a record the
        # answer never needed (bootstrap fleets hand-write serving.json and have
        # no applied record at all — that is normal, not a finding).
        notes.append("note: %s" % out["note"])
    out["lines"] = notes
    return out


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
        # The resume command: recipe, --cluster, --hosts, --port, --no-follow,
        # --ensure, --served-model-name, --tp and the recorded -o overrides.
        # The declared unit goes to the SAME builder ``hscc cluster up`` uses,
        # with only the placement overridden (nodes/port/tp), so a resume from
        # one node of a span re-issues the whole declared workload. Swapping in
        # the live recipe here instead would change sparkrun's intent hash and
        # launch a PARALLEL DUPLICATE of the unit rather than re-issuing it
        # (t_e1ff0e8e).
        #
        # ``prefer_serve_cmd`` is the resume half of the operator's 11-22 ruling
        # (t_09708c17): a resume must reproduce the EXACT argv the unit was
        # launched with, so the recorded serve_cmd wins when the unit carries
        # one. Without it the builder derives the served names from this unit's
        # own fields, and a uniform_pool unit's fields cannot rebuild what the
        # apply path advertised — the reported resume came back with two names
        # where the running unit served three, which is a different intent
        # hash, so ``--ensure`` started a DIFFERENT workload and the node left
        # the orchestrator pool. A record whose placement (recipe/hosts/port)
        # disagrees is refused inside the builder and the derived argv is used.
        resume_cmd = serving._unit_run_cmd(dict(declared, nodes=nodes,
                                                port=port, tp=tp),
                                           prefer_serve_cmd=True)
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
        # Whether the config the flag was computed from was actually readable.
        # The guard reads THIS instead of loading serving.json a second time: one
        # pause, one read of each source, and the guard can never answer from a
        # config that changed between the two reads.
        "serving_config_ok": isinstance(serving_dict, dict),
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
        # ``observed_nodes`` empty is what makes the record report
        # ``observed: False`` — one source for that fact, not a flag on top.
        units.append(_unit_record(live_stub, u, host, serving_dict))
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
        # Fleet-wide, not host-scoped: the guard needs who ELSE serves the
        # alias, and it must get that from THIS sweep rather than shell out a
        # second time (one pause = one sparkrun status read, and the two reads
        # could otherwise disagree about a unit that changed state between them).
        "orchestrator_alias_units": orchestrator_alias_units(serving_dict,
                                                             entries),
    })
    return result


#: :func:`orchestrator_pause_guard` takes the caller's plan as a ``plan=``
#: parameter, which shadows the enumerator inside its own body — so the
#: "caller handed me no plan" fallback path calls it through this name.
_plan_enumerate = plan


# ── THE eligibility rule (the only one) ────────────────────────────────────
#: OPERATOR RULE (11-22, supersedes the ``--span`` semantics of #561/#567):
#: a node may be paused only when EVERY model on it runs on that node ALONE.
#: Several single-node models colocated on one machine are FINE — a pause stops
#: all of them together, which is the point of parking a machine. What is never
#: paused is a model that lives on more than one node: stopping it here stops it
#: over there too, on a node nobody released. So the rule is about SPAN, not
#: about count, and there is no opt-in for it any more — ``--span`` is gone and
#: no flag (not ``--force``, which belongs to the orchestrator guard alone)
#: overrides this refusal.
def _multi_node_units(units):
    """The units of a plan that are NOT confined to one node.

    Two proofs of "not alone", either one enough: the unit's node set holds more
    than one node (``plan`` already merges the observed placement with the
    serving.json declaration, so a sibling that did not answer the sweep still
    shows up), or it declares ``tp`` > 1 — a model split across more than one
    process is not a single-node model, whatever a partial sweep reports.
    """
    out = []
    for unit in (units or []):
        nodes = _hosts_of(unit.get("nodes"))
        tp = _as_int(unit.get("tp"))
        if len(nodes) > 1 or (tp is not None and tp > 1):
            out.append(unit)
    return out


def pause_eligibility(host, plan=None, serving_dict=None, status_reader=None):
    """THE node-pause eligibility rule. One function, both entry points.

    It answers one question — *does every model on this node live on this node
    alone?* — and refuses on three grounds:

    ``plan_unreadable``
        the fleet or the node could not be read. Fail-closed, like the
        orchestrator guard: an unreadable node is not a verified-empty node, and
        "I could not enumerate it" must never become "it runs nothing".
    ``nothing_to_pause``
        the node runs zero units. There is nothing to stop, so a pause would
        only be a record of an idle machine — which is what the heal paths
        already do nothing about. (The operator's rule; the pre-11-22 verb
        allowed it.)
    ``multi_node_unit``
        ANY unit spans other nodes (or declares ``tp`` > 1). The refusal names
        that unit and its other nodes — the operator has to see the whole blast
        radius from the refusal alone.

    Everything else is an ALLOW, including several colocated single-node units:
    they are stopped together, which is what parking a machine means.

    ``--force`` deliberately is NOT a parameter here. It belongs to
    :func:`orchestrator_pause_guard` and to nothing else; a caller that passed
    it through would turn "this model is shared with another node" into a
    warning, which is exactly the semantics the operator retired.

    Args:
        host: the host about to be paused.
        plan: the caller's :func:`plan` result, re-used so one pause never
            sweeps the fleet twice. Computed when not given.
        serving_dict / status_reader: only used on the "no plan handed in" path.

    Returns a dict: ``verdict``, ``reason``, ``lines`` (the exact lines to
    print — callers print them, they do not compose their own), ``units`` (every
    unit id the plan found), ``multi_node`` (the offending unit ids) and
    ``peers`` (the other nodes a stop would reach).
    """
    host = (host or "").strip()
    if plan is None:
        # Only this path reads anything; with a plan handed in the check works
        # from the sweep the caller already has.
        if serving_dict is None:
            serving_dict = serving.load_serving()
        plan = _plan_enumerate(host, serving_dict=serving_dict,
                               status_reader=status_reader)

    units = list(plan.get("units") or [])
    ids = [u.get("unit_id") or u.get("cluster_id") or "?" for u in units]
    offenders = _multi_node_units(units)
    peers = sorted({n for u in offenders for n in _hosts_of(u.get("nodes"))}
                   - {host})
    out = {"host": host, "verdict": ELIG_ALLOW, "reason": None, "lines": [],
           "units": ids, "multi_node": [u.get("unit_id") or "?"
                                        for u in offenders],
           "peers": peers}

    def _refuse(reason, lines):
        out["verdict"] = ELIG_REFUSE
        out["reason"] = reason
        out["lines"] = lines + ["Nothing stopped and nothing recorded."]

    if not plan.get("ok"):
        _refuse(REASON_PLAN_UNREADABLE, [
            f"REFUSED: {host}: its units could not be enumerated — a pause must "
            f"know what it is stopping.",
            f"  why: {plan.get('error') or 'the fleet status read failed'}",
        ])
        return out
    if not units:
        _refuse(REASON_ZERO_UNITS, [
            f"REFUSED: {host} runs nothing — nothing to pause.",
        ])
        return out
    if offenders:
        lines = [f"REFUSED: {host} runs a multi-node unit — a node pause stops "
                 f"only units that run on this node alone."]
        for unit in offenders:
            nodes = _hosts_of(unit.get("nodes"))
            tp = _as_int(unit.get("tp"))
            others = [n for n in nodes if n != host]
            if others:
                lines.append(f"  {unit.get('unit_id')}: spans "
                             f"{', '.join(nodes)} (other node(s): "
                             f"{', '.join(others)})")
            else:
                # tp > 1 reported on one node: the declaration and the sweep
                # disagree, and the declaration is the safer reading.
                lines.append(f"  {unit.get('unit_id')}: declares tp={tp} on "
                             f"{', '.join(nodes)} — a model split across "
                             f"processes is not a single-node model")
        if peers:
            lines.append(f"  Stopping it here also stops it on: "
                         f"{', '.join(peers)}")
        lines.append("  No flag overrides this: park the node when its models "
                     "are all single-node, or stop the shared model on every "
                     "node it lives on.")
        _refuse(REASON_MULTI_NODE, lines)
    return out



# ── stop / resume ──────────────────────────────────────────────────────────
def unit_is_serving(node, port, timeout=5):
    """True when this unit's OWN endpoint answers ``/v1/models`` (t_09708c17).

    The health probe, not the client, is what proves a relaunch worked: a
    ``sparkrun run`` client can wedge on its child ssh session while the
    container it launched is already serving. Asking the unit itself is also the
    only check here that does not go through sparkrun at all, so a wedged
    sparkrun cannot make it lie in either direction.

    Deliberately a plain HTTP GET of the unit's own port — NOT the proxy and NOT
    an alias comparison: the requirement is "container up and answering", and
    alias coverage is ``declaration_agreement``'s leg 5's job. Unreachable,
    refused, or a non-200 answer is simply False ("not up yet"), never an
    exception — the caller polls this until its bound expires, and a probe that
    could raise would turn a slow node into an aborted launch.
    """
    try:
        res = util.http_check("http://%s:%s/v1/models" % (node, port),
                              timeout=timeout)
    except Exception:  # noqa: BLE001 — "cannot ask" IS the answer "not up"
        return False
    return bool(res.get("ok")) and res.get("status") == 200


def _ready_probe_for(is_serving):
    """The ``ready`` callable factory :func:`_run_units` asks per unit.

    Returns ``None`` for a unit it cannot ask about (no nodes, no port) — the
    caller then takes the plain bounded wait, exactly the shape every other
    lifecycle command uses. A span unit is ready only when EVERY node it spans
    answers: declaring a restart verified while a node of the same model is down
    is a lie the resume must not tell.
    """
    def ready_for(unit):
        nodes = _hosts_of(unit.get("nodes"))
        port = unit.get("port")
        if not nodes or port in (None, ""):
            return None

        def ready():
            return all(is_serving(str(node), port) for node in nodes)
        return ready
    return ready_for


def _run_units(units, apply, key, verb, run, echo, ready_for=None):
    """Shared dry-run/apply driver for stop_units and resume_units.

    ``key`` is the per-unit argv (``stop_cmd`` / ``resume_cmd``). Dry-run is the
    default and prints the command it WOULD run; ``apply=True`` executes it
    through ``run`` (default ``util.run_cmd``, which resolves the sparkrun
    argv). A unit with no command, or one whose node set is unknown, is refused
    and counted as a failure — never skipped silently, and never guessed.

    ``ready_for`` (resume only, t_09708c17) turns the launch into a BOUNDED wait
    that ends as soon as the WORKLOAD is up instead of waiting for the CLIENT to
    come back: given a unit it returns the zero-arg probe (or None), the probe
    rides into ``run`` as ``ready=``, and a result that had to cut the client
    loose is reported with ``detached`` plus a printed line saying plainly that
    the health probe — not the client — proved the restart. Never a silent kill
    and never a false "relaunch failed".
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
            ready = ready_for(unit) if ready_for else None
            bound = (SPARKRUN_LAUNCH_WAIT_TIMEOUT if ready is not None
                     else SPARKRUN_CONTROL_TIMEOUT)
            echo(f"{verb.upper()} {unit_id} on {', '.join(nodes)}: {' '.join(cmd)}")
            try:
                res = run(cmd, timeout=bound, ready=ready)
            except Exception as e:  # noqa: BLE001 — a shell-out must not abort the batch
                res = {"ok": False, "output": "", "stderr": str(e)}
            ok = bool(res and res.get("ok"))
            detached = bool(res and res.get("detached"))
            verified_by = (res or {}).get("verified_by")
            if detached:
                # The operator's rule for this shape: say what was cut loose and
                # what actually proved the outcome. A launch we had to detach and
                # could NOT verify is still a failure — and says so, without ever
                # pretending the kill itself was the verdict.
                if ok:
                    echo(f"  the launch client did not exit within {bound}s and "
                         f"was cut loose; the restart is verified by the "
                         f"{verified_by or 'health probe'} ({', '.join(nodes)} "
                         f"answering /v1/models), not by the client")
                else:
                    echo(f"  the launch client did not exit within {bound}s and "
                         f"was cut loose; the unit never answered the health "
                         f"probe either, so this restart is NOT confirmed")
            results.append({"unit_id": unit_id, "cmd": cmd, "nodes": nodes,
                            "applied": True, "ok": ok,
                            "detached": detached, "verified_by": verified_by,
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
                            "applied": False, "ok": True, "error": None,
                            "detached": False, "verified_by": None})
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


def resume_units(units, apply=False, run=None, echo=print,
                 serving_probe=None):
    """Restart the recorded units via sparkrun's ``--ensure`` path.

    Same recipe, same flags, same ``--cluster`` as recorded at pause time: the
    commands are ``serving._unit_run_cmd``'s built with ``prefer_serve_cmd`` —
    the recorded launch argv itself when the unit carries one, so a resume
    reproduces EXACTLY what was running (t_09708c17: the field rebuild dropped a
    uniform_pool unit's third served name and ``--ensure`` started a different
    workload). Dry-run by default; ``apply=True`` executes. A unit whose recipe
    was never recorded is refused with the reason printed.

    The wait is BOUNDED and the success signal is the workload, not the client
    (t_09708c17 DEFECT 3): ``sparkrun run`` can sit on its child ssh session for
    tens of minutes after the container it started is serving, and a resume that
    waited for that client would report a working restart as a failure and keep
    the node parked. Each launch is therefore cut loose as soon as the unit
    answers its own ``/v1/models`` (``SPARKRUN_LAUNCH_WAIT_TIMEOUT`` is the hard
    backstop), and the printed line says plainly that the health probe verified
    it and the client was detached — never a silent kill, and never a false
    "relaunch failed" for a unit that came up.

    ``serving_probe`` is that check, injectable for tests: ``(node, port) ->
    bool``. The default is :func:`unit_is_serving`.
    """
    return _run_units(units, apply, "resume_cmd", "resume",
                      run or util.run_cmd, echo,
                      ready_for=_ready_probe_for(serving_probe
                                                 or unit_is_serving))


# ── the resume policy (ONE implementation for the CLI and the daemon tick) ──
#
# WHY THE TICK CALLS A FUNCTION INSTEAD OF THE CLI VERB
# -----------------------------------------------------
# ``hscc-cluster/node_pause_tools.py`` reuses ``hscc node resume`` by calling it
# in-process under ``contextlib.redirect_stdout`` and reporting the bytes — the
# parent card's deliberate decision ("no second policy, no agent-only path around
# dry-run"), and correct for a single-shot agent turn. A daemon stream thread
# cannot copy that trick: ``redirect_stdout`` rebinds a PROCESS GLOBAL, so for
# the window of one resume it would steal the prints of the ~14 other supervised
# stream threads into this tick's buffer — and the tick's own lines would then
# land wherever the next thread's print went. A tick needs a return value it can
# log and stamp, not text it has to re-parse.
#
# So the policy lives HERE and both callers pass an ``echo``: the CLI's is
# ``print`` (its bytes, order and exit codes are exactly what they were — the
# verb is now a thin parser + this function), the tick's is the daemon log. One
# copy of the refuse-and-keep rules, two surfaces, and no print-capture racing
# other threads.

#: ``action`` values :func:`resume_host` returns. Three, deliberately: a tick
#: that cannot tell "nothing was due" from "the restart failed" cannot stamp a
#: truthful stream.
RESUME_RESUMED = "resumed"
RESUME_KEPT = "kept"
RESUME_DRY_RUN = "dry_run"


def resume_host(host, entry, apply=False, directory=None, echo=print,
                reason=None, run=None, pauses=None, now=None, confirm=None,
                serving_probe=None):
    """Restart the units ``entry`` recorded, then end the pause.

    This IS the resume policy — ``hscc node resume`` and the daemon's
    expiry tick both run it, so the confirmation gate, the rebuild-or-refuse
    rule and the clear pairing cannot drift apart between an operator and the
    daemon.

    ``entry`` comes from the CALLER's own :func:`node_pauses.list_pauses`
    snapshot and is never re-read here: ``node_pauses.load()`` normalises into
    fresh objects on every call, so an entry read twice is not the same object,
    and a decision taken across two reads can disagree with the one that picked
    this host. The caller that selected an expired entry passes what it saw.
    ``pauses`` / ``now`` are that same snapshot and clock, for the span question
    below; a caller that has none (the CLI) lets this function take one read of its
    own, so the guard is never optional — it cannot be skipped by forgetting an
    argument.

    ``apply=False`` (the default) prints every command and mutates nothing —
    including the overlay, so a preview cannot end a pause.

    ``confirm`` is the CLI's mutation gate (operator rule 11-22, which replaced
    ``--apply``): a callable ``(host, recorded_unit_ids) -> bool`` asked after
    the listing below and before anything is rebuilt, restarted or cleared, so
    a declined answer has cost the fleet nothing — not even a status sweep. The
    daemon's expiry tick passes NOTHING, because a tick has no operator to ask:
    it is the scheduled half of the same policy, not an unattended override.
    Wording of the question and of the abort belongs to the caller; this
    function only stops.

    The order is the contract:

      1. rebuild the launch argv. The overlay stores unit IDS, not commands
         (``node_pauses._normalize_entry`` flattens ``units`` to strings), so
         :func:`plan` re-derives them from ``serving.json`` through the same
         builder the keepalive relaunch uses.
      2. restart via :func:`resume_units` — sparkrun's ``--ensure`` path, never a
         hand-composed ``sparkrun run`` (a crafted argv derives a different
         intent id and launches a PARALLEL DUPLICATE of a live unit).
      3. ONLY after a clean restart: clear the pause, then that node's heal
         strikes (``clear`` + ``reset_alert``, the same pairing
         ``hscc heal-clear`` does — clearing the ledger without resetting the
         stamp silences the node forever).

    Every failure path KEEPS the pause and prints why: the record is what makes
    the heal, keepalive, ``cluster up``, autoscale and autodown paths skip this
    node, so losing it because a resume was *attempted* would walk units back onto
    a half-started machine. A kept pause is simply retried by the next call.

    Returns ``{"host", "ok", "action", "error", "pause_cleared",
    "strikes_cleared", "restarts", "detached"}``::

        RESUME_DRY_RUN  ok=True   would restart; nothing changed
        RESUME_RESUMED  ok=True   restart clean; pause ended, strikes cleared
        RESUME_KEPT     ok=False  refused or failed; pause still on disk

    ``ok`` is "this call would exit 0" — the CLI maps it straight to its exit
    code, the tick logs a WARN when it is False. ``error`` is a one-liner for a
    log row; the human-readable detail is already in ``echo``'s output.
    ``detached`` names the units whose launch CLIENT never came back and had to
    be cut loose — a RESUME_RESUMED with a non-empty ``detached`` is a restart
    the health probe proved rather than the client (t_09708c17).
    """
    from . import heal_breaker, node_pauses

    host = str(host or "").strip()
    recorded = list((entry or {}).get("units") or [])
    span_nodes = list((entry or {}).get("span_nodes") or [])
    res = {
        "host": host,
        "ok": False,
        "action": RESUME_KEPT,
        "error": None,
        "pause_cleared": False,
        "strikes_cleared": False,
        "restarts": None,
        # unit_ids whose launch client had to be cut loose (t_09708c17) — empty
        # unless a launch actually ran and had to be detached.
        "detached": [],
    }

    echo(f"{host}: pause recorded {len(recorded)} unit(s)"
         f"{' — dry run, nothing restarted' if not apply else ''}")
    for uid in recorded:
        echo(f"  {uid}")
    if span_nodes:
        echo(f"  every node this affects: {', '.join(span_nodes)}")

    # Rule 0.5 — a span resumes as ONE thing, or not at all.
    #
    # Resuming ``A`` while another pause holds ``B``, which A's span covers, is not
    # a partial resume: ``resume_cmd`` re-issues the WHOLE declared workload (that
    # is what a tensor-parallel unit is), so the restart would bring units up on a
    # node the operator has not released. A verb user can see the fleet and is
    # naming one host, so refusing is the cheap direction to err in — and a tick
    # iterating the overlay has neither the sight nor the intent, which is exactly
    # why the rule sits in the shared policy instead of in the argument parser
    # where only one of the two callers would ever meet it.
    #
    # "Still held" means not merely recorded: an entry whose ``until`` has passed is
    # due to end, so it cannot also be a reason to hold — otherwise one past-due
    # record would freeze every span it touches, including the one that could get
    # the fleet moving again.
    if pauses is None:
        pauses = node_pauses.list_pauses(directory)
    held = []
    for node in span_nodes:
        if node == host:
            continue
        for other_host, other in pauses.items():
            if other_host == host or node_pauses.is_expired(other, now):
                continue
            if other_host == node or node in (other.get("span_nodes") or []):
                held.append((node, other_host))
                break
    if held:
        owners = sorted({owner for _node, owner in held})
        res["error"] = (
            f"this pause spans {', '.join(sorted(node for node, _o in held))}, "
            f"which {'another pause' if len(owners) == 1 else 'other pauses'} "
            f"still hold{'s' if len(owners) == 1 else ''}")
        echo(f"REFUSED: {res['error']}.")
        echo("  The recorded restart re-issues the whole declared workload, so "
             "resuming here would start units on a node that has not been "
             "released.")
        echo(f"  Resume {' '.join(owners)} first "
             f"(hscc node resume {' '.join(owners)}); the pause on "
             f"{host} stays recorded.")
        return res

    # The operator's answer, when this caller asks for one (see the docstring:
    # the tick does not). It sits after the listing above because an operator
    # confirms against what they can READ, and before the plan() below because
    # a declined answer should not have swept the fleet either.
    if apply and confirm is not None and not confirm(host, recorded):
        res["error"] = "confirmation not given — nothing was attempted"
        return res

    if recorded:
        p = plan(host)
        if not p.get("ok"):
            res["error"] = (f"cannot rebuild the launch commands for {host}: "
                            f"{p.get('error')}")
            echo(f"error: {res['error']}")
            echo("The pause stays recorded; nothing was restarted.")
            return res
        by_id = {}
        for unit in p["units"]:
            by_id.setdefault(unit["unit_id"], unit)
        missing = [uid for uid in recorded if uid not in by_id]
        if missing:
            res["error"] = ("no launch command can be rebuilt for "
                            f"{', '.join(missing)}")
            echo(f"REFUSED: {res['error']}.")
            echo("  Nothing is known about how those units were started, so "
                 "resuming them would guess a command.")
            echo(f"  The pause on {host} stays recorded. Start the unit by "
                 f"hand (hscc cluster up / hscc template apply) and then "
                 f"clear the pause.")
            return res
        units = [by_id[uid] for uid in recorded]
        out = resume_units(units, apply=apply, run=run, echo=echo,
                           serving_probe=serving_probe)
        res["restarts"] = out["units"]
        # Which launches had to be cut loose because their CLIENT never returned
        # (t_09708c17). Reaching the clear below means every one of them was then
        # verified by the health probe, so this is a "how it was proved" note for
        # the log and the operator line, never a failure marker.
        res["detached"] = [r["unit_id"] for r in out["units"]
                           if r.get("detached")]
        if not apply:
            res["action"] = RESUME_DRY_RUN
            res["ok"] = True
            echo("Dry run: nothing restarted. Re-run without --dry-run to "
                 "be asked whether to restart them.")
            return res
        if not out["ok"]:
            res["error"] = f"at least one unit failed to restart on {host}"
            echo(f"error: {res['error']}.")
            echo("  The pause stays recorded — the heal paths must not "
                 "resume this node while its units are half up.")
            return res
    elif not apply:
        res["action"] = RESUME_DRY_RUN
        res["ok"] = True
        echo("  the pause recorded no units, so there is nothing to restart")
        echo("Dry run: nothing changed. Re-run without --dry-run to be "
             "asked whether to clear it.")
        return res

    # Restarted (or nothing to restart): end the pause, THEN the strikes — the
    # order is what makes a crash between the two safe: a cleared node with old
    # strikes is a node that heals normally, a cleared strike with the pause
    # still on disk is a node the heal paths would still skip.
    try:
        node_pauses.clear(host, directory=directory)
    except OSError as e:
        # The units are up and the overlay still says the node is parked: every
        # repair path will keep skipping a machine that is back in service. The
        # pause really is still there, so this is a kept result, not a resumed
        # one — say so instead of printing a clear nobody can read.
        res["error"] = (f"the units were restarted but the pause did not "
                        f"reach disk ({e})")
        echo(f"error: {res['error']}.")
        echo(f"  {node_pauses.overlay_path(directory)} still holds "
             f"{host} — clear it with 'hscc node resume {host}' "
             f"once the overlay is writable again.")
        return res
    res["pause_cleared"] = True
    echo(f"pause for {host} cleared.")
    if res["detached"]:
        # The operator's rule (t_09708c17): when a client had to be cut loose,
        # say so plainly and name what actually proved the restart. Silent here
        # would leave a log that reads like the client returned, which is how a
        # wedged sparkrun client gets mistaken for a broken node.
        echo(f"  note: the launch client(s) for {', '.join(res['detached'])} "
             f"never returned and were cut loose — the restart was verified by "
             f"the health probe (the unit answering /v1/models), not by the "
             f"client.")
    if heal_breaker.clear(
            host, reason=reason or f"hscc node resume {host}"):
        heal_breaker.reset_alert(host)
        res["strikes_cleared"] = True
        echo(f"Cleared heal strikes for {host} — healing resumes now.")
    else:
        echo(f"No strikes recorded for {host} (nothing to clear).")
    res["action"] = RESUME_RESUMED
    res["ok"] = True
    return res


# ── the daemon's expiry tick: a pause with an `until` is a SCHEDULE ──────────
#
# WHY THIS RUNS IN THE DAEMON
# ---------------------------
# ``hscc node pause --until 2h`` is a promise about the future, and a promise
# only a long-lived process can keep. The two alternatives both fail: an
# operator typing ``hscc node resume`` is a timer that goes on holiday, and an
# in-memory timer (a ``threading.Timer``, a deadline held in a dict) dies with
# the daemon — which is the worst possible moment, because a crash or an upgrade
# cycles the daemon and the watchdog then "heals" the paused unit back to life on
# top of whatever the node is now doing. So the schedule is the overlay file and
# the clock is read once per tick: ``until`` comes off disk, the decision is
# recomputed from scratch, and nothing has to survive a restart because nothing
# was ever held in memory. (``test_node_pause_expiry`` pins the consequence: this
# module imports neither ``threading`` nor ``sched`` and never sleeps, so an
# in-memory timer is not merely avoided here, it is not expressible.)
#
# THE IDLE PATH IS FREE
# ---------------------
# No pauses is the normal state, so it must not cost a sparkrun call, a serving
# read or even a file read: ``list_pauses`` short-circuits on ``os.path.exists``
# and the pass returns before touching ``plan``/``resume_units``/``serving``.
# Only the stream stamp is written, which every stream in this loop writes on
# every path — a deliberately idle stream must not read as a dead one to
# ``hscc verify``'s staleness math (GAP-C-1's rule: inert-but-healthy-looking is
# the worst read a watcher can have).

#: The daemon's stream name: the key in ``daemon_ops.PERIODIC_INTERVALS``, the
#: state file ``~/.hscc/state/node_pause_expiry.json``, and the row
#: ``hscc verify`` reports. One name for all three, like every other stream.
STREAM_NAME = "node_pause_expiry"

#: Pauses resumed per tick. Each resume is one sparkrun launch with a
#: ``SPARKRUN_LAUNCH_WAIT_TIMEOUT`` (240 s) budget, run sequentially in THIS
#: stream thread, so an unbounded pass on a large backlog could gate the stream
#: for many minutes and let verify's 600 s floor cry wolf on a healthy daemon.
#: Four per tick (the merge queue's one-PR-per-project-per-tick shape) drains four
#: nodes a minute, longest-waiting first, and defers the rest visibly. A pass
#: that had to CUT A LAUNCH CLIENT LOOSE stops at one and defers the remainder —
#: a wedged client costs the full bound, and a pass must not spend it twice.
MAX_RESUMES_PER_TICK = 4


def _due_entries(pauses, now):
    """``[(host, entry)]`` whose ``until`` has passed, from ONE snapshot.

    :func:`node_pauses.pending_resume` answers the same question and is the right
    call for a one-shot reader; it is not used here because it does its own
    ``load()``, and this pass has to decide expiry AND the span question from the
    same snapshot — the overlay is written by the CLI in another process, so two
    reads are two different worlds and a guard built on the second one can be
    reasoning about a pause the first one never saw.

    Longest-waiting first, then host, so the order is reproducible. Deadline
    order comes from the ``until`` STRING, not a re-parse: every writer in this
    codebase stores it through ``node_pauses._iso``, which always emits UTC
    ``+00:00``, so string order IS deadline order. A hand-edited overlay with a
    different offset could at worst reorder the pass — the SELECTION is
    :func:`node_pauses.is_expired`, which parses properly.
    """
    due = [(host, entry) for host, entry in pauses.items()
           if node_pauses.is_expired(entry, now)]
    due.sort(key=lambda row: (row[1]["until"] or "", row[0]))
    return due


def _nodes_still_held(pauses, now):
    """Every node a NON-expired pause owns: keyed hosts plus their spans.

    The set an expired resume must not touch. ``covering``/``paused_nodes`` would
    answer "who is parked" over a second read; this answers it over the snapshot
    the pass already holds, and deliberately skips the expired entries — a pause
    whose deadline passed is due to end, so it cannot also be a reason to hold.
    """
    held = set()
    for host, entry in pauses.items():
        if node_pauses.is_expired(entry, now):
            continue
        held.add(host)
        held.update(entry["span_nodes"])
    return held


def auto_resume_expired(now=None, directory=None, apply=True, echo=None,
                        resume=None, run=None, limit=MAX_RESUMES_PER_TICK,
                        serving_probe=None):
    """End every pause whose ``until`` has passed. Reports; never raises.

    One pass:

      1. read the overlay ONCE and take the due list out of that snapshot
         (:func:`_due_entries`),
      2. drop a candidate whose span overlaps a pause the operator still holds
         (see below),
      3. resume the rest through :func:`resume_host` — the SAME function
         ``hscc node resume`` runs, so the confirmation gate, the
         rebuild-or-refuse rule and the clear pairing are shared, not restated.

    THE SPAN GUARD, which the CLI cannot need and a tick must.
    ``hscc node resume`` refuses a host that only appears in another entry's
    ``span_nodes`` and points at the owner; a tick iterating the overlay's keyed
    hosts can never pick one of those up, so that half comes free. The half it
    CANNOT get from the CLI is the mirror image: an expired pause on ``A`` whose
    unit also runs on ``B``, where ``B`` has its own still-active pause. The
    recorded resume re-issues the whole declared workload, so restarting it would
    walk units onto a node the operator has not given back. Such a candidate is
    SKIPPED and its pause is kept — retried next tick, and by then the other
    pause has normally expired too. Skipping is never silent: it is logged,
    counted in the stamp, and makes the stream red.

    ``expiry_pass`` is deliberately NOT called here, though ``node_pauses``'
    docstring offers it. It drops every expired entry at once — including one
    whose restart just failed, which is "the record is cleared because the tick
    ran": the node would be back in the heal paths with units half up. The clear
    here is per host and only ever after a restart that succeeded, so a failed
    resume stays parked and stays retried.

    ``apply=False`` runs the whole pass as a dry run (nothing restarts, nothing
    clears) — the #478 default, reachable from here for the same reason the verb
    has it. ``resume``/``run``/``echo`` are the injected seams the executor
    already uses. Returns ``{"resumed", "kept", "skipped", "deferred",
    "attempted", "ok", "message"}``.
    """
    say = echo or print
    resumed, kept, skipped, deferred, results = [], [], [], [], []

    # ONE read. Everything below — who is due, and who is still promised to the
    # operator — is answered from this dict, never from a second one.
    pauses = node_pauses.list_pauses(directory)
    due = _due_entries(pauses, now)
    still_held = _nodes_still_held(pauses, now)

    for due_index, (host, entry) in enumerate(due):
        # The candidate's own host is subtracted, so `blocked` can only name
        # nodes some OTHER pause owns: expired entries are not in `still_held`
        # at all, so two pauses that are both due never block each other.
        blocked = sorted((({host} | set(entry["span_nodes"]))
                          & still_held) - {host})
        if blocked:
            why = (f"its span covers {', '.join(blocked)}, which another pause "
                   "still holds — restarting here would start units on a node "
                   "the operator has not released")
            skipped.append((host, why))
            say(f"SKIP auto-resume {host}: {why}")
            continue
        if limit is not None and len(results) >= limit:
            deferred.append(host)
            continue
        res = (resume or resume_host)(
            host, entry, apply=apply, directory=directory, echo=say,
            reason=f"daemon auto-resume of expired pause on {host}", run=run,
            # The SAME snapshot and clock, handed down: the pass must not ask a
            # second read whether the span is clear after deciding from the first
            # that this host was due.
            pauses=pauses, now=now, serving_probe=serving_probe)
        results.append(res)
        if res.get("action") == RESUME_RESUMED:
            resumed.append(host)
        elif not res.get("ok"):
            kept.append((host, res.get("error") or "restart refused"))
            say(f"AUTO-RESUME FAILED for {host}: {res.get('error')}; the pause "
                f"stays recorded and will be retried next tick")
        if res.get("detached"):
            # One launch per pass is the honest bound once a sparkrun client has
            # to be cut loose (t_09708c17): that client is not returning on its
            # own, so each further launch would cost this stream thread up to
            # SPARKRUN_LAUNCH_WAIT_TIMEOUT and could carry the stream past
            # verify's staleness floor while it waited. The rest of the queue
            # waits for the next tick — visible in the stamp as deferred, never
            # silently dropped.
            rest = [h for h, _e in due[due_index + 1:]
                    if h not in [r["host"] for r in results]]
            if rest:
                deferred.extend(rest)
                say(f"  deferring {len(rest)} further expired pause(s) to the "
                    f"next tick: {', '.join(rest)} — the launch client on "
                    f"{host} had to be cut loose and a pass does not spend "
                    f"that bound twice")
            break

    ok = not kept and not skipped
    if kept or skipped:
        bits = []
        if kept:
            bits.append(f"{len(kept)} pause(s) could not be resumed "
                        f"({', '.join(h for h, _w in kept)})")
        if skipped:
            bits.append(f"{len(skipped)} pause(s) held by a spanning pause "
                        f"({', '.join(h for h, _w in skipped)})")
        message = "; ".join(bits)
    elif resumed:
        message = f"auto-resumed {len(resumed)} expired pause(s): " \
                  f"{', '.join(resumed)}"
        if deferred:
            message += f"; {len(deferred)} deferred to the next tick"
    else:
        message = "no expired pauses" if not due else \
            "nothing resumed (dry run)"
    return {"ok": ok, "message": message, "attempted": len(results),
            "resumed": resumed, "kept": kept, "skipped": skipped,
            "deferred": deferred, "results": results}


def expiry_tick(now=None, directory=None, state_dir=None, write=None,
                echo=None, resume=None, run=None):
    """One supervised daemon tick: run the expiry pass, stamp the stream.

    The body of :func:`check_node_pause_expiry`, which is the CHECKS-table entry
    for the ``node_pause_expiry`` stream in ``daemon_ops.PERIODIC_INTERVALS``
    (60 s, the same class as ``kanban_enforce``: free when there is nothing to
    do). Four outcomes and ALL of them stamp:

      * no pauses   -> ``ok: True``, zero fleet reads, zero subprocesses;
      * resumed     -> ``ok: True``, the hosts named;
      * kept/skipped-> ``ok: False`` with the reason — the schedule the operator
        set is NOT being honoured, which is exactly the "policy is not being
        applied" red row ``kanban_enforce`` established. A past-due node silently
        staying parked is the failure this stream exists to make visible;
      * raising     -> ``ok: False``, the exception in the message.

    NEVER raises: a node-pause problem must not be able to take a stream down,
    and a stream that goes dark is indistinguishable from one that died.
    """
    if write is None:
        from .state import write_state as write
    if now is None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
    entry = {"ok": True, "message": "no expired pauses", "resumed": [],
             "kept": [], "skipped": [], "deferred": []}
    try:
        report = auto_resume_expired(now=now, directory=directory,
                                     echo=echo, resume=resume, run=run)
        entry.update(
            ok=report["ok"], message=report["message"],
            resumed=report["resumed"],
            kept=[h for h, _why in report["kept"]],
            skipped=[h for h, _why in report["skipped"]],
            deferred=report["deferred"],
            # The clock this pass judged against, so a reviewer can tell "nothing
            # was due" from "the pass ran on a clock that said nothing was due".
            checked_at=now if isinstance(now, str) else str(now))
        return entry
    except Exception as e:  # noqa: BLE001 — the stream must survive anything
        entry.update(ok=False, message=f"expiry tick failed: {e}")
        return entry
    finally:
        try:
            if state_dir:
                write(STREAM_NAME, entry, directory=state_dir)
            else:
                write(STREAM_NAME, entry)
        except Exception:  # pragma: no cover — write_state itself never raises
            pass


def check_node_pause_expiry():
    """The daemon's ``node_pause_expiry`` stream check (PERIODIC_INTERVALS body).

    The CHECKS-table entry in ``daemon_ops.run_daemon_loop``, so the pass rides
    the loop's standard per-stream machinery: its own supervised
    ``_run_supervised_periodic`` thread (a failure is logged and the thread
    respawned, never able to take another stream down) and the same per-stream
    staleness math in ``hscc verify``. ``expiry_tick`` stamps on every path; this
    wrapper only converts the entry to the check-fn truth convention and
    guarantees a raise cannot escape even if the tick's own guard did.
    """
    try:
        return bool(expiry_tick().get("ok"))
    except Exception as e:  # pragma: no cover — expiry_tick swallows internally
        try:
            from .daemon_ops import log
            log(f"node-pause-expiry: stream check raised: {e}", "ERROR")
        except Exception:
            pass
        return False


# ── doctor row: is the operator's schedule actually being honoured? ─────────
#
# WHY THIS ROW EXISTS (t_005e87fd)
# --------------------------------
# ``check_node_pause_expiry`` above is a supervised stream: ``hscc verify`` sees
# it (``verify._live_stream_intervals`` reads ``daemon_ops.PERIODIC_INTERVALS``),
# so a stream that went dark is detectable. What nothing said was the OTHER
# half — the stream is RED exactly when the operator's own promise is not being
# kept (``kept``: a due pause whose restart failed; ``skipped``: a due pause
# another pause still holds part of), and ``hscc doctor`` — the surface an
# operator runs to ask "why is this machine still parked" — had no row for it.
# ``kanban_enforce`` shipped its row in the same card that made it a stream
# (t_51931733); this is that half for this stream, split out rather than
# smuggled into the stream's own PR.
#
# THE VERDICTS, IN THE ORDER THE OPERATOR NEEDS THEM
# ---------------------------------------------------
#   1. no stamp while the daemon runs           -> RED, fatal (the orch-waker
#      class: the wiring itself is broken, so a past-due pause is parked
#      forever and nothing is even looking);
#   2. newest stamp ``ok: false``               -> RED, not fatal: the pass ran
#      and could not end a pause. Names the hosts out of ``kept``/``skipped``,
#      the ``checked_at`` the pass judged against, and the tick's own message;
#   3. stamp older than 2*cadence+slack         -> warn: the loop is not
#      reaching this stream (or the daemon is not running any more);
#   4. stamp written before this build          -> warn (upgrade race: the
#      running daemon has no such stream yet; the restart later in bootstrap
#      starts it) — never fatal, so a Stage-1 gate never aborts an install for
#      a race it causes itself;
#   5. daemon down / anything else unreadable   -> warn. Doctor is bootstrap's
#      Stage-1 gate and runs before a daemon exists, so only the "something is
#      ticking and this stream has never stamped" class may be fatal.
#
# ONE ROOT, AND IT IS THE WRITERS' ROOT
# --------------------------------------
# The row reads two artefacts — the stamp (``state.read_state(STREAM_NAME)``,
# i.e. ``state.STATE_DIR``) and the overlay (``node_pauses.list_pauses``) — and
# BOTH of their writers resolve from HOME at import, with no ``HSCC_DIR``
# override anywhere. So this row takes a root only from an explicit ``root=``
# caller, one root per call (the stamp at ``<root>/state``, the overlay at
# ``node_pauses.overlay_path(root)``, the pid at ``<root>/daemon.pid``), and the
# registry entry declares ``row_kwargs=()``: a root lifted from the environment
# is a directory no writer can reach, and a row aimed at it answers "nothing is
# parked, nothing is ticking" about a live daemon — the t_adb66608 / t_be690204
# class, measured twice before. The parameter is deliberately NOT called
# ``hscc_dir``: that name is what invited the env wiring those two cards removed.
#
# WHY THE OVERLAY, WHEN THE STAMP ALREADY SAYS ``ok``
# ---------------------------------------------------
# The stamp is the verdict; the overlay is what makes the verdict readable.
# "the stream is healthy" means nothing to an operator with four parked nodes
# until the row says how many are parked and how many are past due *right now*,
# and the never-stamped RED is only actionable when it can name the schedule it
# is failing. The count comes from the same ``_due_entries`` predicate the tick
# itself uses, over one snapshot, so the row and the tick cannot disagree about
# what "past due" means. Corruption is NOT re-detected here — the sibling
# ``node pauses`` row owns that (it listens to ``node_pauses``' own warnings);
# this row only says when its own overlay read was not clean.

#: The row's printed name. One constant for the row, its refusal and the
#: doctor-module fallback, so the three can never print three spellings.
CHECK_NAME = "node pause expiry (durable-until stream)"

#: Freshness slack on top of two stream cycles — the same shape as
#: ``kanban_pause.DOCTOR_SLACK_S`` / ``orch_waker.DOCTOR_SLACK_S``, because a
#: stream that skipped exactly one tick (a slow sparkrun call inside a resume)
#: is drift at three cycles, not a fault at two.
DOCTOR_SLACK_S = 90

#: Fallback cadence if ``daemon_ops.PERIODIC_INTERVALS`` cannot be read
#: (defensive only — the dict is a constant and the real cadence lives there).
_STREAM_CADENCE_FALLBACK = 60

#: The two leaves this row resolves under a root. ``daemon.pid`` belongs to
#: ``daemon_ops`` but is read from the SAME root, and is only ever used to tell
#: "nothing ticks" from "something ticks and it is not this stream".
_STATE_SUBDIR = "state"
_PID_FILENAME = "daemon.pid"

#: How much of the tick's own message the RED row quotes — same cap as
#: ``kanban_pause._doctor_check``'s, because a row nobody can read is a row
#: nobody acts on, and the full text is in the stamp file.
_MAX_QUOTED_MESSAGE = 160

#: Fractional seconds only, and a REGEX rather than ``split(".")``: the
#: ``+00:00`` offset must survive, or a UTC stamp reads as something local
#: (the same reasoning as ``doctor_node_pauses._FRACTIONAL_SECONDS``).
_FRACTIONAL_SECONDS = re.compile(r"\.\d+")

#: What the row says when it cannot be evaluated at all — a warn, never an
#: exception out of ``run_doctor()`` and never a silent green.
_UNVERIFIABLE = "the {} row could not be evaluated"

#: The one fix every "the stream is not running" verdict offers.
_DAEMON_FIX = ("restart the hscc daemon (`hscc start`) and re-run `hscc "
               "doctor`; if it stays red check ~/.hscc/daemon.log for a "
               "node-pause-expiry error")


def stream_cadence() -> int:
    """The daemon loop's cadence for this stream (the SOT that ``verify`` reads).

    Read from ``daemon_ops.PERIODIC_INTERVALS`` rather than restated, so the
    cadence, ``verify``'s staleness math and this row's freshness budget cannot
    drift apart. Imported inside the function: ``daemon_ops`` imports THIS module
    for its ``CHECKS`` table, so a module-level import the other way would only
    work by accident (``kanban_pause.stream_cadence`` does the same).
    """
    try:
        from .daemon_ops import PERIODIC_INTERVALS
        return int(PERIODIC_INTERVALS[STREAM_NAME])
    except Exception:  # noqa: BLE001 - defensive; the dict is a constant
        return _STREAM_CADENCE_FALLBACK


def doctor_freshness_limit_s() -> float:
    """The freshness budget for a stream ticking every :func:`stream_cadence`."""
    return 2 * float(stream_cadence()) + DOCTOR_SLACK_S


def _stamp_path_for(root) -> str:
    """Absolute path of the stream's stamp under ``root`` (``root`` required).

    ``<root>/state/<STREAM_NAME>.json`` is exactly where
    ``state.write_state(STREAM_NAME, entry, directory=<root>/state)`` puts it,
    which is the shape ``expiry_tick(state_dir=...)`` writes.
    """
    return os.path.join(os.path.expanduser(root), _STATE_SUBDIR,
                        f"{STREAM_NAME}.json")


def _pid_path_for(root) -> str:
    """The daemon pid file of the root the WRITERS resolve to.

    With no ``root`` this is derived from ``state.STATE_DIR`` — the same
    constant ``write_state`` stamps through — rather than from a second
    ``expanduser("~/.hscc")`` literal, so the row's liveness probe and the
    stream's stamp can never split into two roots (the t_adb66608 rule: derive
    the root from the writer's own constant, never restate it).
    """
    if root is not None:
        base = os.path.expanduser(root)
    else:
        from . import state as _state
        base = os.path.dirname(os.path.abspath(_state.STATE_DIR))
    return os.path.join(base, _PID_FILENAME)


def _daemon_pid_alive(pid_file=None) -> bool:
    """Is the pid in the daemon pid file a live process? (injected in tests)."""
    try:
        with open(pid_file or _pid_path_for(None)) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _daemon_predates_build(pid_file=None, build_file=None) -> bool:
    """Did the RUNNING daemon start before this build of the expiry stream?

    The upgrade race a fatal verdict must never mis-fire on, verbatim in intent
    from ``kanban_pause._daemon_predates_build``: doctor is bootstrap's Stage-1
    gate and the daemon restart is a later stage, so during an upgrade the live
    daemon was started from code with no ``node_pause_expiry`` stream at all.
    The pid file is written at daemon start, so ``pid mtime >= this file's
    mtime`` means the daemon could have stamped and did not (broken wiring);
    an older pid means it never could (upgrade). Either stat failing answers
    False — an unverdictable check must not sink an install.
    """
    try:
        started = os.path.getmtime(pid_file or _pid_path_for(None))
        built = os.path.getmtime(build_file or __file__)
    except OSError:
        return False
    return started < built


def _read_stamp(root=None) -> dict:
    """The stream's stamp (``{}`` when absent, unreadable or not an object).

    No ``root`` goes through ``state.read_state`` — the exact funnel
    ``expiry_tick`` writes through, including its test-time redirect. A
    malformed stamp returns ``{}``, which lands on the never-stamped branch:
    fail-LOUD, because a stamp nobody can parse is not evidence of a healthy
    stream (and the sibling overlay row reports the corruption itself).
    """
    if root is None:
        from .state import read_state
        try:
            data = read_state(STREAM_NAME)
        except Exception:  # noqa: BLE001 - read_state is local and never raises
            return {}
        return data if isinstance(data, dict) else {}
    path = _stamp_path_for(root)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_overlay(root=None):
    """``(pauses, read_was_dirty)`` — the overlay plus whether IT complained.

    ``node_pauses.load`` never raises: a truncated or malformed overlay reads as
    "no pauses" and says so only through ``_warn``. That fail-open is right for
    a repair path and wrong for a count, so the row listens to the reader's own
    warning instead of re-implementing its validation (the
    ``doctor_node_pauses._read`` precedent) and marks the count it printed as
    one the reader could not vouch for. The swap is restored in ``finally``.
    """
    collected: list = []
    real = getattr(node_pauses, "_warn", None)
    if not callable(real):  # a core with no hook cannot be listened to
        pauses = node_pauses.list_pauses(root)
        return (pauses if isinstance(pauses, dict) else {}), True

    def _capture(msg):
        collected.append(str(msg))

    node_pauses._warn = _capture
    try:
        pauses = node_pauses.list_pauses(root)
    except Exception:  # noqa: BLE001 - load() is documented never to raise
        return {}, True
    finally:
        node_pauses._warn = real
    return (pauses if isinstance(pauses, dict) else {}), bool(collected)


def _stamp_age(stamp, clock):
    """Seconds since ``state.write_state`` wrote the stamp; None if it proves
    nothing. ``timestamp`` is the ISO field every stream has (the same one
    ``verify`` parses); an unparseable one is NOT a fresh stream."""
    raw = stamp.get("timestamp")
    parsed = node_pauses._parse_iso(raw) if isinstance(raw, str) else None
    if parsed is None:
        return None
    return (clock - parsed).total_seconds()


def _iso_note(value) -> str:
    """A stamp for display, microseconds trimmed, offset kept."""
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    trimmed = value.strip()
    return _FRACTIONAL_SECONDS.sub("", trimmed)


def _names(value) -> list:
    """The host list a stamp carries, filtered to printable strings."""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(h).strip() for h in value if isinstance(h, str) and h.strip()]


def _pause_note(pauses, clock) -> str:
    """``"no node pauses parked"`` / ``"2 pause(s) parked, 1 past due"``.

    The past-due count uses :func:`_due_entries` — the predicate the tick itself
    applies to the same snapshot — so the row cannot claim a pause is past due
    when the pass that just ran disagrees.
    """
    if not pauses:
        return "no node pauses parked"
    due = len(_due_entries(pauses, clock))
    return (f"{len(pauses)} pause(s) parked, "
            + (f"{due} past due" if due else "none past due"))


def refused_check() -> dict:
    """The in-suite verdict when no root was supplied and no seam was injected.

    A default-root read inside a suite is a read of the operator's
    ``~/.hscc/state/node_pause_expiry.json`` and ``~/.hscc/node_pauses.json``,
    which would let whatever the fleet happens to be doing decide what the
    suite asserts. An ERROR naming the seam, never a silent pass.
    """
    return {
        "name": CHECK_NAME, "ok": False, "fatal": False,
        "detail": "node-pause-expiry check refused inside a pytest process — "
                  "inject _node_pause_expiry_check or pass root=",
        "fix": "Call node_pause_exec.doctor_check(root=<tmp ~/.hscc-shaped "
               "root>) for the real read path, or inject "
               "_node_pause_expiry_check=lambda: {...} to pin the row's "
               "routing; a suite must never read the operator's stamp or "
               "overlay.",
    }


def _doctor_check(now=None, root=None, stream_state=None, _daemon_running=None,
                  _predates_build=None) -> dict:
    """The five-field verdict: is the durable-until schedule being honoured?

    ``root`` is the ONE root for all three reads (stamp, overlay, pid) and is
    never taken from the environment — see the module comment above. Every
    probe is injectable so no test has to reach a daemon, a node, or the
    operator's ``~/.hscc``.
    """
    if root is None and "pytest" in sys.modules:
        return refused_check()

    # One clock for the whole verdict: the age arithmetic and the past-due
    # question must be answered against the same instant.
    clock = node_pauses._parse_iso(now) if isinstance(now, str) else None
    if clock is None:
        clock = now if isinstance(now, datetime) else node_pauses._utc(None)

    pid_file = _pid_path_for(root)
    if _daemon_running is None:
        _daemon_running = lambda: _daemon_pid_alive(pid_file)  # noqa: E731
    if _predates_build is None:
        _predates_build = lambda: _daemon_predates_build(pid_file)  # noqa: E731

    stamp = stream_state if stream_state is not None else _read_stamp(root)
    pauses, dirty = _read_overlay(root)
    note = _pause_note(pauses, clock)
    if dirty:
        note += ("; the pause overlay was not readable cleanly — see the "
                 "'node pauses' row")

    age = _stamp_age(stamp, clock)
    limit = doctor_freshness_limit_s()
    hint = (_stamp_path_for(root) if root
            else f"~/.hscc/{_STATE_SUBDIR}/{STREAM_NAME}.json")
    running = _daemon_running()      # probed ONCE: a restart mid-check must not
    # make the liveness bit and the age comparison describe two different worlds.

    def _verdict(ok, fatal, detail, fix=""):
        return {"name": CHECK_NAME, "ok": ok, "fatal": fatal,
                "detail": detail, "fix": fix}

    if age is None:
        # (1) The orch-waker class, and the only fatal this row may raise.
        if running and not _predates_build():
            return _verdict(
                False, True,
                f"never stamped while the daemon runs — the {STREAM_NAME} "
                f"stream has no readable stamp at {hint} ({note}); a pause "
                "whose `until` passed would stay parked and nothing would "
                "notice", _DAEMON_FIX)
        if running:
            return _verdict(
                False, False,
                f"the RUNNING daemon predates this build — the "
                f"{STREAM_NAME} stream has never stamped ({note}); the daemon "
                "restart later in bootstrap starts it", _DAEMON_FIX)
        return _verdict(
            False, False,
            f"the daemon is not running — nothing ends an expired pause "
            f"({note}; start it with `hscc start`)", _DAEMON_FIX)

    # (2) The stamped RED: the pass ran and did NOT end what was due. Checked
    # before staleness on purpose (the card's priority): "a pause is past due
    # and still parked" is the actionable fact, and the age is printed with it,
    # so a stamp that is BOTH red and stale still says how old it is.
    if stamp.get("ok") is False:
        kept, skipped = _names(stamp.get("kept")), _names(stamp.get("skipped"))
        bits = []
        if kept:
            bits.append(f"restart FAILED on {', '.join(kept)}")
        if skipped:
            bits.append(f"span HELD by another pause on {', '.join(skipped)}")
        if not bits:
            bits.append("the last pass reported ok=False")
        message = str(stamp.get("message") or "").strip()[:_MAX_QUOTED_MESSAGE]
        return _verdict(
            False, False,
            f"PAST DUE, STILL PARKED: {'; '.join(bits)} (checked_at "
            f"{_iso_note(stamp.get('checked_at'))}, last pass {age:.0f}s ago "
            f"of a {limit:.0f}s budget; {note})"
            + (f" — {message}" if message else ""),
            "end the pause by hand once the cause is fixed "
            "(`hscc node resume <host>`); the stream re-tries every "
            f"{stream_cadence()}s so this clears on its own")

    # (3) Fresh enough to trust, but the loop is not reaching this stream.
    if age > limit:
        return _verdict(
            False, False,
            f"stale: last expiry pass {age:.0f}s ago (budget {limit:.0f}s = "
            f"2*{stream_cadence()}s+{DOCTOR_SLACK_S}s; {note})", _DAEMON_FIX)

    # (4/5) Green: the pass ran recently and nothing is stuck. A deferred host
    # is not a fault (the per-tick limit drained it onto the next tick), but it
    # is named, because "green" that hides a backlog reads as "nothing waiting".
    detail = (f"expiry stream healthy: last pass {age:.0f}s ago (budget "
              f"{limit:.0f}s); {note}")
    deferred = _names(stamp.get("deferred"))
    if deferred:
        detail += f"; {len(deferred)} deferred to the next tick"
    resumed = _names(stamp.get("resumed"))
    if resumed:
        detail += f"; last pass resumed {', '.join(resumed)}"
    return _verdict(True, False, detail, "")


def doctor_check(now=None, root=None, stream_state=None, _daemon_running=None,
                 _predates_build=None) -> dict:
    """The doctor row for the ``node_pause_expiry`` stream. Never raises.

    Thin guard around :func:`_doctor_check`: doctor is a report, and a report
    that throws takes the rest of ``hscc doctor`` down with it. An exception
    here becomes a warn naming itself — the same rule every other feature row
    follows ("a check that did not run prints that it did not run").
    """
    try:
        return _doctor_check(now=now, root=root, stream_state=stream_state,
                             _daemon_running=_daemon_running,
                             _predates_build=_predates_build)
    except Exception as e:  # noqa: BLE001 - a row must never break the report
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": _UNVERIFIABLE.format(STREAM_NAME)
                          + f" ({type(e).__name__}: {e})",
                "fix": _DAEMON_FIX}
