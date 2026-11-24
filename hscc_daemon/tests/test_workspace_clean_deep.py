"""The cache tiers of `hscc clean` (card t_701e22fc) — planning, the gate, execution.

The workspace tier has its own files (t_274530ae, t_fc9296a7) and this one never
re-tests them. What lives here is the contract card 3 owns:

  1. the two tiers are two tiers: the safe set is planned with no ``--confirm``
     anywhere in sight, the deep set is ABSENT from a plan built without
     ``deep=True``, and ``uv`` appears as ``prune`` or as ``clean`` — never both;
  2. the gate: ``--deep --apply`` without ``--confirm`` exits 2, names the token,
     and never reaches the engine at all;
  3. machinery, not failure: a tool that is not installed is
     ``skipped reason="not-installed"`` and the run stays green, a command that
     outlives its cap is killed, lands in ``errors``, and the pass continues;
  4. the two things that make this safe to run on a live host — report-only
     entries are never executed under any tier, and nothing under ``$HOME``'s
     ``.hermes``/``.hscc``/``dev`` is ever a removal target, including for a
     plan that arrived as a dict and named one on purpose;
  5. sizes are measured in the plan and never re-walked by the apply.

SAFETY — read this before editing. Every test here runs against the
``cache_sandbox`` fixture below: ``HOME``/``TMPDIR`` inside ``tmp_path``, and
``PATH`` whose FIRST entry is a directory of planted shims. The seven binaries
this engine may exec are shimmed to append their argv to a log file, so no real
``brew``/``uv``/``npm``/``conda``/``xcrun``/``pip``/``osascript`` on this host
can be reached from a test, and the Trash shim cannot empty anything. That
fixture is autouse HERE and imported by the two older clean-test files, because
since this card ``--apply`` means "run the cache commands" — an unsandboxed
``--apply`` in any of the three files would prune the operator's real caches.
"""

import io
import json
import os
import stat
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import workspace_clean as wc

DAY = 86400
#: The REAL clock, the way t_fc9296a7 stamps its fixtures. A fixed future date
#: would be a trap here: the cache age filter compares live directory mtimes
#: against ``now``, so a 2027 clock makes every fresh temp dir look ancient.
#: Card stamps are relative to this stamp, and the plan is asked for ``now=NOW``
#: so one test is one instant.
NOW = int(time.time())
PAST = NOW - 40 * DAY        # a finished card well past the 7-day default

#: Every executable the cache tiers may name. A shim for each is planted, so an
#: unshimmed name appearing in a test means the engine grew a new one and this
#: list — and the safety story — has to grow with it.
SHIMMED = ("uv", "npm", "conda", "brew", "python3", "xcrun", "osascript")

PROTECTED = (".hermes", ".hscc", "dev")


# ── the sandbox ──────────────────────────────────────────────────────────── #

class Sandbox:
    """A fake host: HOME + TMPDIR under tmp_path, shimmed binaries, one log.

    ``absent`` is the not-installed switch, and it is enforced at the engine's
    `shutil.which` seam rather than by deleting the shim: the sandbox keeps the
    host's own ``PATH`` entries (t_274530ae's tests legitimately run real ``git``
    and ``tar``), so a deleted shim would simply expose the real ``brew`` behind
    it. Deleting a file cannot make a tool uninstalled here; the probe is what
    answers "not-installed", and this makes the probe answer it.
    """

    def __init__(self, home: Path, tmp: Path, bin_dir: Path, log: Path):
        self.home, self.tmp, self.bin, self.log = home, tmp, bin_dir, log
        self.planted = set()
        self.absent = set()

    # --- fixture-side ------------------------------------------------------ #
    def plant(self, name: str, *, rc: int = 0, sleep: float = 0.0) -> Path:
        """A shim that logs `"<name> <argv…>"`, sleeps, then exits ``rc``."""
        body = ['#!/bin/sh',
                f'printf \'%s %s\\n\' "{name}" "$*" >> "$HSCC_CLEAN_SHIM_LOG"']
        if sleep:
            body.append(f"sleep {sleep}")
        body.append(f"exit {rc}")
        shim = self.bin / name
        shim.write_text("\n".join(body) + "\n")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.planted.add(name)
        self.absent.discard(name)
        return shim

    def drop(self, *names: str) -> None:
        """Make tools report as not installed — at the probe, not by luck."""
        for name in names:
            self.absent.add(name)
            (self.bin / name).unlink(missing_ok=True)
            self.planted.discard(name)

    def plant_at(self, rel: str, *, rc: int = 0) -> Path:
        """A shim at a path inside HOME (the conda real-path probe)."""
        target = self.home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'#!/bin/sh\nprintf \'%s %s\\n\' "conda-real" "$*" '
                          f'>> "$HSCC_CLEAN_SHIM_LOG"\nexit {rc}\n')
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return target

    # --- assertions -------------------------------------------------------- #
    def calls(self) -> list:
        """``[(argv0, arg1, …), …]`` from the shim log, in call order."""
        if not self.log.exists():
            return []
        return [ln.split() for ln in self.log.read_text().splitlines() if ln]

    def names(self) -> set:
        return {tuple(c)[0] for c in self.calls()}

    def snapshot(self) -> set:
        """Every path under HOME and TMPDIR — the mutation witness."""
        return {str(p) for root in (self.home, self.tmp) if root.is_dir()
                for p in root.rglob("*")}


