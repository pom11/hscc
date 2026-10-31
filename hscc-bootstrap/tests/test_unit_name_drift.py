"""Every reader of the systemd --user unit name resolves through unit_name.py (t_d8f13693).

The bug this card fixed was not exotic: two installers each typed their own unit
name for one daemon, so a box installed by one was invisible to anything hardcoded
to the other, and `systemctl --user disable <one>` left <the other> enabled. It
lasted because nothing *checked* the vocabulary — each site looked fine alone.

So this file is the canary, and it is deliberately dumb: it greps production
shell/python for a **typed unit-name literal in a command position** and fails any
file that does not resolve through ``hscc_daemon/unit_name.py``. Comment prose is
allowed to *name* both units (the migration docs must), which is why the pattern
needs a systemctl verb or a unit-dir path next to the name rather than a bare
substring.

``test_restart_daemon.py::test_systemd_unit_candidates_match_sibling_emitters`` is
the companion pin for the restart helper's probe-list ordering; this one is
fleet-wide and catches the *new* reader nobody thought about.

No node, no socket, no subprocess: source text only.
"""

import re
import sys
from pathlib import Path

import pytest

BOOT = Path(__file__).resolve().parent.parent
REPO = BOOT.parent
UNIT_MODULE = REPO / "hscc_daemon" / "unit_name.py"

sys.path.insert(0, str(UNIT_MODULE.parent))
from unit_name import LEGACY_UNIT_NAME, UNIT_CANDIDATES, UNIT_NAME  # noqa: E402

#: Files that may type a unit name because they are genuine READERS of the retired
#: name, with the string that must appear in them as the justification. A new
#: reader has to be added HERE, in a diff someone reviews — which is the whole
#: point: the drift survived because no list like this existed.
ALLOWLIST = {
    "restart_daemon.sh": "hscc_daemon/unit_name.py",
    "bootstrap.sh": "hscc_daemon/unit_name.py",
}

#: Python files are legitimate if they actually IMPORT the vocabulary, not merely
#: mention it in a comment.
IMPORTS_UNIT_NAME = re.compile(
    r"^\s*(?:from\s+(?:hscc_daemon\.)?unit_name\s+import|import\s+unit_name\b)", re.M)


def _command_patterns(name):
    """Literal in a COMMAND position: a systemctl argv touching that unit, or a path
    under the user unit dir named after it.

    Prose ("the legacy hscc_daemon.service was retired") does not match, which is
    what keeps the migration docs and these tests readable. Built PER NAME —
    matching against the pattern *source* would need the name unescaped in it, and
    `\\.` never is, so such a filter silently selects nothing (a vacuous canary).
    """
    esc = re.escape(name)
    return [re.compile(rf"systemctl[^\n]*\b{esc}\b"),
            re.compile(rf"systemd/user/{esc}\b")]


SKIP_DIRS = {"_archive", "docs", "changelog.d", "node_modules", ".git", "install"}


def _production_sources():
    for path in sorted(REPO.rglob("*")):
        if path.suffix not in (".py", ".sh") or not path.is_file():
            continue
        rel_parts = set(path.relative_to(REPO).parts)
        if rel_parts & SKIP_DIRS or "tests" in rel_parts:
            continue
        yield path


def test_unit_module_declares_the_vocabulary():
    src = UNIT_MODULE.read_text()
    assert re.search(rf'^UNIT_NAME = "{re.escape(UNIT_NAME)}"$', src, re.M)
    assert re.search(rf'^LEGACY_UNIT_NAME = "{re.escape(LEGACY_UNIT_NAME)}"$', src, re.M)
    assert UNIT_CANDIDATES == (UNIT_NAME, LEGACY_UNIT_NAME)
    assert UNIT_NAME != LEGACY_UNIT_NAME


