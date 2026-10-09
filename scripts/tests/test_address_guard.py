"""Tests for scripts/address_guard.py — the shared detector (t_ec2c2f95).

The hook tests in ``hscc_daemon/tests/test_precommit_address_hook.py`` cover the
commit-time TRIGGER end to end (real ``git commit`` in a throwaway repo). These
cover the detector's own contract, which both triggers share:

  * pattern verdicts — real LAN, real tailnet, sanctioned fixtures, placeholders
  * the STAGED blob is what gets judged, never the working tree
  * renames / spaces / binary content do not derail the batched read
  * the CLI exit codes the hook depends on (0 clean, 1 leak, 2 cannot-run)
  * the failure text carries offending ``file:line`` AND both placeholders
"""

import subprocess
import re
import shutil
import sys
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "address_guard_under_test", REPO / "scripts" / "address_guard.py")
assert _spec is not None and _spec.loader is not None
address_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(address_guard)

def _addr(*parts):
    """Assemble a forbidden-shaped fixture address at runtime.

    The repo's OWN gate scans every tracked file, including this test, so a
    real-shaped address may not appear here as a contiguous literal — exactly
    the rule this guard enforces on everyone else. The parts are harmless alone.
    """
    return "".join(parts)


REAL_LAN = _addr("192", ".168.88.244")            # the live LAN's NAS host
REAL_TAILNET = _addr("100.64", ".55.7")           # CGNAT, outside the fixture block
LAN_PLACEHOLDER = "10.0.0.244"
TAILNET_PLACEHOLDER = "100.64.0.1"


# ── pattern verdicts (shared by pytest gate and hook) ────────────────────────

@pytest.mark.parametrize("text,expect_hit", [
    (f"mount {REAL_LAN}:/models", REAL_LAN),                       # live LAN
    (f"subnet {REAL_LAN}/24", REAL_LAN),                            # export list
    (f"ping {_addr('192', '.168.88.1')}", _addr("192", ".168.88.1")),   # LAN gateway
    (f"api https://{REAL_TAILNET}:8787", REAL_TAILNET),             # CGNAT, not fixture
    (f"host {_addr('100', '.65.1.1')}", _addr("100", ".65.1.1")),   # CGNAT above 100.64
    (f"host {_addr('100.127', '.9.9')}", _addr("100.127", ".9.9")),  # top of CGNAT
    (f"use {LAN_PLACEHOLDER}", None),                                # LAN placeholder
    (f"use {TAILNET_PLACEHOLDER}", None),                            # tailnet placeholder
    ("fixture 100.64.0.3 and 100.64.0.254", None),                   # sanctioned block
    ("172.16.4.5 and 8.8.8.8", None),                                # out of scope
    ("10.0.0.244/24 export", None),                                  # placeholder subnet
    ("192.168.89.244 is a different subnet", None),                  # not the live LAN
])
def test_find_in_text_verdicts(text, expect_hit):
    hits = address_guard.find_in_text(text)
    if expect_hit is None:
        assert hits == [], f"false positive on {text!r}"
    else:
        assert hits and hits[0][1] == expect_hit, f"missed {expect_hit!r} in {text!r}"


def test_reports_the_line_number_of_every_hit():
    text = "line one\nnothing here\nhost " + REAL_LAN + "\nand " + REAL_TAILNET + "\n"
    assert address_guard.find_in_text(text) == [(3, REAL_LAN), (4, REAL_TAILNET)]


def test_scan_blob_carries_file_line_and_address():
    got = address_guard.scan_blob("docs/audit.md", f"# t\nhost {REAL_LAN}\n".encode())
    assert got == [f"docs/audit.md:2: {REAL_LAN}"]


def test_scan_blob_skips_binaries_and_skip_suffixes():
    """A blob the policy does NOT cover stays silent (t_3e6d3db7 narrowed this).

    The old guard skipped *every* NUL-bearing blob, which is how a committed
    `.pyc` escaped all three gates. Only two classes are silent now: a declared
    asset (SKIP_SUFFIXES) and an unknown-extension binary. A build artefact is
    refused instead — see test_scan_blob_refuses_a_pyc_carrying_a_real_address.
    """
    dirty = b"\x00binary " + REAL_LAN.encode() + b"\x00"
    assert address_guard.scan_blob("x.bin", dirty) == []          # unknown extension
    assert address_guard.scan_blob("logo.png", REAL_LAN.encode()) == []


