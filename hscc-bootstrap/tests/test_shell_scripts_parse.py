"""Every shipped shell script must parse. Regression guard.

`bootstrap.sh` shipped SYNTACTICALLY BROKEN: a `--skip-cli` guard was added as
`if $SKIP_CLI; then warn "skipped"; else` and its closing `fi` was never
written, so bash aborted with "unexpected end of file" partway through the
install — after copying plugin files but before the CLI, watchdogs, roles and
daemon steps. The whole Python suite stayed green the entire time, because
nothing executed the installer.

`bash -n` parses without running, so this is safe and fast. The lesson it
encodes: a green pytest suite says nothing about whether the shell entry points
the operator actually types still parse.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _scripts():
    """Git-TRACKED .sh files only.

    `rglob` also walks `.worktrees/`, which holds detached checkouts of older
    commits — including ones that legitimately predate this fix — so it failed
    on history rather than on shipped code. `git ls-files` is the precise
    definition of "what this repo ships".
    """
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "*.sh"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [REPO / line for line in out.stdout.split("\n")
            if line.strip() and (REPO / line).is_file()]


ALL = _scripts()


def test_there_are_scripts_to_check():
    """Guard the guard: an empty glob would make every assertion below vacuous."""
    assert ALL, f"no .sh files found under {REPO}"


@pytest.mark.parametrize("script", ALL, ids=lambda p: str(p.relative_to(REPO)))
def test_script_parses(script):
    r = subprocess.run(["bash", "-n", str(script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, (
        f"{script.relative_to(REPO)} does not parse:\n{r.stderr.strip()}")


def test_bootstrap_help_runs():
    """The installer's own --help must actually execute, not just parse.

    `bash -n` would pass a script whose flag parsing exits non-zero; this runs
    the one path that is guaranteed side-effect free.
    """
    r = subprocess.run(
        ["bash", str(REPO / "hscc-bootstrap" / "bootstrap.sh"), "--help"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"bootstrap.sh --help failed:\n{r.stderr.strip()}"
    assert "Usage:" in r.stdout
