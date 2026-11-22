"""The down-repair paths must not heal a parked node (t_8e5ab6cb).

WHY THIS EXISTS
---------------
``hscc node pause`` parks ONE node on purpose and edits nothing the cluster is
built from, so a parked node's units are simply down with no record anywhere the
repair paths look that says "deliberate". Three paths in ``health.py`` read that
absence as a fault and acted on it:

* ``auto-heal`` — a whole-template force-recreate of the parked node's unit,
* ``worker-relaunch`` — a stop + re-issue of the same unit,
* ``proxy-relaunch`` — the fleet-wide ``sparkrun proxy start --restart``, which
  a parked node's missing alias provokes forever.

Each of them lands a mutation on the GPU the operator just freed, on top of
whatever is now using it. #568 (``heal_breaker.note_heal`` skipping accrual for
a pause) was correct and removed the only thing that limited the loop: a parked
node used to accrue strikes until the breaker refused further heals. This file
pins the seam that replaces that accidental backstop — the pause check at the
mutating surface itself, ahead of ``heal_lease.acquire`` and ahead of
``_note_heal_action``, so a parked node neither leases, nor strikes, nor heals.

THE BOTH-HALVES RULE
--------------------
A skip that always skips is worthless, so every property here is pinned from
both sides: a parked node gets no lease, no strike and no command, and an
unparked node — including a node sitting next to the parked one — is healed
EXACTLY as before. Over-suppression is the same class of bug as the false page a
seam removes, and the lease pin is asserted separately from the heal pin: a
seam that took a lease and then skipped would still be deferring real repairs
elsewhere in the fleet.

ASK ``covering``, NEVER ``paused``
---------------------------------
``node_pauses.paused(host)`` is host-keyed; the other nodes of a tensor-parallel
unit — stopped as one thing — appear only in the record's ``span_nodes``. A seam
that asked ``paused()`` would keep force-recreating the sibling node of a span
the operator parked. Every span test here asserts the two APIs answer
DIFFERENTLY for that node and that the behaviour follows the ``covering``
answer, so swapping ``covering()`` for ``paused()`` reddens this file. It is the
same pin the watchdog seam (t_4ecbd9d0) and the heal breaker (t_e6ee7e60) make —
every skip seam owes it.

FAIL-OPEN DIRECTION
-------------------
``node_pauses.load()`` answers "no pauses" for a missing, unreadable or
malformed overlay and WARNs loudly. A repair path must inherit that direction: a
broken overlay file may never freeze the fleet's ability to repair itself, so
the malformed-overlay tests assert the repair still runs, and still WARNs.

SAFETY
------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24, documentation space, never
this fleet). Every overlay lives in a per-test temp dir passed through
``node_pauses``' own ``directory=`` seam (threaded here as ``pause_directory``);
the tests that exercise the DEFAULT root plant their pause at the conftest-sealed
root and guard-assert that root is inside the tmp dir before writing. ``run_cmd``,
``subprocess.Popen``, ``http_check`` and every proxy probe are stubbed, so no
sparkrun, ssh, node or real ``~/.hscc`` is touched.
"""

import json
import os

import pytest

from hscc_daemon import health, heal_breaker, node_pauses

# TEST-NET-1 (RFC 5737) — documentation space, never routable, never this fleet.
# The exact numbers are not decoration: `_proxy_relaunch_hosts` SORTS the host
# set and the proxy seam is keyed on the leading host, so the numbers encode
# which host leads which test. Sort order is SIBLING(.10) < PAUSED(.20) <
# UNPARED(.30), and each test's host SET picks the leader:
#   [PAUSED, UNPARED] -> PAUSED leads   (the parked host IS the key)
#   [PAUSED, SIBLING] -> SIBLING leads  (only `covering()` can see the pause)
# so the three proxy postures need no per-test host plumbing.
PAUSED_NODE = "192.0.2.20"      # the node the operator parked
SPAN_SIBLING = "192.0.2.10"     # parked only via the pause's span_nodes
UNPARED_NODE = "192.0.2.30"     # a worker nobody parked, next to the parked one
ORCH_NODE = "192.0.2.1"

PORT = 8001


