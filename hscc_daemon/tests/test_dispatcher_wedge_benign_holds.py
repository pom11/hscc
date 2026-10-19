"""Benign dispatcher holds must NOT restart the gateway (t_160d306f).

2026-10-17 incident: card ``t_4e0f0509`` sat READY but was held by the
dispatcher's own respawn guard (``active_pr=1``); every stuck warning said
so ("Last tick held back: active_pr=1.") and the tick ladder climbed
6→11→16→21→26 across ~30 min while the watchdog kicked launchd three times
in 65 minutes. A held-back card is the dispatcher WORKING, not wedging.

Coverage maps 1:1 to the card's proof requirements:

  PARSE (fail-closed matrix)
    * the verbatim incident line parses and classifies benign
    * ``active_pr`` / ``recent_success`` / the cooldowns (rate_limited,
      skipped_locked, memory_pressure) are benign — the vocabulary mirrored
      from Hermes' describe_suppression
    * ``blocker_auth`` and ANY unknown/unparseable reason stay wedge-worthy
    * a stuck warning with NO hold clause stays wedge-worthy

  LAYER 1 (probe) — the required regression test
    * replay the REAL gateway.log tick ladder from the fixture
      (6,11,16,21,26 across ~30 min) → ZERO restart actions
    * same ladder, hold clause stripped → restart fires
    * same ladder, unknown hold (``frobnicated=1``) → restart fires
    * ``blocker_auth=1`` ladder → restart fires (credential wall = page)
    * stale benign warning outside the lookback (a wedged dispatcher stops
      writing) → restart fires
    * hold counts that do not reconcile against the board's pending queue →
      restart fires

  LAYER 2 (recovery board-state gate, sanctioned hermes CLI)
    * red stream + every ready card explainably held (active PR event /
      per-profile cap / fleet cap) → suppressed + notified, ZERO restarts
    * red stream + a plain ready card with no hold evidence → restart fires
    * gate unable to reach the CLI / gate raises → restart fires (fail closed)
    * suppression notifies on a throttle, not every tick

  NON-SPAWNABLE ASSIGNEE (t_ab960663 — the PR #43 review finding)
    * a ready card whose assignee names no Hermes profile suppresses the
      restart (a kickstart never fixes a typo) but must NOT silence us: the
      ladder notifies the operator exactly once, naming card + board +
      assignee, and states that no restart was attempted
    * mixed board (one cap-held card + one non-spawnable card) → zero
      restarts + the notification names ONLY the non-spawnable card
    * profile lookup that itself fails → "assume spawnable" → the restart
      path is unchanged (fail closed)
    * the distinct notify branch is mutation-probed (flip it off → red)

Every side effect (kanban read, gateway log tail, hermes CLI, clock, state
writer, restart runner, notification) is injectable — the suite never reads
the operator's real gateway.log, never shells out to the real CLI (the
conftest stubs the CLI-locator seam), and NEVER touches a live gateway.
"""

import datetime
import json
import os

import pytest

from hscc_daemon import dispatcher_wedge as dw
from hscc_daemon.tests.test_dispatcher_wedge import (
    FakeKanban, Recorder, READY, RUNNING,
)

FIXTURE = os.path.join(os.path.dirname(__file__),
                       "fixtures", "gateway_wedge_incident_20261017.log")

# The REQUIRED verbatim line from the incident report (card body, item 4).
INCIDENT_LINE_VERBATIM = (
    "kanban dispatcher stuck: ready queue non-empty for N consecutive ticks "
    "but 0 workers spawned. Last tick held back: active_pr=1.")


@pytest.fixture(autouse=True)
def _reset_wedge_state():
    """Module streaks are process-global; every test starts clean (mirrors
    test_dispatcher_wedge.py's own reset fixture)."""
    def _clean():
        dw._detector = {
            "stall_ticks": 0, "last_running_total": None, "declared": False,
            "declared_since": None, "peak_stall_ticks": 0,
            "benign_hold_ticks": 0, "last_hold_explained": False,
        }
        dw._recovery_detection_streak = 0
        dw._hold_notify_at = None
    _clean()
    yield
    _clean()


def _fixture_lines():
    with open(FIXTURE) as f:
        return [ln for ln in f.read().splitlines() if ln.strip()
                and not ln.startswith("#")]


def _ts(line):
    """Epoch seconds of a fixture log line (local tz, same as _log_line_ts)."""
    return datetime.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()


# ---------------------------------------------------------------------------
# Fixtures for the hold-evidence seams
# ---------------------------------------------------------------------------

@pytest.fixture()
def incident_log(tmp_path):
    """Copy of the redacted real gateway.log slice at a tmp path."""
    p = tmp_path / "gateway.log"
    p.write_text("\n".join(_fixture_lines()) + "\n")
    return str(p)


def _variant_log(tmp_path, old, new):
    """The fixture with every ``old`` replaced by ``new`` (ladder variants)."""
    lines = [ln.replace(old, new) for ln in _fixture_lines()]
    p = tmp_path / "gateway.log"
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _hold_fn(log_path):
    return lambda now: dw.classify_dispatcher_hold(now=now, log_path=log_path)


# ---------------------------------------------------------------------------
# PARSE — fail-closed matrix
# ---------------------------------------------------------------------------

class TestHoldClauseParsing:
    def test_verbatim_incident_line_parses_benign(self):
        reasons, benign = dw.parse_hold_clause(INCIDENT_LINE_VERBATIM)
        assert reasons == [("active_pr", "1")]
        assert benign is True

    def test_real_gateway_log_line_parses_benign(self):
        line = _fixture_lines()[3]  # 12:09:15 active_pr=1 warning, verbatim
        assert "Last tick held back: active_pr=1." in line
        reasons, benign = dw.parse_hold_clause(line)
        assert reasons == [("active_pr", "1")] and benign is True

    def test_stuck_warning_regex_matches_verbatim_line(self):
        assert dw._STUCK_WARNING_RE.search(INCIDENT_LINE_VERBATIM)

    @pytest.mark.parametrize("clause", [
        "active_pr=1",
        "recent_success=1",
        "rate_limited=2",
        "skipped_locked=1",
        "memory_pressure=critical",
        "active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1, "
        "memory_pressure=elevated",
    ])
    def test_benign_vocabulary_is_not_a_wedge(self, clause):
        reasons, benign = dw.parse_hold_clause(f"Last tick held back: {clause}.")
        assert benign is True, clause
        assert reasons  # parsed something

    @pytest.mark.parametrize("clause,reason", [
        ("blocker_auth=1", "blocker_auth"),      # credential wall = page
        ("frobnicated=1", "frobnicated"),        # unknown / future vocabulary
        ("active_pr=1, frobnicated=2", "frobnicated"),  # one poison pill
    ])
    def test_unknown_reasons_stay_wedge_worthy(self, clause, reason):
        reasons, benign = dw.parse_hold_clause(f"Last tick held back: {clause}.")
        assert benign is False, clause
        assert any(n == reason for n, _ in reasons)

    def test_garbage_clause_is_wedge_worthy(self):
        reasons, benign = dw.parse_hold_clause(
            "Last tick held back: ???nonsense???.")
        assert benign is False and reasons

    def test_absent_clause_is_not_benign(self):
        reasons, benign = dw.parse_hold_clause(
            "dispatcher stuck: ... 0 workers spawned. Check profile health.")
        assert reasons == [] and benign is False

    def test_vocabulary_mirrors_hermes_describe_suppression(self):
        """Every benign name must still exist in Hermes' dispatcher source.
        A RENAMED hold must fail loudly here rather than silently silence the
        watchdog with a stale name (a genuinely NEW name stays wedge-worthy
        by design — it will not be in BENIGN_HOLD_REASONS)."""
        import inspect
        try:
            from hermes_cli import kanban_db_dispatch as kbd
            src = inspect.getsource(kbd)
        except Exception:
            pytest.skip("hermes_cli not importable in this interpreter")
        for name in dw.BENIGN_HOLD_REASONS:
            assert name in src, (
                f"{name!r} no longer appears in Hermes' dispatcher source — "
                "the hold vocabulary moved; re-mirror it (do NOT silently "
                "keep silencing the watchdog)")


