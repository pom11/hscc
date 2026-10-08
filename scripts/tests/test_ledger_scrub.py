"""Tests for scripts/ledger_scrub.py — emitter-side redaction (t_a98c009f).

Three layers, because a unit test of the scrubber alone would prove the wrong
thing (the parent card says so explicitly):

  1. the scrubber's own contract — placeholder mapping, the stderr count line,
     pure stdout, atomic in-place writes, exit codes, idempotency;
  2. the single-implementation guarantee — the scrubber REUSES
     ``scripts/address_guard.py`` and carries no address pattern of its own, in
     the same structural idiom as
     ``hscc_daemon/tests/test_no_real_addresses_committed.py``;
  3. the end-to-end proof, in a throwaway repo carrying the REAL
     ``.githooks/pre-commit`` + the REAL detector + this scrubber (same idiom as
     ``hscc_daemon/tests/test_precommit_address_hook.py``): a raw
     ``showmount``/``mount`` capture is BLOCKED by the armed hook with its
     ``file:line``, and the same capture piped through ``ledger_scrub.py --stdin``
     COMMITS, with the committed blob carrying the documented placeholders and no
     real-shaped address.

Fixture addresses are ASSEMBLED AT RUNTIME (``_addr``), because the repo's own
guard scans this very file — a contiguous real-shaped literal here is (correctly)
blocked by the hook these tests exercise.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
HOOK = REPO / ".githooks" / "pre-commit"
GUARD = SCRIPTS / "address_guard.py"
SCRUBBER = SCRIPTS / "ledger_scrub.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load("address_guard_for_scrub_test", GUARD)
scrub = _load("ledger_scrub_under_test", SCRUBBER)

# The scrubber must use the detector's OWN constants, not copies of them.
LAN_PLACEHOLDER = guard.LAN_PLACEHOLDER            # 10.0.0.x
TAILNET_PLACEHOLDER = guard.TAILNET_PLACEHOLDER    # 100.64.0.1


def _addr(*parts):
    """Assemble a forbidden-shaped fixture address at runtime (see module docstring)."""
    return "".join(parts)


REAL_LAN = _addr("192", ".168.88.244")            # the live LAN's NAS host
REAL_LAN_GW = _addr("192", ".168.88.1")
REAL_TAILNET = _addr("100", ".64.55.7")           # CGNAT, outside the fixture block
REAL_TAILNET_HI = _addr("100", ".127.9.9")        # top of CGNAT
FIXTURE_TAILNET = "100.64.0.3"                    # sanctioned block — must survive
LAN_PLACEHOLDER_LONG = "10.0.0.244"               # placeholder-style text, must survive

GIT = ["git", "-c", "user.name=scrub-test", "-c", "user.email=scrub@example.invalid",
       "-c", "commit.gpgsign=false"]

# What the `hscc-orch-goal-heartbeat` tick actually pastes: raw mount/showmount output.
CAPTURE = (
    "# tick 23:55 — captured `showmount -e` + `mount` (raw)\n"
    f"Export list for {REAL_LAN}:\n"
    f"/volume1/data {REAL_LAN_GW}/24\n"
    f"dfs.fuse -o auto_mount on {REAL_LAN}:/volume1/data\n"
    f"tailscale peer {REAL_TAILNET} endpoint\n"
    f"fixture peer {FIXTURE_TAILNET} endpoint\n"
    f"second leak on one line: {_addr('192', '.168.88.7')} and {REAL_TAILNET_HI}\n"
)


# Count of real-shaped addresses in CAPTURE, asserted as a constant so a fixture
# edit cannot silently change what "the count line" means:
#   line 2 LAN, line 3 LAN, line 4 LAN, line 5 tailnet, line 7 LAN + tailnet = 6
# (line 6 is the sanctioned fixture block, which must NOT be counted).
CAPTURE_ADDRESSES = 6


def _run(args, cwd=None, input_bytes=None, env=None):
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True,
                          input=input_bytes, env=env)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "surrogateescape"),
            proc.stderr.decode("utf-8", "surrogateescape"))


# ── 1. the scrubber's own contract ───────────────────────────────────────────

def test_lan_match_becomes_the_lan_placeholder():
    out, n = scrub.scrub_text(f"mount {REAL_LAN}:/models /Volumes/NAS")
    assert out == f"mount {LAN_PLACEHOLDER}:/models /Volumes/NAS"
    assert n == 1


def test_cgnat_match_becomes_the_tailnet_placeholder():
    out, n = scrub.scrub_text(f"api https://{REAL_TAILNET}:8787/v1")
    assert out == f"api https://{TAILNET_PLACEHOLDER}:8787/v1"
    assert n == 1


def test_both_kinds_in_one_capture_map_to_their_own_placeholder():
    out, n = scrub.scrub_text(f"lan {REAL_LAN} tailnet {REAL_TAILNET}\n")
    assert out == f"lan {LAN_PLACEHOLDER} tailnet {TAILNET_PLACEHOLDER}\n"
    assert n == 2


def test_count_is_per_address_not_per_line():
    """`find_in_text` reports one hit per line (it builds file:line); the count
    the tick records must count every address on the line."""
    line = f"a {REAL_LAN} b {_addr('192', '.168.88.9')} c {REAL_TAILNET}\n"
    assert len(guard.find_in_text(line)) == 1
    _, n = scrub.scrub_text(line)
    assert n == 3


def test_scrubbed_text_has_no_offender_left():
    out, _ = scrub.scrub_text(CAPTURE)
    assert guard.find_in_text(out) == []


@pytest.mark.parametrize("text", [
    f"host {LAN_PLACEHOLDER} and {TAILNET_PLACEHOLDER}\n",
    f"subnet {LAN_PLACEHOLDER}/24 export\n",
    f"placeholder-with-host {LAN_PLACEHOLDER_LONG} still a placeholder\n",
    f"fixture block {FIXTURE_TAILNET} and {TAILNET_PLACEHOLDER}\n",
    "172.16.4.5 and 8.8.8.8 are out of scope\n",
    "no address at all here\n",
    "",
])
def test_clean_text_is_byte_identical(text):
    """Nothing that is NOT an offender may move — not the placeholders, not the
    fixture block, not `10.0.0.244`-style placeholder text."""
    out, n = scrub.scrub_text(text)
    assert out == text and n == 0


def test_idempotent_on_a_full_capture():
    """Hard requirement: double-scrub == single-scrub, byte-for-byte."""
    once, n1 = scrub.scrub_text(CAPTURE)
    twice, n2 = scrub.scrub_text(once)
    assert twice == once
    assert n1 == CAPTURE_ADDRESSES and n2 == 0


def test_scrubbed_capture_keeps_every_non_address_byte():
    once, _ = scrub.scrub_text(CAPTURE)
    src_lines = CAPTURE.split("\n")
    out_lines = once.split("\n")
    assert len(out_lines) == len(src_lines)
    # the fixture line and the header line are untouched
    assert out_lines[0] == src_lines[0]
    assert f"fixture peer {FIXTURE_TAILNET} endpoint" == out_lines[5]


def test_undecodable_bytes_survive_a_scrub():
    """A capture is not always clean UTF-8; a scrub must not drop bytes."""
    raw = f"host {REAL_LAN}\n".encode() + b"\xff\xfe tail\n"
    text = raw.decode("utf-8", errors="surrogateescape")
    out, n = scrub.scrub_text(text)
    assert n == 1
    assert out.encode("utf-8", errors="surrogateescape") == (
        f"host {LAN_PLACEHOLDER}\n".encode() + b"\xff\xfe tail\n")


# ── CLI: --stdin ─────────────────────────────────────────────────────────────

def _cli(args, input_text=None, env=None):
    return _run([sys.executable, str(SCRUBBER), *args],
                input_bytes=(input_text or "").encode(), env=env)


def test_stdin_mode_writes_only_scrubbed_text_to_stdout():
    rc, out, err = _cli(["--stdin"], CAPTURE)
    assert rc == 0, err
    assert out == scrub.scrub_text(CAPTURE)[0]
    assert REAL_LAN not in out and REAL_TAILNET not in out
    # stdout is the text and nothing else — the count lives on stderr
    assert out.startswith("# tick 23:55")
    assert "address(es) scrubbed" not in out


def test_stdin_mode_reports_the_count_on_stderr():
    rc, out, err = _cli(["--stdin"], CAPTURE)
    assert rc == 0
    assert f"ledger_scrub: {CAPTURE_ADDRESSES} address(es) scrubbed" in err, err


def test_stdin_mode_reports_zero_for_a_clean_capture():
    rc, out, err = _cli(["--stdin"], "nothing to redact 10.0.0.x\n")
    assert rc == 0 and out == "nothing to redact 10.0.0.x\n"
    assert "ledger_scrub: 0 address(es) scrubbed" in err


def test_stdin_mode_is_byte_faithful():
    """Text round-trips byte-for-byte (no newline translation, no BOM, no added
    trailing newline) — the tick compares and re-writes these bytes."""
    raw = f"host {REAL_LAN}".encode()   # no trailing newline
    proc = subprocess.run([sys.executable, str(SCRUBBER), "--stdin"],
                          input=raw, capture_output=True)
    assert proc.stdout == f"host {LAN_PLACEHOLDER}".encode()


def test_stdin_mode_survives_a_closed_pipe():
    """`ledger_scrub --stdin | head -1` must not raise BrokenPipeError into the tick."""
    p = subprocess.Popen([sys.executable, str(SCRUBBER), "--stdin"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    out = p.stdout
    inp = p.stdin
    assert out is not None and inp is not None
    out.close()
    try:
        inp.write(CAPTURE.encode())
        inp.close()
    except BrokenPipeError:
        pass
    assert p.wait(timeout=30) in (0, -13)   # clean, or killed by SIGPIPE: never a traceback


# ── CLI: FILE... in place ────────────────────────────────────────────────────

def test_file_mode_scrubs_in_place(tmp_path):
    f = tmp_path / "GOAL_LEDGER_2026-10-09.md"
    f.write_text(CAPTURE, encoding="utf-8")
    rc, out, err = _cli([str(f)])
    assert rc == 0, err
    assert out == ""                                   # stdout stays clean
    assert f.read_text(encoding="utf-8") == scrub.scrub_text(CAPTURE)[0]
    assert f"{CAPTURE_ADDRESSES} address(es)" in err and "1 file(s)" in err


def test_file_mode_scrubs_several_files(tmp_path):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    b.write_text(f"peer {REAL_TAILNET}\n", encoding="utf-8")
    rc, out, err = _cli([str(a), str(b)])
    assert rc == 0, err
    assert a.read_text(encoding="utf-8") == f"host {LAN_PLACEHOLDER}\n"
    assert b.read_text(encoding="utf-8") == f"peer {TAILNET_PLACEHOLDER}\n"
    assert "2 file(s)" in err


def test_file_mode_preserves_mode_and_leaves_no_temp_file(tmp_path):
    f = tmp_path / "ledger.md"
    f.write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    os.chmod(f, 0o640)
    rc, out, err = _cli([str(f)])
    assert rc == 0, err
    assert f.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["ledger.md"], "atomic write left debris"


def test_file_mode_is_atomic_under_a_crash(tmp_path, monkeypatch, capsys):
    """The rename is the commit point: if it fails, the ORIGINAL file is still
    there untouched (the tick must never end up with a truncated ledger).

    In-process, because monkeypatch has to reach the code that does the rename —
    a subprocess would ignore the patch and the test would pass for the wrong
    reason.
    """
    f = tmp_path / "ledger.md"
    original = f"host {REAL_LAN}\n"
    f.write_text(original, encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(scrub.os, "replace", boom)
    rc = scrub.scrub_files([str(f)])
    assert rc == 1
    assert "cannot write" in capsys.readouterr().err
    assert f.read_text(encoding="utf-8") == original, "fail-closed must leave the original"
    assert [p.name for p in tmp_path.iterdir()] == ["ledger.md"], "failed write left debris"


def test_file_mode_does_not_rewrite_a_clean_file(tmp_path):
    """Idempotent re-reads must not churn mtime — the tick re-reads text it already
    scrubbed, and `git status` must not start lying about it."""
    f = tmp_path / "ledger.md"
    f.write_text(f"host {LAN_PLACEHOLDER}\n", encoding="utf-8")
    before = f.stat().st_mtime_ns
    rc, out, err = _cli([str(f)])
    assert rc == 0, err
    assert f.stat().st_mtime_ns == before
    assert "0 address(es) scrubbed across 0 file(s)" in err


def test_file_mode_refuses_binary_without_touching_it(tmp_path):
    f = tmp_path / "asset.bin"
    payload = b"\x00binary " + REAL_LAN.encode() + b"\x00"
    f.write_bytes(payload)
    rc, out, err = _cli([str(f)])
    assert rc == 0
    assert f.read_bytes() == payload
    assert "binary" in err


def test_file_mode_reports_a_missing_file_and_still_scrubs_the_rest(tmp_path):
    f = tmp_path / "ledger.md"
    f.write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    rc, out, err = _cli([str(tmp_path / "absent.md"), str(f)])
    assert rc == 1, "an unreadable input is a failure the tick must see"
    assert "cannot read" in err
    assert f.read_text(encoding="utf-8") == f"host {LAN_PLACEHOLDER}\n"


# ── CLI: usage ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [[], ["--stdin", "x.md"]])
def test_usage_errors_exit_2(args):
    rc, out, err = _cli(args)
    assert rc == 2
    assert err.strip(), "a usage error must say why"


def test_scrubber_is_dependency_free():
    """Stdlib only — the tick runs it on every capture and it must never be the
    thing that needs an environment."""
    src = SCRUBBER.read_text(encoding="utf-8")
    imported = set()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import "):
            imported |= {t.strip().split(" ")[0].split(".")[0]
                         for t in s[len("import "):].split(",")}
        if s.startswith("from ") and " import " in s:
            imported.add(s.split()[1].split(".")[0])
    allowed = {"argparse", "os", "sys", "tempfile", "pathlib", "__future__", "address_guard"}
    assert imported <= allowed, f"non-stdlib import in the scrubber: {imported - allowed}"


# ── 2. single-implementation guarantee ───────────────────────────────────────
# The card's core rule: NO second regex. Detection lives in address_guard.py and
# nowhere else, so the scrubber and both commit gates can never disagree about
# what counts as a leak.

# Literal fragment of the live-LAN branch of the detector's regex, assembled so
# this file stays clean too (same idiom as test_no_real_addresses_committed.py).
_LAN_BRANCH_LITERAL = "192" + r"\.168" + r"\.88" + r"\.\d"
# A dotted-quad-looking digit run: the shape an address literal would have.
_QUAD_SHAPE = re.compile(r"\d+\.\d+\.\d+")


def test_scrubber_imports_the_detector_and_not_its_own_pattern():
    """It must import exactly the shared names the card fixed."""
    src = SCRUBBER.read_text(encoding="utf-8")
    for name in ("FORBIDDEN", "LAN_PLACEHOLDER", "TAILNET_PLACEHOLDER", "find_in_text"):
        assert name in src, f"scrubber must reuse address_guard.{name}"
    assert "from address_guard import" in src or "import address_guard" in src


def test_scrubber_defines_no_pattern_of_its_own():
    src = SCRUBBER.read_text(encoding="utf-8")
    assert "re.compile" not in src, "the scrubber may not compile a pattern of its own"
    assert "FORBIDDEN.sub" in src, "scrubbing must go through the detector's compiled pattern"
    assert _LAN_BRANCH_LITERAL not in src, "the scrubber re-implements the live-LAN branch"


def test_scrubber_source_carries_no_address_literal_in_its_code():
    """Structural proof there is no second pattern hiding as a constant.

    Tokenize, then drop COMMENTS and STRINGS (documentation may name the
    documented placeholders and the fixture block — the detector itself allows
    those) and assert no dotted-quad digit run survives in the CODE. Every address
    shape the scrubber can act on therefore comes from the detector, including the
    LAN/tailnet discriminator, which is derived from TAILNET_PLACEHOLDER.
    """
    with tokenize.open(str(SCRUBBER)) as fh:
        code = "".join(
            tok.string for tok in tokenize.generate_tokens(fh.readline)
            if tok.type not in (tokenize.COMMENT, tokenize.STRING)
        )
    bad = _QUAD_SHAPE.findall(code)
    assert not bad, f"digit-bearing address literal in scrubber code: {bad}"


def test_scrubber_source_is_itself_clean_to_the_detector():
    """The scrubber must satisfy the gate it supports (self-scan, same rule the
    pre-commit hook applies to it)."""
    assert guard.find_in_text(SCRUBBER.read_text(encoding="utf-8")) == []


def test_scrubber_is_in_the_one_holder_scan():
    """The existing single-holder test in hscc_daemon scans tracked files; assert
    here too so a future edit to the scrubber fails WITH these tests."""
    candidates = set(guard.tracked_paths(REPO)) | {
        "scripts/address_guard.py", "scripts/ledger_scrub.py"}
    holders = sorted(
        rel for rel in candidates
        if rel.endswith((".py", ".sh", ".ts", ".js", ".swift"))
        and _LAN_BRANCH_LITERAL in (REPO / rel).read_text(errors="ignore"))
    assert holders == ["scripts/address_guard.py"], holders


# ── 3. END TO END: the SHIPPED hook decides, not a mock ──────────────────────

@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Throwaway repo carrying the real hook, the real detector AND the scrubber."""
    r = tmp_path / "guarded"
    (r / "scripts").mkdir(parents=True)
    (r / ".githooks").mkdir()
    (r / "docs" / "audits").mkdir(parents=True)
    shutil.copy2(GUARD, r / "scripts" / "address_guard.py")
    shutil.copy2(SCRUBBER, r / "scripts" / "ledger_scrub.py")
    shutil.copy2(HOOK, r / ".githooks" / "pre-commit")
    os.chmod(r / ".githooks" / "pre-commit", 0o755)

    rc, out, err = _run(GIT + ["init", "-q", "-b", "main", str(r)], tmp_path)
    assert rc == 0, err
    rc, out, err = _run(GIT + ["config", "core.hooksPath", ".githooks"], r)
    assert rc == 0, err
    # Deterministic interpreter for the hook (also covers the HSCC_ADDRESS_GUARD_PY
    # override the shipped hook honours).
    monkeypatch.setenv("HSCC_ADDRESS_GUARD_PY", sys.executable)
    (r / "README.md").write_text("seed\n", encoding="utf-8")
    _run(GIT + ["add", "README.md"], r)
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "chore: seed (hook + detector + scrubber)"], r)
    assert rc == 0, f"seed commit must pass the armed hook: {err}"
    return r


