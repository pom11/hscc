"""A2 — one-vLLM-per-host launch guard (HEAL_CASCADE 2026-10-14, F7 follow-up).

The invariant (operator decision 2026-10-14): before ANY launch path starts a
container on a host, ask sparkrun status what is already there. A vLLM workload
OUTSIDE the launching unit's declared span -> REFUSE + ALERT (ERROR log + macOS
notification + events.jsonl row), and NEVER stop the foreign workload.
"Status unavailable" is also a refusal — that morning every status surface
reported success while an orphan held :4000, so "could not look" must not read
as "the host is free".

Fakes only: nothing here touches sparkrun, ssh, docker, or the live fleet.
Every status payload below is a literal dict shaped exactly like
ClusterStatusResult.to_dict() (groups[*].meta.port / containers[*].host,
solo_entries[*].host/meta.port — the same keys _sparkrun_created_map reads).
"""

import copy

import pytest

from hscc_daemon import health
from hscc_daemon import launch_guard as lg

H1 = "10.0.0.11"
H2 = "10.0.0.12"
H3 = "10.0.0.13"
VLLM_R = "~/recipes/qwen-solo-vllm.yaml"
OTHER_R = "~/recipes/embed-vllm.yaml"


def solo(host, port, recipe=VLLM_R, cid="sparkrun_aaa"):
    return {"cluster_id": cid, "host": host, "status": "Up 2 hours",
            "image": "img", "meta": {"port": port, "recipe": recipe}}


def status(*entries, errors=None):
    return {"groups": {}, "solo_entries": list(entries),
            "errors": errors or {}, "idle_hosts": [], "preparing_hosts": [],
            "pending_ops": [], "total_containers": len(entries),
            "host_count": 4}


DECLARED = {(H1, 8000), (H2, 8001), (H3, 8002)}


