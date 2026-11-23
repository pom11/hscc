"""Tests for hscc_daemon.merged_pr_resolve (card t_57d8ca28).

The defect: Hermes' respawn guard holds a ready card on any recent GitHub PR URL
in its comments (``kanban_db_dispatch.py:1207-1213``) and re-stamps
``respawn_guarded`` every tick it holds (``kanban_db_dispatch.py:1536-1537``)
without ever asking whether that PR merged. A card whose PR was merged
elsewhere therefore sits ``ready`` forever — measured instance ``t_edc71fcb``
with PR #625 MERGED, 286 dispatcher ticks, completed by hand by fleet-check.

Every test injects a fake kanban lib (in-memory sqlite, the
``test_kanban_blocked.py::_FakeKb`` shape), a fake ``gh`` and a fake
``hermes kanban`` CLI runner, so NOTHING here touches GitHub, a real board or a
real gateway. The suite pins the two halves of the contract: a merged-PR hold
RESOLVES in one pass (never re-guarded), and anything less than a verified
merge touching this card does not touch the board at all.
"""

import ast
import contextlib
import inspect
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from hscc_daemon import dispatcher_wedge
from hscc_daemon import merged_pr_resolve as mpr


NOW = 1_800_000_000  # fixed clock; every event timestamp is derived from it
PR_URL = "https://github.com/pom11/hscc/pull/625"
MERGE_SHA = "134ec3d5c22f36583def63e4389652e8c996d49e"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeKb:
    """In-memory sqlite boards with the three tables this pass reads."""

    def __init__(self, boards):
        self._conns = {}
        self.opened = []
        for slug, specs in boards.items():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT,"
                         " assignee TEXT, status TEXT, created_at INTEGER,"
                         " claim_lock TEXT)")
            conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY"
                         " AUTOINCREMENT, task_id TEXT, kind TEXT, payload"
                         " TEXT, created_at INTEGER)")
            conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY"
                         " AUTOINCREMENT, task_id TEXT, author TEXT, body"
                         " TEXT, created_at INTEGER)")
            for i, spec in enumerate(specs):
                tid = spec["id"]
                conn.execute(
                    "INSERT INTO tasks (id, title, assignee, status,"
                    " created_at, claim_lock) VALUES (?,?,?,?,?,?)",
                    (tid, spec.get("title", "card " + tid),
                     spec.get("assignee", "backend-engineer"),
                     spec.get("status", "ready"),
                     spec.get("created_at", NOW - 3600),
                     spec.get("claim_lock")))
                for reason, at in spec.get("holds", []):
                    conn.execute(
                        "INSERT INTO task_events (task_id, kind, payload,"
                        " created_at) VALUES (?,?,?,?)",
                        (tid, "respawn_guarded",
                         json.dumps({"reason": reason}), at))
                for body, at in spec.get("comments", []):
                    conn.execute(
                        "INSERT INTO task_comments (task_id, author, body,"
                        " created_at) VALUES (?,?,?,?)",
                        (tid, "worker", body, at))
            conn.commit()
            self._conns[slug] = conn

    def list_boards(self):
        return [{"slug": slug} for slug in self._conns]

    @contextlib.contextmanager
    def connect_closing(self, board=None):
        self.opened.append(board)
        yield self._conns.get(board) or self._conns.get("default")

    def events(self, board, task_id):
        conn = self._conns[board]
        return [dict(r) for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ?",
            (task_id,)).fetchall()]


def held_card(cid="t_edc71fcb", **over):
    """A ready card the guard is holding RIGHT NOW, citing one merged PR."""
    spec = {"id": cid, "status": "ready",
            "holds": [("active_pr", NOW - 60)],
            "comments": [(f"Shipped via {PR_URL}", NOW - 3600)]}
    spec.update(over)
    return spec


def gh_view(state="MERGED", card_id="t_edc71fcb", sha=MERGE_SHA,
            head="wt/t_edc71fcb", number=625):
    body = f"Card {card_id} (lane 4). Carried by {card_id}." if card_id else ""
    commit = {"oid": sha} if sha else None
    return {"number": number, "url": PR_URL, "state": state,
            "mergedAt": "2026-11-22T19:07:50Z" if state == "MERGED" else None,
            "mergeCommit": commit, "headRefName": head,
            "baseRefName": "main",
            "title": f"test(hscc-bootstrap): a seam, not a gate ({card_id})",
            "body": body}


