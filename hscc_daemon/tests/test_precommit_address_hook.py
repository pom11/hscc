"""End-to-end tests for the commit-time address guard (.githooks/pre-commit).

Why these tests exist: the address check itself was already correct, and it still
missed a leak — because nothing ran it on a docs-only commit. So the thing under
test here is not the regex (that is exercised by the shared detector's own cases)
but the TRIGGER: a real ``git commit`` in a throwaway repo must fail when a real
address is staged, and must succeed otherwise. The repo is built by copying the
REAL ``.githooks/pre-commit`` and ``scripts/address_guard.py`` so we test the shipped
hook, not a reimplementation of it.

Stdlib + pytest only; every repo lives in ``tmp_path``; nothing touches the real
checkout's config or refs.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / ".githooks" / "pre-commit"
GUARD = REPO / "scripts" / "address_guard.py"

# Real-shaped fixtures: the live LAN /24 (see scripts/address_guard.py for what
# counts as real), so .244 is a real NAS host and is exactly what leaked on
# 2026-10-08. The placeholder forms are the documented replacements. Fixture
# addresses are ASSEMBLED AT RUNTIME below, because the repo's own guard scans
# this very file — a contiguous real-shaped literal here is (correctly) blocked
# by the hook this test exercises.
def _addr(*parts):
    return "".join(parts)


REAL_LAN = _addr("192", ".168.88.244")
REAL_SUBNET = REAL_LAN + "/24"
REAL_TAILNET = _addr("100.64", ".55.7")
LAN_PLACEHOLDER = "10.0.0.244"
TAILNET_PLACEHOLDER = "100.64.0.1"

GIT = ["git", "-c", "user.name=guard-test", "-c", "user.email=guard@example.invalid",
       "-c", "commit.gpgsign=false"]


def _run(args, cwd, env=None, input_bytes=None):
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, input=input_bytes,
                          env=env)
    return proc.returncode, proc.stdout.decode("utf-8", "ignore"), proc.stderr.decode("utf-8", "ignore")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A throwaway git repo carrying the real hook + the real detector."""
    r = tmp_path / "guarded"
    (r / "scripts").mkdir(parents=True)
    (r / ".githooks").mkdir()
    (r / "docs" / "audits").mkdir(parents=True)
    shutil.copy2(GUARD, r / "scripts" / "address_guard.py")
    shutil.copy2(HOOK, r / ".githooks" / "pre-commit")
    os.chmod(r / ".githooks" / "pre-commit", 0o755)

    rc, out, err = _run(GIT + ["init", "-q", "-b", "main", str(r)], tmp_path)
    assert rc == 0, err
    # The mechanism the card is about: a committed .githooks does nothing until
    # core.hooksPath points at it.
    rc, out, err = _run(GIT + ["config", "core.hooksPath", ".githooks"], r)
    assert rc == 0, err
    # Deterministic interpreter for the hook (also covers the HSCC_ADDRESS_GUARD_PY
    # override the hook honours).
    monkeypatch.setenv("HSCC_ADDRESS_GUARD_PY", sys.executable)
    _commit_all(r, "chore: seed (hook + detector)")
    return r


def _commit_all(r, msg):
    """Stage everything present and commit (seed commit)."""
    rc, out, err = _run(GIT + ["add", "-A"], r)
    assert rc == 0, err
    return _run(GIT + ["commit", "-q", "-m", msg], r)


def _commit(r, rel, content, msg, env=None):
    path = r / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    rc, out, err = _run(GIT + ["add", "--", rel], r, env=env)
    assert rc == 0, err
    return _run(GIT + ["commit", "-q", "-m", msg], r, env=env)


# ── the trigger actually fires ───────────────────────────────────────────────