class TestPolicy:
    """launch_guard.evaluate — pure, no I/O."""

    def test_foreign_vllm_on_same_host_refuses(self):
        st = status(solo(H2, 8001), solo(H2, 9100, recipe=OTHER_R))
        ok, reason, detail = lg.evaluate(st, H2, 8001, [H2], declared=DECLARED)
        assert ok is False and reason == lg.REASON_FOREIGN
        assert detail["foreign"][0]["port"] == 9100
        assert ":9100" in detail["error"]

    def test_foreign_workload_on_ANOTHER_host_does_not_refuse(self):
        """The invariant is per-host. A foreign workload elsewhere must not
        block this host — a guard with false positives gets disabled."""
        st = status(solo(H2, 8001), solo(H3, 9100))
        ok, _, _ = lg.evaluate(st, H2, 8001, [H2], declared=DECLARED)
        assert ok is True

    def test_declared_colocation_is_allowed(self):
        """A host may legitimately run orchestrator :8000 AND an embed vLLM
        :8010 on one host. Co-location the serving layer DECLARES is not a
        violation — the guard refuses only what is outside the declared span,
        or it would refuse a working deployment on its first day."""
        st = status(solo(H1, 8000), solo(H1, 8010, recipe=OTHER_R))
        declared = DECLARED | {(H1, 8010)}
        ok, _, _ = lg.evaluate(st, H1, 8000, [H1], declared=declared)
        assert ok is True

    def test_own_container_being_replaced_is_allowed(self):
        """A relaunch finds the very container it is replacing still Up at its
        own (host, port). That is the unit's OWN slot, not a stranger."""
        st = status(solo(H2, 8001))
        ok, _, _ = lg.evaluate(st, H2, 8001, [H2], declared=DECLARED)
        assert ok is True

    def test_tp_span_peer_slots_are_allowed(self):
        """A TP unit spans several hosts; its live containers sit on the
        peers, not only on the host being launched."""
        span = [H1, H2]
        st = status(solo(H1, 8000), solo(H2, 8000))
        ok, _, _ = lg.evaluate(st, H2, 8000, span,
                               declared=DECLARED | {(H1, 8000), (H2, 8000)})
        assert ok is True

    def test_no_status_is_a_refusal(self):
        ok, reason, detail = lg.evaluate(lg.NO_STATUS, H1, 8000, [H1],
                                        declared=DECLARED)
        assert ok is False and reason == lg.REASON_NO_STATUS
        assert "could not look" in detail["error"]

    def test_empty_status_means_empty_fleet_not_unavailable(self):
        """{} == 'we looked, nothing is running' — the distinction the whole
        design rests on. {} must PASS (a fleet-up from nothing is the normal
        case), NO_STATUS must not."""
        ok, _, _ = lg.evaluate({}, H1, 8000, [H1], declared=DECLARED)
        assert ok is True

    def test_per_host_error_is_a_refusal_even_though_host_looks_empty(self):
        """An unreachable host trivially shows no workloads — the guard must
        refuse on the errors map BEFORE concluding 'nothing is there', or the
        hung nodes of 2026-10-14 would have read as 'free'."""
        st = status(solo(H2, 8001), errors={H1: "ssh timeout"})
        ok, reason, _ = lg.evaluate(st, H1, 8000, [H1], declared=DECLARED)
        assert ok is False and reason == lg.REASON_NO_STATUS

    def test_non_vllm_workload_is_not_the_invariants_business(self):
        """A CPU job sharing the node does not claim the whole GPU. Refusing
        model servers because a postgres container exists would make the guard
        useless — and the first person to hit it would disable it."""
        st = status(solo(H2, 5432, recipe="~/recipes/postgres.yaml"))
        ok, _, _ = lg.evaluate(st, H2, 8001, [H2], declared=DECLARED)
        assert ok is True

    def test_unknown_recipe_counts_as_vllm(self):
        """An unlabelled workload is exactly the orphan-proxy shape — an
        untracked listener holding a port nobody declared. Fail closed."""
        st = status({"cluster_id": "sparkrun_orphan", "host": H2,
                     "status": "Up", "image": "?", "meta": {"port": 9999}})
        ok, reason, _ = lg.evaluate(st, H2, 8001, [H2], declared=DECLARED)
        assert ok is False and reason == lg.REASON_FOREIGN

    def test_group_shape_is_read_too(self):
        """Multi-host/TP groups carry port+recipe on group.meta and one
        containers[*] entry per member host — the guard must see foreign
        workloads reported through THAT shape too, not just solo entries."""
        st = {"groups": {"sparkrun_grp": {
                "meta": {"port": 9200, "recipe": OTHER_R},
                "containers": [{"host": H2, "role": "worker",
                                "status": "Up", "image": "i"}]}},
              "solo_entries": [], "errors": {}}
        ok, reason, detail = lg.evaluate(st, H2, 8001, [H2], declared=DECLARED)
        assert ok is False and reason == lg.REASON_FOREIGN
        assert detail["foreign"][0]["port"] == 9200

    def test_status_with_no_declared_context_uses_own_slot_only(self):
        """declared=None is the strictest reading (only the launching unit's
        own footprint is allowed): a co-located workload that the caller could
        not vouch for is refused, not waved through."""
        st = status(solo(H1, 8000), solo(H1, 8010, recipe=OTHER_R))
        ok, reason, _ = lg.evaluate(st, H1, 8000, [H1])
        assert ok is False and reason == lg.REASON_FOREIGN


class TestDeclaredFootprint:
    def test_footprint_mirrors_the_planners_port_resolution(self):
        """Unit port wins; missing unit port falls back exactly as
        fleet_up_plan does (top-level port, then VLLM_PORT default)."""
        serving = {"port": 8000, "units": [
            {"id": "a", "nodes": [H1], "port": 8000},
            {"id": "b", "nodes": [H2, H3]},          # no unit port -> 8000
        ]}
        assert lg.declared_footprint(serving) == {(H1, 8000), (H2, 8000),
                                                  (H3, 8000)}

    def test_garbage_units_are_skipped_not_fatal(self):
        serving = {"units": [None, {"id": "x"}, {"id": "y", "nodes": []},
                             {"id": "z", "nodes": [H1], "port": "nope"}]}
        assert lg.declared_footprint(serving) == set()