# ---------------------------------------------------------------------------
# LAYER 1 — classify_dispatcher_hold freshness + fail-closed
# ---------------------------------------------------------------------------

class TestClassifyDispatcherHold:
    def test_fresh_benign_warning_explains(self, incident_log):
        line = _fixture_lines()[3]
        info = dw.classify_dispatcher_hold(now=_ts(line) + 30,
                                           log_path=incident_log)
        assert info["classified"] and info["benign"]
        assert info["reasons"] == [("active_pr", "1")]

    def test_stale_warning_expires_the_exemption(self, incident_log):
        last = _fixture_lines()[-1]
        info = dw.classify_dispatcher_hold(
            now=_ts(last) + dw.DISPATCHER_HOLD_LOOKBACK_SECONDS + 60,
            log_path=incident_log)
        assert info["classified"] is False, (
            "a warning older than the lookback must not explain anything — "
            "a wedged dispatcher stops writing")

    def test_unreadable_log_is_no_evidence(self, tmp_path):
        info = dw.classify_dispatcher_hold(
            now=0.0, log_path=str(tmp_path / "does-not-exist.log"))
        assert info["classified"] is False and info["benign"] is False

    def test_no_warning_at_all_is_no_evidence(self, tmp_path):
        p = tmp_path / "gateway.log"
        p.write_text("2026-10-17 12:00:00,000 INFO gateway.run: nothing "
                     "to see here\n")
        info = dw.classify_dispatcher_hold(
            now=_ts(p.read_text()) + 5, log_path=str(p))
        assert info["classified"] is False

    def test_undated_warning_line_is_no_evidence(self, tmp_path):
        """The gateway's formatter stamps every record; an undated
        'stuck warning' cannot have its freshness judged, so it must NOT
        exonerate a wedge (fail closed)."""
        p = tmp_path / "gateway.log"
        p.write_text("WARNING gateway.run: kanban dispatcher stuck: ready "
                     "queue non-empty for 26 consecutive ticks but 0 workers "
                     "spawned. Last tick held back: active_pr=1.\n")
        info = dw.classify_dispatcher_hold(now=1_800_000_000.0,
                                           log_path=str(p))
        assert info["classified"] is False

    def test_future_stamped_warning_cannot_explain_a_past_tick(self, tmp_path):
        p = tmp_path / "gateway.log"
        p.write_text("2026-10-17 14:00:00,000 WARNING gateway.run: kanban "
                     "dispatcher stuck: ready queue non-empty for 6 "
                     "consecutive ticks but 0 workers spawned. Last tick "
                     "held back: active_pr=1.\n")
        # now = 13:00 — the only warning on disk is stamped an hour LATER.
        info = dw.classify_dispatcher_hold(
            now=datetime.datetime(2026, 10, 17, 13, 0, 0).timestamp(),
            log_path=str(p))
        assert info["classified"] is False


# ---------------------------------------------------------------------------
# LAYER 1 — the REQUIRED regression: replay today's tick ladder
# ---------------------------------------------------------------------------

def _warn_lines():
    return [ln for ln in _fixture_lines() if dw._STUCK_WARNING_RE.search(ln)]


def _replay_ladder(log_path, boards, cap=4, extra_ticks=6, per_profile=None,
                   gate=None):
    """Run probe tick + recovery tick at every stuck-warning timestamp of
    the fixture (bare and held — the watchdog ticks on its own cadence and
    reads whatever the newest warning says), then ``extra_ticks``
    continuation ticks (5 min apart) so a real wedge has enough red ticks to
    clear the N-declare and M-restart debounces.

    ``gate`` is passed through to the recovery as ``board_gate_fn`` (the
    layer-2 board gate); None keeps the conftest-stubbed default, which is
    the documented fail-closed "cannot verify" path.

    Returns (restart_recorder, notifications, streams_per_tick, results).
    """
    warns = _warn_lines()
    counts = [dw._STUCK_WARNING_RE.search(ln).group(1) for ln in warns]
    # The incident as the gateway actually logged it: two bare warnings at
    # the end of a healthy spawn run, then the held ladder 6→11→16→21→26
    # across ~30 min after the PR comment landed (12:49 → 13:09).
    assert counts == ["6", "11", "11", "11", "6", "11", "16", "21", "26",
                      "6"], f"fixture must carry the real ladder, got {counts}"
    kb = FakeKanban(boards)
    rec = Recorder()
    notifs = []
    streams, results = [], []

    def tick(now):
        captured = {}
        ok = dw.check_dispatcher_wedge(
            kanban_db=kb, max_in_progress=cap, now=now,
            write_state_fn=lambda name, data: captured.__setitem__(name, data),
            max_per_profile=per_profile if per_profile is not None
            else 1_000_000,
            hold_fn=_hold_fn(log_path))
        stream = captured.get("dispatcher", {"ok": ok})
        streams.append(stream)
        results.append(dw.recover_dispatcher_wedge(
            stream_state=stream, restart_fn=rec, now=now,
            board_gate_fn=gate, notify_fn=notifs.append))

    for ln in warns:
        tick(_ts(ln))
    last = _ts(warns[-1])
    for k in range(extra_ticks):
        tick(last + (k + 1) * 300.0)
    return rec, notifs, streams, results