def _commit_file(r, rel, msg):
    _run(GIT + ["add", "--", rel], r)
    return _run(GIT + ["commit", "-q", "-m", msg], r)


LEDGER = "docs/audits/GOAL_LEDGER_TEST.md"


def test_e2e_raw_capture_is_blocked_by_the_shipped_hook(repo):
    """The negative half: WITHOUT the scrubber the tick's commit fails, with the
    offending file:line. This is the failure the scrubber exists to remove."""
    (repo / LEDGER).write_text(CAPTURE, encoding="utf-8")
    rc, out, err = _commit_file(repo, LEDGER, "docs: ledger tick (raw)")
    assert rc != 0, "a raw capture must NOT be committable"
    assert f"{Path(LEDGER).name}:2" in err, err        # file:line of the first leak
    assert LAN_PLACEHOLDER in err and TAILNET_PLACEHOLDER in err, err
    rc, out, _ = _run(GIT + ["log", "--oneline"], repo)
    assert out.strip().count("\n") == 0, "the leaking commit must not exist"


def test_e2e_scrubbed_capture_commits_with_placeholders(repo):
    """The positive half, exactly as the cron calls it: capture | ledger_scrub
    --stdin > docs/audits/GOAL_LEDGER_*.md; git add; git commit -> SUCCEEDS."""
    rc, scrubbed, err = _run([sys.executable, str(repo / "scripts" / "ledger_scrub.py"),
                              "--stdin"], repo, input_bytes=CAPTURE.encode())
    assert rc == 0, err
    assert f"{CAPTURE_ADDRESSES} address(es) scrubbed" in err, err
    (repo / LEDGER).write_text(scrubbed, encoding="utf-8")
    rc, out, err = _commit_file(repo, LEDGER, "docs: ledger tick (scrubbed)")
    assert rc == 0, f"scrubbed capture must commit through the armed hook\n{out}{err}"

    rc, blob, err = _run(GIT + ["show", f"HEAD:{LEDGER}"], repo)
    assert rc == 0, err
    assert LAN_PLACEHOLDER in blob and TAILNET_PLACEHOLDER in blob, blob
    assert REAL_LAN not in blob and REAL_TAILNET not in blob
    assert guard.find_in_text(blob) == [], guard.find_in_text(blob)
    assert FIXTURE_TAILNET in blob, "the sanctioned fixture block must survive"