def test_skip_suffixes_unchanged_from_the_original_gate():
    """The gate's original noise list must not silently shrink."""
    assert address_guard.SKIP_SUFFIXES == (
        ".png", ".jpg", ".jpeg", ".pdf", ".ico", ".zip", ".gz", ".xcuserstate")


# ── build artefacts: refused on the name, not the content (t_3e6d3db7) ───────

def _pyc_like(addr):
    """A `.pyc`-shaped blob: NUL-framed, with the address in a string constant.

    What a real one looks like on the matters-here axis: the compiled constants
    section holds the source's string literals verbatim, so the address is in the
    blob in exactly the form the text scan cannot see. NUL bytes either side so
    the OLD guard would have returned [] and stayed silent about it.

    This is the WEAK mutant. The strong one — `test_a_genuine_compiled_pyc_...`,
    which compiles real source at runtime — is what proves the refusal is not
    merely a content-scan in disguise.
    """
    return b"\xe3\r\r\n\x00\x00\x00\x00" + b"\x00" + addr.encode() + b"\x00\x00"


@pytest.fixture()
def genuine_leaky_pyc(tmp_path):
    """A REAL `.pyc`, compiled by this interpreter, from source that carried an
    address — assembled at runtime so the repo's own guard stays clean.

    Why this fixture exists rather than a hand-built blob: a mutant made by
    splicing the dotted string into NUL filler is MATCHED by the current pattern
    (both boundaries land on non-word bytes) and so proves nothing. A genuinely
    compiled `.pyc` is not: marshal writes the string constant with no delimiter
    before the next interned name, so the character immediately AFTER the address
    is a word character, the trailing `\\b` fails, and FORBIDDEN finds 0 while a
    naive `192\\.168\\.88\\.\\d{1,3}` still finds 1. Measured, not assumed: leading
    char in the compiled blob is U+000E (non-word), trailing is 'N' (word), and
    relaxing the trailing boundary is what restores the match.
    """
    src = tmp_path / "leaky_source.py"
    src.write_text("NAS = " + repr(REAL_LAN) + "\n", encoding="utf-8")
    import py_compile
    py_compile.compile(str(src), cfile=str(tmp_path / "leaky.pyc"), doraise=True)
    blob = (tmp_path / "leaky.pyc").read_bytes()
    assert b"\0" in blob, "a real .pyc is NUL-bearing — that is the whole premise"
    return blob


def test_a_genuine_compiled_pyc_defeats_the_text_detector_so_refusal_is_required(
        genuine_leaky_pyc):
    """THE reason this card's policy is 'refuse the type', not 'scan the bytes'.

    Three assertions, each measured rather than argued:

      1. the address really is inside the compiled blob — a naive scan sees it;
      2. the SHIPPING FORBIDDEN pattern does NOT. Its `\\b` boundaries assume the
         address is delimited, and undelimited binary framing breaks that: marshal
         puts a word character immediately AFTER the constant, so the trailing
         `\\b` fails. Extract-and-scan would inherit exactly this blind spot;
      3. the name-based refusal catches it anyway, because it never looked at the
         bytes at all.

    This is the case an extract-and-scan policy cannot pass, and it is why that
    option was declined rather than merely deferred.
    """
    blob = genuine_leaky_pyc
    assert REAL_LAN.encode() in blob, "the address is in the blob"
    text = blob.decode("utf-8", errors="ignore")
    # (1) the naive scan sees it... (pattern assembled at runtime for the same
    # reason the addresses are: the repo's own single-implementation pin scans
    # this file and must not find a second copy of the detector's literal.)
    naive = re.compile(_addr("192", r"\.168\.88\.\d{1,3}"))
    assert naive.findall(text), "naive scan should see it"
    # (2) ...the shipping detector does not
    assert address_guard.find_in_text(text) == [], (
        "FORBIDDEN unexpectedly matched the compiled blob — if this ever goes red,"
        " the boundary-context finding is stale and extract-and-scan becomes"
        " arguable again; re-measure before believing it")
    # (3) the refusal is blind to that blindness, and still refuses
    got = address_guard.scan_blob("hscc-provision/__pycache__/hscc.cpython-313.pyc", blob)
    assert got == ["hscc-provision/__pycache__/hscc.cpython-313.pyc:0: "
                   "build artefact must not be tracked"]
    # ...and an un-listed extension is still blind to the SAME bytes, which is the
    # honest limit of this card: the type list is what closes it, not the scanner.
    assert address_guard.scan_blob("vendor/blob.bin", blob) == []


