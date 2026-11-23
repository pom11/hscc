"""A resume re-attach must not take the fleet's :4000 down (t_f4ee3ce6).

WHY THIS EXISTS
---------------
``hscc node resume`` brings a unit back healthy. The running worker proxy on
``:4000`` — started while that node was paused — has no backend for it, so the
F4 check reads MISSING and the tick repaired it with ``_proxy_repair_orphan``:
``sparkrun proxy stop`` → reap → ``sparkrun proxy start`` (a full fleet
discovery sweep). All of that happens AFTER the port is dead. Measured live on
2026-11-22 (one resumed node): drift detected 20:02:21.254Z, the repair's own
line at 20:02:21.257Z, the port back at 20:02:35.806Z — a **14.6 s window where
:4000 refused every connection**, and the line said FAILED while the very next
check tick was green.

WHY THE FIX IS THE ORDERING AND NOT "NO RESTART"
------------------------------------------------
There is no supported way to add a backend to the running litellm gateway: its
runtime ``/model/new`` needs a DB (sparkrun ``plugins/litellm/engine.py::
apply_desired_state`` — "Applying a change therefore means rewriting the config
and restarting the process"), and ``proxy sync`` and the 30 s auto-discover
sweep land in the same restart. What hscc CAN stop doing is authoring the
break itself: sparkrun's own ``proxy start`` discovers and PRE-VIEWS the config
while the old gateway still serves and stops it only at the last moment
(``api/proxy/_ops.py::start()``), so a re-attach goes through that one
command. The residual stop→bind window is sparkrun's to close.

WHAT THIS FILE PINS
-------------------
1. NO STOP FOR A RE-ATTACH — a MISSING-only drift on a gateway sparkrun tracks
   emits no ``proxy stop`` and no reap: one ``proxy start … --restart``.
2. OLD SERVES UNTIL NEW BINDS — no sparkrun command in the sequence is ever
   issued while :4000 has no owner. The replacement is prepared before the
   break, which is the whole defect.
3. THE UVX SHAPE VERIFIES — ``sparkrun proxy start`` launches through
   ``uv tool uvx``, so sparkrun's record is the wrapper and the listener is its
   litellm child (measured: owner 15706, tracked 15701). That must verify, not
   read as a failed repair.
4. THE CONTROLS — a foreign listener, a hollow PID record and a dead backend in
   the router each STILL take ``_proxy_repair_orphan`` (stop + reap + start),
   and an adopt that cannot prove the gateway came back falls through to it in
   the same tick. Without these, pins 1-3 are satisfied by a repair that never
   fires.

SAFETY (the no-live rule)
-------------------------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24), never this fleet. The
``sparkrun`` under test is a planted script pinned through ``HSCC_SPARKRUN_BIN``
and kept OFF PATH (the conftest fence identifies the live CLI with
``shutil.which``), so the real CLI, the real venv and the fleet are never
reached and ``ssh``/``docker`` are never invoked. ``_proxy_listener`` (lsof),
``_proxy_tracked_pid``, the parent-chain walk and the reap are stubbed: no test
touches a real ``:4000``, a real PID table or the real ``~/.hscc`` (state goes
through ``state.STATE_DIR``, the overlay through ``node_pauses``' ``directory=``).
"""

import os
import shutil
import sys

import pytest

from hscc_daemon import health

# TEST-NET-1 (RFC 5737). UP was paused and has just been resumed: its unit is
# healthy again and the live router has no backend for it — the re-attach.
RESUMED = "192.0.2.31"
UP = "192.0.2.30"
DEAD = "192.0.2.40"
PORT = 8001

# The two processes ONE gateway is on this fleet: sparkrun records the uvx
# wrapper, the port is held by its litellm child.
WRAPPER_PID = 4101
CHILD_PID = 4102
FOREIGN_PID = 31337
GATEWAY_CMD = "litellm --config /tmp/fake-litellm_config.yaml --port 4000"


def hp(host, port=PORT):
    return f"{host}:{port}"


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    """Record what the stream would tell the operator — never a real notify."""
    seen = {"notify": [], "events": []}
    monkeypatch.setattr(
        health, "send_macos_notification",
        lambda title, msg, priority="normal": seen["notify"].append((title, msg, priority)))
    monkeypatch.setattr(
        health, "emit_event",
        lambda name, data, severity="info": seen["events"].append((name, data, severity)))
    return seen