# ── shared fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root for this test's pauses (node_pauses' only seam)."""
    d = tmp_path / "overlay"
    d.mkdir()
    return str(d)


@pytest.fixture
def ledger(tmp_hfcc_dir, monkeypatch):
    """Point the strike ledger at an explicit tmp path so "no write at all" is
    a meaningful assertion (conftest's autouse seal already does this)."""
    path = str(tmp_hfcc_dir / "heal-breaker-repair-test.json")
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_FILE", path)
    if os.path.exists(path):
        os.unlink(path)
    return path


@pytest.fixture
def small_threshold(monkeypatch):
    """Lower the cap so a test needs 3 actions, not 7 (production cap is
    deliberately above a legitimate ``cluster up`` fan-out)."""
    monkeypatch.setattr(heal_breaker, "HEAL_BREAKER_MAX", 3)
    return 3


@pytest.fixture
def logs(monkeypatch):
    """Capture (level, message) lines from BOTH log bindings a repair tick can
    reach: ``health.log`` (the skip lines) and ``daemon_ops.log`` (what
    ``node_pauses._warn`` imports lazily, so a corrupt overlay's WARN lands
    here). A silent skip is indistinguishable from a unit nobody looked at.
    """
    from hscc_daemon import daemon_ops
    seen = []

    def _capture(msg, level="INFO"):
        seen.append((level, msg))

    monkeypatch.setattr(health, "log", _capture)
    monkeypatch.setattr(daemon_ops, "log", _capture)
    return seen


def park(host, directory, **kw):
    """Record a pause in this test's overlay; returns the stored entry."""
    return node_pauses.pause(host, directory=directory, **kw)


def sealed_default_root():
    """The conftest-sealed ``~/.hscc`` — the only default root a test may write."""
    return str(node_pauses.HSCC_DIR)


def assert_sealed_default_root(tmp_hfcc_dir):
    """Refuse to plant a pause anywhere but this test's tmp root.

    The default-root tests exercise the path the daemon actually takes (no
    ``pause_directory``), which means they write to ``node_pauses.HSCC_DIR``.
    conftest's autouse seal moves that constant — and HOME — into the per-test
    tmp dir, so comparing it against ``expanduser('~/.hscc')`` would pass either
    way; the only honest guard is equality with THIS test's tmp dir. It dies
    rather than parking a node on the live fleet if the seal ever moves.
    """
    assert os.path.realpath(node_pauses.HSCC_DIR) == os.path.realpath(
        str(tmp_hfcc_dir)), (
        "conftest's node_pauses.HSCC_DIR seal moved; refusing to write a pause "
        "anywhere but the per-test tmp root")


# ── the check_workers harness (auto-heal + worker-relaunch) ────────────────

