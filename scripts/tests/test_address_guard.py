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
    """
    return b"\xe3\r\r\n\x00\x00\x00\x00" + b"\x00" + addr.encode() + b"\x00\x00"


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


def test_artefact_allowlist_exempts_the_refusal_not_the_scan(monkeypatch):
    """The escape hatch is narrow: it stops the refusal, never the address scan.

    This is the guarantee that makes widening ALLOWED_BINARY_PATHS safe: an
    allowlisted binary is decoded utf-8/ignore and scanned LIKE TEXT even though
    it is full of NUL bytes, so the hatch can only ever say "this path may be
    tracked", never "this path may be unread".
    """
    monkeypatch.setattr(address_guard, "ALLOWED_BINARY_PATHS", frozenset({"native/bundled.so"}))
    # clean -> tracked, no verdict
    assert address_guard.scan_blob("native/bundled.so", b"\x00clean\x00") == []
    # leaking, NUL-bearing -> the address is still found
    leaky = b"\x00\x00host " + REAL_LAN.encode() + b"\x00\x00"
    assert address_guard.scan_blob("native/bundled.so", leaky) == [
        f"native/bundled.so:1: {REAL_LAN}"]
    # and the hatch is exactly as wide as the path that is on it
    assert address_guard.scan_blob("native/other.so", b"\x00clean\x00") == [
        "native/other.so:0: build artefact must not be tracked"]


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
