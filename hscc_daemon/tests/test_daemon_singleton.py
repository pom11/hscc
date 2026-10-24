"""Tests for daemon_singleton — the daemon-loop single-instance claim (t_52bd498e).

All adjudication runs through the module's hermetic seams (pinned suite-wide
by conftest._isolate_hscc): _PS_START_OVERRIDE (constant ps lstart),
_FAKE_ALIVE_PIDS (injected pid-liveness), _SCAN_OVERRIDE (injected process
count) — so NO test here ever probes the operator's live process table, and
pid-reuse cases are simulated by rebinding _PS_START_OVERRIDE per-test.

Context for every test: the 2026-10 incident this guards against was an
`hscc start` orphan loop serving for 2+ days beside launchd's start-daemon
(t_e37a74c7 root cause #1). The lock convention mirrors autodown._acquire_lock
(on purpose — one lock convention for daemon-local state).
"""

import os
import threading
import time

import pytest


@pytest.fixture
def singleton(tmp_path, monkeypatch):
    """Module + a lock path under tmp (conftest already redirected
    DAEMON_LOCK; we pass lock_path explicitly everywhere anyway to make the
    hermeticity per-test-visible)."""
    from hscc_daemon import daemon_singleton as ds
    ds.DAEMON_LOCK = str(tmp_path / "daemon.lock")
    return ds


@pytest.fixture
def hermetic_foreign(singleton, monkeypatch):
    """A foreign pid (987654) that adjudicates ALIVE with the recorded start."""
    monkeypatch.setattr(singleton, "_FAKE_ALIVE_PIDS", frozenset({987654}))
    return 987654


def _plant(singleton, pid, start="Mon Jan  1 00:00:00 2024", path=None):
    path = path or singleton.DAEMON_LOCK
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"pid={pid} start={start}")
    return path


# ── acquire ────────────────────────────────────────────────────────────────

class TestAcquire:
    def test_free_lock_is_acquired_and_records_holder(self, singleton):
        assert singleton.acquire_daemon_lock() is True
        body = singleton.read_lock_holder()
        assert f"pid={os.getpid()}" in body
        assert "start=" in body

    def test_acquire_is_idempotent_for_same_process(self, singleton):
        assert singleton.acquire_daemon_lock() is True
        # Second claim by the SAME process (e.g. loop backstop after the
        # entry point already claimed) must succeed and keep the record.
        assert singleton.acquire_daemon_lock() is True
        assert f"pid={os.getpid()}" in singleton.read_lock_holder()

    def test_live_foreign_holder_blocks_acquire(self, singleton,
                                                hermetic_foreign):
        _plant(singleton, hermetic_foreign)
        assert singleton.acquire_daemon_lock() is False

    def test_dead_holder_is_broken_and_reacquired(self, singleton):
        # conftest pins _FAKE_ALIVE_PIDS=frozenset(): pid 987654 is dead.
        _plant(singleton, 987654)
        assert singleton.acquire_daemon_lock() is True
        assert f"pid={os.getpid()}" in singleton.read_lock_holder()

    def test_pid_reuse_breaks_the_lock(self, singleton, monkeypatch):
        """The recorded pid is alive again but the process START differs ⇒
        the pid was recycled ⇒ the lock belongs to a dead holder."""
        _plant(singleton, 987654, start="Thu Oct 22 17:06:42 2026")
        monkeypatch.setattr(singleton, "_FAKE_ALIVE_PIDS", frozenset({987654}))
        # conftest's constant start ("Mon Jan  1 00:00:00 2024") ≠ recorded ⇒
        # recycled ⇒ breakable.
        assert singleton.acquire_daemon_lock() is True

    def test_unparseable_holder_uses_age_rule_fresh_blocks(self, singleton):
        _plant(singleton, -1, start="junk")  # no pid= token
        with open(singleton.DAEMON_LOCK, "w") as f:
            f.write("garbage without identity")
        assert singleton.acquire_daemon_lock() is False  # fresh ⇒ respect

    def test_unparseable_holder_aged_out_is_broken(self, singleton):
        _plant(singleton, -1)
        with open(singleton.DAEMON_LOCK, "w") as f:
            f.write("garbage without identity")
        old = time.time() - singleton.LOCK_STALE_AFTER - 5
        os.utime(singleton.DAEMON_LOCK, (old, old))
        assert singleton.acquire_daemon_lock() is True

    def test_live_holder_is_never_broken_however_old(self, singleton,
                                                     hermetic_foreign):
        _plant(singleton, hermetic_foreign)
        old = time.time() - singleton.LOCK_STALE_AFTER - 99999
        os.utime(singleton.DAEMON_LOCK, (old, old))
        # Liveness, not wall-clock, adjudicates a VERIFIABLE holder.
        assert singleton.acquire_daemon_lock() is False


