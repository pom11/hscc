"""t_b72dce57 — the 2026-10-18 01:38 PipelineWatchdog race, replayed.

The incident (full redacted timeline in fixtures/vllm_auto_restart_race_20261018.log,
seeded verbatim from ~/.hscc/daemon.log.1.gz with LAN hosts replaced by the
sanctioned 10.0.0.x placeholders): engine-wedge recovery stopped 'orch' and
relaunched it via `hscc cluster up` at 01:38:33-40 while holding the heal
lease; the proxy stream honoured that lease and deferred at 01:38:34; the
PipelineWatchdog ignored it 10 seconds later — its target-less stop did nothing
("Error: Must specify TARGET or --all"), the flow continued anyway, and the
launch of a SECOND, bare-recipe job was logged "vLLM auto-restart #270:
success" while that container served nothing for ~7h (its :8000 was already
held by the correct cluster-up job; vllm inside it never started).

These tests pin the four repair rules the card demands:
  1. the vLLM auto-restart path consults the SAME heal lease (heal_lease.py)
     the proxy deferral used, and bails out with a logged deferral when held;
  2. it never launches a unit `hscc cluster up` owns (serving.json-registered);
  3. the stop carries an explicit TARGET and a failed stop ABORTS the restart;
  4. "success" requires the launched unit to actually serve (the engine-wedge
     streaming probe gates the counter increment + the success log), and a
     hollow launch escalates through an alarm path.

The fixture-driven replay (TestRaceReplay) is red against the pre-fix code —
old code called restart_vllm_fn and shelled stop-then-launch despite the held
lease — and every gate has a mutation probe in MUTATION_PROBES.md form below:
each test names the mutation it catches.
"""

import json
import os
import re
from pathlib import Path

import pytest

from hscc_daemon import heal_lease, lifecycle, serving

FIXTURE = Path(__file__).parent / "fixtures" / "vllm_auto_restart_race_20261018.log"

# One parsed fixture line: (timestamp, level, message).
LINE_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s+\[\s*(?P<level>[A-Z]+)\]\s+(?P<msg>.*?)\s*$")


def load_timeline():
    lines = []
    for raw in FIXTURE.read_text().splitlines():
        if raw.startswith("#") or not raw.strip():
            continue
        m = LINE_RE.match(raw)
        assert m, f"fixture line unparseable: {raw!r}"
        lines.append((m.group("ts"), m.group("level"), m.group("msg")))
    return lines


class _Recorder:
    """Collects (msg, level) log calls / notification calls / run_cmd argvs."""

    def __init__(self):
        self.entries = []

    def log(self, msg, level="INFO"):
        self.entries.append((str(msg), level))

    def notes(self, title=None, **kw):
        self.entries.append(("NOTE:" + str(title), kw.get("priority", "")))

    def texts(self):
        return "\n".join(m for m, _ in self.entries)


@pytest.fixture
def env(tmp_hfcc_dir, monkeypatch):
    """Hermetic watchdog sandbox: tmp block/state/lease files, recorded logs,
    recorded run_cmd (no subprocess can escape), materialised topology."""
    rec = _Recorder()
    monkeypatch.setattr(lifecycle, "log", rec.log)
    from hscc_daemon import state as state_mod
    state_dir = tmp_hfcc_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
    block_path = str(tmp_hfcc_dir / "block.json")
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", block_path)
    lease_path = str(tmp_hfcc_dir / "heal-lease.json")
    monkeypatch.setattr(heal_lease, "HEAL_LEASE_FILE", lease_path)
    if os.path.exists(lease_path):
        os.unlink(lease_path)
    argvs = []

    def fake_run_cmd(args, timeout=30, as_json=False, shell=False):
        argvs.append(list(args))
        return {"ok": True, "output": "", "stderr": ""}

    monkeypatch.setattr(lifecycle, "run_cmd", fake_run_cmd)
    # Fresh breaker block per test.
    lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                   "blocked_at": None, "failures": [],
                                   "auto_restart_count": 0})
    return {"rec": rec, "argvs": argvs, "block_path": block_path,
            "lease_path": lease_path, "state_dir": state_dir,
            "monkeypatch": monkeypatch}


