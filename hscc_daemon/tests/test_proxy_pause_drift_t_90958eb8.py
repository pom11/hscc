"""A paused node must never take the fleet's only entrypoint down (t_90958eb8).

WHY THIS EXISTS
---------------
``hscc node pause`` stops one worker node's units on purpose and edits nothing
the load-balancer is built from. The running worker proxy on ``:4000`` therefore
keeps routing to a backend that is now deliberately gone, and ``check_proxy``
read that absence as *unhealthy*: "repairing proxy backend drift" -> ``sparkrun
proxy stop`` -> ``sparkrun proxy start`` -> sparkrun's stale-PID refusal
``Proxy is already running (PID ...) ... Use --restart`` -> unverified failure ->
a **300 s cooldown with :4000 refusing every connection**. Measured live on
2026-11-22: one ``hscc node pause`` cost the fleet ~5 minutes of dead entrypoint
(13:09:29-13:14:31Z), and the later relaunch of the same node blipped it again.

The pre-existing node-pause seam (t_8e5ab6cb, ``test_health_repair_node_pause_
t_8e5ab6cb.py``) only covered the repair *target* — which host a tick is keyed
on. It never covered what *triggers* drift, so a pause still marched the tick
into the repair path and only then noticed the node was parked, by which time
the proxy had already been stopped.

THE THREE FIXES THIS FILE PINS
------------------------------
1. EXPECTED-ABSENT — ``_proxy_backend_drift`` is told which nodes the overlay
   parks and drops their backends out of BOTH comparisons before any repair is
   decided. A pause must never cause a proxy stop. The same overlay also keeps
   parked hosts out of the ``--hosts`` value handed to ``sparkrun proxy start``.
2. REPAIR NEVER LEAVES :4000 DOWN — a plain start refused by a stale PID is
   retried once with ``--restart`` (the one case where --restart is the right
   tool: the tracked PID is what is pinning sparkrun's state), then verified.
   A repair that still ends with nothing listening on the port alerts on its
   FIRST tick and asks for a short retry window instead of sleeping 300 s on a
   dead entrypoint.
3. BOTH ORDERS — pause-then-drift and relaunch-then-already-running are driven
   end to end against a planted fake ``sparkrun`` that answers a plain start
   with the exact refusal and succeeds on ``--restart``.

THE CONTROL HALF OF EVERY PIN
-----------------------------
An expected-absent rule that swallows everything is as broken as the false page
it removes, so every suppression here is pinned against the opposite posture: a
genuinely unhealthy backend on a node nobody parked must still report drift and
still take the repair (acceptance: "that must still repair — pin it"), and a
failed repair that left the port LISTENING must not jump the alert queue or the
cooldown.

SAFETY (the no-live rule)
-------------------------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24, documentation space, never
this fleet). ``sparkrun`` is a planted script pinned through ``HSCC_SPARKRUN_BIN``
and deliberately kept OFF PATH (the sparkrun fence identifies the operator's
live CLI with ``shutil.which``), so the real CLI, the real venv python and the
fleet are never reached; ``ssh``/``docker`` are never invoked. ``_proxy_listener``
(lsof) and ``_proxy_tracked_pid`` are stubbed or answered by the fake, so no test
sends a request to a real ``:4000`` or reads the real ``~/.hscc``: the overlay is
always a per-test temp dir passed through ``node_pauses``' own ``directory=``
seam, and the state dir through ``state.STATE_DIR``.
"""

import os
import sys

import pytest

from hscc_daemon import health, node_pauses