def test_commit_of_docs_file_with_real_lan_address_is_blocked(repo):
    """THE regression this card exists for: a docs-only commit carrying the LAN."""
    rc, out, err = _commit(
        repo, "docs/audits/GOAL_LEDGER.md",
        f"# ledger tick\n\nremount: `sudo mount_nfs -o resvport {REAL_LAN}:/models /Volumes/NAS`\n",
        "docs: ledger tick",
    )
    assert rc != 0, f"commit should have been blocked\nSTDOUT={out}\nSTDERR={err}"
    assert "GOAL_LEDGER.md:3" in err, err          # offending file:line
    assert REAL_LAN in err, err                     # the matched address
    assert "10.0.0.x" in err and "100.64.0.1" in err, err  # documented placeholders
    # Nothing landed.
    rc, out, _ = _run(GIT + ["rev-parse", "--short", "HEAD"], repo)
    rc2, out2, _ = _run(GIT + ["log", "--oneline"], repo)
    assert out2.strip().count("\n") == 0, "the leaking commit must not exist"


def test_commit_covers_a_subdirectory_commit_and_a_non_python_extension(repo):
    """Docs coverage: hook fires for any tracked file type, from any cwd."""
    bad = repo / "notes"
    bad.mkdir()
    (bad / "runbook.txt").write_text(f"subnet {REAL_SUBNET}\n", encoding="utf-8")
    _run(GIT + ["add", "--", "notes/runbook.txt"], repo)
    # Commit from a SUBDIRECTORY: the hook resolves the top-level itself.
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "docs: runbook"], repo / "notes")
    assert rc != 0, f"commit from a subdir should be blocked\n{err}"
    assert "runbook.txt:1" in err, err


def test_placeholder_form_is_accepted(repo):
    """The documented placeholders must never trip the guard (else it gets bypassed)."""
    rc, out, err = _commit(
        repo, "docs/audits/SCRUBBED.md",
        f"tailnet host `{TAILNET_PLACEHOLDER}`; LAN node `{LAN_PLACEHOLDER}`; "
        f"subnet `{LAN_PLACEHOLDER.replace('.244', '.0')}/24`\n",
        "docs: scrubbed to placeholders",
    )
    assert rc == 0, f"placeholders must be accepted\nSTDOUT={out}\nSTDERR={err}"


def test_sanctioned_fixture_block_is_accepted(repo):
    """100.64.0.0/24 is the fixture block — tests need tailnet-shaped hosts."""
    rc, out, err = _commit(repo, "docs/hosts.md",
                           "gateway https://100.64.0.3:8787 and 100.64.0.1:8787\n",
                           "docs: fixture hosts")
    assert rc == 0, err


def test_real_tailnet_outside_fixture_block_is_blocked(repo):
    rc, out, err = _commit(repo, "docs/hosts.md", f"api at {REAL_TAILNET}:8787\n", "docs: leak")
    assert rc != 0, f"{REAL_TAILNET} is a real tailnet address\n{err}"
    assert "hosts.md:1" in err, err


def test_clean_staged_tree_is_a_noop(repo):
    rc, out, err = _commit(repo, "docs/clean.md", "# nothing to see\n", "docs: clean")
    assert rc == 0, f"{out}{err}"
    rc, out, _ = _run(GIT + ["status", "--porcelain"], repo)
    assert out.strip() == ""


def test_unstaged_dirt_is_not_the_staged_tree(repo):
    """The guard reads the INDEX. An unstaged edit must not block an unrelated
    commit — and, conversely, a scrubbed working tree must not hide a dirty blob
    (proven by test_staged_blob_beats_scrubbed_worktree)."""
    (repo / "docs" / "dirty.md").write_text(f"leak {REAL_LAN}\n", encoding="utf-8")
    rc, out, err = _run(GIT + ["commit", "-q", "--allow-empty", "-m", "chore: unrelated"], repo)
    assert rc == 0, f"unstaged dirt is not staged content\n{err}"


def test_staged_blob_beats_scrubbed_worktree(repo):
    """The 2026-08-30 failure mode: dirty blob staged, working tree scrubbed.

    Grepping the working tree (the old pre-push check) would let this commit
    through. The hook must read the staged blob and block it.
    """
    leaky = repo / "docs" / "audit.md"
    leaky.write_text(f"live host {REAL_LAN}\n", encoding="utf-8")
    _run(GIT + ["add", "--", "docs/audit.md"], repo)
    leaky.write_text("host 10.0.0.x\n", encoding="utf-8")  # operator scrubs the tree
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "docs: audit"], repo)
    assert rc != 0, "a scrubbed working tree must not mask a dirty staged blob\n" + err
    assert "audit.md:1" in err, err