def tick(env, dgx=False, gw=False, restart_calls=None, notes=None,
         restart_fn=None):
    """Run one PipelineWatchdog tick. restart_calls/notes default to recorders;
    restart_fn overrides the fake entirely (its return dict drives the branch)."""
    if restart_calls is None:
        restart_calls = []
    if notes is None:
        notes = []
    if restart_fn is None:
        def _fake_restart():
            restart_calls.append(1)
            return {"ok": False}
        restart_fn = _fake_restart
    result = lifecycle.pipeline_watchdog(
        check_dgx_fn=lambda: dgx,
        check_gateway_fn=lambda: gw,
        restart_vllm_fn=restart_fn,
        send_macos_notification_fn=lambda *a, **kw: notes.append((a, kw)),
    )
    return result, restart_calls, notes


def seed_incident_lease():
    """Take the lease EXACTLY as engine-wedge recovery held it at 01:38:33,
    with the holder identity derived FROM THE FIXTURE (the proxy WARN line
    'proxy relaunch deferred — ... heal in progress: engine-wedge-recovery
    (orch)'), not hardcoded blind."""
    timeline = load_timeline()
    defer = [m for ts, lvl, m in timeline
             if "proxy relaunch deferred" in m and "heal in progress" in m]
    assert defer, "fixture lost the 01:38:34 proxy-deferral WARN"
    holder = defer[0].split("heal in progress:", 1)[1].strip()
    who, _, subject = holder.partition(" ")
    subject = subject.strip("()")
    ok, rec = heal_lease.acquire(who, subject)
    assert ok and rec["who"] == "engine-wedge-recovery" and rec["subject"] == "orch"
    return holder


# ── Fixture integrity (redaction + verbatim timestamps) ─────────────────────

class TestFixtureFidelity:
    def test_timeline_carries_the_incident_in_order(self):
        timeline = load_timeline()
        msgs = " | ".join(m for _, _, m in timeline)
        # The card's verified timeline, verbatim timestamps and all.
        for ts, needle in [
            ("2026-10-18T01:36:25", "WEDGED unit(s) detected: orch"),
            ("2026-10-18T01:38:29.612316", "streak=3"),
            ("2026-10-18T01:38:33.101616", "stopped unit 'orch'"),
            ("2026-10-18T01:38:33.102153", "relaunching missing units via `hscc cluster up`"),
            ("2026-10-18T01:38:34.133669", "proxy relaunch deferred"),
            ("2026-10-18T01:38:44.864964", "attempting vLLM auto-restart"),
            ("2026-10-18T01:38:45.568170", "Must specify TARGET or --all"),
            ("2026-10-18T01:39:09.978029", "auto-restart #270: success"),
        ]:
            hit = [m for t, _, m in timeline if t.startswith(ts) and needle in m]
            assert hit, f"fixture missing {ts} {needle!r}"
        assert "03fae933e3df92bf" in msgs  # the CORRECT job id kept verbatim

    def test_lan_hosts_redacted_to_placeholders(self):
        text = FIXTURE.read_text()
        # Live LAN /24 must be gone; the sanctioned placeholder used instead.
        assert "192.168.88." not in text
        assert "10.0.0.244:8000" in text  # redaction applied, host kept shape


# ── Rule 1 — in-process replay of the 01:38 race ────────────────────────────