@pytest.fixture
def logs(monkeypatch):
    from hscc_daemon import daemon_ops
    seen = []

    def _capture(msg, level="INFO"):
        seen.append((level, msg))

    monkeypatch.setattr(health, "log", _capture)
    monkeypatch.setattr(daemon_ops, "log", _capture)
    return seen


@pytest.fixture
def fake_sparkrun(tmp_path, monkeypatch):
    """Planted ``sparkrun`` whose port model reproduces the uvx wrapper/child.

    ``proxy status --json`` reports the WRAPPER pid (what sparkrun's state file
    holds); after a successful ``proxy start`` the listener on :4000 is its
    litellm CHILD. ``proxy stop`` leaves the port with no owner. Every argv is
    logged, together with who owned the port at the moment the command was
    issued — that pairing is what pins "prepared before the break".
    """
    bin_dir = tmp_path / "planted-sparkrun"
    bin_dir.mkdir()
    cli = bin_dir / "sparkrun"
    cli.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "with open(os.environ['FAKE_CALL_LOG'], 'a') as fh:\n"
        "    fh.write(' '.join(argv) + '\\n')\n"
        "if argv[:2] == ['proxy', 'status']:\n"
        "    print(json.dumps({'pid': %d}))\n"
        "    sys.exit(0)\n"
        "if argv[:2] == ['proxy', 'start'] and '--restart' in argv:\n"
        "    open(os.environ['FAKE_STARTED'], 'w').write('1')\n"
        "    sys.exit(0)\n"
        "if argv[:2] == ['proxy', 'stop']:\n"
        "    open(os.environ['FAKE_STOPPED'], 'w').write('1')\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n" % WRAPPER_PID)
    cli.chmod(0o755)
    assert os.path.realpath(shutil.which("sparkrun") or "") != \
        os.path.realpath(str(cli)), \
        "the planted CLI must stay off PATH — the fence identifies the live " \
        "CLI with shutil.which and would mis-classify the fake"
    call_log = tmp_path / "sparkrun-calls.log"
    call_log.write_text("")
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
    monkeypatch.setenv("FAKE_CALL_LOG", str(call_log))
    monkeypatch.setenv("FAKE_STARTED", str(started))
    monkeypatch.setenv("FAKE_STOPPED", str(stopped))
    health.sparkrun_bin.reset_cache()
    yield {"log": call_log, "started": started, "stopped": stopped}
    health.sparkrun_bin.reset_cache()


class _Port:
    """Who owns :4000, derived from what the planted CLI has done so far.

    Before any start: the wrapper's litellm child (the running gateway). After
    ``proxy stop``: nobody — :4000 refuses every connection. After a
    ``proxy start``: a NEW child of the wrapper, which is the shape production
    verification used to call a failure.
    """

    def __init__(self, monkeypatch, fake, owner_pid=CHILD_PID, tracked=WRAPPER_PID,
                 ppid_of_child=WRAPPER_PID, tracked_alive=True,
                 owner_after_start=None):
        self.fake = fake
        self.owner_pid = owner_pid
        self.owner_after_start = owner_after_start
        self.tracked = tracked
        self.tracked_alive = tracked_alive
        monkeypatch.setattr(health, "_proxy_listener", lambda: self._listener())
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: self.tracked)
        # No verification wait: these pins are about the COMMANDS and the
        # verdict, and the production bounded wait is covered elsewhere.
        monkeypatch.setattr(health, "PROXY_REPAIR_VERIFY_WAIT_S", 0.0)

        def _ppid(pid):
            if pid == CHILD_PID:
                return ppid_of_child
            return 1

        monkeypatch.setattr(health, "_proc_ppid", _ppid)
        monkeypatch.setattr(health, "_proc_alive",
                            lambda pid: pid == self.tracked and self.tracked_alive)

    def _listener(self):
        """Who owns :4000 right now (0 owner_pid means nothing is listening)."""
        if self.fake["stopped"].exists() and not self.fake["started"].exists():
            return None, None
        pid = self.owner_pid
        if self.owner_after_start is not None and self.fake["started"].exists():
            # The replacement came back as something OTHER than the gateway
            # sparkrun tracks — the shape an adopt cannot verify.
            pid = self.owner_after_start
        if not pid:
            return None, None
        return pid, GATEWAY_CMD