@pytest.fixture(autouse=True)
def cache_sandbox(tmp_path, monkeypatch):
    """Autouse everywhere in this file: no real cache binary is reachable.

    Two independent guarantees, because ``--apply`` on this card means "run the
    cache commands" and the operator's caches are real:

      1. every one of the seven tools resolves INTO the shim dir (``PATH`` puts
         it first), so a command that does run runs a script that only logs;
      2. :meth:`Sandbox.drop` removes a tool at the probe seam, so a test can
         never uncover a real one that was hiding behind a deleted shim.
    """
    home, tmp, bin_dir = tmp_path / "home", tmp_path / "tmp", tmp_path / "bin"
    log = tmp_path / "shim.log"
    for d in (home, tmp, bin_dir):
        d.mkdir(parents=True, exist_ok=True)
    box = Sandbox(home, tmp, bin_dir, log)
    for name in SHIMMED:
        box.plant(name)
    if os.environ.get("HSCC_CLEAN_TEST_NO_TOOLS"):
        # Regression runs of the OTHER clean test files (t_274530ae's and
        # t_fc9296a7's, which predate this sandbox) set this: every cache tool
        # then reports not-installed, so no cache command of any kind can run
        # while those files are exercised.
        box.drop(*SHIMMED)
    real_which = wc.shutil.which

    def which(name, *args, **kwargs):
        return None if str(name) in box.absent \
            else real_which(name, *args, **kwargs)

    monkeypatch.setattr(wc.shutil, "which", which)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setenv("USER", "tester")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HSCC_CLEAN_SHIM_LOG", str(log))
    return box


@pytest.fixture
def lib(tmp_path):
    """The engine's fake board authority — with one readable (empty) board.

    The board exists so a test that never plants a workspace still gets a clean
    plan instead of an ``unreadable board`` error: with no board at all hermes'
    enumeration falls back to the legacy default DB, which this home has not
    got, and every ``rc == 0`` assertion below would be about the wrong thing.
    """
    from hscc_daemon.tests.test_workspace_clean import FakeLib
    fake = FakeLib(tmp_path / "hermes-home")
    fake.add_board("hscc")
    return fake


def make_ws(lib, slug, task_id, *, completed_at=PAST):
    if slug not in lib.boards:
        lib.add_board(slug)
    lib.add_task(slug, task_id, status="done", completed_at=completed_at,
                 created_at=completed_at)
    ws = lib.workspace(slug, task_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "report.md").write_bytes(b"z" * 4096)
    return ws


def seed_caches(box: Sandbox) -> dict:
    """One directory per report-only path, both deep targets, and the two
    safe-tier directory kinds (a stale pytest dir, one UI-test cache)."""
    made = {}
    for rel in ((".cache", "uv"), (".npm",), (".lmstudio",), (".Trash",),
                (".hermes", "profiles"), (".hscc",), ("dev", "repo"),
                ("Library", "Developer", "CoreDevice", "DeviceFS"),
                ("Library", "Developer", "Xcode", "DerivedData"),
                ("Library", "Developer", "CoreSimulator", "Caches"),
                (".cache", "hscc-uitest-demo")):
        d = box.home.joinpath(*rel)
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob.bin").write_bytes(b"q" * 1024)
        made["/".join(rel)] = d
    stale = box.tmp / f"{wc.PYTEST_TEMP_PREFIX}{os.environ['USER']}" / "pytest-1"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "blob.bin").write_bytes(b"q" * 1024)
    old = time.time() - 3 * DAY
    os.utime(stale, (old, old))
    made["pytest-1"] = stale
    return made