# TEST-NET-1 (RFC 5737). The numbers encode which host LEADS the relaunch list:
# `_proxy_relaunch_hosts` sorts the set and the pause-skip / heal strike are
# keyed on the leading host (t_8e5ab6cb). Sort order here is
#   UP(.30) < DEAD(.40) < PARKED(.90) < SIBLING-parked(.11 is only parked via
#   span_nodes, so it never leads a list on its own).
# so a test that wants the parked node NOT to be the key simply leaves it out
# of the leading position, and a test that wants the drift to be the parked
# node's alone never needs a repair at all.
UP = "192.0.2.30"        # parked by nobody, unit healthy
DEAD = "192.0.2.40"      # parked by nobody, unit genuinely down
PARKED = "192.0.2.90"    # the node `hscc node pause` parked
SIBLING = "192.0.2.11"   # parked only through the pause's span_nodes
PORT = 8001

# sparkrun's own stale-PID refusal, verbatim from the 2026-11-22 daemon.log
# (engine.py raises ProxyAlreadyRunning; the CLI writes it to stderr, exits 1).
ALREADY_RUNNING = ("Error: Proxy is already running (PID {pid}). Use --restart.")
TRACKED_PID = 4242


def hp(host, port=PORT):
    return f"{host}:{port}"


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root for this test's pauses (node_pauses' only seam)."""
    d = tmp_path / "overlay"
    d.mkdir()
    return str(d)


@pytest.fixture
def logs(monkeypatch):
    """Capture (level, message) from both log bindings a proxy tick can reach."""
    from hscc_daemon import daemon_ops
    seen = []

    def _capture(msg, level="INFO"):
        seen.append((level, msg))

    monkeypatch.setattr(health, "log", _capture)
    monkeypatch.setattr(daemon_ops, "log", _capture)
    return seen


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    """Record what the stream would tell the operator — never a real notify.

    AUTUSE, not opt-in: the down-repair path alerts on its first failed tick,
    so a case that exercises a failed repair WITHOUT asking for this fixture
    would otherwise run the real ``send_macos_notification`` (osascript on the
    operator's own desktop, for a fake outage) and append to the real
    ``events.jsonl``. A test that asserts on the alert asks for it by name and
    gets the same recorded instance.
    """
    seen = {"notify": [], "events": []}
    monkeypatch.setattr(
        health, "send_macos_notification",
        lambda title, msg, priority="normal":
        seen["notify"].append((title, msg, priority)))
    monkeypatch.setattr(
        health, "emit_event",
        lambda name, data, severity="info":
        seen["events"].append((name, data, severity)))
    return seen


@pytest.fixture
def fake_sparkrun(tmp_path, monkeypatch):
    """Planted ``sparkrun`` that answers exactly like the live CLI in the bug.

    Plain ``proxy start`` -> exit 1 + ``Proxy is already running (PID ...) ...
    Use --restart.``; ``proxy start --restart`` -> exit 0; ``proxy status
    --json`` -> the tracked PID. Every argv is appended to a call log so a test
    can assert what was and was not issued (``proxy stop`` appearing there is
    the forbidden shape). Pinned through ``HSCC_SPARKRUN_BIN`` and kept OFF
    PATH, so the operator's real CLI can never be exec'd — see the conftest
    sparkrun fence.
    """
    import shutil

    bin_dir = tmp_path / "planted-sparkrun"
    bin_dir.mkdir()
    cli = bin_dir / "sparkrun"
    cli.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "argv = sys.argv[1:]\n"
        "with open(os.environ['FAKE_CALL_LOG'], 'a') as fh:\n"
        "    fh.write(' '.join(argv) + '\\n')\n"
        "pid = int(os.environ.get('FAKE_TRACKED_PID', '%d'))\n"
        "if argv[:2] == ['proxy', 'status']:\n"
        "    print(json.dumps({'pid': pid}))\n"
        "    sys.exit(0)\n"
        "if argv[:2] == ['proxy', 'start']:\n"
        "    if os.environ.get('FAKE_REFUSE_PLAIN_START') == '1' \\\n"
        "            and '--restart' not in argv:\n"
        "        sys.stderr.write('Error: Proxy is already running (PID %%d). "
        "Use --restart.\\n' %% pid)\n"
        "        sys.exit(1)\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n" % TRACKED_PID)
    cli.chmod(0o755)
    assert os.path.realpath(shutil.which("sparkrun") or "") != \
        os.path.realpath(str(cli)), \
        "the planted CLI must stay off PATH — the fence identifies the live " \
        "CLI with shutil.which and would mis-classify the fake"
    call_log = tmp_path / "sparkrun-calls.log"
    call_log.write_text("")
    monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
    monkeypatch.setenv("FAKE_CALL_LOG", str(call_log))
    health.sparkrun_bin.reset_cache()
    yield call_log
    health.sparkrun_bin.reset_cache()


class _FakePort:
    """What owns :4000, as a function of the last sparkrun command issued.

    ``stop`` and a refused plain ``start`` leave the port with NO listener — the
    shape the operator hit; ``--restart`` puts the tracked PID back on it. The
    real probe is ``lsof``; stubbing it keeps every test off the real port.
    """

    def __init__(self, monkeypatch, refuse_plain_start=True,
                 restart_works=True, foreign_owner_after_start=False):
        self.owner = (TRACKED_PID, "litellm --config /tmp/x.yaml")
        self.refuse_plain_start = refuse_plain_start
        self.restart_works = restart_works
        self.foreign_owner_after_start = foreign_owner_after_start
        monkeypatch.setattr(health, "_proxy_listener", lambda: self.owner)
        # No verification wait: these tests are about the VERDICT, and the
        # production 20 s bounded wait is covered by test_proxy_stall.py.
        monkeypatch.setattr(health, "PROXY_REPAIR_VERIFY_WAIT_S", 0.0)

    def after(self, argv):
        """Update the port owner from a sparkrun argv the fake just ran."""
        if argv[:2] == ["proxy", "stop"]:
            self.owner = (None, None)
        elif argv[:2] == ["proxy", "start"]:
            if "--restart" in argv:
                self.owner = ((TRACKED_PID, "litellm --config /tmp/x.yaml")
                              if self.restart_works else (None, None))
            elif self.refuse_plain_start:
                # The refusal means nothing was started — and the proxy was
                # already stopped, which is the outage this card is about.
                self.owner = (None, None)
            elif self.foreign_owner_after_start:
                self.owner = (31337, "litellm --config /tmp/x.yaml")
            else:
                self.owner = (TRACKED_PID, "litellm --config /tmp/x.yaml")


class _Rig:
    """``check_proxy()`` over a proxy that ANSWERS and serves every expected
    alias, with a live router pointing at whatever backends a test names.

    The catalogue is deliberately healthy in every case here: the bug was the
    LIVE-ROUTER drift check (F4) — the one that compares ``/v1/model/info``'s
    api_base set against the units that are up — taking the fleet's LB down
    over a unit the operator stopped on purpose.
    """

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
        monkeypatch.setattr(serving, "load_serving",
                            lambda *a, **k: serving_data)
        monkeypatch.setattr(
            health, "_router_drift_probe",
            lambda: {"status": "skip", "warn": False, "detail": "stubbed"})
        monkeypatch.setattr(
            health, "_unit_alias_check_fn",
            lambda serving_data=None: {"ok": True, "findings": [],
                                       "unverified": [], "message": "stubbed"})
        # Catalogue healthy: the proxy serves the alias every worker unit wants.
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": True,
                                                    "models": ["worker-model"],
                                                    "error": ""})
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5: {
                                "ok": True, "backends": set(live), "error": ""})
        scope = set(fleet) if fleet is not None else (set(live)
                                                      | set(healthy_units))
        monkeypatch.setattr(health, "_fleet_unit_backends",
                            lambda serving_data=None, probe=None:
                            (set(healthy_units), scope))
        self.port = port or _FakePort(monkeypatch, refuse_plain_start=False)
        # The reap (lsof + SIGTERM of a listener) has its own suite in
        # test_health.py; here it is a step in the order, never the subject, and
        # it must never reach for a real signal.
        self.reaps = []
        monkeypatch.setattr(health, "_reap_orphan_proxy_listener",
                            lambda tracked: self.reaps.append(tracked) or True)
        # The fake port is a FUNCTION of what sparkrun was told to do, so the
        # two must move together: wrap run_cmd, let the planted CLI run for real
        # (that is what the call log records), and update the port owner from
        # the same argv.
        _real_run_cmd = health.run_cmd

        def _run_cmd(args, **kw):
            argv = list(args or [])
            if argv and str(argv[0]).endswith("sparkrun") and len(argv) > 2:
                self.port.after(argv[1:])
            return _real_run_cmd(args, **kw)

        monkeypatch.setattr(health, "run_cmd", _run_cmd)
        self.leases = []
        import hscc_daemon.heal_lease as heal_lease
        monkeypatch.setattr(
            heal_lease, "acquire",
            lambda actor, detail="": (self.leases.append((actor, detail))
                                      or (True, None)))
        monkeypatch.setattr(heal_lease, "release", lambda actor: None)
        monkeypatch.setattr(health, "heal_lease", heal_lease)
        # `state_mod` is only needed for the seed helpers below.
        self._state_dir = state_dir

    def age_last_relaunch(self, seconds):
        """Move ONLY the attempt stamp back in time, leaving the rest of the
        state — above all the ``cooldown_s`` the code itself wrote — untouched.

        Seeding a window would test the reader, not the writer: the value under
        test must be the one ``check_proxy`` produced.
        """
        import json
        from datetime import datetime, timedelta, timezone
        st = json.loads((self._state_dir / "proxy.json").read_text())
        st["last_relaunch"] = (
            datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        (self._state_dir / "proxy.json").write_text(json.dumps(st))
        return st

    def state(self):
        import json
        return json.loads((self._state_dir / "proxy.json").read_text())


def park(host, directory, **kw):
    """Record a pause in this test's overlay."""
    return node_pauses.pause(host, directory=directory, **kw)


def sparkrun_log(call_log):
    return [line for line in call_log.read_text().splitlines() if line.strip()]


def commands(call_log):
    """The sparkrun SUBCOMMANDS issued, in order, e.g. ['proxy stop', ...]."""
    out = []
    for line in sparkrun_log(call_log):
        parts = line.split()
        out.append(" ".join(parts[:2]) if len(parts) > 1 else parts[0])
    return out


def start_argvs(call_log):
    return [line.split() for line in sparkrun_log(call_log)
            if line.startswith("proxy start")]


# ── fix 1 — a parked node's backends are EXPECTED-ABSENT ───────────────────

class TestPausedBackendsAreExpectedAbsent:
    """The drift DECISION must consult the overlay, not just the repair target.

    ``_proxy_backend_drift`` is where "the router points at a backend that is
    not healthy" becomes a proxy stop. A paused unit is not healthy — that is
    what ``hscc node pause`` was asked to do — so it must be filed as
    expected-absent here, before anything mutates.
    """

    def test_a_paused_nodes_backend_is_expected_absent_not_drift(self):
        """THE INCIDENT DECISION: the router still carries the endpoint the
        parked node's unit used to answer. Not drift, and no repair."""
        from hscc_daemon import health
        ok, reason = health._proxy_backend_drift(
            {hp(PARKED), hp(UP)},                 # live router
            {hp(UP)},                             # units up right now
            {hp(PARKED), hp(UP)},                 # hscc-owned backends
            paused_nodes={PARKED})
        assert ok is True, f"a pause read as drift: {reason}"
        assert "expected-absent" in reason, (
            f"the exclusion must be visible in the verdict, got {reason!r}")

    def test_the_relaunch_command_excludes_a_parked_host(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun, logs):
        """``--hosts`` is what the LB is pointed at: a parked node's endpoint
        cannot serve, so it must not be carried into the relaunch. The TICK's
        key stays the unfiltered leading host (t_8e5ab6cb pins the skip and the
        strike to it), so the filter belongs at the command, not the list."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, PARKED],
                   live={hp(PARKED)}, healthy_units={hp(UP)})
        park(PARKED, overlay, reason="training run")
        # Drift is real here and has NOTHING to do with the parked node: UP is
        # healthy and the router stopped routing to it.
        assert health.check_proxy(pause_directory=overlay) is True
        starts = start_argvs(fake_sparkrun)
        assert starts, f"the drift was never repaired: {sparkrun_log(fake_sparkrun)}"
        hosts_arg = starts[-1][starts[-1].index("--hosts") + 1]
        assert hosts_arg == UP, f"--hosts carried a parked node: {hosts_arg}"
        assert any("excluding paused host" in m for _lvl, m in logs), \
            "the exclusion was silent"

    def test_a_pause_never_stops_the_proxy(self, tmp_hfcc_dir, monkeypatch,
                                           overlay, fake_sparkrun, logs):
        """THE OPERATOR'S BUG, end to end: pause a node, tick the proxy check,
        and the fleet's only entrypoint must still be up afterwards.

        Before the fix this tick logged 'repairing proxy backend drift', ran
        ``sparkrun proxy stop`` and then could not start the proxy again."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, PARKED],
                   live={hp(PARKED), hp(UP)}, healthy_units={hp(UP)})
        park(PARKED, overlay, reason="training run")
        assert health.check_proxy(pause_directory=overlay) is True
        assert "proxy stop" not in commands(fake_sparkrun), (
            "pausing a node stopped the fleet proxy — the incident: "
            f"{sparkrun_log(fake_sparkrun)}")
        assert rig.leases == [], "a parked node's absence took a heal lease"
        st = rig.state()
        assert st["ok"] is True and st.get("backend_drift") is not True, \
            f"the stream still filed the pause as drift: {st}"

    def test_a_dead_backend_on_an_unpaused_node_still_repairs(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, overlay):
        """THE CONTROL the acceptance names: with nothing parked, a router
        pointing at a genuinely dead backend must STILL be drift and STILL take
        the repair. Without this every suppression above is satisfied by a
        drift check that never fires."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)})
        health.check_proxy()
        assert "proxy stop" in commands(fake_sparkrun), \
            "a genuinely unhealthy backend was not repaired"
        assert rig.state()["backend_drift"] is True

    def test_a_pause_on_another_node_does_not_disarm_the_drift_repair(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun):
        """Over-suppression is the same class of bug as the false page: a real
        fault next to a parked node is still a fault."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)})
        park(PARKED, overlay, reason="someone else's training run")
        health.check_proxy(pause_directory=overlay)
        assert "proxy stop" in commands(fake_sparkrun), \
            "a pause elsewhere disarmed the fleet's drift repair"