def test_scan_blob_refuses_a_pyc_carrying_a_real_address():
    """THE regression this card exists for.

    Before the policy, `b"\\0" in data` returned [] and the address rode through
    the hook, the pytest gate and the CI backstop — all three call this function.
    """
    got = address_guard.scan_blob("scripts/__pycache__/hscc.cpython-313.pyc",
                                  _pyc_like(REAL_LAN))
    assert got, "a committed .pyc must not be invisible to the guard"
    assert got[0].startswith("scripts/__pycache__/hscc.cpython-313.pyc:0:")


@pytest.mark.parametrize("rel", [
    "pkg/mod.pyc",                    # the case that actually happened here
    "pkg/__pycache__/mod.cpython-313.pyc",
    "vendor/thing.pyo",
    "native/helper.so",
    "native/helper.dylib",
    "native/helper.dll",
    "build/obj.o",
    "build/lib.a",
    "java/Thing.class",
    "dist/pkg-1.0-py3-none-any.whl",
])
def test_scan_blob_refuses_build_artefacts_even_when_clean(rel):
    """Content-independent: the offence is that the path is tracked at all.

    The guard cannot tell a clean .pyc from a leaking one without the
    extract-and-scan machinery the policy declines to build, so a rule enforced
    only when a leak is provable is a rule that leaks.
    """
    assert address_guard.scan_blob(rel, b"harmless text") == [
        f"{rel}:0: build artefact must not be tracked"]
    assert address_guard.scan_blob(rel, _pyc_like(REAL_TAILNET))


def test_scan_blob_refuses_an_artefact_regardless_of_case_and_separators():
    assert address_guard.is_build_artefact("PKG/MOD.PYC")
    assert address_guard.is_build_artefact("pkg\\__pycache__\\mod.pyc")
    assert address_guard.is_build_artefact("pkg/__pycache__/x.PYC")
    # and the negative controls that make the list mean something
    assert not address_guard.is_build_artefact("docs/pycall.md")
    assert not address_guard.is_build_artefact("data/alpine.software.json")
    assert not address_guard.is_build_artefact("native/source.c")
    assert not address_guard.is_build_artefact("assets/logo.png")


def test_legitimate_binary_asset_is_still_accepted():
    """The tracked binary population must survive the new rule untouched.

    Measured at 3c20990c the tracked tree has exactly two NUL-bearing files, both
    .png, and this is the guard against a policy that "fixes" the .pyc gap by
    breaking a real asset.
    """
    assert not address_guard.is_build_artefact("assets/hscc.png")
    assert address_guard.scan_blob("assets/hscc.png", b"\x89PNG\r\n\x1a\n\x00\x00") == []
    assert address_guard.scan_blob("vendor/blob.dat", b"\x00\x01\x02") == []


# ── terminal name bytes (t_ecc3f190) ─────────────────────────────────────────
#
# The reviewer's probe at 104eac34: `git add -A --force` tracks `evil.pyc ` and
# BOTH gates then return []. Not an accident class — no compiler emits such a
# name — but a name-based control is only as good as the string it compares.

