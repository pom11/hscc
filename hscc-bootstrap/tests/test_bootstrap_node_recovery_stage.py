"""The `Install: node hardware watchdog` stage (card t_ebbe38d1).

What is proven here, and why each half is done the way it is:

  * DYNAMIC (`--dry-run` only): a dry-run must PRINT the per-node plan and run
    node_recovery.py NOT AT ALL. Dry-run performs no writes anywhere, so it is
    safe to execute — and it is the one path where executing the installer
    proves the contract.
  * STATIC (real-run paths): a real run of bootstrap.sh resolves the real
    Hermes venv and a real `sparkrun` on PATH, so it WOULD arm the operator's
    live fleet over ssh. A sandboxed HOME does not contain a remote root
    escalation. So the apply/skip wiring is asserted against the script text
    instead of being executed — the same posture test_orch_all_wiring.py takes
    for bash wiring, and the reason --skip-node-recovery belongs in
    test_bootstrap_isolated's skip set.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO / "hscc-bootstrap" / "bootstrap.sh"
REAL_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
REAL_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"

needs_runtime = pytest.mark.skipif(
    not REAL_VENV_PY.is_file(),
    reason="needs the real Hermes venv interpreter to run the installer")

TREE_DIRS = ("hscc-bootstrap", "hscc_daemon", "hscc-cli", "hscc-cluster",
             "hscc-roles", "hscc-skills")

FAKE_CLUSTER = ('[{"name": "fabricated", "hosts": ["10.0.0.1", "10.0.0.2"],'
                ' "user": "op", "cache_dir": "", "default": true}]')

SRC = BOOTSTRAP.read_text()


# ── static wiring (never executes a remote escalation) ─────────────────────

def _stage_body():
    m = re.search(r'hdr "Install: node hardware watchdog.*?\n(hdr "Install: hermes)',
                  SRC, re.S)
    assert m, "the watchdog stage is not present between the expected headers"
    return m.group(0)


def test_stage_exists_and_is_registered_in_flag_parsing():
    _stage_body()  # present, and terminated by the next header
    assert "--skip-node-recovery) SKIP_NODE_RECOVERY=true ;;" in SRC
    help_line = [ln for ln in SRC.splitlines() if "--help|-h)" in ln]
    assert help_line and "--skip-node-recovery" in help_line[0], \
        "the flag must be advertised in bootstrap --help"


def test_stage_runs_the_deployed_module_not_a_second_implementation():
    """One implementation: the stage calls the deployed node_recovery.py."""
    body = _stage_body()
    assert "hscc-cluster/node_recovery.py" in body
    assert "ssh " not in body, "bootstrap must not hand-roll ssh (sparkrun owns the fleet)"


def test_stage_passes_hosts_as_argv_not_interpolated_shell():
    body = _stage_body()
    assert re.search(r'--hosts "\$HOSTS_CSV"', body), \
        "hosts must pass as one quoted argv value"
    assert "eval" not in body


def test_stage_is_non_fatal_but_never_silent():
    """A node that cannot be armed must WARN loudly, and must not be reported
    as protected — the failure mode that matters is a false green."""
    body = _stage_body()
    assert "warn" in body, "stage must degrade to a warning, not die"
    assert "NOT armed on every node" in body
    assert "die " not in body, "a per-node fault must not abort the install"


def test_stage_respects_both_opt_outs():
    body = _stage_body()
    assert "$SKIP_NODE_RECOVERY" in body
    assert "HSCC_BOOTSTRAP_SKIP_NODE_RECOVERY" in body, \
        "an env guard is what keeps a sandboxed harness off the live fleet"


def test_dry_run_branch_only_plans():
    body = _stage_body()
    dry = re.search(r"elif \$DRY_RUN; then\n(.*?)\nelif", body, re.S)
    assert dry, "no dry-run branch in the stage"
    assert "plan " in dry.group(1)
    assert "$PYBIN" not in dry.group(1), \
        "dry-run must not EXECUTE the module at all (no interpreter call)"
    assert "NOTHING" in dry.group(1)


def test_missing_module_is_reported_not_treated_as_success():
    body = _stage_body()
    assert "node_recovery.py missing" in body


# ── dynamic: dry-run only (performs no writes by contract) ─────────────────

def _make_tree(tmp_path):
    root = tmp_path / "opt" / "libexec" / "hscc-9.9.9"
    for d in TREE_DIRS:
        shutil.copytree(REPO / d, root / d,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    (root / ".hscc-brew-libexec").write_text("fabricated\n")
    (root / "VERSION").write_text("9.9.9\n")
    return root


def _make_home(tmp_path):
    home = tmp_path / "home"
    hermes = home / ".hermes"
    (hermes / "hermes-agent").mkdir(parents=True)
    (hermes / "hermes-agent" / "venv").symlink_to(REAL_HERMES_AGENT / "venv")
    (home / ".hscc").mkdir()
    binp = home / "bin"
    binp.mkdir()
    stub = binp / "sparkrun"
    stub.write_text("#!/bin/sh\n"
                    'if [ "$1" = cluster ] && [ "$2" = list ]; then\n'
                    f"  printf '%s' '{FAKE_CLUSTER}'\n"
                    "  exit 0\n"
                    "fi\nexit 0\n")
    stub.chmod(0o755)
    return {"home": home, "hermes": hermes, "hscc": home / ".hscc", "bin": binp}


def _run_dry(tree, sandbox, extra=(), env_extra=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("HERMES_HOME", "HSCC_DIR", "HERMES_SCRIPTS", "RECIPES_DIR",
                        "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE",
                        "HERMES_DELEGATED_CHILD_CONTEXT", "HSCC_BOOTSTRAP_SH",
                        "HSCC_BOOTSTRAP_SKIP_NODE_RECOVERY")}
    env.update({"HERMES_HOME": str(sandbox["hermes"]),
                "HSCC_DIR": str(sandbox["hscc"]),
                "HERMES_SCRIPTS": str(sandbox["home"] / "scripts"),
                "RECIPES_DIR": str(sandbox["home"] / "recipes"),
                "PATH": f'{sandbox["bin"]}:/usr/bin:/bin'})
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(tree / "hscc-bootstrap" / "bootstrap.sh"), "--yes", "--dry-run", *extra],
        capture_output=True, text=True, env=env, timeout=300,
        cwd=str(sandbox["home"]))


def _snapshot(root: Path):
    out = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            out.add(str(Path(dirpath, name).relative_to(root)))
    return out


@needs_runtime
def test_dry_run_plans_the_watchdog_and_names_every_node(tmp_path):
    tree = _make_tree(tmp_path)
    sandbox = _make_home(tmp_path)
    r = _run_dry(tree, sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    plan = [ln for ln in out.splitlines()
            if "→ plan:" in ln and "node hardware watchdog" in ln]
    assert plan, f"no watchdog plan line:\n{out[-3000:]}"
    assert "10.0.0.1,10.0.0.2" in plan[0], plan[0]
    assert "NOTHING" in plan[0] and "daemon-reexec" in plan[0]


@needs_runtime
def test_dry_run_writes_nothing_including_no_watchdog_state(tmp_path):
    tree = _make_tree(tmp_path)
    sandbox = _make_home(tmp_path)
    before_h, before_s = _snapshot(sandbox["hermes"]), _snapshot(sandbox["hscc"])
    r = _run_dry(tree, sandbox)
    assert r.returncode == 0
    assert _snapshot(sandbox["hermes"]) == before_h
    assert _snapshot(sandbox["hscc"]) == before_s


@needs_runtime
def test_dry_run_never_claims_a_node_was_armed(tmp_path):
    """A dry-run must not read like an install: no 'armed' success line."""
    tree = _make_tree(tmp_path)
    sandbox = _make_home(tmp_path)
    out = _run_dry(tree, sandbox).stdout
    assert "armed on every cluster node" not in out


@needs_runtime
def test_skip_flag_composes_with_dry_run(tmp_path):
    tree = _make_tree(tmp_path)
    sandbox = _make_home(tmp_path)
    out = _run_dry(tree, sandbox, extra=("--skip-node-recovery",)).stdout
    assert "skipped (--skip-node-recovery" in out
    assert not [ln for ln in out.splitlines()
                if "→ plan:" in ln and "hardware watchdog" in ln]