@pytest.mark.parametrize("name", [UNIT_NAME, LEGACY_UNIT_NAME])
def test_no_production_file_types_a_unit_name_without_resolving_through_the_module(name):
    offenders = []
    for path in _production_sources():
        text = path.read_text(errors="replace")
        hit = any(p.search(text) for p in _command_patterns(name))
        if not hit:
            continue
        allowed = ALLOWLIST.get(path.name)
        if allowed:
            assert allowed in text, (
                f"{path.relative_to(REPO)} is allowlisted for typing {name}, but "
                f"the justification {allowed!r} is missing — a reader must say "
                f"which module its names mirror")
            continue
        if path.suffix == ".py" and IMPORTS_UNIT_NAME.search(text):
            continue
        offenders.append(str(path.relative_to(REPO)))
    assert not offenders, (
        f"{name} is typed directly in {offenders} with no link to "
        f"hscc_daemon/unit_name.py. Import UNIT_NAME / LEGACY_UNIT_NAME (python), "
        f"take the name from `hscc.py unit-emit --json` (shell), or — if this file "
        f"is a genuine reader of the retired name — add it to ALLOWLIST with the "
        f"reason. That is exactly how the two installers drifted apart once."
    )


def test_emitters_write_only_the_canonical_name():
    """A file that WRITES a unit file must write the canonical one. The legacy name
    may appear in a purge, never in an emitter's output path."""
    install = (REPO / "hscc_daemon" / "install.py").read_text()
    assert "SYSTEMD_UNIT_NAME = UNIT_NAME" in install, (
        "install.py must take its name from unit_name.py")
    assert "LEGACY_SYSTEMD_UNIT_NAME = LEGACY_UNIT_NAME" in install
    # the raw template is a module constant: one body, one place
    assert install.count("SYSTEMD_UNIT_CONTENT = ") == 1


def _code_lines_only(text):
    """Shell source with whole-line comments dropped.

    These pins read a script's SOURCE for what it may *do*; prose is allowed — and
    expected — to name the bug being prevented (systemd-setup.sh documents that
    `hscc.py plist > UNIT_FILE` once wrote an unparseable unit). Only code counts.
    """
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_systemd_setup_takes_both_names_from_the_emitter_json():
    """The setup helper used to hardcode `hscc_daemon.service` AND pipe a rendered
    console view into the unit file. Neither may come back."""
    setup = _code_lines_only((REPO / "hscc_daemon" / "systemd-setup.sh").read_text())
    assert f'UNIT_NAME="{UNIT_NAME}"' not in setup and \
        f'UNIT_NAME="{LEGACY_UNIT_NAME}"' not in setup, (
        "systemd-setup.sh must read the unit name out of the unit-emit JSON — "
        "typing it is how the two install paths disagreed")
    assert "unit-emit" in setup
    assert "plist" not in setup, (
        "`hscc.py plist > UNIT_FILE` redirected a rendered Rich console view into "
        "a unit file systemd could not parse; unit-emit writes the raw template")


def test_cluster_bootstrap_probes_both_unit_files():
    """Card body: `hscc-cluster/bootstrap.sh:58 already references
    ~/.config/systemd/user/hscc_daemon.service, so count that site too.` It must
    probe BOTH: only-canonical calls a pre-migration node unsupervised and then
    runs `hscc install` on top of a unit systemd already owns."""
    boot = (REPO / "hscc-cluster" / "bootstrap.sh").read_text()
    assert f"systemd/user/{UNIT_NAME}" in boot
    assert f"systemd/user/{LEGACY_UNIT_NAME}" in boot
    assert "unit_name" in boot, "the probe list must be documented against its source"


def test_restart_daemon_helper_still_probes_both():
    """The card says the candidate-list pin is the canary — keep it, and keep it
    covering the retired name until the operator runs the migration."""
    helper = (BOOT / "restart_daemon.sh").read_text()
    m = re.search(r'SYSTEMD_UNIT_CANDIDATES="([^"]+)"', helper)
    assert m, "helper must keep its probe list declarative"
    assert set(m.group(1).split()) == set(UNIT_CANDIDATES)