# ── renew / release ────────────────────────────────────────────────────────

class TestRenewRelease:
    def test_renew_only_by_holder(self, singleton):
        # pid 987654 is DEAD under the conftest-pinned empty fake-alive set.
        _plant(singleton, 987654)
        assert singleton.renew_daemon_lock() is False      # not ours
        assert singleton.acquire_daemon_lock() is True     # holder dead
        assert singleton.renew_daemon_lock() is True       # ours now

    def test_renew_refused_while_live_foreign_holds(self, singleton,
                                                    hermetic_foreign):
        _plant(singleton, hermetic_foreign)               # alive foreign
        assert singleton.renew_daemon_lock() is False

    def test_renew_refreshes_mtime_and_identity(self, singleton):
        singleton.acquire_daemon_lock()
        old = time.time() - 4242
        os.utime(singleton.DAEMON_LOCK, (old, old))
        assert singleton.renew_daemon_lock() is True
        assert os.path.getmtime(singleton.DAEMON_LOCK) > old

    def test_release_is_ownership_gated(self, singleton):
        # Dead foreign holder: release must refuse to unlink it...
        _plant(singleton, 987654)
        assert singleton.release_daemon_lock() is False
        assert os.path.exists(singleton.DAEMON_LOCK)       # foreign intact
        # ...and remove only our own claim.
        singleton.acquire_daemon_lock()
        assert singleton.release_daemon_lock() is True
        assert not os.path.exists(singleton.DAEMON_LOCK)

    def test_release_refused_while_live_foreign_holds(self, singleton,
                                                      hermetic_foreign):
        _plant(singleton, hermetic_foreign)
        assert singleton.release_daemon_lock() is False
        assert os.path.exists(singleton.DAEMON_LOCK)

    def test_release_never_raises_when_absent(self, singleton):
        assert singleton.release_daemon_lock() is False


# ── the entry-point hold() ─────────────────────────────────────────────────

