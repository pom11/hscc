"""t_8bf34130 — the compaction probe must refuse in a door a test SPAWNED.

``_probe_compaction_endpoint()`` is a real ``urlopen(timeout=5)`` against the
configured fleet compaction endpoint, and ``enable()`` calls it at the end of every
wire-up. Its only seal was the ``HSCC_SKIP_COMPACT_PROBE`` opt-out plus a print gate
that read ``"pytest" in sys.modules``. Both are process-local in the way that
matters: the opt-out is an env var a caller may simply not have (only
``scripts/run_tests.sh`` and ``hscc-bootstrap/tests/conftest.py`` set it), and the
``sys.modules`` read sees nothing in a child — which is the process the suite
actually spawns. The door ``doctor_tools.doctor_findings`` execs is exactly such a
child, and ``doctor.py``'s reconcile row calls ``enable()`` from inside it.

Measured before this fix, with planted HOME/HERMES_HOME and the opt-out absent from
the child env: the marked child (``PYTEST_CURRENT_TEST`` set, no pytest imported)
opened ``http://192.0.2.1:9/v1/models`` TWICE — once per direct call, once through
``enable()`` — and returned the "model present" verdict ``True``. Nothing failed;
the suite read the operator's live orch and got a free pass.

Both directions, same door, same planted HOME, same interpreter, the marker the only
difference (the shape #732/#761 established, because a control is what stops a
refusal from guarding a branch nothing can reach):

* marked child — the probe refuses, names the seam, and the live transport was
  reached ZERO times;
* unmarked child — the same door takes the ambient path: the transport is reached
  and the request really is aimed at TEST-NET-1.

The child reports WHICH BRANCH IT TOOK — a call count on the live seam plus the URLs
it saw — rather than whether anything was logged. The transport is counted by
patching ``urllib.request.urlopen`` INSIDE the child, never by installing a spy on
``sys.modules[...]`` from here: the door is loaded with ``importlib`` so the module
object the child counts on is the one it calls (a ``runpy.run_path`` namespace would
make every count read 0 in the control half, which is how a guard of this shape
passes while measuring nothing).

Hermetic by construction: planted HOME/HERMES_HOME only (no agent tree), a state
naming TEST-NET-1 (192.0.2.1) on a closed port, and a ``urlopen`` that never leaves
the process. The child echoes HOME/HERMES_HOME back so a broken seal fails here
instead of reading live state in silence.
"""
import json
import os
import subprocess
import sys

import enable_plugins

_BOOT = os.path.dirname(os.path.abspath(enable_plugins.__file__))
_MARKER = "PYTEST_CURRENT_TEST"

#: The phrase the repo's refusal convention always carries (``doctor_rows``,
#: ``_refused_launchd_status``): it names the fence AND the seam. Matched exactly
#: because a probe error's own text can contain the bare word ``refused`` —
#: ``<urlopen error [Errno 61] Connection refused>`` does.
REFUSAL = "refused inside a pytest process"

#: The seam the fence must name, so a reader knows what to inject.
SEAM = "_probe_compaction_endpoint(_fetch="

#: Where the probe is aimed in both halves: TEST-NET-1, and a port nothing listens
#: on. The control half must NOT reach it for real — see _CHILD's urlopen.
COMPACT_URL = "http://192.0.2.1:9/v1"
COMPACT_MODELS_URL = "http://192.0.2.1:9/v1/models"

