"""Tests for `hscc node pause|resume|paused` (card t_c28f4f14).

Hermetic by construction — the same rule the overlay and executor tests follow
(``nolive-rule.txt``): every host is a documentation placeholder (192.0.2.x,
RFC 5737); ``serving.load_serving``, ``node_pause_exec.fetch_cluster_status``
and ``util.run_cmd`` are all patched, so no test here reads the operator's
serving.json, runs sparkrun's status query, or spawns a single subprocess. The
autouse ``_isolate_hscc`` conftest fixture already seals ``node_pauses.HSCC_DIR``
and ``heal_breaker.HEAL_BREAKER_FILE``, so every overlay/strike write lands in
this test's tmp dir.

What this file owns that the executor's tests do not: the CLI contract.
Dry-run-by-default (#478) asserted as "no argv reached ``run_cmd``", the
orchestrator refusal, the ``--span`` opt-in, overlay-written-AFTER-stop,
``-h`` never reaching a mutating verb (B10), and the ``--until`` typo that
``parse_until`` would otherwise swallow as open-ended.
"""

import ast
import io
import json
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import heal_breaker, node_pause_exec as npe
from hscc_daemon import node_pauses, serving, util

# ── the fixture fleet (placeholders only) ──────────────────────────────────
ORCH = "192.0.2.11"
W1 = "192.0.2.12"
W2 = "192.0.2.13"
#: Declared by no serving.json unit and running nothing — the empty node.
QUIET = "192.0.2.30"

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


def _group(cid, recipe, hosts, port, model="meta-llm/Work-8B", served=None,
           tp=None):
    meta = {"recipe": recipe, "hosts": list(hosts), "port": port,
            "model": model}
    if served:
        meta["served_model_name"] = served
    if tp:
        meta["tensor_parallel"] = tp
    return {cid: {"meta": meta,
                  "containers": [{"host": h, "role": "worker",
                                  "status": "running", "image": "img"}
                                 for h in hosts]}}


def fleet_payload():
    """One status snapshot of the whole fixture fleet."""
    groups = {}
    groups.update(_group("cid_orch", ORCH_RECIPE, [ORCH], 8000,
                         model="meta-llm/Orch-8B",
                         served="meta-llm/Orch-8B orchestrator-model"))
    groups.update(_group("cid_w1", WORKER_RECIPE, [W1], 8100,
                         served="meta-llm/Work-8B worker-model"))
    groups.update(_group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2,
                         served="meta-llm/Work-8B worker-model"))
    return {"groups": groups, "solo_entries": [], "errors": {},
            "idle_hosts": [], "preparing_hosts": [], "pending_ops": [],
            "total_containers": 0, "host_count": 0}


class Recorder:
    """Fake ``util.run_cmd``: records argv, answers ok unless told otherwise."""

    def __init__(self, results=()):
        self.calls = []
        self._results = list(results)

    def __call__(self, cmd, timeout=None, **kw):
        self.calls.append([str(c) for c in cmd])
        if self._results:
            return self._results.pop(0)
        return {"ok": True, "output": "", "stderr": ""}


@pytest.fixture
def fleet(monkeypatch):
    """Patch the three seams a node verb can reach: config, status, exec."""
    rec = Recorder()
    monkeypatch.setattr(serving, "load_serving", lambda path=None: SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        lambda: (fleet_payload(), None))
    monkeypatch.setattr(util, "run_cmd", rec)
    return rec


def run(argv):
    """Drive the verb group in-process; returns (rc, stdout)."""
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            rc = unified._handle_node(list(argv))
    except SystemExit as e:  # handlers must RETURN, never exit
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


def main_run(monkeypatch, argv):
    """Drive ``main()`` so the dispatch branch itself is exercised."""
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            unified.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


def seed_pause(host=W1, **kw):
    """Write a pause entry straight to the (sealed) overlay."""
    kw.setdefault("reason", "maintenance")
    kw.setdefault("units", ["w1", "wspan"])
    kw.setdefault("span_nodes", [W1, W2])
    kw.setdefault("stopped", True)
    return node_pauses.pause(host, **kw)


