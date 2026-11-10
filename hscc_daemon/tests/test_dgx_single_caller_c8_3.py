"""proxy-stall c8-3 — DGX has exactly ONE in-process caller.

`check_dgx` is the most expensive probe in the daemon: an 8 s SSH echo, a 10 s
nvidia-smi SSH, then two sparkrun status sweeps (25 s + 10 s caps). During the
2026-10-31 episode it ran from inside `pipeline_watchdog` as well as from its own
stream thread (`dgx_ok = check_dgx_fn()`, lifecycle.py:523). Measured
consequences in docs/audits/2026-10-31-proxy-stalls.md §c4/c6: a stream declared
at 60 s ticked at a ~24 s median, up to ~112 extra full checks/day piled sweeps
onto a node that was already slow, and the `FLEET VISIBILITY LOST` WARN was
re-printed by every caller — two WARNs 0.50 s apart, which one tick cannot do.

The fix: the dgx stream publishes its verdict (health._DGX_VERDICT) and the
watchdog's default DGX signal reads that publication instead of re-running the
check. These tests pin the CALL SITE, because a unit test of
`health.dgx_verdict()` alone would stay green if someone put
`check_dgx_fn = check_dgx` back at lifecycle.py — the removed call is the whole
fix (test 1 below is the one that catches exactly that regression).

Fixture coupling, stated on purpose: every pre-existing watchdog test in
test_lifecycle.py / test_lifecycle_watchdog_race_20261018.py passes an explicit
`check_dgx_fn=lambda: ...`, which is still the documented test seam and still
honoured verbatim — those tests' assertions are untouched and none of them now
exercises the default path. The tests HERE cover the default path by publishing
a verdict through health.publish_dgx_verdict / a real check_dgx() run, which is
the equivalent stub for a signal that no longer comes from a callable the caller
supplies.
"""

import inspect
import json
import time

import pytest