_CHILD = r"""
import importlib.util, io, json, os, sys
from contextlib import redirect_stderr

sys.path.insert(0, os.environ["HSCC_BOOT_DIR"])
_spec = importlib.util.spec_from_file_location(
    "hscc_probe_door", os.path.join(os.environ["HSCC_BOOT_DIR"], "enable_plugins.py"))
mod = importlib.util.module_from_spec(_spec)
sys.modules["hscc_probe_door"] = mod
_spec.loader.exec_module(mod)

# The live seam, counted INSIDE the child: the door returns its own numbers, so a
# spy installed from the parent on a module object it never loaded cannot silently
# report 0. It answers with the model present, so the ONLY thing that can stop the
# socket is the fence.
calls = []


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _spy(req, timeout=None):
    calls.append((getattr(req, "full_url", str(req)), timeout))
    return _Resp(json.dumps({"data": [{"id": "orchestrator-model"}]}).encode())


import urllib.request
urllib.request.urlopen = _spy

mod.COMPACT_URL = os.environ["HSCC_TEST_COMPACT_URL"]
mod.COMPACT_MODEL = "orchestrator-model"
mod.HOOKS_DIR = os.path.join(os.environ["HSCC_SANDBOX"], "hooks")
mod.CLUSTER_GUARD_DST = os.path.join(mod.HOOKS_DIR, "cluster-guard.py")

err = io.StringIO()
with redirect_stderr(err):
    direct = mod._probe_compaction_endpoint()
n_direct = len(calls)

cfg = os.path.join(os.environ["HSCC_SANDBOX"], "config.yaml")
with open(cfg, "w") as fh:
    fh.write("plugins:\n  enabled: []\ntoolsets: []\n")
with redirect_stderr(err):
    mod.enable(cfg)

print(json.dumps({
    "home": os.environ.get("HOME"),
    "hermes_home": os.environ.get("HERMES_HOME"),
    "marker": os.environ.get("PYTEST_CURRENT_TEST", ""),
    "pytest_imported": "pytest" in sys.modules,
    "skip_optout_env": os.environ.get("HSCC_SKIP_COMPACT_PROBE"),
    "sockets_direct": n_direct,
    "sockets_total": len(calls),
    "urls": sorted({u for u, _ in calls}),
    "direct_return": direct,
    "stderr": err.getvalue(),
}))
"""


def _spawn(tmp_path, *, marked, extra_env=None):
    """Run the door in a fresh interpreter; return what it reported."""
    sandbox = tmp_path / ("marked" if marked else "unmarked")
    (sandbox / "hooks").mkdir(parents=True, exist_ok=True)
    door = tmp_path / "probe_door.py"
    door.write_text(_CHILD)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(sandbox),
        "HERMES_HOME": str(sandbox / "hermes-home"),
        "HERMES_KANBAN_HOME": str(sandbox / "kanban"),
        "PYTHONPATH": "",
        "HSCC_BOOT_DIR": _BOOT,
        "HSCC_SANDBOX": str(sandbox),
        "HSCC_TEST_COMPACT_URL": COMPACT_URL,
    }
    # The runner's seal is DELIBERATELY absent: this is the path where nothing but
    # the in-code fence stands between a child and the socket.
    env.pop("HSCC_SKIP_COMPACT_PROBE", None)
    if marked:
        env[_MARKER] = "tests/test_compaction_probe_fence.py::test_item (call)"
    env.update(extra_env or {})

    p = subprocess.run([sys.executable, str(door)], capture_output=True,
                       text=True, env=env, cwd=str(sandbox), timeout=120)
    assert p.returncode == 0, f"door died rc={p.returncode}\n{p.stderr[-2000:]}"
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── the spawned halves ───────────────────────────────────────────────────────

def test_spawned_marked_door_refuses_and_opens_no_socket(tmp_path):
    """The ruling's NO, pinned: a child of a pytest item dials nothing out."""
    got = _spawn(tmp_path, marked=True)

    assert got["marker"], "the child must carry the marker it is being fenced for"
    assert got["pytest_imported"] is False, (
        "the point of this half is that pytest was never imported here; if it "
        "were, this file would be re-testing the process-local fence")
    assert got["skip_optout_env"] is None, (
        "the opt-out must be absent, or this half proves nothing about the fence")
    assert got["sockets_direct"] == 0, (
        f"the fence let the probe open {got['sockets_direct']} socket(s) to "
        f"{got['urls']} from a marked child")
    assert got["sockets_total"] == 0, (
        f"enable() probed the endpoint from a marked child: {got['urls']}")
    assert REFUSAL in got["stderr"], (
        "a refusal that prints nothing is the silent pass this fence exists to "
        f"prevent; stderr was: {got['stderr'][:300]!r}")
    assert SEAM in got["stderr"], (
        "the refusal must name what to inject, or a reader cannot fix the rig")
    # A refusal is not "the model is missing": the verdict stays non-alarming.
    assert got["direct_return"] is True
    assert got["home"] and str(tmp_path) in got["home"], "HOME must stay planted"


