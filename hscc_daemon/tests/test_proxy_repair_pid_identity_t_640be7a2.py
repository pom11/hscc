"""A healthy proxy must never be reported as a FAILED repair (t_640be7a2).

WHY THIS EXISTS
---------------
``sparkrun proxy status --json`` reports the PID in its own state file — for
``uvx litellm`` that is the LAUNCHER WRAPPER. The process that actually holds
the socket on ``:4000`` is the wrapper's litellm CHILD (the same hollow-PID
shape ``_proxy_kill_hollow_tracked`` was written for). The repair verifier
compared the two with ``==``, so on 2026-11-22 (a worker node, main 2a60cc67)
a tick that had just fixed real drift wrote:

    proxy repair UNVERIFIED on :4000 — port owner 15706, sparkrun tracks 15701
    [ERROR] proxy repair on :4000 FAILED (repair incomplete (unverified)): …

while ``:4000`` answered every request. Three wrong things followed from one
pid-bookkeeping mismatch: an ERROR-level line about a healthy service, a
spurious repair-failure record, and a streak that can walk the tick into an
escalation and a stall page for a proxy that is up.

THE TWO FIXES THIS FILE PINS
----------------------------
1. IDENTITY IS THE PROCESS TREE. Verification accepts the port owner when it is
   the tracked PID **or a descendant of it**, walked breadth-first with a
   bounded depth and a bounded PID budget (:func:`_proxy_owner_is_tracked_proxy`).
   The reap uses the same rule: a listener that is a child of the tracked
   launcher is our own proxy, and signalling it would take a working ``:4000``
   down.
2. THE END STATE DECIDES THE VERDICT'S SEVERITY. When the tracked proxy tree
   owns the port, the repair worked: ``repaired`` at INFO, never ``FAILED`` at
   ERROR. The narrower residual — proxy verified healthy but the ``sparkrun``
   command itself returned a failure — stays visible as a WARN and keeps
   ``ok=False``, because the streak and escalation machinery must not lose their
   arms.

THE CONTROL HALF OF EVERY PIN
-----------------------------
A descendant rule that matched anything would be worse than the bug it fixes: it
would certify a squatter on ``:4000`` (a dev server, a second hscc-api) as "our
proxy" and hide a real outage. So every acceptance here is pinned against a
foreign owner that stays UNVERIFIED and still logs ERROR, and the walk is pinned
as BOUNDED — a descendant below the depth cap, or beyond the PID budget, is not
credited.

SAFETY (the no-live rule)
-------------------------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24, documentation space, never
this fleet). ``sparkrun`` is a planted script pinned through
``HSCC_SPARKRUN_BIN`` and kept OFF PATH (the conftest sparkrun fence identifies
the operator's live CLI with ``shutil.which``), so the real CLI, its venv python
and the fleet are never reached; ``ssh``/``docker`` are never invoked. The port
owner comes from a fake table, never ``lsof``; the process table comes from a
fake parent→children map, never ``ps`` (conftest defaults it to an empty table
and every test here installs its own). ``os.kill`` is replaced by a recorder, so
no signal leaves the process. No test touches a real ``:4000``, the real
``~/.hscc`` or the operator's process list.
"""

import os
import shutil
import sys

import pytest

from hscc_daemon import health

UP = "192.0.2.30"          # healthy unit the live router still serves
GONE = "192.0.2.40"        # healthy unit the live router DROPPED -> drift
PORT = 8001

# The PIDs from the 2026-11-22 log (real numbers, and meaningless ones: no pid
# in this file is ever signalled — `os.kill` is a recorder).
WRAPPER = 15701            # what sparkrun tracks: the `uvx litellm` launcher
LITELLM_CHILD = 15706      # what actually binds :4000
GRANDCHILD = 15707
FOREIGN = 31337            # a squatter on :4000 that is NOT our proxy
LITELLM_CMD = "litellm --config /tmp/proxy.yaml"


def hp(host, port=PORT):
    return f"{host}:{port}"


def errors(seen):
    return [m for lvl, m in seen if lvl == "ERROR"]