@pytest.mark.parametrize("rel,expect", [
    # the four spellings the reviewer measured, plus the ones the same fix costs
    ("evil.pyc ", True),                     # trailing space
    ("evil.pyc.", True),                     # trailing dot
    ("dir/evil.pyc\t", True),                # trailing tab
    ("evil.pyc\x00", True),                  # NUL in the name
    ("evil.pyc \n", True),                   # trailing space AND newline
    ("evil.pyc \t .", True),                 # several, mixed
    ("evil.pyc\u00a0", True),                # trailing NBSP — paths arrive decoded
    ("evil.PYc.", True),                     # case + terminal byte together
    ("native/helper.so.", True),
    # the SEGMENT tier has the same hole: here the artefact is the DIRECTORY, and
    # a whole-path trim would never see its trailing byte
    ("pkg/__pycache__ /m.dat", True),
    ("pkg/ __pycache__/m.dat", True),        # leading byte defeats the exact match
    # mid-name bytes are NOT name bytes — these were refused before and still are
    ("docs/ev il.pyc", True),
    ("leak.pyc", True),
    ("leak.PYC", True),
    ("PKG/__pycache__/m.pyc", True),
    ("PKG/__PYCACHE__/m.dat", True),
    # and the negatives that keep the list meaning something. `README.` is the
    # case the card asked me to protect: a leaf that legitimately ends in `.`
    # must not become newly blocked, so the trim applies to the ARTEFACT test
    # only, never as a blanket "normalise the path" rewrite.
    ("README.", False),
    ("docs/notes.", False),
    ("Makefile.", False),
    ("docs/pycall.md", False),
    ("assets/logo.png", False),
    ("docs/evil.pyc.txt", False),            # documented limit #3, unchanged here
])
def test_terminal_name_bytes_do_not_dodge_the_refusal(rel, expect):
    assert address_guard.is_build_artefact(rel) is expect, (
        f"{rel!r}: expected refusal={expect}; the name tiers must compare "
        f"_name_key(), not the raw string")


