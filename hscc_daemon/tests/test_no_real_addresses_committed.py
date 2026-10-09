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

import pytest

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


# ── containment tripwire: the class the regex structurally cannot see ────────
#
# `address_guard.FORBIDDEN` carries an ANTI-TRUNCATION boundary,
# `(?<![0-9])…(?![0-9])`, which deliberately refuses a match when a digit abuts
# the quad — that is what keeps `1.2.3.44`-shaped version strings from failing
# every commit (t_1b7b3166). The consequence nobody pinned until round 1 of that
# card caught it in review: a real value written as `1<value>` or `<value>1` is
# INVISIBLE to `scan_tracked`, to the pre-commit hook, to the CI backstop and to
# `.github/scripts/redact_guard_report.py`, because all four read the same
# pattern. This card's own audit note committed the live LAN value three times
# that way and every gate was green; the added-lines scan that "proved" the diff
# clean was guard-based and therefore shared the blind spot, so it was not
# evidence for the claim it was cited for.
#
# Substring containment has no boundary to miss. This is the companion control
# that makes the class unclosable-by-accident again: it covers every adjacency,
# and it reads BYTES so the files the guard skips (a `SKIP_SUFFIXES` asset, an
# unlisted NUL-bearing blob) are covered too.
#
# Both values are assembled from parts at runtime, exactly like `_addr` in
# `scripts/tests/test_address_guard.py`: this file is itself tracked and scanned,
# and the parts-split form is already proven clean of the single-implementation
# pin below (`_LAN_BRANCH_LITERAL`).
def _addr(*parts):
    return "".join(parts)


_REAL_VALUES = {
    "LAN": _addr("192", ".168.88.244"),
    "tailnet": _addr("100.64", ".55.7"),
}


def _fixture(tmp_path, name, content):
    """A throwaway 'repo' (a bare dir — `scan_paths`/containment take rel paths,
    not a git repo) holding one file. Returns (root, [rel])."""
    root = tmp_path / "r"
    root.mkdir(exist_ok=True)
    (root / name).write_text(content, encoding="utf-8")
    return root, [name]


def _containment_offenders(root, rels):
    """``rel:line: which`` for every tracked file carrying a real value as a
    SUBSTRING, at any adjacency.

    Failure text carries the path, the line and whether a digit abuts — NEVER the
    value. That is not decoration: a failing assertion message lands in the
    GitHub job log, the log is public, and the redactor that normally masks a
    leak in a log keys on the same anti-truncation boundary that hid the leak in
    the first place. A tripwire that printed what it found would be the 2026-08-30
    bug relocated into the test suite.
    """
    root = Path(root)
    hits = []
    for rel in rels:
        path = root / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue                      # deleted-but-tracked / unreadable: same rule as scan_paths
        for which, value in _REAL_VALUES.items():
            needle = value.encode("utf-8")
            at = data.find(needle)
            while at >= 0:
                line = data.count(b"\n", 0, at) + 1
                end = at + len(needle)
                pre = data[at - 1:at]
                post = data[end:end + 1]
                digit = (pre.isdigit() or post.isdigit()) if (pre or post) else False
                hits.append(
                    f"{rel}:{line}: {which} value present"
                    + (" (digit-abutting — invisible to the guard's regex)" if digit else "")
                )
                at = data.find(needle, at + 1)
    return hits


def test_no_real_value_survives_as_a_substring_in_any_tracked_file():
    """Every adjacency class, including the digit-adjacent residue the regex refuses.

    The population claim this pins: zero occurrences tree-wide. Verified against
    origin/main (0) and against this tree (0 after the round-1 scrub).
    """
    offenders = _containment_offenders(REPO, address_guard.tracked_paths(REPO))

    assert not offenders, (
        "A real operator address is present in a tracked file as a substring — "
        "this repo is PUBLIC.\n"
        "If the finding says 'digit-abutting', `guard --tracked` will NOT flag it "
        "and neither will the hook: scrub the whole token to a placeholder "
        "(10.0.0.x / 100.64.0.1), do not trust the guard to tell you it is there.\n"
        + "\n".join(offenders[:20])
    )