class _Rig:
    """``check_proxy()`` over a live proxy whose catalogue is healthy and whose
    live router points at whatever backends a test names."""

    def __init__(self, tmp_hfcc_dir, monkeypatch, hosts, live, healthy_units,
                 fleet=None, port=None):
        from hscc_daemon import serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", set(hosts))
        serving_data = {"version": 2, "units": [
            {"id": f"w-{h}", "role": "worker", "keepalive": True,
             "nodes": [h], "port": PORT, "recipe": "r"} for h in hosts]}
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: serving_data)
        monkeypatch.setattr(health, "_router_drift_probe",
                            lambda: {"status": "skip", "warn": False, "detail": "stubbed"})
        monkeypatch.setattr(health, "_unit_alias_check_fn",
                            lambda serving_data=None: {"ok": True, "findings": [],
                                                       "unverified": [],
                                                       "message": "stubbed"})
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": True,
                                                    "models": ["worker-model"],
                                                    "error": ""})
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: {"ok": True,
                                                         "backends": set(live),
                                                         "error": ""})
        scope = set(fleet) if fleet is not None else (set(live) | set(healthy_units))
        monkeypatch.setattr(health, "_fleet_unit_backends",
                            lambda serving_data=None, probe=None:
                            (set(healthy_units), scope))
        self.port = port
        self.reaps = []
        monkeypatch.setattr(health, "_reap_orphan_proxy_listener",
                            lambda tracked: self.reaps.append(tracked) or True)
        self.issued = []  # (argv, port owner at issue time)
        _real_run_cmd = health.run_cmd

        def _run_cmd(args, **kw):
            argv = [str(a) for a in (args or [])]
            if argv and argv[0].endswith("sparkrun") and len(argv) > 2:
                owner, _cmd = self.port._listener() if self.port else (None, None)
                self.issued.append((argv[1:], owner))
            return _real_run_cmd(args, **kw)

        monkeypatch.setattr(health, "run_cmd", _run_cmd)
        self.leases = []
        import hscc_daemon.heal_lease as heal_lease
        monkeypatch.setattr(heal_lease, "acquire",
                            lambda actor, detail="": (self.leases.append((actor, detail))
                                                      or (True, None)))
        monkeypatch.setattr(heal_lease, "release", lambda actor: None)
        monkeypatch.setattr(health, "heal_lease", heal_lease)
        self._state_dir = state_dir

    def state(self):
        import json
        return json.loads((self._state_dir / "proxy.json").read_text())


def sparkrun_log(fake):
    return [line for line in fake["log"].read_text().splitlines() if line.strip()]


def commands(fake):
    return [" ".join(line.split()[:2]) for line in sparkrun_log(fake)]


def start_argvs(fake):
    return [line.split() for line in sparkrun_log(fake)
            if line.startswith("proxy start")]


# ── 1 + 2 — the re-attach never breaks the port by hand ────────────────────