def plan_for(lib, box, *, deep=False, days=7):
    """The engine's plan against the sandbox host."""
    return wc.plan_clean(kanban_db=lib, days=days, now=NOW, deep=deep)


def run_clean(argv, lib):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = unified._handle_clean(argv, kanban_db=lib)
    return rc, out.getvalue()


def main_clean(monkeypatch, argv):
    """Drive ``main()`` so the gate is proven on the real argv path, rc included."""
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    out, err = io.StringIO(), io.StringIO()
    rc = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            unified.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue(), err.getvalue()


def named(plan, name):
    return [e for e in plan["cache_actions"] if e["name"] == name]


def argv_of(plan, name):
    rows = named(plan, name)
    assert rows, f"{name} is not in the plan"
    return rows[0]["argv"]


def paths_touched(result):
    """Every directory the executor actually removed (kind cache_path, ran)."""
    return [e["path"] for e in result["cache"]
            if e["kind"] == wc.KIND_PATH and e["status"] == "ran"]


# ── 1. the safe set: planned with no --confirm anywhere ──────────────────── #

class TestSafeTier:
    def test_safe_set_is_planned_without_confirm(self, lib):
        plan = plan_for(lib, None)
        by = {e["name"]: e for e in plan["cache_actions"]}
        assert by["uv cache"]["argv"][-1] == "prune"
        assert by["uv cache"]["tier"] == wc.TIER_SAFE
        assert by["npm cache"]["argv"][-3:] == ["cache", "clean", "--force"]
        assert by["conda pkgs"]["argv"][-3:] == ["clean", "--all", "-y"]
        assert by["homebrew"]["argv"][-2:] == ["cleanup", "--prune=30"]
        assert by["pip cache"]["argv"][-4:] == ["-m", "pip", "cache", "purge"]
        assert by["simulators"]["argv"][-2:] == ["delete", "unavailable"]
        assert all(e["tier"] == wc.TIER_SAFE
                   for e in plan["cache_actions"]
                   if e["kind"] == wc.KIND_COMMAND and e["name"] != "uv cache")

    def test_pytest_dirs_are_filtered_by_age(self, lib, cache_sandbox):
        # The prefix is the ENGINE's constant, not spelled here: this file is
        # swept by test_planted_hermes_cli_single_source for exactly that copy.
        root = cache_sandbox.tmp / f"{wc.PYTEST_TEMP_PREFIX}tester"
        old = root / "pytest-1"
        new = root / "pytest-2"
        for d in (old, new):
            d.mkdir(parents=True)
            (d / "f").write_bytes(b"a")
        stamp = time.time() - 3 * DAY
        os.utime(old, (stamp, stamp))
        plan = plan_for(lib, cache_sandbox)
        rows = named(plan, wc.PYTEST_TEMP_NAME)
        assert [r["path"] for r in rows] == [str(old)], rows
        assert rows[0]["tier"] == wc.TIER_SAFE
        assert (root / "pytest-2").is_dir(), "a young temp dir was a candidate"

    def test_uitest_dirs_are_matched_by_prefix(self, lib, cache_sandbox):
        cache = cache_sandbox.home / ".cache"
        (cache / "hscc-uitest-one").mkdir(parents=True)
        (cache / "hscc-uitest-two").mkdir(parents=True)
        (cache / "something-else").mkdir(parents=True)
        plan = plan_for(lib, cache_sandbox)
        rows = named(plan, wc.UITEST_NAME)
        assert sorted(Path(r["path"]).name for r in rows) == [
            "hscc-uitest-one", "hscc-uitest-two"]
        assert all(r["root"] == str(cache) for r in rows)

    def test_sizes_are_measured_in_the_plan(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox)
        uv = named(plan, wc.UV_NAME)[0]
        assert uv["size_bytes"] >= 1024, uv
        report = {e["name"]: e for e in plan["report_only"]}
        assert report["LM Studio models"]["size_bytes"] >= 1024

    def test_apply_does_not_re_walk_the_trees(self, lib, cache_sandbox,
                                              monkeypatch):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox)
        monkeypatch.setattr(wc, "_dir_bytes",
                            lambda p: (_ for _ in ()).throw(
                                AssertionError("re-walked during apply")))
        result = wc.execute(plan, apply=True, kanban_db=lib)
        assert result["errors"] == []


# ── 2. the deep set only exists under deep=True ──────────────────────────── #

