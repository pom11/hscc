"""Tests for ci_log.sh — the sanctioned CI-log reader (t_914b8db5).

The class this closes: `scripts/address_guard.py` prints each offender as
``rel:line: <matched address>``, and pom11/hscc is PUBLIC, so a raw
`gh run view --log` on any job that ran the guard without stream redirection
prints the leak into scrollback, a paste, or an attachment. ci_log.sh must be
the only path workers use, and these tests pin its contract by EXECUTION with
a fake `gh` on PATH — never by reading the script's text and hoping.

The fake gh writes a real-shaped address into its log — assembled at RUNTIME
via ``_addr`` because the repo's own guard scans every tracked file, this one
included, and a contiguous literal here is (correctly) blocked.

Every case gets its own TMPDIR (tmp_path), which is also how the "raw dump is
gone by default / exactly one file with --keep" claims are checked.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CI_SCRIPTS = HERE.parent
REPO = CI_SCRIPTS.parents[1]
CI_LOG = CI_SCRIPTS / "ci_log.sh"
REDACTOR = CI_SCRIPTS / "redact_guard_report.py"
GUARD = REPO / "scripts" / "address_guard.py"

_spec = importlib.util.spec_from_file_location("redact_guard_report_for_ci_log", REDACTOR)
assert _spec is not None and _spec.loader is not None
redactor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redactor)


def _addr(*parts):
    """Assemble a forbidden-shaped address at runtime (see module docstring)."""
    return "".join(parts)


REAL_LAN = _addr("192", ".168.88.244")
REAL_TAILNET = _addr("100.64", ".55.7")
LAN_PLACEHOLDER = "10.0.0.244"
TAILNET_PLACEHOLDER = "100.64.0.1"

# What a guard report inside a job log looks like (the leak payload), and a
# byte-exact CLEAN log for the byte-identity case.
OFFENDER_OUT = f"  docs/hosts.md:7: {REAL_LAN}"
OFFENDER_ERR = f"  docs/audits/audit.md:68: {REAL_TAILNET}"
CLEAN_LOG = (
    f"  LAN node     -> {LAN_PLACEHOLDER}\n"
    f"  tailnet host -> {TAILNET_PLACEHOLDER}\n"
    "address_guard: PASS (no real operator addresses in the committed tree)\n"
)


_FAKE_GH = """#!/usr/bin/env bash
# Fake `gh` for ci_log.sh tests. Emits a guard report carrying real-shaped
# addresses on BOTH streams (the point: stderr is job content too and must
# reach the redactor, never /dev/null), records argv, and exits as scripted.
printf '%s\\n' "$*" >"{argv_file}"
if [ -f "{log_file}" ]; then
  cat "{log_file}"
fi
if [ -n "{stderr_line}" ]; then
  printf '%s\\n' "{stderr_line}" >&2