# ── fix 2 — the repair must never leave :4000 down ─────────────────────────

class TestRepairNeverLeavesTheProxyDown:
    """Stop-then-start is only safe if the start actually starts.

    sparkrun's ``is_running()`` reads its own state-file PID, so the stop+start
    pair can end with the state still pinning a hollow PID: the plain start
    refuses with ``Proxy is already running (PID ...) ... Use --restart`` and —
    this is the part that cost five minutes — the tick returns UNVERIFIED and
    buys itself a silent 300 s cooldown with the port refusing everything.
    """

    def test_a_stale_pid_refusal_is_retried_with_restart_and_verified(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, logs):
        """Fake sparkrun: plain start -> the exact refusal, --restart -> ok.
        The repair must take the --restart branch and come back VERIFIED, not
        return the refusal as its verdict."""
        port = _FakePort(monkeypatch, refuse_plain_start=True,
                         restart_works=True)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)}, port=port)
        monkeypatch.setenv("FAKE_REFUSE_PLAIN_START", "1")
        assert health.check_proxy() is True, (
            "a refused plain start was left as the final verdict")
        starts = start_argvs(fake_sparkrun)
        plain = [s for s in starts if "--restart" not in s]
        restarted = [s for s in starts if "--restart" in s]
        assert plain and restarted, \
            f"expected a plain start then --restart, got {starts}"
        assert plain[0] == restarted[0][:-1], \
            "--restart must be the SAME command retried, not a different one"
        assert port.owner[0] == TRACKED_PID, "the port is not owned after the retry"
        assert any("refused by a stale tracked PID" in m for _lvl, m in logs), \
            "the refusal and the retry were not logged"

    def test_a_repair_that_leaves_the_port_down_alerts_at_once(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, alerts):
        """Verification still failed and NOTHING listens on :4000. The stream
        must say so (proxy_down), alert on the FIRST failed tick rather than
        waiting for the streak, and ask for the short retry window."""
        port = _FakePort(monkeypatch, refuse_plain_start=True,
                         restart_works=False)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)}, port=port)
        monkeypatch.setenv("FAKE_REFUSE_PLAIN_START", "1")
        assert health.check_proxy() is False
        st = rig.state()
        assert st["proxy_down"] is True, f"the port was not proved down: {st}"
        assert st["cooldown_s"] == health.PROXY_RELAUNCH_DOWN_COOLDOWN_S, (
            "a confirmed-down proxy got the normal cooldown back: "
            f"{st['cooldown_s']}s")
        assert st["cooldown_s"] < health.PROXY_RELAUNCH_COOLDOWN_S
        stalled = [e for e in alerts["events"] if e[0] == "proxy.relaunch.stalled"]
        assert stalled, (
            "the proxy was left down in silence — the 5-minute window: "
            f"{alerts}")
        assert stalled[0][1]["proxy_down"] is True
        assert alerts["notify"], "no operator notification for a down entrypoint"

    def test_a_down_repair_retries_inside_the_old_300s_window(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun):
        """THE 5-MINUTES-BECOMES-90-SECONDS PIN. A repair that leaves :4000 down
        writes its own short window; 90 s later — inside the old 300 s cooldown
        — the next tick must try again instead of sleeping on a dead entrypoint.

        The window is what the FIRST tick wrote, not what this test seeded: a
        seeded value would pin the reader and leave the writer free to keep
        stamping 300 s."""
        port = _FakePort(monkeypatch, refuse_plain_start=True,
                         restart_works=False)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)}, port=port)
        monkeypatch.setenv("FAKE_REFUSE_PLAIN_START", "1")
        assert health.check_proxy() is False          # tick 1: left the port down
        assert rig.state()["cooldown_s"] == health.PROXY_RELAUNCH_DOWN_COOLDOWN_S
        rig.age_last_relaunch(90)                     # 90 s later, still < 300 s
        before = len(start_argvs(fake_sparkrun))
        health.check_proxy()                          # tick 2
        assert len(start_argvs(fake_sparkrun)) > before, (
            "a down proxy was left suppressed by a 300 s cooldown 90 s in "
            f"(cmds: {sparkrun_log(fake_sparkrun)})")

    def test_a_failed_repair_with_the_port_listening_keeps_the_normal_wait(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, alerts):
        """THE CONTROL for fix 2: a failed repair where SOMETHING still owns
        the port is not an outage — it keeps the 300 s window and waits for the
        streak, so a wedged-but-listening proxy cannot become a notify storm."""
        port = _FakePort(monkeypatch, refuse_plain_start=False,
                         restart_works=False, foreign_owner_after_start=True)
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)}, port=port)
        assert health.check_proxy() is False
        st = rig.state()
        assert st["proxy_down"] is False
        assert st["cooldown_s"] == health.PROXY_RELAUNCH_COOLDOWN_S
        assert [e for e in alerts["events"]
                if e[0] == "proxy.relaunch.stalled"] == [], \
            "a first failed tick alerted — the streak gate is gone"


