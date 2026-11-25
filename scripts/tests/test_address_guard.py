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


REAL_LAN = _addr("192", ".168.88.244")            # the old live LAN's NAS host
REAL_LAN10 = _addr("10", ".0.0.244")             # the current live LAN's orch host
REAL_LAN10_NAS = _addr("10", ".0.0.249")         # the current live LAN's NAS
REAL_TAILNET = _addr("100.64", ".55.7")           # CGNAT, outside the fixture block
LAN_PLACEHOLDER = "10.0.0.x"
TAILNET_PLACEHOLDER = "100.64.0.1"


# ── pattern verdicts (shared by pytest gate and hook) ────────────────────────

@pytest.mark.parametrize("text,expect_hit", [
    (f"mount {REAL_LAN}:/models", REAL_LAN),                       # old live LAN
    (f"subnet {REAL_LAN}/24", REAL_LAN),                            # export list
    (f"ping {_addr('192', '.168.88.1')}", _addr("192", ".168.88.1")),   # LAN gateway
    (f"mount {REAL_LAN10}:/models", REAL_LAN10),                    # current live LAN
    (f"node {REAL_LAN10_NAS}", REAL_LAN10_NAS),                     # current LAN NAS
    (f"api https://{REAL_TAILNET}:8787", REAL_TAILNET),             # CGNAT, not fixture
    (f"host {_addr('100', '.65.1.1')}", _addr("100", ".65.1.1")),   # CGNAT above 100.64
    (f"host {_addr('100.127', '.9.9')}", _addr("100.127", ".9.9")),  # top of CGNAT
    (f"use {LAN_PLACEHOLDER}", None),                                # LAN placeholder
    (f"use {TAILNET_PLACEHOLDER}", None),                            # tailnet placeholder
    ("fixture 100.64.0.3 and 100.64.0.254", None),                   # sanctioned block
    ("172.16.4.5 and 8.8.8.8", None),                                # out of scope
    ("192.0.2.244/24 export", None),                                  # documentation range
    ("10.0.0.x/24 export", None),                                     # placeholder subnet
    ("192.168.89.244 is a different subnet", None),                  # not the old live LAN
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
    # (2) ...the shipping detector does not — for the ADDRESS class, which is the
    # one whose `\b` boundaries assume a delimited, text-shaped occurrence. Asked
    # as `address_hits` rather than `find_in_text` since t_25f7a806 added the
    # identity branch, and the reason is measured rather than argued: this blob
    # embeds the test's own tmp path (`pytest-of-<user>`), whose `-` and `/` are
    # delimiters the identity branch accepts, so FORBIDDEN now returns exactly that
    # one hit. It is a finding, not the blindness this pin is about — the address
    # class stays blind, and the address class is what the refusal exists for.
    assert address_guard.address_hits(text) == [], (
        "FORBIDDEN's ADDRESS branch unexpectedly matched the compiled blob — if this"
        " ever goes red, the boundary-context finding is stale and extract-and-scan"
        " becomes arguable again; re-measure before believing it")
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
    assert address_guard.address_hits(
        genuine_leaky_pyc.decode("utf-8", errors="ignore")) == [], (
        "FORBIDDEN's ADDRESS branch now sees a compiled blob: the hatch's"
        " justification changed, re-measure before touching the two-tier rule"
        " (the identity branch may legitimately match a name in the blob — see"
        " test_a_genuine_compiled_pyc_defeats_the_text_detector)")
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


# ── operator home paths (t_3480956c) ─────────────────────────────────────────
#
# PR #724 reached origin with an interpreter path in two docs files. Not a review
# failure — the guard had no home-path concept, so `--tracked`, `--staged` and a
# reviewer grepping for addresses were all equally blind. These pin the class both
# ways: a concrete user trips, and BOTH documented replacements do not.
#
# `"/Users"` and `"/home"` as bare literals are safe to write here (the pattern
# needs a user component after the slash, and the source has a quote instead); the
# user names are assembled so this tracked file cannot be its own offence.

_HOME_ROOTS = ("/Users", "/home")
_REAL_USER = "hscc" + "-realuser"          # non-sanctioned, and nobody's real name


def _home(root, user=_REAL_USER, rest=""):
    return f"{root}/{user}{rest}"


@pytest.mark.parametrize("text,expect_hit", [
    # trips: every concrete user, whatever the context around it
    (f"venv {_home('/Users', rest='/.hermes/venv/bin/python')}", True),
    (f"home = {_home('/home')}", True),
    (f'quoted "{_home("/Users", rest="/x")}"', True),       # no \b: quotes do not hide it
    (f"link ]({_home('/home', rest='/deploy/run.sh')})", True),   # no \b: parens do not hide it
    (_home("/Users"), True),                                      # bare, no tail
    (f"/Users/{'hscc-fixture'}-2/x", True),                       # no allowance by prefix
    # clean: the two documented replacements, and the out-of-scope shapes
    ("socket /Users/<operator>/.hermes/gateway.sock", None),
    ("suite /home/<operator>/.hscc/suite", None),
    ('json {"home": "/Users/<operator>"}', None),
    (f"/Users/{'hscc-fixture'}/.venv/bin/python", None),
    (f"/home/{'hscc-fixture'}/work", None),
    ("/home/runner/work/_temp/pytest-of-runner/pytest-0", None),   # GitHub's CI account
    ("~/.hermes/gateway.sock", None),
    ("/usr/local/bin/python3", None),
    ("/private/var/folders/9x/T/pytest-of-someone", None),
    # clean: a user component that cannot be an account (t_34de41c3). Assembled,
    # because writing the ellipsis form literally would plant the finding here.
    ("/Users/" + "." * 3 + "/dev/eco", None),                    # prose ellipsis
    ("/home/" + "." + "local/bin/hscc", None),                   # a dotfile, not a home
    # trips: a digit-led account name is still a concrete user
    (f"/Users/{'2'}fac/x", True),
])
def test_find_in_text_home_verdicts(text, expect_hit):
    hits = address_guard.home_hits(text)
    if expect_hit is None:
        assert hits == [], f"false positive on a home path: {text!r}"
    else:
        assert hits, f"missed the home path in {text!r}"
        assert address_guard.is_home_occurrence(hits[0][1])


def test_a_newline_escaped_home_path_still_trips_the_branch():
    """THE reason this branch has a lookahead and not a lookbehind (t_34de41c3).

    A home path written after a ``\\n`` escape inside a string literal has a WORD
    CHARACTER in front of the slash in the file bytes — the `n` of the escape. Any
    left-boundary lookbehind over the path-name class (``(?<![A-Za-z0-9._-])``,
    the shape proposed for the relative-path false positives) therefore declines
    it, and the hook, the pytest gate and the CI backstop all pass a real leak
    committed as ``".../bin/zsh\\n/Users/<user>/.ssh/id_rsa\\n"``.

    Same failure family as the ``.pyc`` trap that forces the build-artefact
    refusal, and the reason ``\\b`` is refused here. If a future change adds a
    left boundary to ``_HOME_BRANCH``, this test is the one that goes red.
    """
    planted = "/bin/zsh\\n" + _home("/Users", rest="/.ssh/id_rsa") + "\\n"
    assert "\\n" in planted, "the escape must be the two bytes backslash-n"
    hits = address_guard.home_hits(planted)
    assert hits, f"a \\n escape must not hide a home path: {planted!r}"
    assert hits[0][1].startswith(_home("/Users", rest="/.ssh")), hits


def test_a_home_path_after_a_word_char_still_trips_the_branch():
    """The quote/paren/start-of-line family, widened to the adjacency a
    lookbehind would eat — no context may hide the branch (t_3480956c's rule,
    re-pinned for t_34de41c3)."""
    for ctx, label in (
            (f'"{_home("/Users", rest="/x")}"', "double quote"),
            (f"({_home('/home', rest='/x')})", "paren"),
            (_home("/Users", rest="/x"), "start of line"),
            (f"cmd --home={_home('/home', rest='/x')}", "after ="),
            (f"file://{_home('/Users', rest='/x')}", "after a URL scheme"),
            (f"| {_home('/home', rest='/x')} |", "Markdown table cell"),
            (f"<code>{_home('/Users', rest='/x')}</code>", "HTML code element"),
    ):
        assert address_guard.home_hits(ctx), f"{label}: the branch went blind"


def test_an_address_never_hides_behind_a_home_path_on_the_same_line(monkeypatch):
    """THE interaction hazard of making one class advisory, pinned in both orders.

    With one match per line, a line holding a home path and a real LAN address
    would have reported only the home path while the home class was advisory — so
    the blocking address would leave the gate unseen and the commit would pass.
    `find_in_text` returns every match for that reason, and this is the test that
    would have caught it.

    t_34de41c3 made the home class blocking, so the hazard changed shape and is
    now pinned in BOTH postures: the address must reach the blocking subset while
    home is filtered out (advisory) *and* while home blocks alongside it.
    """
    addr = "10" + ".0.0.5"
    path = _home("/Users", rest="/x")
    addr_only = f"d.md:1: {addr}"
    for line, label in ((f"host {addr} and {path}", "address first"),
                        (f"{path} and host {addr}", "home path first")):
        assert [v for _, v in address_guard.address_hits(line)] == [addr], label
        assert [v for _, v in address_guard.home_hits(line)] == [path], label
        offenders = address_guard.scan_blob("d.md", line.encode())
        assert len(offenders) == 2, (label, offenders)
        # enforced (the shipped posture since t_34de41c3): both block.
        assert addr_only in address_guard.blocking(offenders), (
            f"{label}: the address escaped the blocking subset")
        assert len(address_guard.blocking(offenders)) == 2, label
        # advisory (reachable only by editing the constant now): the address
        # still blocks on its own — it was never allowed to ride along with home.
        monkeypatch.setattr(address_guard, "HOME_PATHS_ENFORCE", False)
        assert address_guard.blocking(offenders) == [addr_only], (
            f"{label}: advisory mode lost the address")
        monkeypatch.setattr(address_guard, "HOME_PATHS_ENFORCE", True)


def test_home_path_is_visible_to_the_pattern_the_redactor_loads():
    """THE cross-file invariant, not a nicety.

    `.github/scripts/redact_guard_report.py` redacts the CI report by loading the
    guard's `FORBIDDEN` and nothing else. A home rule written as a SECOND pattern
    would therefore print `/Users/<operator's real name>/...` into a public job log
    — the backstop publishing the leak it exists to stop. Home paths live inside
    FORBIDDEN for exactly this reason, so pin that the redactor's one pattern sees
    them.
    """
    planted = _home("/Users", rest="/.hermes/gateway.sock")
    assert address_guard.FORBIDDEN.search(planted), (
        "FORBIDDEN no longer matches a home path: the CI redactor (which loads only"
        " FORBIDDEN) would print the operator's real home path into a public log")
    assert address_guard.is_home_occurrence(address_guard.FORBIDDEN.search(planted).group(0))


def test_scan_blob_formats_a_home_offender_with_file_line_and_fix():
    got = address_guard.scan_blob("docs/testing/loadstress-leg.md",
                                  f"run {_home('/Users', rest='/bin/python')}\n".encode())
    assert len(got) == 1
    assert got[0].startswith(f"docs/testing/loadstress-leg.md:1: {_home('/Users')}")
    assert address_guard.is_home_offender(got[0]), "an offender the report cannot classify"
    assert address_guard.HOME_PATH_OFFENDER_SUFFIX in got[0]


# ── the fixture pair, pinned both ways (the readme_lint habit) ───────────────

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "home_path"


def _fixture_files():
    return sorted(p.relative_to(_FIXTURES).as_posix() for p in _FIXTURES.rglob("*")
                  if p.is_file())


@pytest.mark.parametrize("root", _HOME_ROOTS)
def test_broken_fixture_trips_under_either_root(root):
    """The must-trip fixture, substituted — and every line of it trips."""
    text = (_FIXTURES / "broken" / "leak.md.template").read_text(encoding="utf-8")
    body = [ln for ln in text.splitlines() if ln.startswith("    ")]
    assert len(body) == 5, f"the must-trip fixture lost a line: {len(body)}"
    for line in body:
        filled = line.replace("__HOME_ROOT__", root).replace("__OPERATOR_USER__", _REAL_USER)
        assert address_guard.home_hits(filled), f"must-trip line went unseen: {filled!r}"


def test_clean_fixtures_do_not_trip():
    """The must-not-trip fixtures: the placeholder and the sanctioned fixture user."""
    for rel in _fixture_files():
        if not rel.startswith("clean/"):
            continue
        text = (_FIXTURES / rel).read_text(encoding="utf-8")
        assert address_guard.scan_blob(rel, text.encode()) == [], (
            f"the documented replacement in {rel} is itself an offender — the scrub "
            "would have nowhere to scrub to")


def test_the_fixtures_themselves_are_clean_tracked_content():
    """Anti-vacuity for the fixtures.

    These files are tracked, so the pytest gate scans them. A fixture that only
    trips *after* substitution is worthless if the committed bytes flag the gate:
    it would turn the suite red the moment HOME_PATHS_ENFORCE flips, and the fix
    would be to delete the coverage that proves the rule works.
    """
    assert address_guard.scan_paths(_FIXTURES, _fixture_files()) == []


# ── advisory now, blocking on the flip ───────────────────────────────────────

def test_report_explains_a_home_path_differently_from_an_address(monkeypatch):
    """The fix for a home path is not `10.0.0.x`, and a wrong fix on a commit path
    is how --no-verify gets used. Both postures, because the header text differs
    between them and a reader must never have to guess which one fired."""
    planted = address_guard.home_offender("a.md", 3, _home("/Users", rest="/x"))

    # enforced — the shipped posture since t_34de41c3.
    text = address_guard.report([planted])
    assert "a.md:3" in text
    assert address_guard.HOME_PLACEHOLDER in text
    assert address_guard.FIXTURE_HOME_USER in text
    assert "PUBLIC" in text
    assert "blocked" in text, "the enforced header must say the commit is blocked"
    assert "ADVISORY" not in text, (
        "an enforced finding must not be described as advisory — that wording is "
        "how a hard rule reads as optional to whoever hits it")
    # mixed: the address still gets its own placeholders, the home path gets its own
    mixed = address_guard.report([planted, "b.md:1: " + REAL_LAN])
    assert "10.0.0.x" in mixed and "100.64.0.1" in mixed
    assert "a.md:3" in mixed and "b.md:1" in mixed

    # advisory — reachable only by editing the constant, so the wording stays
    # exercised rather than rotting into a lie.
    monkeypatch.setattr(address_guard, "HOME_PATHS_ENFORCE", False)
    adv = address_guard.report([planted])
    assert "ADVISORY" in adv, "advisory must be stated, not left to the exit code"
    assert address_guard.advisory([planted]) == [planted]
    assert address_guard.blocking([planted]) == []


def test_cli_blocks_a_home_path_now_that_home_is_enforced(tmp_path):
    """The flip, through the CLI the hook and CI both call (t_34de41c3).

    This is the "prove the rule bites" half of the advisory-then-flip habit: a
    knob nobody can turn on is a knob that silently stays off, and an advisory
    class is exactly how the PR #724 shape reached origin. The advisory posture is
    covered in-process (report/blocking above) rather than by an env-var off
    switch ON PURPOSE — `HSCC_ADDRESS_GUARD_HOME_ENFORCE` can only strengthen the
    rule, so a CI job or a checkout cannot mute the home class by exporting it.
    """
    r = _repo(tmp_path)
    (r / "loadstress.md").write_text(
        f"interpreter: {_home('/Users', rest='/.hermes/venv/bin/python')}\n",
        encoding="utf-8")
    _git(r, "add", "loadstress.md")

    def run(**env):
        import os
        proc_env = {**os.environ, **env}
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "address_guard.py"),
             "--staged", "--repo", str(r)],
            capture_output=True, env=proc_env)

    default = run()
    assert default.returncode == 1, (
        "HOME_PATHS_ENFORCE is True, so a planted home path must exit 1 — if this "
        "passes with 0 the flip did not land")
    err = default.stderr.decode()
    assert "loadstress.md:1" in err, err                        # reported, not silent
    assert "blocked" in err and address_guard.HOME_PLACEHOLDER in err

    # the env knob can still only strengthen: a truthy value changes nothing
    assert run(**{address_guard.HOME_PATHS_ENFORCE_ENV: "true"}).returncode == 1
    # ...and a falsy value must NOT mute the class
    assert run(**{address_guard.HOME_PATHS_ENFORCE_ENV: "0"}).returncode == 1, (
        "the env var became an OFF switch: an exported variable in CI could now "
        "disable the home rule")
    # a real address still blocks whatever the home knob says
    (r / "leak.md").write_text("host " + REAL_LAN + "\n", encoding="utf-8")
    _git(r, "add", "leak.md")
    assert run().returncode == 1