class TestDeepTier:
    def test_deep_actions_are_absent_from_a_plain_plan(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox, deep=False)
        text = json.dumps(plan)
        assert "DerivedData" not in text
        assert "empty trash" not in text
        assert not [e for e in plan["cache_actions"] if e["tier"] == wc.TIER_DEEP]
        assert argv_of(plan, wc.UV_NAME)[-1] == "prune", "safe tier ran clean"

    def test_deep_plan_carries_the_deep_set(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox, deep=True)
        assert plan["deep"] is True
        rows = {e["name"]: e for e in plan["cache_actions"]
                if e["tier"] == wc.TIER_DEEP}
        assert set(rows) == {wc.UV_NAME, wc.DERIVED_DATA_NAME,
                             wc.SIMULATOR_CACHE_NAME, wc.TRASH_NAME}, rows
        assert rows[wc.UV_NAME]["argv"][-1] == "clean"
        assert rows[wc.DERIVED_DATA_NAME]["kind"] == wc.KIND_PATH
        assert rows[wc.SIMULATOR_CACHE_NAME]["kind"] == wc.KIND_PATH
        assert argv_of(plan, wc.UV_NAME)[-1] == "clean"

    def test_uv_appears_once_prune_or_clean_never_both(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        for deep, verb in ((False, "prune"), (True, "clean")):
            rows = named(plan_for(lib, cache_sandbox, deep=deep), wc.UV_NAME)
            assert len(rows) == 1, rows
            assert rows[0]["argv"][-1] == verb

    def test_trash_goes_through_finder_never_a_delete(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox, deep=True)
        trash = named(plan, wc.TRASH_NAME)[0]
        assert Path(trash["argv"][0]).name == "osascript"
        assert trash["argv"][1:] == ["-e", wc.TRASH_APPLESCRIPT]
        assert trash["kind"] == wc.KIND_COMMAND, "the Trash is never a path row"
        # Belt over the same rule from the other side: nothing in the plan may
        # carry the Trash as a removable directory, because the engine's one
        # recursive-delete path (cache_path rows) must never see it.
        assert not [p for p in plan["cache_actions"]
                    if p["kind"] == wc.KIND_PATH and p["name"] == wc.TRASH_NAME]

    def test_deep_removals_run_only_under_deep(self, lib, cache_sandbox):
        made = seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox, deep=True)
        result = wc.execute(plan, apply=True, kanban_db=lib)
        gone = paths_touched(result)
        assert str(made["Library/Developer/Xcode/DerivedData"]) in gone
        assert str(made["Library/Developer/CoreSimulator/Caches"]) in gone
        assert made["Library/Developer/Xcode/DerivedData"].exists() is False
        assert made[".Trash"].is_dir(), "the Trash directory itself survives"
        assert "osascript -e" in " ".join(" ".join(c) for c in
                                          cache_sandbox.calls())


# ── 3. the --confirm gate ────────────────────────────────────────────────── #

class TestConfirmGate:
    def test_deep_apply_without_confirm_exits_2_and_names_the_token(
            self, monkeypatch):
        monkeypatch.setattr(wc, "plan_clean",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("planned a refused deep run")))
        rc, out, err = main_clean(monkeypatch, ["clean", "--apply", "--deep"])
        assert rc == 2, (out, err)
        assert "usage: hscc clean" in err
        assert "--confirm" in err, err

    def test_refusal_mutates_nothing(self, cache_sandbox, monkeypatch):
        box = cache_sandbox
        made = seed_caches(box)
        before = box.snapshot()
        rc, out, err = main_clean(monkeypatch, ["clean", "--apply", "--deep"])
        assert rc == 2
        assert box.calls() == [], "a refused deep run ran a command"
        assert box.snapshot() == before
        assert made[".Trash"].is_dir()

    def test_deep_alone_is_allowed_and_inert(self, lib, cache_sandbox):
        box = cache_sandbox
        seed_caches(box)
        before = box.snapshot()
        rc, out = run_clean(["--deep"], lib)
        assert rc == 0, out
        assert box.calls() == [], "a report ran a mutating command"
        assert box.snapshot() == before
        assert "MODE: DRY RUN" in out
        assert "deep set" in out

    def test_deep_dry_run_is_allowed_even_with_confirm(self, lib, cache_sandbox):
        rc, out = run_clean(["--deep", "--confirm"], lib)
        assert rc == 0 and "MODE: DRY RUN" in out

    def test_deep_apply_with_confirm_runs(self, lib, cache_sandbox):
        made = seed_caches(cache_sandbox)
        rc, out = run_clean(["--apply", "--deep", "--confirm"], lib)
        assert rc == 0, out
        assert "MODE: APPLY" in out
        assert made["Library/Developer/Xcode/DerivedData"].exists() is False
        assert "uv cache clean" in " ".join(" ".join(c)
                                            for c in cache_sandbox.calls())

    def test_confirm_alone_is_harmless(self, lib, cache_sandbox):
        rc, out = run_clean(["--confirm"], lib)
        assert rc == 0 and "MODE: DRY RUN" in out
        assert cache_sandbox.calls() == []

    def test_unknown_flags_are_still_refused(self, monkeypatch):
        for flag in ("--deepest", "--force"):
            rc, out, err = main_clean(monkeypatch, ["clean", flag])
            assert rc == 2 and f"unknown flag: {flag}" in err