class TestHold:
    def test_free_claim_succeeds(self, singleton):
        ok, why = singleton.hold_daemon_loop_lock(lock_path=singleton.DAEMON_LOCK,
                                                  contender_wait=0)
        assert ok is True and why is None

    def test_live_twin_is_refused_loudly(self, singleton, hermetic_foreign,
                                         monkeypatch):
        _plant(singleton, hermetic_foreign)
        monkeypatch.setattr(singleton.time, "sleep", lambda s: None)
        ok, why = singleton.hold_daemon_loop_lock(
            lock_path=singleton.DAEMON_LOCK, contender_wait=1)
        assert ok is False
        assert "another daemon loop is ALREADY holding" in why
        assert "987654" in why            # holder pid surfaced to the operator

    def test_stop_event_during_wait_refuses_without_waiting(
            self, singleton, hermetic_foreign):
        _plant(singleton, hermetic_foreign)
        ev = threading.Event()
        ev.set()
        t0 = time.monotonic()
        ok, why = singleton.hold_daemon_loop_lock(
            stop_event=ev, lock_path=singleton.DAEMON_LOCK,
            contender_wait=30)
        assert ok is False and "shutdown requested" in why
        assert time.monotonic() - t0 < 1.0     # no pointless 30s wait

    def test_holder_released_during_wait_is_reacquired(
            self, singleton, hermetic_foreign):
        _plant(singleton, hermetic_foreign)

        def _release():
            time.sleep(0.05)
            try:
                os.unlink(singleton.DAEMON_LOCK)
            except OSError:
                pass
        threading.Thread(target=_release, daemon=True).start()
        ok, why = singleton.hold_daemon_loop_lock(
            lock_path=singleton.DAEMON_LOCK, contender_wait=5, poll=0.02)
        assert ok is True and why is None

    def test_fail_open_when_lock_dir_unpreparable(self, singleton, monkeypatch):
        def _boom(*a, **k):
            raise OSError("read-only fs")
        monkeypatch.setattr(singleton.os, "makedirs", _boom)
        ok, why = singleton.hold_daemon_loop_lock_fail_open(
            lock_path="/nonexistent-root/x/daemon.lock")
        assert ok is True                       # daemon must still start
        assert "disabled" in why

    def test_fail_open_on_adjudication_crash(self, singleton, monkeypatch):
        def _boom(*a, **k):
            raise OSError("EACCES")
        monkeypatch.setattr(singleton, "hold_daemon_loop_lock", _boom)
        ok, why = singleton.hold_daemon_loop_lock_fail_open(
            lock_path=singleton.DAEMON_LOCK)
        assert ok is True and "disabled" in why

    def test_fail_open_still_refuses_a_live_twin(self, singleton,
                                                 hermetic_foreign, monkeypatch):
        _plant(singleton, hermetic_foreign)
        monkeypatch.setattr(singleton.time, "sleep", lambda s: None)
        ok, why = singleton.hold_daemon_loop_lock_fail_open(
            lock_path=singleton.DAEMON_LOCK, contender_wait=1)
        assert ok is False                      # a REAL twin is never failed-open


# ── process scan + report ──────────────────────────────────────────────────

class TestScan:
    @pytest.mark.parametrize("cmd,want", [
        ("/Users/x/miniconda3/envs/p313/bin/hscc start", True),
        ("/Users/x/.hermes/hermes-agent/venv/bin/python "
         "/Users/x/.hermes/plugins/hscc_daemon/hscc.py start-daemon", True),
        ("hscc start-daemon", True),
        ("hscc status", False),
        ("/x/bin/hscc api start --tailscale --no-qr", False),
        ("hscc cluster monitor --live", False),
        ("python -m hermes_cli.main serve --port 9119", False),
        ("hscc start extra-flag", False),          # verb must be LAST token
        ("helper start", False),                    # no hscc token at all
    ])
    def test_loop_command_matcher(self, singleton, cmd, want):
        assert singleton._is_loop_command(cmd) is want

    def test_scan_honours_override(self, singleton, monkeypatch):
        monkeypatch.setattr(singleton, "_SCAN_OVERRIDE", 2)
        assert singleton.count_loop_processes() == 2

    def test_scan_failure_is_zero(self, singleton, monkeypatch):
        monkeypatch.setattr(singleton, "_SCAN_OVERRIDE", None)

        def _boom(*a, **k):
            raise OSError("ps gone")
        monkeypatch.setattr(singleton.subprocess, "run", _boom)
        assert singleton.count_loop_processes() == 0

    def test_report_twin_true(self, singleton, monkeypatch):
        monkeypatch.setattr(singleton, "_SCAN_OVERRIDE", 2)
        _plant(singleton, os.getpid())
        rep = singleton.daemon_instance_report()
        assert rep["twin"] is True and rep["loop_processes"] == 2
        assert rep["holder_alive"] is True

    def test_report_no_lock_no_twin(self, singleton, monkeypatch):
        monkeypatch.setattr(singleton, "_SCAN_OVERRIDE", 1)
        rep = singleton.daemon_instance_report()
        assert rep["lock_present"] is False
        assert rep["twin"] is False

    def test_report_dead_holder_is_stale_claim(self, singleton, monkeypatch):
        monkeypatch.setattr(singleton, "_SCAN_OVERRIDE", 0)
        _plant(singleton, 987654)   # dead under conftest fake-alive set
        rep = singleton.daemon_instance_report()
        assert rep["holder_alive"] is False
        assert rep["lock_stale_claim"] is True

    def test_report_never_raises_when_scan_off(self, singleton):
        rep = singleton.daemon_instance_report(scan=False)
        assert rep["twin"] is None            # UNKNOWN, not False


