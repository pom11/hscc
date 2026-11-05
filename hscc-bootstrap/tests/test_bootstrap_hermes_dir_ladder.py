"""t_8579fba4 — bootstrap.sh's `resolve_hermes_dir` ladder, exercised in a real shell.

The stage that applies the curated hermes patch set used to open with
`HERMES_DIR="$HERMES_HOME/hermes-agent"` — a raw env join. Hermes exports
`HERMES_HOME=<root>/profiles/<name>` for every profile session, so from a
profile session HERMES_DIR named a directory that cannot exist;
`apply_patches.py --check` reported it "not found", the grep for `'"ok": true'`
failed, and the stage printed "already patched, or upstream drifted" and
skipped the apply. The patch set silently never landed and the message blamed
upstream. Same bug class as the $PYBIN join fixed in t_314eb13c, one level up.

These tests extract the EXACT `resolve_hermes_dir` block from bootstrap.sh and
source it in bash against fabricated roots (never the operator's ~/.hermes),
asserting which candidate wins and that "no checkout" is a distinct outcome.
Mirrors test_bootstrap_venv_ladder.py's extract-and-run pattern: nothing else
in the suite executes shell.
"""
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BOOT = HERE.parent / "bootstrap.sh"
SRC = BOOT.read_text()

_HEAD = "resolve_hermes_dir() {"
_TAIL = "  return 1\n}"


def _block() -> str:
    start = SRC.index(_HEAD)
    end = SRC.index(_TAIL, start) + len(_TAIL)
    return SRC[start:end]


LADDER = _block()


def resolve(tmp_path, *, pybin, hermes_home=None, home=None):
    """Run the extracted resolver with PYBIN/HERMES_HOME/HOME pinned.

    Returns (rc, HERMES_DIR, HERMES_DIR_FROM).
    """
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME",)}
    env["HOME"] = str(home if home is not None else tmp_path)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    env["PATH"] = "/usr/bin:/bin"
    script = (
        "set -u\n"
        f'PYBIN="{pybin}"\n'
        f"{LADDER}\n"
        "resolve_hermes_dir; rc=$?\n"
        'printf "%s\\t%s\\t%s" "$rc" "$HERMES_DIR" "$HERMES_DIR_FROM"\n'
    )
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env=env, timeout=30)
    assert p.returncode == 0, f"resolver crashed: {p.stderr}"
    rc, hermes_dir, src = p.stdout.split("\t")
    return int(rc), hermes_dir, src


def _mk_agent(root: Path) -> Path:
    agent = root / "hermes-agent"
    agent.mkdir(parents=True)
    return agent


def _mk_pyvenv(root: Path) -> Path:
    bindir = _mk_agent(root) / "venv" / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python3"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    return py


_HDR = 'hdr "Install: hermes/sparkrun patches"'


def _stage() -> str:
    start = SRC.index(_HDR)
    end = SRC.index('hdr "', start + len(_HDR))
    return SRC[start:end]


# ── dynamic: the ladder in a real bash, fabricated layouts ───────────────────

def test_pyvenv_grandparent_wins(tmp_path):
    """The already-laddered PYBIN pins the checkout: py -> venv -> hermes-agent."""
    root = tmp_path / ".hermes"
    py = _mk_pyvenv(root)
    profile = root / "profiles" / "backend-engineer"
    profile.mkdir(parents=True)

    rc, d, src = resolve(tmp_path, pybin=py, hermes_home=profile)
    assert rc == 0 and src == "pyvenv"
    assert d == str(root / "hermes-agent")


def test_profile_home_falls_back_to_the_root_checkout(tmp_path):
    """PYBIN is a PATH python (no venv shape); HERMES_HOME is a profile dir."""
    root = tmp_path / ".hermes"
    agent = _mk_agent(root)
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)

    rc, d, src = resolve(tmp_path, pybin="/usr/bin/python3", hermes_home=profile)
    assert rc == 0 and src == "profiles-root"
    assert d == str(agent)