# ── help is inert: it loads nothing, reads nothing, stops nothing ──────────
class TestHelpIsInert:
    """The B6/B10/B12 contract for this group: a help token can never reach a
    verb that stops live units."""

    @pytest.fixture(autouse=True)
    def _no_plan(self, monkeypatch):
        """Any status read at all is a failure on these paths."""
        def boom(*a, **kw):
            raise AssertionError("a help request must not read the fleet")
        monkeypatch.setattr(npe, "plan", boom)
        monkeypatch.setattr(npe, "fetch_cluster_status", boom)

    @pytest.mark.parametrize("argv", [[], ["--help"], ["-h"]])
    def test_group_help_lists_all_three_verbs(self, argv):
        rc, out = run(argv)
        assert rc == 0, out
        assert "Node pause commands:" in out
        assert "hscc node pause <host> [flags]" in out
        assert "hscc node resume <host> [--apply]" in out
        assert "hscc node paused [--json]" in out
        # The dry-run default is advertised, not implied (#478).
        assert "DRY-RUN by default" in out

    def test_a_leading_flag_is_not_a_subcommand(self):
        """Same refusal shape as `hscc cluster --whatever`: the token is not a
        verb, and a flag is never quietly read as a request for help."""
        rc, out = run(["--theme", "dark"])
        assert rc == 1, out
        assert "unknown node subcommand: --theme" in out
        assert "pause, resume, paused" in out

    @pytest.mark.parametrize("argv", [
        ["pause", "--help"], ["pause", "-h"],
        ["pause", W1, "--help"], ["pause", "--apply", "--force", "--help"],
        ["pause", "--apply", W1, "-h"],
        ["resume", "--help"], ["resume", W1, "--help"], ["resume", "-h"],
        ["paused", "--help"], ["paused", "--json", "-h"],
    ])
    def test_verb_help_wins_over_dispatch(self, argv):
        """A help token ANYWHERE in the tail prints usage and runs nothing."""
        rc, out = run(argv)
        assert rc == 0, out
        assert out.startswith("Usage: hscc node "), out
        assert not node_pauses.list_pauses(), "help must not record a pause"

    def test_short_flag_and_long_flag_print_the_same_bytes(self):
        for verb in ("pause", "resume", "paused"):
            long_rc, long_out = run([verb, "--help"])
            short_rc, short_out = run([verb, "-h"])
            assert (long_rc, long_out) == (short_rc, short_out)

    def test_unknown_subcommand_refuses_and_lists_the_verbs(self):
        rc, out = run(["bogus"])
        assert rc == 1
        assert "unknown node subcommand: bogus" in out
        assert "pause, resume, paused" in out

    def test_main_dispatches_the_group(self, monkeypatch):
        """`hscc node` resolves through main(), not only the handler."""
        rc, out = main_run(monkeypatch, ["node", "--help"])
        assert rc == 0, out
        assert "hscc node paused [--json]" in out

    def test_help_node_documents_the_dry_run_default(self, monkeypatch):
        """`hscc help node` prints the same COMMAND_HELP blurb the audit reads."""
        rc, out = main_run(monkeypatch, ["help", "node"])
        assert rc == 0, out
        assert "Subcommands: pause <host>  resume <host>  paused" in out

    def test_primary_help_names_the_verb(self, monkeypatch):
        """F5 surface honesty: a dispatched TOP_LEVEL_COMMAND is listed."""
        rc, out = main_run(monkeypatch, ["--help"])
        assert rc == 0, out
        assert "\n  node " in out