# ── loop entry + release wiring (daemon_ops) ───────────────────────────────

class TestLoopWiring:
    def test_run_daemon_loop_refuses_when_claim_refused(self, monkeypatch):
        """The unbypassable backstop: entry to run_daemon_loop with a refused
        claim exits nonzero (3) BEFORE any check import, thread or log file."""
        from hscc_daemon import daemon_ops, daemon_singleton as ds
        called = []
        monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                            lambda **k: (False, "twin live pid=42"))
        monkeypatch.setattr(daemon_ops, "log",
                            lambda *a, **k: called.append(a))
        with pytest.raises(SystemExit) as exc:
            daemon_ops.run_daemon_loop()
        assert exc.value.code == 3
        assert any("refused" in str(a[0]) and a[1:2] == ("ERROR",)
                   for a in called), called

    def test_run_daemon_loop_proceeds_on_claim(self, monkeypatch, tmp_path):
        """ok=True ⇒ the loop gets past the claim (we stop it immediately by
        making the very first post-claim step raise; the claim itself must
        NOT be what stops us)."""
        from hscc_daemon import daemon_ops, daemon_singleton as ds
        acquired = []
        real = ds.acquire_daemon_lock

        def spy(lock_path=None, now=None):
            r = real(lock_path, now)
            if r:
                acquired.append(True)
            return r
        monkeypatch.setattr(ds, "acquire_daemon_lock", spy)
        # First post-claim import in run_daemon_loop is os.umask + health
        # import; blow up deterministically at umask to keep the test cheap.
        monkeypatch.setattr(daemon_ops.os, "umask",
                            lambda m: (_ for _ in ()).throw(RuntimeError("stop-here")))
        with pytest.raises(RuntimeError, match="stop-here"):
            daemon_ops.run_daemon_loop()
        assert acquired, "run_daemon_loop must take the claim before booting"

    def test_heartbeat_body_renews_lock_on_tick(self, singleton):
        """The main-loop body renews the claim each heartbeat tick."""
        from hscc_daemon import daemon_ops
        singleton.acquire_daemon_lock()
        old = time.time() - 5000
        os.utime(singleton.DAEMON_LOCK, (old, old))
        ev = threading.Event()
        t = threading.Thread(
            target=daemon_ops.run_heartbeat_loop,
            args=(ev,), kwargs={"interval": 0, "renew_lock_fn":
                                lambda: singleton.renew_daemon_lock(
                                    singleton.DAEMON_LOCK)},
            daemon=True)
        t.start()
        time.sleep(0.3)
        ev.set()
        t.join(2)
        assert os.path.getmtime(singleton.DAEMON_LOCK) > old

    def test_heartbeat_survives_renewal_error(self, singleton):
        """A raising renew fn is WARN'd, never fatal (heartbeat continuity
        outranks lock bookkeeping)."""
        from hscc_daemon import daemon_ops
        ev = threading.Event()

        def boom():
            raise RuntimeError("lock fs gone")
        t = threading.Thread(
            target=daemon_ops.run_heartbeat_loop,
            args=(ev,), kwargs={"interval": 0, "renew_lock_fn": boom},
            daemon=True)
        t.start()
        time.sleep(0.2)
        alive = t.is_alive()
        ev.set()
        t.join(2)
        assert alive, "a raising lock renewal must not kill the heartbeat loop"


# ── entry points (cli) ─────────────────────────────────────────────────────

