"""The repo-root ``conftest.py`` collection guard (t_0c4989d7).

WHAT WOULD BREAK WITHOUT IT. Collecting one file from two legs in ONE pytest
process errored during setup — 27 errors, every test in the cluster file::

    ImportError: attempted relative import with no known parent package

The text blames the engineer's own code and names nothing true. The mechanism is
written up in the root ``conftest.py``: passing args from two legs makes rootdir
the repo root, the walk down to ``hscc-cluster/tests/`` passes *through*
``hscc-cluster/``, pytest builds a ``Package`` node for that payload dir (it
holds an ``__init__.py``) and imports it as a module named ``__init__`` — and the
payload's own ``from . import schemas`` has no parent package to resolve against,
because a dir named ``hscc-cluster`` is not an importable package.

Claims, each pinned separately:

  1. the card's repro — cluster file + daemon file in one process — RUNS green
     (not merely collects: the failure was at node *setup*, so ``--collect-only``
     was always clean and proved nothing), in both argument orders;
  2. neither leg regressed alone — the shape ``scripts/run_tests.sh`` runs them
     in, and the shape CI gates on. The guard must not become a way to break the
     shipped lanes;
  3. the node SHAPES are right in both directions (``fixtures/node_shape_plugin.py``):
     the hyphenated payload is collected as ``Dir``, and ``hscc_daemon`` is still
     collected as ``Package``. Claim 1 alone would also be satisfied by a guard
     that de-packaged the whole repo — which would quietly change how the daemon
     lane imports its own tests, so it is pinned explicitly.

Why a subprocess for 1-3: the failure is a rootdir/collection artifact, so it
only exists in a real pytest process started the way an engineer starts it (from
the repo root, two paths as args). In-process collection under this leg's own
config could not produce it. ``-p no:cacheprovider`` keeps the child from writing
``.pytest_cache`` into the checkout.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The card's pair: a cluster test and the manifest-drift test — the two files
#: an engineer adding a cluster tool actually wants to run together.
CLUSTER_FILE = "hscc-cluster/tests/test_ops.py"
DAEMON_FILE = "hscc_daemon/tests/test_plugin_manifest_drift.py"

#: The exact string the bug produced. Pinned separately from the exit code, so a
#: future failure that is NOT this one still reads as itself.
CRYPTIC = "attempted relative import with no known parent package"


def _run_pytest(*files: str, node_shape: str | None = None,
                timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a real child pytest the way an engineer does: from the repo root."""
    env = dict(os.environ)
    if node_shape:
        env["HSCC_NODE_SHAPE_JSON"] = node_shape
        # The plugin lives beside this file, not in an importable package.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(FIXTURES), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        files = files + ("-p", "node_shape_plugin")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *files, "-p", "no:cacheprovider"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout,
    )


def _tail(proc: subprocess.CompletedProcess, lines: int = 40) -> str:
    return "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-lines:])


@pytest.mark.parametrize("files", [
    (CLUSTER_FILE, DAEMON_FILE),
    (DAEMON_FILE, CLUSTER_FILE),
], ids=["cluster-first", "daemon-first"])
def test_two_legs_in_one_process_run(files):
    """The repro: two legs in one process collect AND run, no import error."""
    proc = _run_pytest(*files)
    assert CRYPTIC not in proc.stdout + proc.stderr, (
        "a hyphenated plugin payload dir was imported as a Python package again "
        "— the repo-root conftest's pytest_collect_directory guard is not "
        "taking effect for this invocation:\n" + _tail(proc)
    )
    assert proc.returncode == 0, "cross-leg run did not pass:\n" + _tail(proc)
    assert " passed" in proc.stdout, (
        "the cross-leg run exited 0 but reported no tests — a collection that "
        "silently found nothing would keep this guard green for free:\n"
        + _tail(proc)
    )


@pytest.mark.parametrize("solo", [CLUSTER_FILE, DAEMON_FILE],
                         ids=["cluster-alone", "daemon-alone"])
def test_leg_still_green_alone(solo):
    """Each leg on its own is unchanged — the shape run_tests.sh runs."""
    proc = _run_pytest(solo)
    assert proc.returncode == 0, f"{solo} alone did not pass:\n" + _tail(proc)


def test_daemon_conftest_import_survives_a_host_without_kqueue():
    """The daemon conftest must import where ``select.kqueue`` does not exist.

    GitHub runs this leg on Linux, and the child pytest above collects
    ``hscc_daemon/tests``, whose autouse conftest imports ``event_driven``.
    That module exposes a ``select.kqueue`` annotation; Python 3.13 (CI's
    interpreter) evaluates annotations at definition time, so on Linux the
    child died during collection before a single test ran. Delete the
    attribute to reproduce a Linux ``select`` from macOS, then import the
    module the conftest imports.
    """
    code = (
        "import select\n"
        "if hasattr(select, 'kqueue'):\n"
        "    del select.kqueue\n"
        "from hscc_daemon import event_driven\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, (
        "hscc_daemon.event_driven no longer imports on a host without "
        "select.kqueue (i.e. Linux), so the daemon conftest turns the "
        "cross-leg run red during collection:\n" + _tail(proc))


def test_payload_dir_is_a_dir_and_the_real_package_is_still_a_package(
        tmp_path):
    """Both directions of the guard's scope, read off the child's own tree.

    ``Package:hscc-cluster`` is the bug (pytest importing a payload's
    ``__init__.py`` as module ``__init__``). ``Package:hscc_daemon`` is the
    thing the guard must NOT touch: a validly-named package whose lane relies on
    package-scoped collection.
    """
    shape_file = tmp_path / "node_shape.json"
    proc = _run_pytest(CLUSTER_FILE, DAEMON_FILE, node_shape=str(shape_file))
    assert proc.returncode == 0, "instrumented run failed:\n" + _tail(proc)

    records = [json.loads(line) for line in
               shape_file.read_text(encoding="utf-8").splitlines()]
    assert records, "the plugin recorded no collection chains — the run collected nothing"

    cluster = [r["chain"] for r in records
               if r["nodeid"].startswith(CLUSTER_FILE + "::")]
    daemon = [r["chain"] for r in records
              if r["nodeid"].startswith(DAEMON_FILE + "::")]
    assert cluster and daemon, (
        f"expected items from both legs, got {len(cluster)}/{len(daemon)}:\n"
        + "\n".join(r["nodeid"] for r in records[:5])
    )

    for chain in cluster:
        assert "Package:hscc-cluster" not in chain, (
            f"hscc-cluster was collected as a Python package again — this is "
            f"the t_0c4989d7 failure: {chain}"
        )
        assert "Dir:hscc-cluster" in chain, (
            f"hscc-cluster is missing from the cluster items' own ancestor "
            f"chain — collection stopped descending into it: {chain}"
        )
    for chain in daemon:
        assert "Package:hscc_daemon" in chain, (
            "hscc_daemon is no longer collected as a Package: the guard widened "
            "past non-importable dir names and changed how the shipped daemon "
            f"lane imports its own tests: {chain}"
        )
