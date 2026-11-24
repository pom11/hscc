"""Sandbox for the "documented read creates no ``$HSCC_HOME/daemon.log``" guards.

The recipe is ``hscc-cluster/tests/test_doctor_tool.py::_seal_door_env`` —
``HOME``/``HERMES_HOME`` redirected to a throwaway tree, a fake ``bin`` first on
``PATH`` whose ``sparkrun``/``curl``/``ssh``/``gh``/``hermes`` stubs record that
they were the ones invoked, planted ``~/.hscc`` state naming only TEST-NET-1
(192.0.2.x) hosts and closed loopback ports, and the uv cache sealed OUTSIDE the
snapshot so "the read wrote nothing" is a claim about HSCC state and not about
uv. Nothing in a sandboxed child can reach the fleet and nothing needs to.

This module builds that environment; it deliberately does NOT hold the
invariant. The assert — "``daemon.log`` did not appear" — lives in each
surface's own suite (the #704 lesson: a surface may not rely on another file
holding its invariant), because a shared guard is a guard one refactor can move
away.

Two things make these guards bite rather than pass vacuously:

* ``.hermes/hermes-agent`` is **absent** in the sandbox and
  ``HERMES_AGENT_PATH`` is deleted, so ``autodown._load_kanban_db_or_default``
  takes its failure branch — the exact leaf that appends the WARN. Each guard
  then asserts the payload NAMED THE SANDBOX PATH, which is only possible if
  the child resolved ``~`` to the sandbox and reached that leaf.
* :func:`snapshot` + :func:`the_seal_actually_took`: the first proves the
  comparison can see a write (canary), the second proves the sandbox is what
  the child read, so a green test can never be reading the operator's live
  ``~/.hscc``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

#: Captured at import, before any fixture runs, so :func:`the_seal_actually_took`
#: can tell a working seal from a leak (after the seal, this process's own
#: ``expanduser`` answers the sandbox and comparing it against itself would pass
#: anywhere — same reason test_doctor_tool.py captures these at module level).
REAL_HOME = os.path.expanduser("~")
REAL_HSCC = os.path.expanduser("~/.hscc")

#: The repo the child must import (never the deployed copy under ~/.hermes).
REPO = Path(__file__).resolve().parents[2]

#: Env that would either point the child back at the operator's tree or let it
#: reach the fleet. ``HERMES_AGENT_PATH`` is the load-bearing one: the leaf these
#: guards exercise is "the Hermes source tree is not there", and an inherited
#: override would make the library reachable again — the guard would then pass
#: while proving nothing.
_DROP = (
    "HERMES_AGENT_PATH", "HSCC_HOME", "HSCC_DIR", "HERMES_REAL_HOME",
    "HSCC_SPARKRUN_BIN", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT",
    "COLUMNS",
)

FAKE_BIN_FILES = {
    # Every stub records the call, so a test can prove the stub answered (and a
    # real transport never ran), then answers in the shape that keeps the read
    # fast and offline.
    "sparkrun": (
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('sparkrun ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('{}')\n"
    ),
    "curl": (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('curl ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.exit(22)\n"
    ),
    "ssh": (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('ssh ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.stderr.write('nolive: ssh refused\\n')\n"
        "sys.exit(255)\n"
    ),
    # ``hscc autodown status`` screens PR/CI with ``gh``. Two reasons to stub it:
    # the real gh answers from the sandbox and creates ``$HOME/.local/state/gh``
    # (measured in docs/audits/read-surface-logging_t_88abe739.md §2) — a write
    # that is nobody's bug but would muddy a "the read wrote nothing" snapshot;
    # and it must answer POSITIVELY CLEAR ("0" for every count), not fail,
    # because the PR/CI fail-safe WARN is deliberately NOT muted by
    # ``autodown.silent_read`` (test_status_json_survives_a_real_gh_blip pins it
    # audible). A failing stub would put a second, legitimately-written WARN in
    # the sandbox and this guard would be guarding the wrong leaf.
    "gh": (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('gh ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('0\\n')\n"
    ),
    "hermes": (
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.path.join(os.environ['FAKE_BIN_LOG']), 'a') as fh:\n"
        "    fh.write('hermes ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "sys.stderr.write('nolive: hermes unavailable\\n')\n"
        "sys.exit(97)\n"
    ),
}


def snapshot(directory):
    """name -> bytes for every file under ``directory`` ({} when absent)."""
    root = Path(directory)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def the_seal_actually_took():
    """Fail loudly if the sandbox is not what the child will read."""
    assert os.path.expanduser("~") != REAL_HOME, (
        "HOME was not redirected — the read would run against the operator's "
        f"live {REAL_HOME}")
    assert os.path.expanduser("~/.hscc") != REAL_HSCC, (
        "the read's ~/.hscc still resolves to the operator's live state dir "
        f"({REAL_HSCC})")


def seal(tmp_path, monkeypatch):
    """Build the sealed sandbox + the env a child CLI process must run with.

    Returns a namespace with ``home``/``hscc``/``hermes``/``bin``/``log``/
    ``cache``/``env``/``cwd``. The sandbox has NO ``.hermes/hermes-agent`` on
    purpose: that absence is what drives the borrowed import seam into the
    branch that writes the WARN this guard forbids.
    """
    home = tmp_path / "fakehome"
    hermes = home / ".hermes"
    (hermes / "plugins").mkdir(parents=True)
    (hermes / "config.yaml").write_text(
        "model:\n  default: worker-model\n"
        "  base_url: http://127.0.0.1:1/v1\n", encoding="utf-8")
    hscc = home / ".hscc"
    hscc.mkdir()
    # TEST-NET-1 + a closed loopback port: an address that cannot be a real
    # host, so no probe can reach the fleet even if a stub were bypassed.
    (hscc / "cluster.json").write_text(json.dumps(
        {"clusters": [{"name": "sandbox", "hosts": ["192.0.2.1"]}]}),
        encoding="utf-8")
    (hscc / "serving.json").write_text(json.dumps(
        {"units": [{"name": "sandbox-unit", "host": "127.0.0.1", "port": 1,
                   "model": "sandbox-model"}]}), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "fake-bin.log"
    log.write_text("", encoding="utf-8")
    for name, body in FAKE_BIN_FILES.items():
        p = bin_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("FAKE_BIN_LOG", str(log))
    monkeypatch.setenv("PATH",
                       f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # uv keeps a cache under $HOME and writes it even when HSCC itself writes
    # nothing, so seal it outside the snapshot: the claim is about HSCC state.
    (tmp_path / "xdg-cache").mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "xdg-cache" / "uv"))
    for var in _DROP:
        monkeypatch.delenv(var, raising=False)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CONDA_") and k not in _DROP}
    env.update({
        "HOME": str(home),
        "HERMES_HOME": str(hermes),
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        "FAKE_BIN_LOG": str(log),
        "XDG_CACHE_HOME": str(tmp_path / "xdg-cache"),
        "UV_CACHE_DIR": str(tmp_path / "xdg-cache" / "uv"),
        # The code under test, not the deployed copy under ~/.hermes/plugins.
        "PYTHONPATH": os.pathsep.join(
            [str(REPO), str(REPO / "hscc-project")]),
        "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "120",
        "HSCC_THEME": "dark",
    })
    for var in _DROP:
        env.pop(var, None)
    return SimpleNamespace(home=home, hscc=hscc, hermes=hermes, bin=bin_dir,
                           log=log, cache=tmp_path / "xdg-cache", env=env,
                           cwd=tmp_path)


def run_read(seal, argv, timeout=240):
    """Run one ``hscc`` read as the operator runs it, inside the sealed sandbox."""
    return subprocess.run(
        [sys.executable, "-m", "hscc_daemon.hscc", *argv],
        env=seal.env, cwd=str(seal.cwd), capture_output=True, text=True,
        timeout=timeout)


def first_json_line(proc):
    """Parse the ``--json`` payload from a door that may also print warnings.

    The machine channel is one JSON document; stderr carries the human warnings.
    Tolerates leading blank lines but never guesses: unparseable stdout is the
    door's failure and the test says so with the raw bytes.
    """
    text = proc.stdout.strip()
    return json.loads(text)