def test_e2e_committed_blob_is_a_scrub_fixpoint(repo):
    """Re-scrubbing what is now COMMITTED is a byte-for-byte no-op — the tick may
    re-read and re-write its own ledger safely."""
    _run([sys.executable, str(repo / "scripts" / "ledger_scrub.py"), "--stdin"],
         repo, input_bytes=CAPTURE.encode())
    rc, scrubbed, err = _run([sys.executable, str(repo / "scripts" / "ledger_scrub.py"),
                              "--stdin"], repo, input_bytes=CAPTURE.encode())
    (repo / LEDGER).write_text(scrubbed, encoding="utf-8")
    assert _commit_file(repo, LEDGER, "docs: tick")[0] == 0

    first = (repo / LEDGER).read_bytes()
    rc2, again, err2 = _run([sys.executable, str(repo / "scripts" / "ledger_scrub.py"),
                             "--stdin"], repo, input_bytes=first)
    assert rc2 == 0, err2
    assert again.encode() == first, "double-scrub is not byte-identical"
    assert "0 address(es) scrubbed" in err2


def test_e2e_file_mode_on_the_ledger_then_commit(repo):
    """The other documented interface (in-place FILE args) also produces text the
    shipped hook accepts."""
    (repo / LEDGER).write_text(CAPTURE, encoding="utf-8")
    rc, out, err = _run([sys.executable, str(repo / "scripts" / "ledger_scrub.py"),
                         LEDGER], repo)
    assert rc == 0, err
    assert _commit_file(repo, LEDGER, "docs: tick (file mode)")[0] == 0
    rc, blob, _ = _run(GIT + ["show", f"HEAD:{LEDGER}"], repo)
    assert guard.find_in_text(blob) == [] and LAN_PLACEHOLDER in blob