class TestIncidentLadderReplay:
    """The card's item 4: replay the exact incident → ZERO restart actions;
    same ladder without (or against) a benign hold → the restart fires."""

    def test_real_incident_ladder_restarts_nothing(self, incident_log):
        # The board state the dispatcher was holding against: 1 pending card,
        # free worker slots — exactly why the OLD logic called it a wedge
        # (spawnable + roomy + zero spawns).
        rec, notifs, streams, _ = _replay_ladder(
            incident_log, {"default": [READY]})
        assert rec.calls == 0, "benign active_pr hold must never restart"
        assert all(s["ok"] is True for s in streams), (
            "the stream must never go red while the dispatcher reports a "
            "benign hold")
        assert any(s.get("hold_explained") for s in streams)

    def test_bare_warning_ladder_still_restarts(self, tmp_path):
        """Same ladder with the hold clause STRIPPED (the dispatcher holds
        back NOTHING while spawning nothing) → the original purpose stands:
        the restart must fire."""
        path = _variant_log(tmp_path, "Last tick held back: active_pr=1. ", "")
        rec, _, streams, results = _replay_ladder(path, {"default": [READY]})
        assert rec.calls >= 1, (
            "a true wedge (no hold explanation) must still escalate — "
            f"streams={[s['ok'] for s in streams]} results={results}")

    def test_unknown_hold_ladder_still_restarts(self, tmp_path):
        path = _variant_log(tmp_path, "active_pr=1", "frobnicated=1")
        rec, _, _, results = _replay_ladder(path, {"default": [READY]})
        assert rec.calls >= 1, (
            f"unknown hold vocabulary must stay wedge-worthy {results}")

    def test_blocker_auth_ladder_still_restarts(self, tmp_path):
        path = _variant_log(tmp_path, "active_pr=1", "blocker_auth=1")
        rec, _, _, _ = _replay_ladder(path, {"default": [READY]})
        assert rec.calls >= 1, "a credential wall must still escalate"

    def test_unreconcilable_hold_counts_still_restart(self, tmp_path):
        """The dispatcher claims 3 held cards but the board holds only 1:
        the claim does not add up → not trusted → wedge path proceeds."""
        path = _variant_log(tmp_path, "active_pr=1", "active_pr=3")
        rec, _, _, _ = _replay_ladder(path, {"default": [READY]})
        assert rec.calls >= 1

    def test_stale_benign_warning_cannot_mask_a_live_wedge(self, tmp_path):
        """A dispatcher that WEDGES stops writing warnings; once the last
        (benign) warning ages out of the lookback, the stall must declare and
        the restart must fire even though the newest line on disk is benign."""
        p = tmp_path / "gateway.log"
        t0 = datetime.datetime(2026, 10, 17, 12, 0, 0).timestamp()
        p.write_text(
            "2026-10-17 12:00:00,000 WARNING gateway.run: kanban dispatcher "
            "stuck: ready queue non-empty for 6 consecutive ticks but 0 "
            "workers spawned. Last tick held back: active_pr=1. Check "
            "profile health (venv, PATH, credentials) and "
            "`hermes kanban list --status ready`.\n")
        kb = FakeKanban({"default": [READY]})
        rec = Recorder()
        for k in range(dw.DISPATCHER_DETECT_TICKS
                       + dw.DISPATCHER_RESTART_AFTER_DETECTIONS + 4):
            now = t0 + dw.DISPATCHER_HOLD_LOOKBACK_SECONDS + 60 + k * 60
            captured = {}
            dw.check_dispatcher_wedge(
                kanban_db=kb, max_in_progress=4, now=now,
                write_state_fn=lambda n, d: captured.__setitem__(n, d),
                max_per_profile=1_000_000,
                hold_fn=_hold_fn(str(p)))
            dw.recover_dispatcher_wedge(
                stream_state=captured.get("dispatcher", {"ok": True}),
                restart_fn=rec, now=now, notify_fn=lambda m: None)
        assert rec.calls == 1, (
            "an aged-out benign hold must not shield a live wedge forever")


# ---------------------------------------------------------------------------
# LAYER 2 — belt-and-braces board-state gate (sanctioned hermes CLI)
# ---------------------------------------------------------------------------

class FakeProc:
    def __init__(self, payload, rc=0):
        self.returncode = rc
        self.stdout = json.dumps(payload)
        self.stderr = ""


