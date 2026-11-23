"""Bootstrap entry points refuse to touch live state before parsing is settled.

The incident behind t_77db1513: ``migrate_systemd_unit_name.py --help`` had no
argparse, so the flag was ignored and a LIVE fleet migration ran. These tests
execute the REAL scripts as subprocesses with fake fleet/power tools first on
PATH. Every fake writes a marker if invoked, and HOME/HERMES_HOME/HSCC_HOME/
PLUGINS/SCRIPTS point inside the test's tmp tree, so a regression cannot reach
the operator's fleet or homes.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent

#: Scripts where `--help`/a typo must be decided by argparse, before any work.
PARSED_ONLY = [
    "migrate_systemd_unit_name.py",
    "install_autoreboot.py",
    "install_gpu_clock_cap.py",
    "install_notifier_mask.py",
    "install_payload.py",
    "install_scripts.py",
    "preserve_autodown.py",
    "install_hooks.py",
    "install_triggers.py",
    "ensure_review_feature.py",
]

#: Scripts whose no-argument invocation must ALSO print usage instead of probing.
NO_ARGS_USAGE = [
    "migrate_systemd_unit_name.py",
    "install_autoreboot.py",
    "install_gpu_clock_cap.py",
    "install_notifier_mask.py",
]

TOOLS = ("ssh", "sparkrun", "scp", "rsync", "sudo", "systemctl", "launchctl",
         "nvidia-smi", "hermes", "git")


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "tool-called"
    for tool in TOOLS:
        p = bin_dir / tool
        p.write_text(f"#!/bin/sh\necho '{tool} $*' >> '{marker}'\nexit 99\n")
        p.chmod(0o755)
    home = tmp_path / "home"
    for d in ("hermes", "hscc", "plugins", "scripts"):
        (home / d).mkdir(parents=True)
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        "HOME": str(home),
        "HERMES_HOME": str(home / "hermes"),
        "HSCC_HOME": str(home / "hscc"),
        "PLUGINS": str(home / "plugins"),
        "SCRIPTS": str(home / "scripts"),
        "REPO_ROOT": str(BOOT.parent),
    })
    return env, marker


def _run(script, args, env):
    return subprocess.run([sys.executable, str(BOOT / script), *args], env=env,
                          capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize("script", PARSED_ONLY)
def test_help_exits_zero_and_never_calls_a_tool(script, sandbox):
    env, marker = sandbox
    res = _run(script, ["--help"], env)
    assert res.returncode == 0, (script, res.stdout, res.stderr)
    assert "usage:" in res.stdout
    assert not marker.exists(), f"{script} --help invoked: {marker.read_text()}"


@pytest.mark.parametrize("script", PARSED_ONLY)
def test_unknown_flag_exits_two_and_never_calls_a_tool(script, sandbox):
    env, marker = sandbox
    res = _run(script, ["--definitely-not-a-flag"], env)
    assert res.returncode == 2, (script, res.stdout, res.stderr)
    assert "usage:" in res.stderr
    assert not marker.exists(), f"{script} typo invoked: {marker.read_text()}"


@pytest.mark.parametrize("script", NO_ARGS_USAGE)
def test_no_args_prints_usage_and_never_calls_a_tool(script, sandbox):
    env, marker = sandbox
    res = _run(script, [], env)
    assert res.returncode == 2, (script, res.stdout, res.stderr)
    assert "usage:" in res.stdout
    assert not marker.exists(), f"{script} no-args invoked: {marker.read_text()}"