from hscc_daemon import health, lifecycle, serving, state as state_mod


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def wd(tmp_hfcc_dir, monkeypatch):
    """Hermetic watchdog sandbox: tmp block/state files, recorded log, no
    subprocess of any kind reachable."""
    logs = []
    monkeypatch.setattr(lifecycle, "log", lambda msg, level="INFO": logs.append((level, str(msg))))
    state_dir = tmp_hfcc_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", str(tmp_hfcc_dir / "block.json"))
    argvs = []

    def fake_run_cmd(args, timeout=30, as_json=False, shell=False):
        argvs.append(list(args))
        return {"ok": True, "output": "", "stderr": ""}

    monkeypatch.setattr(lifecycle, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(health, "run_cmd", lambda args, **k: {"ok": True, "output": ""})
    monkeypatch.setattr(health, "log", lambda *a, **k: None)
    monkeypatch.setattr(health, "ssh_cmd", lambda *a, **k: {"ok": True, "output": "reachable"})
    monkeypatch.setattr(health, "http_check", lambda url, timeout=5: {"ok": True, "status": 200})
    monkeypatch.setattr(serving, "PRIMARY_NODE", "192.0.2.2")
    monkeypatch.setattr(serving, "VLLM_HEALTH_URL", "http://192.0.2.2:8000/health")
    return {"logs": logs, "argvs": argvs, "state_dir": state_dir, "monkeypatch": monkeypatch}


def tick(wd, gw=True, restart_ok=False, restart_calls=None):
    """One watchdog tick on the DEFAULT DGX signal — check_dgx_fn is NOT passed,
    which is the point: this is the wiring the daemon actually runs."""
    if restart_calls is None:
        restart_calls = []
    restarts = []

    def _restart():
        restart_calls.append(1)
        restarts.append(1)
        return {"ok": restart_ok}

    result = lifecycle.pipeline_watchdog(
        check_gateway_fn=lambda: gw,
        restart_vllm_fn=_restart,
        send_macos_notification_fn=lambda *a, **kw: None,
    )
    return result


def texts(wd):
    return "\n".join(m for _, m in wd["logs"])


# ── 1. the removed call site (the regression this card exists to prevent) ─────

class TestWatchdogDoesNotRunTheDgxCheck:
    """The whole fix in one assertion: a default-path tick must not execute
    check_dgx. Every test here would go red if `check_dgx_fn = check_dgx` came
    back at lifecycle.py:444 — and a unit test of health.dgx_verdict() alone
    would not."""

    def test_default_tick_never_calls_check_dgx(self, wd):
        boom_called = []

        def boom():
            boom_called.append(1)
            raise AssertionError("pipeline_watchdog re-ran check_dgx")

        wd["monkeypatch"].setattr(health, "check_dgx", boom)
        health.publish_dgx_verdict(True, True)

        assert tick(wd) is True
        assert boom_called == [], "watchdog must consume the published verdict, not re-run the check"

    def test_default_tick_shells_out_zero_times(self, wd):
        """Stronger than a name patch: no subprocess at all. check_dgx's cost is
        shell-outs, so 'zero argvs recorded across the tick' is the cost the card
        asked to remove."""
        health.publish_dgx_verdict(True, True)
        assert tick(wd) is True
        assert wd["argvs"] == [], f"watchdog tick shelled out: {wd['argvs']}"

    def test_repeated_ticks_do_not_multiply_checks(self, wd):
        """c6 arithmetic, pinned: N watchdog ticks must cost ZERO checks, not N.
        This is the pile-up — the tick that re-runs a 40 s sweep every 30 s is
        why a 60 s stream measured a 24 s median."""
        runs = []
        real_verdict = health.dgx_verdict

        def counting_check():
            runs.append(1)
            return True

        wd["monkeypatch"].setattr(health, "check_dgx", counting_check)
        health.publish_dgx_verdict(True, True)
        for _ in range(5):
            assert tick(wd) is True
        assert runs == []
        assert real_verdict() is not None  # sanity: a verdict really was there

    def test_source_has_no_check_dgx_binding_left(self, wd):
        """The binding itself, not just behaviour: `from .health import ...
        check_dgx` inside pipeline_watchdog is what a future reader follows back
        into the sweep. A text/AST guard keeps it from silently returning."""
        src = inspect.getsource(lifecycle.pipeline_watchdog)
        assert "check_dgx" in src, "docstring seam note expected"
        assert "check_dgx_fn = check_dgx" not in src, src
        assert "import check_dgx" not in src, src


# ── 2. the verdict is consumed verbatim (semantics unchanged) ────────────────

class TestVerdictConsumedVerbatim:
    """The card's 'no semantics change' claim, tested rather than asserted."""

    def test_published_false_drives_the_failure_path(self, wd):
        """dgx FAILS (ssh or vLLM down) → tick returns False and vLLM restart is
        attempted, exactly as when the watchdog called check_dgx itself."""
        health.publish_dgx_verdict(False, False)
        calls = []
        assert tick(wd, gw=True, restart_calls=calls) is False
        assert calls == [1], "a failed DGX verdict must still trigger the auto-restart path"

    def test_degraded_fleet_keeps_the_watchdog_healthy(self, wd):
        """THE semantics pin. check_dgx writes ok=publish_ok to the state file
        but RETURNS serving_ok, and they diverge exactly when sparkrun status
        polling is what failed (fleet_visibility degraded, serving fine). A
        'simplification' that read the STATE FILE — the findings doc's first
        option — would read publish_ok=False here and start stop/relaunch
        cycles against a healthy vLLM. The returned verdict must win."""
        state_dir = wd["state_dir"]
        # A real check_dgx run with a dead sparkrun status path: publish_ok
        # False (stream RED), serving_ok True (the return value).
        wd["monkeypatch"].setattr(health, "_sparkrun_venv_python", lambda: None)
        wd["monkeypatch"].setattr(
            health, "run_cmd",
            lambda args, **k: {"ok": False, "output": "[Errno 2] no sparkrun"})

        assert health.check_dgx() is True          # the returned verdict
        entry = json.loads((state_dir / "dgx.json").read_text())
        assert entry["ok"] is False                # the state file disagrees
        assert entry["details"]["fleet_visibility"] == "degraded"

        calls = []
        assert tick(wd, gw=True, restart_calls=calls) is True
        assert calls == [], (
            "a status-polling outage must not make the watchdog restart a "
            "healthy serving layer — that is the semantics the state file would have changed")

    def test_verdict_carries_both_verdicts_for_readers(self, wd):
        health.publish_dgx_verdict(True, False)
        v = health.dgx_verdict()
        assert v["serving_ok"] is True and v["publish_ok"] is False
        assert v["stale"] is False   # no age window requested => no judgement


# ── 3. no-verdict and stale: neutral pass, never a fabricated failure ────────

class TestNoVerdictIsUnknown:
    """No verdict published = UNKNOWN, which is neither OK nor FAIL. The
    watchdog's fail action is a vLLM stop/relaunch and then a pipeline block +
    critical notification, so a startup tick where the 30 s loop outran the 60 s
    dgx thread must not be counted as a failure it has no evidence for — and a
    genuine gateway failure must not be swallowed by the missing DGX leg."""

    def test_no_verdict_with_healthy_gateway_passes_without_restarting(self, wd):
        health.reset_dgx_verdict()
        calls = []
        assert tick(wd, gw=True, restart_calls=calls) is True
        assert calls == [], "no DGX verdict must not restart vLLM on a layer nobody has probed"
        assert any(lvl == "WARN" and "not counting this tick as a failure" in m
                   for lvl, m in wd["logs"]), texts(wd)

    def test_no_verdict_stamps_unknown_not_a_lie_in_the_state_file(self, wd):
        """The stream file must say it does not KNOW (dgx: None), not dgx: True.
        `hscc verify` and an operator reading `hscc status` both consume this; a
        fake True is how a blind spot gets reported as a healthy fleet."""
        import json
        health.reset_dgx_verdict()
        assert tick(wd, gw=True) is True
        entry = json.loads((wd["state_dir"] / "watchdog.json").read_text())
        assert entry["dgx"] is None, entry
        assert entry["ok"] is True          # gateway answered: no failure
        assert entry["gateway"] is True
        assert "not yet published" in entry["message"]

    def test_no_verdict_does_not_reset_or_accumulate_breaker_failures(self, wd):
        """A startup tick is not a success either — writing a success_entry would
        let the 3-failure breaker's `cleanup_old_failures` reset be driven by a
        tick that observed nothing. The unknown tick must not touch failures."""
        health.reset_dgx_verdict()
        lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                       "blocked_at": None,
                                       "failures": [{"timestamp": lifecycle.now_iso(),
                                                     "dgx": False, "gateway": True}],
                                       "auto_restart_count": 0})
        assert tick(wd, gw=True) is True
        block = lifecycle.load_watchdog_block()
        assert len(block["failures"]) == 1, (
            "an UNKNOWN tick must neither add nor clear breaker history")

    def test_no_verdict_still_counts_a_real_gateway_failure(self, wd):
        """The asymmetry that makes this fork more than `return True`: a gateway
        failure is a failure even when the DGX leg is blind — the tick returns
        False and stamps ok:False. What it must NOT do is restart vLLM: a
        gateway-only failure never did that before (the old `not dgx_ok` gate
        could only be True for a REAL DGX failure), and the restart targets the
        serving layer, so a leg that never reported must not justify a
        stop/relaunch.

        NOTE, pre-existing and deliberately untouched: this path (gateway-only
        failure) does not `save_watchdog_block`, so the entry is not persisted
        and the breaker does not accumulate it — true of the old code too
        (`dgx_ok=True, gw_ok=False` took this same branch). UNKNOWN is kept
        behaviour-identical to what a `dgx_ok=True` tick did; whether the
        gateway leg *should* count toward the breaker is a separate question,
        filed as a LEFT finding rather than smuggled in here.
        """
        health.reset_dgx_verdict()
        calls = []
        assert tick(wd, gw=False, restart_calls=calls) is False
        assert calls == [], "a gateway-only failure must not restart vLLM"
        entry = json.loads((wd["state_dir"] / "watchdog.json").read_text())
        assert entry["ok"] is False, "a gateway failure must still stamp unhealthy"
        assert entry["dgx"] is None, "the record must keep DGX UNKNOWN, not invent a bool"
        assert entry["gateway"] is False
        assert any(lvl == "WARN" and "gateway leg FAILED" in m
                   for lvl, m in wd["logs"]), texts(wd)

    def test_gateway_only_failure_with_a_known_good_dgx_still_does_not_restart(self, wd):
        """The pre-existing rule this card must not disturb, pinned against the
        new default signal: DGX healthy + gateway down => no vLLM restart
        (restart_vllm is gated on a DGX failure, not on any pipeline failure)."""
        health.publish_dgx_verdict(True, True)
        calls = []
        assert tick(wd, gw=False, restart_calls=calls) is False
        assert calls == []

    def test_dgx_signal_returns_none_for_unknown(self, wd):
        """The signal itself is honest: None, not a bool."""
        health.reset_dgx_verdict()
        assert lifecycle.dgx_verdict_serving_ok() is None

    def test_stale_verdict_is_carried_not_inverted(self, wd):
        """A verdict older than the window is reported stale and still used.
        The audit measured a legitimate 157.85 s sweep; failing on that would
        block a healthy pipeline."""
        health.publish_dgx_verdict(True, True, at=time.monotonic() - 10_000)
        calls = []
        assert tick(wd, gw=True, restart_calls=calls) is True
        assert calls == []
        assert any(lvl == "WARN" and "old" in m for lvl, m in wd["logs"]), texts(wd)

    def test_stale_verdict_can_still_drive_a_failure(self, wd):
        """Staleness must not become a get-out-of-failure clause: a stale FAIL
        is still a fail, so a dead stream cannot mask a dead cluster."""
        health.publish_dgx_verdict(False, False, at=time.monotonic() - 10_000)
        calls = []
        assert tick(wd, gw=True, restart_calls=calls) is False
        assert calls == [1]

    def test_stale_window_is_derived_from_the_sot_cadence(self, wd):
        """No private literal: the window must track PERIODIC_INTERVALS["dgx"],
        the same SOT rule verify uses so the two never disagree about 'old'."""
        from hscc_daemon import daemon_ops
        base = lifecycle._dgx_signal_max_age()
        expected = 2 * daemon_ops.PERIODIC_INTERVALS["dgx"] + 30
        assert base == expected, (base, expected)
        wd["monkeypatch"].setattr(daemon_ops, "PERIODIC_INTERVALS",
                                  dict(daemon_ops.PERIODIC_INTERVALS, dgx=600))
        assert lifecycle._dgx_signal_max_age() == 2 * 600 + 30

    def test_dgx_verdict_default_makes_no_freshness_judgement(self, wd):
        health.publish_dgx_verdict(True, True, at=time.monotonic() - 10_000)
        assert health.dgx_verdict()["stale"] is False