class TestRaceReplay:
    def test_lease_held_watchdog_defers_and_touches_nothing(self, env):
        """THE replay: lease held by engine-wedge-recovery(orch) (as at
        01:38:34) + the watchdog tick that ran at 01:38:42-44 with DGX and
        Gateway both ok=False (fixture lines 01:38:44.710/.864).

        RED-ON-OLD: pre-fix code called restart_vllm_fn here (the 01:38:44.865
        'Restarting vLLM via sparkrun...' line) and shelled stop+launch.
        NEW: nothing but a logged deferral. Mutation caught: deleting the
        heal_lease.read_lease() gate in pipeline_watchdog."""
        holder = seed_incident_lease()
        result, restart_calls, notes = tick(env, dgx=False, gw=False)

        assert result is False
        assert restart_calls == [], "watchdog attempted a restart while a heal was in flight"
        assert env["argvs"] == [], "watchdog shelled a command while a heal was in flight"
        assert notes == [], "a deferral is not an event to alarm about"
        # The deferral is LOUD in the log and names the holder from the fixture.
        assert "heal in progress" in env["rec"].texts()
        assert holder in env["rec"].texts()
        # Counter NOT incremented.
        assert lifecycle.load_watchdog_block()["auto_restart_count"] == 0
        # State stream records the deferral, not an auto-restart.
        entry = json.loads((env["state_dir"] / "watchdog.json").read_text())
        assert entry["auto_restart_deferred"] is True
        assert entry["auto_restart"] is False
        assert entry["auto_restart_count"] == 0
        assert holder in entry["message"]

    def test_lease_held_restart_vllm_itself_defers(self, env):
        """Same rule enforced one layer down (restart_vllm is also reachable
        from trigger.py without the watchdog). Mutation caught: removing the
        read_lease() gate inside restart_vllm — the stop+launch would run."""
        seed_incident_lease()
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["deferred"] is True and res["ok"] is False
        assert env["argvs"] == []
        assert "heal in progress: engine-wedge-recovery (orch)" in env["rec"].texts()

    def test_lease_held_deferral_outranks_registered_refusal(self, env,
                                                             tmp_hfcc_dir,
                                                             monkeypatch):
        """Precedence of rule 1 over rule 2: lease held by
        engine-wedge-recovery(orch) AND the target registered — the exact
        01:38:44 state — must report the DEFERRAL, not a registry refusal.
        While a heal is in flight the honest answer is 'a repair is running',
        not 'cluster up owns this'. Mutation caught: dropping restart_vllm's
        upfront read_lease() check (the acquire() further down still defers
        for unregistered targets, so only this combined state distinguishes
        the two — the upfront check must run BEFORE the registry lookup)."""
        seed_incident_lease()
        serving.reset_resolution_cache()
        serving._materialize_topology_defaults()
        path = tmp_hfcc_dir / "serving.json"
        path.write_text(json.dumps({"port": 8000, "units": [{
            "id": "orch", "role": "orchestrator",
            "nodes": [serving.PRIMARY_NODE],
            "recipe": "/tmp/models/orch-recipe.yaml", "port": 8000}]}))
        monkeypatch.setattr(serving, "SERVING_JSON", str(path))
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["deferred"] is True, "held lease must defer, whatever the registry says"
        assert res["refused"] is False
        assert env["argvs"] == []
        assert "heal in progress: engine-wedge-recovery (orch)" in env["rec"].texts()

    def test_lease_released_watchdog_proceeds_normally(self, env):
        """NEGATIVE CONTROL: no lease ⇒ the (unregistered-node) attempt runs.
        Guards against 'never restart anything' as a fake-green fix."""
        lifecycle.VLLM_SERVE_VERIFY_TIMEOUT_S = 0.0
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        notes = []
        result, calls, notes = tick(env, dgx=False, gw=False)
        assert calls == [1] and result is False
        assert "attempting vLLM auto-restart" in env["rec"].texts()


# ── Rule 3 — stop needs TARGET; a failed stop ABORTS ────────────────────────