class TestAlert:
    def test_refusal_logs_notifies_and_emits(self):
        notes, events = [], []
        detail = {"host": H1, "port": 8000, "span": [H1],
                  "error": "1 vLLM workload(s) already on host", "foreign": []}
        lg.alert_refusal("autoheal-recreate", "w-1", lg.REASON_FOREIGN, detail,
                         notify=lambda *a, **k: notes.append((a, k)),
                         emit=lambda t, p, **k: events.append((t, p)))
        assert len(notes) == 1 and len(events) == 1
        title, body = notes[0][0][0], notes[0][0][1]
        assert "Refused" in title or "Refused" in body
        # The alert must say hscc will NOT touch the foreign workload — an
        # operator reading it needs to know nothing was preempted.
        assert "will NOT stop" in body
        assert events[0][0] == "launch_guard.refused"
        assert events[0][1]["reason"] == lg.REASON_FOREIGN
        assert events[0][1]["actor"] == "autoheal-recreate"

    def test_alert_never_raises(self):
        """A broken notifier must not turn a refusal into a crash of the heal
        loop — the loop keeps refusing quietly instead."""
        def boom(*a, **k):
            raise RuntimeError("notify down")
        detail = {"host": H1, "port": 1, "span": [], "error": "x"}
        lg.alert_refusal("a", "u", lg.REASON_FOREIGN, detail,
                         notify=boom, emit=boom)   # must not raise


class TestStatusTransport:
    """health._sparkrun_cluster_status — the ONE status fetch behind every
    launch site. The transport itself is sparkrun's structured query; here we
    pin the FAIL-CLOSED behaviour around it."""

    def test_fetch_failure_returns_sentinel_not_stale_cache(self):
        from hscc_daemon import health as h
        h._LAUNCH_STATUS_CACHE.update({"at": 0.0, "status": None})
        monkey_run = {"ok": False, "output": ""}
        orig_run, orig_venv = h.run_cmd, h._sparkrun_venv_python
        try:
            h.run_cmd = lambda *a, **k: dict(monkey_run)
            h._sparkrun_venv_python = lambda: "/fake/python"
            assert h._sparkrun_cluster_status(force=True) is lg.NO_STATUS
        finally:
            h.run_cmd, h._sparkrun_venv_python = orig_run, orig_venv

    def test_unresolvable_venv_returns_sentinel(self):
        from hscc_daemon import health as h
        h._LAUNCH_STATUS_CACHE.update({"at": 0.0, "status": None})
        orig_venv = h._sparkrun_venv_python
        try:
            h._sparkrun_venv_python = lambda: None
            assert h._sparkrun_cluster_status(force=True) is lg.NO_STATUS
        finally:
            h._sparkrun_venv_python = orig_venv


def _guard_env(monkeypatch, st, serving_units, hfcc):
    """Wire a launch test: fake status, fake serving.json, silent log, and
    captured alerts. Returns (launches, stops, alerts)."""
    launches, stops, alerts = [], [], []
    monkeypatch.setattr(health, "_launch_guard_status_fn",
                        lambda force=False: copy.deepcopy(st))
    from hscc_daemon import serving
    monkeypatch.setattr(serving, "load_serving",
                        lambda: {"version": 1, "port": 8000,
                                 "units": serving_units})
    monkeypatch.setattr(health, "log", lambda *a, **kw: None)
    monkeypatch.setattr(health, "send_macos_notification",
                        lambda *a, **k: alerts.append(a) or True)
    monkeypatch.setattr(health, "emit_event",
                        lambda t, p, **k: alerts.append((t, p)) or {})
    monkeypatch.setattr(health, "run_cmd",
                        lambda args, **k: stops.append(list(args)) or
                        {"ok": True, "output": ""})
    monkeypatch.setattr(health.subprocess, "Popen",
                        lambda argv, **k: launches.append(list(argv)) or None)
    monkeypatch.setattr(health.sparkrun_bin, "argv", lambda *a: list(a))
    monkeypatch.setattr(health, "_sparkrun_exec_argv", lambda argv: list(argv))
    return launches, stops, alerts


SERVING_W = [{"id": "w-1", "role": "worker", "keepalive": True,
              "nodes": [H2], "recipe": VLLM_R, "model": "W", "tp": 1,
              "port": 8001}]