def test_the_backslash_form_is_a_named_limit_not_a_claim():
    """Windows-shaped home paths are NOT detected, and that is stated not hidden.

    Zero occurrences in the tracked tree today (measured on origin/main), so the
    rule does not claim them. This test exists so the gap is a decision someone can
    read, rather than a silent hole someone has to rediscover.
    """
    windows = "C:" + chr(92) + "Users" + chr(92) + _REAL_USER
    assert address_guard.find_in_text(windows) == [], (
        "the backslash form is now matched — delete this test or move it to the"
        " verdict list; do not leave a passing 'limit' pin masking coverage")


# ── the operator-identity class (t_25f7a806) ─────────────────────────────────
#
# A name has no SHAPE, so these fixtures inject one rather than hold a literal:
# `hscc-fixture` is a sanctioned name nobody owns, and the probes compile the GUARD'S
# OWN builder with it (`forbidden_pattern`) so the one-pattern rule stays true while
# the semantics still run on a GitHub runner, where the shipped branch is inert by
# design. The tests that need the class live — the CLI flip and the no-echo proof —
# skip on such a host and say why, rather than passing vacuously.

_IDENTITY_PROBE_NAME = address_guard.FIXTURE_HOME_USER
_PROBE_PATTERN = re.compile(address_guard.forbidden_pattern([_IDENTITY_PROBE_NAME]))
_PROBE_LOOKUP = address_guard.identity_lookup([_IDENTITY_PROBE_NAME])
_LIVE_NAME = next(iter(address_guard.IDENTITY_NAMES), None)
_NO_LIVE_IDENTITY = ("this host's account names nobody (a service account, or a "
                     "sanctioned CI user), so the identity branch is inert here")


