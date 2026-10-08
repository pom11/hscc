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
    dirty = b"\x00binary " + REAL_LAN.encode() + b"\x00"
    assert address_guard.scan_blob("x.bin", dirty) == []
    assert address_guard.scan_blob("logo.png", REAL_LAN.encode()) == []


def test_skip_suffixes_unchanged_from_the_original_gate():
    """The gate's original noise list must not silently shrink."""
    assert address_guard.SKIP_SUFFIXES == (
        ".png", ".jpg", ".jpeg", ".pdf", ".ico", ".zip", ".gz", ".xcuserstate")


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