class _WorkerRig:
    """``check_workers()`` with every mutating surface recorded.

    One rig, two paths: ``auto_heal`` sets the debounce to 1 so the down tick
    reaches the force-recreate gate, ``relaunch`` sets it far above the streak so
    the same tick falls through to the gentle per-unit relaunch gate. Both gates
    sit behind their own pause seam, and both are pinned separately here.
    """

    def __init__(self, tmp_hfcc_dir, monkeypatch, nodes, *, mode):
        from hscc_daemon import serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))

        units = [{"id": "orch", "role": "orchestrator", "nodes": [ORCH_NODE],
                  "recipe": "~/r/orch.yaml", "model": "M"}]
        for n in nodes:
            units.append({"id": f"w-{n}", "role": "worker", "keepalive": True,
                          "nodes": [n], "recipe": "~/r/27b.yaml", "model": "W"})
        monkeypatch.setattr(serving, "ORCH_NODES", {ORCH_NODE})
        monkeypatch.setattr(serving, "load_serving",
                            lambda *a, **k: {"version": 1, "units": units})

        for d in ("_worker_relaunch_at", "_worker_down_streak",
                  "_worker_last_autoheal", "_worker_container_state"):
            getattr(health, d).clear()

        # Not loading: an old container, so neither gate's load-grace shield
        # hides the tick from the seam under test.
        old = __import__("time").time() - health.WORKER_AUTOHEAL_LOAD_GRACE - 60
        monkeypatch.setattr(health, "_worker_container_created_ts",
                            lambda node, port: old)
        # Down, and nothing real is ever invoked: every mutating surface below
        # records instead of acting.
        monkeypatch.setattr(health, "http_check",
                            lambda url, timeout=5: {"ok": False})
        self.calls = {"leases": [], "releases": [], "recreates": [],
                      "cmds": [], "pops": []}
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: self.calls["cmds"].append(args)
                            or {"ok": True})
        monkeypatch.setattr(health.subprocess, "Popen",
                            lambda *a, **k: self.calls["pops"].append(a))
        monkeypatch.setattr(health, "_autoheal_worker_fn",
                            lambda *a: self.calls["recreates"].append(a)
                            or {"status": "ok"})
        # The launch guard is a separate seam (A2, foreign vLLM on the GPU) with
        # its own tests; here it must never be the reason nothing happened.
        monkeypatch.setattr(health, "_guard_launch_or_refuse",
                            lambda *a, **k: True)

        import hscc_daemon.heal_lease as heal_lease
        monkeypatch.setattr(heal_lease, "acquire",
                            lambda actor, detail="": (
                                self.calls["leases"].append((actor, detail))
                                or (True, None)))
        monkeypatch.setattr(heal_lease, "release",
                            lambda actor: self.calls["releases"].append(actor))
        monkeypatch.setattr(health, "heal_lease", heal_lease)

        monkeypatch.setattr(health, "WORKER_AUTOHEAL_DEBOUNCE",
                            1 if mode == "auto_heal" else 99)
        monkeypatch.setattr(health, "WORKER_AUTOHEAL_COOLDOWN_MINUTES", 0)
        self.units = [f"w-{n}" for n in nodes]
        self.health = health


# ── the check_proxy harness ────────────────────────────────────────────────

