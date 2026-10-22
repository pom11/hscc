import json
import os, sys

# Put the plugin dir on sys.path so `import clusterlib` resolves without
# importing the plugin package (whose hyphenated dir name isn't importable).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# This tests/ dir too, so suites (and other plugins' conftests) can import the
# shared sparkrun fence by name.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
import clusterlib
import sparkrun_fence


@pytest.fixture(autouse=True)
def _fence_sparkrun_argv(monkeypatch):
    """Fence literal-argv sparkrun calls for EVERY test in this suite.

    LIVE INCIDENT (t_be4b94ca, 2026-10-22): the operator's inference proxy on
    :4000 was SIGTERM'd twice while this suite ran. ``sparkrun proxy stop`` is
    exactly that, and ``hscc-cluster`` reaches it with literal argv —
    ``subprocess.run(["sparkrun", "proxy", "stop"], ...)`` in cluster_template.py
    (:225/:252/:381/:530 area) and ``SPARKRUN = "sparkrun"`` in clusterlib.py:21
    / hscc.py:34 / discovery.py:31 / recipe_cost.py:18 — raw calls that the
    daemon's ``HSCC_SPARKRUN_BIN`` autouse fence (hscc_daemon/tests/conftest.py:
    ``_hermetic_sparkrun_resolution``) cannot see at all. Fencing here was
    per-test (``patch.object(subprocess, "run", ...)``), so any test, helper or
    teardown that reached one of those sites without its own patch exec'd the
    operator's real CLI, which is on PATH on this host.

    This fixture makes that impossible by construction: the wrapper raises
    ``SparkrunFenceViolation`` instead of exec'ing the live CLI. Tests that patch
    ``subprocess.run``/``Popen`` themselves intercept first and are unaffected
    (they never reach exec); tests that want a sparkrun *command* point argv at a
    fake script under ``tmp_path``, which resolves elsewhere.
    """
    sparkrun_fence.install(monkeypatch)


#: argv signatures already surfaced by the guard fixture, so one leak fails
#: once instead of cascading into every later test.
_fence_reported = set()


@pytest.fixture(autouse=True)
def _record_sparkrun_calls():
    """Per-test fence recorder + the session guard's enforcement point.

    After the test (and its other fixtures' teardowns, which run first) no
    call may have been blocked unless a fence test asked for it. Failing HERE
    — a real test failure with traceback — rather than in a session-finish hook
    is deliberate: a blocked call in a fixture or teardown used to reach the
    operator's live proxy silently, and rewriting pytest's exitstatus that
    late is unreliable. Distinct leaks are reported once each so one leak does
    not cascade into 300 failures.
    """
    sparkrun_fence.SEEN.clear()
    yield sparkrun_fence.SEEN
    fresh = []
    for e in sparkrun_fence.unexpected_blocked():
        key = (tuple(e["argv"]), e["why"])
        if key not in _fence_reported:
            _fence_reported.add(key)
            fresh.append(e)
    if fresh:
        raise AssertionError(
            "SPARKRUN FENCE GUARD: a test/fixture tried to exec the operator's "
            f"live sparkrun CLI ({len(fresh)} call(s)). Without the fence this "
            "would have run for real — t_be4b94ca: literal-argv "
            "`sparkrun proxy stop` SIGTERM'd the live :4000 proxy. Offending "
            f"argv: {[e['argv'] for e in fresh]}. Point the test at a fake "
            "script under tmp_path, or patch subprocess.run / "
            "sparkrun_bridge.remote_cmd."
        )


def pytest_terminal_summary(terminalreporter):
    """Say out loud, at the end of every run of this suite, what the fence saw.

    A silent fence is indistinguishable from a suite that never tried: the
    operator's proxy was down for hours before anyone noticed. This line makes
    "no sparkrun-shaped call reached exec" part of the visible output.
    """
    lines = sparkrun_fence.report().splitlines()
    terminalreporter.write_sep("=", lines[0])
    for line in lines[1:]:
        terminalreporter.write_line(line)