def _identity_shapes(name):
    """The three shapes the card published, plus two more of the same family."""
    return [
        f"`crontab -l` → no crontab for {name}",                    # captured output
        f"role {name}_hermes already exists",                       # in an identifier
        f"/private/tmp/claude-501/-Users-{name}/abc/scratchpad/x.md",   # the slug
        f'entity: "{name}"',                                        # quoted, bare
        f"profiles/{name}/state.db",                                # path component
    ]


@pytest.mark.parametrize("shape", _identity_shapes(_IDENTITY_PROBE_NAME))
def test_the_published_identity_shapes_trip_the_branch(shape):
    m = _PROBE_PATTERN.search(shape)
    assert m, f"the identity branch went blind on {shape!r}"
    assert _PROBE_LOOKUP.match(m.group(0)), f"matched something that is not the name: {m!r}"


@pytest.mark.parametrize("shape", _identity_shapes(_IDENTITY_PROBE_NAME))
def test_a_name_nobody_owns_is_still_invisible_to_the_shipped_pattern(shape):
    """The card's claim, re-pinned from the other side.

    `FORBIDDEN` as compiled on THIS host carries no literal username, so a name that
    is not this host's is invisible — which is exactly why 21 tracked occurrences of
    the operator's own name measured 0 findings. A guard that flagged every name in
    every document would be a guard nobody could run.
    """
    assert address_guard.FORBIDDEN.search(shape) is None, (
        f"the shipped pattern flagged a name it was never told about: {shape!r}")