# ── 4. machinery, not failure ────────────────────────────────────────────── #

class TestMissingAndHangingTools:
    def test_a_missing_tool_is_skipped_and_the_run_is_green(
            self, lib, cache_sandbox):
        cache_sandbox.drop("brew", "xcrun", "osascript", "conda")
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox, deep=True)
        for name in (wc.BREW_NAME, wc.SIMCTL_NAME, wc.CONDA_NAME,
                     wc.TRASH_NAME):
            row = named(plan, name)[0]
            assert row["status"] == "skipped", row
            assert row["reason"] == "not-installed", row
            assert row["reason"] in wc.CACHE_REASONS
        result = wc.execute(plan, apply=True, kanban_db=lib)
        assert result["errors"] == [], "a Mac without Xcode turned red"
        assert {e["name"]: e["status"] for e in result["cache"]}[wc.SIMCTL_NAME] \
            == "skipped"

    def test_a_timing_out_tool_lands_in_errors_and_the_pass_continues(
            self, lib, cache_sandbox, monkeypatch):
        """One command outliving its cap is killed; the pass runs on.

        The compressed cap goes to THE HANG ONLY, injected at ``_run`` — the seam
        the engine itself names for this ("patch this, not ``subprocess.run``").
        It is deliberately NOT compressed by shrinking :data:`CACHE_TIMEOUT` for
        the whole pass. That constant is the production 300 s because a
        ``brew cleanup`` can wedge on a tap fetch, and shrinking it globally put
        every OTHER command in the same pass on a 200 ms wall-clock race it was
        never meant to enter: measured at ``f2f8bc2d`` under three concurrent
        ``hscc_daemon`` legs (load avg 11-16), 1 of 14 samples went red with
        ``uv cache`` missing from ``ran`` while ``npm cache``, ``conda pkgs`` and
        ``simulators`` were in it. The pass HAD continued; the case failed anyway,
        because it was asserting a budget in a behaviour assertion's clothes.
        """
        cache_sandbox.plant("brew", sleep=5)
        hang_cap = 1                           # the hang's cap only; see below
        production_cap = wc.CACHE_TIMEOUT      # whatever ships today; the case
        monkeypatch.setattr(wc, "CACHE_TIMEOUT", hang_cap)   # must not depend on
        real_run = wc._run                     # the number, only on its being
        caps = []                              # kept for everything else

        def capped_run(argv, timeout):
            """Compress the cap for the planted hang; hand the rest back."""
            tool = os.path.basename(str(argv[0])) if argv else ""
            if tool not in SHIMMED:
                return real_run(argv, timeout)  # not a cache command (git, tar)
            applied = hang_cap if tool == "brew" else production_cap
            caps.append((tool, timeout, applied))
            return real_run(argv, applied)

        monkeypatch.setattr(wc, "_run", capped_run)
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox)
        result = wc.execute(plan, apply=True, kanban_db=lib)
        # Teeth for the injection, so this cannot quietly become a no-op: the
        # hang is the one command that ran under the compressed cap, every cache
        # command was ASKED for the patched constant (a default argument would
        # have frozen it at def time and proved nothing), and the rest of the
        # pass ran under the cap production actually uses.
        assert [c for c in caps if c[0] == "brew"] == \
            [("brew", hang_cap, hang_cap)], caps
        assert {asked for _, asked, _ in caps} == {hang_cap}, caps
        assert sum(1 for _, _, applied in caps
                   if applied == production_cap) >= 2, caps
        brew = [e for e in result["cache"] if e["name"] == wc.BREW_NAME][0]
        assert brew["status"] == "timed-out", brew
        assert any("homebrew" in e for e in result["errors"]), result["errors"]
        assert [e["name"] for e in result["cache"]
                if e["status"] == "timed-out"] == [wc.BREW_NAME], result["cache"]
        # The claim the case is NAMED for, stated as a fact about the plan rather
        # than as a race: nothing may be left unattempted. `uv`/`npm` are planned
        # BEFORE the hang, so `ran` alone cannot see an abort — an engine that
        # `break`s out of the loop on the first failing command kept this case
        # green (measured: mutation "stop the pass after an error status" ->
        # 1 passed), because everything it checked had already run. Whatever sits
        # after the hang in the plan must still have been reached.
        left = [e["name"] for e in result["cache"] if e["status"] == "planned"]
        assert left == [], f"the pass stopped on the hang, never attempted: {left}"
        ran = {e["name"] for e in result["cache"] if e["status"] == "ran"}
        assert {wc.UV_NAME, wc.NPM_NAME} <= ran, "the pass stopped on the hang"

    def test_a_failing_command_is_an_error_and_the_pass_continues(
            self, lib, cache_sandbox):
        cache_sandbox.plant("npm", rc=7)
        seed_caches(cache_sandbox)
        result = wc.execute(plan_for(lib, cache_sandbox), apply=True,
                            kanban_db=lib)
        npm = [e for e in result["cache"] if e["name"] == wc.NPM_NAME][0]
        assert npm["status"] == "failed", npm
        assert any("npm cache" in e for e in result["errors"])
        assert {wc.UV_NAME, wc.BREW_NAME} <= {e["name"] for e in result["cache"]
                                              if e["status"] == "ran"}

    def test_conda_is_probed_at_its_real_path_first(self, cache_sandbox):
        real = cache_sandbox.plant_at(os.path.join("miniconda3", "bin", "conda"))
        box = cache_sandbox
        plan = wc.plan_clean(kanban_db=None, days=99999, now=NOW)
        argv = argv_of(plan, wc.CONDA_NAME)
        assert argv[0] == str(real), "the real path is what the script uses"

    def test_no_test_can_reach_a_real_binary(self, cache_sandbox):
        import shutil
        for name in SHIMMED:
            found = shutil.which(name)
            assert found and str(cache_sandbox.bin) in found, \
                f"{name} resolved outside the sandbox: {found}"