def cmds(call_log):
    """The sparkrun SUBCOMMANDS the planted CLI was asked to run, in order."""
    return [" ".join(line.split()[:2])
            for line in call_log.read_text().splitlines() if line.strip()]


# ── fakes: a port-owner table, a process table, a planted CLI ───────────────

class FakePortTable:
    """What owns each port — the answer ``lsof`` would give, from a dict.

    Nothing here opens a socket, binds a port or runs ``lsof``.
    """

    def __init__(self, monkeypatch):
        self.owners = {}
        monkeypatch.setattr(health, "_proxy_listener", self._listener)
        # No verification wait: these tests are about the VERDICT, and the
        # production 20 s bounded wait is covered by test_proxy_stall.py.
        monkeypatch.setattr(health, "PROXY_REPAIR_VERIFY_WAIT_S", 0.0)

    def _listener(self):
        return self.owners.get(health.PROXY_PORT, (None, None))

    def set_owner(self, pid, cmdline=LITELLM_CMD):
        self.owners[health.PROXY_PORT] = (pid, cmdline)

    def clear(self):
        self.owners.pop(health.PROXY_PORT, None)


class FakeProcessTable:
    """parent -> [children]: the answer ``ps -o pid= -P`` would give, from a dict.

    ``calls`` records every lookup, so a test can pin that the walk stayed
    BOUNDED rather than scanning the machine's process list.
    """

    def __init__(self, monkeypatch, table=None):
        self.table = dict(table or {})
        self.calls = []
        monkeypatch.setattr(health, "_proc_children", self.children)

    def children(self, pid):
        self.calls.append(int(pid))
        return list(self.table.get(int(pid), []))

    def chain(self, root, length):
        """root -> c1 -> … for ``length`` links; returns the deepest PID."""
        pid = int(root)
        for _ in range(length):
            nxt = pid + 1000
            self.table.setdefault(pid, [])
            if pid not in self.table or nxt not in self.table[pid]:
                self.table[pid] = self.table.get(pid, []) + [nxt]
            pid = nxt
        return pid


@pytest.fixture
def port(monkeypatch):
    return FakePortTable(monkeypatch)


@pytest.fixture
def procs(monkeypatch):
    """The incident's process shape: sparkrun tracks the wrapper, the child binds."""
    return FakeProcessTable(monkeypatch, {WRAPPER: [LITELLM_CHILD],
                                          LITELLM_CHILD: [GRANDCHILD]})