class TestCmdStartDaemonClaim:
    """cmd_start_daemon must claim BEFORE write_stopped/save_pid — the exact
    overwrite sequence that let launchd re-label supervision over a live
    orphan (t_e37a74c7 root cause #1) — and must not touch the pid file when
    the claim is refused."""

    def _wire(self, tmp_hfcc_dir, monkeypatch, hold_result):
        from hscc_daemon import cli, daemon_ops
        from hscc_daemon import state as state_mod
        import hscc_daemon as pkg
        monkeypatch.setattr(state_mod, "STATE_DIR", str(tmp_hfcc_dir / "state"))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(daemon_ops, "LOG_FILE", str(tmp_hfcc_dir / "log"))
        order = []
        monkeypatch.setattr(daemon_ops, "run_daemon_loop",
                            lambda: order.append("loop"))
        monkeypatch.setattr(daemon_ops, "write_stopped",
                            lambda: order.append("write_stopped"))
        monkeypatch.setattr(daemon_ops, "save_pid",
                            lambda: order.append("save_pid"))
        monkeypatch.setattr(state_mod, "ensure_state_dir", lambda: None)
        # cli does `from . import log` — patch the PACKAGE attribute.
        monkeypatch.setattr(pkg, "log", lambda *a, **k: order.append("log"))
        from hscc_daemon import daemon_singleton as ds
        monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                            lambda **k: (order.append("claim"),
                                         hold_result)[1])
        return order

    def test_claim_precedes_pid_bookkeeping(self, tmp_hfcc_dir, monkeypatch):
        order = self._wire(tmp_hfcc_dir, monkeypatch, (True, None))
        from hscc_daemon import cli
        cli.cmd_start_daemon()
        assert order.index("claim") < order.index("write_stopped")
        assert order.index("claim") < order.index("save_pid")
        assert "loop" in order

    def test_refused_claim_exits_3_without_pid_bookkeeping(
            self, tmp_hfcc_dir, monkeypatch):
        order = self._wire(tmp_hfcc_dir, monkeypatch,
                           (False, "another daemon loop holds the lock"))
        from hscc_daemon import cli
        with pytest.raises(SystemExit) as exc:
            cli.cmd_start_daemon()
        assert exc.value.code == 3
        assert "write_stopped" not in order, \
            "a refused contender must NOT delete the live holder's pid file"
        assert "save_pid" not in order
        assert "loop" not in order