class TestStopTarget:
    def test_stop_argv_carries_explicit_target(self, env):
        """Positive: the issued stop is `sparkrun stop <recipe> --hosts <node>`
        — never the target-less no-op. Mutation caught: reverting
        serving._rebuild_vllm_cmds to the bare ['stop','--hosts',node]."""
        seed = "/tmp/models/orch-recipe.yaml"
        serving.VLLM_RECIPE = seed
        serving._rebuild_vllm_cmds()
        assert serving.VLLM_STOP_CMD[:3] == ["sparkrun", "stop", seed]
        assert serving.VLLM_STOP_CMD[3:5] == ["--hosts", serving.PRIMARY_NODE]
        lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        stop, start = env["argvs"][0], env["argvs"][1]
        assert stop[:3] == ["sparkrun", "stop", os.path.expanduser(seed)]
        assert "--all" not in stop  # --all is never an acceptable fallback
        assert start[1] == "run"

    def test_stop_failure_aborts_before_any_launch(self, env, monkeypatch):
        """Replays 01:38:45.568: sparkrun rejects the stop with 'Must specify
        TARGET or --all.' ⇒ the restart ABORTS; no `sparkrun run` is issued.
        Mutation caught: ignoring stop_result['ok'] (the pre-fix flow)."""
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()

        def stop_rejects(args, timeout=30, as_json=False, shell=False):
            env["argvs"].append(list(args))
            return {"ok": False, "output": "Error: Must specify TARGET or --all.",
                    "stderr": "Error: Must specify TARGET or --all."}

        monkeypatch.setattr(lifecycle, "run_cmd", stop_rejects)
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["stop_failed"] is True and res["ok"] is False
        assert len(env["argvs"]) == 1, "a launch was attempted after a failed stop"
        assert env["argvs"][0][1] == "stop"
        assert "aborting restart" in env["rec"].texts()

    def test_no_target_resolvable_never_issues_the_broken_argv(self, env):
        """With no recipe anywhere, vllm_stop_cmd returns None and the restart
        aborts WITHOUT ever issuing the target-less argv (the 01:38:45 line was
        the daemon handing sparkrun an argv it rejects). Mutation caught:
        falling back to ['stop','--hosts',node] or '--all'."""
        serving.VLLM_RECIPE = ""
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["stop_failed"] is True and res["ok"] is False
        assert env["argvs"] == [], "the target-less stop argv was issued"
        assert "no explicit stop TARGET" in env["rec"].texts()

    def test_serving_vllm_stop_cmd_refuses_targetless(self, tmp_path):
        """Unit-level rule 3: no recipe ⇒ None (unbuildable), never a silent
        no-op form."""
        assert serving.vllm_stop_cmd(node="10.0.0.1", recipe="") is None
        assert serving.vllm_stop_cmd(node="10.0.0.1", recipe=None) is None
        cmd = serving.vllm_stop_cmd(node="10.0.0.1", recipe="/r/model.yaml")
        assert cmd == ["sparkrun", "stop", "/r/model.yaml", "--hosts", "10.0.0.1"]


# ── Rule 2 — never launch a cluster-up-owned unit ───────────────────────────