class _ProxyRig:
    """``check_proxy()`` on a proxy whose catalogue is missing the alias the
    parked node's unit used to serve — the shape that made this path relaunch
    the whole fleet's LB once per cooldown tick, forever."""

    def __init__(self, tmp_hfcc_dir, monkeypatch, hosts, leading_unit_host):
        from hscc_daemon import serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(health, "log", lambda *a, **kw: None)
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", set(hosts))
        serving_data = {"version": 2, "units": [
            {"id": "w1", "role": "worker", "keepalive": True,
             "nodes": [leading_unit_host], "port": PORT},
        ]}
        monkeypatch.setattr(serving, "load_serving",
                            lambda *a, **k: serving_data)
        monkeypatch.setattr(
            health, "_router_drift_probe",
            lambda: {"status": "skip", "warn": False, "detail": "stubbed"})
        # The proxy ANSWERS (so this is not a dead-LB tick) but serves an alias
        # serving.json does not recognise: unhealthy, and the repair this path
        # knows is `proxy start`.
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": True,
                                                    "models": ["some-model"],
                                                    "error": ""})
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: {
                                "ok": False, "backends": set(),
                                "error": "stubbed — drift check skipped"})
        self.calls = {"leases": [], "cmds": []}
        monkeypatch.setattr(health, "run_cmd",
                            lambda args, **k: self.calls["cmds"].append(args)
                            or {"ok": False})
        import hscc_daemon.heal_lease as heal_lease
        monkeypatch.setattr(heal_lease, "acquire",
                            lambda actor, detail="": (
                                self.calls["leases"].append((actor, detail))
                                or (True, None)))
        monkeypatch.setattr(heal_lease, "release", lambda actor: None)
        monkeypatch.setattr(health, "heal_lease", heal_lease)
        self.state_dir = state_dir
        self.health = health

    def seed_relaunch_stamp(self, iso):
        """Pre-date the durable cooldown stamp so a skip must carry it forward
        verbatim instead of buying a fresh window for a repair it did not make."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "proxy.json").write_text(
            json.dumps({"ok": False, "last_relaunch": iso}))

    def state(self):
        return json.loads((self.state_dir / "proxy.json").read_text())


# ── the force-recreate path ────────────────────────────────────────────────

class TestAutoHealLeavesAParkedNodeAlone:
    """``auto-heal`` is the most destructive of the four repair paths: it
    recreates the whole template. On a parked node that is the one outcome the
    overlay exists to forbid."""

    def test_a_parked_node_is_never_force_recreated(self, tmp_hfcc_dir,
                                                    monkeypatch, overlay,
                                                    ledger, logs):
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        park(PAUSED_NODE, overlay, reason="training run")
        for _ in range(5):
            health.check_workers(pause_directory=overlay)
        assert rig.calls["recreates"] == [], "a parked node was force-recreated"
        assert rig.calls["leases"] == [], "a skip took a heal lease anyway"
        assert rig.calls["releases"] == []
        assert heal_breaker.strike_count(PAUSED_NODE) == 0
        assert os.path.exists(ledger) is False, (
            "the suppressed tick opened the strike ledger for writing")
        # And the tick was not silent: a WARN names the node and the reason.
        skip_lines = [m for lvl, m in logs if lvl == "WARN" and "paused" in m]
        assert skip_lines, f"the skip was not logged: {logs}"
        assert PAUSED_NODE in skip_lines[0] and "training run" in skip_lines[0]

    def test_the_stream_says_why_nothing_was_done(self, tmp_hfcc_dir,
                                                  monkeypatch, overlay, logs):
        from hscc_daemon import state as state_mod
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        park(PAUSED_NODE, overlay, reason="training run")
        assert health.check_workers(pause_directory=overlay) is False
        st = state_mod.read_state("workers")
        label = f"w-{PAUSED_NODE}"
        assert st["down"] == [label], "a parked unit is still down — the truth"
        assert st["paused"] == [label], "the stream must say WHY nothing ran"
        assert "parked" in st["message"]

    def test_the_same_node_with_no_pause_is_still_force_recreated(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        """THE CONTROL. Without it every assertion above is satisfied by a
        repair path that simply never fires."""
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        health.check_workers(pause_directory=overlay)
        assert len(rig.calls["recreates"]) == 1
        assert len(rig.calls["leases"]) == 1
        assert rig.calls["leases"][0][0] == "auto-heal"
        assert rig.calls["leases"][0][1].startswith(f"{PAUSED_NODE}:"), (
            "the lease must name the node the repair acts on")
        assert heal_breaker.strike_count(PAUSED_NODE) == 1

    def test_a_pause_on_another_node_does_not_disarm_the_recreate(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        """Over-suppression is the same class of bug as the false page: a real
        fault on a node nobody parked must still be repaired."""
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch,
                         [PAUSED_NODE, UNPARED_NODE], mode="auto_heal")
        park(PAUSED_NODE, overlay, reason="training run")
        health.check_workers(pause_directory=overlay)
        recreated_nodes = [c[2] for c in rig.calls["recreates"]]
        assert recreated_nodes == [UNPARED_NODE]
        # Only the unparked node may take a lease; the parked one may not even
        # advertise a repair that will not happen.
        assert [c[0] for c in rig.calls["leases"]] == ["auto-heal"]
        assert len(rig.calls["leases"]) == 1
        assert heal_breaker.strike_count(UNPARED_NODE) == 1
        assert heal_breaker.strike_count(PAUSED_NODE) == 0

    def test_a_span_sibling_is_never_force_recreated(self, tmp_hfcc_dir,
                                                     monkeypatch, overlay,
                                                     ledger, logs):
        """The unit was stopped as ONE thing across two hosts; the sibling is
        parked too even though no pause is keyed to it."""
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [SPAN_SIBLING],
                         mode="auto_heal")
        park(PAUSED_NODE, overlay, reason="tp2 training run",
             span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        # The two APIs genuinely disagree here — that is the pin.
        assert node_pauses.paused(SPAN_SIBLING, directory=overlay) is None
        assert node_pauses.covering(SPAN_SIBLING, directory=overlay) is not None
        health.check_workers(pause_directory=overlay)
        assert rig.calls["recreates"] == []
        assert rig.calls["leases"] == []
        assert heal_breaker.strike_count(SPAN_SIBLING) == 0


# ── the gentle per-unit relaunch path ──────────────────────────────────────

class TestGentleRelaunchLeavesAParkedNodeAlone:
    """The gentle relaunch is still ``sparkrun stop`` + re-issue of the unit's
    whole span: it disturbs whatever replaced the unit and re-occupies the GPU
    the operator freed, so it is skipped for exactly the same reason."""

    def test_a_parked_node_is_never_relaunched(self, tmp_hfcc_dir, monkeypatch,
                                               overlay, ledger, logs):
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="relaunch")
        park(PAUSED_NODE, overlay, reason="training run")
        for _ in range(3):
            health.check_workers(pause_directory=overlay)
        assert rig.calls["cmds"] == [], "a parked node got a `sparkrun stop`"
        assert rig.calls["pops"] == [], "a parked node got a relaunch"
        assert rig.calls["leases"] == []
        assert heal_breaker.strike_count(PAUSED_NODE) == 0
        assert os.path.exists(ledger) is False

    def test_the_same_node_with_no_pause_is_still_relaunched(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="relaunch")
        health.check_workers(pause_directory=overlay)
        assert len(rig.calls["leases"]) == 1
        assert rig.calls["leases"][0][0] == "worker-relaunch"
        assert rig.calls["leases"][0][1].startswith(f"{PAUSED_NODE}:")
        assert len(rig.calls["cmds"]) == 1, "expected the `sparkrun stop`"
        assert len(rig.calls["pops"]) == 1, "expected the relaunch Popen"
        assert heal_breaker.strike_count(PAUSED_NODE) == 1

    def test_a_pause_on_another_node_does_not_disarm_the_relaunch(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch,
                         [PAUSED_NODE, UNPARED_NODE], mode="relaunch")
        park(PAUSED_NODE, overlay, reason="training run")
        health.check_workers(pause_directory=overlay)
        assert [c[0] for c in rig.calls["leases"]] == [
            "worker-relaunch"], "only the unparked node may lease"
        assert len(rig.calls["leases"]) == 1
        assert heal_breaker.strike_count(UNPARED_NODE) == 1
        assert heal_breaker.strike_count(PAUSED_NODE) == 0

    def test_a_span_sibling_is_never_relaunched(self, tmp_hfcc_dir, monkeypatch,
                                                overlay, ledger, logs):
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [SPAN_SIBLING],
                         mode="relaunch")
        park(PAUSED_NODE, overlay, reason="tp2 training run",
             span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        assert node_pauses.paused(SPAN_SIBLING, directory=overlay) is None
        health.check_workers(pause_directory=overlay)
        assert rig.calls["leases"] == []
        assert rig.calls["pops"] == []
        assert heal_breaker.strike_count(SPAN_SIBLING) == 0


# ── the fleet-wide proxy relaunch ──────────────────────────────────────────

class TestProxyRelaunchLeavesAParkedNodeAlone:
    """A parked node keeps its alias out of the catalogue, which makes this
    check see an "unhealthy" proxy forever. The repair it knows —
    ``sparkrun proxy start --restart`` — cannot fix a gap the operator made on
    purpose; it only SIGTERMs the working LB once per cooldown tick and then
    pages about the operator's own command."""

    def test_a_parked_key_gets_no_lease_no_strike_and_no_command(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                        leading_unit_host=PAUSED_NODE)
        park(PAUSED_NODE, overlay, reason="training run")
        assert health.check_proxy(pause_directory=overlay) is False
        assert rig.calls["cmds"] == [], "the fleet LB was restarted for a pause"
        assert rig.calls["leases"] == []
        assert heal_breaker.strike_count(PAUSED_NODE) == 0
        assert os.path.exists(ledger) is False
        st = rig.state()
        assert st["paused"] is True, "the stream must say it skipped"
        assert st["relaunch_failed"] is False, (
            "nothing was attempted; a skip is not a failed repair")
        assert st["deferred"] is False, "a skip is not a lease deferral"
        assert PAUSED_NODE in st["message"] and "training run" in st["message"]

    def test_a_skip_does_not_buy_the_cooldown_a_fresh_window(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger):
        """Same rule as a suppressed or lease-deferred tick: re-stamping on a
        tick that attempted nothing would push the window out every tick and
        make the cooldown perpetual."""
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                        leading_unit_host=PAUSED_NODE)
        rig.seed_relaunch_stamp("2026-01-01T00:00:00+00:00")
        park(PAUSED_NODE, overlay, reason="training run")
        health.check_proxy(pause_directory=overlay)
        assert rig.state()["last_relaunch"] == "2026-01-01T00:00:00+00:00"

    def test_with_no_pause_the_proxy_is_still_relaunched(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger):
        """THE CONTROL: the same tick, no pause, must still strike and still
        invoke `sparkrun proxy start`."""
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                        leading_unit_host=PAUSED_NODE)
        health.check_proxy(pause_directory=overlay)
        assert rig.calls["leases"] == [("proxy-relaunch",
                                        f"worker proxy on :{health.PROXY_PORT}")]
        assert len(rig.calls["cmds"]) == 1
        assert heal_breaker.strike_count(PAUSED_NODE) == 1
        assert rig.state()["paused"] is False

    def test_a_pause_on_a_host_that_is_not_the_key_still_repairs_the_proxy(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger):
        """The strike and the skip share ONE key — the leading relaunch host —
        so a pause elsewhere must not disarm the fleet's load-balancer."""
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                        leading_unit_host=PAUSED_NODE)
        park(UNPARED_NODE, overlay, reason="someone else's training run")
        health.check_proxy(pause_directory=overlay)
        assert len(rig.calls["cmds"]) == 1
        assert heal_breaker.strike_count(PAUSED_NODE) == 1

    def test_a_span_sibling_key_is_skipped_too(self, tmp_hfcc_dir, monkeypatch,
                                               overlay, ledger):
        """``.11`` is covered only by ``span_nodes`` and it is the leading
        relaunch host, so only ``covering()`` can see the pause here."""
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, SPAN_SIBLING],
                        leading_unit_host=SPAN_SIBLING)
        park(PAUSED_NODE, overlay, reason="tp2 training run",
             span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        assert node_pauses.paused(SPAN_SIBLING, directory=overlay) is None
        assert node_pauses.covering(SPAN_SIBLING, directory=overlay) is not None
        health.check_proxy(pause_directory=overlay)
        assert rig.calls["cmds"] == []
        assert rig.calls["leases"] == []
        assert heal_breaker.strike_count(SPAN_SIBLING) == 0


# ── the seam is the daemon's seam, not just a threaded parameter ───────────

class TestTheDefaultRootIsTheOneTheDaemonUses:
    """The daemon calls ``check_workers()`` / ``check_proxy()`` with no
    ``pause_directory`` — production reads the default root. A seam that only
    worked when a caller threaded a directory would be a seam nobody uses."""

    def test_a_pause_at_the_default_root_stops_the_force_recreate(
            self, tmp_hfcc_dir, monkeypatch, ledger, logs):
        assert_sealed_default_root(tmp_hfcc_dir)
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        park(PAUSED_NODE, sealed_default_root(), reason="training run")
        for _ in range(3):
            health.check_workers()
        assert rig.calls["recreates"] == []
        assert rig.calls["leases"] == []
        assert heal_breaker.strike_count(PAUSED_NODE) == 0

    def test_a_pause_at_the_default_root_stops_the_proxy_relaunch(
            self, tmp_hfcc_dir, monkeypatch, ledger):
        assert_sealed_default_root(tmp_hfcc_dir)
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                        leading_unit_host=PAUSED_NODE)
        park(PAUSED_NODE, sealed_default_root(), reason="training run")
        health.check_proxy()
        assert rig.calls["cmds"] == []
        assert heal_breaker.strike_count(PAUSED_NODE) == 0

    def test_the_overlay_is_read_never_written_by_a_repair_tick(
            self, tmp_hfcc_dir, monkeypatch, overlay):
        """The repair paths consult the overlay; only the CLI and the resume
        path end a pause."""
        park(PAUSED_NODE, overlay, reason="training run",
             units=[f"w-{PAUSED_NODE}"], span_nodes=[PAUSED_NODE, SPAN_SIBLING])
        path = os.path.join(overlay, node_pauses.OVERLAY_NAME)
        before = open(path, "rb").read()
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        for _ in range(3):
            health.check_workers(pause_directory=overlay)
        rig2 = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                         leading_unit_host=PAUSED_NODE)
        health.check_proxy(pause_directory=overlay)
        assert open(path, "rb").read() == before