# ── `node paused` — read-only, and provably fleet-free ─────────────────────
class TestPaused:
    def test_empty_overlay_is_not_an_error(self, fleet):
        rc, out = run(["paused"])
        assert rc == 0, out
        assert "No paused nodes" in out
        assert fleet.calls == []

    def test_never_reads_the_fleet(self, monkeypatch):
        """`paused` answers with sparkrun and serving both unreachable."""
        def boom(*a, **kw):
            raise AssertionError("paused must not read the fleet")
        monkeypatch.setattr(npe, "plan", boom)
        monkeypatch.setattr(serving, "load_serving", boom)
        seed_pause()
        rc, out = run(["paused"])
        assert rc == 0, out
        assert W1 in out

    def test_prints_the_recorded_fields(self):
        seed_pause(reason="disk swap", until="2h")
        rc, out = run(["paused"])
        assert rc == 0, out
        assert W1 in out
        assert "w1" in out and "wspan" in out
        assert "disk swap" in out
        assert "confirmed down" in out
        for field in ("since:", "until:"):
            assert field in out
        # The span names the node that is parked but not keyed.
        assert W2 in out

    def test_open_until_says_what_ends_it(self):
        seed_pause()
        rc, out = run(["paused"])
        assert rc == 0, out
        assert "open-ended" in out

    def test_past_due_until_is_flagged(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        seed_pause(until=past)
        rc, out = run(["paused"])
        assert rc == 0, out
        assert "PAST DUE" in out
        assert f"hscc node resume {W1} --apply" in out

    def test_json_is_one_machine_readable_object(self):
        seed_pause(reason="disk swap", until="2h")
        rc, out = run(["paused", "--json"])
        assert rc == 0, out
        payload = json.loads(out)
        assert payload["count"] == 1
        row = payload["pauses"][0]
        assert row["host"] == W1
        assert row["units"] == ["w1", "wspan"]
        assert row["reason"] == "disk swap"
        assert row["stopped"] is True
        assert row["since"] and row["until"]
        assert row["span_nodes"] == [W1, W2]

    def test_json_of_nothing_is_a_zero_count(self):
        rc, out = run(["paused", "--json"])
        assert rc == 0, out
        assert json.loads(out) == {"count": 0, "pauses": []}

    def test_unknown_flag_is_refused_not_dropped(self):
        rc, out = run(["paused", "--verbose"])
        assert rc == 1, out
        assert "unknown flag: --verbose" in out


# ── `node pause` — dry-run default, refusals, then stop + record ───────────
class TestPauseDryRun:
    def test_default_stops_nothing_and_records_nothing(self, fleet):
        rc, out = run(["pause", W1])
        assert rc == 0, out
        assert fleet.calls == [], "dry run must not shell out"
        assert node_pauses.paused(W1) is None
        assert "nothing stopped" in out

    def test_dry_run_prints_every_unit_and_every_affected_node(self, fleet):
        rc, out = run(["pause", W1])
        assert rc == 0, out
        assert "w1" in out and "wspan" in out
        assert "every node this affects:" in out
        assert W2 in out, "the span sibling must be printed, not hidden"

    def test_no_units_is_reported_as_such(self, fleet):
        rc, out = run(["pause", QUIET])
        assert rc == 0, out
        assert "0 unit(s)" in out
        assert "nothing to stop" in out

    def test_missing_host_is_a_usage_error(self, fleet):
        rc, out = run(["pause"])
        assert rc == 1, out
        assert "needs exactly one host" in out


class TestPauseRefusals:
    def test_orchestrator_host_refuses_with_the_reason(self, fleet):
        rc, out = run(["pause", ORCH])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "orchestrator model" in out
        # The WHY names the unit the plan actually read — not a model name.
        assert "orch" in out
        assert fleet.calls == []
        assert node_pauses.paused(ORCH) is None

    def test_orchestrator_host_needs_force_to_apply(self, fleet):
        rc, out = run(["pause", ORCH, "--apply", "--force"])
        assert rc == 0, out
        assert fleet.calls, "--force --apply must stop"
        entry = node_pauses.paused(ORCH)
        assert entry is not None and entry["stopped"] is True

    def test_span_needs_the_span_flag_opt_in(self, fleet):
        rc, out = run(["pause", W1, "--apply"])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "wspan" in out
        assert W2 in out, "the refusal names the node it would also hit"
        assert fleet.calls == [], "a refused pause must not stop anything"
        assert node_pauses.paused(W1) is None

    def test_span_flag_without_apply_is_a_warning_not_a_stop(self, fleet):
        rc, out = run(["pause", W1, "--span"])
        assert rc == 0, out
        assert "--span without --apply" in out
        assert fleet.calls == []

    def test_unparseable_until_refuses_instead_of_parking_forever(self, fleet):
        """node_pauses.parse_until degrades garbage to open-ended; a CLI typo
        must not silently become a node that never comes back."""
        rc, out = run(["pause", W1, "--apply", "--span", "--until", "2days"])
        assert rc == 1, out
        assert "not a duration" in out
        assert fleet.calls == []
        assert node_pauses.paused(W1) is None

    def test_dangling_value_flag_is_refused(self, fleet):
        rc, out = run(["pause", W1, "--until"])
        assert rc == 1, out
        assert "--until needs a value" in out

    def test_theme_is_refused_because_it_would_do_nothing(self, fleet):
        rc, out = run(["pause", W1, "--theme", "dark"])
        assert rc == 1, out
        assert "--theme" in out

    def test_failed_status_read_is_not_read_as_an_empty_node(self, fleet,
                                                             monkeypatch):
        monkeypatch.setattr(npe, "fetch_cluster_status",
                            lambda: (None, "sparkrun status query failed"))
        rc, out = run(["pause", W1, "--apply", "--span"])
        assert rc == 1, out
        assert "cannot enumerate" in out
        assert fleet.calls == []
        assert node_pauses.paused(W1) is None


# ── the uniform pool: EVERY replica advertises the orchestrator alias ───────
# The defect this section pins (#567 → t_51a11d16, the operator's live dry-run):
# ``serving._served_model_name`` stamps BOTH role aliases on every unit of a
# uniform pool (hscc_daemon/serving.py:444-464), so a per-unit "serves the
# orchestrator model" flag is True on EVERY node — and the verb refused the
# whole fleet, making the feature's first real use require --force. The applied
# template declares ONE orchestrator_node; the rest are interchangeable behind
# the proxy. Every host here is an RFC 5737 placeholder (nolive-rule.txt).
POOL_A = "192.0.2.21"   # the applied template's declared orchestrator_node
POOL_B = "192.0.2.22"
POOL_C = "192.0.2.23"
POOL_D = "192.0.2.24"
POOL_NODES = (POOL_A, POOL_B, POOL_C, POOL_D)
POOL_RECIPE = "/srv/recipes/pool.yaml"
POOL_MODEL = "meta-llm/Pool-8B"
#: Both aliases on one unit — what a uniform-pool replica actually advertises.
#: ``hscc-cluster/templates/4node/flash-next-all.yaml`` says it outright:
#: "every unit advertises BOTH ``orchestrator-model`` AND ``worker-model``, so
#: each alias load-balances across ALL FOUR units and nothing cares which node
#: is nominally the orchestrator".
POOL_SERVED = f"{POOL_MODEL} worker-model orchestrator-model"
#: A uniform pool gives every unit a DISTINCT port (8000..8003) so proxy
#: discovery does not collapse the replicas into one backend — the fixture
#: carries that shape rather than four units sharing :8000.
POOL_PORTS = tuple(8000 + i for i in range(len(POOL_NODES)))

POOL_SERVING = {
    "port": POOL_PORTS[0],
    "uniform_pool": True,
    "units": [{"id": f"pool-{i}",
               "role": "orchestrator" if i == 0 else "worker",
               "nodes": [h], "port": p, "recipe": POOL_RECIPE,
               "model": POOL_MODEL, "tp": 1}
              for i, (h, p) in enumerate(zip(POOL_NODES, POOL_PORTS))],
}


def pool_payload(down=()):
    """One sweep of the pool; ``down`` hosts are absent from it entirely."""
    groups = {}
    for i, (h, p) in enumerate(zip(POOL_NODES, POOL_PORTS)):
        if h in down:
            continue
        groups.update(_group(f"cid_pool_{i}", POOL_RECIPE, [h], p,
                             model=POOL_MODEL, served=POOL_SERVED))
    return {"groups": groups, "solo_entries": [], "errors": {},
            "idle_hosts": [], "preparing_hosts": [], "pending_ops": [],
            "total_containers": 0, "host_count": 0}


def write_applied_state(tmp_path, node, template="4node-flash-next-all"):
    """The applied-template record at the path the SANCTIONED reader uses.

    The conftest redirects ``~/.hscc`` to this test's tmp dir, so writing the
    real file here makes ``declaration_agreement.load_applied_state`` — the one
    reader the guard is allowed to use — find a real record. Nothing is patched
    on the reader itself, which is the point: a guard that re-parsed the file by
    hand would pass a patched-seam test and fail on a real apply. The template
    name is the one the operator's live dry-run was refused on.
    """
    path = tmp_path / "hscc" / "applied_template.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "template": template,
        "applied_at": "2026-11-22T00:00:00",
        "orchestrator_node": node,
        "uniform_pool": True,
        "units": len(POOL_NODES),
    }))
    return path