class TestRegisteredUnitRefusal:
    def _register_orch(self, tmp_hfcc_dir, monkeypatch):
        """serving.json declares the orchestrator on PRIMARY_NODE:8000 — the
        registry `hscc cluster up` launches from."""
        serving.reset_resolution_cache()
        serving._materialize_topology_defaults()
        data = {"port": 8000, "units": [{
            "id": "orch", "role": "orchestrator",
            "nodes": [serving.PRIMARY_NODE],
            "recipe": "/tmp/models/orch-recipe.yaml", "port": 8000}]}
        path = tmp_hfcc_dir / "serving.json"
        path.write_text(json.dumps(data))
        monkeypatch.setattr(serving, "SERVING_JSON", str(path))

    def test_registered_unit_launch_refused_and_logged(self, env, tmp_hfcc_dir,
                                                       monkeypatch):
        """The watchdog finding its target registered ⇒ REFUSED: no stop, no
        launch, a WARN naming the owner. Mutation caught: removing the
        _registered_serving_unit() gate (this is exactly how the duplicate
        job of the incident appeared next to cluster-up's 03fae933)."""
        self._register_orch(tmp_hfcc_dir, monkeypatch)
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["refused"] is True and res["ok"] is False
        assert env["argvs"] == [], "watchdog stopped/launched a cluster-up-owned unit"
        texts = env["rec"].texts()
        assert "refused" in texts and "hscc cluster up" in texts
        assert "orch" in texts

    def test_unregistered_target_still_restarts(self, env, tmp_hfcc_dir,
                                                monkeypatch):
        """NEGATIVE CONTROL for rule 2: nothing registered at (node, port) ⇒
        the (targeted, verified) restart proceeds — the refusal must not
        degrade into 'watchdog can never restart'."""
        serving.reset_resolution_cache()
        serving._materialize_topology_defaults()
        path = tmp_hfcc_dir / "serving.json"
        path.write_text(json.dumps({"port": 8000, "units": [{
            "id": "other", "role": "worker", "nodes": ["10.0.0.99"],
            "recipe": "/tmp/models/x.yaml", "port": 8001}]}))
        monkeypatch.setattr(serving, "SERVING_JSON", str(path))
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["ok"] is True and res["refused"] is False
        assert [a[1] for a in env["argvs"]] == ["stop", "run"]

    def test_registered_detection_matches_unit_port(self, env, tmp_hfcc_dir,
                                                    monkeypatch):
        """A unit registered on a DIFFERENT port than the watchdog target is
        not a match (the watchdog's (node, port) is what must be owned)."""
        serving.reset_resolution_cache()
        serving._materialize_topology_defaults()
        path = tmp_hfcc_dir / "serving.json"
        path.write_text(json.dumps({"port": 8000, "units": [{
            "id": "orch2", "role": "orchestrator",
            "nodes": [serving.PRIMARY_NODE],
            "recipe": "/tmp/models/orch-recipe.yaml", "port": 8123}]}))
        monkeypatch.setattr(serving, "SERVING_JSON", str(path))
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["refused"] is False and res["ok"] is True


# ── Rule 4 — no hollow successes ────────────────────────────────────────────

