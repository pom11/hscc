"""Shared test fixtures for hscc_daemon unit tests.

Usage::

    # For state module:
    def test_something(tmp_hfcc_dir, monkeypatch):
        from hscc_daemon import state
        monkeypatch.setattr(state, "STATE_DIR", str(tmp_hfcc_dir / "state"))
        state.ensure_state_dir()
        ...

    # For lifecycle module:
    def test_watchdog(tmp_hfcc_dir, monkeypatch):
        from hscc_daemon.lifecycle import save_watchdog_block
        monkeypatch.setattr("hscc_daemon.lifecycle.WATCHDOG_BLOCK_FILE",
                            str(tmp_hfcc_dir / "watchdog-block.json"))
        ...
"""

import json
import os
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Sparkrun argv fence (t_be4b94ca — live proxy SIGTERM incident)
# ---------------------------------------------------------------------------
#
# The existing `_hermetic_sparkrun_resolution` fixture (further down) pins
# HSCC_SPARKRUN_BIN so every path that resolves through hscc_daemon.sparkrun_bin
# execs a per-test fake. It cannot see literal-argv calls — and this package has
# two: cluster_live.HostStream runs `["sparkrun", "cluster", "monitor", ...]`
# via subprocess.Popen, and serving's command constants are plain argv lists.
# The shared fence wrapper (same helper the hscc-cluster suite fences with, and
# the module whose absence must fail collection loudly) closes exactly that gap:
# a sparkrun argv that resolves to the operator's LIVE CLI raises instead of
# exec'ing; the HSCC_SPARKRUN_BIN fake resolves under tmp_path and passes.