def test_normalisation_is_applied_where_it_can_only_widen_a_refusal(monkeypatch):
    """The asymmetry, pinned — the part of this fix that could have opened a hole.

    `_name_key` is used by the refusal and NOT by the skip list or the hatch, and
    the direction of that choice is the whole safety argument: normalising a
    REFUSAL can only catch more; normalising a SKIP or a WAIVER would catch less.
    So a trailing-byte name must never match a hatch entry (it falls through to
    the refusal — fails closed), and `x.png ` must stay scanned rather than being
    skipped as a "declared asset".
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"vendor/__pycache__/fixture.dat"}))
    # exact entry: waived, as the segment tier promises
    assert not address_guard.is_build_artefact("vendor/__pycache__/fixture.dat")
    # the same path with a terminal byte: does NOT match the waiver -> refused
    assert address_guard.is_build_artefact("vendor/__pycache__/fixture.dat."), (
        "a terminal name byte reached the hatch — the waiver is now wider than "
        "the path someone reviewed")
    assert address_guard.is_build_artefact("vendor/__pycache__ /fixture.dat")
    # and a waiver that itself names a compiled artefact still waives nothing
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"vendor/leak.pyc "}))
    assert address_guard.is_build_artefact("vendor/leak.pyc ")
    # the skip list stays on the raw path: `x.png ` is scanned, not skipped
    assert address_guard.is_skipped_asset("a.png") is True
    assert address_guard.is_skipped_asset("a.png ") is False
    leaky = ("remount " + REAL_LAN + ":/models\n").encode()
    assert address_guard.scan_blob("a.png ", leaky) == [f"a.png :1: {REAL_LAN}"], (
        "a terminal name byte turned a scannable file into an unread one")


def test_scan_blob_names_the_raw_path_so_the_two_gates_name_one_file():
    """The verdict carries the REAL path, not the normalised one.

    The fix the report tells you to run is `git rm --cached <path>`; a verdict
    that printed `evil.pyc` for a file named `evil.pyc ` would name a file that
    does not exist, and the hook and the CI job would then describe the same tree
    with two different strings — the agreement failure t_3e6d3db7 §4 is about.
    """
    rel = "pkg/evil.pyc "
    assert address_guard.scan_blob(rel, b"\x00clean\x00") == [
        "pkg/evil.pyc :0: build artefact must not be tracked"]
    assert address_guard.artefact_offender("evil.pyc.") == (
        "evil.pyc.:0: build artefact must not be tracked")


def test_no_tracked_path_changes_verdict_under_the_normalisation():
    """The blast radius of a security control, measured instead of argued.

    The card asked for this before choosing the normalisation: a rule that newly
    refuses a path someone legitimately tracks is a rule that gets `--no-verify`'d
    on its first real use. Population measured at this base: 1103 tracked files,
    0 trailing-whitespace names, 0 trailing-dot names, 0 NUL-bearing names, and 0
    paths whose verdict the trim changes. Re-measured here on every run, so if a
    future commit adds `docs/notes.` this tells us in CI rather than at a commit.
    """
    rels = address_guard.tracked_paths(REPO)
    assert rels, "no tracked files — this gate stopped measuring anything"

    def legacy_refusal(r):
        """What the pre-t_ecc3f190 code decided: suffix/segment on the RAW name."""
        low = r.lower()
        return (low.endswith(address_guard.BUILD_ARTEFACT_SUFFIXES)
                or "__pycache__" in low.replace("\\", "/").split("/"))

    newly_refused = [r for r in rels if address_guard.is_build_artefact(r)
                     and not legacy_refusal(r)]
    # Paths where the TRIM (not the case-fold or the backslash fold) changed the
    # name — the only ones where this control touches an existing tracked file.
    folded = [r for r in rels
              if address_guard._name_key(r) != r.lower().replace("\\", "/")]
    assert newly_refused == [], f"newly refused tracked paths: {newly_refused!r}"
    for r in folded:
        assert not address_guard.is_skipped_asset(r), (
            f"{r!r} is a declared asset whose name the trim touches")


def test_a_surrogate_escape_in_a_path_never_raises_on_the_commit_path():
    """Crash safety for the new per-character trim, on the shape git really gives us.

    `tracked_paths()` / `staged_paths()` decode `git ... -z` output with
    `surrogateescape`, so any non-UTF-8 byte in a real filename arrives as a lone
    surrogate — `b"docs/caf\\xe9.pyc "` becomes `'docs/caf\\udce9.pyc '`. `_name_key`
    now looks at every character of every segment (`.isspace()`), so this is the
    new code's own risk, and a raise here is a traceback on the commit path —
    which is precisely how `--no-verify` gets used and the guard stops existing.
    """
    for raw in (b"docs/caf\xe9.pyc ", b"\xff\xfe.pyc", b"docs/\xc3\x28bad.pyc",
                b"docs/caf\xe9.md", b"pkg/__pycache__\xe9/m.dat"):
        rel = raw.decode("utf-8", "surrogateescape")
        try:
            verdict = address_guard.is_build_artefact(rel)
        except Exception as exc:                      # noqa: BLE001 - the point
            pytest.fail(f"is_build_artefact({raw!r}) raised {type(exc).__name__}: {exc}")
        assert verdict is True or verdict is False
    # and the surrogate case still refuses: the byte is mid-segment, the suffix is intact
    assert address_guard.is_build_artefact(b"docs/caf\xe9.pyc ".decode("utf-8", "surrogateescape"))
    # a non-artefact with a surrogate is still not refused
    assert not address_guard.is_build_artefact(b"docs/caf\xe9.md".decode("utf-8", "surrogateescape"))


def test_a_trailing_byte_name_does_not_make_the_tracked_scan_silent(tmp_path):
    """Both gates over a real index entry, without a hook in the way.

    `scan_tracked` is what the CI job and the pytest gate run, and it takes its
    artefact verdict from `tracked_paths()` — the same `git ls-files -z` output
    the reviewer measured as `b'evil.pyc \\n'`. This pins that the refusal fires
    from the tracked population alone, with no working-tree bytes required.
    """
    r = tmp_path / "tracked"
    r.mkdir()
    assert _git(r, "init", "-q", "-b", "main").returncode == 0
    (r / "evil.pyc ").write_bytes(b"\xe3\r\n\x00\x00" + REAL_LAN.encode() + b"\x00")
    assert _git(r, "add", "-A", "--force").returncode == 0
    assert "evil.pyc " in address_guard.tracked_paths(r)
    assert address_guard.scan_tracked(r) == [
        "evil.pyc :0: build artefact must not be tracked"]
    # and the same blob via the staged gate, so neither invocation is the one
    # that depends on having read the bytes
    assert address_guard.scan_staged(r) == [
        "evil.pyc :0: build artefact must not be tracked"]



def test_a_compiled_artefact_suffix_is_never_waivable_by_the_hatch(monkeypatch):
    """The hatch cannot reach the tier whose bytes the scanner cannot read.

    Round-1 review (t_3e6d3db7) found the claim "widening the hatch can never
    hide a leak" was FALSE while the hatch could waive a `.pyc`: the text scan is
    blind to a compiled blob (see
    `test_a_genuine_compiled_pyc_defeats_the_text_detector_so_refusal_is_required`),
    so waiving the refusal waived detection too. The fix is structural, not
    wording: the suffix tier in `is_build_artefact` ignores the hatch, so every
    compiled shape below stays refused from the hatch alone.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS", frozenset(
        ["native/bundled.so", "pkg/leak.pyc", "pkg/leak.pyo", "native/x.pyd",
         "build/obj.o", "build/lib.a", "native/x.dylib", "native/x.dll",
         "java/Thing.class", "dist/pkg-1.0-py3-none-any.whl"]))
    for rel in address_guard.ALLOWED_BINARY_PATHS:
        assert address_guard.scan_blob(rel, b"\x00clean\x00") == [
            f"{rel}:0: build artefact must not be tracked"], (
            f"{rel} is on the hatch and got through: a compiled artefact the text"
            " scan cannot read is now tracked and unscanned")


