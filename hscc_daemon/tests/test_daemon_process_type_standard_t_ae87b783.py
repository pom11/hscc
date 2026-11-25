"""Every plist this repo emits must carry `ProcessType => Standard`.

Why this file exists (t_ae87b783, operator decision 2026-11-25): the daemon's
launchd job shipped as `ProcessType => Background`, and Background is a job role
the OS applies to the WHOLE job subtree.  Measured on this host by bootstrapping
throwaway probe jobs into `gui/<uid>` and running a fixed 40M-iteration CPU loop
in each (full table in the comment above `LIVENESS_STAMP_S` in merge_queue.py):

    launchd ProcessType | gate leg                      | `ps -o pri` |  it/s
    Background          | plain                         | 4           |  5.5 M
    Background          | `taskpolicy -c utility`       | 4           |  5.6 M
    Standard            | plain                         | 20          | 39.1 M
    Standard            | `taskpolicy -c utility`       | 20          | 41.9 M

~7x, and the only lever that reaches it is this plist key — `taskpolicy -c`
parses only `utility`, `background` and `maintenance`, and clamping a child of a
Background job to utility does not lift it.  A 1323 s roster cost the merge queue
~75 min because of it (docs/audits/merge-queue-gate-leg-timings_t_fdc2bff0.md).

So the durability requirement is not "fix the live plist" (the operator did that
by hand, backup at ~/fleet-check/com.hermes.hscc_daemon.plist.bak-20261125) — it
is "no bootstrap may hand the box a Background job again".  Three emitters write
that key, and a fourth (hscc_daemon/launchd-setup.sh) renders the template; all
of them are asserted here, on the value a plist parser reads rather than on
prose, because the plist is what launchd hands the daemon.
"""

import plistlib
import re
import string
from pathlib import Path

import pytest

from hscc_daemon import install as install_mod

HERE = Path(__file__).resolve().parent
DAEMON_DIR = HERE.parent                      # hscc_daemon/
REPO = DAEMON_DIR.parent

#: What launchd must be told. Anything else is a regression to be justified by a
#: measurement, not a preference: Background throttles the job subtree ~7x and
#: Interactive claims foreground scheduling for a headless supervisor.
REQUIRED = "Standard"

TEMPLATE = DAEMON_DIR / "com.hermes.hscc_daemon.plist.template"


def _fill_placeholders(text):
    """Give every ``{field}`` in a Python format template a harmless value.

    Filling rather than regexing means what gets parsed is the plist launchd
    would actually receive, and a stray ``{`` in the template raises here loudly
    instead of producing a plist that quietly fails to parse in production.
    """
    fields = {f for _, f, _, _ in string.Formatter().parse(text) if f}
    return text.format(**{f: "/tmp/hscc-test" for f in fields})


def _process_type(text):
    """The ProcessType a plist parser reads, and only that.

    Parsing rather than grepping matters here: the files carry long comments
    that *say* "Standard" and "Background" while explaining the difference, and
    a text search would pass on a plist whose value key had been flipped back.
    """
    # The launchd template carries a `__TMUX_ENV__` line that is not valid XML
    # until launchd-setup.sh substitutes it; blank it, which is what the script
    # does when the installing shell has no TMUX_TMPDIR.
    text = re.sub(r"^\s*__TMUX_ENV__\s*$", "", text, flags=re.MULTILINE)
    parsed = plistlib.loads(_fill_placeholders(text).encode("utf-8"))
    values = re.findall(r"<key>ProcessType</key>\s*<string>([^<]*)</string>",
                        text)
    assert len(values) == 1, f"expected exactly one ProcessType key: {values}"
    assert parsed.get("ProcessType") == values[0] == REQUIRED, (
        f"ProcessType is {values[0]!r}, must be {REQUIRED!r}")


def test_shipped_daemon_template_is_standard():
    """hscc_daemon/com.hermes.hscc_daemon.plist.template — what launchd-setup.sh
    installs for `com.hermes.hscc_daemon`, the job every gate leg inherits."""
    _process_type(TEMPLATE.read_text())


def test_install_py_plist_is_standard():
    """hscc_daemon/install.py — the `hscc install` emitter. It had drifted to
    `Interactive`, a third posture for the same key."""
    _process_type(install_mod.PLIST_CONTENT)


def test_flightdeck_daemon_plist_is_standard():
    """hscc-project/flightdeck/commands/daemon_install.py — the flightdeck
    supervisor's own job, same key, same throttle.

    Read from the file rather than imported: that module does relative imports
    (`from ..core import daemon`), so loading it from the hscc_daemon leg would
    drag flightdeck's package in — and two legs each owning a `_theme.py` is a
    known collection hazard, not worth crossing to check one plist key.  The
    assert on the match is what makes a rename of the constant a failure here.
    """
    path = REPO / "hscc-project" / "flightdeck" / "commands" / "daemon_install.py"
    if not path.exists():
        pytest.skip(f"{path} not present in this layout")
    m = re.search(r'_PLIST_TEMPLATE = """(.*?)"""', path.read_text(), re.S)
    assert m, "_PLIST_TEMPLATE not found — renamed? fix this test to match"
    _process_type(m.group(1))


def test_the_three_emitters_agree():
    """One posture, three files: a fix applied to only one of them is the drift
    that produced this card (install.py said Interactive while the template said
    Background while the operator's live plist said Standard)."""
    assert plistlib.loads(
        _fill_placeholders(install_mod.PLIST_CONTENT).encode())["ProcessType"] \
        == REQUIRED
    assert _process_type_strings(TEMPLATE.read_text()) == []


def _process_type_strings(text):
    return re.findall(r"<string>\s*(Background|Interactive)\s*</string>", text)


def test_no_emitter_names_a_throttling_process_type():
    """Positive-space version: no `<string>Background</string>` / `Interactive`
    anywhere near the ProcessType key of the shipped template."""
    assert _process_type_strings(TEMPLATE.read_text()) == []
