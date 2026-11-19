"""Bootstrap calls `hscc main start` at the end (M4, t_d6dee427).

The shipped function is extracted verbatim from ``bootstrap.sh`` and run in a
real bash against throwaway dirs, so the test exercises the actual control
flow: --dry-run plans, --skip-roles skips, no tmux server prints the fix, and a
real server invokes exactly ``main start`` (through the installed shim or the
deployed daemon module). Nothing here reaches the operator's tmux server or
``~/.hscc``.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BOOT = HERE.parent / "bootstrap.sh"
SRC = BOOT.read_text()

_HEAD = "start_main_orchestrator() {"


def _block() -> str:
    start = SRC.index(_HEAD)
    end = SRC.index("\n}\n", start) + 3
    return SRC[start:end]


BLOCK = _block()


def _run(tmp_path, *, skip_roles=False, dry_run=False, tmux=True,
         server=True, shim=True, path_shim=False, rc=0,
         output="main orchestrator: ops started"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    binp = tmp_path / "bin"
    binp.mkdir()
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    calls = tmp_path / "calls.log"

    if tmux:
        t = binp / "tmux"
        t.write_text("#!/bin/sh\n"
                     'echo "$@" >> "$TMUX_CALL_LOG"\n'
                     f'exit {0 if server else 1}\n')
        t.chmod(0o755)

    argv_log = tmp_path / "argv.log"
    body = ("#!/bin/sh\n"
            'printf "%s\\n" "$*" >> "$ARGV_LOG"\n'
            f'printf "%s\\n" "{output}"\n'
            f"exit {rc}\n")
    if shim:
        h = venv / "hscc"
        h.write_text(body)
        h.chmod(0o755)
    if path_shim:
        h = binp / "hscc"
        h.write_text(body)
        h.chmod(0o755)
    pybin = venv / "python"
    pybin.write_text(body)
    pybin.chmod(0o755)

    env = {k: v for k, v in os.environ.items()
           if k not in ("HSCC_DIR", "HERMES_HOME", "PYTHONPATH", "TMUX")}
    env.update({
        "PYBIN": str(pybin),
        "PLUGINS": str(tmp_path / "plugins"),
        "HSCC_DIR": str(tmp_path / "hscc"),
        "HERMES_HOME": str(tmp_path / "hermes"),
        "MAIN_ORCH_NAME": "ops",
        "PATH": f"{binp}:/usr/bin:/bin",
        "ARGV_LOG": str(argv_log),
        "TMUX_CALL_LOG": str(calls),
    })
    script = (
        "set -u\n"
        f'SKIP_ROLES={str(skip_roles).lower()}\n'
        f'DRY_RUN={str(dry_run).lower()}\n'
        'say()  { echo "  $1"; }\n'
        'ok()   { echo "  ✓ $1"; }\n'
        'warn() { echo "  ⚠ $1"; }\n'
        'plan() { echo "  → plan: $1"; }\n'
        f"{BLOCK}\n"
        "start_main_orchestrator\n"
    )
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, timeout=30)
    return p, argv_log, calls


def test_dry_run_plans_and_invokes_nothing(tmp_path):
    p, argv, _ = _run(tmp_path, dry_run=True)
    assert p.returncode == 0, p.stderr
    assert "→ plan: main orchestrator pane: hscc main start" in p.stdout
    assert "window named ops" in p.stdout
    assert not argv.exists(), "dry run must not run the CLI"


def test_skip_roles_skips_the_pane(tmp_path):
    p, argv, _ = _run(tmp_path, skip_roles=True)
    assert p.returncode == 0, p.stderr
    assert "skipped (--skip-roles)" in p.stdout
    assert not argv.exists()


def test_no_tmux_server_prints_how_to_start_it(tmp_path):
    p, argv, _ = _run(tmp_path, tmux=False)
    assert p.returncode == 0, p.stderr
    assert "no tmux server" in p.stdout
    assert "start it later: hscc main start" in p.stdout
    assert not argv.exists(), "bootstrap must not start a tmux server"

    p, argv, _ = _run(tmp_path / "down", tmux=True, server=False)
    assert p.returncode == 0, p.stderr
    assert "no tmux server" in p.stdout
    assert not argv.exists()


def test_installed_shim_is_called_with_main_start(tmp_path):
    p, argv, _ = _run(tmp_path, shim=True)
    assert p.returncode == 0, p.stderr
    assert argv.read_text().splitlines() == ["main start"]
    assert "✓ main orchestrator pane: main orchestrator: ops started" in p.stdout


def test_module_fallback_calls_the_deployed_cli(tmp_path):
    p, argv, _ = _run(tmp_path, shim=False)
    assert p.returncode == 0, p.stderr
    line = argv.read_text().strip()
    assert line == "-m hscc_daemon.hscc main start", line


def test_path_shim_is_used_when_the_venv_has_none(tmp_path):
    p, argv, _ = _run(tmp_path, shim=False, path_shim=True)
    assert p.returncode == 0, p.stderr
    assert argv.read_text().splitlines() == ["main start"]


def test_failure_warns_with_the_one_line_fix(tmp_path):
    p, argv, _ = _run(tmp_path, rc=1, output="error: 2 tmux panes")
    assert p.returncode == 0, "bootstrap must not die when the pane cannot start"
    assert "main orchestrator pane not started: error: 2 tmux panes" in p.stdout
    assert "fix: hscc main start" in p.stdout


def test_stage_sits_at_the_end_after_the_daemon():
    stage = SRC.index('hdr "Install: main orchestrator pane"')
    assert SRC.index('hdr "Install: daemon"') < stage
    assert stage < SRC.index('hdr "Done"')
    assert SRC[stage:stage + 300].count("start_main_orchestrator") == 1