# ── 4. the publisher ─────────────────────────────────────────────────────────

class TestCheckDgxPublishes:
    """Every path must publish, including the failing one — the watchdog needs
    the bad verdict most of all."""

    def test_healthy_check_publishes_serving_and_publish_ok(self, wd):
        health.reset_dgx_verdict()
        assert health.check_dgx() is True
        v = health.dgx_verdict()
        assert v["serving_ok"] is True and v["publish_ok"] is True

    def test_failed_serving_publishes_false(self, wd):
        health.reset_dgx_verdict()
        wd["monkeypatch"].setattr(health, "ssh_cmd",
                                  lambda *a, **k: {"ok": False, "output": "timed out"})
        assert health.check_dgx() is False
        v = health.dgx_verdict()
        assert v["serving_ok"] is False and v["publish_ok"] is False

    def test_degraded_check_publishes_false_publish_ok(self, wd):
        health.reset_dgx_verdict()
        wd["monkeypatch"].setattr(health, "_sparkrun_venv_python", lambda: None)
        wd["monkeypatch"].setattr(health, "run_cmd",
                                  lambda args, **k: {"ok": False, "output": "boom"})
        assert health.check_dgx() is True            # serving still reachable
        v = health.dgx_verdict()
        assert v["serving_ok"] is True and v["publish_ok"] is False

    def test_verdict_is_process_local_so_a_cli_run_cannot_reach_the_daemon(self, wd):
        """`hscc check dgx` runs in its OWN process under
        state.persist_disabled(), precisely so a manual CLI run cannot clobber
        the daemon's shared STATE FILE (an off-LAN laptop's transient failure
        would otherwise make `hscc status` report the fleet as broken).

        The verdict cache needs no equivalent gate, and this pins WHY rather
        than leaving the next reader to wonder: `_DGX_VERDICT` is a module
        global, i.e. process-local. A CLI process publishing its own verdict can
        never reach the daemon's cache, by construction — no cross-process
        surface exists to protect. Inside the CLI's own process publishing is
        harmless and consistent with what the persist_disabled overlay already
        does for read_state (the CLI still sees the check it just ran).
        """
        health.reset_dgx_verdict()
        with state_mod.persist_disabled():
            assert health.check_dgx() is True
        # The daemon's on-disk state stays untouched (existing guarantee):
        assert not (wd["state_dir"] / "dgx.json").exists()
        # ...and the only thing the CLI run touched is its own process's cache.
        assert health.dgx_verdict()["serving_ok"] is True