def test_a_name_glued_into_a_word_is_not_an_identity():
    """The delimiter floor — why the branch has boundaries and what they refuse.

    The two refusals are the false-positive floor this rule needs (a machine owned by
    `test` or `root` would otherwise drown), and the accepted contexts are the shapes
    the class actually arrives in: `_` for an identifier, `-` for a flattened path,
    `/` for a path component.
    """
    n = _IDENTITY_PROBE_NAME
    for text, label in ((f"desk{n}", "prefix glued"), (f"{n}yard", "suffix glued"),
                        (f"{n.upper()}X", "glued, upper-cased")):
        assert _PROBE_PATTERN.search(text) is None, f"{label}: a name became a word"
    for ctx, label in ((f"for {n}", "space"), (f'"{n}"', "double quote"),
                       (f"({n})", "paren"), (f"{n},", "comma"),
                       (f"user={n}", "equals"), (f"-Users-{n}/x", "slug join"),
                       (f"{n}_hermes", "identifier join"), (f"{n}@example.com", "at sign"),
                       (f"{n}.bak", "dot join"), (f"a\n{n}\nb", "own line")):
        assert _PROBE_PATTERN.search(ctx), f"{label}: the branch went blind"


def test_the_documented_identity_replacements_are_clean():
    """Every target this card scrubs TO must itself be invisible, or the scrub has
    nowhere to go and every fix becomes a new finding."""
    for text in ("`crontab -l` → no crontab for <user>",
                 "no crontab for <operator>",
                 "memori_entity id=3 `hermes_role`, date_created 2026-08-14",
                 '{ "entityId": "hermes_role", "projectId": "hermes_cluster" }',
                 "/private/tmp/…/scratchpad/hermes-notes/v0.21.6.md"):
        assert _PROBE_PATTERN.search(text) is None, f"the replacement is an offender: {text!r}"