def test_e2e_a_second_tick_that_forgets_to_scrub_is_still_blocked(repo):
    """The scrubber is defence in depth, not a replacement: the armed hook still
    guards a tick that skips it."""
    _run([sys.executable, str(repo / "scripts" / "ledger_scrub.py"), "--stdin"],
         repo, input_bytes=CAPTURE.encode())
    (repo / LEDGER).write_text("first tick\n", encoding="utf-8")
    assert _commit_file(repo, LEDGER, "docs: tick 1")[0] == 0
    (repo / LEDGER).write_text("first tick\nsecond tick " + REAL_LAN + "\n", encoding="utf-8")
    rc, out, err = _commit_file(repo, LEDGER, "docs: tick 2 (forgot to scrub)")
    assert rc != 0, "the hook must still block a leak the scrubber never saw"
    assert f"{Path(LEDGER).name}:2" in err, err


def test_e2e_scrubber_ships_next_to_the_detector_it_imports(repo):
    """The throwaway repo proves the copy, not a mock: the scrubber the hook
    accepted is the one on disk beside the real detector."""
    assert (repo / "scripts" / "ledger_scrub.py").is_file()
    assert (repo / "scripts" / "ledger_scrub.py").read_bytes() == SCRUBBER.read_bytes()
    assert (repo / ".githooks" / "pre-commit").read_bytes() == HOOK.read_bytes()