class FakeCli:
    """Fake sanctioned CLI: matches subcommand-key tuples against argv."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        for key, payload in self.payloads.items():
            if all(k in argv for k in key):
                return FakeProc(payload)
        return FakeProc({"error": "unmapped argv"}, rc=1)


def _ready_row(tid, assignee="worker-a"):
    return {"id": tid, "status": "ready", "assignee": assignee}


def _show_with_guard(tid, reason, age_s, now):
    return {"task": {"id": tid, "status": "ready", "assignee": "worker-a"},
            "events": [{"kind": "respawn_guarded",
                        "payload": {"reason": reason},
                        "created_at": now - age_s}],
            "runs": [], "comments": []}


@pytest.fixture()
def cli_available(monkeypatch):
    monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
    # Fake assignees ("worker-a") are not real profiles. Under the Hermes venv
    # `hermes_cli.profiles` imports, so without this pin they read as
    # non-spawnable and skip the hold-evidence path these tests exercise.
    monkeypatch.setattr(dw, "_assignee_is_nonspawnable", lambda assignee: False)
    # max_per_profile=None falls back to the LIVE ~/.hermes config (cap 2 on
    # the operator's box); three running worker-a rows would then be
    # "explained" by the per-profile cap instead of the hold under test.
    monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)


class TestBoardStateGate:
    def test_active_pr_hold_on_every_card_suppresses_restart(
            self, cli_available):
        now = 1_800_000_000.0
        kb = FakeKanban({"default": [READY, RUNNING, RUNNING, RUNNING]})
        cli = FakeCli({
            ("list", "--status", "ready"): [_ready_row("t_x1")],
            ("show", "t_x1"): _show_with_guard("t_x1", "active_pr", 60, now),
        })
        stream = {"ok": False, "declared": True, "spawnable": ["default"],
                  "total_running": 3}
        explained, why = dw.board_state_explains_stall(
            stream, kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=now)
        assert explained is True, why
        assert "active_pr" in why
        # The gate really did go through the sanctioned CLI:
        assert cli.calls and cli.calls[0][:2] == ["hermes", "kanban"]

    def test_plain_ready_card_without_hold_restarts(self, cli_available):
        now = 1_800_000_000.0
        kb = FakeKanban({"default": [READY]})
        cli = FakeCli({
            ("list", "--status", "ready"): [_ready_row("t_plain")],
            ("show", "t_plain"): {"task": {"id": "t_plain"}, "events": [],
                                  "runs": [], "comments": []},
        })
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=now)
        assert explained is False
        assert "unexplained" in why

    def test_fleet_at_capacity_explains_every_board(self, cli_available):
        kb = FakeKanban({"default": [READY] + [RUNNING] * 4})
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 4},
            kanban_db=kb, max_in_progress=4, max_per_profile=None,
            cli_fn=FakeCli({}), now=1.0)
        assert explained is True and "at capacity" in why

    def test_per_profile_cap_explains(self, cli_available):
        now = 1_800_000_000.0
        # worker-a has 2 running (cap 2) + ready work → per-profile hold.
        kb = FakeKanban({"default": [READY, RUNNING, RUNNING]})
        cli = FakeCli({("list", "--status", "ready"): [_ready_row("t_cap")]})
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 2},
            kanban_db=kb, max_in_progress=6, max_per_profile=2,
            cli_fn=cli, now=now)
        assert explained is True and "explainably held" in why
        assert not any("show" in c for c in cli.calls), (
            "a capped profile needs no per-card lookup")

    def test_unassigned_ready_card_is_unexplained(self, cli_available):
        kb = FakeKanban({"default": [("ready", None)]})
        cli = FakeCli({("list", "--status", "ready"):
                       [{"id": "t_u", "status": "ready", "assignee": None}]})
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=1.0)
        assert explained is False

    @pytest.mark.parametrize("payload", [None, "not-json", {"weird": True}])
    def test_unparseable_cli_payload_fails_closed(self, cli_available,
                                                  payload):
        kb = FakeKanban({"default": [READY]})
        cli = FakeCli({("list", "--status", "ready"): payload})
        explained, _ = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=1.0)
        assert explained is False, "an unreadable board never silences us"

    def test_missing_cli_binary_fails_closed(self, monkeypatch):
        monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: None)
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=FakeKanban({"default": [READY]}),
            max_in_progress=6, now=1.0)
        assert explained is False and "CLI" in why

    def test_rate_limited_failure_error_explains(self, cli_available):
        now = 1_800_000_000.0
        kb = FakeKanban({"default": [READY]})
        cli = FakeCli({
            ("list", "--status", "ready"): [_ready_row("t_rl")],
            ("show", "t_rl"): {
                "task": {"id": "t_rl",
                         "last_failure_error": "rate limit hit (EX_TEMPFAIL)"},
                "events": [], "runs": [], "comments": []},
        })
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=now)
        assert explained is True and "rate_limited" in why

    def test_blocker_auth_event_never_explains(self, cli_available):
        now = 1_800_000_000.0
        kb = FakeKanban({"default": [READY]})
        cli = FakeCli({
            ("list", "--status", "ready"): [_ready_row("t_auth")],
            ("show", "t_auth"): _show_with_guard("t_auth", "blocker_auth",
                                                 60, now),
        })
        explained, _ = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=now)
        assert explained is False

    def test_stale_guard_event_is_not_a_hold(self, cli_available):
        """A 30-day-old active_pr event is outside Hermes' own 24h PR window
        — it no longer describes why the dispatcher would hold the card."""
        now = 1_800_000_000.0
        kb = FakeKanban({"default": [READY]})
        cli = FakeCli({
            ("list", "--status", "ready"): [_ready_row("t_old")],
            ("show", "t_old"): _show_with_guard("t_old", "active_pr",
                                                30 * 86400, now),
        })
        explained, _ = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=now)
        assert explained is False


class TestGateWiredIntoRecovery:
    """The gate runs on the restart path — before any launchctl kick."""

    def _red(self):
        return {"ok": False, "declared": True, "spawnable": ["default"],
                "total_running": 0}

    def _drive_to_restart(self, tmp_path, gate_fn, notify_fn=None):
        state = str(tmp_path / "recover.json")
        rec = Recorder()
        notifs = []
        results = []
        for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS):
            results.append(dw.recover_dispatcher_wedge(
                stream_state=self._red(), restart_fn=rec,
                now=1000.0 + k * 60, state_file=state,
                board_gate_fn=gate_fn,
                notify_fn=notify_fn or notifs.append))
        return rec, notifs, results, state

    def test_explained_stall_suppresses_and_notifies(self, tmp_path):
        rec, notifs, results, _ = self._drive_to_restart(
            tmp_path, lambda s: (True, "board 'default': all 1 ready cards "
                                       "explainably held (active_pr=1)"))
        assert rec.calls == 0, "explained hold must never kick launchd"
        assert results[-1]["result"] == "suppressed"
        assert results[-1]["reason"] == "benign_dispatcher_hold"
        assert len(notifs) == 1, "log + notify, exactly one notification"
        assert "active_pr" in notifs[0]

    def test_unexplained_stall_restarts(self, tmp_path):
        rec, notifs, results, _ = self._drive_to_restart(
            tmp_path, lambda s: (False, "unexplained ready cards: t_x"))
        assert rec.calls == 1
        assert results[-1]["result"] == "recovered"
        assert notifs == []

    def test_raising_gate_fails_closed_toward_restart(self, tmp_path):
        def boom(_s):
            raise RuntimeError("gate exploded")
        rec, _, results, _ = self._drive_to_restart(tmp_path, boom)
        assert rec.calls == 1, "a broken gate must not disarm the watchdog"
        assert results[-1]["result"] == "recovered"

    def test_suppression_notifies_on_a_throttle_not_every_tick(self, tmp_path):
        """3 hours of 60s recovery ticks against a sustained hold: notify is
        throttled (the incident restarted the gateway every 30 min; we must
        not page the operator every minute either)."""
        state = str(tmp_path / "recover.json")
        rec = Recorder()
        notifs = []
        t = 1000.0
        for _ in range(int(3 * 3600 / 60)):
            dw.recover_dispatcher_wedge(
                stream_state=self._red(), restart_fn=rec, now=t,
                state_file=state,
                board_gate_fn=lambda s: (True, "held: active_pr=1"),
                notify_fn=notifs.append)
            t += 60
        assert rec.calls == 0
        assert 1 <= len(notifs) <= 4, (
            f"expected ~1 notify/hour over 3h, got {len(notifs)}")

    def test_default_gate_fails_closed_when_cli_absent(self, tmp_path):
        """Integration of the DEFAULT gate on the restart path: with the CLI
        locator stubbed to None (conftest), the gate cannot verify anything →
        the restart proceeds (every pre-existing recovery test relies on
        exactly this)."""
        rec, _, results, _ = self._drive_to_restart(tmp_path, None)
        assert rec.calls == 1, (
            "unverifiable board state must NOT silence the restart path")
        assert results[-1]["result"] == "recovered"


# ---------------------------------------------------------------------------
# COMBINED CASE — parked review row vs the benign-hold layers (t_dc04732e)
# ---------------------------------------------------------------------------
# Neither branch tested this pairing alone: #35 owns "a review row a runtime
# cannot dispatch is not spawnable work", #43 owns "a claimed benign hold is
# not a wedge". Rebase review found the interaction: main's reconciliation
# counter (``pending >= claimed``) counted ``ready+review`` rows
# UNCONDITIONALLY, so on a runtime WITHOUT a review lane one parked review
# row padded the count and reconciled a claim the dispatcher could not have
# made (its own tick never enumerates review rows when
# ``review_dispatch_enabled()`` is off). A bogus claim then silently exempted
# a live wedge for up to the lookback window. The fix threads #35's same
# capability answer (``review_capable``) through the pending lane set.
#
# The L2 half is a NEGATIVE pin: the board-state gate must never let a parked
# review row count as part of "every ready card explainably held".

REVIEW_ROW = ("review", "architect")


def _hold_log(tmp_path, now, clause="active_pr=3"):
    """A gateway log whose single stuck warning is stamped at ``now`` with
    ``clause`` — always inside the lookback of the tick that reads it, so
    LAYER-1 classification (fresh + benign) is held constant while the
    reconciliation is what is under test."""
    p = tmp_path / "gateway.log"
    stamp = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    p.write_text(
        f"{stamp},000 WARNING gateway.run: kanban dispatcher stuck: ready "
        "queue non-empty for 11 consecutive ticks but 0 workers spawned. "
        f"Last tick held back: {clause}. Check profile health (venv, PATH, "
        "credentials) and `hermes kanban list --status ready`.\n")
    return str(p)


class TestCombinedCaseReviewRowVsBenignHold:
    """The case no branch covered alone (rebase t_dc04732e)."""

    @pytest.mark.parametrize("review_capable,expected", [
        (False, 1),  # parked review row must NOT pad the reconciliation
        (True, 2),   # dispatchable lane: a held review card may legitimately
                     # sit in the dispatcher's hold count
    ])
    def test_pending_lane_set_follows_review_capability(self, review_capable,
                                                        expected):
        """RED-GREEN pin for the pending-lane fix (t_dc04732e).

        Before the fix both parameterisations returned 2 (the count ignored
        the capability answer entirely), so the non-capable case could not
        fail-close its exemption.
        """
        kb = FakeKanban({"default": [READY, REVIEW_ROW]})
        cap = dw._capture_kanban(kanban_db=kb, max_in_progress=4,
                                 max_per_profile=1_000_000,
                                 review_capable=review_capable)
        assert cap["total_pending"] == expected

    def test_parked_review_row_cannot_reconcile_a_claimed_hold(self):
        """Non-capable runtime: board has 1 dispatchable pending row, the
        clause claims 3 held. Reconciling 3 against 1+1-parked-review is the
        bug; the tick must be judged exactly as an unreconcilable claim."""
        kb = FakeKanban({"default": [READY, REVIEW_ROW, REVIEW_ROW]})
        cap = dw._capture_kanban(kanban_db=kb, max_in_progress=4,
                                 max_per_profile=1_000_000,
                                 review_capable=False)
        assert cap["total_pending"] == 1 and cap["stale_review"] is True
        det = {"stall_ticks": 0, "last_running_total": None,
               "declared": False, "declared_since": None,
               "peak_stall_ticks": 0}
        dw.evaluate_stall_tick(
            cap, det, 1000.0,
            hold_info={"classified": True, "benign": True,
                       "reasons": [("active_pr", "3")], "age_s": 30})
        assert det["last_hold_explained"] is False, (
            "a parked review row on a runtime with no review lane must not "
            "reconcile the dispatcher's hold claim")

    def test_held_review_card_reconciles_when_lane_is_spawnable(self):
        """The paired positive: when this runtime CAN dispatch review, a
        review card can legitimately sit in the dispatcher's hold count, so
        1 ready + 2 review reconcile a claim of 3 (today's #43 semantics
        keep working — the fix is capability-scoped, not a blanket ban)."""
        kb = FakeKanban({"default": [READY, REVIEW_ROW, REVIEW_ROW]})
        cap = dw._capture_kanban(kanban_db=kb, max_in_progress=4,
                                 max_per_profile=1_000_000,
                                 review_capable=True)
        assert cap["total_pending"] == 3 and cap["stale_review"] is False
        det = {"stall_ticks": 0, "last_running_total": None,
               "declared": False, "declared_since": None,
               "peak_stall_ticks": 0}
        dw.evaluate_stall_tick(
            cap, det, 1000.0,
            hold_info={"classified": True, "benign": True,
                       "reasons": [("active_pr", "3")], "age_s": 30})
        assert det["last_hold_explained"] is True

    def test_full_probe_padded_hold_still_declares_and_restarts(self,
                                                                 tmp_path):
        """End-to-end for the exact combined state: ready work + a parked
        review row + a fresh benign clause that only balances if the parked
        row counts. The stall must DECLARE (the bug suppressed it for the
        whole lookback) and recovery must restart EXACTLY once — the #43
        benign-hold exemption must not shield it, the #35 restart latch must
        not shield it (the stall is dispatcher-fixable: proven ready rows)."""
        t0 = datetime.datetime(2026, 10, 18, 9, 0, 0).timestamp()
        kb = FakeKanban({"default": [READY, REVIEW_ROW]})
        rec = Recorder()
        streams, results = [], []
        for k in range(8):
            now = t0 + k * 60.0
            log_path = _hold_log(tmp_path, now, clause="active_pr=2")
            captured = {}
            dw.check_dispatcher_wedge(
                kanban_db=kb, max_in_progress=4, now=now,
                write_state_fn=lambda n, d: captured.__setitem__(n, d),
                max_per_profile=1_000_000, review_capable=False,
                hold_fn=_hold_fn(log_path))
            stream = captured["dispatcher"]
            streams.append(stream)
            results.append(dw.recover_dispatcher_wedge(
                stream_state=stream, restart_fn=rec, now=now,
                notify_fn=lambda m: None))
        assert [s["ok"] for s in streams][-3:] == [False, False, False], (
            f"stream must go red: {[s['ok'] for s in streams]}")
        assert rec.calls == 1, (
            f"a live wedge padded by a parked review row must still "
            f"escalate exactly once; results={results}")
        assert streams[-1]["stale_review"] is True, (
            "the parked row is still REPORTED on the red stream")

    def test_same_board_stays_green_when_review_is_spawnable(self, tmp_path):
        """Counterpart with the review lane dispatchable and a claim that
        reconciles honestly (3 dispatchable pending rows, claim 3): the #43
        benign hold must suppress — this file's core promise survives #35."""
        t0 = datetime.datetime(2026, 10, 18, 9, 0, 0).timestamp()
        kb = FakeKanban({"default": [READY, REVIEW_ROW, REVIEW_ROW]})
        rec = Recorder()
        streams = []
        for k in range(8):
            now = t0 + k * 60.0
            log_path = _hold_log(tmp_path, now, clause="active_pr=3")
            captured = {}
            dw.check_dispatcher_wedge(
                kanban_db=kb, max_in_progress=4, now=now,
                write_state_fn=lambda n, d: captured.__setitem__(n, d),
                max_per_profile=1_000_000, review_capable=True,
                hold_fn=_hold_fn(log_path))
            streams.append(captured["dispatcher"])
            dw.recover_dispatcher_wedge(
                stream_state=captured["dispatcher"], restart_fn=rec,
                now=now, notify_fn=lambda m: None)
        assert all(s["ok"] is True for s in streams), (
            "a genuinely reconciled benign hold stays green when review is "
            f"spawnable: {[s['ok'] for s in streams]}")
        assert rec.calls == 0
        assert any(s.get("hold_explained") for s in streams)

    def test_board_gate_never_explains_with_a_parked_review_row(
            self, cli_available):
        """L2 negative pin: the sanctioned CLI gate enumerates READY cards
        only. Even with a parked review row carrying a fresh benign hold
        event on the same board, a plain unheld ready card stays
        unexplained — a parked review row can never be the evidence that a
        genuine wedge is 'just the dispatcher holding'."""
        now = 1_800_000_000.0
        kb = FakeKanban({"default": [READY, REVIEW_ROW]})
        cli = FakeCli({
            ("list", "--status", "ready"): [_ready_row("t_plain")],
            ("show", "t_plain"): {"task": {"id": "t_plain"}, "events": [],
                                  "runs": [], "comments": []},
            ("show", "t_rev"): _show_with_guard("t_rev", "active_pr", 60,
                                                now),
        })
        # The stream from the probe: the board is spawnable because of the
        # READY row (review-capable runtime), so the gate DOES consult it.
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"], "total_running": 0},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=cli, now=now)
        assert explained is False, (
            f"parked review row must not explain the ready wedge: {why}")
        assert "unexplained" in why
        # And the gate only ever asked for READY cards:
        assert all("review" not in c for c in cli.calls), \
            "the gate must not enumerate review rows as hold evidence"

    def test_review_only_board_is_never_asked_by_the_gate(
            self, cli_available, monkeypatch):
        """The stronger L2 pin on the #35 side: on a runtime that cannot
        dispatch review, a board whose ONLY work is a parked review row is
        not in the probe's ``spawnable`` list at all, so the gate must not
        even be consulted for it — and a red stream naming no spawnable
        board fails closed toward the restart (the gate cannot be silenced
        by parked review rows)."""
        kb = FakeKanban({"default": [REVIEW_ROW]})
        cap = dw._capture_kanban(kanban_db=kb, max_in_progress=4,
                                 max_per_profile=1_000_000,
                                 review_capable=False)
        assert cap["spawnable"] == [] and cap["stale_review"] is True
        asked = []

        def spy_gate_fn(board):
            asked.append(board)
            return (True, "unreachable in this test")

        monkeypatch.setattr(dw, "_board_ready_facts", spy_gate_fn)
        explained, why = dw.board_state_explains_stall(
            {"ok": False, "spawnable": cap["spawnable"],
             "total_running": 0,
             "stale_review": True, "stale_review_boards": ["default"],
             "review_count": 1},
            kanban_db=kb, max_in_progress=6, max_per_profile=None,
            cli_fn=FakeCli({}), now=1.0)
        assert asked == [], "gate must not consult a non-spawnable board"
        assert explained is False, (
            "a red stream naming no spawnable board must fail closed")