@pytest.fixture
def pool(monkeypatch, tmp_path):
    """Uniform-pool fleet + an applied record naming POOL_A the orchestrator."""
    rec = Recorder()
    monkeypatch.setattr(serving, "load_serving", lambda path=None: POOL_SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        lambda: (pool_payload(), None))
    monkeypatch.setattr(util, "run_cmd", rec)
    write_applied_state(tmp_path, POOL_A)
    return rec


def park(*hosts):
    """Hold nodes with the pause OVERLAY (parked, still answering the sweep).

    The distinction the guard must get right: a parked replica is not a healthy
    server, and it is not the same state as a node missing from the sweep. The
    autouse conftest fixture has already redirected ``node_pauses.HSCC_DIR`` to
    this test's tmp dir, so this writes an overlay entry and nothing else.
    """
    for h in hosts:
        node_pauses.pause(h, units=[f"pool-{POOL_NODES.index(h)}"],
                          reason="maintenance")


class TestUniformPoolOrchestratorGuard:
    """The four acceptance cases the operator's dry-run could not get past."""

    def test_pausing_a_replica_is_allowed_and_says_who_still_serves_it(
            self, pool):
        rc, out = run(["pause", POOL_B])
        assert rc == 0, out
        assert "REFUSED" not in out, out
        # 4 healthy replicas, this one goes down → 3 others keep the alias up.
        assert "orchestrator-model stays served by 3 other node(s)" in out, out
        for other in (POOL_A, POOL_C, POOL_D):
            assert other in out, f"{other} must be named as a remaining server"
        assert pool.calls == [], "a dry run must not shell out"
        assert node_pauses.paused(POOL_B) is None

    def test_declared_orchestrator_node_refuses_with_the_declared_reason(
            self, pool):
        rc, out = run(["pause", POOL_A])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "declared orchestrator node" in out, out
        # The WHY cites the record the reader actually read, not a model name.
        assert "orchestrator_node=" in out and POOL_A in out, out
        assert "4node-flash-next-all" in out, "the refusal names the applied template"
        assert pool.calls == []
        assert node_pauses.paused(POOL_A) is None

    def test_sole_healthy_server_refuses_even_though_replicas_exist(
            self, pool, monkeypatch):
        """Others DOWN (missing from the sweep), not paused: this host is the
        only healthy alias server left, so the pause must be refused."""
        monkeypatch.setattr(
            npe, "fetch_cluster_status",
            lambda: (pool_payload(down={POOL_A, POOL_C, POOL_D}), None))
        rc, out = run(["pause", POOL_B])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "would leave no healthy unit" in out, out
        assert pool.calls == []
        assert node_pauses.paused(POOL_B) is None

    def test_parked_replicas_are_not_counted_as_healthy_servers(self, pool,
                                                                monkeypatch):
        """Others PAUSED (still in the sweep), not down: the overlay wins."""
        park(POOL_A, POOL_C, POOL_D)
        rc, out = run(["pause", POOL_B])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "would leave no healthy unit" in out, out
        # The refusal must explain that the visible replicas are parked, or the
        # operator reads a healthy fleet and an unexplained refusal.
        assert "paused and therefore not counted" in out, out
        assert POOL_C in out and POOL_D in out, out
        assert pool.calls == []

    def test_force_overrides_the_declared_node_refusal(self, pool):
        rc, out = run(["pause", POOL_A, "--apply", "--force"])
        assert rc == 0, out
        assert "note: --force given" in out, out
        assert "declared orchestrator node" in out, out
        assert pool.calls, "--force --apply must stop"
        entry = node_pauses.paused(POOL_A)
        assert entry is not None and entry["stopped"] is True

    def test_force_overrides_the_no_healthy_server_refusal(self, pool,
                                                          monkeypatch):
        monkeypatch.setattr(
            npe, "fetch_cluster_status",
            lambda: (pool_payload(down={POOL_A, POOL_C, POOL_D}), None))
        rc, out = run(["pause", POOL_B, "--apply", "--force"])
        assert rc == 0, out
        assert "note: --force given" in out, out
        assert "would leave no healthy unit" in out, out
        assert pool.calls, "--force --apply must stop"

    def test_the_note_is_a_dry_run_verdict_too(self, pool, monkeypatch):
        """The ALLOW note is part of the plan's answer, not of the stop: it must
        print with no --apply (the operator's dry-run is what was broken)."""
        rc, out = run(["pause", POOL_C])
        assert rc == 0, out
        assert "Dry run: nothing stopped" in out
        assert "orchestrator-model stays served by" in out

    def test_a_plain_pause_prints_no_orchestrator_note(self, fleet):
        """Non-uniform template, worker node: unchanged behaviour — no note, no
        refusal, and no claim about a model this node does not serve."""
        rc, out = run(["pause", QUIET])
        assert rc == 0, out
        assert "orchestrator" not in out.lower(), out

    def test_non_uniform_sole_orchestrator_still_refuses(self, fleet):
        """Non-uniform template (one dedicated orchestrator unit, no applied
        record): the healthy-count rule reproduces the old refusal."""
        rc, out = run(["pause", ORCH])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "would leave no healthy unit" in out, out
        assert fleet.calls == []

    def test_no_applied_record_is_reported_not_silently_ignored(self, pool,
                                                                monkeypatch,
                                                                tmp_path):
        """A fleet with no applied-template record (hscc-bootstrap writes
        serving.json by hand) must not lose the healthy-count guard, and must
        say out loud that the declaration could not be consulted."""
        (tmp_path / "hscc" / "applied_template.json").unlink()
        rc, out = run(["pause", POOL_B])
        assert rc == 0, out
        assert "orchestrator-model stays served by 3 other node(s)" in out, out
        assert "applied-template record could not be consulted" in out, out


