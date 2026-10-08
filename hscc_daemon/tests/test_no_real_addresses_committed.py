"""Repo hygiene: no real operator address may be committed.

pom11/hscc is a PUBLIC repository. During the 2026-08-30 audit an audit report
quoted the live tailnet address and it reached origin before anyone noticed —
the pre-push check that was supposed to catch it grepped the WORKING TREE,
which had already been scrubbed, while the COMMITTED blob still carried the
address.

This test closes that gap for the tracked tree: it scans the files git actually
tracks, so a scrubbed checkout cannot mask a dirty commit. Placeholders are the
documented convention (10.0.0.x for LAN, 100.64.0.1 for tailnet) and are allowed.

The detection lives in ``scripts/address_guard.py``, NOT here, because a second
trigger now has to agree on what counts as a leak: ``.githooks/pre-commit`` runs
the same detector over the STAGED tree at commit time. That hook exists because
this pytest gate only fires when somebody runs the suite — and the 2026-10-08
leak (a ledger tick recording a verbatim ``mount_nfs`` command) was a docs-only
commit that never ran it. The hook's behaviour is exercised in
``test_precommit_address_hook.py``.
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# scripts/ is not an importable package (repo dirs here deploy standalone); put
# it on sys.path and import it bare, same as scripts/tests/*.py do.
_SCRIPTS = REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import address_guard  # noqa: E402


def test_no_real_operator_address_in_tracked_files():
    offenders = address_guard.scan_tracked(REPO)

    assert not offenders, (
        "Real operator addresses found in tracked files — this repo is PUBLIC.\n"
        "Use the documented placeholders (10.0.0.x for LAN, 100.64.0.1 for tailnet).\n"
        + "\n".join(offenders[:20])
    )


# ── single-implementation guarantees ─────────────────────────────────────────
# The card's core requirement: test and hook share ONE detector. These assert it
# structurally, so a future edit cannot quietly fork the pattern again.

# Literal fragment of the live-LAN branch of the regex, assembled so this file
# (which must not itself look like a second implementation) stays clean.
_LAN_BRANCH_LITERAL = "192" + r"\.168" + r"\.88" + r"\.\d"


def test_forbidden_pattern_is_defined_in_exactly_one_tracked_file():
    """No competing implementation of the detector may exist in the repo.

    Scans tracked files PLUS the detector itself, so the assertion is meaningful
    both before and after this card's files land.
    """
    detector = "scripts/address_guard.py"
    candidates = set(address_guard.tracked_paths(REPO)) | {detector}
    holders = sorted(
        rel
        for rel in candidates
        if rel.endswith((".py", ".sh", ".ts", ".js", ".swift"))
        and _LAN_BRANCH_LITERAL in (REPO / rel).read_text(errors="ignore")
    )
    assert holders == [detector], (
        "the forbidden-address regex must live in exactly one place, found in: "
        + ", ".join(holders)
    )


def test_hook_delegates_detection_instead_of_reimplementing_it():
    """The hook is plumbing: it may not carry an address pattern of its own."""
    hook = REPO / ".githooks" / "pre-commit"
    assert hook.is_file(), ".githooks/pre-commit must be committed (plain .git/hooks is not cloned)"
    text = hook.read_text()
    assert "address_guard.py" in text, "hook must call the shared detector"
    assert "--staged" in text, "hook must scan the STAGED tree, not the working tree"
    for digits in ("192", "100.64", "100.6"):
        assert digits not in text, (
            f"hook carries its own address pattern ({digits}) — use the shared detector"
        )
    assert hook.stat().st_mode & 0o111, "a non-executable hook is silently never run"


def test_hooks_path_is_configured():
    """A committed hook dir does nothing unless core.hooksPath points at it."""
    got = subprocess.run(["git", "-C", str(REPO), "config", "--get", "core.hooksPath"],
                         capture_output=True, text=True)
    assert got.stdout.strip() == ".githooks", (
        "core.hooksPath is not '.githooks' — run: "
        "python3 hscc-bootstrap/install_hooks.py (bootstrap does this automatically)"
    )