@pytest.fixture(autouse=True)
def _pin_serving_json_to_tmp(monkeypatch, tmp_path):
    """Defense-in-depth: redirect cluster_template.SERVING_JSON to a per-test
    tmp file so NO cluster-template test can write fixture data into the
    operator's LIVE ~/.hscc/serving.json (t_501fb7f1 — the fourth live-state
    leak in this project).

    ``_provision_models`` stamps a unit's serve_cmd into SERVING_JSON whenever a
    launch returns 0. Several test helpers call it directly with mock subprocess
    and, historically, did NOT redirect SERVING_JSON — leaking fixture
    serve_cmds (e.g. recipe ~/recipes/orch.yaml → /Users/desac/recipes/orch.yaml,
    hosts 10.0.0.x, port 9000) into the real serving.json. Tests that need a
    SPECIFIC serving.json still override it themselves (monkeypatch.setattr);
    this fixture only guarantees the default is never the live path.
    """
    import cluster_template as ct
    monkeypatch.setattr(ct, "SERVING_JSON", tmp_path / "serving.json")


@pytest.fixture(autouse=True)
def _stub_sparkrun_proxy_write(monkeypatch):
    """Defense-in-depth (same doctrine as _pin_serving_json_to_tmp): NO test may
    drive the real sparkrun proxy.yaml write path.

    ``_apply_proxy_declaration``'s default writer shells into sparkrun's venv
    and merges into ``~/.config/sparkrun/proxy.yaml`` — the operator's LIVE
    balancer config. On this host sparkrun IS on PATH, so an apply-integration
    test on a uniform_pool template would otherwise write the real file. Here the
    default writer is a no-op recorder that reports "already in effect" (changed
    False — no restart_required, apply stays successful); a test that wants a
    different outcome monkeypatches ``ct._sparkrun_proxy_write`` (or passes
    ``_write=``) itself, which runs after this fixture and wins.
    """
    import cluster_template as ct
    calls = []

    def _stub(regions):
        calls.append(regions)
        return {"ok": True, "error": None, "mode": "direct", "changed": False,
                "before": None, "after": None}

    monkeypatch.setattr(ct, "_sparkrun_proxy_write", _stub)
    monkeypatch.setattr(ct, "_SPARKRUN_WRITE_CALLS", calls)


#: clusterlib's topology names are LAZY (module ``__getattr__`` -> discovery).
_TOPOLOGY_NAMES = ("HEAD", "NODES", "NAS_HOST")


#: What `sparkrun cluster list --json` answers for EVERY test in this suite
#: (the sanctioned documentation range, same IPs as _pin_topology above). The
#: real I/O boundary is pinned by _pin_discovery_io below; this is its payload.
FIXTURE_CLUSTER_JSON = json.dumps([{
    "name": "hscc",
    "hosts": ["192.0.2.10", "192.0.2.11", "192.0.2.12", "192.0.2.13"],
    "user": "spark", "cache_dir": "/mnt/nas", "default": True,
}])