def test_a_genuinely_compiled_pyc_on_the_hatch_is_still_refused(
        monkeypatch, genuine_leaky_pyc):
    """The exact probe the round-1 review ran, inverted into a requirement.

    Before the two-tier hatch, `scan_blob` returned `[]` here — silent — because
    the hatch waived the refusal and the text scan was blind to the blob. It must
    now name the path, and the case is worthless unless the text scan is *still*
    blind, which the first assertion re-pins from the inside rather than trusting.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS", frozenset({"pkg/leak.pyc"}))
    assert address_guard.find_in_text(
        genuine_leaky_pyc.decode("utf-8", errors="ignore")) == [], (
        "FORBIDDEN now sees a compiled blob: the hatch's justification changed,"
        " re-measure before touching the two-tier rule")
    got = address_guard.scan_blob("pkg/leak.pyc", genuine_leaky_pyc)
    assert got == ["pkg/leak.pyc:0: build artefact must not be tracked"], (
        "the hatch let a compiled artefact through — the blind blob is now tracked"
        " and unscanned, which is the leak this card exists to prevent")


def test_hatch_waives_only_the_segment_refusal_and_still_scans(monkeypatch):
    """What the hatch *does* buy: a `__pycache__`-segment path is tracked, decoded
    and text-scanned even though it is full of NUL bytes and would be a skipped
    asset by extension. "This path may be tracked", never "this path may be unread".
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"vendor/__pycache__/fixture.dat", "docs/reference.pdf"}))
    # clean -> tracked, no verdict
    assert address_guard.scan_blob("vendor/__pycache__/fixture.dat", b"\x00clean\x00") == []
    # leaking + NUL-bearing -> the address is still found (the scan applies to a
    # path the hatch has waived, NULs and all)
    leaky = b"\x00\x00host " + REAL_LAN.encode() + b"\x00\x00"
    assert address_guard.scan_blob("vendor/__pycache__/fixture.dat", leaky) == [
        f"vendor/__pycache__/fixture.dat:1: {REAL_LAN}"]
    # and the waiver is exactly as wide as the path on the hatch
    assert address_guard.scan_blob("vendor/__pycache__/other.dat", b"\x00clean\x00") == [
        "vendor/__pycache__/other.dat:0: build artefact must not be tracked"]


def test_hatch_does_not_bypass_the_skip_suffix_shortcut_silently(monkeypatch):
    """A hatch entry must not turn a declared asset into an unread, unscanned file.

    Same guarantee, the other hole the review's logic exposes: `SKIP_SUFFIXES` is a
    second way a path escapes the scan. An allowlisted path is decoded even when its
    extension is on that list, so a reviewer adding one entry can never — by
    accident or on purpose — get "tracked AND never read" for a path they listed.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"docs/reference.pdf"}))
    leaky = "remount " + REAL_LAN + ":/models\n"
    assert address_guard.scan_blob("docs/reference.pdf", leaky.encode()) == [
        f"docs/reference.pdf:1: {REAL_LAN}"]
    # without the hatch the same path is a declared asset and stays unread
    assert address_guard.scan_blob("docs/other.pdf", leaky.encode()) == []


def test_scan_paths_and_scan_blob_agree_on_a_hatched_path(tmp_path, monkeypatch):
    """`scan_paths` has its own pre-read skip shortcut; it must honour the hatch.

    The two gates re-implement the same silence rules in two places, which is
    exactly where the hook and the CI job drift apart. Pin the pair on the one
    input where they could disagree: an allowlisted path with a skipped extension.
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS",
                        frozenset({"docs/reference.pdf"}))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "reference.pdf").write_text(
        "remount " + REAL_LAN + ":/models\n", encoding="utf-8")
    expected = [f"docs/reference.pdf:1: {REAL_LAN}"]
    assert address_guard.scan_paths(tmp_path, ["docs/reference.pdf"]) == expected
    assert address_guard.scan_blob(
        "docs/reference.pdf",
        (tmp_path / "docs" / "reference.pdf").read_bytes()) == expected