class TestAutohealRecreateIsGuarded:
    """The integration test class: the REAL _default_autoheal_worker must
    refuse BEFORE its destructive stop when the host is occupied."""

    def test_refuses_before_the_stop(self, tmp_hfcc_dir, monkeypatch):
        """A foreign vLLM on the unit's host => NO stop, NO launch. The order
        matters as much as the refusal: the old code stopped the unit's own
        (possibly healthy, mid-load) span first, so a wrong-target heal took
        healthy capacity DOWN on its way to over-committing the GPU."""
        st = status(solo(H2, 8001), solo(H2, 9100, recipe=OTHER_R))
        launches, stops, alerts = _guard_env(monkeypatch, st, SERVING_W,
                                             tmp_hfcc_dir)
        res = health._default_autoheal_worker(
            (H2, 8001), "w-1", H2, 8001)
        assert stops == [], "refusal must happen BEFORE the destructive stop"
        assert launches == []
        assert res.get("ok") is False
        assert "launch guard" in res.get("error", "")

    def test_refusal_alerts_once_via_notification_and_event(self, tmp_hfcc_dir,
                                                            monkeypatch):
        st = status(solo(H2, 8001), solo(H2, 9100, recipe=OTHER_R))
        launches, stops, alerts = _guard_env(monkeypatch, st, SERVING_W,
                                             tmp_hfcc_dir)
        health._default_autoheal_worker((H2, 8001), "w-1", H2, 8001)
        events = [a for a in alerts if isinstance(a, tuple) and
                  a and a[0] == "launch_guard.refused"]
        notes = [a for a in alerts if isinstance(a, tuple) and
                 isinstance(a[0], str) and "launch refused" in a[0].lower()]
        assert len(events) == 1, f"no events.jsonl row: {alerts}"
        assert len(notes) == 1, f"notification missing: {alerts}"
        assert events[0][1]["reason"] == lg.REASON_FOREIGN
        assert events[0][1]["actor"] == "autoheal-recreate"

    def test_clean_host_heals_normally(self, tmp_hfcc_dir, monkeypatch):
        """Positive control: with nothing foreign there, the heal proceeds —
        stop + exactly one launch. Proves the guard is not a blanket refusal."""
        st = status(solo(H2, 8001))          # only the unit's own container
        launches, stops, alerts = _guard_env(monkeypatch, st, SERVING_W,
                                             tmp_hfcc_dir)
        res = health._default_autoheal_worker((H2, 8001), "w-1", H2, 8001)
        assert res.get("ok") is True and res.get("status") == "recreating"
        assert len(stops) == 1 and len(launches) == 1

    def test_no_status_refuses_the_heal(self, tmp_hfcc_dir, monkeypatch):
        """'Status unavailable' == refusal (that morning status lied). A heal
        that cannot see the fleet does not get to guess it is safe."""
        launches, stops, alerts = _guard_env(monkeypatch, lg.NO_STATUS,
                                             SERVING_W, tmp_hfcc_dir)
        res = health._default_autoheal_worker((H2, 8001), "w-1", H2, 8001)
        assert res.get("ok") is False and stops == [] and launches == []

    def test_alert_is_deduped_across_ticks(self, tmp_hfcc_dir, monkeypatch):
        """The refusal re-happens every heal tick (every ~60s); the PUSH must
        not. The 2026-10-08 NAS rule fired 178 notifications in 11 minutes by
        alerting per tick."""
        st = status(solo(H2, 8001), solo(H2, 9100, recipe=OTHER_R))
        launches, stops, alerts = _guard_env(monkeypatch, st, SERVING_W,
                                             tmp_hfcc_dir)
        monkeypatch.setattr(health, "_LAUNCH_ALERT_AT", {})
        for _ in range(5):
            health._default_autoheal_worker((H2, 8001), "w-1", H2, 8001)
        notes = [a for a in alerts if isinstance(a, tuple) and
                 isinstance(a[0], str) and "launch refused" in a[0]]
        assert len(notes) == 1, f"notification storm: {len(notes)}"