fi
exit {gh_rc}
"""


def _fake_gh(tmp_path, *, log_text=None, stderr_line=OFFENDER_ERR, gh_rc=0):
    """Install a fake `gh` on PATH; return (env, argv_file, log_file).

    ``log_text`` is written verbatim to the file the fake cats onto its
    stdout — that is the "CI log" ci_log.sh must never echo raw.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    argv_file = tmp_path / "gh-argv.txt"
    log_file = tmp_path / "gh-log.txt"
    if log_text is not None:
        log_file.write_text(log_text, encoding="utf-8")
    gh = bindir / "gh"
    gh.write_text(
        _FAKE_GH.format(argv_file=argv_file, log_file=log_file,
                        stderr_line=stderr_line, gh_rc=gh_rc),
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    # The wrapper picks its own redactor interpreter; pin it to the one
    # running pytest so the both-interpreter gate actually exercises BOTH.
    env["HSCC_CI_LOG_PY"] = sys.executable
    # tmp_path IS the TMPDIR: the wrapper's temp dir must land somewhere the
    # test can inspect afterwards (and it also proves the 0700 mktemp choice).
    env["TMPDIR"] = str(tmp_path)
    return env, argv_file, log_file


def _run_ci_log(tmp_path, args, env):
    return subprocess.run([str(CI_LOG)] + args, capture_output=True, env=env,
                          cwd=str(tmp_path))


def _leftover_dumps(tmp_path):
    """Every hscc-ci-log.* dir the wrapper left in its TMPDIR."""
    return sorted(p for p in tmp_path.glob("hscc-ci-log.*"))


# ── the headline contract (executed against a fake gh) ───────────────────────

def test_stdout_keeps_position_and_drops_the_address(tmp_path):
    env, argv_file, _ = _fake_gh(
        tmp_path, log_text=f"starting scan\n{OFFENDER_OUT}\ndone\n")
    proc = _run_ci_log(tmp_path, ["12345"], env)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, err
    # the useful half survives...
    assert "docs/hosts.md:7" in out
    # ...the value does not, on EITHER published stream, and the mask is there
    assert REAL_LAN not in out and REAL_LAN not in err
    assert REAL_TAILNET not in out and REAL_TAILNET not in err
    assert "***" in out
    assert proc.stdout.count(REAL_LAN.encode()) == 0


def test_gh_stderr_is_redacted_not_swallowed(tmp_path):
    """gh's stderr must go through the SAME redactor (parent card, §9).

    The fake writes an offender line to stderr only. It must still reach the
    reader (a swallowed gh error is its own bug class) and must still be
    redacted — both properties in one assertion pair.
    """
    env, _, _ = _fake_gh(tmp_path, log_text="nothing on stdout\n")
    proc = _run_ci_log(tmp_path, ["12345"], env)
    out = proc.stdout.decode("utf-8", "replace")
    assert REAL_TAILNET not in out                      # redacted
    assert "docs/audits/audit.md:68" in out             # not swallowed
    assert "***" in out


def test_clean_log_passes_through_byte_identical(tmp_path):
    """No address in, byte-identical out — splitlines(True), no newline damage.

    The fake's stdout is written verbatim from CLEAN_LOG; its stderr line is
    disabled so the whole published stream is exactly CLEAN_LOG's bytes.
    """
    env, _, _ = _fake_gh(tmp_path, log_text=CLEAN_LOG, stderr_line="")
    proc = _run_ci_log(tmp_path, ["12345"], env)
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout == CLEAN_LOG.encode("utf-8")


def test_clean_log_without_trailing_newline_is_not_repaired(tmp_path):
    env, _, _ = _fake_gh(tmp_path, log_text="tail with no newline", stderr_line="")
    proc = _run_ci_log(tmp_path, ["12345"], env)
    assert proc.stdout == b"tail with no newline"


def test_gh_invocation_and_exit_code(tmp_path):
    """The wrapper must actually call `gh run view --log`, honour --job, and
    propagate gh's exit code (a red gh is information, not something to eat).
    """
    env, argv_file, _ = _fake_gh(tmp_path, log_text="ok\n", gh_rc=1)
    proc = _run_ci_log(tmp_path, ["987", "--job", "guard"], env)
    assert proc.returncode == 1
    argv = argv_file.read_text().split()
    assert argv[:4] == ["run", "view", "987", "--job"]
    assert "guard" in argv and "--log" in argv


def test_raw_dump_is_gone_by_default(tmp_path):
    env, _, _ = _fake_gh(tmp_path, log_text=OFFENDER_OUT + "\n")
    proc = _run_ci_log(tmp_path, ["12345"], env)
    assert proc.returncode == 0, proc.stderr.decode()
    assert _leftover_dumps(tmp_path) == []          # nothing 0600 left behind


def test_keep_leaves_one_0600_unredacted_dump_and_warns(tmp_path):
    env, _, _ = _fake_gh(tmp_path, log_text=OFFENDER_OUT + "\n")
    proc = _run_ci_log(tmp_path, ["12345", "--keep"], env)
    assert proc.returncode == 0, proc.stderr.decode()
    dumps = _leftover_dumps(tmp_path)
    assert len(dumps) == 1, f"--keep must leave exactly one dir: {dumps}"
    files = list(dumps[0].iterdir())
    assert len(files) == 1, f"--keep must leave exactly one file: {files}"
    raw = files[0]
    assert (raw.stat().st_mode & 0o777) == 0o600, "raw dump must be mode 0600"
    data = raw.read_bytes()
    # It is genuinely the RAW dump: the address IS in it — which is precisely
    # why the warning is mandatory.
    assert REAL_LAN.encode() in data
    err = proc.stderr.decode("utf-8", "replace")
    assert "UNREDACTED" in err
    assert str(raw) in err                           # and it names the path


# ── fail closed: redactor cannot load the pattern → nothing is printed ───────

def _sandbox(tmp_path, *, guard=None):
    """A copy of the tooling in a throwaway REPO-shaped tree.

    The redactor resolves scripts/address_guard.py two levels above itself, so
    omitting/breaking the guard here breaks pattern loading for real — this
    exercises the actual child-process extraction path, not a stub of it.
    """
    work = tmp_path / "sandbox"
    scripts = work / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CI_LOG, scripts / "ci_log.sh")
    shutil.copy2(REDACTOR, scripts / "redact_guard_report.py")
    if guard is not None:
        (work / "scripts").mkdir()
        (work / "scripts" / "address_guard.py").write_text(guard, encoding="utf-8")
    return work


