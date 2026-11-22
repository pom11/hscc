"""Tests for the node-pause executor (:mod:`hscc_daemon.node_pause_exec`).

Hermetic by construction — the card's rule, and the fleet's own rule
(``nolive-rule.txt``):

* every host is a documentation placeholder (192.0.2.x, RFC 5737);
* ``sparkrun``, ``ssh`` and ``docker`` are planted fake scripts on PATH, each
  appending to a per-test call log, so an attempt to reach a real host is
  visible in the log rather than executed (the transport tests read that log to
  prove an applied stop really did shell out to the planted CLI, and that
  neither ``ssh`` nor ``docker`` was ever run);
* every other test injects a status snapshot and a fake ``run``, so no
  subprocess is spawned at all. The sparkrun status payload itself is never
  exec'd from this file: the shared sparkrun fence blocks it, correctly,
  because on the operator's host that payload drives the live fleet.

Nothing here reads the operator's serving.json or talks to the cluster.
"""

import inspect
import json
import os
import sys
import time

import pytest

from hscc_daemon import node_pause_exec as npe
from hscc_daemon import serving, sparkrun_bin, util

# ── the fixture fleet (placeholders only) ──────────────────────────────────
ORCH = "192.0.2.11"
W1 = "192.0.2.12"
W2 = "192.0.2.13"
#: Two single-node models colocated on one machine — the shape the 11-22 rule
#: ALLOWS (every model on it lives there alone, so all of them park together).
W3 = "192.0.2.14"
#: One single-node model and nothing else — the plain allowed case.
W4 = "192.0.2.15"
#: A host no serving.json unit declares — the shape of a foreign container.
FOREIGN = "192.0.2.20"

ORCH_RECIPE = "/srv/recipes/orchestrator.yaml"
WORKER_RECIPE = "/srv/recipes/worker.yaml"
SPAN_RECIPE = "/srv/recipes/worker-tp.yaml"