class TestAutoupIsGuarded:
    """autodown.autoup() must route a guard refusal through its
    start-failure path (autodown §8 wake-fails) — never a silent skip that
    claims the wake happened.

    Helpers are inlined (mirroring test_autodown's fixtures) rather than
    imported: test_autodown is importable only under that suite's sys.path
    convention, and this file must run standalone too.
    """

    @pytest.fixture
    def autodown_file(self, tmp_path, monkeypatch):
        """Local copy of test_autodown's fixture (it is module-local there, not
        in conftest) — point AUTODOWN_FILE at tmp so nothing touches ~/.hscc."""
        import hscc_daemon.autodown as ad
        path = tmp_path / "hscc" / "autodown.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
        return path

    def _autoup_harness(self, tmp_path, monkeypatch, autodown_file, poison):
        """Wire the wake precondition (latched intentional block + down cfg)
        and run autoup with an injected runner/probe. `poison` is the status
        payload the guard sees. Returns (res, runner)."""
        import json
        import hscc_daemon.autodown as ad
        import hscc_daemon.lifecycle as _lifecycle

        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        data = {"port": 8000, "units": [
            {"id": "orch", "role": "orchestrator",
             "nodes": ["10.0.0.244", "10.0.0.246"], "port": 8000,
             "recipe": "~/.sparkrun-local/recipes/orch.yaml"},
            {"id": "wk1", "role": "worker", "keepalive": False,
             "nodes": ["10.0.0.247"], "port": 8000,
             "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
            {"id": "wk-keep", "role": "worker", "keepalive": True,
             "nodes": ["10.0.0.248"], "port": 8000,
             "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
        ]}
        serving_path = tmp_path / "serving.json"
        serving_path.write_text(json.dumps(data))
        _lifecycle.save_watchdog_block(
            {"blocked": True, "intentional": "autodown",
             "reason": ad.WATCHDOG_TEARDOWN_REASON,
             "blocked_at": "2026-10-14T00:00:00+00:00", "failures": []})
        cfg = dict(ad.DEFAULT_CONFIG)
        cfg.update({"enabled": True, "state": "down", "idle_minutes": 10,
                    "down_since": "2026-10-14T00:00:00+00:00",
                    "last_activity_iso": "2026-10-14T00:00:00+00:00"})
        ad.save_config(cfg)
        runner = []

        def run_fn(cmd, timeout=30):
            runner.append({"cmd": list(cmd)})
            return {"ok": True, "output": ""}

        monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)
        monkeypatch.setattr(health, "_launch_guard_status_fn",
                            lambda force=False: poison)
        res = ad.autoup(
            serving_path=str(serving_path), run_cmd_fn=run_fn,
            http_check_fn=lambda url, timeout=5: {"ok": True, "status": 200},
            clock=lambda: 0.0, sleep_fn=lambda _s: None, notify=False)
        return res, runner

    def test_refusal_becomes_start_failure_not_a_silent_wake(
            self, tmp_path, monkeypatch, autodown_file):
        """Foreign workload on wk1's host at an UNdeclared port -> the wake
        FAILS LOUDLY (§8: clear intentional, record failure, notify). Units
        planned BEFORE wk1 (orch, wk-keep — both hosts clean) may already have
        started, which is honest §8 partial-start semantics; what must NEVER
        happen is the refused unit's own start being issued, or the wake
        claiming success."""
        poison = status(solo("10.0.0.247", 9100, recipe=OTHER_R))
        res, runner = self._autoup_harness(tmp_path, monkeypatch,
                                           autodown_file, poison)
        assert res["result"] == "start-failed", res
        assert res.get("failed_at") == "wk1", res
        # The refused unit's host appears in NO issued start.
        for call in runner:
            assert "10.0.0.247" not in " ".join(call["cmd"]), call["cmd"]
        # orch + wk-keep legitimately started (they are ordered before wk1).
        assert len(runner) == 2

    def test_autoup_launches_normally_on_clean_status(
            self, tmp_path, monkeypatch, autodown_file):
        """Positive control for the same seam: empty status (fleet down, which
        is exactly what a wake faces) => all three units start."""
        res, runner = self._autoup_harness(tmp_path, monkeypatch,
                                           autodown_file, {})
        assert res["result"] == "up" and len(runner) == 3