@pytest.mark.parametrize("guard_src", [
    None,                                            # guard missing
    "raise SystemExit(9)\n",                         # guard explodes at import
    'FORBIDDEN = "not a compiled regex"\n',          # FORBIDDEN wrong type
], ids=["guard-missing", "guard-exits", "forbidden-not-regex"])
def test_fail_closed_prints_nothing(tmp_path, guard_src):
    """Pattern unavailable → non-zero exit and stdout EXACTLY empty.

    Not "mostly redacted": the acceptance bar is that the fail-closed path
    cannot fall through to the raw dump, so the strongest assertion is that
    ci_log.sh published nothing at all — including the offending position
    lines, which the wrapper only owns via the redactor.
    """
    work = _sandbox(tmp_path, guard=guard_src)
    env, argv_file, _ = _fake_gh(tmp_path, log_text=OFFENDER_OUT + "\n")
    proc = subprocess.run([str(work / ".github" / "scripts" / "ci_log.sh"),
                           "12345"], capture_output=True, env=env,
                          cwd=str(tmp_path))
    assert proc.returncode != 0
    assert proc.stdout == b"", "fail-closed must publish NOTHING on stdout"
    # the fake gh was never even invoked: no fetch after the safety check
    assert not argv_file.exists()
    # and nothing raw was left behind either
    assert _leftover_dumps(tmp_path) == []


def test_fail_closed_when_redactor_binary_missing(tmp_path):
    work = _sandbox(tmp_path)                    # ci_log.sh present, redactor not
    (work / ".github" / "scripts" / "redact_guard_report.py").unlink()
    env, _, _ = _fake_gh(tmp_path, log_text=OFFENDER_OUT + "\n")
    proc = subprocess.run([str(work / ".github" / "scripts" / "ci_log.sh"),
                           "12345"], capture_output=True, env=env,
                          cwd=str(tmp_path))
    assert proc.returncode == 3
    assert proc.stdout == b""


# ── the mode-100755 trap (measured in t_ec2c2f95) ────────────────────────────

def test_ci_log_is_100755_in_the_git_tree():
    """A 0644 script is silently unrunnable BY PATH — the git TREE mode is the
    one that ships. `git ls-files -s` is the only truth CI sees after checkout.
    """
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-s", "--", ".github/scripts/ci_log.sh"],
        capture_output=True)
    assert proc.returncode == 0
    line = proc.stdout.decode().strip()
    assert line.startswith("100755 "), \
        f"ci_log.sh must be 100755 in the git tree, got: {line!r}"