@pytest.fixture(autouse=True)
def _pin_discovery_io(monkeypatch, tmp_path):
    """Pin discovery's ENTIRE I/O boundary: no live sparkrun, no live files.

    Same doctrine as `_stub_sparkrun_proxy_write` and the daemon's
    `_hermetic_topology` (t_be4b94ca added this after the sparkrun fence caught
    it): `ops.cluster_status` calls `discovery.discover()` best-effort, and
    discover() shells out to a REAL `sparkrun cluster list --json` and writes
    the operator's `~/.hscc/discovery_cache.json` — on EVERY test that touches
    cluster_status. Here:

      * the three ~/.hscc files discovery reads point at per-test tmp paths,
      * `_run` answers the fixture cluster for `cluster list` and fails anything
        else (so a curl/ssh probe can never reach a node),
      * `_write_cache` is a recorder, never the real file.

    A test that wants different answers patches `discovery._run` itself — its
    monkeypatch runs after this fixture and wins.
    """
    import discovery as d

    calls = []

    def _inert_run(args, timeout=20):
        calls.append(list(args))
        if len(args) >= 3 and args[1:3] == ["cluster", "list"]:
            return {"ok": True, "stdout": FIXTURE_CLUSTER_JSON, "stderr": ""}
        return {"ok": False, "stdout": "", "stderr": "inert test default"}

    monkeypatch.setattr(d, "CLUSTER_JSON", str(tmp_path / "cluster.json"))
    monkeypatch.setattr(d, "DISCOVERY_CACHE", str(tmp_path / "discovery_cache.json"))
    monkeypatch.setattr(d, "SERVING_JSON", str(tmp_path / "serving.json"))
    monkeypatch.setattr(d, "_run", _inert_run)
    monkeypatch.setattr(d, "_write_cache", lambda topo: calls.append(["_write_cache"]))


@pytest.fixture(autouse=True)
def _reset_recipe_cost_caches():
    """recipe_cost keeps two MODULE-LEVEL caches keyed by recipe string
    (`_CACHE`, `_EXISTS_CACHE`). They survive monkeypatched runners, so without
    this a test that injects a fake cost/probe leaks its answer into every later
    test that asks about the same recipe — order-dependent green.
    """
    import recipe_cost as rc
    rc._CACHE.clear()
    rc._EXISTS_CACHE.clear()
    yield
    rc._CACHE.clear()
    rc._EXISTS_CACHE.clear()


@pytest.fixture
def fake_sparkrun_cli(tmp_path):
    """A FAKE sparkrun CLI for tests that must exercise the real argv path.

    The sparkrun fence blocks the operator's live CLI (see sparkrun_fence.py);
    this is the sanctioned way to still run code that builds `sparkrun ...`
    argv: write an executable under tmp_path and point the module's SPARKRUN
    name at it. Records argv per call in `fake.calls`.
    """
    script = tmp_path / "sparkrun"
    binlog = tmp_path / "calls.txt"
    script.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{binlog}'\n"
        "cat \"${SPARKRUN_FAKE_STDOUT:-/dev/null}\"\n"
        "exit ${SPARKRUN_FAKE_RC:-0}\n")
    script.chmod(0o755)
    script.calls = lambda: (binlog.read_text().splitlines()  # noqa: no-live-argv
                            if binlog.exists() else [])
    return script


@pytest.fixture(autouse=True)
def _pin_topology():
    """Pin cluster topology to fixed example IPs so tests are independent of
    the machine's live ~/.hscc/cluster.json (which clusterlib resolves from at
    import). Tests assert against these documentation-range addresses.

    Deliberately NOT ``monkeypatch.setattr(clusterlib, "HEAD", ...)``: setattr
    READS the old value first to build its undo, and reading those names
    triggers clusterlib's lazy ``__getattr__`` -> ``discovery.discover()`` ->
    a REAL ``sparkrun cluster list --json`` (plus a discovery_cache write) on
    EVERY test in this suite. The sparkrun fence (t_be4b94ca) caught it on its
    first run. Writing straight into ``module.__dict__`` shadows ``__getattr__``
    without invoking it, and teardown deletes the pins so the lazy mechanism is
    handed back — same doctrine as hscc_daemon/tests/conftest.py::_hermetic_topology.
    """
    saved = {n: clusterlib.__dict__[n] for n in _TOPOLOGY_NAMES
             if n in clusterlib.__dict__}
    clusterlib.__dict__.update({
        "HEAD": "192.0.2.10",
        "NODES": ["192.0.2.11", "192.0.2.12", "192.0.2.13"],
        "NAS_HOST": "192.0.2.20",
    })
    yield
    for name in _TOPOLOGY_NAMES:
        clusterlib.__dict__.pop(name, None)
    clusterlib.__dict__.update(saved)
