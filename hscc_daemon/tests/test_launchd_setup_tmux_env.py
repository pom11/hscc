"""launchd-setup.sh plist rendering — TMUX_TMPDIR propagation (t_6b5a5c61).

Why this exists: the daemon's orch-waker injects into tmux panes, and tmux
locates its server socket under TMUX_TMPDIR. A launchd-started process gets a
CLEAN environment, so before this fix the installed plist never carried the
operator's TMUX_TMPDIR (the live one lived under ~/.tmux/tmp, created by the
iTerm2-integration tmux server). The daemon found no tmux server, and every
board failed `no tmux pane` forever while panes visibly existed — 2,283 audit
lines, zero ok=true, measured live 2026-11-03.

These tests drive the REAL script (bash launchd-setup.sh render-only <out>)
and parse the generated plist as XML — the plist, not the prose, is what
launchd hands the daemon. `render-only` renders and exits before any launchctl
interaction, so nothing here installs, loads, or touches a real job.

Note: the card asked to "extend the existing launchd-setup test (grep -rn
launchd-setup hscc_daemon/tests/)" — that grep is EMPTY. No hscc_daemon test
ever drove this script (the only launchd-setup assertions lived in
hscc-bootstrap/tests/test_restart_daemon.py and only pin the job LABEL
constant). This module is the missing harness, not an extension.
"""

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PKG_DIR = HERE.parent
SETUP = PKG_DIR / "launchd-setup.sh"
TEMPLATE = PKG_DIR / "com.hermes.hscc_daemon.plist.template"


def _render(tmp_path, tmux_tmpdir):
    """Run `launchd-setup.sh render-only <out>` in a controlled env and return
    the parsed plist dict, plus the raw text for key-absence assertions.

    ``tmux_tmpdir``: a str is exported as TMUX_TMPDIR into the installing
    shell's env; None leaves TMUX_TMPDIR absent from the env entirely.
    """
    if not SETUP.exists():  # pragma: no cover — guards against a moved script
        pytest.skip(f"{SETUP} not found (deployed layout without setup script?)")
    if shutil.which("launchctl") is None:
        pytest.skip("launchd-setup.sh preflight requires launchctl (macOS)")

    # A fake HOME with a real, executable python so the interpreter probe
    # resolves deterministically instead of picking the test runner's python.
    home = tmp_path / "home"
    (home / ".hermes" / "hermes-agent" / "venv" / "bin").mkdir(parents=True)
    py = home / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)

    out = tmp_path / "rendered.plist"
    env = {
        "HOME": str(home),
        "PATH": "/bin:/usr/bin:/usr/sbin:/sbin",
        # The script is bash; keep BASH_ENV/ENV empty so a user rc file cannot
        # inject state into the render.
        "BASH_ENV": "",
        "ENV": "",
    }
    if tmux_tmpdir is not None:
        env["TMUX_TMPDIR"] = tmux_tmpdir

    proc = subprocess.run(
        ["bash", str(SETUP), "render-only", str(out)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"render-only failed rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    text = out.read_text()
    return plistlib.loads(text.encode()), text


class TestRenderOnlySeam:
    """render-only must render WITHOUT touching launchctl or the real plist."""

    def test_render_writes_plist_without_installing(self, tmp_path):
        parsed, _ = _render(tmp_path, tmux_tmpdir=None)
        # Parsed at all -> well-formed XML with the daemon's own identity.
        assert parsed["Label"] == "com.hermes.hscc_daemon"

    def test_render_leaves_no_placeholder_behind(self, tmp_path):
        _, text = _render(tmp_path, tmux_tmpdir=None)
        assert "__TMUX_ENV__" not in text
        assert "__HOME__" not in text
        assert "__PYBIN__" not in text

    def test_env_unset_renders_differ_from_set(self, tmp_path):
        """Anti-vacuity pair: 'set' and 'unset' renders must DIFFER in the
        TMUX_TMPDIR key — proves the seam reads the real environment."""
        _, unset_text = _render(tmp_path, tmux_tmpdir=None)
        _, set_text = _render(tmp_path / "s", tmux_tmpdir="/tmp/tmux-set")
        assert "TMUX_TMPDIR" not in unset_text
        assert "TMUX_TMPDIR" in set_text


class TestTmuxTmpdirPropagation:
    """The card's two directions: env set -> key present with value;
    env unset -> key absent (never an empty value)."""

    def test_env_set_key_present_with_value(self, tmp_path):
        socket_dir = str(tmp_path / ".tmux" / "tmp")
        parsed, text = _render(tmp_path, tmux_tmpdir=socket_dir)
        env_vars = parsed["EnvironmentVariables"]
        assert "TMUX_TMPDIR" in env_vars
        assert env_vars["TMUX_TMPDIR"] == socket_dir
        assert "<key>TMUX_TMPDIR</key>" in text

    def test_env_unset_key_absent(self, tmp_path):
        parsed, text = _render(tmp_path, tmux_tmpdir=None)
        env_vars = parsed["EnvironmentVariables"]
        assert "TMUX_TMPDIR" not in env_vars
        # Absent means ABSENT — not an empty string launchd would export as "".
        assert "TMUX_TMPDIR" not in text

    def test_env_empty_string_treated_as_unset(self, tmp_path):
        """`export TMUX_TMPDIR=` is 'unset' for tmux purposes; writing an
        empty value would point tmux at "" — worse than omitting the key."""
        parsed, text = _render(tmp_path, tmux_tmpdir="")
        assert "TMUX_TMPDIR" not in parsed["EnvironmentVariables"]
        assert "TMUX_TMPDIR" not in text

    def test_value_with_xml_metacharacters_stays_valid(self, tmp_path):
        """XML-escaping is not paranoia: a home path with & is legal, and a
        broken plist means launchd refuses the job (the whole daemon is down,
        not just the waker)."""
        socket_dir = str(tmp_path / "we&ird<dir>" / "tmux")
        parsed, _ = _render(tmp_path, tmux_tmpdir=socket_dir)
        assert parsed["EnvironmentVariables"]["TMUX_TMPDIR"] == socket_dir

    def test_existing_env_vars_untouched(self, tmp_path):
        """The propagation is additive: PATH/HOME/PYTHONPATH survive the
        placeholder rewrite (the t_b543e530 PATH guard stays true)."""
        parsed, _ = _render(tmp_path, tmux_tmpdir="/tmp/tmux-x")
        env_vars = parsed["EnvironmentVariables"]
        assert "/.local/bin" in env_vars["PATH"]
        assert env_vars["HOME"]
        assert env_vars["PYTHONPATH"]


class TestTemplateCarriesPlaceholder:
    """Static pins so a template edit that drops the placeholder fails loudly
    instead of silently shipping pane-blind daemons again."""

    def test_template_has_placeholder_key(self):
        text = TEMPLATE.read_text()
        assert "__TMUX_ENV__" in text, (
            "plist template lost the __TMUX_ENV__ placeholder — launchd-setup.sh "
            "renders TMUX_TMPDIR into it; without it the install path ships a "
            "pane-blind daemon (t_6b5a5c61: every board 'no tmux pane')"
        )
