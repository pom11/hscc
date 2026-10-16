"""LAZY cluster-config resolution — audit BROKEN B11 (t_8d2eb400).

The bug: importing ``hscc_daemon.hscc`` (ANY invocation, every ``--help``,
even ``hscc --help`` in a fresh HOME) ran module-level
``resolve_cluster_config()``, which shells ``sparkrun cluster list --json``
whenever ``~/.hscc/cluster.json`` is absent. The CLI help audit canary-proved
it trips on EVERY help probe in a fresh sandbox: printing help executed the
fleet, and fresh/CI environments shelled out implicitly.

These tests run real subprocesses with a fake ``sparkrun`` on PATH that
COUNTS every invocation (that is exactly the canary the audit used — an
in-process monkeypatch cannot prove the import is silent, the import IS the
thing under test). Invariants pinned:

  1. ``import hscc_daemon.serving`` / ``import hscc_daemon.hscc`` and
     ``hscc --help`` shell ZERO times with an empty HOME (B11's repro);
  2. the FIRST real use of the topology shells EXACTLY once, resolves the
     fake default cluster's primary host, and caches (every later read in the
     same process shells ZERO more times);
  3. ``hscc --help`` in a fresh HOME with NO sparkrun anywhere still exits 0
     (the fresh-HOME fragility the same import-time call caused).

Address placeholders only (10.0.0.x documented LAN range, 192.0.2.x doc space).
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Minimal sanitized environment: no HERMES/kanban/delegation vars, no
# CONDA_, and no sparkrun override (the fake on PATH is what we count).
_STRIP = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
          "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_SPARKRUN_BIN",
          "AUDIT_CANARY_LOG")


def _fake_sparkrun(bin_dir: Path, counter: Path, payload: str = "[]"):
    """A sparkrun on PATH that counts invocations and answers with payload.

    Also plants a `timeout` shim: the resolver shells
    ``timeout 2 sparkrun cluster list --json``, and macOS keeps ``timeout`` in
    the Homebrew prefix, which the minimal test PATH excludes. The shim drops
    the duration and execs the real command so the counter sees the sparkrun
    call itself.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / "sparkrun"
    cli.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{counter}"\n'
        f"printf '%s' '{payload}'\n"
        "exit 0\n")
    cli.chmod(0o755)
    tmo = bin_dir / "timeout"
    tmo.write_text("#!/bin/sh\nshift\nexec \"$@\"\n")
    tmo.chmod(0o755)
    return cli


def _env(tmp_path: Path, path_dirs):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CONDA_") and k not in _STRIP}
    env.update({
        "HOME": str(tmp_path / "empty-home"),   # NO ~/.hscc at all
        "PYTHONPATH": str(REPO),
        "COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb",
        "HSCC_THEME": "dark",
        "PATH": os.pathsep.join(str(p) for p in path_dirs)
                + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
    })
    (tmp_path / "empty-home").mkdir(parents=True, exist_ok=True)
    return env