def test_renamed_and_deleted_files_do_not_break_the_guard(repo):
    _commit(repo, "docs/old.md", "# old\n", "docs: old file")
    (repo / "docs" / "old.md").unlink()
    (repo / "docs" / "new.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _run(GIT + ["add", "-A"], repo)
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "docs: rename with leak"], repo)
    assert rc != 0, err
    assert "new.md:1" in err, err
    # And a pure deletion commits fine.
    _run(GIT + ["reset", "-q", "--hard"], repo)
    (repo / "docs" / "old.md").unlink()
    _run(GIT + ["add", "-A"], repo)
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "docs: delete"], repo)
    assert rc == 0, err


def test_binary_and_unusual_files_are_survivable(repo):
    (repo / "asset.bin").write_bytes(b"\x00\x01" + REAL_LAN.encode() + b"\x00")
    (repo / "weird name with spaces.md").write_text(f"host {REAL_LAN}\n", encoding="utf-8")
    _run(GIT + ["add", "-A"], repo)
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "feat: asset"], repo)
    assert rc != 0, err
    assert "weird name with spaces.md:1" in err, err   # spaces in paths handled
    assert "asset.bin" not in err                      # binary skipped, not decoded


def test_tracked_build_artefact_is_blocked_by_the_hook(repo):
    """t_3e6d3db7: a committed `.pyc` was invisible to all three gates.

    The blob is NUL-bearing, so the pre-policy scanner returned [] for it and the
    commit went through. The name-based refusal must stop it at the hook.
    """
    pyc = repo / "hscc-provision" / "__pycache__"
    pyc.mkdir(parents=True)
    (pyc / "hscc.cpython-313.pyc").write_bytes(
        b"\xe3\r\r\n\x00\x00\x00\x00\x00" + REAL_LAN.encode() + b"\x00\x00")
    rc, out, err = _run(GIT + ["add", "-A"], repo)
    assert rc == 0, err
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "chore: build output"], repo)
    assert rc != 0, f"a tracked .pyc must be refused\nSTDOUT={out}\nSTDERR={err}"
    assert "hscc-provision/__pycache__/hscc.cpython-313.pyc" in err, err
    assert "git rm --cached" in err, err
    rc, out, _ = _run(GIT + ["log", "--oneline"], repo)
    assert out.strip().count("\n") == 0, "the artefact commit must not exist"


def test_hook_and_tracked_agree_on_a_tracked_build_artefact(repo):
    """Same input, both gates, same verdict — they share `scan_blob`.

    The card's test requirement, and the reason the artefact verdict is formatted
    in ONE place: the hook (staged) and the CI backstop / pytest gate (tracked)
    must never disagree about the same tree. `--no-verify` gets the blob into
    HEAD here so the tracked scan has something to find; that is the state a
    bypass leaves behind, and it is exactly what the backstop has to catch.
    """
    pyc = repo / "scripts" / "__pycache__"
    pyc.mkdir(parents=True)
    (pyc / "mod.cpython-313.pyc").write_bytes(
        b"\xe3\r\r\n\x00\x00\x00\x00\x00" + REAL_TAILNET.encode() + b"\x00\x00")
    _run(GIT + ["add", "-A"], repo)
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "chore: bypassed", "--no-verify"], repo)
    assert rc == 0, err

    # Gate 1: the hook, over the staged artefact. The blob is in HEAD now, so the
    # hook's contract ("judge what this commit introduces") needs the path to be
    # *changed* to appear in the staged set again — a re-`add` with new bytes is
    # the state where the hook gets a second chance to stop it.
    (pyc / "mod.cpython-313.pyc").write_bytes(
        b"\xe3\r\r\n\x00\x00\x00\x00\x01" + REAL_TAILNET.encode() + b"\x00\x00")
    (repo / "touch.md").write_text("x\n", encoding="utf-8")
    _run(GIT + ["add", "-A"], repo)
    rc, hook_out, hook_err = _run(GIT + ["commit", "-q", "-m", "chore: next"], repo)
    assert rc != 0, "the hook must still refuse the staged artefact"

    # Gate 2: the CLI the CI job runs (`python3 scripts/address_guard.py --tracked`),
    # so the comparison is between the two shipped invocations, not two imports.
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--tracked", "--repo", str(repo)],
        capture_output=True)
    assert proc.returncode == 1, proc.stderr.decode()
    tracked = proc.stderr.decode().splitlines()

    named_by_hook = [ln for ln in hook_err.splitlines()
                     if "mod.cpython-313.pyc" in ln]
    named_by_tracked = [ln for ln in tracked if "mod.cpython-313.pyc" in ln]
    assert named_by_hook and named_by_tracked, (hook_err, tracked)
    # And not merely "both mentioned it": the verdict strings are identical.
    assert named_by_hook[0].strip() == named_by_tracked[0].strip(), (
        f"hook says {named_by_hook[0]!r}, tracked says {named_by_tracked[0]!r}")