@pytest.fixture
def kill_sink(monkeypatch):
    """``os.kill`` becomes a recorder: no signal may leave this process."""
    killed = []
    monkeypatch.setattr(health.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(health.time, "sleep", lambda s: None)   # no reap ladder
    return killed


@pytest.fixture
def fake_sparkrun(tmp_path, monkeypatch):
    """Planted ``sparkrun`` whose state file tracks a PID a test chooses.

    ``proxy status --json`` -> ``{"pid": FAKE_TRACKED_PID}``, so the tracked PID
    reaches the verifier through the real ``_proxy_tracked_pid`` parser rather
    than a stub of it; every other argv exits 0 and is appended to a call log.
    Pinned via ``HSCC_SPARKRUN_BIN`` and kept off PATH — see the conftest
    sparkrun fence.
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
        "pid = int(os.environ.get('FAKE_TRACKED_PID', '0'))\n"
        "if argv[:2] == ['proxy', 'status']:\n"
        "    print(json.dumps({'pid': pid}))\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n")
    cli.chmod(0o755)
    assert os.path.realpath(shutil.which("sparkrun") or "") != \
        os.path.realpath(str(cli)), \
        "the planted CLI must stay off PATH — the fence identifies the live " \
        "CLI with shutil.which and would mis-classify the fake"
    call_log = tmp_path / "sparkrun-calls.log"
    call_log.write_text("")
    monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(cli))
    monkeypatch.setenv("FAKE_CALL_LOG", str(call_log))
    monkeypatch.setenv("FAKE_TRACKED_PID", str(WRAPPER))
    health.sparkrun_bin.reset_cache()
    yield call_log
    health.sparkrun_bin.reset_cache()


@pytest.fixture
def logs(monkeypatch):
    """(level, message) from both log bindings a proxy tick can reach."""
    from hscc_daemon import daemon_ops
    seen = []

    def _capture(msg, level="INFO"):
        seen.append((level, msg))

    monkeypatch.setattr(health, "log", _capture)
    monkeypatch.setattr(daemon_ops, "log", _capture)
    return seen


# ── fix 1 — the tracked proxy IS its process tree, bounded ─────────────────

class TestTrackedTreeIdentity:
    """``_proxy_owner_is_tracked_proxy`` — the whole fix in one predicate."""

    def test_tracked_pid_itself_matches_at_depth_zero(self, procs):
        assert health._proxy_owner_is_tracked_proxy(WRAPPER, WRAPPER) == \
            (True, 0)

    def test_child_of_tracked_pid_matches_at_depth_one(self, procs):
        """THE INCIDENT SHAPE: port owner 15706, sparkrun tracks 15701 — and
        15706 is 15701's litellm child. That is our proxy, not an orphan."""
        assert health._proxy_owner_is_tracked_proxy(LITELLM_CHILD, WRAPPER) == \
            (True, 1)

    def test_grandchild_matches_at_depth_two(self, procs):
        assert health._proxy_owner_is_tracked_proxy(GRANDCHILD, WRAPPER) == \
            (True, 2)

    def test_foreign_pid_never_matches(self, procs):
        """THE CONTROL: widening identity to the proxy's own tree must not
        become widening it to any pid. A squatter stays unmatched."""
        assert health._proxy_owner_is_tracked_proxy(FOREIGN, WRAPPER) == \
            (False, None)

    def test_child_of_an_unrelated_pid_does_not_match(self, procs):
        """A descendant of SOMEONE ELSE is not a descendant of the proxy: the
        child of a dev server on :4000 is still not our proxy."""
        squatter_child = 41001
        procs.table[FOREIGN] = [squatter_child]
        assert health._proxy_owner_is_tracked_proxy(squatter_child, WRAPPER) == \
            (False, None)

    def test_missing_owner_or_missing_tracked_pid_never_matches(self, procs):
        assert health._proxy_owner_is_tracked_proxy(None, WRAPPER) == \
            (False, None)
        assert health._proxy_owner_is_tracked_proxy(LITELLM_CHILD, None) == \
            (False, None)
        assert health._proxy_owner_is_tracked_proxy(0, 0) == (False, None)

    def test_unreadable_process_table_fails_closed(self, monkeypatch):
        """A `ps` that answers nothing is NOT a licence to credit a foreign pid
        — and it is the conftest default, so a test that forgot to install a
        process table cannot accidentally pass."""
        monkeypatch.setattr(health, "_proc_children", lambda pid: [])
        assert health._proxy_owner_is_tracked_proxy(LITELLM_CHILD, WRAPPER) == \
            (False, None)

    def test_walk_stops_at_the_depth_cap(self, procs):
        """BOUNDED, not open-ended: a descendant below the cap is not credited,
        and the walk costs one lookup per level it visited."""
        root = 20000          # a clean root: one child per level, no branching
        deepest = procs.chain(root, health.PROXY_TRACKED_TREE_MAX_DEPTH + 2)
        matched, _depth = health._proxy_owner_is_tracked_proxy(deepest, root)
        assert matched is False, "the walk is unbounded — any descendant matches"
        assert len(procs.calls) <= health.PROXY_TRACKED_TREE_MAX_DEPTH, \
            f"the walk did not stop at the cap: {len(procs.calls)} lookups"

    def test_walk_stops_at_the_pid_budget(self, procs):
        """A chatty parent (a supervisor with hundreds of children) must not
        turn a bounded DEPTH into an unbounded scan."""
        fan = health.PROXY_TRACKED_TREE_MAX_PIDS + 50
        procs.table[WRAPPER] = [WRAPPER + 100000 + i for i in range(fan)]
        far = WRAPPER + 100000 + fan - 1
        matched, _depth = health._proxy_owner_is_tracked_proxy(far, WRAPPER)
        assert matched is False, "the PID budget is not enforced"
        assert len(procs.calls) <= 2, \
            f"the walk kept scanning past its budget: {len(procs.calls)} lookups"


# ── fix 1 applied — the repair verdict ────────────────────────────────────

class TestRepairVerdictUsesTheTree:
    """``_proxy_repair_orphan`` must not fail a proxy it just brought up."""

    def _repair(self, monkeypatch, start_ok=True):
        """`sparkrun` for the repair to talk to: status reports the WRAPPER as
        tracked (through the real ``_proxy_tracked_pid`` JSON parser), start
        succeeds or reports a failure, and no other argv matters here."""
        import json

        def run_cmd(args, **kw):
            argv = [str(a) for a in (args or [])]
            if "status" in argv:
                return {"ok": True, "output": json.dumps({"pid": WRAPPER})}
            return {"ok": start_ok,
                    "output": "started" if start_ok else "Error: refused"}

        monkeypatch.setattr(health, "run_cmd", run_cmd)
        return health._proxy_repair_orphan([UP])

    def test_child_owning_the_port_verifies_and_reports_ok(
            self, monkeypatch, port, procs):
        """THE OPERATOR'S CASE: sparkrun tracks the wrapper, the child owns the
        port. VERIFIED, and the repair reports success."""
        port.set_owner(LITELLM_CHILD)
        res = self._repair(monkeypatch)
        assert res["verified"] is True and res["ok"] is True
        assert res["healthy"] is True and res["owner_depth"] == 1
        assert res["proxy_down"] is False

    def test_child_owning_the_port_logs_success_not_unverified(
            self, monkeypatch, port, procs, logs):
        port.set_owner(LITELLM_CHILD)
        monkeypatch.setattr(health, "log",
                            lambda m, lvl="INFO": logs.append((lvl, m)))
        self._repair(monkeypatch)
        assert not any("UNVERIFIED" in m for _l, m in logs), \
            f"a healthy proxy was still called unverified: {logs}"
        ok_lines = [m for _l, m in logs if "repaired on" in m]
        assert ok_lines and "15706" in ok_lines[0] and "15701" in ok_lines[0], \
            f"the success line must name BOTH processes: {ok_lines}"

    def test_unrelated_owner_stays_unverified(self, monkeypatch, port, procs,
                                              logs):
        """NOT widened into accepting any pid: a foreign owner is still a
        failure, and the port is not ours to call repaired."""
        port.set_owner(FOREIGN)
        monkeypatch.setattr(health, "log",
                            lambda m, lvl="INFO": logs.append((lvl, m)))
        res = self._repair(monkeypatch)
        assert res["verified"] is False and res["ok"] is False
        assert res["healthy"] is False
        assert any("UNVERIFIED" in m for _l, m in logs)

    def test_tracked_owner_still_verifies(self, monkeypatch, port, procs):
        """The pre-fix equality case keeps working — this widens, it does not
        replace."""
        port.set_owner(WRAPPER)
        res = self._repair(monkeypatch)
        assert res["verified"] is True and res["ok"] is True
        assert res["owner_depth"] == 0

    def test_nothing_on_the_port_is_not_healthy(self, monkeypatch, port, procs):
        port.clear()
        res = self._repair(monkeypatch)
        assert res["verified"] is False and res["healthy"] is False
        assert res["proxy_down"] is True

    def test_healthy_repair_emits_no_error_line(self, monkeypatch, port, procs,
                                                logs):
        """The card's wording: the healthy path must not produce an ERROR."""
        port.set_owner(LITELLM_CHILD)
        monkeypatch.setattr(health, "log",
                            lambda m, lvl="INFO": logs.append((lvl, m)))
        self._repair(monkeypatch)
        assert errors(logs) == [], f"ERROR noise over a healthy proxy: {logs}"

    def test_reap_never_signals_a_child_of_the_tracked_proxy(
            self, port, procs, kill_sink, logs):
        """Same identity rule, sharper edge: the listener IS the proxy. The old
        code saw pid != tracked and SIGTERM'd the process serving :4000."""
        port.set_owner(LITELLM_CHILD)
        assert health._reap_orphan_proxy_listener(WRAPPER) is False
        assert kill_sink == [], f"the repair killed its own proxy: {kill_sink}"
        assert any("not an orphan" in m for _l, m in logs), logs

    def test_reap_still_reaps_a_foreign_litellm_orphan(self, monkeypatch, port,
                                                       procs, logs):
        """CONTROL: the orphan path the reap exists for — untracked, foreign
        tree, litellm cmdline — is still reaped, and the port comes free."""
        port.set_owner(FOREIGN)
        killed = []

        def fake_kill(pid, sig):
            killed.append((pid, sig))
            if sig:
                port.clear()                  # the listener dies on the signal

        monkeypatch.setattr(health.os, "kill", fake_kill)
        monkeypatch.setattr(health.time, "sleep", lambda s: None)
        assert health._reap_orphan_proxy_listener(WRAPPER) is True
        assert killed and killed[0][0] == FOREIGN, \
            f"the foreign orphan was not reaped: {killed}"


# ── fix 2 — the tick never calls a working proxy FAILED ────────────────────

class _Rig:
    """``check_proxy()`` over a proxy that ANSWERS, with a router that dropped
    one healthy backend — the drift that sends a tick into the repair path.

    ``owner_after_start`` is which PID owns :4000 once ``proxy start`` has run:
    the wrapper (the equality case), its litellm child (the incident case), a
    foreign pid (the control), or None (nothing bound — a real outage).
    """

    def __init__(self, tmp_hfcc_dir, monkeypatch, port, start_ok=True,
                 owner_after=LITELLM_CHILD):
        # owner_after: int | None — None means "nothing rebinds :4000".
        from hscc_daemon import serving
        from hscc_daemon import state as state_mod
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        hosts = [UP, GONE]
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
        healthy = {hp(UP), hp(GONE)}
        # Catalogue side healthy: the proxy answers and serves the alias.
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: {"ok": True,
                                                    "models": ["worker-model"],
                                                    "error": ""})
        # THE DRIFT: the router dropped GONE, which is still catalogue-healthy.
        monkeypatch.setattr(health, "_proxy_backend_probe",
                            lambda url=None, timeout=5:
                            {"ok": True, "backends": {hp(UP)}, "error": ""})
        monkeypatch.setattr(health, "_fleet_unit_backends",
                            lambda serving_data=None, probe=None:
                            (healthy, set(healthy)))
        import hscc_daemon.heal_lease as heal_lease
        monkeypatch.setattr(heal_lease, "acquire",
                            lambda actor, detail="": (True, None))
        monkeypatch.setattr(heal_lease, "release", lambda actor: None)
        monkeypatch.setattr(health, "heal_lease", heal_lease)
        # Alert sinks: RECORD, never notify the operator's desktop or append to
        # the real events.jsonl. Recorded on the rig (not patched away) so the
        # "a dead :4000 still pages" control can assert on it.
        self.notified = []
        monkeypatch.setattr(
            health, "send_macos_notification",
            lambda title, body, priority="normal", **kw:
            self.notified.append((title, body, priority)) or True)
        monkeypatch.setattr(health, "emit_event",
                            lambda name, data, severity="info", **kw: True)
        self.owner_after_start = owner_after
        self.start_ok = start_ok
        # The planted CLI runs for real (that is what the call log records) and
        # the port-owner table follows the same argv, so the verdict a test gets
        # is the verdict the commands produced.
        real_run_cmd = health.run_cmd

        def run_cmd(args, **kw):
            argv = [str(a) for a in (args or [])]
            res = real_run_cmd(args, **kw)
            if len(argv) > 2 and argv[0].endswith("sparkrun"):
                if "stop" in argv:
                    port.clear()
                elif "start" in argv:
                    # `proxy start` is what puts the proxy back; `owner_after`
                    # is who ends up serving :4000. A FAILED start still leaves
                    # them there — that is the residual shape fix 2 is about:
                    # sparkrun reported a failure over a port that is served.
                    if self.owner_after_start is None:
                        port.clear()
                    else:
                        port.set_owner(self.owner_after_start)
                    if not self.start_ok:
                        res = dict(res, ok=False,
                                   output="Error: sparkrun start failed")
            return res

        monkeypatch.setattr(health, "run_cmd", run_cmd)
        self._state_dir = state_dir

    def state(self):
        import json
        return json.loads((self._state_dir / "proxy.json").read_text())