# ── fix 3 — both orders, driven end to end against fake sparkrun ───────────

class TestBothOrders:
    """The operator hit the same entrypoint twice: once by pausing a node, once
    by relaunching it. Each order is walked through the real check with the
    real argv shape."""

    def test_pause_then_drift_order_leaves_the_proxy_serving(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun, logs):
        """Order 1: pause recorded, then the tick that used to repair drift.
        Two sweeps, because the second is the one that used to sit in cooldown
        with the port dead."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, PARKED],
                   live={hp(PARKED), hp(UP)}, healthy_units={hp(UP)})
        park(PARKED, overlay, reason="training run")
        for _ in range(2):
            assert health.check_proxy(pause_directory=overlay) is True
        assert "proxy stop" not in commands(fake_sparkrun), \
            f"the pause stopped the proxy: {sparkrun_log(fake_sparkrun)}"
        assert start_argvs(fake_sparkrun) == [], (
            "a proxy that serves every healthy unit was restarted for a pause")

    def test_relaunch_then_already_running_order_ends_with_an_owned_port(
            self, tmp_hfcc_dir, monkeypatch, fake_sparkrun, logs):
        """Order 2: the repair relaunches the LB, sparkrun's stale PID refuses
        the plain start, and the tick must end with the port owned by the
        tracked PID — not UNVERIFIED, not cooling down."""
        port = _FakePort(monkeypatch, refuse_plain_start=True,
                         restart_works=True)
        _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
             live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)}, port=port)
        monkeypatch.setenv("FAKE_REFUSE_PLAIN_START", "1")
        assert health.check_proxy() is True
        seq = commands(fake_sparkrun)
        assert seq.index("proxy stop") < max(
            i for i, c in enumerate(seq) if c == "proxy start"), \
            f"stop must precede the start that fixed it: {seq}"
        assert start_argvs(fake_sparkrun)[-1][-1] == "--restart" or \
            "--restart" in start_argvs(fake_sparkrun)[-1]
        assert port.owner[0] == TRACKED_PID
        assert any("repaired on" in m for _lvl, m in logs), \
            "the verified repair was not logged"

    def test_the_span_sibling_of_a_parked_span_is_expected_absent(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun):
        """The both-APIs pin for this file: ``paused(SIBLING)`` is None (no
        pause is keyed to it) while the pause's ``span_nodes`` covers it, and
        the drift verdict follows the COVERING answer. Swapping the seam for
        ``paused()`` reddens this test."""
        from hscc_daemon import health
        park(PARKED, overlay, reason="tp2 training run",
             span_nodes=[PARKED, SIBLING])
        assert node_pauses.paused(SIBLING, directory=overlay) is None
        assert node_pauses.covering(SIBLING, directory=overlay) is not None
        parked = health._paused_nodes(overlay)
        assert {PARKED, SIBLING} <= parked, parked
        ok, reason = health._proxy_backend_drift(
            {hp(SIBLING), hp(UP)}, {hp(UP)},
            {hp(SIBLING), hp(UP), hp(PARKED)}, paused_nodes=parked)
        assert ok is True, f"a span sibling read as drift: {reason}"

    def test_a_malformed_overlay_never_freezes_the_drift_repair(
            self, tmp_hfcc_dir, monkeypatch, overlay, fake_sparkrun):
        """Fail-open direction, same rule as every other pause seam: a junk
        overlay reads as "nothing parked" LOUDLY, and a real fault behind it is
        still repaired."""
        with open(os.path.join(overlay, node_pauses.OVERLAY_NAME), "w") as f:
            f.write('{"pauses": [this is not an object')
        rig = _Rig(tmp_hfcc_dir, monkeypatch, hosts=[UP, DEAD],
                   live={hp(UP), hp(DEAD)}, healthy_units={hp(UP)})
        health.check_proxy(pause_directory=overlay)
        assert "proxy stop" in commands(fake_sparkrun), \
            "a corrupt overlay froze the fleet's ability to repair itself"