def test_the_identity_classifier_cannot_widen_a_match():
    """The classifier compares whole names and nothing else — an unanchored one would
    re-open the exemption-by-prefix the fixture allowlist already refuses."""
    n = _IDENTITY_PROBE_NAME
    assert _PROBE_LOOKUP.match(n) and _PROBE_LOOKUP.match(n.upper())
    assert not _PROBE_LOOKUP.match(f"x{n}") and not _PROBE_LOOKUP.match(f"{n}-2")
    assert not _PROBE_LOOKUP.match("role " + n + " here")
    assert address_guard.identity_lookup([]) is None


def test_an_identity_offender_never_carries_the_name_it_matched():
    """THE reason this class may live inside FORBIDDEN.

    `.github/scripts/redact_guard_report.py` masks the public job log with the pattern
    ITS OWN environment derives: a report generated on the operator's machine and
    redacted on a runner has no name in common to mask, so an offender that echoed the
    match would publish the leak from the backstop. Echo the placeholder instead and
    the guarantee stops depending on where the redactor happens to run.
    """
    got = address_guard.identity_offender("docs/audit.md", 7)
    assert got.startswith("docs/audit.md:7: ")
    assert address_guard.IDENTITY_PLACEHOLDER in got
    assert address_guard.is_identity_offender(got)
    assert address_guard.advisory([got]) == [got]           # the shipped posture
    assert address_guard.blocking([got]) == []
    if _LIVE_NAME:
        assert _LIVE_NAME not in got, "the offender echoed the name it found"