# NON-SPAWNABLE ASSIGNEE — notify, do not go quiet (t_ab960663)
# ---------------------------------------------------------------------------
# The PR #43 review finding: the L2 gate counted a ready card whose assignee
# is not a Hermes profile as an EXPLAINED hold, so the watchdog suppressed the
# restart AND said nothing — on exactly the silent no-dispatch case (dispatcher
# silently drops unknown assignees; the card sits in ``ready`` forever).
# Suppressing the restart is correct; silence is not.
#
# Detection is NOT redefined here: the gate keeps using ``_assignee_is_nonspawnable``
# (the profile lookup mirroring the dispatcher's own ``skipped_nonspawnable``
# bucket) and these tests stub that ONE seam, as ``cli_available`` does.

# A ready card on a name no profile matches (typo'd, or a deleted profile).
READY_GHOST = ("ready", "ghost-bot")
# The REAL profile-probe helper, captured at import time — before the autouse
# sandbox in conftest replaces it — so the fail-closed test can exercise the
# actual code path rather than the sandbox's stub.
_REAL_NONSPAWNABLE = dw._assignee_is_nonspawnable
# A review-status card on a REAL fake profile. Its only job is to keep the
# probe's own ``has_spawnable_ready``/``has_spawnable_review`` verdict honest:
# the board is genuinely spawnable to Hermes' definition (review work on a real
# lane) while NO READY card is runnable — the production shape where the L2
# gate runs and finds every ready card non-spawnable.
REVIEW_REAL = ("review", "worker-a")