class TestTickNeverCallsAHealthyProxyFailed:
    """End to end: the drift tick over a wrapper-tracked, child-served proxy."""

    def test_drift_repair_over_child_owned_port_emits_no_error(
            self, tmp_hfcc_dir, monkeypatch, port, procs, logs, fake_sparkrun):
        """RED ON OLD with the operator's exact log: UNVERIFIED + ERROR FAILED,
        over a proxy that answered every request."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, port)
        assert health.check_proxy() is True
        assert "proxy stop" in cmds(fake_sparkrun), \
            "the drift never took the repair path — this pin would be vacuous"
        assert errors(logs) == [], f"ERROR over a healthy proxy: {errors(logs)}"
        assert not any("FAILED" in m for _l, m in logs), \
            f"a working proxy was called FAILED: {logs}"
        assert any("repaired on" in m for _l, m in logs), \
            f"the repair never said it succeeded: {logs}"
        assert rig.state()["ok"] is True

    def test_state_records_a_repair_not_a_failure(
            self, tmp_hfcc_dir, monkeypatch, port, procs, logs, fake_sparkrun):
        """The other half of the false alarm: no spurious repair-failure record
        and no page over a proxy that is serving."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, port)
        health.check_proxy()
        st = rig.state()
        assert st["ok"] is True, f"the stream filed a healthy proxy as down: {st}"
        assert st["relaunch_failed"] is False and not st.get("relaunch_error"), \
            f"spurious failure record: {st}"
        assert st["message"] == "repaired", st["message"]
        assert rig.notified == [], f"a healthy proxy paged the operator: {rig.notified}"

    def test_healthy_proxy_but_command_failed_logs_warn_not_error(
            self, tmp_hfcc_dir, monkeypatch, port, procs, logs, fake_sparkrun):
        """The narrow residual: :4000 IS served by our proxy tree but the
        ``sparkrun`` command said no. Not an outage -> no ERROR; not a silent
        pass -> the streak stays armed."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, port, start_ok=False)
        health.check_proxy()
        assert errors(logs) == [], f"ERROR over a served port: {errors(logs)}"
        assert any("proxy repair on" in m for lvl, m in logs if lvl == "WARN"), \
            f"the mismatch went unreported: {logs}"
        assert not any("FAILED" in m for _l, m in logs), logs
        assert rig.state()["relaunch_streak"] >= 1, \
            "a genuine command failure lost its escalation armament"

    def test_foreign_owner_still_logs_error(
            self, tmp_hfcc_dir, monkeypatch, port, procs, logs, fake_sparkrun):
        """CONTROL for fix 2: ERROR is still correct when something that is NOT
        our proxy owns :4000."""
        _Rig(tmp_hfcc_dir, monkeypatch, port, owner_after=FOREIGN)
        health.check_proxy()
        assert any("proxy repair on" in m and "FAILED" in m
                   for m in errors(logs)), \
            f"a squatter on :4000 did not surface as an error: {logs}"

    def test_repair_that_leaves_the_port_empty_still_alerts(
            self, tmp_hfcc_dir, monkeypatch, port, procs, logs, fake_sparkrun):
        """CONTROL: `proxy_down` survives this change — a repair that ends with
        nothing on the port is an outage, ERROR and first-tick alert included."""
        rig = _Rig(tmp_hfcc_dir, monkeypatch, port, owner_after=None)
        health.check_proxy()
        assert any("proxy repair on" in m and "FAILED" in m
                   for m in errors(logs)), logs
        assert rig.notified, f"a dead :4000 did not page anyone: {logs}"
