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

import pytest

from hscc_daemon import node_pause_exec as npe
from hscc_daemon import serving, sparkrun_bin, util

# ── the fixture fleet (placeholders only) ──────────────────────────────────
ORCH = "192.0.2.11"
W1 = "192.0.2.12"
W2 = "192.0.2.13"
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


# ── the two commands ───────────────────────────────────────────────────────
class TestCommands:
    def test_stop_cmd_names_every_node_of_a_span_unit(self):
        plan = npe.plan(W1, serving_dict=SERVING,
                        status_reader=reader(payload_for_host(W1)))
        assert unit_of(plan, "wspan")["stop_cmd"] == [
            "sparkrun", "stop", SPAN_RECIPE, "--hosts", f"{W1},{W2}",
            "--port", "8200"]

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
    cli.write_text("#!" + sys.executable + "\n"
                   "import os, sys\n"
                   "with open(os.environ['FAKE_CALL_LOG'], 'a') as f:\n"
                   "    f.write('sparkrun ' + ' '.join(sys.argv[1:]) + '\\n')\n")
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