def _ghost_gate(dead, detail="board 'default': all ready cards explainably "
                            "held (active_pr=1)"):
    """A gate stub shaped exactly like the real ``GateVerdict``: explained, with
    a generic hold ``detail`` plus the structured non-spawnable findings. The
    detail deliberately names nothing from ``dead``, so a test can tell the
    DISTINCT config-typo message apart from the generic hold message."""
    return (lambda stream: dw.GateVerdict(True, detail, dead)), detail


class TestNonspawnableAssigneeGate:
    """Layer-2 detection: which cards the gate files as non-spawnable."""

    def _drive(self, monkeypatch, ready_rows, extra_cli=None, per_profile=None,
               running=(), nonspawnable=("ghost-bot",), patch_probe=True):
        """Run the real gate over one board with ``ready_rows`` listed by the
        stub CLI.

        ``patch_probe`` stubs the ONE non-spawnability seam the gate uses
        (``_assignee_is_nonspawnable``); pass False to exercise the real helper
        against a sabotaged profile lookup.

        Returns (verdict, explained, why, findings, cli)."""
        monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
        monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)
        monkeypatch.setattr(dw, "_read_max_in_progress", lambda: None)
        if patch_probe:
            monkeypatch.setattr(dw, "_assignee_is_nonspawnable",
                                lambda assignee: assignee in nonspawnable)
        now = 1_800_000_000.0
        kb = FakeKanban({"default": list(running)})
        payloads = {("list", "--status", "ready"): list(ready_rows)}
        payloads.update(extra_cli or {})
        cli = FakeCli(payloads)
        verdict = dw.board_state_explains_stall(
            {"ok": False, "spawnable": ["default"],
             "total_running": len(running)},
            kanban_db=kb, max_in_progress=6, max_per_profile=per_profile,
            cli_fn=cli, now=now)
        return (verdict, verdict[0], verdict[1],
                dw._gate_nonspawnable(verdict), cli)

    def test_gate_carries_nonspawnable_findings(self, monkeypatch):
        verdict, explained, why, dead, cli = self._drive(
            monkeypatch, [_ready_row("t_ghost", assignee="ghost-bot")])
        assert explained is True, why          # restart suppression stays correct
        assert "non-spawnable" in why          # …but the evidence says WHY
        assert dead == [{"board": "default", "card": "t_ghost",
                         "assignee": "ghost-bot"}]
        assert cli.calls[0][:2] == ["hermes", "kanban"]

    def test_findings_shape_is_a_2_tuple_for_callers(self, monkeypatch):
        """The gate's contract stays a 2-tuple: old callers that unpack it (and
        the injectable fakes that return one) keep working."""
        verdict, explained, why, dead, _ = self._drive(
            monkeypatch, [_ready_row("t_ghost", assignee="ghost-bot")])
        assert isinstance(verdict, tuple) and len(verdict) == 2
        a, b = verdict                      # plain unpacking, as every caller does
        assert (a, b) == (explained, why)
        # A plain tuple carries no findings (custom gates stay valid).
        assert dw._gate_nonspawnable((True, "held")) == []
        assert dw._gate_nonspawnable(verdict) == list(dead)

    def test_mixed_board_reports_only_the_nonspawnable_card(self, monkeypatch):
        """One card held by the per-profile cap + one typo'd card: the gate
        explains the board (no restart) and names ONLY the typo'd card."""
        # worker-a at cap (2 running, cap 2) explains t_cap with no per-card
        # lookup; ghost-bot is not a profile at all.
        verdict, explained, why, dead, cli = self._drive(
            monkeypatch,
            [_ready_row("t_cap", assignee="worker-a"),
             _ready_row("t_ghost", assignee="ghost-bot")],
            per_profile=2,
            running=[("running", "worker-a"), ("running", "worker-a")])
        assert explained is True, why
        assert dead == [{"board": "default", "card": "t_ghost",
                         "assignee": "ghost-bot"}]
        assert "t_ghost" in why and "t_cap" not in why
        assert not any("show" in c for c in cli.calls)

    def test_all_spawnable_card_is_never_reported(self, monkeypatch):
        """The findings list is exactly the non-spawnable subset: a board whose
        ready cards all sit on real lanes (held by an active PR) reports
        nothing, so the generic hold notification stays untouched."""
        now = 1_800_000_000.0
        verdict, explained, why, dead, _ = self._drive(
            monkeypatch,
            [_ready_row("t_ok")],
            extra_cli={("show", "t_ok"): _show_with_guard("t_ok", "active_pr",
                                                           60, now)})
        assert explained is True and dead == []
        assert "non-spawnable" not in why and "active_pr" in why

    def test_profile_lookup_failure_keeps_the_restart_path(self, monkeypatch):
        """Fail CLOSED at the source: when the profile lookup cannot decide,
        ``_assignee_is_nonspawnable`` returns False ("assume spawnable"), the
        card then needs real hold evidence, has none → unexplained → restart.

        Runs the REAL helper (the sandbox stub is replaced by the function
        captured at import time) against a ``hermes_cli.profiles`` whose
        ``profile_exists`` raises, so this pins the helper's own fail-closed
        direction rather than a stub's return value. Interpreter-agnostic: on
        a box without ``hermes_cli`` the import branch fails the same way."""
        import sys
        class _BrokenProfiles:
            @staticmethod
            def profile_exists(name):
                raise RuntimeError("profile store unreadable")
        monkeypatch.setitem(sys.modules, "hermes_cli.profiles", _BrokenProfiles)
        monkeypatch.setattr(dw, "_assignee_is_nonspawnable", _REAL_NONSPAWNABLE)
        assert dw._assignee_is_nonspawnable("ghost-bot") is False, (
            "a profile lookup that raises must mean 'assume spawnable' — the "
            "stricter, fail-closed direction")
        verdict, explained, why, dead, _ = self._drive(
            monkeypatch,
            [_ready_row("t_ghost", assignee="ghost-bot")],
            extra_cli={("show", "t_ghost"): {"task": {"id": "t_ghost"},
                                              "events": [], "runs": [],
                                              "comments": []}},
            patch_probe=False)   # the real helper, with the lookup sabotaged
        assert explained is False
        assert dead == [] and "unexplained" in why


