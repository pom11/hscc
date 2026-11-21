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

import io
import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

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
        seed_pause()
        heal_breaker.note_heal(W1)
        rc, _ = run(["resume", W1, "--apply"])
        assert rc == 0
        assert heal_breaker.strike_count(W1) == 0
        assert heal_breaker.suspended(W1) == 0.0