def test_spawned_unmarked_door_still_probes(tmp_path):
    """The control: the branch the fence guards is one the operator really takes.

    Without the marker the same door must reach the live transport, at the same URL,
    with the same 5 s budget. If this ever reports 0, the fenced half above is
    guarding a dead branch and its green is worth nothing.
    """
    got = _spawn(tmp_path, marked=False)

    assert got["marker"] == ""
    assert got["skip_optout_env"] is None
    assert got["sockets_direct"] == 1, f"ambient probe did not run: {got}"
    assert got["sockets_total"] == 2, (
        "the ambient path probes once directly and once via enable(); a drop here "
        f"means bootstrap stopped probing: {got['urls']}")
    assert got["urls"] == [COMPACT_MODELS_URL]
    assert got["direct_return"] is True


# ── the same fence inside the pytest process itself ──────────────────────────
# The door above proves the fence is CHILD-visible; these prove it did not become
# child-ONLY (the in-process case is what a rig that forgot to stub hits first).

def test_in_process_marked_probe_refuses_without_a_seam(monkeypatch, capsys):
    calls = []

    def _never(*a, **kw):
        calls.append(a)
        raise AssertionError("the fence let an unstubbed probe touch the network")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _never)
    monkeypatch.delenv("HSCC_SKIP_COMPACT_PROBE", raising=False)
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", COMPACT_URL)

    assert enable_plugins._probe_compaction_endpoint() is True
    assert calls == []
    err = capsys.readouterr().err
    assert REFUSAL in err and SEAM in err


def test_in_process_marked_probe_runs_when_the_transport_is_injected(monkeypatch):
    """An injected transport is ownership, not a leak: the fence must not eat it.

    Without this case the fence could satisfy the whole card by refusing EVERYTHING,
    and every rig that legitimately drives the probe would be quietly dead.
    """
    seen = []
    monkeypatch.delenv("HSCC_SKIP_COMPACT_PROBE", raising=False)
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", COMPACT_URL)
    monkeypatch.setattr(enable_plugins, "COMPACT_MODEL", "orchestrator-model")

    ok = enable_plugins._probe_compaction_endpoint(
        _fetch=lambda url: seen.append(url) or {"data": [{"id": "orchestrator-model"}]})

    assert ok is True
    assert seen == [COMPACT_MODELS_URL]


def test_in_process_marked_probe_still_refuses_when_opted_out_silently(monkeypatch,
                                                                       capsys):
    """The opt-out stays silent: the fence must not make the runner's seal noisy.

    ``enable()`` runs ~66 times per leg with the opt-out set; a refusal printed on
    each would bury the real warnings this file's console output exists for.
    """
    calls = []

    def _never(*a, **kw):
        calls.append(a)
        raise AssertionError("opted-out probe touched the network")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _never)
    monkeypatch.setenv("HSCC_SKIP_COMPACT_PROBE", "1")
    monkeypatch.setattr(enable_plugins, "COMPACT_URL", COMPACT_URL)

    assert enable_plugins._probe_compaction_endpoint() is True
    assert calls == []
    assert REFUSAL not in capsys.readouterr().err


# ── the predicate itself must stay child-visible, not re-derived ─────────────

def test_fence_asks_the_one_predicate_and_not_a_process_local_copy():
    """No new copy of the "am I under pytest?" rule may appear in this file's lane.

    The fence calls ``doctor_rows.under_pytest()``; a future edit back to
    ``"pytest" in sys.modules`` would read as green in this process while guarding
    nothing in the child the card was written about.
    """
    src = open(os.path.join(_BOOT, "enable_plugins.py")).read()
    probe = src[src.index("def _probe_compaction_endpoint("):]
    probe = probe[:probe.index("\ndef ")]
    assert "doctor_rows.under_pytest()" in probe
    assert '"pytest" in sys.modules' not in probe