# ── 5. the report-only section is never executed ─────────────────────────── #

class TestReportOnly:
    def test_report_only_entries_are_measured_and_listed(self, lib,
                                                         cache_sandbox):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox)
        rows = {r["name"]: r for r in plan["report_only"]}
        assert set(rows) == {"Xcode device support", "hermes profiles",
                             "LM Studio models", "Trash"}
        assert all(r["kind"] == wc.KIND_REPORT for r in rows.values())
        assert rows["LM Studio models"]["size_bytes"] >= 1024

    @pytest.mark.parametrize("argv,deep", (([], False), (["--deep"], False),
                                           (["--apply"], True),
                                           (["--apply", "--deep", "--confirm"],
                                            True)))
    def test_report_only_is_never_touched_under_any_tier(self, lib,
                                                        cache_sandbox, argv,
                                                        deep):
        made = seed_caches(cache_sandbox)
        keep = [made[".lmstudio"], made[".hermes/profiles"], made[".Trash"],
                made["Library/Developer/CoreDevice/DeviceFS"]]
        plan = plan_for(lib, cache_sandbox, deep=deep)
        result = wc.execute(plan, apply="--apply" in argv, kanban_db=lib)
        for path in keep:
            assert path.is_dir() and (path / "blob.bin").is_file(), path
        assert [e["path"] for e in result["cache"]
                if e["kind"] == wc.KIND_REPORT] == []
        assert all(e["status"] == "report-only" for e in result["report_only"])

    def test_the_plan_and_result_carry_report_only_untouched(self, lib,
                                                            cache_sandbox):
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox)
        result = wc.execute(plan, apply=True, kanban_db=lib)
        assert result["report_only"] == plan["report_only"]


# ── 6. nothing under ~/.hermes, ~/.hscc or ~/dev is a target ─────────────── #