class TestNoHollowSuccess:
    def test_hollow_launch_not_ok_and_not_verified(self, env, monkeypatch):
        """Sandboxed fake: `sparkrun run` returns rc=0 (container created) but
        the serve probe says the port is dead ⇒ NOT ok, verified=False.
        Mutation caught: trusting rc=0 (old code logged 'success' here)."""
        monkeypatch.setattr(lifecycle, "VLLM_SERVE_VERIFY_TIMEOUT_S", 0.0)
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: False)
        assert res["ok"] is False
        assert res["verified"] is False
        assert "hollow launch" in env["rec"].texts()
        assert [a[1] for a in env["argvs"]] == ["stop", "run"]

    def test_hollow_launch_does_not_advance_counter(self, env, monkeypatch):
        """The tick that ran a hollow launch must NOT mint a '#NNN: success':
        no counter increment, no load-grace window, no success log. #270 was
        exactly this and stayed invisible ~7h."""
        monkeypatch.setattr(lifecycle, "VLLM_SERVE_VERIFY_TIMEOUT_S", 0.0)
        lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                       "failures": [], "auto_restart_count": 269})
        notes = []
        result, calls, notes = tick(env, dgx=False, gw=False)
        assert calls == [1]
        assert lifecycle.load_watchdog_block()["auto_restart_count"] == 269
        blk = lifecycle.load_watchdog_block()
        assert blk.get("restart_cooldown_until") is None
        assert blk["unverified_restarts"] == 1
        assert "success" not in env["rec"].texts()
        entry = json.loads((env["state_dir"] / "watchdog.json").read_text())
        assert entry["restart_verified"] is False
        assert entry["auto_restart_count"] == 269

    def test_two_hollow_launches_fire_critical_alarm(self, env, monkeypatch):
        """Alarm path: a second consecutive unverified launch escalates a
        NOT-SERVING critical notification (rule 4's alarm). Mutation caught:
        dropping the alarm branch, or advancing the streak on non-launches."""
        monkeypatch.setattr(lifecycle, "VLLM_SERVE_VERIFY_TIMEOUT_S", 0.0)
        tick(env, dgx=False, gw=False)          # streak 1: warning only
        _, _, notes = tick(env, dgx=False, gw=False)  # streak 2: alarm
        assert any(a[0] == "HSCC vLLM Auto-Restart NOT SERVING" for a, kw in notes)
        assert lifecycle.load_watchdog_block()["unverified_restarts"] == 2

    def test_verified_serving_advances_counter_and_logs_success(self, env):
        """POSITIVE CONTROL for rule 4: a VERIFIED serving restart (ok+verified
        — what restart_vllm returns only after the probe says serving)
        advances the counter, logs '#N: success', opens the load-grace
        window, and resets the unverified streak."""
        lifecycle.save_watchdog_block({"blocked": False, "reason": "",
                                       "failures": [], "auto_restart_count": 269,
                                       "unverified_restarts": 1})
        calls = []

        def verified_restart():
            calls.append(1)
            return {"ok": True, "output": "", "verified": True}

        _, calls_seen, notes = tick(env, dgx=False, gw=False,
                                    restart_calls=calls, restart_fn=verified_restart)
        assert calls == [1]
        blk = lifecycle.load_watchdog_block()
        assert blk["auto_restart_count"] == 270
        assert blk["unverified_restarts"] == 0
        assert "vLLM auto-restart #270: success" in env["rec"].texts()
        assert blk.get("restart_cooldown_until")
        # A verified success alarms with the routine high-priority note, not
        # the critical NOT-SERVING alarm.
        assert all(a[0] == "HSCC vLLM Auto-Restart" for a, kw in notes)

    def test_probe_is_the_engine_wedge_streaming_probe(self, env, monkeypatch):
        """Rule 4 names the SAME streaming probe engine-wedge uses — pin that
        restart_vllm reaches health._probe_unit_generation when not injected."""
        monkeypatch.setattr(lifecycle, "VLLM_SERVE_VERIFY_TIMEOUT_S", 0.0)
        seen = []
        import hscc_daemon.health as health
        monkeypatch.setattr(health, "_probe_unit_generation",
                            lambda node, port, timeout=None, max_tokens=None:
                            seen.append((node, port)) or {"ok": True})
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        res = lifecycle.restart_vllm()
        assert res["ok"] is True and seen, "health probe never consulted"


# ── Lease discipline ────────────────────────────────────────────────────────

class TestRestartHoldsLease:
    def test_restart_holds_lease_across_stop_and_launch(self, env, monkeypatch):
        """While THIS restart is mid-flight the lease must be held by it, so
        the other three repair paths back off against it (F2 semantics).
        Mutation caught: dropping acquire/release around the mutating window."""
        serving.VLLM_RECIPE = "/tmp/models/orch-recipe.yaml"
        serving._rebuild_vllm_cmds()
        observed = []

        def spy_run(args, timeout=30, as_json=False, shell=False):
            env["argvs"].append(list(args))
            observed.append(heal_lease.read_lease())
            return {"ok": True, "output": "", "stderr": ""}

        monkeypatch.setattr(lifecycle, "run_cmd", spy_run)
        res = lifecycle.restart_vllm(verify_fn=lambda n, p, timeout: True)
        assert res["ok"] is True
        assert all(h and h["who"] == "vllm-auto-restart" for h in observed), \
            "stop/launch ran without holding the heal lease"
        # Released afterwards — the next repair may proceed.
        assert heal_lease.read_lease() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