class TestNonspawnableAssigneeNotifies:
    """The operator must hear about it: distinct message, one notify, 0 restarts."""

    def _dead(self, card="t_ghost", board="default", assignee="ghost-bot"):
        return [{"board": board, "card": card, "assignee": assignee}]

    def test_ladder_with_only_nonspawnable_cards_notifies_once_and_never_restarts(
            self, incident_log, tmp_path, monkeypatch):
        """THE required ladder: the real 6→11→16→21→26 warning ladder (hold
        clause stripped — the dispatcher gives NO benign reason), on a board
        whose READY cards all sit on a non-existent profile → ZERO restarts AND
        exactly one operator notification naming card, board and assignee."""
        monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)
        path = _variant_log(tmp_path, "Last tick held back: active_pr=1. ", "")
        dead = self._dead()
        gate, _ = _ghost_gate(dead)
        rec, notifs, streams, results = _replay_ladder(
            path, {"default": [READY_GHOST, REVIEW_REAL]}, gate=gate)
        assert rec.calls == 0, (
            "restarting the gateway has never fixed a typo'd assignee — "
            f"results={results}")
        assert len(notifs) == 1, (
            f"exactly one throttled notification, got {len(notifs)}: {notifs}")
        msg = notifs[0]
        assert "t_ghost" in msg and "ghost-bot" in msg and "default" in msg
        assert "no worker will ever run it" in msg
        assert "restart" in msg.lower()          # states that none was attempted
        assert any(r["result"] == "suppressed" for r in results)
        assert any(r.get("reason") == "nonspawnable_assignee" for r in results)

    def test_ladder_without_the_notify_branch_stays_quiet(self, tmp_path,
                                                          monkeypatch):
        """Mutation sentinel for the notify branch: with the findings suppressed
        (what PR #43 shipped — explained ⇒ suppress AND go quiet) the same
        ladder must produce ZERO notifications. If this test ever passes with
        ``notifs`` non-empty, the two classes are no longer distinguishable."""
        monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)
        path = _variant_log(tmp_path, "Last tick held back: active_pr=1. ", "")
        silent_gate = lambda stream: (True, "board 'default': all 1 ready cards "
                                           "explainably held")   # plain 2-tuple
        rec, notifs, _streams, results = _replay_ladder(
            path, {"default": [READY_GHOST, REVIEW_REAL]}, gate=silent_gate)
        assert rec.calls == 0
        assert len(notifs) == 1, (
            "the plain-hold class still notifies — the DISTINCT message is the "
            f"only thing that changes, got {notifs}")
        assert "ghost" not in notifs[0], (
            "a gate that reports no findings must not invent one: " + notifs[0])

    def test_mixed_board_notification_names_only_the_nonspawnable_card(
            self, tmp_path):
        """Required mixed-board case: cap-held card + typo'd card → zero
        restarts, and the notification is about the card the operator can act
        on — the capped card is NOT page-worthy."""
        state = str(tmp_path / "recover.json")
        rec = Recorder()
        notifs = []
        gate, _ = _ghost_gate(self._dead("t_ghost"))
        results = None
        for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS):
            results = dw.recover_dispatcher_wedge(
                stream_state={"ok": False, "declared": True,
                              "spawnable": ["default"], "total_running": 2},
                restart_fn=rec, now=1000.0 + k * 60, state_file=state,
                board_gate_fn=gate, notify_fn=notifs.append)
        assert rec.calls == 0
        assert len(notifs) == 1
        assert results is not None
        assert "t_ghost" in notifs[0] and "ghost-bot" in notifs[0]
        assert "t_cap" not in notifs[0], notifs[0]
        assert results["reason"] == "nonspawnable_assignee"
        assert results["nonspawnable"] == self._dead("t_ghost")

    def test_lookup_exception_on_the_restart_path_keeps_the_restart(self,
                                                                     tmp_path):
        """Fail-closed end-to-end: a gate whose profile lookup cannot decide
        reports no findings and cannot explain the board, so the restart
        proceeds (the pre-fix behaviour, unchanged) and nothing notifies."""
        def gate(stream):
            # Exactly what the real gate returns when every card needs hold
            # evidence and none exists — no findings, not explained.
            return dw.GateVerdict(False, "unexplained ready cards: t_ghost")
        rec = Recorder()
        notifs = []
        state = str(tmp_path / "recover.json")
        results = []
        for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS + 1):
            results.append(dw.recover_dispatcher_wedge(
                stream_state={"ok": False, "declared": True,
                              "spawnable": ["default"], "total_running": 0},
                restart_fn=rec, now=1000.0 + k * 60, state_file=state,
                board_gate_fn=gate, notify_fn=notifs.append))
        assert rec.calls == 1, "a lookup that cannot decide must not silence us"
        assert any(r["result"] == "recovered" for r in results), results
        assert notifs == []

    def test_throttle_applies_to_the_config_typo_class_too(self, tmp_path):
        """3 h of 60 s ticks against a sustained typo: ONE notify per hour, not
        one per minute — the existing 1/h throttle is the mechanism."""
        state = str(tmp_path / "recover.json")
        rec = Recorder()
        notifs = []
        gate, _ = _ghost_gate(self._dead())
        t = 1000.0
        for _ in range(int(3 * 3600 / 60)):
            dw.recover_dispatcher_wedge(
                stream_state={"ok": False, "declared": True,
                              "spawnable": ["default"], "total_running": 0},
                restart_fn=rec, now=t, state_file=state,
                board_gate_fn=gate, notify_fn=notifs.append)
            t += 60
        assert rec.calls == 0
        assert 1 <= len(notifs) <= 4, (
            f"expected ~1 notify/hour over 3h, got {len(notifs)}")

    def test_message_class_is_config_typo_not_a_wedge(self):
        msg = dw._nonspawnable_message(
            self._dead("t_ab", "hscc", "backend-enginer"))
        assert "ready card t_ab on board hscc" in msg
        assert "backend-enginer" in msg
        assert "config typo" in msg
        assert "NO gateway restart was attempted" in msg

    def test_message_names_every_card_when_there_are_several(self):
        dead = self._dead("t_one") + self._dead("t_two", board="other")
        msg = dw._nonspawnable_message(dead)
        assert "t_one" in msg and "t_two" in msg and "other" in msg
        assert "no worker will ever run them" in msg

    def test_end_to_end_default_gate_and_recovery_path_notifies(self, tmp_path,
                                                                monkeypatch):
        """The real gate + the real recovery, with only the EXTERNAL seams
        injected: red stream → ``board_state_explains_stall`` (sanctioned CLI
        fake) → non-spawnable findings → ``recover_dispatcher_wedge`` suppresses
        the restart AND notifies.

        This is the call-site pin: the recovery calls ``gate_fn(stream)`` with
        no arguments of its own, so a delegating wrapper is the only way to
        reach the real gate with fakes (and the only way to prove the recovery
        actually consults the findings — flip that branch off and this goes
        red). It never touches the real CLI or a live board.
        """
        monkeypatch.setattr(dw, "_hermes_cli_argv", lambda: ["hermes", "kanban"])
        monkeypatch.setattr(dw, "_read_max_per_profile", lambda: None)
        monkeypatch.setattr(dw, "_read_max_in_progress", lambda: None)
        monkeypatch.setattr(dw, "_assignee_is_nonspawnable",
                            lambda assignee: assignee == "ghost-bot")
        kb = FakeKanban({"default": [REVIEW_REAL]})   # board IS spawnable
        cli = FakeCli({("list", "--status", "ready"):
                       [_ready_row("t_ghost", assignee="ghost-bot")]})

        def gate(stream):
            return dw.board_state_explains_stall(
                stream, kanban_db=kb, max_in_progress=6, max_per_profile=None,
                cli_fn=cli, now=1_800_000_000.0)

        rec = Recorder()
        notifs = []
        state = str(tmp_path / "recover.json")
        results = []
        for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS + 1):
            results.append(dw.recover_dispatcher_wedge(
                stream_state={"ok": False, "declared": True,
                              "spawnable": ["default"], "total_running": 0},
                restart_fn=rec, now=1000.0 + k * 60, state_file=state,
                board_gate_fn=gate, notify_fn=notifs.append))
        assert rec.calls == 0, f"restart must stay suppressed: {results}"
        assert len(notifs) == 1, notifs
        assert "t_ghost" in notifs[0] and "ghost-bot" in notifs[0]
        assert any(r.get("reason") == "nonspawnable_assignee" for r in results)
        # The gate really did read the board through the sanctioned CLI.
        assert any("list" in c and "ready" in c for c in cli.calls)