class FakeGh:
    """argv -> canned `gh pr view` results; records every argv it was asked."""

    def __init__(self, views=None, fail=False, raise_it=False):
        self.views = views or {}
        self.calls = []
        self.fail = fail
        self.raise_it = raise_it

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.raise_it:
            raise RuntimeError("gh: could not reach api.github.com")
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="gh: error")
        number = argv[3]
        repo = argv[argv.index("--repo") + 1]
        view = self.views.get(f"{repo}/{number}")
        if view is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="no PR")
        return SimpleNamespace(returncode=0, stdout=json.dumps(view),
                               stderr="")


class FakeCli:
    """The `hermes kanban` runner. Records argv + env, returns a canned result."""

    def __init__(self, returncode=0, stdout="", stderr="", env_capable=True):
        self.calls = []
        self.envs = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        if not env_capable:
            # State the capability the way the wedge's contract expects.
            self.cli_env_capable = False

    def __call__(self, argv, env=None):
        self.calls.append(list(argv))
        self.envs.append(env)
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout,
                               stderr=self.stderr)

    def verbs(self):
        """The (verb, task_id) pair of each call, ignoring flags."""
        out = []
        for argv in self.calls:
            rest = argv[argv.index("kanban") + 1:]
            verb = next((a for a in rest if a in ("complete", "block", "list",
                                                  "show")), None)
            tid = next((a for a in rest if a.startswith("t_")), None)
            out.append((verb, tid))
        return out


@pytest.fixture(autouse=True)
def _seams(monkeypatch):
    """A hermes CLI always exists; per-card bookkeeping starts empty."""
    monkeypatch.setattr(dispatcher_wedge, "_hermes_cli_argv",
                        lambda: ["/bin/hermes", "kanban"])
    mpr._reset_resolution_state()
    monkeypatch.delenv("HSCC_MERGED_PR_RESOLVE", raising=False)
    monkeypatch.delenv("HSCC_MERGED_PR_RESOLVE_DRY_RUN", raising=False)


def resolve(kb, gh, cli, **over):
    over.setdefault("notify_fn", lambda m: None)
    return mpr.resolve_merged_pr_cards(kanban_db=kb, now=NOW, gh_runner=gh,
                                       cli_runner=cli, **over)


# ---------------------------------------------------------------------------
# The acceptance case: merged PR + unresolved card resolves in ONE pass
# ---------------------------------------------------------------------------

class TestResolveOnMerge:
    def test_merged_covering_pr_completes_card_in_one_pass(self):
        kb = FakeKb({"hscc": [held_card()]})
        gh, cli = FakeGh({"pom11/hscc/625": gh_view()}), FakeCli()
        report = resolve(kb, gh, cli)
        assert len(report["resolved"]) == 1
        assert report["resolved"][0]["card"] == "t_edc71fcb"
        assert report["resolved"][0]["sha"] == MERGE_SHA
        assert report["blocked"] == [] and report["held"] == []
        assert cli.verbs() == [("complete", "t_edc71fcb")]

    def test_completion_carries_covering_sha_and_pr_in_metadata(self):
        kb = FakeKb({"hscc": [held_card()]})
        cli = FakeCli()
        resolve(kb, FakeGh({"pom11/hscc/625": gh_view()}), cli)
        argv = cli.calls[0]
        meta = json.loads(argv[argv.index("--metadata") + 1])
        assert meta["merge_sha"] == MERGE_SHA
        assert meta["merged_pr"] == 625
        assert meta["merged_pr_url"] == PR_URL
        assert meta["resolved_by"] == "hscc-merged-pr"
        # The covering evidence is recorded, not just asserted internally.
        assert meta["card_id_match"] in ("headRefName", "title", "body")
        result = argv[argv.index("--result") + 1]
        assert MERGE_SHA[:12] in result and "625" in result

    def test_board_slug_is_always_stated_even_for_default(self):
        kb = FakeKb({"default": [held_card()]})
        cli = FakeCli()
        resolve(kb, FakeGh({"pom11/hscc/625": gh_view()}), cli)
        argv = cli.calls[0]
        assert argv[:3] == ["/bin/hermes", "kanban", "--board"]
        assert argv[3] == "default"
        assert argv[4] == "complete"

    def test_second_pass_has_nothing_left_to_guard(self):
        """After the resolve the card is out of `ready`, so the guard has no
        candidate: the loop cannot respawn past a merge."""
        kb = FakeKb({"hscc": [held_card()]})
        gh, cli = FakeGh({"pom11/hscc/625": gh_view()}), FakeCli()
        resolve(kb, gh, cli)
        # Simulate what the sanctioned `complete` did to the board.
        kb._conns["hscc"].execute(
            "UPDATE tasks SET status = 'done' WHERE id = 't_edc71fcb'")
        kb._conns["hscc"].commit()
        report = resolve(kb, gh, cli)
        assert report["resolved"] == [] and report["held"] == []
        assert gh.calls == gh.calls  # second pass asked GitHub about nothing new
        assert len(gh.calls) == 1

    def test_hold_window_expiry_means_the_guard_no_longer_holds(self):
        """An event older than Hermes' own PR-guard window means the dispatcher
        stopped holding this card: it is nobody's business here."""
        kb = FakeKb({"hscc": [held_card(
            holds=[("active_pr", NOW - 3 * 86400)])]})
        gh = FakeGh({"pom11/hscc/625": gh_view()})
        cli = FakeCli()
        report = resolve(kb, gh, cli)
        assert report["resolved"] == [] and report["held"] == []
        assert gh.calls == [] and cli.calls == []


