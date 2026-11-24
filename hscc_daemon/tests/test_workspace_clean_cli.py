"""The `hscc clean` CLI verb (card t_fc9296a7) — dispatch, help surface, output.

The ENGINE (`hscc_daemon/workspace_clean.py`, t_274530ae) owns what is safe to
archive; this file never re-tests that. It pins the four things only the verb
owns:

  1. the three help surfaces agree (dispatch branch, TOP_LEVEL_COMMANDS,
     COMMAND_HELP) and the verb is named on the primary `hscc --help` surface —
     the same structural contract test_unknown_command_hint.py enforces for
     every verb, restated here so a regression names `clean`;
  2. invariant D for THIS verb: `--help` / `-h` / `hscc help clean` exit 0,
     print the COMMAND_HELP text, and cannot reach `plan_clean` (pinned by
     making plan_clean raise, and structurally by the engine import sitting
     INSIDE the handler);
  3. an unknown flag — `--deep` used to be the pinned example (card
     t_fc9296a7 refused it for card t_701e22fc to claim); it is legal now, so
     the refused-flag cases here are typos instead —
     exits 2 with the usage line instead of being silently accepted;
  4. the two output paths: the human view says DRY RUN and prefixes every
     mutating line with `would`; `--json` is the engine's dict verbatim
     (repo hard invariant), byte-stable, parseable, no ANSI.

Safety: nothing here touches the real `~/.hermes`. Every plan/apply runs
against the engine's own ``FakeLib`` (a temp home) injected through
``kanban_db``; the only ``main()`` calls are help and usage-error paths, which
by construction never reach the engine. Fixtures stamp cards relative to the
REAL clock (a future-dated ``completed_at`` is not a candidate at all).
"""

import ast
import inspect
import io
import json
import re
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import workspace_clean as wc
# t_701e22fc: same reason as in test_workspace_clean.py — the engine's apply
# path now runs the cache tiers, so every test in this file gets the sandbox.
from hscc_daemon.tests.test_workspace_clean_deep import cache_sandbox  # noqa: F401,E402

REPO = Path(__file__).resolve().parents[2]
BUDGET = 78            # the (B) gate's render budget
AUTHORED = BUDGET - 2  # _handle_help prints one 2-space prefix for the value
DAY = 86400
# 40 days, not 30: `--days 30` in the cutoff test must clear the fixture with
# room, not by the millisecond between the stamp and the plan's own clock.
PAST = int(time.time()) - 40 * DAY      # comfortably past the 7-day default
YOUNG = int(time.time()) - 2 * DAY      # inside it


# ── fixtures / helpers ─────────────────────────────────────────────────────

@pytest.fixture
def lib(tmp_path):
    """The engine's own fake board authority, over a throwaway home."""
    from hscc_daemon.tests.test_workspace_clean import FakeLib
    return FakeLib(tmp_path / "home")


def make_ws(lib, slug, task_id, *, bytes_written=2048, completed_at=PAST,
            status="done"):
    """One finished card + a plain (git-free, therefore clean) workspace."""
    lib.add_task(slug, task_id, status=status, completed_at=completed_at,
                 created_at=completed_at)
    ws = lib.workspace(slug, task_id)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "report.md").write_bytes(b"x" * bytes_written)
    return ws


def two_board_lib(lib):
    """Boards `hscc` (2 old, 1 young, 1 skipped) and `other` (1 old)."""
    lib.add_board("hscc")
    lib.add_board("other")
    make_ws(lib, "hscc", "t_old1")
    make_ws(lib, "hscc", "t_old2", bytes_written=1024)
    make_ws(lib, "hscc", "t_young", completed_at=YOUNG)
    # A live claim: kept, and named — the skip path without any git in the way.
    lib.add_task("hscc", "t_claim", completed_at=PAST, created_at=PAST,
                 claim_lock="host:1", claim_expires=int(time.time()) + 600)
    claim = lib.workspace("hscc", "t_claim")
    claim.mkdir(parents=True, exist_ok=True)
    (claim / "notes.md").write_bytes(b"y" * 512)
    make_ws(lib, "other", "t_other")
    return lib


def run_clean(argv, lib):
    """Drive the verb with an injected fake lib; returns (rc, stdout)."""
    out = io.StringIO()
    with redirect_stdout(out):
        rc = unified._handle_clean(argv, kanban_db=lib)
    return rc, out.getvalue()