# ── fail-open direction ────────────────────────────────────────────────────

class TestFailOpenDirection:
    """A broken overlay must never freeze the fleet's ability to repair itself.
    ``node_pauses.load()`` reads "no pauses" for a missing or malformed file
    (loudly WARNed) and these paths inherit that direction, so a real outage
    behind a corrupted file still gets healed."""

    def _malform(self, overlay):
        with open(os.path.join(overlay, node_pauses.OVERLAY_NAME), "w") as f:
            f.write('{"pauses": [this is not an object')

    def test_a_malformed_overlay_still_heals_the_worker(self, tmp_hfcc_dir,
                                                        monkeypatch, overlay,
                                                        ledger, logs):
        self._malform(overlay)
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [UNPARED_NODE],
                         mode="auto_heal")
        health.check_workers(pause_directory=overlay)
        assert len(rig.calls["recreates"]) == 1, (
            "a corrupt overlay froze the repair path — the wrong failure")
        assert heal_breaker.strike_count(UNPARED_NODE) == 1
        # And the corruption is not silent: node_pauses' own WARN reaches the log.
        from hscc_daemon import daemon_ops
        warns = [m for lvl, m in logs if lvl == "WARN"]
        assert warns, "the repair ran but nobody was told the overlay was junk"

    def test_a_malformed_overlay_still_heals_the_proxy(self, tmp_hfcc_dir,
                                                      monkeypatch, overlay,
                                                      ledger):
        self._malform(overlay)
        rig = _ProxyRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE, UNPARED_NODE],
                        leading_unit_host=PAUSED_NODE)
        health.check_proxy(pause_directory=overlay)
        assert len(rig.calls["cmds"]) == 1
        assert heal_breaker.strike_count(PAUSED_NODE) == 1

    def test_a_missing_overlay_is_not_a_pause(self, tmp_hfcc_dir, monkeypatch,
                                              overlay, ledger, logs):
        assert os.path.exists(os.path.join(overlay,
                                           node_pauses.OVERLAY_NAME)) is False
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [UNPARED_NODE],
                         mode="auto_heal")
        health.check_workers(pause_directory=overlay)
        assert len(rig.calls["recreates"]) == 1
        assert len(rig.calls["leases"]) == 1
        assert rig.calls["leases"][0][0] == "auto-heal"