def test_the_identity_knob_may_only_strengthen(monkeypatch):
    """Advisory at landing, and provably turnable-on — the `HOME_PATHS_ENFORCE` habit.

    The falsy-env half is the one that matters: a variable exported in CI must never
    be able to mute a class, so `identity_enforced()` ORs the knob and never ANDs it.
    """
    planted = address_guard.identity_offender("a.md", 1)
    assert address_guard.advisory([planted]) == [planted]
    assert address_guard.blocking([planted]) == []

    monkeypatch.setattr(address_guard, "OPERATOR_IDENTITIES_ENFORCE", True)
    assert address_guard.blocking([planted]) == [planted], (
        "OPERATOR_IDENTITIES_ENFORCE=True did not block: the rule can never be turned on")
    assert address_guard.advisory([planted]) == []

    monkeypatch.setattr(address_guard, "OPERATOR_IDENTITIES_ENFORCE", False)
    monkeypatch.setenv(address_guard.OPERATOR_IDENTITIES_ENFORCE_ENV, "1")
    assert address_guard.identity_enforced(), "the env knob failed to strengthen the rule"
    monkeypatch.setattr(address_guard, "OPERATOR_IDENTITIES_ENFORCE", True)
    monkeypatch.setenv(address_guard.OPERATOR_IDENTITIES_ENFORCE_ENV, "0")
    assert address_guard.identity_enforced(), (
        "the env var became an OFF switch: an exported value in CI could mute the class")