def test_ci_log_is_executable_on_disk():
    assert os.access(CI_LOG, os.X_OK), "on-disk mode must match the tree mode"


# ── usage ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [[], ["--job", "x"], ["--nope"], ["1", "2"]])
def test_usage_errors_exit_2(tmp_path, args):
    env, argv_file, _ = _fake_gh(tmp_path, log_text="ok\n")
    proc = _run_ci_log(tmp_path, args, env)
    assert proc.returncode == 2
    assert proc.stdout == b""
    assert not argv_file.exists(), "usage errors must not fetch anything"


# ── redactor FILE-argument mode (unit level; t_914b8db5 added this) ─────────

def test_redactor_file_args_mode(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text(OFFENDER_OUT + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-E", str(REDACTOR), str(f)],
        capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()
    assert REAL_LAN.encode() not in proc.stdout
    assert proc.stdout == b"  docs/hosts.md:7: ***\n"


def test_redactor_file_args_are_byte_identical_when_clean(tmp_path):
    cases = {
        "plain": b"a\nb\nc\n",
        "no-trailing-newline": b"a\nb",
        "crlf": b"a\r\nb\r\n",
        "empty": b"",
        "not-utf8": b"caf\xe9 \xff guard line\n",
    }
    for name, data in cases.items():
        f = tmp_path / name
        f.write_bytes(data)
        proc = subprocess.run(
            [sys.executable, "-W", "ignore", "-E", str(REDACTOR), str(f)],
            capture_output=True)
        assert proc.returncode == 0, (name, proc.stderr)
        assert proc.stdout == data, f"{name}: clean bytes must round-trip exactly"


def test_redactor_dash_and_multi_file(tmp_path):
    a = tmp_path / "a.log"; b = tmp_path / "b.log"
    a.write_text(OFFENDER_OUT + "\n", encoding="utf-8")
    b.write_text("tail err " + REAL_LAN + "\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-E", str(REDACTOR), str(a), str(b)],
        capture_output=True)
    assert proc.returncode == 0
    assert REAL_LAN.encode() not in proc.stdout
    assert proc.stdout.count(b"***") == 2


def test_redactor_missing_file_fails_closed_without_path_echo(tmp_path):
    """rc=3 and a FIXED diagnostic — an OSError message would echo the path,
    and log paths on this repo have carried addresses in their names."""
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-E", str(REDACTOR),
         str(tmp_path / "no-such-file-9f3a.log")], capture_output=True)
    assert proc.returncode == 3
    assert b"no-such-file" not in proc.stderr
    assert redactor.MSG_UNEXPECTED.encode() in proc.stderr


def test_stdin_mode_is_unchanged(tmp_path):
    """The shipped workflow step pipes on stdin; FILE mode must not disturb it."""
    proc = subprocess.run(
        [sys.executable, "-W", "ignore", "-E", str(REDACTOR)],
        input=(OFFENDER_OUT + "\n").encode(), capture_output=True)
    assert proc.returncode == 0
    assert REAL_LAN.encode() not in proc.stdout
    assert proc.stdout == b"  docs/hosts.md:7: ***\n"


# ── the written rule exists where workers will read it ───────────────────────

def test_rule_is_written_in_the_audit(tmp_path):
    text = (REPO / "docs" / "audits" / "ci-backstop-address-guard-t_9a4b7687.md"
            ).read_text(encoding="utf-8")
    lowered = text.lower()
    assert "redact_guard_report.py" in lowered
    # the rule sentence itself: paste/attach/comment + scan-before + ci_log.sh
    assert "ci_log.sh" in lowered
    assert re.search(r"never paste[^.]*raw[^.]*log", lowered), \
        "the scan-before-attach rule must be written as a rule, not prose"