# ── the seam's own contract, pinned against the cheap rewrites ─────────────

class TestTheSeamIsNotDecoration:
    """Mutation-shaped pins: each one dies at exactly one cheap rewrite of the
    seam, which is how a reviewer can tell these are load-bearing."""

    def test_the_pause_is_asked_before_the_lease_not_after_it(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        """If the order ever flips, the skip still returns no command — but a
        lease is taken and released, which tells every OTHER repair path that a
        repair is in flight for a repair we decided not to perform. Recording the
        acquire (rather than the outcome) is what makes that reddens here."""
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        park(PAUSED_NODE, overlay, reason="training run")
        health.check_workers(pause_directory=overlay)
        assert rig.calls["leases"] == []
        assert rig.calls["releases"] == []

    def test_the_skip_does_not_swallow_the_down_verdict(self, tmp_hfcc_dir,
                                                        monkeypatch, overlay,
                                                        logs):
        """A parked unit is DOWN. A seam that quietly filed it as online would
        turn a skipped repair into a green board over a dead fleet."""
        from hscc_daemon import state as state_mod
        _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE], mode="auto_heal")
        park(PAUSED_NODE, overlay, reason="training run")
        assert health.check_workers(pause_directory=overlay) is False
        st = state_mod.read_state("workers")
        assert st["ok"] is False and f"w-{PAUSED_NODE}" in st["down"]

    def test_an_empty_pause_reason_still_reports_a_skip(
            self, tmp_hfcc_dir, monkeypatch, overlay, ledger, logs):
        """``hscc node pause`` without ``--reason`` is the common case; the
        skip line must not depend on one existing."""
        rig = _WorkerRig(tmp_hfcc_dir, monkeypatch, [PAUSED_NODE],
                         mode="auto_heal")
        park(PAUSED_NODE, overlay)
        health.check_workers(pause_directory=overlay)
        assert rig.calls["recreates"] == []
        assert any("paused" in m for _lvl, m in logs)