class TestProtectedRoots:
    def test_no_planned_action_targets_a_protected_root(self, lib,
                                                       cache_sandbox):
        seed_caches(cache_sandbox)
        for deep in (False, True):
            for e in plan_for(lib, cache_sandbox, deep=deep)["cache_actions"]:
                target = e["path"]
                if not target:
                    # A command row has no target: its containment rule is the
                    # executable, asserted by test_every_shim_resolves_into_the.
                    continue
                resolved = os.path.realpath(target)
                for rel in PROTECTED:
                    root = os.path.realpath(str(cache_sandbox.home / rel))
                    assert resolved != root and root not in Path(resolved).parents, \
                        f"{e['name']} targets {rel}: {resolved}"

    def test_uitest_cache_under_a_symlinked_cache_is_refused(self, lib,
                                                            cache_sandbox):
        """The two-gate case: containment passes, protection still refuses.

        ``~/.cache`` pointing into ``~/.hermes`` makes the containment check
        true (both sides resolve to the same place), so only the protected-root
        gate can stop this removal.
        """
        real = cache_sandbox.home / ".hermes" / "cache"
        (real / "hscc-uitest-one").mkdir(parents=True)
        (real / "hscc-uitest-one" / "blob.bin").write_bytes(b"q" * 512)
        (cache_sandbox.home / ".cache").symlink_to(real)
        plan = plan_for(lib, cache_sandbox)
        row = [r for r in named(plan, wc.UITEST_NAME)][-1]
        assert "hscc-uitest-one" in row["path"]
        result = wc.execute(plan, apply=True, kanban_db=lib)
        entry = [e for e in result["cache"]
                 if e["name"] == wc.UITEST_NAME][-1]
        assert entry["status"] == "skipped" and entry["reason"] == "protected", \
            entry
        assert (real / "hscc-uitest-one").is_dir()
        assert any("protected" in e for e in result["errors"]), result["errors"]

    @pytest.mark.parametrize("name,path", [
        (wc.DERIVED_DATA_NAME, "home/.hermes/profiles"),
        (wc.DERIVED_DATA_NAME, "home/dev/repo"),
        (wc.SIMULATOR_CACHE_NAME, "home/.hscc/state"),
        (wc.PYTEST_TEMP_NAME, "home/.hermes/kanban/workspaces"),
        (wc.UITEST_NAME, "home/.cache/../.hermes/profiles"),
    ])
    def test_a_forged_plan_cannot_name_a_protected_directory(
            self, cache_sandbox, name, path):
        target = cache_sandbox.home.parent / path
        target.mkdir(parents=True, exist_ok=True)
        (target / "blob.bin").write_bytes(b"q" * 256)
        forged = {"cache_actions": [{"kind": wc.KIND_PATH, "name": name,
                                     "tier": wc.TIER_SAFE, "argv": [],
                                     "path": str(target), "root": "",
                                     "size_bytes": 256, "status": "planned",
                                     "reason": "", "note": ""}],
                  "report_only": []}
        result = wc.execute(forged, apply=True, kanban_db=lib_none())
        assert result["removed"] == 0
        assert (target / "blob.bin").is_file(), "a forged plan removed data"
        assert result["cache"][0]["status"] == "skipped"
        assert result["cache"][0]["reason"] in ("outside-root", "protected")

    def test_a_plan_cannot_invent_a_removable_name(self, cache_sandbox):
        (cache_sandbox.home / "Library" / "Developer" / "Xcode").mkdir(
            parents=True)
        loot = cache_sandbox.home / "Library" / "Developer" / "Xcode" / "MyWork"
        loot.mkdir()
        (loot / "blob.bin").write_bytes(b"q" * 256)
        forged = {"cache_actions": [
            {"kind": wc.KIND_PATH, "name": "Xcode MyWork", "tier": wc.TIER_DEEP,
             "argv": [], "path": str(loot),
             "root": str(loot.parent), "size_bytes": 256, "status": "planned",
             "reason": "", "note": ""}], "report_only": []}
        result = wc.execute(forged, apply=True, kanban_db=lib_none())
        assert loot.is_dir(), "an unknown name was honoured as a removal"
        assert result["cache"][0]["reason"] == "outside-root"

    def test_a_leaf_that_is_not_the_one_we_named_is_refused(self, cache_sandbox):
        xcode = cache_sandbox.home / "Library" / "Developer" / "Xcode"
        loot = xcode / "iOS Device Support"
        loot.mkdir(parents=True)
        (loot / "blob.bin").write_bytes(b"q" * 256)
        forged = {"cache_actions": [
            {"kind": wc.KIND_PATH, "name": wc.DERIVED_DATA_NAME,
             "tier": wc.TIER_DEEP, "argv": [], "path": str(loot),
             "root": str(xcode), "size_bytes": 256, "status": "planned",
             "reason": "", "note": ""}], "report_only": []}
        result = wc.execute(forged, apply=True, kanban_db=lib_none())
        assert loot.is_dir()
        assert result["cache"][0]["reason"] == "outside-root"

    def test_the_archive_path_is_the_only_hermes_path_written(self, lib,
                                                             cache_sandbox):
        """The workspace archive is a write INSIDE ~/.hermes; it is the only one,
        and no cache entry may be a removal inside it."""
        make_ws(lib, "hscc", "t_old")
        seed_caches(cache_sandbox)
        plan = plan_for(lib, cache_sandbox, deep=True)
        result = wc.execute(plan, apply=True, kanban_db=lib)
        assert result["removed"] == 1
        archives = list((lib.home / "kanban" / "workspace-archive"
                         ).rglob("*.tar.gz"))
        assert len(archives) == 1
        for e in result["cache"]:
            # A command row has no path; realpath("") would answer with the
            # cwd, which of course lives under ~/.hermes on this fleet.
            if e.get("path"):
                assert ".hermes" not in os.path.realpath(e["path"]), e

    def test_protected_roots_are_the_three_the_card_names(self, cache_sandbox):
        roots = [p.name for p in wc._protected_roots(cache_sandbox.home)]
        assert roots == [".hermes", ".hscc", "dev"]