class TestNonspawnableNotifyBranchIsLive:
    """Mutation sentinels for the exact PR #43 regression: 'explained' used to
    mean 'say nothing'. Each test fails if the distinct notify branch is
    flipped off, and passes for the WRONG reason if the message is not
    distinct — so both directions are pinned."""

    def _dead(self, card="t_ghost", assignee="ghost-bot"):
        return [{"board": "default", "card": card, "assignee": assignee}]

    def test_generic_hold_message_is_not_reused_for_the_typo_class(self,
                                                                   tmp_path):
        """A typo'd assignee must not be reported as 'the dispatcher is only
        HOLDING work back' — that phrasing tells the operator to wait, when in
        fact the card needs its assignee fixed."""
        state = str(tmp_path / "recover.json")
        notifs = []
        gate, _ = _ghost_gate(self._dead())
        for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS):
            dw.recover_dispatcher_wedge(
                stream_state={"ok": False, "declared": True,
                              "spawnable": ["default"], "total_running": 0},
                restart_fn=Recorder(), now=1000.0 + k * 60, state_file=state,
                board_gate_fn=gate, notify_fn=notifs.append)
        assert len(notifs) == 1
        assert "HOLDING work back" not in notifs[0], notifs[0]
        assert "no worker will ever run it" in notifs[0]

    def test_findings_reaching_the_gate_without_a_notify_would_be_a_bug(self,
                                                                        tmp_path):
        """Pin the reason field the operator/trigger rules key on: the two
        classes must stay separable in the recovery result, not collapse into
        one 'benign_dispatcher_hold' bucket."""
        hold_only = lambda s: (True, "held: active_pr=1")   # plain 2-tuple
        typo = _ghost_gate(self._dead("t_typo"))[0]
        outs = []
        for gate in (hold_only, typo):
            rec_i = Recorder()
            notifs = []
            # The notify throttle is process-global (one 1/h ping for the
            # whole watchdog, by design) — reset it so the two classes are
            # exercised independently, as they would be in separate incidents.
            dw._hold_notify_at = None
            state = str(tmp_path / f"recover-{id(gate)}.json")
            for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS):
                outs.append(dw.recover_dispatcher_wedge(
                    stream_state={"ok": False, "declared": True,
                                  "spawnable": ["default"], "total_running": 0},
                    restart_fn=rec_i, now=1000.0 + k * 60, state_file=state,
                    board_gate_fn=gate, notify_fn=notifs.append))
            assert rec_i.calls == 0 and len(notifs) == 1, (
                f"each class must notify exactly once: {notifs}")
        reasons = {o.get("reason") for o in outs if o["result"] == "suppressed"}
        assert reasons == {"benign_dispatcher_hold", "nonspawnable_assignee"}
        typo_results = [o for o in outs
                        if o.get("reason") == "nonspawnable_assignee"]
        assert typo_results and typo_results[0]["nonspawnable"] == self._dead("t_typo")

    def test_nonspawnable_notify_uses_the_same_channel(self, monkeypatch):
        """Requirement 3: no second notify path — the config-typo class goes
        through the same desktop sender as the hold class, only titled
        differently."""
        sent = []
        import hscc_daemon.desktop as desktop
        monkeypatch.setattr(desktop, "send_macos_notification",
                            lambda t, m, priority="normal": sent.append((t, m)))
        dw._notify_nonspawnable("boom")
        dw._notify_hold("quiet")
        assert [t for t, _m in sent] == [
            "HSCC Dispatcher: unrunnable ready card",
            "HSCC Dispatcher Hold (no restart)"]
        assert [m for _t, m in sent] == ["boom", "quiet"]

    def test_recovery_routes_the_default_notify_by_class(self, tmp_path,
                                                        monkeypatch):
        """Production wiring (``notify_fn`` omitted): the typo class must reach
        the desktop sender with the distinct title, NOT a second channel."""
        sent = []
        import hscc_daemon.desktop as desktop
        monkeypatch.setattr(desktop, "send_macos_notification",
                            lambda t, m, priority="normal": sent.append((t, m)))
        state = str(tmp_path / "recover.json")
        rec = Recorder()
        gate, _ = _ghost_gate(self._dead())
        for k in range(dw.DISPATCHER_RESTART_AFTER_DETECTIONS):
            dw.recover_dispatcher_wedge(
                stream_state={"ok": False, "declared": True,
                              "spawnable": ["default"], "total_running": 0},
                restart_fn=rec, now=1000.0 + k * 60, state_file=state,
                board_gate_fn=gate)
        assert rec.calls == 0
        assert len(sent) == 1 and sent[0][0] == "HSCC Dispatcher: unrunnable ready card"
        assert "ghost-bot" in sent[0][1]