def test_scan_paths_refuses_a_tracked_artefact_even_if_unreadable(tmp_path):
    """The tracked gate must not depend on reading the bytes.

    A path in HEAD's index is a tracked artefact whether or not the working tree
    carries it (deleted-but-tracked) or it cannot be opened. The name is verdict
    enough, so the pre-read skip shortcuts must not run for it.
    """
    rel = "hscc-provision/__pycache__/hscc.cpython-313.pyc"
    assert address_guard.scan_paths(tmp_path, [rel]) == [
        f"{rel}:0: build artefact must not be tracked"]
    # present-but-unreadable via the injected reader, same verdict
    def boom(_p):
        raise OSError("EIO")
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_bytes(_pyc_like(REAL_LAN))
    assert address_guard.scan_paths(tmp_path, [rel], read=boom) == [
        f"{rel}:0: build artefact must not be tracked"]


def test_cli_blocks_a_staged_pyc_carrying_a_real_address(tmp_path):
    """The card's acceptance case, through the CLI the hook actually calls."""
    r = _repo(tmp_path)
    pyc = r / "scripts" / "__pycache__"
    pyc.mkdir(parents=True)
    (pyc / "hscc.cpython-313.pyc").write_bytes(_pyc_like(REAL_LAN))
    _git(r, "add", "-A")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                           "--staged", "--repo", str(r)], capture_output=True)
    assert proc.returncode == 1
    err = proc.stderr.decode()
    assert "hscc.cpython-313.pyc" in err
    assert "git rm --cached" in err


def test_cli_tracked_flags_a_committed_pyc_even_after_a_scrubbed_worktree(tmp_path):
    """`--tracked` must name the artefact from the INDEX, not the working tree.

    Same failure mode as the 2026-08-30 audit, one class over: the file may be
    gone (or clean) on disk while HEAD still carries the blob.
    """
    r = _repo(tmp_path)
    pyc = r / "pkg" / "__pycache__"
    pyc.mkdir(parents=True)
    (pyc / "m.cpython-313.pyc").write_bytes(_pyc_like(REAL_TAILNET))
    _git(r, "add", "-A")
    assert _git(r, "commit", "-q", "-m", "oops", "--no-verify").returncode == 0
    # Working tree scrubbed: the bytes are gone, the tracked path is not.
    shutil.rmtree(pyc)
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                           "--tracked", "--repo", str(r)], capture_output=True)
    assert proc.returncode == 1, proc.stderr.decode()
    assert "pkg/__pycache__/m.cpython-313.pyc" in proc.stderr.decode()


def test_report_names_the_artefact_and_the_removal_command():
    text = address_guard.report(["pkg/__pycache__/m.pyc:0: build artefact must not be tracked"])
    assert "pkg/__pycache__/m.pyc" in text
    assert "git rm --cached" in text, "the fix for an artefact is untracking, not scrubbing"
    assert "PUBLIC" in text
    # Mixed input: both classes explained, and an address still gets placeholders.
    mixed = address_guard.report([f"a.md:3: {REAL_LAN}", "m.pyc:0: build artefact must not be tracked"])
    assert "a.md:3" in mixed and "10.0.0.x" in mixed and "100.64.0.1" in mixed
    assert "git rm --cached" in mixed


# ── staged content, not working-tree content ─────────────────────────────────

def _git(repo, *args, input_bytes=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.invalid",
                           "-C", str(repo), *args], capture_output=True, input=input_bytes)


def _repo(tmp_path):
    r = tmp_path / "guard"
    r.mkdir()
    assert _git(r, "init", "-q", "-b", "main").returncode == 0
    (r / "a.md").write_text("clean\n", encoding="utf-8")
    _git(r, "add", "-A")
    assert _git(r, "commit", "-q", "-m", "seed").returncode == 0
    return r


def test_staged_paths_excludes_deletions_and_includes_new_files(tmp_path):
    r = _repo(tmp_path)
    (r / "new.md").write_text("new\n", encoding="utf-8")
    (r / "a.md").unlink()
    _git(r, "add", "-A")
    assert address_guard.staged_paths(r) == ["new.md"]