def test_a_genuinely_compiled_pyc_is_blocked_by_the_hook(repo, tmp_path):
    """End-to-end with REAL compiler output, not a hand-built blob.

    The orchestrator's probe (2026-10-09) showed a genuinely compiled `.pyc` of
    leaking source is invisible even to the shipping FORBIDDEN pattern once
    decoded as text — in marshal's framing the byte after the string constant is
    a word char, so the pattern's trailing `\\b` fails. The hook's refusal never
    looks at the bytes, so it is the only local control that catches this blob.
    """
    # Compile real source that carries a real-shaped address (assembled at
    # runtime; the repo's own guard scans this file).
    src = tmp_path / "leaky_source.py"
    src.write_text("NAS = " + repr(REAL_LAN) + "\n", encoding="utf-8")
    import py_compile
    py_compile.compile(str(src), cfile=str(tmp_path / "leaky.pyc"), doraise=True)
    blob = (tmp_path / "leaky.pyc").read_bytes()
    assert b"\0" in blob and REAL_LAN.encode() in blob

    # `git add -f` past the .gitignore — the accident this card is about.
    target = repo / "vendor_pkg" / "__pycache__"
    target.mkdir(parents=True)
    (target / "leaky.cpython-313.pyc").write_bytes(blob)
    rc, out, err = _run(GIT + ["add", "-f", "-A"], repo)
    assert rc == 0, err
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "chore: compiled"], repo)
    assert rc != 0, f"a real compiled .pyc must be refused\nSTDOUT={out}\nSTDERR={err}"
    assert "vendor_pkg/__pycache__/leaky.cpython-313.pyc" in err, err


def test_a_genuinely_compiled_pyc_on_a_widened_hatch_is_still_blocked_by_the_hook(repo, tmp_path):
    """The two-tier hatch, end to end through the real hook.

    Round-1 review of t_3e6d3db7: with a one-tier hatch, a reviewer who widened
    `ALLOWED_BINARY_PATHS` to cover a `.pyc` would have opened a silent hole —
    the refusal waived, and the text scan blind to compiled bytes. This runs the
    widened-hatch guard under a real `git add -f` + `git commit` so the guarantee
    is proven at the trigger, not only in the unit.
    """
    guard = repo / "scripts" / "address_guard.py"
    src = guard.read_text(encoding="utf-8")
    assert "ALLOWED_BINARY_PATHS = frozenset()" in src, (
        "the guard's hatch changed shape; update this test's patch site")
    src = src.replace("ALLOWED_BINARY_PATHS = frozenset()",
                      'ALLOWED_BINARY_PATHS = frozenset({"vendor/leak.pyc"})', 1)
    guard.write_text(src, encoding="utf-8")
    _run(GIT + ["add", "-A"], repo)
    _run(GIT + ["commit", "-q", "-m", "chore: widen hatch", "--no-verify"], repo)

    src_file = tmp_path / "leaky_source.py"
    src_file.write_text("NAS = " + repr(REAL_LAN) + "\n", encoding="utf-8")
    import py_compile
    py_compile.compile(str(src_file), cfile=str(tmp_path / "leaky.pyc"), doraise=True)
    (repo / "vendor").mkdir()
    (repo / "vendor" / "leak.pyc").write_bytes((tmp_path / "leaky.pyc").read_bytes())
    _run(GIT + ["add", "-f", "-A"], repo)
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "chore: compiled on hatch"], repo)
    assert rc != 0, (
        "a hatch entry must never make a compiled artefact trackable and unscanned"
        f"\nSTDOUT={out}\nSTDERR={err}")
    assert "vendor/leak.pyc" in err, err


