"""Run the REAL installer into a throwaway home and assert it stays there.

Why this exists: three releases in a row shipped bugs that only a real run
could find — `bootstrap.sh` did not parse at all (2.5.0-2.5.2), and several
helpers hardcoded `~/.hermes` / `~/.hscc`, so an install aimed at another home
silently read and wrote the OPERATOR's live config, triggers and autodown
state. The Python suite was green throughout, because nothing executed the
installer.

Isolation is HERMES_HOME / HSCC_DIR / HERMES_SCRIPTS pointed into tmp.

HOME is deliberately NOT faked, even though that would contain a helper that
still hardcodes `expanduser("~/.hscc/...")`. Preflight legitimately discovers
prerequisites under the real home — sparkrun's cluster config lives in
`~/.config/sparkrun` — so a fake HOME fails the doctor with "no cluster
configured" and the installer never runs. Two controls cover the gap instead:
  * preventive: ``test_helpers_do_not_hardcode_the_home_paths`` fails on a new
    hardcoded path, before it can ever run;
  * detective: the live-state checksum assertion below fails if a run did
    reach outside the sandbox.

Skipped steps and why:
  --skip-cli      installs a console script INTO the Hermes venv (shared, real)
  --skip-patches  patches the hermes-agent git checkout (shared, real)
  --skip-daemon   installs launchd/systemd units and restarts the live daemon
Those three are the only steps that intentionally write outside the home.
"""

import hashlib
import os
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


def _md5(p: Path):
    return hashlib.md5(p.read_bytes()).hexdigest() if p.is_file() else None


@pytest.fixture
def sandbox(tmp_path):
    """A throwaway HERMES_HOME/HSCC_DIR with the real venv reachable."""
    home = tmp_path / "home"
    hermes = home / ".hermes"
    hscc = home / ".hscc"
    for d in (hermes, hscc):
        d.mkdir(parents=True)
    # Symlink the DIRECTORY, not the python binary: symlinking the binary
    # relocates sys.prefix and the venv's packages (rich, yaml) disappear.
    (hermes / "hermes-agent").symlink_to(REAL_HERMES_AGENT)
    return {"home": home, "hermes": hermes, "hscc": hscc}


def _run(sandbox, extra=()):
    env = dict(os.environ)
    env.update({
        # NOTE: HOME is intentionally left alone — see the module docstring.
        "HERMES_HOME": str(sandbox["hermes"]),
        "HSCC_DIR": str(sandbox["hscc"]),
        "HERMES_SCRIPTS": str(sandbox["hermes"] / "scripts"),
    })
    return subprocess.run(
        ["bash", str(BOOTSTRAP), "--yes",
         "--skip-cli", "--skip-daemon", "--skip-patches", *extra],
        capture_output=True, text=True, env=env, timeout=900)


@needs_runtime
def test_installer_completes_and_is_self_contained(sandbox):
    live_cfg = Path.home() / ".hermes" / "config.yaml"
    live_trig = Path.home() / ".hscc" / "triggers.json"
    before = (_md5(live_cfg), _md5(live_trig))

    r = _run(sandbox)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"installer failed:\n{out[-3000:]}"
    assert "Done" in out, f"installer did not reach its summary:\n{out[-2000:]}"

    # Payload landed in the sandbox.
    for rel in ("plugins", "profiles", "skills", "scripts", "SOUL.md"):
        assert (sandbox["hermes"] / rel).exists(), f"missing {rel} in HERMES_HOME"
    for rel in ("serving.json", "triggers.json", "autodown.json", "autonomy"):
        assert (sandbox["hscc"] / rel).exists(), f"missing {rel} in HSCC_DIR"

    # And nothing reached the operator's live state.
    assert (_md5(live_cfg), _md5(live_trig)) == before, \
        "the isolated install modified LIVE state"


@needs_runtime
def test_fresh_home_says_not_wired_instead_of_claiming_success(sandbox):
    """A fresh home has no config.yaml, so wiring is a deliberate no-op.

    It must SAY so. Reporting "config wired" for a machine where nothing was
    wired is the silent gap this step exists to close.
    """
    out = _run(sandbox).stdout
    assert "NOT WIRED" in out, f"fresh install did not flag unwired config:\n{out[-2000:]}"
    assert "config wired (" not in out, "claimed success with no config.yaml"


@needs_runtime
def test_status_lines_are_not_polluted_by_rich_panels(sandbox):
    """Helpers print a PLAIN line when captured.

    bootstrap.sh embeds a helper's stdout into its own status line, so a Rich
    panel there arrived as box-drawing characters pasted mid-sentence.
    """
    out = _run(sandbox).stdout
    for line in out.splitlines():
        if line.lstrip().startswith(("✓", "⚠")) and "─" in line:
            pytest.fail(f"status line contains panel art: {line[:160]}")


@needs_runtime
def test_rerun_is_idempotent(sandbox):
    """Installing twice must not fail or double-apply triggers."""
    first = _run(sandbox)
    assert first.returncode == 0
    second = _run(sandbox)
    assert second.returncode == 0, f"second run failed:\n{(second.stdout + second.stderr)[-2000:]}"
    assert "added 0" in second.stdout, \
        "second run re-added trigger rules (not idempotent)"


def test_helpers_do_not_hardcode_the_home_paths():
    """Static guard, runs without the runtime.

    Every bootstrap helper must resolve HERMES_HOME / HSCC_DIR from the
    environment rather than baking `~/.hermes` or `~/.hscc`, or an install into
    another home reaches into the operator's live state. Checked statically so
    a regression is caught even where the real installer cannot run.
    """
    offenders = []
    for py in sorted((REPO / "hscc-bootstrap").glob("*.py")):
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if "expanduser(" not in line or line.lstrip().startswith("#"):
                continue
            if '"~/.hermes' not in line and '"~/.hscc' not in line:
                continue
            # Acceptable: used only as the FALLBACK for an env lookup.
            if ("environ.get" in line or "or os.path.expanduser" in line
                    or "hermes_home or" in line):
                continue
            offenders.append(f"{py.name}:{n}: {line.strip()}")
    assert not offenders, (
        "bootstrap helpers hardcode a home path instead of reading the env:\n  "
        + "\n  ".join(offenders))