class TestPauseApply:
    def test_apply_stops_every_unit_then_records_the_overlay(self, fleet):
        rc, out = run(["pause", W1, "--apply", "--span", "--reason",
                       "disk swap", "--until", "2h"])
        assert rc == 0, out
        assert len(fleet.calls) == 2, fleet.calls
        assert all("stop" in " ".join(c) for c in fleet.calls)
        entry = node_pauses.paused(W1)
        assert entry is not None
        assert entry["units"] == ["w1", "wspan"]
        assert entry["span_nodes"] == [W1, W2]
        assert entry["reason"] == "disk swap"
        assert entry["stopped"] is True
        assert entry["since"] and entry["until"]
        # --until 2h reached the overlay as a deadline ~2h out, not open-ended.
        until = datetime.fromisoformat(entry["until"])
        lead = (until - datetime.now(timezone.utc)).total_seconds()
        assert 3400 < lead < 7400, entry["until"]

    def test_reason_survives_spaces_as_one_token(self, fleet):
        rc, _ = run(["pause", W1, "--apply", "--span", "--reason",
                     "replace the disk"])
        assert rc == 0, _
        assert node_pauses.paused(W1)["reason"] == "replace the disk"

    def test_stop_happens_before_the_overlay_is_written(self, fleet,
                                                        monkeypatch):
        """Ordering is the overlay's documented contract: a pause recorded
        before the stop would let a reader believe units are down that are not.
        """
        seen = {}
        real_pause = node_pauses.pause

        def spy_pause(*a, **kw):
            seen["argv_at_record_time"] = list(fleet.calls)
            return real_pause(*a, **kw)
        monkeypatch.setattr(node_pauses, "pause", spy_pause)
        rc, _ = run(["pause", W1, "--apply", "--span"])
        assert rc == 0
        assert seen["argv_at_record_time"], "recorded before anything stopped"

    def test_a_failing_stop_records_stopped_false_and_exits_nonzero(self,
                                                                     fleet,
                                                                     monkeypatch):
        monkeypatch.setattr(util, "run_cmd",
                            Recorder([{"ok": False, "output": "",
                                       "stderr": "sparkrun stop failed"}]))
        rc, out = run(["pause", W1, "--apply", "--span"])
        assert rc == 1, out
        assert "did not stop cleanly" in out
        entry = node_pauses.paused(W1)
        # The pause IS in force (the heal paths must skip the node); only the
        # "units confirmed down" claim is withheld.
        assert entry is not None and entry["stopped"] is False

    def test_overlay_write_failure_is_reported_as_failure(self, fleet,
                                                          monkeypatch):
        def boom(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(node_pauses, "save", boom)
        rc, out = run(["pause", W1, "--apply", "--span"])
        assert rc == 1, out
        assert "did not reach disk" in out
        assert len(fleet.calls) == 2, "the stop really did happen — say so"

    def test_pausing_a_quiet_node_still_parks_it(self, fleet):
        """No units is a legitimate pause: the point is that heal stops here."""
        rc, out = run(["pause", QUIET, "--apply"])
        assert rc == 0, out
        assert fleet.calls == []
        entry = node_pauses.paused(QUIET)
        assert entry is not None and entry["units"] == []
        assert entry["stopped"] is True


# ── `node resume` — restart the recorded span, then clear ─────────────────
class TestResume:
    def test_unpaused_host_is_a_no_op(self, fleet):
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert "not paused" in out
        assert fleet.calls == []

    def test_span_sibling_points_at_the_owner_instead_of_acting(self, fleet):
        seed_pause()  # keyed W1, spans W2
        rc, out = run(["resume", W2, "--apply"])
        assert rc == 1, out
        assert "span of the pause on" in out and W1 in out
        assert fleet.calls == []
        assert node_pauses.paused(W1) is not None, "must not clear the owner"

    def test_dry_run_prints_the_ensure_command_and_changes_nothing(
            self, fleet):
        seed_pause()
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert fleet.calls == [], "dry run must not restart anything"
        assert "WOULD RESUME" in out
        assert "--ensure" in out, "resume re-issues the declared workload"
        assert node_pauses.paused(W1) is not None

    def test_apply_restarts_then_clears_pause_and_strikes(self, fleet):
        # Past the breaker's cap, so the node is genuinely SUSPENDED before the
        # resume — clearing three strikes would prove nothing (HEAL_BREAKER_MAX
        # is 6; suspended() is 0.0 below it). The slate is earned BEFORE the
        # pause: #568 made note_heal a no-op on a node the overlay holds, so a
        # post-pause slate could never trip the breaker. That a slate earned
        # while healing survives the pause is exactly the contract resume clears.
        for _ in range(7):
            heal_breaker.note_heal(W1)
        assert heal_breaker.strike_count(W1) == 7
        assert heal_breaker.suspended(W1) > 0.0, \
            "the fixture must trip the breaker for this to mean anything"
        seed_pause()
        # The pause must be in force at the moment of the resume, so this is a
        # real "clear strikes THROUGH a pause" case, not a plain unparked resume.
        assert node_pauses.paused(W1) is not None
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 0, out
        assert len(fleet.calls) == 2, fleet.calls
        assert all("--ensure" in " ".join(c) for c in fleet.calls)
        assert node_pauses.paused(W1) is None, "pause must be cleared"
        assert heal_breaker.strike_count(W1) == 0, "strikes must be cleared"
        assert heal_breaker.suspended(W1) == 0.0
        assert "healing resumes now" in out

    def test_a_failing_restart_keeps_the_pause_and_the_strikes(self, fleet,
                                                               monkeypatch):
        # One strike earned BEFORE the pause (note_heal is a no-op on a node the
        # overlay holds since #568). This test guards that a FAILED restart keeps
        # BOTH the pause and that strike — heal_breaker.clear() runs only after a
        # successful restart, so clearing the strike here would be the bug.
        heal_breaker.note_heal(W1)
        assert heal_breaker.strike_count(W1) == 1, (
            "the strike must be EARNED before the pause — without this pin "
            "the test cannot show the slate the failed resume keeps was "
            "actually accrued pre-pause")
        seed_pause()
        # The pause must be in force at the moment of the resume, or "keeps the
        # pause" would be vacuous.
        assert node_pauses.paused(W1) is not None
        monkeypatch.setattr(util, "run_cmd",
                            Recorder([{"ok": False, "output": "",
                                       "stderr": "ensure failed"}]))
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 1, out
        assert "failed to restart" in out
        assert node_pauses.paused(W1) is not None, (
            "a half-started node stays parked")
        assert heal_breaker.strike_count(W1) == 1, (
            "a failed resume must NOT clear the strike it did not earn")

    def test_a_unit_with_no_rebuildable_command_refuses_and_keeps_pause(
            self, fleet):
        seed_pause(units=["w1", "vanished"])
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 1, out
        assert "REFUSED" in out
        assert "vanished" in out
        assert fleet.calls == [], "never guess a launch command"
        assert node_pauses.paused(W1) is not None

    def test_a_pause_with_no_units_clears_on_apply(self, fleet):
        seed_pause(units=[], span_nodes=[])
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert "nothing to restart" in out
        assert node_pauses.paused(W1) is not None, "dry run keeps it"
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 0, out
        assert fleet.calls == []
        assert node_pauses.paused(W1) is None

    def test_failed_status_read_keeps_the_pause(self, fleet, monkeypatch):
        seed_pause()
        monkeypatch.setattr(npe, "fetch_cluster_status",
                            lambda: (None, "sparkrun status query failed"))
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 1, out
        assert "cannot rebuild" in out
        assert node_pauses.paused(W1) is not None

    def test_resume_without_a_recorded_reason_still_clears_strikes(self,
                                                                   fleet):
        """Clearing is unconditional on a successful restart: a paused node
        that comes back must not still be carrying old strikes."""
        # Strike BEFORE the park, and prove it landed. With the pause seeded
        # first this case was green on main while proving nothing: note_heal is
        # a no-op on a node the overlay holds (#568), so the slate it "cleared"
        # had never been written and both assertions below held on an empty
        # ledger. Same law as the two tests above, same reason.
        heal_breaker.note_heal(W1)
        assert heal_breaker.strike_count(W1) == 1, (
            "the strike must be EARNED before the pause — without this pin the "
            "resume below clears a slate nobody recorded")
        seed_pause()
        assert node_pauses.paused(W1) is not None, \
            "the pause must be in force for the resume, or this is no resume"
        rc, _ = run(["resume", W1, "--apply"])
        assert rc == 0
        assert heal_breaker.strike_count(W1) == 0
        assert heal_breaker.suspended(W1) == 0.0


# ── the fixture-ordering class that made these tests red ───────────────────
class TestNoStrikeAccrualBehindAPause:
    """Guard, not a behaviour test: no fixture in THIS file may expect heal
    strikes to accrue for a node its own setup has already parked.

    ``heal_breaker.note_heal()`` deliberately accrues NOTHING while the
    node-pause overlay covers the node — no strike, no ledger write, ``tripped``
    False (#568; its own docstring, pinned by
    ``test_heal_breaker_node_pause_t_e6ee7e60.py``). A fixture that seeds the
    pause BEFORE the strikes therefore builds no slate at all, and every later
    ``strike_count``/``suspended`` assertion reads a ledger nothing wrote: two
    tests here were red on main that way and a third passed vacuously, and the
    tool tests picked up the same shape from the same merge order. This guard is
    why the class cannot come back quietly.

    It reads THIS file's own source, so the pin travels with the fixtures and
    needs no production seam, and it fires only where a test DEMANDS a non-empty
    slate (``strike_count(...) == n`` with n non-zero, ``["count"] == n``, or
    ``suspended(...) > x``). A test asserting the ABSENCE of accrual for a parked
    node is the contract rather than a violation, so ``strike_count(...) == 0``
    stays perfectly legal to write — that is what keeps this guard from ever
    eating the pin it is standing on.

    Matching is host-keyed, so a fixture may legitimately park one node and keep
    striking another. A pause seeded with no host argument (``seed_pause()``
    parks this file's default) matches any host rather than nothing.
    """

    # Leaf names: what parks a node, what earns a strike, what reads the slate.
    PARKING = ("pause", "seed_pause", "park", "node_pause")
    ACCRUING = "note_heal"
    READS = ("strike_count", "suspended")

    @staticmethod
    def _leaf(call):
        """``heal_breaker.note_heal`` / ``seed_pause`` -> the last dotted name."""
        node, leaf = call.func, ""
        while isinstance(node, ast.Attribute):
            leaf, node = node.attr, node.value
        if not leaf and isinstance(node, ast.Name):
            leaf = node.id
        return leaf

    @classmethod
    def _host(cls, call, source):
        """Source text of the host a call parks or strikes: the first positional
        argument, else a ``host=`` keyword, else ``"*"``. A pause seeded with
        neither (``seed_pause()``) parks this file's default host, so it has to
        match any host rather than none."""
        if call.args:
            return ast.get_source_segment(source, call.args[0])
        for kw in call.keywords:
            if kw.arg == "host":
                return ast.get_source_segment(source, kw.value)
        return "*"

    @staticmethod
    def _same(a, b):
        """Same host? Compares NAME TOKENS, not substrings, so `W1` is never
        mistaken for a `W10`/`W12`, while a dict argument such as
        ``{"host": W1}`` still matches `W1`."""
        if "*" in (a, b):
            return True
        tokens = lambda text: {  # noqa: E731 - tiny local helper, used twice
            t for t in re.findall(r"\d+\.\d+\.\d+\.\d+|[A-Za-z_]\w*", text)}
        return bool(tokens(a) & tokens(b))

    @staticmethod
    def _demands(comparison):
        """True when the comparison REQUIRES a non-empty slate: ``> x``, ``!= n``,
        or ``== n`` with a truthy n. ``== 0`` is the suppression contract."""
        consts = [c.value for c in comparison.comparators
                  if isinstance(c, ast.Constant)
                  and isinstance(c.value, (int, float))]
        if not consts:
            return False
        if any(isinstance(op, (ast.Gt, ast.GtE)) for op in comparison.ops):
            return True
        if any(isinstance(op, (ast.Lt, ast.LtE)) for op in comparison.ops):
            return False
        return (any(isinstance(op, ast.NotEq) for op in comparison.ops)
                or any(consts))

    def _demanded_hosts(self, fn, source):
        """Hosts this test demands a non-empty slate for."""
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Compare) or not self._demands(node):
                continue
            left = node.left
            if isinstance(left, ast.Call) and self._leaf(left) in self.READS:
                out.append(self._host(left, source))
            elif (isinstance(left, ast.Subscript)
                  and isinstance(left.slice, ast.Constant)
                  and left.slice.value == "count"
                  and isinstance(left.value, ast.Call)
                  and self._leaf(left.value) == self.ACCRUING):
                out.append(self._host(left.value, source))
        return out

    def test_no_fixture_accrues_strikes_behind_its_own_pause(self):
        source = Path(__file__).read_text(encoding="utf-8")
        bad = []
        for fn in (n for n in ast.walk(ast.parse(source))
                   if isinstance(n, ast.FunctionDef)
                   and n.name.startswith("test_")):
            calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
            parks = [(self._host(c, source), c.lineno) for c in calls
                     if self._leaf(c) in self.PARKING]
            heals = [(self._host(c, source), c.lineno) for c in calls
                     if self._leaf(c) == self.ACCRUING]
            if not parks or not heals:
                continue
            demanded = self._demanded_hosts(fn, source)
            for park_host, park_line in parks:
                for heal_host, heal_line in heals:
                    if heal_line <= park_line:
                        continue
                    if not self._same(park_host, heal_host):
                        continue
                    if any(self._same(h, heal_host) for h in demanded):
                        bad.append(
                            f"{fn.name}:{park_line} parks the node, then expects "
                            f"note_heal at line {heal_line} (host {heal_host}) to "
                            f"accrue — a parked node accrues nothing (#568); "
                            f"accrue the slate first, then park")
        assert bad == [], "\n".join(bad)