# ---------------------------------------------------------------------------
# Merged but not proven to be THIS card's work: block with a named PR
# ---------------------------------------------------------------------------

class TestBlockWhenUnproven:
    def test_merged_pr_naming_only_a_sibling_card_blocks_needs_input(self):
        """The documented shape: #625 was branched wt/t_5b560adf while the
        held card was t_edc71fcb. A merge that never names the card is not
        proof the card shipped, so a human decides — by name, not by respawn."""
        kb = FakeKb({"hscc": [held_card()]})
        gh = FakeGh({"pom11/hscc/625": gh_view(card_id="t_5b560adf",
                                               head="wt/t_5b560adf")})
        cli = FakeCli()
        report = resolve(kb, gh, cli)
        assert report["resolved"] == []
        assert [b["card"] for b in report["blocked"]] == ["t_edc71fcb"]
        assert cli.verbs() == [("block", "t_edc71fcb")]
        argv = cli.calls[0]
        assert argv[argv.index("--kind") + 1] == "needs_input"
        reason = argv[argv.index("block") + 2]
        assert PR_URL in reason and "active_pr" in reason

    def test_merged_without_a_merge_commit_sha_cannot_be_completed(self):
        kb = FakeKb({"hscc": [held_card()]})
        gh = FakeGh({"pom11/hscc/625": gh_view(sha=None)})
        cli = FakeCli()
        report = resolve(kb, gh, cli)
        assert report["resolved"] == [] and report["blocked"]
        assert cli.verbs() == [("block", "t_edc71fcb")]

    def test_partially_merged_citations_block_rather_than_complete(self):
        """A card citing its own merged PR plus a still-open one must not be
        completed — that would silently drop the open half."""
        kb = FakeKb({"hscc": [held_card(
            comments=[(f"Shipped via {PR_URL}", NOW - 3600),
                      ("follow-up in https://github.com/pom11/hscc/pull/711",
                       NOW - 3600)])]})
        gh = FakeGh({"pom11/hscc/625": gh_view(),
                     "pom11/hscc/711": gh_view(state="OPEN", number=711)})
        cli = FakeCli()
        report = resolve(kb, gh, cli)
        assert report["resolved"] == [] and report["blocked"]
        assert cli.verbs() == [("block", "t_edc71fcb")]

    def test_body_only_card_id_match_counts_as_covering_evidence(self):
        """A squash-merge commit message is title+body, so a body hit means the
        merge commit itself names the card (exactly t_edc71fcb / #625)."""
        kb = FakeKb({"hscc": [held_card()]})
        gh = FakeGh({"pom11/hscc/625": gh_view(
            card_id="t_5b560adf", head="wt/t_5b560adf")})
        gh.views["pom11/hscc/625"]["body"] += " Carried by t_edc71fcb."
        cli = FakeCli()
        report = resolve(kb, gh, cli)
        assert [c["card"] for c in report["resolved"]] == ["t_edc71fcb"]
        assert cli.verbs() == [("complete", "t_edc71fcb")]