class TestCmdStartClaim:
    """The forked path: the grandchild claims BEFORE save_pid, and a refusal
    surfaces to the CLI parent instead of a fake 'started' panel."""

    class _ForkExit(Exception):
        """Faithful os._exit emulation: real _exit never returns, so the
        stub must STOP the code path (append + raise), not fall through."""

        def __init__(self, code):
            super().__init__(code)
            self.code = code

    def _fork_emu(self, monkeypatch, cli, daemon_ops, tmp_hfcc_dir,
                  hold_result, seq=(0, 0)):
        from hscc_daemon import state as state_mod
        order = []
        monkeypatch.setattr(state_mod, "STATE_DIR", str(tmp_hfcc_dir / "state"))
        monkeypatch.setattr(state_mod, "ensure_state_dir", lambda: None)
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        monkeypatch.setattr(daemon_ops, "get_pid", lambda: None)
        monkeypatch.setattr(daemon_ops, "write_stopped",
                            lambda: order.append("stopped"))
        monkeypatch.setattr(daemon_ops, "save_pid",
                            lambda: order.append("save_pid"))
        monkeypatch.setattr(daemon_ops, "run_daemon_loop",
                            lambda: order.append("loop"))
        monkeypatch.setattr(cli, "_detach_stdio", lambda: order.append("detach"))
        from hscc_daemon import daemon_singleton as ds
        monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                            lambda **k: (order.append("claim"),
                                         hold_result)[1])

        def _forks():
            for v in seq:
                yield v
            return 1        # any further fork in a test is a parent: no loop
        gen = _forks()
        monkeypatch.setattr(cli.os, "fork", lambda: next(gen))
        monkeypatch.setattr(cli.os, "setsid", lambda: None)
        monkeypatch.setattr(cli.os, "chdir", lambda p: None)

        def _exit(code=0):
            order.append(f"exit{code}")
            raise TestCmdStartClaim._ForkExit(code)
        monkeypatch.setattr(cli.os, "_exit", _exit)
        import signal as _sig
        monkeypatch.setattr(_sig, "signal", lambda *a, **k: None)
        return order

    def test_grandchild_claims_between_detach_and_save_pid(
            self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli, daemon_ops
        order = self._fork_emu(monkeypatch, cli, daemon_ops, tmp_hfcc_dir,
                               (True, None))
        cli.cmd_start()
        assert "claim" in order
        assert order.index("detach") < order.index("claim")
        assert order.index("claim") < order.index("save_pid"), \
            "claim BEFORE the pid label — the twin incident started here"
        assert "loop" in order

    def test_refusal_exits_3_before_save_pid(self, tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import cli, daemon_ops
        order = self._fork_emu(monkeypatch, cli, daemon_ops, tmp_hfcc_dir,
                               (False, "twin live"))
        # Real os._exit(3) TERMINATES the grandchild: faithful emulation stops
        # the code path, so save_pid/loop must never run.
        with pytest.raises(TestCmdStartClaim._ForkExit) as exc:
            cli.cmd_start()
        assert exc.value.code == 3
        assert "save_pid" not in order
        assert "loop" not in order
        assert "exit3" in order


class TestStatusTwinSurface:
    """`hscc status` reports >1 loop when it happens (report-only: status
    must never be able to kill anything)."""

    def _run_status(self, tmp_hfcc_dir, monkeypatch, report):
        import io
        from contextlib import redirect_stdout
        from hscc_daemon import cli, daemon_ops
        from hscc_daemon import state as state_mod
        from hscc_daemon import daemon_singleton as ds
        state_dir = tmp_hfcc_dir / "state"
        state_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        monkeypatch.setattr(daemon_ops, "PID_FILE", str(tmp_hfcc_dir / "pid"))
        # report may be a curated dict OR a callable (raise-injected).
        fn = report if callable(report) else (lambda **k: report)
        monkeypatch.setattr(ds, "daemon_instance_report", fn)
        f = io.StringIO()
        with redirect_stdout(f):
            cli.cmd_status()
        return f.getvalue()

    def test_twin_line_printed(self, tmp_hfcc_dir, monkeypatch):
        out = self._run_status(
            tmp_hfcc_dir, monkeypatch,
            {"lock_present": True, "holder": "pid=999 start=X",
             "holder_pid": 999, "holder_alive": True,
             "loop_processes": 2, "twin": True, "lock_stale_claim": False})
        assert "TWIN LOOP DETECTED" in out
        assert "2" in out

    def test_no_twin_no_line(self, tmp_hfcc_dir, monkeypatch):
        out = self._run_status(
            tmp_hfcc_dir, monkeypatch,
            {"lock_present": True, "holder": f"pid={os.getpid()} start=X",
             "holder_pid": os.getpid(), "holder_alive": True,
             "loop_processes": 1, "twin": False, "lock_stale_claim": False})
        assert "TWIN LOOP DETECTED" not in out

    def test_unknown_twin_prints_nothing(self, tmp_hfcc_dir, monkeypatch):
        """twin=None is UNKNOWN — must not cry wolf."""
        out = self._run_status(
            tmp_hfcc_dir, monkeypatch,
            {"lock_present": False, "holder": None, "holder_pid": None,
             "holder_alive": None, "loop_processes": None, "twin": None,
             "lock_stale_claim": False})
        assert "TWIN LOOP DETECTED" not in out

    def test_stale_claim_note(self, tmp_hfcc_dir, monkeypatch):
        out = self._run_status(
            tmp_hfcc_dir, monkeypatch,
            {"lock_present": True, "holder": "pid=1 start=X",
             "holder_pid": 1, "holder_alive": False,
             "loop_processes": 0, "twin": False, "lock_stale_claim": True})
        assert "stale daemon single-instance lock" in out

    def test_broken_report_is_silent(self, tmp_hfcc_dir, monkeypatch):
        """A diagnostic that explodes must not take the status surface down
        (same silence contract as the update notice)."""
        def boom(**k):
            raise RuntimeError("report exploded")
        # must not raise:
        out = self._run_status(tmp_hfcc_dir, monkeypatch, boom)
        assert "HSCC Daemon Status" in out