# ── one resume policy, two callers (card t_110adc1a) ────────────────────────
class TestResumePolicyIsShared:
    """`hscc node resume` and the daemon's expiry tick must run the SAME code.

    The tick resumes what the operator scheduled, so any policy the verb holds
    that the tick lacks (or the other way round) is a node that comes back under
    one caller and stays parked under the other. Both were therefore pointed at
    :func:`node_pause_exec.resume_host`, and these tests are what keep that
    arrangement from quietly decaying back into two copies:

    * the behavioural one drives the verb with the executor's policy replaced, so
      if the verb ever resumes for itself again the fake never fires and the test
      goes red — and it pins the exit-code mapping (``ok`` → rc) in both
      directions, which is the only thing the verb still decides;
    * the source one reads the verb's own body and refuses to let the resume
      primitives come back into it.
    """

    def test_the_verb_calls_the_executors_resume_policy(self, fleet,
                                                        monkeypatch):
        seen = []

        def fake_resume(host, entry, **kw):
            seen.append((host, dict(entry), kw))
            return {"ok": True, "action": npe.RESUME_RESUMED}

        monkeypatch.setattr(npe, "resume_host", fake_resume)
        seed_pause()
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 0, out
        assert len(seen) == 1, seen
        host, entry, kw = seen[0]
        assert host == W1
        assert entry["units"] == ["w1", "wspan"], (
            "the verb hands over the overlay entry it selected, not a re-read")
        assert kw == {"apply": True}, (
            "--apply is how the verb says 'do it'; the tick passes apply too")
        assert fleet.calls == [], (
            "the verb no longer restarts anything by itself — resume_host does")

    def test_a_dry_run_ask_is_passed_through_as_apply_false(self, fleet,
                                                           monkeypatch):
        seen = []
        monkeypatch.setattr(
            npe, "resume_host",
            lambda host, entry, **kw: seen.append(kw) or {
                "ok": True, "action": npe.RESUME_DRY_RUN})
        seed_pause()
        rc, out = run(["resume", W1])
        assert rc == 0, out
        assert seen == [{"apply": False}], seen

    def test_a_kept_pause_exits_nonzero(self, fleet, monkeypatch):
        """The verb's whole remaining decision is ok → exit code, so pin both
        directions: a kept pause (restart failed / refused) must be a red rc, or
        a script that parks and resumes would believe a failed restart."""
        monkeypatch.setattr(
            npe, "resume_host",
            lambda host, entry, **kw: {"ok": False,
                                       "action": npe.RESUME_KEPT})
        seed_pause()
        rc, out = run(["resume", W1, "--apply"])
        assert rc == 1, out

    def test_the_policy_primitives_are_not_in_the_verb(self):
        """Source pin: the verb owns argument grammar, the span-owner answer and
        the exit code — nothing else. If ``resume_units`` / ``heal_breaker.clear``
        / ``node_pauses.clear`` show up in its body again, someone re-inlined the
        policy and the daemon tick is now running a different one."""
        import inspect
        body = ast.parse(inspect.getsource(unified._node_resume))
        banned = {"resume_units", "stop_units", "plan", "clear",
                  "reset_alert", "resume_host"}
        called = {node.func.attr for node in ast.walk(body)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and node.func.attr in banned}
        assert called == {"resume_host"}, (
            f"_node_resume calls {sorted(called)}; only resume_host belongs "
            f"there — the rest is policy and lives in node_pause_exec")