# ---------------------------------------------------------------------------
# Fail closed: no verified merge, no board mutation
# ---------------------------------------------------------------------------

class TestFailClosed:
    def test_open_pr_is_left_held_and_untouched(self):
        kb = FakeKb({"hscc": [held_card()]})
        gh = FakeGh({"pom11/hscc/625": gh_view(state="OPEN")})
        cli = FakeCli()
        report = resolve(kb, gh, cli)
        assert report["resolved"] == [] and report["blocked"] == []
        assert [h["reason"] for h in report["held"]] == ["pr_open"]
        assert cli.calls == []

    def test_unreadable_pr_state_makes_no_mutation(self):
        kb = FakeKb({"hscc": [held_card()]})
        for gh in (FakeGh(fail=True), FakeGh(raise_it=True), FakeGh({})):
            cli = FakeCli()
            report = resolve(kb, gh, cli)
            assert report["resolved"] == [] and report["blocked"] == []
            assert [h["reason"] for h in report["held"]] == ["unverified"]
            assert cli.calls == []

    def test_no_active_pr_hold_event_is_never_a_candidate(self):
        """A ready card that merely mentions a merged PR in prose is not held by
        the guard and must never be auto-completed."""
        kb = FakeKb({"hscc": [held_card(holds=[])]})
        gh = FakeGh({"pom11/hscc/625": gh_view()})
        cli = FakeCli()
        assert resolve(kb, gh, cli)["resolved"] == []
        assert gh.calls == [] and cli.calls == []

    def test_other_guard_reasons_are_not_this_passes_business(self):
        kb = FakeKb({"hscc": [held_card(holds=[("recent_success", NOW - 60)]),
                              held_card(id="t_ffffffff",
                                        holds=[("blocker_auth", NOW - 60)])]})
        gh = FakeGh({"pom11/hscc/625": gh_view()})
        assert resolve(kb, gh, FakeCli())["resolved"] == []

    def test_claimed_or_non_ready_rows_are_never_candidates(self):
        kb = FakeKb({"hscc": [
            held_card(id="t_running1", status="running"),
            held_card(id="t_claimed1", claim_lock="lock:123"),
            held_card(id="t_blocked1", status="blocked"),
            held_card(id="t_done____1", status="done"),
        ]})
        gh = FakeGh({"pom11/hscc/625": gh_view()})
        cli = FakeCli()
        assert resolve(kb, gh, cli)["resolved"] == []
        assert cli.calls == []

    def test_pin_conflict_verdict_prevents_the_action(self):
        """board_pin says the board is unverifiable under a conflicting pin →
        refuse the mutation (the same fail-closed verdict the wedge gate uses)."""
        kb = FakeKb({"hscc": [held_card()]})
        cli = FakeCli()
        original = dispatcher_wedge._gate_child_env
        dispatcher_wedge._gate_child_env = (
            lambda kanban_db, board, cli_fn: (None, "board state unverifiable"))
        try:
            report = resolve(kb, FakeGh({"pom11/hscc/625": gh_view()}), cli)
        finally:
            dispatcher_wedge._gate_child_env = original
        assert report["resolved"] == [] and report["blocked"] == []
        assert any("unverifiable" in e for e in report["errors"])
        assert cli.calls == []

    def test_failed_action_retries_then_stops_and_notifies_once(self):
        kb = FakeKb({"hscc": [held_card()]})
        gh, pings = FakeGh({"pom11/hscc/625": gh_view()}), []
        cli = FakeCli(returncode=1, stderr="hermes: task has a live claim")
        for _ in range(mpr.MAX_ATTEMPTS_PER_CARD):
            resolve(kb, gh, cli, notify_fn=pings.append)
        assert len(cli.calls) == mpr.MAX_ATTEMPTS_PER_CARD
        report = resolve(kb, gh, cli)
        assert [s["reason"] for s in report["skipped"]] == ["attempt_cap"]
        assert len(cli.calls) == mpr.MAX_ATTEMPTS_PER_CARD
        # One ping for the incident, not one per retry.
        assert len(pings) == 1


# ---------------------------------------------------------------------------
# Knobs, the tick boundary, and the wedge invariant
# ---------------------------------------------------------------------------