def test_report_explains_an_identity_finding_without_echoing_the_name(monkeypatch):
    """The fix for a name is not `10.0.0.x`, and the header must say which posture
    fired — the same reason the home class has its own text (t_34de41c3)."""
    planted = address_guard.identity_offender("docs/x.md", 3)

    adv = address_guard.report([planted])
    assert "docs/x.md:3" in adv and "OPERATOR IDENTITY" in adv
    assert address_guard.IDENTITY_PLACEHOLDER in adv
    assert "ADVISORY" in adv, "advisory must be stated, not left to the exit code"
    assert "the commit is blocked" not in adv

    monkeypatch.setattr(address_guard, "OPERATOR_IDENTITIES_ENFORCE", True)
    hard = address_guard.report([planted])
    assert "the commit is blocked" in hard and "ADVISORY" not in hard, (
        "an enforced finding must not be described as advisory")

    mixed = address_guard.report([planted, "b.md:1: " + REAL_LAN])
    assert "b.md:1" in mixed and "10.0.0.x" in mixed, mixed
    assert "docs/x.md:3" in mixed and "Scrub the name" in mixed, (
        "a mixed report must carry BOTH fixes: the address header wins the title, so"
        " the identity section is the only thing telling the reader what to do with"
        " the name — and its fix is not `10.0.0.x`")

    if _LIVE_NAME:
        assert _LIVE_NAME not in adv and _LIVE_NAME not in hard, (
            "the report echoed the name — a public job log would carry it")


def test_the_shipped_identity_branch_is_the_one_the_pattern_uses():
    """No drift, and no second pattern: `FORBIDDEN` IS `forbidden_pattern()`, and the
    identity rule is inside it — the one regex the CI redactor loads."""
    assert address_guard.FORBIDDEN.pattern == address_guard.forbidden_pattern()
    branch = address_guard.identity_branch_for([_IDENTITY_PROBE_NAME])
    assert branch in address_guard.forbidden_pattern([_IDENTITY_PROBE_NAME])
    assert re.compile(address_guard.forbidden_pattern([_IDENTITY_PROBE_NAME])).search(
        f"no crontab for {_IDENTITY_PROBE_NAME}"), (
        "the identity rule is not inside FORBIDDEN: the redactor loads only FORBIDDEN"
        " and would print the value into a public job log")
    assert address_guard.identity_branch_for([]) is None, (
        "an empty name set must decline the branch, not match every token")


def test_the_guard_and_its_tests_carry_no_identity_literal():
    """The control cannot publish what it does not hold.

    The objection t_3480956c raised against a username rule was that it writes the
    name into the security control. A rule derived from the runtime environment does
    not, and this is the machine-checked version of that claim: on a host that HAS an
    identity, the detector and this file still do not contain it.
    """
    if not _LIVE_NAME:
        pytest.skip(_NO_LIVE_IDENTITY)
    for rel in ("scripts/address_guard.py", "scripts/tests/test_address_guard.py"):
        text = (REPO / rel).read_text(errors="ignore").lower()
        assert _LIVE_NAME.lower() not in text, f"{rel} carries the operator's name"