def test_a_trailing_byte_artefact_name_is_blocked_by_the_hook_and_named_alike(repo, tmp_path):
    """The card's ASK 2: a real `git add -f` + `git commit` of `evil.pyc `.

    Reviewer round 3 of t_3e6d3db7 measured that `git add -A --force` tracks
    `evil.pyc ` verbatim while BOTH gates returned [] — the suffix test ran
    against the raw string. This pins it at the git level, not in the unit: real
    `py_compile` output, real index, real hook.

    The trailing space is load-bearing. The verdict must still CARRY it, or the
    fix the report prescribes (`git rm --cached <path>`) would name a file that
    does not exist and the two gates would describe the same tree with two
    different strings — t_3e6d3db7 §4's agreement failure, one spelling over.
    """
    src = tmp_path / "leaky_source.py"
    src.write_text("NAS = " + repr(REAL_LAN) + "\n", encoding="utf-8")
    import py_compile
    py_compile.compile(str(src), cfile=str(tmp_path / "evil.pyc"), doraise=True)
    blob = (tmp_path / "evil.pyc").read_bytes()
    assert b"\0" in blob and REAL_LAN.encode() in blob, "the premise is a real .pyc"

    name = "evil.pyc "                       # the trailing space IS the bypass
    (repo / name).write_bytes(blob)
    rc, out, err = _run(GIT + ["add", "-f", "-A"], repo)
    assert rc == 0, err

    # Premise check in RAW bytes (`_run` decodes, which is exactly what would hide
    # a NUL): if git stopped tracking the trailing-space name this test would be
    # silently proving nothing.
    raw = subprocess.run(GIT + ["ls-files", "-z"], cwd=str(repo),
                         capture_output=True).stdout
    assert b"evil.pyc \x00" in raw, (
        f"git did not track the trailing-space name; premise gone: {raw!r}")

    # Gate 1 — the shipped hook refuses the commit.
    rc, out, hook_err = _run(GIT + ["commit", "-q", "-m", "chore: compiled"], repo)
    assert rc != 0, "a trailing-space .pyc must be refused\nSTDOUT=%s\nSTDERR=%s" % (out, hook_err)
    rc, out, _ = _run(GIT + ["log", "--oneline"], repo)
    assert out.strip().count("\n") == 0, "the artefact commit must not exist"

    # The state a bypass leaves behind: `--no-verify` gets it into HEAD.
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "chore: bypassed", "--no-verify"], repo)
    assert rc == 0, err

    # Gate 2 — the CLI the CI job / pytest gate runs over the tracked tree.
    tracked = subprocess.run([sys.executable, str(GUARD), "--tracked", "--repo", str(repo)],
                             capture_output=True)
    assert tracked.returncode == 1, tracked.stderr.decode()

    # Gate 3 — the same CLI over the staged tree (what the hook wraps). Re-add
    # with new bytes: after the commit the staged set is empty, and an empty
    # staged set would make "both gates agree" true of nothing.
    (repo / name).write_bytes(blob + b"\x00")
    assert _run(GIT + ["add", "-f", "-A"], repo)[0] == 0
    staged = subprocess.run([sys.executable, str(GUARD), "--staged", "--repo", str(repo)],
                            capture_output=True)
    assert staged.returncode == 1, staged.stderr.decode()

    # The agreement property, stated as the card asks it: all three shipped
    # surfaces name the SAME path with the SAME verdict string.
    want = "evil.pyc :0: build artefact must not be tracked"
    for label, text in (("hook", hook_err),
                        ("--tracked", tracked.stderr.decode()),
                        ("--staged", staged.stderr.decode())):
        assert want in text, f"{label} does not name {want!r}:\n{text}"