class TestReattachNeverStopsTheProxy:
    def test_a_resumed_backend_is_adopted_not_stopped(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        """THE CARD: RESUMED is healthy, the live router has no backend for it,
        the listener is sparkrun's own gateway. No stop, no reap, one
        ``proxy start … --restart``."""
        port = _Port(monkeypatch, fake_sparkrun)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        health.check_proxy()
        cmds = commands(fake_sparkrun)
        assert "proxy stop" not in cmds, \
            f"a resume re-attach stopped the fleet proxy: {sparkrun_log(fake_sparkrun)}"
        assert rig.reaps == [], f"a re-attach reached for the reap: {rig.reaps}"
        starts = start_argvs(fake_sparkrun)
        assert len(starts) == 1, f"expected ONE start, got {starts}"
        assert "--restart" in starts[0], \
            "the re-attach must go through sparkrun's own restart"
        assert rig.state()["ok"] is True, "the stream filed the resume as a failure"

    def test_every_command_is_issued_while_the_old_gateway_still_serves(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        """MAKE-BEFORE-BREAK, pinned offline: hscc must never act on a port that
        is already dead. ``_ops.py::start()`` prepares the config before it
        stops, so the only command this path issues is issued with the old
        gateway still owning :4000."""
        port = _Port(monkeypatch, fake_sparkrun)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        health.check_proxy()
        assert rig.issued, "nothing was issued at all"
        for argv, owner in rig.issued:
            assert owner is not None, (
                f"`{' '.join(argv)}` was issued while :4000 had no owner — that "
                "is the refusal window this card is about")

    def test_the_adopt_is_the_one_command_and_names_the_hosts(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        port = _Port(monkeypatch, fake_sparkrun)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        health.check_proxy()
        argv = start_argvs(fake_sparkrun)[0]
        assert argv[:2] == ["proxy", "start"]
        hosts_arg = argv[argv.index("--hosts") + 1]
        assert set(hosts_arg.split(",")) == {UP, RESUMED}, hosts_arg
        assert "--port" in argv and str(health.PROXY_PORT) in argv


# ── 3 — the uvx wrapper/child shape verifies ───────────────────────────────

class TestGatewayIdentity:
    """The verdict ``_proxy_gateway_identity`` reaches about who owns :4000."""

    def _wire(self, monkeypatch, listener, tracked, *, ppid=None, alive=True,
              config_matches=True):
        monkeypatch.setattr(health, "_proxy_listener", lambda: listener)
        monkeypatch.setattr(health, "_proxy_tracked_pid", lambda: tracked)

        def _ppid(pid):
            if callable(ppid):
                return ppid(pid)
            return ppid if pid == listener[0] else 1

        monkeypatch.setattr(health, "_proc_ppid", _ppid)
        monkeypatch.setattr(health, "_proc_alive", lambda pid: bool(alive))
        from hscc_daemon import proxy_router
        monkeypatch.setattr(proxy_router, "LITELLM_CONFIG_PATH",
                            "/tmp/fake-litellm_config.yaml")
        if not config_matches:
            monkeypatch.setattr(health, "_is_sparkrun_gateway_config",
                                lambda path: False)

    def test_the_recorded_pid_listening_is_tracked(self, monkeypatch):
        self._wire(monkeypatch, (WRAPPER_PID, GATEWAY_CMD), WRAPPER_PID)
        assert health._proxy_gateway_identity()[0] == "tracked"

    def test_the_uvx_child_listening_is_a_child_not_a_failure(self, monkeypatch):
        """MEASURED SHAPE (2026-11-22): port owner 15706, sparkrun tracks 15701,
        and 15706 is 15701's child. One gateway — today this reads FAILED."""
        self._wire(monkeypatch, (CHILD_PID, GATEWAY_CMD), WRAPPER_PID,
                   ppid=WRAPPER_PID)
        assert health._proxy_gateway_identity()[0] == "child"

    def test_a_foreign_listener_is_never_trusted(self, monkeypatch):
        """The F4 posture is unchanged: a litellm on the port that is NOT the
        tracked gateway (and not its child) is the orphan shape."""
        self._wire(monkeypatch, (FOREIGN_PID, GATEWAY_CMD), WRAPPER_PID,
                   ppid=lambda pid: 1)
        assert health._proxy_gateway_identity()[0] == "foreign"

    def test_a_hollow_record_is_trusted_only_through_the_config_it_reads(
            self, monkeypatch):
        """sparkrun's PID is gone; the listener is a litellm reading sparkrun's
        OWN generated config, which is the strongest proof left."""
        self._wire(monkeypatch, (CHILD_PID, GATEWAY_CMD), WRAPPER_PID,
                   ppid=lambda pid: 1, alive=False)
        assert health._proxy_gateway_identity()[0] == "stale"
        self._wire(monkeypatch, (CHILD_PID, GATEWAY_CMD), WRAPPER_PID,
                   ppid=lambda pid: 1, alive=False, config_matches=False)
        assert health._proxy_gateway_identity()[0] == "foreign"

    def test_nothing_listening_is_absent(self, monkeypatch):
        self._wire(monkeypatch, (None, None), WRAPPER_PID)
        assert health._proxy_gateway_identity()[0] == "absent"

    def test_the_child_verdict_never_consults_a_children_table(
            self, monkeypatch):
        """THE PIN t_0df7fc35 ASKS FOR: the uvx wrapper/child pair is classified
        ``child`` through the SHARED predicate, i.e. from the parent chain and
        nothing else. Two halves. Behavioural: only ``_proc_ppid`` was consulted,
        once, so no downward walk over the machine's process table happened.
        Structural: there is no children-table seam left to consult, so a future
        edit cannot quietly grow a second answer to "is this PID ours"."""
        consulted = []

        def ppid(pid):
            consulted.append(pid)
            return WRAPPER_PID if pid == CHILD_PID else 1

        self._wire(monkeypatch, (CHILD_PID, GATEWAY_CMD), WRAPPER_PID,
                   ppid=ppid)
        assert health._proxy_gateway_identity()[0] == "child"
        assert consulted == [CHILD_PID], \
            f"identity was not decided by the parent walk alone: {consulted}"
        assert not hasattr(health, "_proc_children"), \
            "a children-table seam is back: two predicates, one question"
        assert not hasattr(health, "_proxy_owner_is_tracked_proxy"), \
            "the second 'is this PID ours' predicate is back"

    def test_the_walk_is_bounded(self, monkeypatch):
        """A process table that never reaches the tracked PID must terminate,
        on the ONE budget. The lookup count is pinned because the patch below
        only bites at all thanks to t_0df7fc35: this function used to own a
        private PROXY_TRACKED_TREE_DEPTH, and now passes the shared
        ``max_hops`` explicitly, which is what makes this a real bound."""
        seen = []

        def ppid(pid):
            seen.append(pid)
            return pid + 1

        self._wire(monkeypatch, (CHILD_PID, GATEWAY_CMD), WRAPPER_PID,
                   ppid=ppid, alive=True)
        monkeypatch.setattr(health, "PROXY_ANCESTRY_MAX_HOPS", 4)
        assert health._proxy_gateway_identity()[0] == "foreign"
        assert len(seen) == 4, f"the walk outlived its budget: {seen}"

    def test_the_adopt_accepts_the_child_shape_as_verified(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, logs):
        """End to end: the re-attach's own verification runs over the
        wrapper/child pair, so the tick reports a repair, not a FAILED."""
        port = _Port(monkeypatch, fake_sparkrun)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        assert health.check_proxy() is True
        assert any("proxy adopted on" in m for _lvl, m in logs), \
            "the adopt never reported its verdict"
        assert not any("UNVERIFIED" in m for _lvl, m in logs), \
            f"the uvx wrapper/child shape was called a failure: {[m for _l, m in logs]}"


# ── 4 — the controls: everything else still takes the orphan repair ────────

class TestOrphanRepairStillFires:
    def test_a_foreign_listener_still_gets_stop_reap_and_start(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        port = _Port(monkeypatch, fake_sparkrun, owner_pid=FOREIGN_PID,
                     ppid_of_child=1)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        health.check_proxy()
        assert "proxy stop" in commands(fake_sparkrun), \
            "an untracked listener on :4000 was not reaped and restarted"
        assert rig.reaps, "the cmdline-vetted reap was skipped"

    def test_a_dead_backend_in_the_router_still_gets_the_repair(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        """NOT the re-attach shape: the router points at a unit that is down."""
        port = _Port(monkeypatch, fake_sparkrun)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)}, port=port)
        health.check_proxy()
        assert "proxy stop" in commands(fake_sparkrun), \
            "a genuinely dead backend in the router was adopted instead of repaired"

    def test_a_hollow_record_still_gets_the_repair(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        """The incident shape: sparkrun's tracked PID is alive but is NOT the
        listener and not its parent — the F4 orphan. Adopting cannot fix it."""
        port = _Port(monkeypatch, fake_sparkrun, owner_pid=FOREIGN_PID,
                     tracked=WRAPPER_PID, ppid_of_child=1, tracked_alive=True)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        health.check_proxy()
        assert "proxy stop" in commands(fake_sparkrun), \
            "the hollow-PID shape was adopted; only stop + reap can fix it"

    def test_an_adopt_that_cannot_prove_the_gateway_came_back_falls_through(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, logs):
        """Adoptable at decision time; the replacement that comes back is NOT
        the gateway sparkrun tracks. A failed adopt must never leave the fleet
        worse than the old code: the orphan repair still runs, in the same tick,
        and only after the adopt was attempted."""
        port = _Port(monkeypatch, fake_sparkrun, owner_after_start=FOREIGN_PID)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, RESUMED],
                   live={hp(UP)}, healthy_units={hp(UP), hp(RESUMED)}, port=port)
        health.check_proxy()
        assert any("adopt" in m and "orphan repair" in m for _lvl, m in logs), \
            "the failed adopt never fell through to the repair"
        cmds = [c for c in commands(fake_sparkrun)
                if c != "proxy status"]  # the read, not an action
        assert "proxy stop" in cmds, \
            f"no fallback repair ran: {sparkrun_log(fake_sparkrun)}"
        assert cmds[0] == "proxy start", \
            f"the adopt was not attempted first: {cmds}"