class TestKnobsAndInvariants:
    def test_dry_run_plans_everything_and_touches_no_board(self):
        kb = FakeKb({"hscc": [held_card()]})
        cli = FakeCli()
        report = resolve(kb, FakeGh({"pom11/hscc/625": gh_view()}), cli,
                         dry_run_flag=True)
        assert [c["card"] for c in report["resolved"]] == ["t_edc71fcb"]
        assert report["resolved"][0]["dry_run"] is True
        assert cli.calls == []

    def test_disabled_pass_makes_no_calls_at_all(self, monkeypatch):
        monkeypatch.setenv("HSCC_MERGED_PR_RESOLVE", "off")
        report = mpr.tick(now=NOW)
        assert report["skipped"] == [{"reason": "disabled"}]
        assert mpr.enabled() is False

    def test_tick_never_raises_when_a_seam_explodes(self, monkeypatch):
        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise RuntimeError("board db is on a NAS that went away")

        monkeypatch.setattr(mpr, "resolve_merged_pr_cards", boom)
        report = mpr.tick(now=NOW)
        assert report["errors"] and "pass failed" in report["errors"][0]

    def test_dry_run_env_knob_is_honoured(self, monkeypatch):
        monkeypatch.setenv("HSCC_MERGED_PR_RESOLVE_DRY_RUN", "1")
        kb = FakeKb({"hscc": [held_card()]})
        cli = FakeCli()
        assert mpr.dry_run() is True
        resolve(kb, FakeGh({"pom11/hscc/625": gh_view()}), cli)
        assert cli.calls == []

    def test_wedge_benign_hold_invariant_is_untouched(self):
        """t_160d306f must survive this feature: `active_pr` stays benign and
        the classifier still explains a zero-spawn tick with it (a merged-PR
        hold is resolved, never a reason to restart the gateway)."""
        line = ("2026-11-23 19:55:01,402 WARNING kanban dispatcher stuck: "
                "ready queue non-empty for 11 consecutive ticks but 0 workers "
                "spawned. Last tick held back: active_pr=1.")
        reasons, benign = dispatcher_wedge.parse_hold_clause(line)
        assert benign is True and reasons == [("active_pr", "1")]
        assert "active_pr" in dispatcher_wedge.BENIGN_HOLD_REASONS

    def test_held_and_reported_card_keeps_being_reported_not_retried(self):
        """A genuinely open PR is re-read each tick (it may merge later) but
        still produces zero mutations."""
        kb = FakeKb({"hscc": [held_card()]})
        gh = FakeGh({"pom11/hscc/625": gh_view(state="OPEN")})
        cli = FakeCli()
        for _ in range(3):
            assert resolve(kb, gh, cli)["resolved"] == []
        assert cli.calls == [] and len(gh.calls) == 3


class TestDaemonWiring:
    """The pass must actually run on the daemon's dispatcher tick — and run
    UNCONDITIONALLY, because t_160d306f keeps the wedge stream green for the
    exact hold this resolves (the 286-tick incident was never red)."""

    def _loop_body(self):
        from hscc_daemon import daemon_ops
        tree = ast.parse(inspect.getsource(daemon_ops))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "run_dispatcher_wedge_recover_loop"), None)
        assert fn is not None, "the dispatcher-wedge recovery loop is gone"
        loop = next((n for n in ast.walk(fn) if isinstance(n, ast.While)), None)
        assert loop is not None
        return loop

    def test_recovery_loop_calls_the_resolution_pass(self):
        loop = self._loop_body()
        calls = []
        for node in ast.walk(loop):
            if isinstance(node, ast.Call) and isinstance(node.func,
                                                         ast.Attribute):
                calls.append(ast.unparse(node))
        assert any("recover_dispatcher_wedge" in c for c in calls)
        assert any(c.strip().startswith("_mpr.tick(") for c in calls), \
            "merged_pr_resolve.tick() is not wired into the dispatcher tick"

    def test_resolution_pass_is_not_gated_on_a_declared_stall(self):
        """A direct child of the loop body, not buried in an ``if``: a stall
        the wedge explains as benign must not skip the resolution."""
        loop = self._loop_body()
        ticks = [n for n in loop.body
                 if isinstance(n, ast.Try)
                 and any(isinstance(s, ast.Expr)
                         and isinstance(s.value, ast.Call)
                         and ast.unparse(s.value).startswith("_mpr.tick(")
                         for s in n.body)]
        assert ticks, "_mpr.tick() is not an unconditional statement of the loop"