# ── 5. the trigger path is not a DGX caller ─────────────────────────────────

class TestTriggerEngineIsNotADgxCaller:
    """The audit counted a 15 s DGX caller because daemon_ops wired
    `check_dgx_fn=check_dgx` into trigger_engine. The body never invoked it
    (0 call sites), so the fix is to remove the wiring that invites the
    misreading — and to keep it removed."""

    def test_signature_has_no_check_dgx_fn(self):
        from hscc_daemon import trigger
        params = inspect.signature(trigger.trigger_engine).parameters
        assert "check_dgx_fn" not in params, (
            "re-adding check_dgx_fn to trigger_engine re-creates the audit's "
            "fourth-caller reading; DGX has one caller: the dgx stream thread")

    def test_trigger_loop_wiring_drops_the_dgx_kwarg(self):
        """daemon_ops.run_trigger_loop must not pass it either — the wiring is
        what a reader trusts, more than the signature."""
        import ast

        from hscc_daemon import daemon_ops
        src = inspect.getsource(daemon_ops.run_daemon_loop)
        tree = ast.parse(src)
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "trigger_engine":
                if any(kw.arg == "check_dgx_fn" for kw in node.keywords):
                    bad.append(ast.get_source_segment(src, node))
        assert not bad, f"run_trigger_loop passes check_dgx_fn again: {bad}"

    def test_trigger_engine_runs_with_no_dgx_argument(self, wd, tmp_hfcc_dir, monkeypatch):
        """End-to-end: a full trigger_engine() tick with a DGX state file
        present still costs zero checks, and still evaluates its dgx rules off
        the state file (the mechanism it always used)."""
        from hscc_daemon import trigger

        monkeypatch.setattr(trigger, "TRIGGERS_FILE", str(tmp_hfcc_dir / "triggers.json"))
        monkeypatch.setattr(trigger, "COOLDOWN_FILE", str(tmp_hfcc_dir / "cooldowns.json"))
        monkeypatch.setattr(trigger, "EVENTS_FILE", str(tmp_hfcc_dir / "events.jsonl"))
        (tmp_hfcc_dir / "triggers.json").write_text(json.dumps({"rules": [
            {"id": "dgx-down", "trigger_type": "notify", "enabled": True,
             "cooldown_seconds": 0,
             "condition": {"metric": "failed_dgx", "op": "==", "value": "True"},
             "trigger_params": {"title": "t", "body": "b"}},
        ]}))
        (wd["state_dir"] / "dgx.json").write_text(json.dumps(
            {"timestamp": state_mod.now_iso(), "stream": "dgx", "ok": False, "details": {}}))
        notes = []
        monkeypatch.setattr(trigger, "send_macos_notification",
                            lambda *a, **kw: notes.append(a))

        runs = []
        monkeypatch.setattr(health, "check_dgx", lambda: runs.append(1) or True)

        assert trigger.trigger_engine() is True
        assert notes, "the failed_dgx rule must still fire off the state file"
        assert runs == [], "trigger_engine must not run the DGX check"