def test_an_address_never_hides_behind_a_name_on_the_same_line(monkeypatch):
    """The advisory-interaction hazard, re-pinned for the third class (both orders).

    With first-match-wins, an address sitting after a name on one line would have been
    reported as identity-only — identity is advisory, so the BLOCKING address left the
    gate unseen and the commit passed. `all_hits` is what stops that, and this is the
    test that would have caught it.
    """
    if not _LIVE_NAME:
        pytest.skip(_NO_LIVE_IDENTITY)
    addr = "10" + ".0.0.5"
    addr_only = f"d.md:1: {addr}"
    for line, label in ((f"host {addr}: no crontab for {_LIVE_NAME}", "address first"),
                        (f"no crontab for {_LIVE_NAME} — host {addr}", "name first")):
        assert [v for _, v in address_guard.address_hits(line)] == [addr], label
        assert [v for _, v in address_guard.identity_hits(line)] == [_LIVE_NAME], label
        offenders = address_guard.scan_blob("d.md", line.encode())
        assert len(offenders) == 2, (label, offenders)
        # shipped posture: identity is advisory, the address blocks on its own.
        assert address_guard.blocking(offenders) == [addr_only], (
            f"{label}: the address escaped the blocking subset")
        # enforced: both block, and the identity arm still carries no name.
        monkeypatch.setattr(address_guard, "OPERATOR_IDENTITIES_ENFORCE", True)
        blocked = address_guard.blocking(offenders)
        assert len(blocked) == 2 and addr_only in blocked, (label, blocked)
        assert all(_LIVE_NAME not in o for o in blocked), blocked
        monkeypatch.setattr(address_guard, "OPERATOR_IDENTITIES_ENFORCE", False)


def test_cli_reports_an_identity_finding_and_blocks_once_the_knob_is_set(tmp_path):
    """The flip, through the CLI the hook and CI both call — the shape of
    `test_cli_blocks_a_home_path_now_that_home_is_enforced`, inverted for a class that
    ships advisory: rc 0 with the finding REPORTED today, rc 1 when the knob is set.

    Skipped where the branch is inert, because there the CLI has nothing to bite on;
    the in-process tests above cover the mechanism on every host.
    """
    if not _LIVE_NAME:
        pytest.skip(_NO_LIVE_IDENTITY)
    r = _repo(tmp_path)
    (r / "audit.md").write_text(f"`crontab -l` → no crontab for {_LIVE_NAME}\n",
                                encoding="utf-8")
    _git(r, "add", "audit.md")

    def run(**env):
        import os
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "address_guard.py"),
             "--staged", "--repo", str(r)],
            capture_output=True, env={**os.environ, **env})

    default = run()
    assert default.returncode == 0, (
        "OPERATOR_IDENTITIES_ENFORCE is False, so an identity finding must REPORT and "
        "not block — a hard rule that turns the merge queue red on landing is how the "
        "scrub cards get frozen out")
    err = default.stderr.decode()
    assert "audit.md:1" in err and "ADVISORY" in err and "OPERATOR IDENTITY" in err
    assert _LIVE_NAME not in err, "the report echoed the name into the terminal/log"

    forced = run(**{address_guard.OPERATOR_IDENTITIES_ENFORCE_ENV: "1"})
    assert forced.returncode == 1, (
        "the env knob did not make the class block: the flip cannot be turned on, so "
        "advisory is permanent")
    assert "the commit is blocked" in forced.stderr.decode()
    assert _LIVE_NAME not in forced.stderr.decode()

    # a real address still blocks whatever the identity knob says
    (r / "leak.md").write_text("host " + REAL_LAN + "\n", encoding="utf-8")
    _git(r, "add", "leak.md")
    assert run().returncode == 1


# keep pytest import light: imported late so the module-under-test load order is explicit
import pytest  # noqa: E402