def _main(monkeypatch, argv):
    """Drive unified.main() with argv; returns (rc, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["hscc", *argv])
    out, err = io.StringIO(), io.StringIO()
    rc = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            unified.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue(), err.getvalue()


# ── 1. the three surfaces agree (and the primary one names the verb) ───────

class TestSurfacesAgree:
    def test_clean_is_listed_helped_and_dispatched(self):
        assert "clean" in unified.TOP_LEVEL_COMMANDS
        assert "clean" in unified.COMMAND_HELP
        src = inspect.getsource(unified.main)
        assert re.search(r'\bif cmd == "clean":', src), \
            "no `if cmd == \"clean\":` dispatch branch in main()"

    def test_primary_help_names_the_verb(self, monkeypatch):
        rc, out, _ = _main(monkeypatch, ["--help"])
        assert rc == 0
        assert re.search(r"^\s{2}clean(?:\s|$)", out, re.MULTILINE), \
            "`hscc --help` does not name clean at the usage column"

    def test_bogus_verb_hint_names_clean(self, monkeypatch):
        rc, out, _ = _main(monkeypatch, ["bogus-verb"])
        assert rc == 1
        line = [ln for ln in out.splitlines() if ln.startswith("Available:")][0]
        assert "clean" in [t.strip() for t in line.split(":", 1)[1].split(",")]

    def test_authored_help_lines_fit_the_budget(self):
        for ln in unified.COMMAND_HELP["clean"].splitlines():
            assert len(ln) <= AUTHORED, f"{len(ln)} cols: {ln!r}"

    def test_rendered_help_surfaces_fit_the_budget(self, monkeypatch):
        for argv in (["clean", "--help"], ["clean", "-h"], ["help", "clean"]):
            rc, out, _ = _main(monkeypatch, argv)
            assert rc == 0, argv
            over = [ln for ln in out.splitlines() if len(ln) > BUDGET]
            assert not over, f"{argv}: over budget {over}"

    def test_no_internal_package_name_in_rendered_help(self, monkeypatch):
        rc, out, _ = _main(monkeypatch, ["clean", "--help"])
        assert rc == 0
        assert "flightdeck" not in out.lower()


# ── 2. invariant D: help executes nothing ──────────────────────────────────

class TestHelpIsInert:
    @pytest.mark.parametrize("argv", (["clean", "--help"], ["clean", "-h"],
                                      ["clean", "--json", "--help"]))
    def test_help_never_reaches_the_engine(self, monkeypatch, argv):
        def boom(*a, **k):
            raise AssertionError(f"plan_clean ran for {argv}")
        monkeypatch.setattr(wc, "plan_clean", boom)
        rc, out, _ = _main(monkeypatch, argv)
        assert rc == 0, out

    def test_help_text_is_the_command_help_source(self, monkeypatch):
        """--help, -h and `hscc help clean` print the SAME bytes — one source,
        so the three cannot drift (and -h parity is an audit (D-h) gate)."""
        renders = set()
        for argv in (["clean", "--help"], ["clean", "-h"], ["help", "clean"]):
            rc, out, _ = _main(monkeypatch, argv)
            assert rc == 0, argv
            renders.add(out)
        assert len(renders) == 1, renders
        assert unified.COMMAND_HELP["clean"] in next(iter(renders))

    def test_handler_guards_direct_callers_too(self, monkeypatch):
        """The intercept in main() is the first line; the handler's own guard
        is the second — a direct caller must not reach plan_clean either."""
        monkeypatch.setattr(wc, "plan_clean",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("plan_clean ran")))
        rc, out = run_clean(["--help"], None)
        assert rc == 0
        assert unified.COMMAND_HELP["clean"] in out

    def test_engine_import_is_not_at_module_scope(self):
        """Structural invariant-D pin: the engine is imported INSIDE the
        handler, never when `hscc.py` is imported."""
        tree = ast.parse(inspect.getsource(unified))

        def imported_names(node):
            """Every dotted name an import statement can introduce. Both the
            module (`from hscc_daemon import x` -> hscc_daemon) and the bound
            name (`x`) count: `import a.b` and `from a import b` are different
            ways of pulling the same module in."""
            if isinstance(node, ast.Import):
                return {a.name for a in node.names}
            if isinstance(node, ast.ImportFrom):
                return {node.module or ""} | {a.name for a in node.names}
            return set()

        at_module_scope = set()
        for node in tree.body:
            at_module_scope |= imported_names(node)
        assert not any("workspace_clean" in n for n in at_module_scope), \
            "workspace_clean must not be imported at module scope"
        fn = [n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "_handle_clean"][0]
        inside = set()
        for node in ast.walk(fn):
            inside |= imported_names(node)
        assert any("workspace_clean" in n for n in inside), \
            "_handle_clean must import the engine lazily"


# ── 3. argv contract ───────────────────────────────────────────────────────

class TestArgvContract:
    @pytest.mark.parametrize("argv,needle", [
        # `--deep` was refused here until t_701e22fc: it is a legal flag now, and
        # `--apply --deep` without `--confirm` is still a usage error — with a
        # different reason, which is the whole point of the gate.
        (["--apply", "--deep"], "--deep with --apply needs --confirm"),
        (["--day", "7"], "unknown flag: --day"),
        (["hscc"], "unexpected argument: hscc"),
        (["--days"], "--days needs a value"),
        (["--days", "abc"], "--days needs a whole number"),
        # `-1` reads as another flag to the hand-rolled parser (the shape
        # _parse_node_args keeps), so it is refused as a missing value; the
        # `=` form reaches the numeric guard.
        (["--days", "-1"], "--days needs a value"),
        (["--days=-1"], "--days cannot be negative"),
        (["--board"], "--board needs a value"),
    ])
    def test_bad_usage_exits_2_with_the_usage_line(self, monkeypatch, argv,
                                                   needle):
        monkeypatch.setattr(wc, "plan_clean",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("plan_clean ran on bad usage")))
        rc, out, err = _main(monkeypatch, ["clean", *argv])
        assert rc == 2, (argv, out, err)
        assert "usage: hscc clean" in err
        assert needle in err, (argv, err)

    def test_days_is_the_engine_default(self):
        opts, err = unified._parse_clean_args([])
        assert err is None
        assert opts["--days"] is None      # the handler applies DEFAULT_DAYS
        assert unified._parse_clean_args(["--days", "0"])[1] is None
        assert unified._parse_clean_args(["--days=14"])[0]["--days"] == 14

    def test_json_and_apply_are_switches(self):
        opts, err = unified._parse_clean_args(["--apply", "--json"])
        assert err is None and opts["--apply"] and opts["--json"]


# ── 4a. the dry run (human view) ───────────────────────────────────────────

class TestDryRunHumanView:
    def test_header_says_dry_run_and_nothing_moves(self, lib, tmp_path):
        two_board_lib(lib)
        before = sorted(p.relative_to(lib.home).as_posix()
                        for p in lib.home.rglob("*"))
        rc, out = run_clean([], lib)
        assert rc == 0, out
        assert "MODE: DRY RUN (add --apply to clean)" in out
        assert "kanban workspaces of finished cards older than 7d" in out
        assert sorted(p.relative_to(lib.home).as_posix()
                      for p in lib.home.rglob("*")) == before
        assert not lib.connects, "the plan must use the read-only door"

    def test_every_mutating_line_is_prefixed_would(self, lib):
        two_board_lib(lib)
        rc, out = run_clean([], lib)
        per_card = [ln for ln in out.splitlines()
                    if re.search(r"(?:^|\s)(?:hscc|other)/t_", ln)]
        assert len(per_card) == 4, out        # 3 actions + 1 skip
        for ln in per_card:
            assert re.match(r"^   (would archive|kept)\s", ln), ln
        # The three mutating lines are the `would` ones; the kept line never
        # claims to be one.
        assert sum(1 for ln in per_card if ln.startswith("   would")) == 3
        assert sum(1 for ln in per_card if ln.startswith("   kept")) == 1

    def test_per_card_line_shape_and_summary(self, lib):
        two_board_lib(lib)
        rc, out = run_clean([], lib)
        for card in ("hscc/t_old1", "hscc/t_old2", "other/t_other"):
            assert re.search(rf"would archive\s+{re.escape(card)}\s+\d+(\.\d+)? "
                             r"(B|KiB|MiB|GiB)\b", out), f"{card}: {out}"
        assert "-> 3 workspaces, 1 skipped" in out
        assert "t_young" not in out, "a card inside --days is not a candidate"

    def test_a_skip_names_its_reason_and_keeps_the_workspace(self, lib):
        two_board_lib(lib)
        rc, out = run_clean([], lib)
        kept = [ln for ln in out.splitlines() if "kept" in ln]
        assert len(kept) == 1, out
        assert "hscc/t_claim" in kept[0]
        reason = kept[0].split("(")[-1].rstrip(")")
        assert reason in wc.SKIP_REASONS, f"invented reason {reason!r}"
        assert lib.workspace("hscc", "t_claim").is_dir()

    def test_board_narrows_the_pass(self, lib):
        two_board_lib(lib)
        rc, out = run_clean(["--board", "other"], lib)
        assert "-> 1 workspaces, 0 skipped" in out
        assert "other/t_other" in out
        assert "hscc/t_old1" not in out

    def test_days_moves_the_cutoff_both_ways(self, lib):
        """`--days N` is a MINIMUM age: a bigger N is stricter, not looser.

        Pinned in both directions because reading N as "clean anything from the
        last N days" inverts the verb — that reading would archive a card that
        finished this morning.
        """
        two_board_lib(lib)
        rc, out = run_clean(["--days", "1"], lib)
        assert "t_young" in out, out
        assert "-> 4 workspaces, 1 skipped" in out
        rc, out = run_clean(["--days", "30"], lib)
        assert "t_young" not in out, out
        assert "-> 3 workspaces, 1 skipped" in out
        rc, out = run_clean(["--days", "60"], lib)
        assert "-> 0 workspaces, 0 skipped" in out, out

    def test_an_error_is_reported_and_exits_1(self, lib, monkeypatch):
        from hscc_daemon import autodown
        two_board_lib(lib)
        monkeypatch.setattr(autodown, "_KANBAN_LOAD", {"reason": "no hermes here"})
        monkeypatch.setattr(autodown, "_load_kanban_db_or_default", lambda: None)
        rc, out = run_clean([], None)
        assert rc == 1
        assert "no hermes here" in out


# ── 4b. --json is the engine's dict, verbatim ──────────────────────────────

class TestJsonIsTheEngineDict:
    def test_dry_run_json_matches_the_plan_key_set_exactly(self, lib):
        two_board_lib(lib)
        rc, out = run_clean(["--json"], lib)
        assert rc == 0
        payload = json.loads(out)                       # parses
        direct = wc.plan_clean(kanban_db=lib)           # the engine's own dict
        assert set(payload) == set(direct)
        assert payload == json.loads(json.dumps(direct))
        # t_701e22fc extends this contract DELIBERATELY, in that card's commit:
        # the workspace keys keep their exact meaning, and the cache tiers are
        # two additive top-level keys. A third card that grows the dict without
        # naming it here is changing card t_fc9296a7's --json contract.
        assert {"days", "cutoff", "actions", "skipped", "errors"} <= set(payload)
        assert {"cache_actions", "report_only"} <= set(payload)
        assert all(a["kind"] == wc.KIND for a in payload["actions"]), \
            "a cache row must never join the workspace actions"
        for entry in payload["actions"] + payload["skipped"]:
            assert set(entry) == set(direct["actions"][0]) | set() or \
                set(entry) <= set(direct["actions"][0]) | {"reason"}
        assert all(e["kind"] == wc.KIND for e in payload["actions"])

    def test_json_is_byte_stable_and_free_of_ansi(self, lib):
        two_board_lib(lib)
        first = run_clean(["--json"], lib)[1]
        second = run_clean(["--json"], lib)[1]
        assert first == second, "--json is not byte-stable"
        assert "\x1b[" not in first
        json.loads(first)["actions"]

    def test_apply_json_is_the_result_dict(self, lib):
        two_board_lib(lib)
        rc, out = run_clean(["--apply", "--json"], lib)
        assert rc == 0
        payload = json.loads(out)
        assert {"apply", "archived", "removed", "skipped", "errors",
                "freed_bytes"} <= set(payload)
        # t_701e22fc adds the cache half to the result dict too.
        assert {"cache", "cache_freed_bytes", "report_only"} <= set(payload)
        assert payload["apply"] is True
        assert payload["removed"] == 3
        assert payload["freed_bytes"] > 0
        assert all(set(a) == {"path", "archive", "size_bytes"}
                   for a in payload["archived"])

    def test_json_never_carries_added_text(self, lib):
        two_board_lib(lib)
        rc, out = run_clean(["--json"], lib)
        assert out.strip().startswith("{") and out.strip().endswith("}")
        assert "MODE:" not in out and "would" not in out


# ── 4c. --apply (human view), against the temp home only ───────────────────

class TestApplyHumanView:
    def test_apply_reports_mode_and_freed_total(self, lib):
        two_board_lib(lib)
        rc, out = run_clean(["--apply"], lib)
        assert rc == 0, out
        assert "MODE: APPLY" in out
        assert "MODE: DRY RUN" not in out
        assert "freed" in out
        assert "-> 3 workspaces, 1 skipped" in out
        assert not any(re.match(r"^\s+would\b", ln) for ln in out.splitlines()), \
            "an apply must not claim it would do something"

    def test_apply_removes_only_the_safe_set(self, lib):
        two_board_lib(lib)
        claim = lib.workspace("hscc", "t_claim")
        run_clean(["--apply"], lib)
        assert not lib.workspace("hscc", "t_old1").exists()
        assert not lib.workspace("other", "t_other").exists()
        assert lib.workspace("hscc", "t_young").is_dir(), "young card removed"
        assert claim.is_dir(), "a live claim was removed"
        # The archive landed under the injected home, not the operator's.
        archives = list((lib.home / "kanban" / "workspace-archive").rglob("*.tar.gz"))
        assert len(archives) == 3, archives

    def test_the_board_database_survives(self, lib):
        two_board_lib(lib)
        db = lib.db_path("hscc")
        before = db.read_bytes()
        run_clean(["--apply"], lib)
        assert db.read_bytes() == before