def _load_sparkrun_fence():
    import importlib.util

    _here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        # repo layout: <repo>/hscc_daemon/tests -> <repo>/hscc-cluster/tests
        os.path.join(_here, os.pardir, os.pardir, "hscc-cluster", "tests",
                     "sparkrun_fence.py"),
        # deployed layout: ~/.hermes/plugins/hscc_daemon/tests -> ...
        os.path.join(os.path.expanduser("~"), ".hermes", "plugins",
                     "hscc-cluster", "tests", "sparkrun_fence.py"),
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.exists(path):
            spec = importlib.util.spec_from_file_location("sparkrun_fence", path)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules["sparkrun_fence"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError(
        "sparkrun fence module not found (looked in: "
        + ", ".join(candidates)
        + ") — refusing to run this suite unfenced (t_be4b94ca)."
    )


_sparkrun_fence = _load_sparkrun_fence()


@pytest.fixture(autouse=True)
def _fence_sparkrun_argv(monkeypatch):
    """Block any test that would exec the operator's real sparkrun CLI."""
    _sparkrun_fence.install(monkeypatch)


_fence_reported = set()


@pytest.fixture(autouse=True)
def _fence_guard():
    """Fail the test (or fixture) that tried to reach the live sparkrun argv.

    See hscc-cluster/tests/conftest.py::_record_sparkrun_calls for why this
    enforcement lives in a fixture teardown and not in a session-finish hook.
    """
    yield
    fresh = []
    for e in _sparkrun_fence.unexpected_blocked():
        key = (tuple(e["argv"]), e["why"])
        if key not in _fence_reported:
            _fence_reported.add(key)
            fresh.append(e)
    if fresh:
        raise AssertionError(
            "SPARKRUN FENCE GUARD: a test/fixture tried to exec the operator's "
            f"live sparkrun CLI: {[e['argv'] for e in fresh]}. Resolve through "
            "sparkrun_bin (pinned hermetic by _hermetic_sparkrun_resolution) or "
            "patch the seam — t_be4b94ca."
        )


def pytest_terminal_summary(terminalreporter):
    """The fence's session report is part of the visible output, every run."""
    lines = _sparkrun_fence.report().splitlines()
    terminalreporter.write_sep("=", lines[0])
    for line in lines[1:]:
        terminalreporter.write_line(line)


# ---------------------------------------------------------------------------
# Autouse ~/.hscc isolation (RELEASE BLOCKER regression)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_hscc(tmp_path, monkeypatch):
    """Redirect EVERY ~/.hscc write path to a per-test tmp dir.

    Runs for EVERY test in the package (autouse), so no test can reach the
    operator's real ~/.hscc even when it forgets to patch individually.

    Why this exists (RELEASE BLOCKER): test_daemon_ops.py patched
    ``autodown.load_config`` to return {"enabled": True} but did NOT patch
    ``save_config`` / ``autodown.AUTODOWN_FILE``. Phase 6 added activity probes
    to ``cycle()``, so the timing had ``record_activity()`` running during
    those tests, calling the PATCHED loader and ``save_config``-ing the partial
    3-key dict straight to the REAL ``~/.hscc/autodown.json`` — silently arming
    idle-teardown on the live cluster. A later variant leaked into the LIVE
    ``~/.hscc/watchdog-block.json`` (test_trigger.py / test_health.py did not
    patch ``lifecycle.WATCHDOG_BLOCK_FILE``): the suite wrote
    {"blocked": true, "intentional": "autodown"} over the real block, silently
    disabling cluster supervision for ~1 hour.

    Belt-and-braces, covering the DIRECTORY AS A WHOLE:
      (a) Patch ``os.path.expanduser`` so any ``~/.hscc/...`` path computed at
          RUNTIME (function-scoped ``expanduser`` calls, e.g.
          lifecycle.py:30/:87, verify.py:132, daemon_ops usage) resolves under
          the per-test tmp dir. Everything outside ``~/.hscc`` is delegated to
          the real expanduser unchanged.
      (b) Patch every module-level ``~/.hscc``-rooted constant (each module
          computes its own copy at IMPORT time, so they are already baked in
          before this fixture runs and must be overwritten explicitly). See
          ``_module_attrs()`` — the enumerated audit in the card.
      (c) Redirect ``state.STATE_DIR`` to the tmp state dir too, so the common
          ``state.write_state`` funnel can never reach ``~/.hscc/state``.

    Function-scoped monkeypatch is torn down after each test, and any test
    that patches its own tmp path simply overrides (LIFO teardown → the test's
    patch wins during the test).
    """
    base = str(tmp_path / "hscc")

    from hscc_daemon import (
        autodown, daemon_ops, desktop, dispatcher_wedge, hscc, lifecycle,
        recover, state, trigger,
    )
    # (a) Runtime expanduser redirect for the whole ~/.hscc directory tree.
    real_hscc = os.path.expanduser("~/.hscc")
    _real_expanduser = os.path.expanduser

    def _redirect_expanduser(path):
        expanded = _real_expanduser(path)
        if expanded == real_hscc:
            return base
        if expanded.startswith(real_hscc + os.sep):
            return os.path.join(base, expanded[len(real_hscc) + 1:])
        return expanded

    monkeypatch.setattr(os.path, "expanduser", _redirect_expanduser)

    # (b) Module-level constants already computed at import — overwrite each.
    def _module_attrs():
        from hscc_daemon import (
            autodown, daemon_ops, daemon_singleton, desktop, health,
            heal_breaker, heal_lease,
            hscc, lifecycle,
            serving, state, trigger, update_check, usage,
        )
        def p(sub):
            return os.path.join(base, sub)
        return [
            # autodown.py
            (autodown, "AUTODOWN_FILE", p("autodown.json")),
            (autodown, "AUTODOWN_LOCK", p("autodown.lock")),
            # daemon_singleton.py (t_52bd498e) — the daemon-loop
            # single-instance claim. This MUST be redirected: every
            # run_daemon_loop / cmd_start / cmd_start_daemon test path takes
            # and releases it, so an unredirected path would write a LIVE
            # claim into the operator's real ~/.hscc (visible to the
            # no-live-leak audit as a new file) and could suppress the real
            # daemon's next start.
            (daemon_singleton, "DAEMON_LOCK", p("daemon.lock")),
            # Hermetic twin scan: the suite must never observe the OPERATOR's
            # live process table (a real second loop on the dev box would
            # make cmd_status print the twin line and break byte-comparison
            # tests — flaky-by-environment). Pin the override to 0; tests
            # that want a specific count set _SCAN_OVERRIDE themselves.
            (daemon_singleton, "_SCAN_OVERRIDE", 0),
            # Hermetic holder adjudication (t_52bd498e): no test may run
            # `ps -o lstart` / os.kill against pids on the operator's host.
            # Constant start string + empty foreign-alive set ⇒ every planted
            # foreign holder adjudicates DEAD ("stale", breakable), every
            # self-claim adjudicates "mine"; tests needing a live foreign
            # holder set both seams per-test.
            (daemon_singleton, "_PS_START_OVERRIDE", "Mon Jan  1 00:00:00 2024"),
            (daemon_singleton, "_FAKE_ALIVE_PIDS", frozenset()),
            (autodown, "AGENTS_FILE", p("agents.json")),
            (autodown, "HTTP_ACTIVITY_STATE", p("activity.json")),
            # Hermes cron jobs.json is Hermes' source of truth (feat
            # t_2b711a94) — redirect so no test ever reads the real host's
            # scheduled jobs. A missing tmp file yields CRON_UNREADABLE (fail-
            # closed); CLI tests stub list_active_cron_jobs for their cases.
            (autodown, "CRON_JOBS_FILE", p("cron/jobs.json")),
            # lifecycle.py
            (lifecycle, "BRIDGE_FILE", p("bridge.json")),
            (lifecycle, "WATCHDOG_BLOCK_FILE", p("watchdog-block.json")),
            # state.py (the write_state/read_state funnel)
            (state, "STATE_DIR", p("state")),
            # usage.py (budget config — never read the operator's real one)
            (usage, "BUDGET_FILE", p("budget.json")),
            # recover.py (cooldown + attempt-cap state)
            (recover, "RECOVER_STATE_FILE", p("recover.json")),
            # trigger.py
            (trigger, "EVENTS_FILE", p("events.jsonl")),
            (trigger, "TRIGGERS_FILE", p("triggers.json")),
            (trigger, "COOLDOWN_FILE", p("cooldowns.json")),
            # daemon_ops.py
            (daemon_ops, "PID_FILE", p("daemon.pid")),
            (daemon_ops, "HEARTBEAT_FILE", p("heartbeat")),
            (daemon_ops, "LOG_FILE", p("daemon.log")),
            (daemon_ops, "STATE_DIR", p("state")),
            (daemon_ops, "HSCC_DIR", base),
            # desktop.py (emit_event / notifications live under HSCC_DIR)
            (desktop, "HSCC_DIR", base),
            # hscc.py CLI module
            (hscc, "BRIDGE_FILE", p("bridge.json")),
            (hscc, "ORCH_ENDPOINT_STATE", p("orch-endpoint")),
            (hscc, "PROFILES_DIR", p("profiles")),
            # serving.py — the autouse fixture previously did NOT cover these,
            # and serving.update_orchestrator_followers() REWRITES config.yaml
            # files under ~/.hermes/profiles (a real latent leak seam). Redirect
            # every $HOME-rooted constant so a test calling any serving/health
            # path function can never reach the operator's live files. (Tests
            # that patch these themselves win via LIFO teardown.)
            (serving, "SERVING_JSON", p("serving.json")),
            (serving, "CLUSTER_JSON", p("cluster.json")),
            (serving, "PROFILES_DIR", p("profiles")),
            (serving, "ORCH_ENDPOINT_STATE", p("orch-endpoint")),
            # health.py — the worker relaunch + container-age state files.
            # The auto-heal startup grace keyed on container creation time
            # persists has-served / created_ts here, so it MUST be redirected
            # or the sandboxed no-live-leak audit trips (it appeared as a new
            # file in the planted ~/.hscc). Redirect both to the per-test base.
            (health, "_WORKER_RELATCH_FILE", p("worker_relaunch.json")),
            (health, "_WORKER_CONTAINER_STATE_FILE",
             p("worker_container_state.json")),
            # heal_lease.py (F2) — the durable single-flight repair lease. This
            # MUST be redirected: check_workers / check_proxy / recover now all
            # take and release it, so an unredirected path would write a LIVE
            # lease into the operator's real ~/.hscc and suppress the running
            # daemon's heals for the whole TTL.
            (heal_lease, "HEAL_LEASE_FILE", p("heal-lease.json")),
            # heal_breaker.py (F5) — the durable per-node strike ledger. Same
            # MUST-redirect rationale as the lease: check_workers / check_proxy
            # now count a strike on every heal, so an unredirected path would
            # write strikes into the operator's real ~/.hscc and could suspend
            # healing a LIVE node from the test run.
            (heal_breaker, "HEAL_BREAKER_FILE", p("heal-breaker.json")),
            # update_check.py (t_a4055572) — the 24h GitHub-release cache. It
            # is the ONE file this module may ever write, and both ambient
            # surfaces (the status notice and the daemon's daily tick) reach
            # it, so an unredirected path would litter the operator's real
            # ~/.hscc from every test that renders status. The constant is
            # kept as a "~"-template precisely so the (a) expanduser redirect
            # above covers it too; rebinding here is the belt-and-braces half.
            (update_check, "CACHE_FILE", p("update-check.json")),
            # update_check.py (t_9b98e696) — the PATH shadow scan DISABLED by
            # default for the whole suite. The scan's inputs are PATH itself,
            # which conftest must NOT rewrite (subprocess lookups for bash
            # etc. depend on it); emptying SHADOW_SCAN_MODES makes every
            # default-path call return None before it walks or probes
            # anything, so no verb test can exec a real `hscc --version` off
            # the operator's live PATH. Shadow tests re-arm it explicitly
            # (monkeypatch SHADOW_SCAN_MODES) and always pass a fabricated
            # path_env + probe, never the real PATH.
            (update_check, "SHADOW_SCAN_MODES", ()),
            # launch guard (A2) — every launch path consults
            # health._launch_guard_status_fn() before starting a container.
            # Default it to "we looked, nothing foreign is running" ({}), so
            # the ~100 pre-existing heal/autoup tests keep their semantics
            # instead of failing on NO_STATUS, and freshen the guard's caches
            # so a refusal-alert cooldown from one test cannot suppress
            # another test's assertion. A2's own tests re-patch the seam.
            (health, "_launch_guard_status_fn", lambda force=False: {}),
            (health, "_LAUNCH_STATUS_CACHE", {"at": 0.0, "status": None}),
            (health, "_LAUNCH_ALERT_AT", {}),
            # dispatcher_wedge.py (t_160d306f) — the layer-1 hold classifier
            # tails the GATEWAY LOG. Redirect to a per-test path that does not
            # exist: the tail then returns None (= "no hold evidence", the
            # fail-closed default), so no test ever reads the operator's live
            # ~/.hermes/logs/gateway.log, and tests wanting hold evidence pass
            # an explicit hold_fn / write their own log file.
            (dispatcher_wedge, "GATEWAY_LOG", p("gateway.log")),
            # Layer-2 board gate (t_160d306f): the default gate consults the
            # hermes CLI. In the sandbox the binary EXISTS on this host, so
            # without this the existing restart tests would shell out to the
            # real CLI and read real boards (slow, and their outcome would
            # depend on live board state). Stub "no hermes CLI" = the gate's
            # documented fail-closed path → the restart proceeds exactly as
            # pre-fix, so every pre-existing recovery test keeps its
            # semantics. Gate tests re-patch this seam explicitly.
            (dispatcher_wedge, "_hermes_cli_argv", lambda: None),
            # ...and the same for the gate's profile probe: the real
            # hermes_cli.profiles IS importable in the test interpreter, so
            # without this, fake test assignees ("worker-a") would resolve as
            # control-plane lanes and every ready card would self-explain.
            # Default False = "unknown, assume spawnable" — the fail-closed
            # direction (stricter evidence required). Gate tests opt in via
            # monkeypatch when they exercise the nonspawnable path.
            (dispatcher_wedge, "_assignee_is_nonspawnable",
             lambda assignee: False),
        ]

    for mod, attr, val in _module_attrs():
        monkeypatch.setattr(mod, attr, val, raising=False)

    # NOT neutered here, deliberately. desktop._notify_macos / _notify_linux
    # shell out to osascript / notify-send, so a test that reaches a real
    # notification path would push one on the operator's desktop. The obvious
    # fix -- an autouse floor replacing both -- was tried and BROKE the suite:
    # test_desktop.py tests those two functions directly (fakes subprocess.run,
    # asserts the rc-0 return), so replacing them replaces the code under test
    # (2 failures, and the no-live-leak canary failed on its nested run). A
    # global floor needs a growing carve-out list, i.e. it silently changes
    # behaviour for 1460 tests to protect against a hypothetical.
    #
    # Convention instead, already used across the suite: a test that exercises
    # a notification path patches the high-level sender itself
    # (monkeypatch.setattr(<module>, "send_macos_notification", ...)) -- see
    # test_autodown.py (30+ sites), test_cli.py, test_heal_breaker.py:332 and
    # test_launch_guard.py. Patch the sender, not the OS shim.


@pytest.fixture(autouse=True)
def _hermetic_topology():
    """Pin serving's LAZY topology to the documented defaults for every test.

    serving.py resolves cluster topology on the first READ of a topology name
    (t_8d2eb400 — import must execute nothing). Tests that patch one name
    (``monkeypatch.setattr(serving, "VLLM_HEALTH_URL", ...)``) and let a check
    read another (``serving.PRIMARY_NODE``) would otherwise trigger a REAL
    resolution inside the test — including the ``sparkrun cluster list --json``
    fallback shell-out (since t_be4b94ca a list argv built by
    ``sparkrun_bin.argv``, so ``HSCC_SPARKRUN_BIN`` DOES cover it now, but a
    test that never intended to resolve topology must still not reach even the
    hermetic fake). Materialising the defaults here costs no I/O, keeps
    ``setattr``-style patches (raising=True) working, and teardown hands the
    names back to the lazy mechanism so every test starts unresolved. Tests
    that want resolution call ``resolve_cluster_config()`` themselves (or
    delete these globals explicitly).
    """
    from hscc_daemon import serving

    serving._materialize_topology_defaults()
    yield
    serving.reset_resolution_cache()


@pytest.fixture(autouse=True)
def _hermetic_sparkrun_resolution(tmp_path, monkeypatch):
    """Pin sparkrun resolution so no test can reach the operator's real CLI.

    t_b543e530 made the daemon resolve ``sparkrun`` from the FILESYSTEM (PATH,
    then a candidate list) instead of only PATH — so an unpinned test would
    resolve the developer machine's real ``~/.local/bin/sparkrun``, and any test
    asserting on the resolved argv would pass here and fail on a host without
    sparkrun. Default EVERY test to a per-test fake CLI (an ``exit 0`` shell
    script named ``sparkrun``) via the ``HSCC_SPARKRUN_BIN`` override, which is
    also hermetic by construction: no test ever execs the real CLI, the real
    venv python, or the fleet.

    A test that wants a different environment simply re-points the override:
    a path that does not exist == "sparkrun is not installed anywhere"
    (the degraded shape), or its own script whose shebang names the
    interpreter it wants. Always call ``sparkrun_bin.reset_cache()`` after
    changing the override — resolution is cached per process.
    """
    from hscc_daemon import sparkrun_bin

    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_cli = fake_bin / "sparkrun"
    fake_cli.write_text("#!/bin/sh\nexit 0\n")
    fake_cli.chmod(0o755)
    monkeypatch.setenv("HSCC_SPARKRUN_BIN", str(fake_cli))
    sparkrun_bin.reset_cache()
    yield str(fake_cli)
    sparkrun_bin.reset_cache()


class _SubprocessResult:
    """Fake subprocess.CompletedProcess for controlling command output."""

    def __init__(self, stdout="", stderr="", returncode=0,
                 timeout_exc=False, file_not_found=False):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self._timeout_exc = timeout_exc
        self._file_not_found = file_not_found


class _FakeSubprocess:
    """Patch subprocess.run and provide set_result() for the next call."""

    def __init__(self, monkeypatch):
        self._queue = []

    def _fake_run(self, *args, **kwargs):
        if self._queue:
            result = self._queue.pop(0)
        else:
            result = _SubprocessResult()
        if result._timeout_exc:
            raise subprocess.TimeoutExpired(
                cmd=args[0] if args else "cmd",
                timeout=kwargs.get("timeout", 30)
            )
        if result._file_not_found:
            raise FileNotFoundError(
                args[0][0] if args and isinstance(args[0], list) else "cmd"
            )
        return result

    def set_result(self, stdout="", stderr="", returncode=0,
                   timeout_exc=False, file_not_found=False):
        self._queue.append(_SubprocessResult(
            stdout=stdout, stderr=stderr, returncode=returncode,
            timeout_exc=timeout_exc, file_not_found=file_not_found
        ))


@pytest.fixture
def tmp_hfcc_dir(tmp_path):
    """Return a path to use as HSCC_DIR / state dir for a single test."""
    d = tmp_path / "hscc"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def state_dir(tmp_hfcc_dir):
    """Create an empty state directory."""
    d = tmp_hfcc_dir / "state"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def sample_state(state_dir):
    """Write a sample state file and return (path, data_dict)."""
    data = {
        "timestamp": "2026-06-09T12:00:00+00:00",
        "stream": "dgx",
        "ok": True,
        "details": {"gpu_count": 8, "ssh_reachable": True},
    }
    path = state_dir / "dgx.json"
    path.write_text(json.dumps(data, indent=2))
    return path, data


@pytest.fixture
def agents_json(tmp_hfcc_dir):
    """Create an agents.json with known content. Returns (path, data_dict)."""
    data = {
        "agents": [
            {"id": "agent-001", "model": "auto", "endpoint": "", "status": "idle"},
            {"id": "agent-002", "model": "auto", "endpoint": "", "status": "working"},
        ]
    }
    path = tmp_hfcc_dir / "agents.json"
    path.write_text(json.dumps(data, indent=4))
    return path, data


@pytest.fixture
def workers_json(tmp_hfcc_dir):
    """Create a workers.json with known content. Returns (path, data_dict)."""
    data = {
        "workers": [
            {"node": "10.0.0.246", "status": "online"},
            {"node": "10.0.0.247", "status": "offline"},
        ]
    }
    path = tmp_hfcc_dir / "workers.json"
    path.write_text(json.dumps(data, indent=4))
    return path, data


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Patch subprocess.run to control command output.

    Patches the top-level subprocess module AND all hscc_daemon submodules
    that import subprocess, since they each hold their own reference.

    Returns an object with ``.set_result(**kwargs)`` that overrides the default
    subprocess.run behaviour for the *next* call.

    Example::

        fake_subprocess.set_result(stdout="hello", returncode=0)
        fake_subprocess.set_result(timeout_exc=True)
    """
    import hscc_daemon.util as util_mod
    import hscc_daemon.desktop as desktop_mod
    import hscc_daemon.serving as serving_mod
    import hscc_daemon.health as health_mod
    import hscc_daemon.daemon_ops as daemon_ops_mod
    import hscc_daemon.lifecycle as lifecycle_mod

    fs = _FakeSubprocess(monkeypatch)
    monkeypatch.setattr(subprocess, "run", fs._fake_run)
    for mod in (util_mod, desktop_mod, serving_mod, health_mod, daemon_ops_mod, lifecycle_mod):
        if hasattr(mod, "subprocess"):
            monkeypatch.setattr(mod.subprocess, "run", fs._fake_run)
    return fs