def lib_none():
    """A board authority nothing can be archived through: cache-only applies."""
    class _None:
        @staticmethod
        def list_boards():
            return []

        @staticmethod
        def boards_root():
            return Path(os.path.expanduser("~")) / "kanban" / "boards"

        @staticmethod
        def workspaces_root(board=None):
            return Path(os.path.expanduser("~")) / "kanban" / "workspaces"

        @staticmethod
        def kanban_db_path(board=None):
            return Path(os.path.expanduser("~")) / "kanban.db"

        @staticmethod
        def list_tasks(conn, **kw):
            return []

    return _None()


# ── 7. the human view of the tiers ───────────────────────────────────────── #

class TestHumanView:
    def test_dry_run_prefixes_cache_lines_with_would(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        rc, out = run_clean(["--deep"], lib)
        assert rc == 0
        assert "would run" in out and "would remove" in out
        assert "safe set (runs with --apply)" in out
        assert "deep set (needs --apply --deep --confirm)" in out
        assert "report only — never touched by this verb" in out
        assert "-> caches:" in out

    def test_apply_never_claims_would(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        rc, out = run_clean(["--apply", "--deep", "--confirm"], lib)
        assert rc == 0, out
        assert not any(line.lstrip().startswith("would")
                       for line in out.splitlines()), out
        assert "safe set (applied)" in out
        assert "-> caches:" in out

    def test_workspace_summary_line_is_unchanged(self, lib, cache_sandbox):
        make_ws(lib, "hscc", "t_old")
        seed_caches(cache_sandbox)
        rc, out = run_clean([], lib)
        assert "-> 1 workspaces, 0 skipped" in out, out
        ws_line = [ln for ln in out.splitlines() if "hscc/t_old" in ln]
        assert ws_line and ws_line[0].strip().startswith("would archive"), out

    def test_missing_tools_are_rendered_as_skipped_with_their_reason(
            self, lib, cache_sandbox):
        cache_sandbox.drop("brew", "conda", "xcrun", "osascript")
        seed_caches(cache_sandbox)
        rc, out = run_clean(["--deep"], lib)
        assert "skipped" in out and "(not-installed)" in out

    def test_the_json_is_the_engine_dict_with_the_new_keys(self, lib,
                                                          cache_sandbox):
        seed_caches(cache_sandbox)
        rc, out = run_clean(["--json", "--deep"], lib)
        payload = json.loads(out)
        direct = wc.plan_clean(kanban_db=lib, now=None, deep=True)
        assert set(payload) == set(direct)
        assert payload["deep"] is True
        assert {"cache_actions", "report_only"} <= set(payload)
        assert all(e["kind"] in (wc.KIND_COMMAND, wc.KIND_PATH)
                   for e in payload["cache_actions"])
        assert "would" not in out

    def test_apply_json_is_the_result_dict(self, lib, cache_sandbox):
        seed_caches(cache_sandbox)
        rc, out = run_clean(["--apply", "--deep", "--confirm", "--json"], lib)
        payload = json.loads(out)
        assert set(payload) == {"apply", "archived", "removed", "skipped",
                                "errors", "freed_bytes", "cache",
                                "cache_freed_bytes", "report_only"}
        assert payload["apply"] is True