SERVING = {
    "port": 8000,
    "units": [
        {"id": "orch", "role": "orchestrator", "nodes": [ORCH], "port": 8000,
         "recipe": ORCH_RECIPE, "model": "meta-llm/Orch-8B", "tp": 1},
        {"id": "w1", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "wspan", "role": "worker", "nodes": [W1, W2], "port": 8200,
         "recipe": SPAN_RECIPE, "model": "meta-llm/Work-8B", "tp": 2},
        {"id": "w3a", "role": "worker", "nodes": [W3], "port": 8300,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "w3b", "role": "worker", "nodes": [W3], "port": 8310,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-9B", "tp": 1},
        {"id": "w4", "role": "worker", "nodes": [W4], "port": 8400,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-7B", "tp": 1},
    ],
}


def status_payload(groups=None, solo=None, errors=None, idle=None):
    """A ClusterStatusResult.to_dict() shape, as sparkrun emits it."""
    return {
        "groups": groups or {},
        "solo_entries": solo or [],
        "errors": errors or {},
        "idle_hosts": idle or [],
        "preparing_hosts": [],
        "pending_ops": [],
        "total_containers": 0,
        "host_count": 0,
    }


def group(cid, recipe, hosts, port, model="meta-llm/Work-8B", served=None,
          tp=None, observed=None):
    """One multi-container group; ``observed`` narrows the reported placement
    (a sibling node that did not answer the sweep) while meta.hosts keeps the
    launched span."""
    meta = {"recipe": recipe, "hosts": list(hosts), "port": port,
            "model": model}
    if served:
        meta["served_model_name"] = served
    if tp:
        meta["tensor_parallel"] = tp
    seen = hosts if observed is None else observed
    return {cid: {"meta": meta,
                  "containers": [{"host": h, "role": "worker",
                                  "status": "running", "image": "img"}
                                 for h in seen]}}


def solo(workload_id, host, recipe, port, served=None):
    meta = {"recipe": recipe, "hosts": [host], "port": port}
    if served:
        meta["served_model_name"] = served
    return [{"workload_id": workload_id, "host": host, "meta": meta,
             "status": "running", "image": "img"}]


def payload_for_host(host):
    """The whole fixture fleet, as one status snapshot."""
    groups = {}
    groups.update(group("cid_orch", ORCH_RECIPE, [ORCH], 8000,
                        model="meta-llm/Orch-8B",
                        served="meta-llm/Orch-8B orchestrator-model"))
    groups.update(group("cid_w1", WORKER_RECIPE, [W1], 8100,
                        served="meta-llm/Work-8B worker-model"))
    groups.update(group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2,
                        served="meta-llm/Work-8B worker-model"))
    groups.update(group("cid_w3a", WORKER_RECIPE, [W3], 8300,
                        model="meta-llm/Work-8B",
                        served="meta-llm/Work-8B worker-model"))
    groups.update(group("cid_w3b", WORKER_RECIPE, [W3], 8310,
                        model="meta-llm/Work-9B",
                        served="meta-llm/Work-9B worker-model"))
    groups.update(group("cid_w4", WORKER_RECIPE, [W4], 8400,
                        model="meta-llm/Work-7B",
                        served="meta-llm/Work-7B worker-model"))
    return status_payload(groups=groups, idle=[host])


def reader(payload=None, error=None):
    """A status_reader standing in for the shell-out: ``(data, error)``."""
    def _read():
        return payload, error
    return _read


class Recorder:
    """Fake ``run``: records argv, answers from a queue (ok by default)."""

    def __init__(self, results=()):
        self.calls = []
        self._results = list(results)

    def __call__(self, cmd, timeout=None, **kw):
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        if self._results:
            return self._results.pop(0)
        return {"ok": True, "output": "", "stderr": ""}


class Lines:
    """Fake ``echo``: collects printed lines."""

    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


def unit_of(units, unit_id):
    """One unit record out of a plan (or a unit list) — fails loudly if absent."""
    units = units["units"] if isinstance(units, dict) else list(units)
    for u in units:
        if u["unit_id"] == unit_id:
            return u
    raise AssertionError(f"{unit_id} not in plan units "
                         f"{[u['unit_id'] for u in units]}")


# ── plan(): enumeration ────────────────────────────────────────────────────
class TestPlanEnumeration:
    def test_lists_only_the_units_touching_the_host(self):
        plan = npe.plan(ORCH, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(ORCH)))
        assert plan["ok"] is True
        assert [u["unit_id"] for u in plan["units"]] == ["orch"]

    def test_two_units_on_one_node_are_both_listed(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        assert sorted(u["unit_id"] for u in plan["units"]) == ["w1", "wspan"]

    def test_unit_carries_identity_recipe_port_and_tp(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        u = unit_of(plan, "wspan")
        assert u["recipe"] == SPAN_RECIPE
        assert u["port"] == 8200
        assert u["tp"] == 2
        assert u["kind"] == "worker"
        assert u["serving_match"] is True

    def test_solo_entries_are_enumerated_too(self):
        payload = status_payload(solo=solo("cid_solo", W2, "/srv/x.yaml", 8300))
        plan = npe.plan(W2, serving_dict=SERVING, status_reader=reader(payload))
        assert unit_of(plan, "cid_solo")["observed"] is True

    def test_solo_id_is_read_under_both_spellings(self):
        """sparkrun's to_dict() says ``workload_id``; older readers said
        ``cluster_id``. Either spelling must yield a real id, never '?'."""
        entry = solo("cid_solo", W2, "/srv/x.yaml", 8300)[0]
        entry["cluster_id"] = entry.pop("workload_id")
        plan = npe.plan(W2, serving_dict=SERVING,
                        status_reader=reader(status_payload(solo=[entry])))
        assert unit_of(plan, "cid_solo")["cluster_id"] == "cid_solo"

    def test_declared_unit_the_sweep_does_not_show_is_listed_unobserved(self):
        """A unit that is loading (or already down) is still owned by the pause
        — but it must be marked not observed, so no caller can claim it
        stopped something."""
        groups = {}
        groups.update(group("cid_w1", WORKER_RECIPE, [W1], 8100))
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(status_payload(groups=groups)))
        assert unit_of(plan, "wspan")["observed"] is False
        assert unit_of(plan, "w1")["observed"] is True

    def test_uses_load_serving_when_no_config_is_handed_in(self, monkeypatch):
        seen = {}

        def fake_load(path=None):
            seen["called"] = True
            return SERVING
        monkeypatch.setattr(serving, "load_serving", fake_load)
        plan = npe.plan(ORCH, status_reader=reader(payload_for_host(ORCH)))
        assert seen.get("called") is True
        assert plan["units"][0]["kind"] == "orchestrator"


# ── plan(): span detection ─────────────────────────────────────────────────
class TestSpanDetection:
    def test_tp_unit_spanning_two_nodes_is_flagged_with_both_nodes(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        u = unit_of(plan, "wspan")
        assert u["span"] is True
        assert u["nodes"] == [W1, W2]

    def test_span_is_visible_from_the_other_node_too(self):
        """Pausing either node of a TP pair must show the same pair — the
        operator has to see the blast radius whichever side they start from."""
        plan = npe.plan(W2, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W2)))
        assert unit_of(plan, "wspan")["nodes"] == [W1, W2]
        assert plan["span_units"] == ["wspan"]

    def test_single_node_unit_is_not_a_span(self):
        plan = npe.plan(ORCH, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(ORCH)))
        assert unit_of(plan, "orch")["span"] is False
        assert plan["span_units"] == []

    def test_span_survives_a_sibling_that_did_not_answer_the_sweep(self):
        """The dangerous case: meta.hosts records a TP pair, but only one node
        answered, so the containers list is half-empty. The unit must still be
        reported as a span, with the unverified sibling named."""
        groups = {}
        groups.update(group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2,
                            observed=[W1]))
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(status_payload(groups=groups)))
        u = unit_of(plan, "wspan")
        assert u["span"] is True
        assert u["nodes"] == [W1, W2]
        assert u["unverified_nodes"] == [W2]

    def test_undocumented_placement_still_counts_as_a_span(self):
        """observed containers say two hosts, serving.json declares one — the
        live placement wins, because that is what a stop would take down."""
        groups = {}
        groups.update(group("cid_w1", WORKER_RECIPE, [W1], 8100,
                            observed=[W1, W2]))
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(status_payload(groups=groups)))
        assert unit_of(plan, "w1")["span"] is True

    def test_launch_record_is_the_only_span_source_for_an_unconfigured_job(
            self):
        """The case the serving.json union cannot rescue: a workload no
        serving.json unit describes, whose second node did not answer the
        sweep. ``meta.hosts`` — what sparkrun recorded the launch across — is
        then the ONLY source of the sibling, and losing it hides the blast
        radius completely."""
        groups = {}
        groups.update(group("cid_odd", "/srv/odd.yaml", [W1, W2], 8600,
                            observed=[W1]))
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(status_payload(groups=groups)))
        u = unit_of(plan, "cid_odd")
        assert u["serving_match"] is False
        assert u["span"] is True
        assert u["nodes"] == [W1, W2]
        assert u["unverified_nodes"] == [W2]
        assert plan["span_siblings"] == [W2]

    def test_a_solo_entry_carries_its_recorded_span(self):
        """``solo_entries`` report one container each, but the job's own record
        still says how many nodes it was launched across."""
        entry = solo("cid_pair", W1, "/srv/odd.yaml", 8700)[0]
        entry["meta"]["hosts"] = [W1, W2]
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(status_payload(solo=[entry])))
        u = unit_of(plan, "cid_pair")
        assert u["serving_match"] is False
        assert u["span"] is True
        assert u["nodes"] == [W1, W2]

    def test_span_nodes_are_every_affected_node_including_the_host(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        assert plan["span_nodes"] == [W1, W2]
        assert plan["span_siblings"] == [W2]


# ── plan(): fail-closed reads ──────────────────────────────────────────────
class TestPlanFailClosed:
    def test_failed_read_is_not_read_as_an_empty_node(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(None, "sparkrun status raised"))
        assert plan["ok"] is False
        assert plan["units"] == []
        assert "sparkrun status raised" in plan["error"]

    def test_failed_read_still_flags_the_orchestrator_host(self):
        """Without a read there is no evidence the host is NOT the orchestrator
        node, and the CLI's refusal is the cheap direction to err in."""
        plan = npe.plan(ORCH, serving_dict=SERVING,
                        status_reader=reader(None, "boom"))
        assert plan["serves_orchestrator_model"] is True

    def test_host_that_did_not_answer_the_sweep_reports_no_units(self):
        payload = status_payload(errors={W1: "ssh: timeout"},
                                 groups=dict(group("cid_span", SPAN_RECIPE,
                                                   [W1, W2], 8200, tp=2)))
        plan = npe.plan(W1, serving_dict=SERVING, status_reader=reader(payload))
        assert plan["ok"] is False
        assert plan["units"] == []
        assert W1 in plan["error"]
        assert plan["unreachable_hosts"] == [W1]

    def test_status_answered_without_fleet_data_is_an_error(self, monkeypatch):
        """The script's no-hosts early exit answers {"host_list": []} — a
        reply, not an observation (health applies the same rule)."""
        monkeypatch.setattr(util, "run_cmd",
                            lambda *a, **k: {"ok": True, "output": json.dumps(
                                {"host_list": []})})
        data, error = npe.fetch_cluster_status()
        assert data is None
        assert "without fleet data" in error

    def test_unresolvable_venv_python_is_reported_not_silenced(self,
                                                                monkeypatch):
        from hscc_daemon import health as health_mod
        monkeypatch.setattr(health_mod, "_sparkrun_venv_python", lambda: None)
        data, error = npe.fetch_cluster_status()
        assert data is None
        assert "venv python unresolvable" in error

    def test_non_zero_exit_is_reported(self, monkeypatch):
        monkeypatch.setattr(util, "run_cmd",
                            lambda *a, **k: {"ok": False, "output": "",
                                             "stderr": "Permission denied"})
        data, error = npe.fetch_cluster_status()
        assert data is None
        assert "Permission denied" in error

    def test_unparsable_json_is_reported(self, monkeypatch):
        monkeypatch.setattr(util, "run_cmd",
                            lambda *a, **k: {"ok": True, "output": "{oops"})
        data, error = npe.fetch_cluster_status()
        assert data is None
        assert "unparsable" in error


# ── plan(): orchestrator-model detection ───────────────────────────────────
class TestOrchestratorDetection:
    def test_flagged_from_the_served_model_config(self):
        plan = npe.plan(ORCH, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(ORCH)))
        assert plan["serves_orchestrator_model"] is True
        assert "orch" in plan["orchestrator_evidence"]

    def test_a_plain_worker_node_is_not_flagged(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        assert plan["serves_orchestrator_model"] is False

    def test_live_alias_flags_a_host_the_config_does_not_mention(self):
        """Corroboration: a workload advertising the orchestrator alias on a
        node serving.json does not list as orchestrator still gets flagged."""
        serving_no_orch = {"units": [
            {"id": "w", "role": "worker", "nodes": [W1], "port": 8100,
             "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B"}]}
        payload = status_payload(groups=dict(
            group("cid_odd", WORKER_RECIPE, [W1], 8100,
                  served="meta-llm/Work-8B orchestrator-model")))
        plan = npe.plan(W1, serving_dict=serving_no_orch,
                        status_reader=reader(payload))
        assert plan["serves_orchestrator_model"] is True
        assert "advertises" in plan["orchestrator_evidence"]

    def test_a_worker_alias_on_a_worker_is_not_flagged(self):
        """The alias test is token-exact: ``worker-model`` must not match just
        because it ends in ``-model``."""
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        assert unit_of(plan, "w1")["kind"] == "worker"

    def test_missing_serving_config_fails_closed(self):
        flag, evidence = npe.orchestrator_flag(W1, None)
        assert flag is True
        assert "cannot rule out" in evidence

    def test_unreadable_serving_config_makes_the_plan_refuse_prone(self,
                                                                   monkeypatch):
        monkeypatch.setattr(serving, "load_serving", lambda path=None: None)
        plan = npe.plan(W1, status_reader=reader(payload_for_host(W1)))
        assert plan["serves_orchestrator_model"] is True


# ── the guard: ONE check, fleet-wide (card t_51a11d16) ─────────────────────
# The uniform pool this guard exists for: three replicas, every one of them
# advertising the orchestrator alias, ONE of them declared by the applied
# template. Placeholders only (RFC 5737); the status read is injected, so
# nothing here touches a host (nolive-rule.txt).
P1, P2, P3 = "192.0.2.41", "192.0.2.42", "192.0.2.43"
POOL_RECIPE = "/srv/recipes/pool.yaml"
POOL_MODEL = "meta-llm/Pool-8B"
POOL_SERVED = f"{POOL_MODEL} worker-model orchestrator-model"
POOL_PORTS = (8000, 8001, 8002)
POOL_NODES = (P1, P2, P3)

POOL_SERVING = {
    "port": POOL_PORTS[0],
    "uniform_pool": True,
    "units": [{"id": f"pu{i}",
               "role": "orchestrator" if i == 0 else "worker",
               "nodes": [h], "port": p, "recipe": POOL_RECIPE,
               "model": POOL_MODEL, "tp": 1}
              for i, (h, p) in enumerate(zip(POOL_NODES, POOL_PORTS))],
}


def pool_status(down=(), served=POOL_SERVED, unobserved=()):
    """A sweep of the pool.

    ``down`` hosts are missing from the sweep entirely; ``unobserved`` hosts are
    still declared but the sweep shows no container for them (still loading, or
    already gone) — the two states the healthy count must treat the same way and
    the overlay state must treat differently.
    """
    groups = {}
    for i, (h, p) in enumerate(zip(POOL_NODES, POOL_PORTS)):
        if h in down:
            continue
        groups.update(group(f"cid_pu{i}", POOL_RECIPE, [h], p,
                            model=POOL_MODEL, served=served,
                            observed=[] if h in unobserved else None))
    return status_payload(groups=groups)


def pool_plan(host, **payload_kw):
    return npe.plan(host, serving_dict=POOL_SERVING,
                    status_reader=reader(pool_status(**payload_kw)))


DECLARED_P1 = ({"template": "4node-flash-next-all", "uniform_pool": True,
                "orchestrator_node": P1}, None)


class TestOrchestratorPauseGuard:
    """The guard's own contract, through its injectable seams.

    The CLI suite proves the verdict reaches the operator; this proves the
    verdict is computed from the right inputs — the plan it was handed, the
    overlay it reads, and the applied record it is allowed to consult only
    through the sanctioned reader.
    """

    def test_a_replica_pause_is_allowed_and_names_the_remaining_servers(self):
        v = npe.orchestrator_pause_guard(
            P2, plan=pool_plan(P2), paused=set(), applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_ALLOW
        assert v["others"] == [P1, P3]
        assert v["remaining_units"] == 2
        assert "orchestrator-model stays served by 2 other node(s)" \
            in v["lines"][0]

    def test_the_declared_node_refuses_and_says_which_record_said_so(self):
        v = npe.orchestrator_pause_guard(
            P1, plan=pool_plan(P1), paused=set(), applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["reason"] == npe.REASON_DECLARED_NODE
        joined = "\n".join(v["lines"])
        assert "declared orchestrator node" in joined
        assert "4node-flash-next-all" in joined and f"orchestrator_node={P1}" \
            in joined
        assert v["lines"][-1] == "Nothing stopped and nothing recorded."

    def test_an_absent_record_declares_nothing_and_is_said_so(self):
        """A hand-written serving.json (hscc-bootstrap) has no applied record.
        That must not become "every node is the orchestrator node" — and must not
        pass for a checked declaration either."""
        v = npe.orchestrator_pause_guard(
            P1, plan=pool_plan(P1), paused=set(),
            applied_state=(None, "unverified: no applied-template state"))
        assert v["verdict"] == npe.GUARD_ALLOW
        assert v["declared_node"] is None
        assert "could not be consulted" in "\n".join(v["lines"])

    def test_a_healthy_count_is_what_protects_the_last_server(self):
        """The other two are gone from the sweep: P2 alone serves the alias."""
        v = npe.orchestrator_pause_guard(
            P2, plan=pool_plan(P2, down={P1, P3}), paused=set(),
            applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["reason"] == npe.REASON_NO_HEALTHY_SERVER
        assert "would leave no healthy unit" in "\n".join(v["lines"])
        assert v["remaining_units"] == 0

    def test_a_declared_unit_the_sweep_never_saw_is_not_a_server(self):
        """Down and merely-unobserved are the same thing to a healthy count:
        neither is evidence of a server that would still answer."""
        v = npe.orchestrator_pause_guard(
            P2, plan=pool_plan(P2, unobserved={P1, P3}), paused=set(),
            applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["healthy_units"] == 1

    def test_parked_replicas_are_not_counted_as_healthy_servers(self):
        """The overlay is a third state the sweep cannot express: P1/P3 are up
        and advertising, and deliberately parked. Counting them would allow the
        pause and leave nothing serving."""
        v = npe.orchestrator_pause_guard(
            P2, plan=pool_plan(P2), paused={P1, P3}, applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["reason"] == npe.REASON_NO_HEALTHY_SERVER
        joined = "\n".join(v["lines"])
        assert "paused and therefore not counted" in joined
        assert P1 in joined and P3 in joined

    def test_a_span_unit_leaves_with_every_node_it_spans(self):
        """A shared unit counts as ONE server: taking it down here takes it down
        on its sibling node too, so the sibling is not a remaining server."""
        serving = {"uniform_pool": True, "units": [
            {"id": "pair", "role": "orchestrator", "nodes": [P1, P2],
             "port": 8000, "recipe": POOL_RECIPE, "model": POOL_MODEL,
             "tp": 2}]}
        payload = status_payload(groups=dict(
            group("cid_pair", POOL_RECIPE, [P1, P2], 8000,
                  model=POOL_MODEL, served=POOL_SERVED, tp=2)))
        plan = npe.plan(P1, serving_dict=serving, status_reader=reader(payload))
        v = npe.orchestrator_pause_guard(
            P1, plan=plan, paused=set(),
            applied_state=({"orchestrator_node": "192.0.2.99"}, None))
        assert plan["span_nodes"] == [P1, P2]
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["others"] == [], "the span sibling must not count as a survivor"
        assert "and its span " in "\n".join(v["lines"])

    def test_a_declared_orchestrator_counts_without_advertising_the_alias(self):
        """Config-declared role and live alias are two proofs, not two gates: a
        unit serving only its concrete model name still serves the orchestrator."""
        serving = {"units": [
            {"id": "orch-only", "role": "orchestrator", "nodes": [P1],
             "port": 8000, "recipe": POOL_RECIPE, "model": POOL_MODEL}]}
        payload = status_payload(groups=dict(
            group("cid_orch_only", POOL_RECIPE, [P1], 8000,
                  model=POOL_MODEL, served=POOL_MODEL)))
        v = npe.orchestrator_pause_guard(
            P1, plan=npe.plan(P1, serving_dict=serving,
                              status_reader=reader(payload)),
            paused=set(), applied_state=(None, "no applied state"))
        assert v["verdict"] == npe.GUARD_REFUSE, \
            "the one orchestrator unit must be protected even without the token"

    def test_a_host_serving_no_alias_gets_no_note_at_all(self):
        serving = {"units": [{"id": "w", "role": "worker", "nodes": [P2],
                              "port": 8100, "recipe": WORKER_RECIPE,
                              "model": "meta-llm/Work-8B"}]}
        payload = status_payload(groups=dict(
            group("cid_w", WORKER_RECIPE, [P2], 8100,
                  served="meta-llm/Work-8B worker-model")))
        v = npe.orchestrator_pause_guard(
            P2, plan=npe.plan(P2, serving_dict=serving,
                              status_reader=reader(payload)),
            paused=set(), applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_ALLOW
        assert v["lines"] == [], "nothing to say about a model it does not serve"

    def test_force_overrides_either_ground_and_says_which(self):
        declared = npe.orchestrator_pause_guard(
            P1, plan=pool_plan(P1), paused=set(), applied_state=DECLARED_P1,
            force=True)
        assert declared["verdict"] == npe.GUARD_ALLOW
        assert declared["forced"] is True
        assert "declared orchestrator node" in declared["lines"][0]
        last = npe.orchestrator_pause_guard(
            P2, plan=pool_plan(P2, down={P1, P3}), paused=set(),
            applied_state=DECLARED_P1, force=True)
        assert last["verdict"] == npe.GUARD_ALLOW and last["forced"] is True
        assert "would leave no healthy unit" in last["lines"][0]

    def test_an_unreadable_fleet_read_refuses_rather_than_allows(self):
        plan = npe.plan(P2, serving_dict=POOL_SERVING,
                        status_reader=reader(None, "sparkrun status query failed"))
        v = npe.orchestrator_pause_guard(P2, plan=plan, paused=set(),
                                         applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["reason"] == npe.REASON_UNREADABLE
        assert "could not be" in "\n".join(v["lines"])

    def test_an_unreadable_serving_config_refuses_too(self, monkeypatch):
        monkeypatch.setattr(serving, "load_serving", lambda path=None: None)
        v = npe.orchestrator_pause_guard(
            P2, plan=npe.plan(P2, status_reader=reader(pool_status())),
            paused=set(), applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_REFUSE
        assert v["reason"] == npe.REASON_UNREADABLE

    def test_a_hand_handed_plan_means_the_fleet_is_read_once(self):
        """The guard must not sweep again behind the caller's back: a second
        read could disagree with the plan already printed, and doubles the cost
        of every pause."""
        def boom():
            raise AssertionError("the guard swept the fleet a second time")
        plan = pool_plan(P2)
        v = npe.orchestrator_pause_guard(P2, plan=plan, paused=set(),
                                         status_reader=boom,
                                         applied_state=DECLARED_P1)
        assert v["verdict"] == npe.GUARD_ALLOW

    def test_the_plan_carries_the_fleet_wide_alias_view(self):
        """The guard counts servers on nodes it was not asked about, and it must
        get them from the plan — one sweep, one answer."""
        units = pool_plan(P2)["orchestrator_alias_units"]
        assert sorted(u["nodes"][0] for u in units) == [P1, P2, P3]
        assert all(u["observed"] for u in units)
        assert [u["role"] for u in units if u["nodes"] == [P1]] == ["orchestrator"]


# ── THE eligibility rule: ONE check, both entry points (operator rule 11-22) ─
# The rule: a node parks only when EVERY model on it lives on that node ALONE.
# Several colocated single-node models park TOGETHER (that is the point of
# parking a machine); ANY multi-node model refuses outright, with no opt-in —
# `--span` is gone and `--force` belongs to the orchestrator guard alone.
# Placeholders only (nolive-rule.txt); the status read is injected.
class TestPauseEligibility:
    """The verdict, through the same injectable seams the guard uses."""

    def _elig(self, host, **payload_kw):
        plan = npe.plan(host, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(host)))
        return npe.pause_eligibility(host, plan)

    def test_one_single_node_unit_is_allowed(self):
        v = self._elig(W4)
        assert v["verdict"] == npe.ELIG_ALLOW
        assert v["units"] == ["w4"] and v["multi_node"] == []
        assert v["lines"] == [], "an allow prints nothing of its own"

    def test_colocated_single_node_units_are_allowed_together(self):
        """The 11-22 amendment: colocated is FINE. Count is not the rule —
        span is. Both models are the pause's, and the refusal machinery must
        not fire just because a second unit shares the machine."""
        v = self._elig(W3)
        assert v["verdict"] == npe.ELIG_ALLOW
        assert v["units"] == ["w3a", "w3b"]
        assert v["multi_node"] == [] and v["peers"] == []

    def test_a_colocated_mix_refuses_and_names_the_multi_node_one(self):
        """W1 runs a single-node unit AND a TP pair: the refusal names the pair
        and its other node — and nothing else about the node is paused."""
        v = self._elig(W1)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["reason"] == npe.REASON_MULTI_NODE
        assert v["multi_node"] == ["wspan"], "the solo unit is not the offender"
        joined = "\n".join(v["lines"])
        assert "wspan" in joined, "the refusal names the model"
        assert W2 in joined, "the refusal names the other node it lives on"
        assert f"other node(s): {W2}" in joined
        assert "multi-node" in joined
        assert v["lines"][-1] == "Nothing stopped and nothing recorded."

    def test_the_refusal_is_the_same_from_the_sibling_node(self):
        """The blast radius reads the same whichever side the operator starts
        from — the sibling names the node left behind."""
        v = self._elig(W2)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["multi_node"] == ["wspan"]
        assert W1 in "\n".join(v["lines"])
        assert v["peers"] == [W1]

    def test_a_span_unit_that_is_alone_on_the_node_still_refuses(self):
        """The case #561/#567 let through with --span: exactly ONE unit on the
        node, but it lives on two nodes. There is no flag for it any more."""
        serving = {"units": [{"id": "wspan", "role": "worker",
                              "nodes": [W1, W2], "port": 8200,
                              "recipe": SPAN_RECIPE,
                              "model": "meta-llm/Work-8B", "tp": 2}]}
        groups = {}
        groups.update(group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2))
        plan = npe.plan(W1, serving_dict=serving,
                        status_reader=reader(status_payload(groups=groups)))
        v = npe.pause_eligibility(W1, plan)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["reason"] == npe.REASON_MULTI_NODE
        assert len(plan["units"]) == 1, "one unit is not what makes it pausable"

    def test_a_span_surviving_a_silent_sibling_is_still_refused(self):
        """meta.hosts says TP pair, only one node answered: the unverified
        sibling is still a node this stop would take down."""
        groups = {}
        groups.update(group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2,
                            observed=[W1]))
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(status_payload(groups=groups)))
        v = npe.pause_eligibility(W1, plan)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["peers"] == [W2] and W2 in "\n".join(v["lines"])

    def test_a_multi_process_unit_on_one_reported_node_is_not_solo(self):
        """tp>1 with a single-node placement: the declaration and the sweep
        disagree, and a model split across processes is the thing this rule
        exists to protect — the safer reading wins."""
        serving = {"units": [{"id": "tp-only", "role": "worker",
                              "nodes": [W4], "port": 8400,
                              "recipe": WORKER_RECIPE,
                              "model": "meta-llm/Work-7B", "tp": 4}]}
        payload = status_payload(groups=dict(
            group("cid_tp", WORKER_RECIPE, [W4], 8400,
                  model="meta-llm/Work-7B", tp=4)))
        v = npe.pause_eligibility(W4, npe.plan(W4, serving_dict=serving,
                                               status_reader=reader(payload)))
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["multi_node"] == ["tp-only"]
        assert "tp=4" in "\n".join(v["lines"])

    def test_a_node_running_nothing_refuses_nothing_to_pause(self):
        """Zero units is NOT a pause: there is nothing to stop. (The pre-11-22
        verb recorded an empty node — the operator retired that.) FOREIGN is a
        host serving.json declares nothing on, so the plan really is empty."""
        plan = npe.plan(FOREIGN, serving_dict=SERVING,
                        status_reader=reader(status_payload()))
        assert plan["ok"] and plan["units"] == []
        v = npe.pause_eligibility(FOREIGN, plan)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["reason"] == npe.REASON_ZERO_UNITS
        assert v["lines"] == [
            f"REFUSED: {FOREIGN} runs nothing — nothing to pause.",
            "Nothing stopped and nothing recorded."]

    def test_an_unreadable_node_is_refused_not_read_as_empty(self):
        """Fail-closed: 'could not enumerate' must not become 'runs nothing' —
        that would turn a dead host into a verified-empty one."""
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(None, "sparkrun status raised"))
        v = npe.pause_eligibility(W1, plan)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert v["reason"] == npe.REASON_PLAN_UNREADABLE
        joined = "\n".join(v["lines"])
        assert "could not be enumerated" in joined
        assert "sparkrun status raised" in joined

    def test_no_force_can_override_the_rule(self):
        """`--force` is the orchestrator guard's, not this one's — a caller
        cannot even hand it in."""
        sig = inspect.signature(npe.pause_eligibility)
        assert "force" not in sig.parameters
        v = self._elig(W1)
        assert v["verdict"] == npe.ELIG_REFUSE
        assert "No flag overrides this" in "\n".join(v["lines"])

    def test_a_handed_plan_means_the_fleet_is_swept_once(self):
        def boom():
            raise AssertionError("eligibility swept the fleet a second time")
        plan = npe.plan(W3, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W3)))
        v = npe.pause_eligibility(W3, plan, status_reader=boom)
        assert v["verdict"] == npe.ELIG_ALLOW

    def test_a_uniform_pool_replica_is_eligible_by_itself(self):
        """Eligibility is orthogonal to the orchestrator guard: a pool replica
        is a single solo unit, so the ELIGIBILITY check allows it and the
        guard is what answers the stays-served question (compose, not merge)."""
        plan = pool_plan(P2)
        assert npe.pause_eligibility(P2, plan)["verdict"] == npe.ELIG_ALLOW

    def test_the_verdict_vocabulary_is_the_guards_own(self):
        """Both checks answer with the same two words and one shared closing
        line, so a caller can print either verdict without re-wording it."""
        assert (npe.ELIG_ALLOW, npe.ELIG_REFUSE) == (npe.GUARD_ALLOW,
                                                     npe.GUARD_REFUSE)


# ── the two commands ───────────────────────────────────────────────────────
class TestCommands:
    def test_stop_cmd_names_every_node_of_a_span_unit(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        # The intent flags ride on the stop too (t_8ad06a53): `--hosts`/`--port`
        # alone make sparkrun recompute a DIFFERENT intent, answer "No running
        # workload matches intent …" and stop nothing.
        assert unit_of(plan, "wspan")["stop_cmd"] == [
            "sparkrun", "stop", SPAN_RECIPE, "--hosts", f"{W1},{W2}",
            "--port", "8200",
            "--served-model-name", "meta-llm/Work-8B worker-model",
            "--tp", "2"]

    def test_resume_cmd_is_the_keepalive_ensure_command(self):
        """Byte-identical to the sanctioned relaunch builder, so --ensure
        re-issues the declared workload instead of deriving another intent id."""
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        cmd = unit_of(plan, "wspan")["resume_cmd"]
        expected = serving._unit_run_cmd(
            dict(SERVING["units"][2], nodes=[W1, W2], port=8200, tp=2))
        assert cmd == expected
        assert "--ensure" in cmd and "--no-follow" in cmd
        assert cmd[:3] == ["sparkrun", "run", SPAN_RECIPE]
        assert "--cluster" in cmd and "hscc" in cmd
        assert cmd[cmd.index("--hosts") + 1] == f"{W1},{W2}"
        assert cmd[cmd.index("--tp") + 1] == "2"

    def test_a_foreign_workload_gets_no_resume_command(self):
        """No serving.json match means the recipe/flags were never recorded —
        an invented launch would duplicate a live unit, so it is refused."""
        payload = status_payload(groups=dict(
            group("cid_foreign", "/srv/foreign.yaml", [FOREIGN], 8400)))
        plan = npe.plan(FOREIGN, serving_dict=SERVING,
                        status_reader=reader(payload))
        u = unit_of(plan, "cid_foreign")
        assert u["resume_cmd"] is None
        assert "not recorded" in u["resume_blocked"]
        assert u["stop_cmd"] is not None

    def test_a_foreign_workload_on_a_declared_node_is_not_matched(self):
        """Node overlap with a declared unit but no port agreement is NOT a
        match — matching on recipe or node alone would hand a foreign container
        another unit's relaunch command."""
        payload = status_payload(groups=dict(
            group("cid_foreign", "/srv/foreign.yaml", [W2], 8400)))
        plan = npe.plan(W2, serving_dict=SERVING, status_reader=reader(payload))
        u = unit_of(plan, "cid_foreign")
        assert u["serving_match"] is False
        assert u["resume_cmd"] is None
        assert "port 8400" in u["resume_blocked"]

    def test_ambiguous_port_match_gets_no_resume_command(self):
        """Two declared units claim the same nodes with no port to separate them
        — unique-match rule (recover._match_by_identity) applies."""
        serving_ambig = {"units": [
            {"id": "a", "role": "worker", "nodes": [W2], "recipe": "/a.yaml"},
            {"id": "b", "role": "worker", "nodes": [W2], "recipe": "/b.yaml"}]}
        payload = status_payload(groups=dict(
            group("cid_a", "/a.yaml", [W2], None)))
        plan = npe.plan(W2, serving_dict=serving_ambig,
                        status_reader=reader(payload))
        u = plan["units"][0]
        assert u["resume_cmd"] is None
        assert "share these nodes" in u["resume_blocked"]

    def test_declared_unit_without_recipe_cannot_be_resumed(self):
        serving_norecipe = {"units": [
            {"id": "nr", "role": "worker", "nodes": [W2], "port": 8500}]}
        payload = status_payload(groups=dict(
            group("cid_nr", "/srv/nr.yaml", [W2], 8500)))
        plan = npe.plan(W2, serving_dict=serving_norecipe,
                        status_reader=reader(payload))
        u = unit_of(plan, "nr")
        assert u["resume_cmd"] is None
        assert "no recipe" in u["resume_blocked"]


# ── stop_units / resume_units: the dry-run rule ────────────────────────────
class TestDryRunIsDefault:
    def _units(self, host=W1):
        return npe.plan(host, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(host)))["units"]

    def test_stop_runs_nothing_without_apply(self):
        rec = Recorder()
        # A run that would RAISE if called: dry-run must never reach it.
        def exploding_run(cmd, timeout=None, **kw):
            raise AssertionError(f"dry-run executed {cmd}")
        lines = Lines()
        res = npe.stop_units(self._units(), run=exploding_run, echo=lines)
        assert res["applied"] is False
        assert res["ok"] is True
        assert rec.calls == []
        assert all(l.startswith("WOULD STOP") for l in lines.lines)

    def test_stop_dry_run_prints_the_span_in_full(self):
        lines = Lines()
        npe.stop_units(self._units(W1), echo=lines)
        span_lines = [l for l in lines.lines if "wspan" in l]
        assert len(span_lines) == 1
        assert W1 in span_lines[0] and W2 in span_lines[0]

    def test_resume_runs_nothing_without_apply(self):
        def exploding_run(cmd, timeout=None, **kw):
            raise AssertionError(f"dry-run executed {cmd}")
        lines = Lines()
        res = npe.resume_units(self._units(W1), run=exploding_run, echo=lines)
        assert res["applied"] is False
        assert all(l.startswith("WOULD RESUME") for l in lines.lines)

    def test_dry_run_marks_an_unobserved_unit_as_a_noop(self):
        groups = {}
        groups.update(group("cid_w1", WORKER_RECIPE, [W1], 8100))
        units = npe.plan(W1, serving_dict=SERVING,
                         status_reader=reader(
                             status_payload(groups=groups)))["units"]
        lines = Lines()
        npe.stop_units(units, echo=lines)
        assert any("not observed" in l for l in lines.lines)


class TestApplyMutates:
    def _units(self, host=W1):
        return npe.plan(host, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(host)))["units"]

    def test_apply_true_runs_one_stop_per_unit(self):
        rec = Recorder()
        res = npe.stop_units(self._units(W1), apply=True, run=rec,
                             echo=Lines())
        assert res["applied"] is True
        assert res["ok"] is True
        assert [c["cmd"][1] for c in rec.calls] == ["stop", "stop"]
        assert all(c["timeout"] == npe.SPARKRUN_CONTROL_TIMEOUT
                   for c in rec.calls)

    def test_apply_true_reports_a_failing_stop(self):
        rec = Recorder(results=[{"ok": False, "output": "",
                                 "stderr": "no such container"}])
        res = npe.stop_units(self._units(W1), apply=True, run=rec,
                             echo=Lines())
        assert res["ok"] is False
        assert any("no such container" == r["error"] for r in res["units"])

    def test_apply_true_reports_a_raising_stop(self):
        class Boom(Recorder):
            def __call__(self, cmd, timeout=None, **kw):
                self.calls.append({"cmd": list(cmd), "timeout": timeout})
                raise OSError("[Errno 2] No such file or directory: 'sparkrun'")
        rec = Boom()
        res = npe.stop_units(self._units(ORCH), apply=True, run=rec,
                             echo=Lines())
        assert res["ok"] is False
        assert "Errno 2" in res["units"][0]["error"]

    def test_resume_apply_runs_the_ensure_command(self):
        rec = Recorder()
        res = npe.resume_units(self._units(W1), apply=True, run=rec,
                               echo=Lines())
        assert res["applied"] is True
        assert all("--ensure" in c["cmd"] for c in rec.calls)

    def test_refused_unit_is_reported_and_nothing_runs(self):
        payload = status_payload(groups=dict(
            group("cid_foreign", "/srv/foreign.yaml", [FOREIGN], 8400)))
        units = npe.plan(FOREIGN, serving_dict=SERVING,
                         status_reader=reader(payload))["units"]
        rec = Recorder()
        lines = Lines()
        res = npe.resume_units(units, apply=True, run=rec, echo=lines)
        assert rec.calls == []
        assert res["ok"] is False
        assert "REFUSED resume" in lines.text

    def test_an_empty_batch_is_a_successful_no_op(self):
        rec = Recorder()
        res = npe.stop_units([], apply=True, run=rec, echo=Lines())
        assert res == {"applied": True, "ok": True, "units": []}
        assert rec.calls == []

    def test_units_survive_a_json_round_trip(self):
        """The CLI writes the pause record to the overlay and resumes from it
        later — so the records must be plain serialisable data."""
        units = self._units(W1)
        revived = json.loads(json.dumps(units))
        assert npe.resume_units(revived, run=Recorder(),
                                echo=Lines())["ok"] is True


# ── the transport, against planted fakes on PATH ───────────────────────────
@pytest.fixture
def fake_fleet(tmp_path, monkeypatch):
    """Planted ``sparkrun`` CLI plus fake ``ssh``/``docker``.

    The CLI's shebang names THIS interpreter and it is pinned through
    ``HSCC_SPARKRUN_BIN``, so ``sparkrun_bin`` resolves sparkrun's "own venv
    python" and every control argv to files this test controls — the same
    resolution the daemon uses, with no operator install in the path. ``ssh``
    and ``docker`` are PATH hijacks; the CLI deliberately is NOT on PATH,
    because the sparkrun fence identifies the operator's live CLI through
    ``shutil.which`` — shadowing it with a fake would make the fence classify
    the fake as the live one and block it.

    All three append to one call log, so an attempt to reach a host (the
    forbidden shape) is recorded rather than executed, and a test can assert
    on it.
    """
    import shutil

    bin_dir = tmp_path / "planted-bin"
    bin_dir.mkdir()
    net_dir = tmp_path / "planted-netbin"
    net_dir.mkdir()
    call_log = tmp_path / "calls.log"
    call_log.write_text("")

    cli = bin_dir / "sparkrun"
    # The fake is STRICTER than the real CLI in one way, on purpose
    # (t_09708c17): a literal '~' in any argv token exits non-zero, the way
    # sparkrun answers ``Recipe '~…' not found`` when a shell never expanded it.
    # Every recipe-bearing argv in this feature must therefore be tilde-free
    # before it reaches a child — that is DEFECT 1's regression pin, and the
    # fake's bite is proven by TestTildeIsRejectedByTheFakeCli.
    # ``FAKE_HANG`` reproduces DEFECT 3: a client that never returns while the
    # container it launched is already serving. It records its pid into
    # ``FAKE_PID_FILE`` the moment it reaches the hang, which is how a test can
    # tell "the client got as far as hanging" (the stand-in for the unit being
    # up and answering) from "the client never started".
    cli.write_text("#!" + sys.executable + "\n"
                   "import os, sys, time\n"
                   "with open(os.environ['FAKE_CALL_LOG'], 'a') as f:\n"
                   "    f.write('sparkrun ' + ' '.join(sys.argv[1:]) + '\\n')\n"
                   "tilde = [a for a in sys.argv[1:] if '~' in a]\n"
                   "if tilde:\n"
                   "    sys.stderr.write('Recipe not found: %s\\n' % tilde[0])\n"
                   "    sys.exit(3)\n"
                   "if os.environ.get('FAKE_HANG'):\n"
                   "    if os.environ.get('FAKE_PID_FILE'):\n"
                   "        with open(os.environ['FAKE_PID_FILE'], 'w') as f:\n"
                   "            f.write(str(os.getpid()))\n"
                   "    time.sleep(float(os.environ['FAKE_HANG']))\n")
    cli.chmod(0o755)
    assert os.path.realpath(shutil.which("sparkrun") or "") != \
        os.path.realpath(str(cli)), \
        "the planted CLI must stay off PATH — the fence identifies the live " \
        "CLI with shutil.which and would mis-classify the fake"
    for name in ("ssh", "docker"):
        script = net_dir / name
        script.write_text("#!/bin/sh\n"
                          "printf '%s ' \"%s\" >> \"$FAKE_CALL_LOG\"\n"
                          "printf '\\n' >> \"$FAKE_CALL_LOG\"\n"
                          "exit 0\n")
        script.chmod(0o755)

    monkeypatch.setenv("PATH", str(net_dir) + os.pathsep
                       + os.environ.get("PATH", ""))
    monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
    monkeypatch.setenv("FAKE_CALL_LOG", str(call_log))
    sparkrun_bin.reset_cache()
    yield {"log": call_log, "cli": cli, "bin": bin_dir}
    sparkrun_bin.reset_cache()


def _log_lines(call_log):
    return call_log.read_text().splitlines()


class TestTransportIsSparkrunOnly:
    """The transport contract: health's sanctioned structured query, and
    nothing else.

    The status payload is deliberately NEVER exec'd here — the shared sparkrun
    fence blocks any argv carrying sparkrun's payload markers, because on the
    operator's host that payload drives the LIVE fleet. So the contract is
    pinned two ways instead: the exact argv + interpreter the module hands to
    ``run_cmd`` (this class's first test), and a REAL child for the lifecycle
    command, against the planted fake CLI (the fence allows a fake that
    resolves under tmp_path).
    """

    def test_status_query_reuses_healths_sanctioned_transport(
            self, fake_fleet, monkeypatch):
        """sparkrun's OWN venv python running health's status script — the
        same two lines ``recover.fetch_identity_entries`` runs. A second
        transport, a hardcoded interpreter, or a re-implemented query all fail
        here."""
        from hscc_daemon import health as health_mod
        calls = []

        def fake_run(args, timeout=None, **kw):
            calls.append({"args": list(args), "timeout": timeout})
            return {"ok": True, "output": json.dumps(payload_for_host(W1)),
                    "stderr": ""}
        monkeypatch.setattr(util, "run_cmd", fake_run)

        plan = npe.plan(W1, serving_dict=SERVING)
        assert plan["ok"] is True
        assert sorted(u["unit_id"] for u in plan["units"]) == ["w1", "wspan"]
        assert len(calls) == 1
        assert calls[0]["args"] == [sparkrun_bin.sparkrun_venv_python(), "-c",
                                    health_mod._SPARKRUN_STATUS_SCRIPT]
        assert calls[0]["timeout"] == health_mod.SPARKRUN_STATUS_TIMEOUT
        # The interpreter came from the planted fake CLI's shebang through
        # sparkrun_bin — not from a PATH guess or a literal.
        assert calls[0]["args"][0] == sys.executable

    def test_applied_stop_executes_the_resolved_fake_cli(self, fake_fleet):
        """apply=True must actually shell out — via the PATH-independent sparkrun
        resolution, not a bare name that would die with Errno 2 under a
        service-supervised PATH. The status read is injected; the CONTROL command
        is deliberately not, so the exec path is the production one."""
        units = npe.plan(W1, serving_dict=SERVING,
                         status_reader=reader(payload_for_host(W1)))["units"]
        npe.stop_units([unit_of(units, "wspan")], apply=True, echo=Lines())
        stops = [l for l in _log_lines(fake_fleet["log"])
                 if l.startswith("sparkrun stop")]
        assert len(stops) == 1
        assert SPAN_RECIPE in stops[0]
        assert f"--hosts {W1},{W2}" in stops[0]

    def test_no_ssh_and_no_docker_are_ever_executed(self, fake_fleet):
        """The nolive rule, pinned mechanically: with fake ssh/docker first on
        PATH, an applied stop must leave neither in the call log."""
        units = npe.plan(W1, serving_dict=SERVING,
                         status_reader=reader(payload_for_host(W1)))["units"]
        res = npe.stop_units(units, apply=True, echo=Lines())
        assert res["applied"] is True
        log = fake_fleet["log"].read_text()
        assert "ssh " not in log
        assert "docker" not in log
        # and the stops really did run through the planted CLI
        assert len([l for l in log.splitlines()
                    if l.startswith("sparkrun stop")]) == 2

    def test_dry_run_executes_no_sparkrun_control_call(self, fake_fleet):
        units = npe.plan(W1, serving_dict=SERVING,
                         status_reader=reader(payload_for_host(W1)))["units"]
        npe.stop_units(units, echo=Lines())
        npe.resume_units(units, echo=Lines())
        log = fake_fleet["log"].read_text()
        assert "sparkrun stop" not in log
        assert "sparkrun run" not in log


class TestNoLocalDockerFallback:
    """The v1.15.0 failure class: a local ``docker ps`` answers *empty*, not
    *error*, so a docker-based reader fails open. Nothing in this module's code
    may shell out to docker or drive ssh directly — and a failed status read
    must say so rather than return an empty fleet."""

    @staticmethod
    def _code_only(src):
        """Source with comments and string literals tokenised away.

        Prose is allowed to NAME the thing it forbids (the docstring explains
        why docker is wrong); the executable code is not.
        """
        import io
        import tokenize
        keep = []
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            keep.append(tok.string)
        return " ".join(keep)

    def test_module_code_names_neither_docker_nor_ssh_nor_subprocess(self):
        code = self._code_only(inspect.getsource(npe))
        for forbidden in ("docker", "ssh", "subprocess"):
            assert forbidden not in code, (
                f"node_pause_exec code references {forbidden!r} — fleet access "
                "goes through sparkrun's status path only")

    def test_status_reader_is_the_only_fleet_dependency(self):
        """The transport seam is exactly one call: health's script under
        sparkrun's venv python, via util.run_cmd."""
        code = self._code_only(inspect.getsource(npe))
        assert "_SPARKRUN_STATUS_SCRIPT" in code
        assert "_sparkrun_venv_python" in code


# ── t_09708c17: the resume argv must BE the launch argv ────────────────────
#: A uniform_pool node, as the operator's live report describes it. RFC 5737;
#: the recipe name is a fake (the operator's real one must not be committed).
#: Prefixed RESUME_ rather than POOL_ because this file already has a
#: uniform_pool fixture set (POOL_SERVING/pool_plan) that these must not shadow.
RESUME_NODE = "192.0.2.16"
RESUME_RECIPE_TILDE = \
    "~/sparkrun-local/recipes/local-fixed/lab-flash-solo-vllm.yaml"
RESUME_RECIPE_ABS = os.path.expanduser(RESUME_RECIPE_TILDE)
RESUME_SERVED = "lab/Flash-Next-NVFP4 worker-model orchestrator-model"
#: The unit AS serving.json stores it for a uniform_pool fleet: a ``~`` recipe,
#: and no per-unit ``uniform_pool`` key (that flag is top-level, and the resume
#: record is the unit as stored — which is exactly why the field rebuild dropped
#: the orchestrator alias).
RESUME_UNIT = {"id": "pool1", "role": "worker", "nodes": [RESUME_NODE],
               "port": 8003, "recipe": RESUME_RECIPE_TILDE,
               "model": "lab/Flash-Next-NVFP4", "tp": 1, "max_num_seqs": 5}
#: The argv the template apply really launched (the shape
#: ``cluster_template._render_serve_cmd`` records).
RESUME_LAUNCH = ["sparkrun", "run", RESUME_RECIPE_ABS, "--cluster", "hscc",
                 "--hosts", RESUME_NODE, "--port", "8003", "--no-follow",
                 "--ensure", "--served-model-name", RESUME_SERVED, "--tp", "1",
                 "-o", "max_num_seqs=5"]


def resume_pool_payload():
    groups = {}
    groups.update(group("cid_pool", RESUME_RECIPE_ABS, [RESUME_NODE], 8003,
                        model="lab/Flash-Next-NVFP4", served=RESUME_SERVED,
                        tp=1))
    return status_payload(groups=groups, idle=[RESUME_NODE])


def resume_pool_plan(serve_cmd=None):
    """The plan for the uniform_pool node, with or without a stored record."""
    unit = dict(RESUME_UNIT)
    if serve_cmd is not None:
        unit["serve_cmd"] = list(serve_cmd)
    return npe.plan(RESUME_NODE,
                    serving_dict={"version": 3, "uniform_pool": True,
                                  "units": [unit]},
                    status_reader=reader(resume_pool_payload()))


class TestResumeReplaysTheRecordedLaunchArgv:
    """The reported live resume, at the executor seam (DEFECTS 1 and 2)."""

    def _unit(self, serve_cmd=None):
        return unit_of(resume_pool_plan(serve_cmd), "pool1")

    def test_resume_cmd_is_the_recorded_launch_argv_byte_for_byte(self):
        """The acceptance pin: what resume runs == what was originally run."""
        u = self._unit(RESUME_LAUNCH)
        assert u["resume_cmd"] == RESUME_LAUNCH
        assert " ".join(u["resume_cmd"]).encode("utf-8") == \
            " ".join(RESUME_LAUNCH).encode("utf-8")
        assert "~" not in " ".join(u["resume_cmd"])
        # All THREE names ride, so --ensure re-issues the same workload instead
        # of starting a different one and dropping the node out of the pool.
        assert serving._flag_after(u["resume_cmd"],
                                   "--served-model-name") == RESUME_SERVED

    def test_the_record_is_what_carries_the_alias_the_fields_cannot(self):
        """Rebuilt from this unit's own fields, UNSTAMPED, the argv serves TWO
        names — the defect as reported. The record is where the third name is
        still available for a stored uniform_pool unit, which is the operator's
        reason resume replays it.

        (``_unit_run_cmd`` is called here without the ``uniform_pool`` stamp
        because that is what the reported path did and what any caller outside
        the pause plan gets; the plan itself stamps it, pinned next door.)
        """
        u = self._unit(RESUME_LAUNCH)
        rebuilt = serving._unit_run_cmd(dict(RESUME_UNIT,
                                             nodes=[RESUME_NODE], port=8003,
                                             tp=1))
        assert serving._flag_after(rebuilt, "--served-model-name") == \
            "lab/Flash-Next-NVFP4 worker-model"
        assert u["resume_cmd"] != rebuilt

    def test_a_unit_with_no_record_still_gets_an_openable_path(self):
        """DEFECT 1 is fixed on the fallback too, not only on the record path:
        a ``~`` in serving.json must never reach a child process.

        The other half of what this case used to leave open is now CLOSED, by
        the placement stamp t_8ad06a53 landed on main: the pause builds its
        resume from the same dict as its stop, and that dict carries
        ``uniform_pool`` from the serving top level, so a pool unit with no
        recorded serve_cmd rebuilds ALL THREE names rather than the two its own
        role field can derive. Pinned here because it is a composition of two
        cards' fixes — neither side's test would catch the other being dropped.
        """
        u = self._unit()
        assert "~" not in " ".join(u["resume_cmd"])
        assert u["resume_cmd"][2] == RESUME_RECIPE_ABS
        assert serving._flag_after(u["resume_cmd"],
                                   "--served-model-name") == RESUME_SERVED


class TestTildeIsRejectedByTheFakeCli:
    """DEFECT 1's regression pin at the transport: the planted CLI refuses a
    literal ``~`` the way sparkrun does, so an argv that still carries one
    cannot pass — through the production exec path, not a mocked one."""

    def test_the_planted_cli_rejects_a_literal_tilde(self, fake_fleet):
        """The pin bites: same CLI, same argv shape, tilde in ⇒ exit 3."""
        import subprocess
        bad = subprocess.run([str(fake_fleet["cli"]), "run",
                              RESUME_RECIPE_TILDE], capture_output=True,
                             text=True, timeout=60)
        assert bad.returncode == 3
        assert "Recipe not found" in bad.stderr
        good = subprocess.run([str(fake_fleet["cli"]), "run",
                               RESUME_RECIPE_ABS], capture_output=True,
                              text=True, timeout=60)
        assert good.returncode == 0

    def test_an_applied_resume_never_hands_the_cli_a_literal_tilde(
            self, fake_fleet):
        """The real path: unit stored with a ``~`` recipe, no record ⇒ derived
        argv, executed through ``util.run_cmd``'s sparkrun resolution. The strict
        fake answers 0 only because the path arrived expanded."""
        u = unit_of(resume_pool_plan(), "pool1")
        res = npe.resume_units([u], apply=True, echo=Lines(),
                               serving_probe=lambda node, port: False)
        assert res["ok"] is True, res["units"][0]["error"]
        log = fake_fleet["log"].read_text()
        runs = [line for line in log.splitlines()
                if line.startswith("sparkrun run")]
        assert len(runs) == 1
        assert "~" not in log
        assert RESUME_RECIPE_ABS in runs[0]

    def test_the_pin_bites_an_argv_that_still_carries_a_tilde(self,
                                                             fake_fleet):
        """The control for the case above: a resume argv that DOES carry the
        tilde fails through the same call, so the green case is not vacuous."""
        broken = list(RESUME_LAUNCH)
        broken[2] = RESUME_RECIPE_TILDE
        units = [{"unit_id": "pool1", "nodes": [RESUME_NODE], "port": 8003,
                  "resume_cmd": broken}]
        res = npe.resume_units(units, apply=True, echo=Lines(),
                               serving_probe=lambda node, port: False)
        assert res["ok"] is False
        assert "Recipe not found" in res["units"][0]["error"]


# ── t_09708c17 DEFECT 3: a wedged launch client must not wedge the resume ───
class TestBoundedLaunchWait:
    """``sparkrun run`` is a CLIENT, and a client can sit on its child ssh
    session for tens of minutes after the container it launched is serving
    (the operator's live launch: serving in ~10 min, client cut loose at ~40).

    So the resume's success signal is the unit answering its own ``/v1/models``,
    the wait is bounded, and a client that had to be cut loose is reported as
    such — never a silent kill, never a false "relaunch failed".
    """

    @staticmethod
    def _units(nodes=(RESUME_NODE,), port=8003):
        return [{"unit_id": "pool1", "nodes": list(nodes), "port": port,
                 "resume_cmd": RESUME_LAUNCH, "observed": False}]

    @staticmethod
    def _wedged_client(cmd, timeout=None, ready=None, **kw):
        """A ``run`` standing in for a client that never returns: the only ways
        out are the probe turning true or the bound expiring."""
        deadline = time.monotonic() + (timeout or 0)
        while time.monotonic() < deadline:
            if ready is not None and ready():
                return {"ok": True, "output": "", "stderr": "",
                        "detached": True, "timed_out": False,
                        "verified_by": "health probe"}
            time.sleep(0.02)
        return {"ok": False, "output": f"Command timed out after {timeout}s",
                "stderr": "", "detached": True, "timed_out": True}

    def test_a_wedged_client_is_cut_loose_and_the_probe_proves_the_restart(
            self, fake_fleet, monkeypatch, tmp_path):
        """The card's case: a REAL child that never exits, and a fake container
        that answers only once it is up.

        The planted CLI hangs forever and writes its pid the moment it reaches
        the hang — the stand-in for "the container has come up and answers
        ``/v1/models``", so the probe is driven by the fake unit, not by the
        test waving it true.
        """
        monkeypatch.setenv("FAKE_HANG", "300")
        up = tmp_path / "wedged-pid"
        monkeypatch.setenv("FAKE_PID_FILE", str(up))
        # A bound well above the ~2 s the fake takes to reach its hang, and far
        # below production's: a regression that ignores the probe fails the
        # early-exit assertion in 30 s instead of 240.
        monkeypatch.setattr(npe, "SPARKRUN_LAUNCH_WAIT_TIMEOUT", 30)
        asked = []

        def probe(node, port):
            asked.append((str(node), port))
            return up.exists()

        lines = Lines()
        started = time.monotonic()
        res = npe.resume_units(self._units(), apply=True, echo=lines,
                               serving_probe=probe)
        assert res["ok"] is True, res["units"][0]["error"]
        assert res["units"][0]["detached"] is True
        assert res["units"][0]["verified_by"] == "health probe"
        assert res["units"][0]["error"] is None
        # The client really was the planted fake, really reached its hang, and
        # really ran the argv the record supplied.
        assert up.exists(), "the client never reached its hang"
        assert res["units"][0]["cmd"] == RESUME_LAUNCH
        assert [l for l in fake_fleet["log"].read_text().splitlines()
                if l.startswith("sparkrun run")]
        # The probe ended the wait, not the bound — and no real endpoint was
        # asked: the probe only ever saw the placeholder node and its port.
        assert time.monotonic() - started < 10
        assert asked and set(asked) == {(RESUME_NODE, 8003)}
        assert any("cut loose" in line and "health probe" in line
                   for line in lines.lines), lines.text
        assert any("not by the client" in line for line in lines.lines)
        # The signal reached the CLIENT and nothing else: the fake is gone.
        with pytest.raises(ProcessLookupError):
            os.kill(int(up.read_text()), 0)

    def test_a_wedged_client_with_no_answer_is_unconfirmed_never_a_hang(
            self, fake_fleet, monkeypatch):
        """Same child, unit never answers: the resume fails — and says what it
        cut loose and what it could not prove, rather than hanging or quietly
        killing."""
        monkeypatch.setenv("FAKE_HANG", "300")
        monkeypatch.setattr(npe, "SPARKRUN_LAUNCH_WAIT_TIMEOUT", 3)
        lines = Lines()
        started = time.monotonic()
        res = npe.resume_units(self._units(), apply=True, echo=lines,
                               serving_probe=lambda node, port: False)
        assert res["ok"] is False
        assert time.monotonic() - started < 30
        row = res["units"][0]
        assert row["detached"] is True
        assert "timed out after 3s" in row["error"]
        assert any("NOT confirmed" in line for line in lines.lines), lines.text

    def test_the_launch_gets_the_launch_bound_and_the_probe(self):
        """The plumbing contract on the ``run`` seam: resume passes the bounded
        launch wait plus the readiness probe; a stop does not."""
        calls = []

        def run(cmd, timeout=None, ready=None, **kw):
            calls.append({"timeout": timeout, "ready_given": ready is not None})
            return {"ok": True, "output": "", "stderr": ""}

        npe.resume_units(self._units(), apply=True, echo=Lines(), run=run,
                         serving_probe=lambda node, port: True)
        assert calls == [{"timeout": npe.SPARKRUN_LAUNCH_WAIT_TIMEOUT,
                          "ready_given": True}]
        calls.clear()
        npe.stop_units([{"unit_id": "pool1", "nodes": [RESUME_NODE],
                         "port": 8003,
                         "stop_cmd": ["sparkrun", "stop", RESUME_RECIPE_ABS]}],
                       apply=True, echo=Lines(), run=run)
        assert calls == [{"timeout": npe.SPARKRUN_CONTROL_TIMEOUT,
                          "ready_given": False}]

    def test_a_span_is_only_verified_when_every_node_it_spans_answers(self,
                                                                       monkeypatch):
        """Declaring a restart verified while one node of the same model is
        down would be a lie, so all of them have to answer."""
        monkeypatch.setattr(npe, "SPARKRUN_LAUNCH_WAIT_TIMEOUT", 3)

        def probe(node, port):
            return str(node) == W1

        row = npe.resume_units(self._units(nodes=(W1, W2)), apply=True,
                               echo=Lines(), run=self._wedged_client,
                               serving_probe=probe)["units"][0]
        assert row["ok"] is False and row["detached"] is True

    def test_a_unit_the_probe_cannot_ask_about_keeps_the_plain_wait(self):
        """No port ⇒ no probe ⇒ the control-call bound and today's shape."""
        calls = []

        def run(cmd, timeout=None, ready=None, **kw):
            calls.append({"timeout": timeout, "ready_given": ready is not None})
            return {"ok": True, "output": "", "stderr": ""}

        units = [{"unit_id": "pool1", "nodes": [RESUME_NODE], "port": None,
                  "resume_cmd": RESUME_LAUNCH}]
        assert npe.resume_units(units, apply=True, echo=Lines(),
                                run=run)["ok"] is True
        assert calls == [{"timeout": npe.SPARKRUN_CONTROL_TIMEOUT,
                          "ready_given": False}]

    def test_the_probe_is_a_plain_get_of_the_units_own_endpoint(
            self, monkeypatch):
        """``unit_is_serving`` asks the unit, over HTTP, and nothing else: not
        the proxy, not sparkrun, not an alias comparison."""
        asked = []

        def fake_http_check(url, timeout=5):
            asked.append(url)
            return {"ok": True, "status": 200}

        monkeypatch.setattr(util, "http_check", fake_http_check)
        assert npe.unit_is_serving(RESUME_NODE, 8003) is True
        assert asked == [f"http://{RESUME_NODE}:8003/v1/models"]

    def test_an_endpoint_that_is_not_200_is_not_serving(self, monkeypatch):
        monkeypatch.setattr(util, "http_check",
                            lambda url, timeout=5: {"ok": True, "status": 503})
        assert npe.unit_is_serving(RESUME_NODE, 8003) is False
        monkeypatch.setattr(util, "http_check",
                            lambda url, timeout=5: {"ok": False,
                                                    "output": "refused"})
        assert npe.unit_is_serving(RESUME_NODE, 8003) is False

        def explode(url, timeout=5):
            raise OSError("no route to host")

        monkeypatch.setattr(util, "http_check", explode)
        assert npe.unit_is_serving(RESUME_NODE, 8003) is False, \
            "a probe that cannot ask must answer 'not up', never raise"