def test_profile_local_checkout_wins_over_the_root(tmp_path):
    root = tmp_path / ".hermes"
    _mk_agent(root)
    profile = root / "profiles" / "worker"
    local = _mk_agent(profile)

    rc, d, src = resolve(tmp_path, pybin="/usr/bin/python3", hermes_home=profile)
    assert rc == 0 and src == "home"
    assert d == str(local)


def test_default_home_used_when_unset(tmp_path):
    agent = _mk_agent(tmp_path / ".hermes")
    rc, d, src = resolve(tmp_path, pybin="/usr/bin/python3", hermes_home=None)
    assert rc == 0 and src == "home"
    assert d == str(agent)


def test_no_checkout_anywhere_is_a_distinct_failure(tmp_path):
    """rc!=0 -> the stage must warn 'NOT applied', never blame upstream drift."""
    rc, d, src = resolve(tmp_path, pybin="/usr/bin/python3",
                        hermes_home=tmp_path / "nope")
    assert rc == 1 and d == "" and src == "none"


def test_a_hermes_agent_file_is_not_a_checkout(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir(parents=True)
    (root / "hermes-agent").write_text("stale file\n")
    rc, d, src = resolve(tmp_path, pybin="/usr/bin/python3", hermes_home=None)
    assert rc == 1 and d == ""


def test_pyvenv_shape_must_actually_be_hermes_agent(tmp_path):
    """/opt/x/venv/bin/python3 must not be trusted as a Hermes checkout."""
    fake = tmp_path / "brew" / "venv" / "bin"
    fake.mkdir(parents=True)
    py = fake / "python3"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    rc, d, src = resolve(tmp_path, pybin=py, hermes_home=tmp_path / "nope")
    assert rc == 1 and d == ""


def test_works_under_set_u_without_hermes_home(tmp_path):
    """bootstrap.sh runs `set -uo pipefail`; the resolver must not trip -u."""
    rc, d, _ = resolve(tmp_path, pybin="/usr/bin/python3", hermes_home=None)
    assert rc in (0, 1)  # no crash — outcome depends on whether HOME/.hermes exists


# ── static pins: the stage keeps the contract, no raw join returns ──────────

def _code_lines(text: str) -> str:
    """The script's non-comment lines — the pin must match CODE, not the prose
    that documents the old bug (the block comment quotes the old join)."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_no_raw_join_remains_in_the_patch_stage():
    code = _code_lines(SRC)
    assert 'HERMES_DIR="$HERMES_HOME/hermes-agent"' not in code
    stage = _stage()
    assert "resolve_hermes_dir" in stage
    assert "$HERMES_HOME/hermes-agent" not in _code_lines(stage), \
        "the patch stage must never rejoin HERMES_HOME raw (t_8579fba4)"


def test_missing_checkout_is_reported_as_not_applied_not_drift():
    stage = _stage()
    assert "! resolve_hermes_dir" in stage, \
        "no-checkout must be its own branch, ahead of the drift message"
    drift_line = [ln for ln in stage.splitlines() if "upstream drifted" in ln]
    assert drift_line and "$HERMES_DIR" in drift_line[0], \
        "the drift message must name the checkout it actually checked"


def test_check_and_apply_target_the_same_resolved_dir():
    """The --check that gates the apply must check the SAME dir the apply writes.

    The old stage checked apply_patches.py's internal env-derived default while
    applying to HERMES_DIR — the two disagreed silently under a profile
    session. `--target "$HERMES_DIR"` on both calls makes that impossible.
    """
    stage = _stage()
    check_call = [ln for ln in stage.splitlines()
                  if ln.lstrip().startswith("CHK=") and "--check" in ln]
    assert check_call and '--target "$HERMES_DIR"' in check_call[0]


def test_dry_run_reports_the_resolved_dir_and_ladder_source():
    stage = _stage()
    dry = [ln for ln in stage.splitlines() if "plan " in ln]
    assert dry and "$HERMES_DIR" in dry[0] and "$HERMES_DIR_FROM" in dry[0]