def test_a_legitimate_binary_asset_still_commits(repo):
    """The new rule must not break the binaries that belong here.

    The tracked tree carries two .png files; a policy that refuses them is a
    policy that gets --no-verify'd on its first real use.
    """
    (repo / "assets").mkdir()
    (repo / "assets" / "hscc.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x0dIHDR")
    rc, out, err = _run(GIT + ["add", "-A"], repo)
    assert rc == 0, err
    rc, out, err = _run(GIT + ["commit", "-q", "-m", "feat: logo"], repo)
    assert rc == 0, f"a real asset must commit\nSTDOUT={out}\nSTDERR={err}"


def test_guard_is_dependency_free_and_bounded(repo):
    """Requirement 4: fast + stdlib-only. A slow leak check gets --no-verify'd."""
    src = GUARD.read_text(encoding="utf-8")
    imported = set()
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("import "):
            imported |= {t.strip().split(" ")[0].split(".")[0] for t in s[len("import "):].split(",")}
    allowed = {"argparse", "re", "subprocess", "sys", "pathlib", "__future__", "json", "os"}
    assert imported <= allowed, f"non-stdlib/unknown import in the guard: {imported - allowed}"

    # One batched blob read: at most a handful of git calls regardless of file count.
    payload = "\n".join(f"line {i} ok" for i in range(200))
    for i in range(12):
        (repo / f"docs/f{i}.md").write_text(payload, encoding="utf-8")
    _run(GIT + ["add", "-A"], repo)
    env = dict(os.environ, HSCC_ADDRESS_GUARD_PY=sys.executable)
    started = time.monotonic()
    rc, out, err = _run([sys.executable, str(GUARD), "--staged", "--repo", str(repo)],
                        repo, env=env)
    elapsed = time.monotonic() - started
    assert rc == 0, err
    assert elapsed < 5.0, f"guard took {elapsed:.2f}s on 13 staged files — too slow to avoid bypass"


def test_hook_survives_without_the_env_override(repo, monkeypatch):
    """Without HSCC_ADDRESS_GUARD_PY the hook must still find an interpreter."""
    monkeypatch.delenv("HSCC_ADDRESS_GUARD_PY", raising=False)
    if shutil.which("python3") is None and shutil.which("python") is None:
        pytest.skip("no python on PATH in this environment")
    rc, out, err = _commit(repo, "docs/hosts.md", f"api {REAL_TAILNET}:8787\n", "docs: leak")
    assert rc != 0, f"hook must still fire with interpreter discovery\n{err}"


def test_hook_fails_closed_if_detector_is_missing(repo):
    """A guard that cannot run must block the commit, not wave it through."""
    (repo / "scripts" / "address_guard.py").unlink()
    rc, out, err = _commit(repo, "docs/x.md", "harmless\n", "docs: x")
    assert rc != 0, "missing detector must fail closed"
    assert "cannot run" in err.lower() or "missing" in err.lower(), err


def test_missing_hooks_path_config_is_skipped_not_faked(tmp_path):
    """install_hooks must not claim success for a repo with no committed hook."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "install_hooks_under_test", REPO / "hscc-bootstrap" / "install_hooks.py")
    install_hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install_hooks)

    r = tmp_path / "nohooks"
    (r / "scripts").mkdir(parents=True)
    shutil.copy2(GUARD, r / "scripts" / "address_guard.py")
    _run(GIT + ["init", "-q", str(r)], tmp_path)
    res = install_hooks.install_hooks(r)
    assert res["action"] == "skipped", res
    assert install_hooks.current_hooks_path(r) is None, "must not set hooksPath for a hookless repo"