def _run(env, *args, timeout=90):
    return subprocess.run([sys.executable, *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


def _shells(counter: Path):
    if not counter.exists():
        return []
    return [l for l in counter.read_text().splitlines() if l.strip()]


FAKE_CLUSTER = json.dumps(
    [{"name": "hscc", "default": True, "hosts": ["10.0.0.7:22"]}])


class TestImportExecutesNothing:
    """B11's repro: import must not shell out (fresh HOME, fake counted CLI)."""

    def test_import_serving_shells_zero(self, tmp_path):
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        r = _run(env, "-c", "import hscc_daemon.serving")
        assert r.returncode == 0, r.stderr
        assert _shells(counter) == [], (
            "importing hscc_daemon.serving executed the fleet — B11 regression")

    def test_import_cli_shells_zero(self, tmp_path):
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        r = _run(env, "-c", "import hscc_daemon.hscc")
        assert r.returncode == 0, r.stderr
        assert _shells(counter) == [], (
            "importing the CLI executed the fleet — B11 regression")

    def test_cli_help_shells_zero(self, tmp_path):
        """`hscc --help` in a fresh HOME with a counting sparkrun on PATH."""
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        r = _run(env, "-m", "hscc_daemon.hscc", "--help")
        assert r.returncode == 0, r.stderr
        assert "usage" in (r.stdout + r.stderr).lower()
        assert _shells(counter) == [], (
            "--help shelled out to sparkrun — violates 'help executes nothing'")

    def test_public_names_stay_importable(self, tmp_path):
        """Lazy != gone: `from hscc_daemon.serving import <name>` still works
        (it RESOLVES on first read — which is the whole design) and the CLI
        re-export surface is unchanged."""
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        r = _run(env, "-c",
                 "from hscc_daemon.serving import resolve_cluster_config, "
                 "load_serving, PRIMARY_NODE, NAS_HOST, ORCH_NODES; "
                 "from hscc_daemon.hscc import resolve_cluster_config as r2; "
                 "assert callable(resolve_cluster_config) and callable(r2); "
                 "assert isinstance(ORCH_NODES, set); "
                 "print(PRIMARY_NODE, NAS_HOST)")
        assert r.returncode == 0, r.stderr
        # Exactly one shell for the first USE (the reads above), proving the
        # public names still resolve through the lazy path.
        shells = _shells(counter)
        assert len(shells) == 1, f"expected exactly 1 shell, got {shells}"
        assert "cluster list --json" in shells[0]
        assert "10.0.0.7" in r.stdout  # resolved from the fake default cluster


class TestFirstUseShellsOnceAndCaches:
    """A real use resolves EXACTLY once per process, then caches."""

    def test_first_read_shells_once_and_resolves(self, tmp_path):
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        r = _run(env, "-c",
                 "import hscc_daemon.serving as s; print(s.PRIMARY_NODE)")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().endswith("10.0.0.7"), r.stdout
        shells = _shells(counter)
        assert len(shells) == 1, f"first use must shell exactly once: {shells}"
        assert "cluster list --json" in shells[0]

    def test_repeated_reads_do_not_reshell(self, tmp_path):
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        r = _run(env, "-c",
                 "import hscc_daemon.serving as s; "
                 "vals = [s.PRIMARY_NODE, s.NAS_HOST, s.ORCH_NODES, "
                 "s.VLLM_HEALTH_URL, s.VLLM_STOP_CMD, s.KEEPALIVE_NODES, "
                 "s.VLLM_RECIPE, s.PRIMARY_NODE]; "
                 "print(vals[0], vals[3], bool(vals[4]))")
        assert r.returncode == 0, r.stderr
        # 8 reads of 7 distinct names, ONE resolution.
        assert len(_shells(counter)) == 1, (
            "topology re-resolved on later reads — cache broken")
        out = r.stdout.split()
        assert out[0].endswith("10.0.0.7")             # PRIMARY_NODE resolved
        assert out[1] == "http://10.0.0.7:8000/health"  # cmds rebuilt from it
        assert out[2] == "True"

    def test_cluster_json_present_shells_zero(self, tmp_path):
        """With ~/.hscc/cluster.json present, use shells ZERO — the sparkrun
        fallback is only the fallback (unchanged precedence)."""
        counter = tmp_path / "shell-count.log"
        bin_dir = tmp_path / "fakebin"
        _fake_sparkrun(bin_dir, counter, payload=FAKE_CLUSTER)
        env = _env(tmp_path, [bin_dir])
        hscc_dir = tmp_path / "empty-home" / ".hscc"
        hscc_dir.mkdir(parents=True, exist_ok=True)
        (hscc_dir / "cluster.json").write_text(json.dumps({
            "gateway": {"ip": "10.0.0.1"},
            "workers": [{"ip": "10.0.0.2"}],
            "nasDevices": [{"ip": "10.0.0.10"}],
        }))
        r = _run(env, "-c",
                 "import hscc_daemon.serving as s; "
                 "print(s.PRIMARY_NODE, s.NAS_HOST)")
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["10.0.0.1", "10.0.0.10"]
        assert _shells(counter) == []


class TestFreshHomeWithoutSparkrun:
    """The fresh-HOME fragility: no cluster.json AND no sparkrun anywhere —
    the old import-time fallback made every CLI start depend on that shell."""

    def test_help_exits_zero_with_no_sparkrun(self, tmp_path):
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        env = _env(tmp_path, [empty_bin])
        r = _run(env, "-m", "hscc_daemon.hscc", "--help")
        assert r.returncode == 0, (
            f"hscc --help crashed with no cluster.json and no sparkrun: "
            f"rc={r.returncode}\n{r.stderr}")
        assert "usage" in (r.stdout + r.stderr).lower()

    def test_import_survives_sparkrun_less_home(self, tmp_path):
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        env = _env(tmp_path, [empty_bin])
        r = _run(env, "-c",
                 "import hscc_daemon.serving as s; "
                 "assert s.PRIMARY_NODE == '192.0.2.10', s.PRIMARY_NODE; "
                 "print('ok')")
        assert r.returncode == 0, r.stderr
        assert "ok" in r.stdout


class TestLazyMechanismInProcess:
    """The mechanism itself, exercised in-process (hermetic, no subprocess):
    the flags stay consistent and the cache can be handed back to the lazy
    path — that is what the conftest fixture and future reload paths rely on."""

    def test_direct_call_still_resolves_eagerly(self, tmp_path, monkeypatch):
        from hscc_daemon import serving
        serving._materialize_topology_defaults()
        cluster = tmp_path / "cluster.json"
        cluster.write_text(json.dumps({"gateway": {"ip": "10.0.0.4"},
                                       "workers": [], "nasDevices": []}))
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster))
        monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_path / "none.json"))
        serving.resolve_cluster_config()
        assert serving.PRIMARY_NODE == "10.0.0.4"

    def test_reset_hands_names_back_to_lazy_path(self, tmp_path, monkeypatch):
        from hscc_daemon import serving
        serving._materialize_topology_defaults()
        cluster = tmp_path / "cluster.json"
        cluster.write_text(json.dumps({"gateway": {"ip": "10.0.0.8"},
                                       "workers": [], "nasDevices": []}))
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster))
        monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_path / "none.json"))
        serving.reset_resolution_cache()
        # Unresolved again: the first READ must pick up the NEW cluster.json
        # (an eager-at-import design could not; a broken cache could not).
        assert serving.PRIMARY_NODE == "10.0.0.8"

    def test_reads_inside_the_resolver_do_not_recurse(self, tmp_path,
                                                      monkeypatch):
        """A public read performed WHILE the resolver is mid-flight must not
        re-enter it: `_topo_resolving` is set before the body runs, so the
        lazy __getattr__ short-circuits. Instrumented: the body runs exactly
        once even though the read inside it is an ordinary external read."""
        from hscc_daemon import serving
        serving._materialize_topology_defaults()
        serving.reset_resolution_cache()
        cluster = tmp_path / "cluster.json"
        cluster.write_text(json.dumps({"gateway": {"ip": "10.0.0.9"},
                                       "workers": [], "nasDevices": []}))
        monkeypatch.setattr(serving, "CLUSTER_JSON", str(cluster))
        monkeypatch.setattr(serving, "SERVING_JSON", str(tmp_path / "none.json"))

        calls = {"n": 0}
        real_body = serving._resolve_cluster_config_body

        def instrumented():
            calls["n"] += 1
            serving.PRIMARY_NODE        # external read DURING resolution
            serving.VLLM_HEALTH_URL     # (overlay + rebuild path)
            real_body()

        monkeypatch.setattr(serving, "_resolve_cluster_config_body",
                            instrumented)
        assert serving.PRIMARY_NODE == "10.0.0.9"
        assert calls["n"] == 1, (
            f"resolver re-entered {calls['n']}x — recursion guard broken")
        assert serving.VLLM_STOP_CMD[-1] == "10.0.0.9"  # commands rebuilt