def test_the_containment_tripwire_sees_what_the_guard_regex_cannot(tmp_path):
    """The tripwire must be strictly stronger than the regex on the leak class.

    Written as a differential pin rather than as prose: a fixture holding the
    value with a digit glued to each side is the exact shape this card leaked in
    review. `find_in_text` returns [] on it by design — the LAN branch is a fixed
    literal prefix, so a leading digit fails the lookbehind and a trailing digit
    fails the lookahead with no way to re-anchor — while the tripwire must still
    name the file. If someone ever "simplifies" the tripwire into a regex reusing
    `FORBIDDEN`, this goes red.
    """
    digit_abutting = ("code `1" + _REAL_VALUES["LAN"] + "` and `"
                      + _REAL_VALUES["LAN"] + "1`")

    assert address_guard.find_in_text(digit_abutting) == [], (
        "the anti-truncation boundary no longer refuses digit adjacency — so the "
        "differential below proves nothing; re-measure before editing this test"
    )
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "notes.md").write_text(digit_abutting, encoding="utf-8")

    found = _containment_offenders(repo, ["notes.md"])
    assert len(found) == 2, f"tripwire missed the digit-abutting class: {found}"
    assert all("digit-abutting" in f for f in found), found
    for which, value in _REAL_VALUES.items():
        assert value not in "\n".join(found), f"tripwire echoed the {which} value it found"


def test_a_digit_neighbour_can_shift_what_the_regex_thinks_it_matched(tmp_path):
    """Why "the guard flagged it" is not evidence that a file is value-clean.

    Found while writing the differential above, and the reason that differential
    uses the LAN value. The tailnet value followed by one digit is NOT refused:
    its last octet is matched by ``\\d{1,3}``, so the pattern absorbs the abutting
    digit and reports a LONGER quad — a hit whose reported text is not the value
    that is actually in the file. Absorption is bounded by the octet width, so the
    value plus four digits is refused again.

    So the regex's verdict on a quad-shaped token is neither a reliable yes nor a
    reliable no once a digit is adjacent, and the containment check — which reads
    the value, not a shape — is the control of record. The LAN branch has no
    quantifier before the last octet, which is why it is blind in both directions
    and why the review leak went unnoticed.
    """
    tailnet = _REAL_VALUES["tailnet"]

    absorbed = address_guard.find_in_text(tailnet + "1")
    assert len(absorbed) == 1, "the pattern stopped absorbing a digit neighbour"
    # Deliberately not interpolating `absorbed` into the message: the matched text
    # is a real value extended by a digit, and a failing assertion lands in the
    # public job log. Same rule as `_containment_offenders`.
    assert absorbed[0][1] != tailnet, "the pattern no longer shifts its read"
    assert address_guard.find_in_text(tailnet + "1234") == [], (
        "absorption is supposed to stop at the octet width; if that changed, the "
        "differential in the test above needs re-measuring"
    )
    # Containment is indifferent to all of it.
    root, rels = _fixture(tmp_path, "shift.md", f"{tailnet}1 and {tailnet}1234")
    assert len(_containment_offenders(root, rels)) == 2


def test_the_containment_tripwire_covers_blobs_the_guard_skips(tmp_path):
    """Bytes-level is load-bearing: a skipped asset must not be a free pass.

    `scan_tracked` returns [] for a `.png` and for an unlisted NUL-bearing blob
    without decoding them (t_3e6d3db7 documents that as a limit). Containment does
    not care, so the tripwire covers the population the regex gate hands over.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    asset = b"\x89PNG\r\n\x1a\n" + b"x" * 8 + _REAL_VALUES["LAN"].encode()
    (repo / "logo.png").write_bytes(asset)
    blob = b"binary\x00\x00" + _REAL_VALUES["tailnet"].encode() + b"\x00tail"
    (repo / "vendor.bin").write_bytes(blob)

    # scan_paths, not scan_tracked: the fixture is a bare directory, not a git repo.
    assert address_guard.scan_paths(repo, ["logo.png", "vendor.bin"]) == [], (
        "the guard's skip rules changed — this differential no longer proves the "
        "tripwire is the wider net; re-measure"
    )
    found = _containment_offenders(repo, ["logo.png", "vendor.bin"])
    assert len(found) == 2, f"tripwire missed a blob the guard skips: {found}"


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
    """A committed hook dir does nothing unless core.hooksPath points at it.

    An UNSET value is a skip, not a failure: ``core.hooksPath`` is machine state
    that bootstrap (or ``install_hooks.py``) writes, so a fresh clone that has
    not been bootstrapped has nothing wrong with its *contents*. A WRONG value is
    a hard failure — that is the signature of a bypass (e.g. pointing at
    ``.git/hooks`` so the guard is never found), which is exactly what must not
    pass quietly on the fleet's own checkouts.
    """
    got = subprocess.run(["git", "-C", str(REPO), "config", "--get", "core.hooksPath"],
                         capture_output=True, text=True)
    value = got.stdout.strip()
    if not value:
        pytest.skip(
            "commit-time address guard is NOT ARMED on this checkout — run "
            "`python3 hscc-bootstrap/install_hooks.py` (bootstrap does it automatically)"
        )
    assert value == ".githooks", (
        f"core.hooksPath is {value!r}, not '.githooks' — the committed guard would "
        "never run. Fix: python3 hscc-bootstrap/install_hooks.py"
    )