def test_staged_blobs_reads_the_index_not_the_worktree(tmp_path):
    r = _repo(tmp_path)
    (r / "a.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _git(r, "add", "a.md")
    (r / "a.md").write_text("scrubbed 10.0.0.x\n", encoding="utf-8")   # operator scrubs
    assert address_guard.staged_blobs(r, ["a.md"])["a.md"].decode() == f"host {REAL_LAN}\n"
    assert address_guard.scan_staged(r) == [f"a.md:1: {REAL_LAN}"]


def test_staged_blobs_survives_awkward_path_names(tmp_path):
    """Spaces, unicode, and a NEWLINE in the name (git allows it).

    A newline-in-path file is the only known way to make a line-framed
    `cat-file --batch` stream skip a file; it goes through the per-path
    `cat-file blob` path instead and must still be caught.
    """
    r = _repo(tmp_path)
    (r / "docs").mkdir()
    spaced = "docs/a b.md"
    uni = "docs/наас.md"
    newline_name = "docs/we\nird.md"
    for name in (spaced, uni, newline_name):
        (r / name).write_text(f"host {REAL_TAILNET}\n", encoding="utf-8")
    _git(r, "add", "-A")
    got = sorted(address_guard.scan_staged(r))
    assert got == sorted([f"{spaced}:1: {REAL_TAILNET}",
                          f"{uni}:1: {REAL_TAILNET}",
                          f"{newline_name}:1: {REAL_TAILNET}"]), got


def test_scan_staged_is_empty_on_a_clean_tree(tmp_path):
    r = _repo(tmp_path)
    assert address_guard.staged_paths(r) == []
    assert address_guard.scan_staged(r) == []


def test_scan_staged_on_a_repo_without_head(tmp_path):
    """First commit (no HEAD yet): the whole index is the staged tree."""
    r = tmp_path / "fresh"
    r.mkdir()
    assert _git(r, "init", "-q", "-b", "main").returncode == 0
    (r / "leak.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _git(r, "add", "-A")
    assert address_guard.scan_staged(r) == ["leak.md:1: " + REAL_LAN]


def test_scan_tracked_matches_the_pytest_gate_population(tmp_path):
    r = _repo(tmp_path)
    (r / "sub").mkdir()
    (r / "sub" / "b.md").write_text(f"{REAL_TAILNET}\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "second")
    (r / "untracked.md").write_text("not tracked\n", encoding="utf-8")
    offenders = address_guard.scan_tracked(r)
    assert offenders == ["sub/b.md:1: " + REAL_TAILNET]
    assert "untracked.md" not in "\n".join(address_guard.tracked_paths(r))


# ── report text + CLI ────────────────────────────────────────────────────────

def test_report_names_offenders_and_placeholders():
    text = address_guard.report([f"a.md:7: {REAL_LAN}"])
    assert "a.md:7" in text
    assert "10.0.0.x" in text and "100.64.0.1" in text
    assert "PUBLIC" in text


def test_cli_exit_codes(tmp_path):
    r = _repo(tmp_path)

    def run(args, repo=r):
        return subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                               *args, "--repo", str(repo)], capture_output=True)

    assert run(["--staged"]).returncode == 0                       # clean
    (r / "leak.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _git(r, "add", "leak.md")
    proc = run(["--staged"])
    assert proc.returncode == 1                                    # leak
    assert f"leak.md:1: {REAL_LAN}" in proc.stderr.decode()
    (r / "leak.md").unlink()
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "x", "--no-verify")
    assert run(["--tracked"]).returncode == 0                       # clean tracked
    assert run(["--staged"], repo=tmp_path).returncode == 2         # not a repo -> cannot run


def test_cli_failure_text_is_actionable(tmp_path):
    r = _repo(tmp_path)
    (r / "notes.txt").write_text(f"subnet {REAL_LAN}/24\n", encoding="utf-8")
    _git(r, "add", "notes.txt")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "address_guard.py"),
                           "--staged", "--repo", str(r)], capture_output=True)
    err = proc.stderr.decode()
    assert "notes.txt:1" in err and "10.0.0.x" in err and "100.64.0.1" in err


# keep pytest import light: imported late so the module-under-test load order is explicit
import pytest  # noqa: E402