# ── ONE orchestrator guard, both entry points (card t_51a11d16) ────────────
class TestOrchestratorGuardIsShared:
    """`hscc node pause` owns NO orchestrator decision of its own.

    #567 put the refusal inline in the verb. The agent tool inherited it by
    calling the verb — one copy, but a copy that could only see one host, which
    is why it refused a whole uniform pool. The replacement is ONE function in
    ``node_pause_exec`` and the verb prints its verdict; these two tests are the
    teeth behind that "one shared check" requirement. Delete the call and the
    behavioural refusal tests go red; re-inline any part of the decision and the
    source pin goes red.
    """

    def test_the_verb_asks_the_guard_and_prints_only_its_lines(self, pool,
                                                               monkeypatch):
        seen = []
        monkeypatch.setattr(
            npe, "orchestrator_pause_guard",
            lambda host, **kw: seen.append((host, kw)) or {
                "verdict": npe.GUARD_REFUSE, "reason": "pinned", "forced": False,
                "lines": ["REFUSED: pinned by the test"], "others": []})
        rc, out = run(["pause", POOL_B])
        assert rc == 1, out
        assert out.endswith("REFUSED: pinned by the test\n"), out
        assert len(seen) == 1, seen
        host, kw = seen[0]
        assert host == POOL_B
        assert kw["force"] is False
        assert kw["plan"]["host"] == POOL_B, (
            "the verb hands the guard the plan it already computed — a guard "
            "that swept the fleet again could disagree with the plan printed "
            "above it, and doubles the sparkrun read")
        assert pool.calls == [] and node_pauses.paused(POOL_B) is None, \
            "a refused pause stops nothing, whoever decided the refusal"

    def test_the_verb_holds_no_orchestrator_policy_of_its_own(self):
        """Source pin: the verb reads the guard's verdict and prints its lines.

        The word ``orchestrator`` may not appear in any string the verb decides
        on, and the guard's own primitives may not be called from it — the moment
        either returns, there are two answers to "may I park this node?" and the
        agent tool is running whichever one the verb forgot to update.
        """
        import inspect
        body = ast.parse(inspect.getsource(unified._node_pause))
        called = {n.func.attr for n in ast.walk(body)
                  if isinstance(n, ast.Call) and isinstance(n.func,
                                                            ast.Attribute)}
        assert "orchestrator_pause_guard" in called, \
            "the verb no longer asks the guard — the guard is now dead code"
        banned = {"orchestrator_flag", "orchestrator_alias_units",
                  "orchestrator_nodes", "paused_nodes"}
        assert not (called & banned), (
            f"_node_pause calls {sorted(called & banned)}; that is policy and "
            "lives in node_pause_exec")
        read = {n.attr for n in ast.walk(body) if isinstance(n, ast.Attribute)}
        assert "serves_orchestrator_model" not in read, \
            "the plan flag is advisory; the guard is the decision"
        doc = ast.get_docstring(body) or ""
        # Comments are not in the AST, so this scans the strings the verb could
        # print or compare against — prose about the delegation is welcome.
        said = [n.value for n in ast.walk(body)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value != doc]
        offending = [s for s in said if "orchestrator" in s.lower()]
        assert not offending, (
            f"the verb strings {offending}; the refusal wording belongs to the "
            "guard so both entry points say the same thing")
