"""The proxy check must STATE its verdict, not just announce that it ran (t_e26aaa56).

WHY THIS EXISTS
---------------
During the 2026-11-22 :4000 outage `check_proxy` logged `Running proxy check`
every 60 s and **nothing else** for ~80 ticks
(`docs/audits/2026-11-22-proxy-connection-error.md` §4.1). Reconstructing the
incident therefore required proving a *negative*: filtering ~11k proxy lines and
arguing that the absence of a complaint meant the check had stayed green for 45
minutes. A tick line proves the LOOP ran; it says nothing about what the loop
CONCLUDED. An incident report should not have to lean on an argument from
absence — and that reasoning nearly produced the wrong conclusion there.

WHAT THE FIX PROMISES (and this file pins)
------------------------------------------
`_proxy_verdict_tick` logs the verdict the check ALREADY computes, at the three
moments it is worth reading:

  * a **transition** — GREEN -> UNHEALTHY and UNHEALTHY -> GREEN, once each,
    naming the reason text the check produced;
  * a **baseline** — the first verdict tick a process makes, labelled as an
    observation: a daemon restart must not report a transition it never
    observed, because the change may have happened while the daemon was down;
  * a **heartbeat** — every 30th verdict tick (~30 min at the 60 s cadence)
    while nothing changes, so a green streak has positive anchors.

Repeated identical ticks emit nothing, which is the property that keeps a 60 s
loop from becoming a 60 s log line.

WHAT THIS FILE DELIBERATELY DOES NOT RELAX
------------------------------------------
The check's decisions are pinned elsewhere (test_health.py's TestCheckProxy,
test_proxy_pause_drift, test_proxy_reattach_no_stop). Here the return values and
the pre-existing lines are asserted UNCHANGED — pin 6 — because this change is
logging only: no new verdict, no new probe, no new remediation.

SAFETY (the no-live rule)
-------------------------
Every host is TEST-NET-1 (RFC 5737 — 192.0.2.0/24). The probes, the serving
layer, the router-drift read, the unit-alias read, the operator-down gate and
the relaunch cooldown are all stubbed, and `run_cmd`/`ssh_cmd` **raise**, so no
test here can reach a real `:4000`, a real `sparkrun`, `ssh` or the operator's
`~/.hscc` (state goes through `state.STATE_DIR`, redirected to the tmp dir). A
restart is simulated by `health.reset_proxy_verdict_memory()` with the state
file left on disk — exactly what a daemon restart is: process memory gone,
durable state kept.
"""

import json

import pytest

from hscc_daemon import health
from hscc_daemon import serving
from hscc_daemon import state as state_mod

# TEST-NET-1 (RFC 5737). One keep-alive worker; its alias is what a green
# catalogue must contain.
NODE = "192.0.2.11"
ALIAS = "worker-model"
SERVING = {"version": 2, "units": [
    {"id": "w1", "role": "worker", "keepalive": True,
     "nodes": [NODE], "port": 8001},
]}

VERDICT_PREFIX = "proxy verdict: "


class _Harness:
    """check_proxy with every seam faked, its log lines captured.

    One instance = one "daemon process". :meth:`restart` drops the process-local
    memory and the captured lines but keeps the state file, which is what makes
    the restart pins honest rather than tautological.
    """

    def __init__(self, tmp_path, monkeypatch, heartbeat_every=None):
        self._monkeypatch = monkeypatch
        state_dir = tmp_path / "hscc" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(state_mod, "STATE_DIR", str(state_dir))
        self.state_file = state_dir / "proxy.json"
        self.logs = []
        monkeypatch.setattr(
            health, "log",
            lambda msg, level="INFO", **kw: self.logs.append((level, msg)))
        if heartbeat_every is not None:
            monkeypatch.setattr(health, "PROXY_VERDICT_HEARTBEAT_TICKS",
                                heartbeat_every)
        health.reset_proxy_verdict_memory()

        # ── the serving layer and every probe: stubbed ──
        monkeypatch.setattr(serving, "KEEPALIVE_NODES", {NODE})
        monkeypatch.setattr(serving, "load_serving", lambda *a, **k: SERVING)
        monkeypatch.setattr(health, "_router_drift_probe",
                            lambda: {"status": "skip", "warn": False,
                                     "detail": "stubbed"})
        monkeypatch.setattr(health, "_unit_alias_check_fn",
                            lambda serving_data=None: {
                                "ok": True, "findings": [], "unverified": [],
                                "message": "stubbed"})
        # The F4 backend probe "could not ask the proxy" — the documented
        # fail-open (drift check skipped), so the verdict is the catalogue's.
        monkeypatch.setattr(
            health, "_proxy_backend_probe",
            lambda url=None, timeout=5: {"ok": False, "backends": set(),
                                         "error": "stubbed — drift check skipped"})
        self.probe = {"ok": True, "models": [ALIAS], "error": ""}
        monkeypatch.setattr(health, "_proxy_models_probe",
                            lambda url, timeout=5: dict(self.probe))
        # t_f0b8051b added a through-proxy completion probe to check_proxy's
        # healthy path. This module is about VERDICT LOGGING, not the completion
        # verdict, so it is neutralised to the probe's no-evidence (healthy)
        # shape — the same injection seam every other check_proxy rig uses — so
        # a green tick here never dials the operator's live :4000 (which a dev
        # machine answers and a CI runner does not, making the healthy-path pins
        # pass here and fail there). The real probe is covered in test_health.py.
        monkeypatch.setattr(health, "_proxy_completion_probe",
                            lambda url=None, model=None, timeout=None:
                            {"ok": True, "status": None, "skipped": True,
                             "error": ""})

        # ── the repair machinery: never reached, and loud if it is ──
        def _no_command(*a, **k):
            raise AssertionError("no external command may run in these tests")

        monkeypatch.setattr(health, "run_cmd", _no_command)
        monkeypatch.setattr(health, "ssh_cmd", _no_command)
        monkeypatch.setattr(health, "send_macos_notification",
                            lambda *a, **k: None)
        monkeypatch.setattr(health, "emit_event", lambda *a, **k: None)
        monkeypatch.setattr(health, "_operator_down", lambda: None)
        monkeypatch.setattr(health, "_intentional_window", lambda: False)
        # Unhealthy ticks stay in the cooldown-suppressed branch — the shape an
        # outage actually spends its minutes in (a relaunch attempt every 5 min,
        # a probe every 60 s) — so no lease, breaker or command is involved.
        monkeypatch.setattr(health, "_proxy_relaunch_cooldown_state",
                            lambda: 1.0)
        monkeypatch.setattr(health, "_proxy_relaunch_window", lambda: 300)

    # ── driving ─────────────────────────────────────────────────────────────

    def tick(self, green=True):
        """One check tick. `green` flips the catalogue before the probe runs."""
        self.probe = {"ok": True, "models": [ALIAS] if green else ["other-model"],
                      "error": ""}
        return health.check_proxy()

    def ticks(self, n, green=True):
        return [self.tick(green=green) for _ in range(n)]

    def restart(self):
        """What a daemon restart is: process memory gone, state file kept."""
        health.reset_proxy_verdict_memory()
        self.logs = []

    def set_operator_down(self, reason="manual cluster down"):
        self._monkeypatch.setattr(health, "_operator_down",
                                  lambda: {"reason": reason})

    def clear_operator_down(self):
        self._monkeypatch.setattr(health, "_operator_down", lambda: None)

    # ── reading what it said ────────────────────────────────────────────────

    def verdicts(self):
        """(level, text) for every verdict line, in order."""
        return [(lvl, msg) for lvl, msg in self.logs if msg.startswith(VERDICT_PREFIX)]

    def state(self):
        return json.loads(self.state_file.read_text())

    def verdict_block(self):
        return self.state().get(health.PROXY_VERDICT_STATE_KEY)


@pytest.fixture
def hp(tmp_path, monkeypatch):
    return _Harness(tmp_path, monkeypatch)


# ── the two pins the card requires ──────────────────────────────────────────

def test_green_to_unhealthy_to_green_logs_each_transition_once(hp):
    """The card's pin 1: a green→unhealthy→green sequence produces EXACTLY the
    transition lines, and repeated identical ticks duplicate none of them.

    Before this change the same sequence logged `Running proxy check` x6 and one
    per-tick unhealthy complaint, and the only evidence the proxy had come BACK
    was the absence of further complaints.
    """
    hp.tick(green=True)            # tick 1: baseline (observation, not transition)
    hp.tick(green=False)           # tick 2: GREEN -> UNHEALTHY
    hp.ticks(3, green=False)       # ticks 3-5: the outage drags on, silently
    hp.tick(green=True)            # tick 6: UNHEALTHY -> GREEN
    hp.ticks(2, green=True)        # ticks 7-8: recovery holds, silently

    assert hp.verdicts() == [
        ("INFO", "proxy verdict: GREEN | baseline (first proxy verdict recorded) "
                 f"| reason: expected aliases ['{ALIAS}'] all served"),
        ("WARN", "proxy verdict: GREEN -> UNHEALTHY | reason: "
                 f"missing expected alias(es) ['{ALIAS}']; served: ['other-model']"),
        ("INFO", "proxy verdict: UNHEALTHY -> GREEN | reason: "
                 f"expected aliases ['{ALIAS}'] all served"),
    ]


def test_restart_mid_green_logs_the_anchor_once_not_a_transition(hp):
    """The card's pin 2: a restart mid-green logs the anchor once, not a
    transition. The change did not happen while the daemon was watching, and
    saying `GREEN -> GREEN` would be a lie the log cannot support.
    """
    hp.tick(green=True)
    hp.tick(green=True)            # green holds; nothing to say
    hp.restart()
    assert hp.tick(green=True) is True
    assert hp.verdicts() == [
        ("INFO", "proxy verdict: GREEN | baseline (first check since daemon start) "
                 f"| reason: expected aliases ['{ALIAS}'] all served"),
    ]
    # ...and the anchor is once per process, not once per tick.
    hp.tick(green=True)
    assert len(hp.verdicts()) == 1
    assert "->" not in hp.verdicts()[0][1]


def test_a_multi_line_reason_cannot_split_the_line(hp):
    """`grep "proxy verdict:"` is only a timeline if every event is ONE line.

    No reason builder reachable today emits a newline, so this pins the helper
    directly rather than through a fake that would have to lie about the probes:
    a reason carrying newlines and runs of spaces still lands flat, and the
    stored text stays bounded.
    """
    hp.tick(green=True)                      # durable record: GREEN
    hp.logs = []
    block = health._proxy_verdict_tick(
        False, "probe failed:\n  ConnectionError   upstream\n  retry exhausted")
    assert len(hp.logs) == 1
    level, line = hp.logs[0]
    assert level == "WARN" and "\n" not in line
    assert line.startswith("proxy verdict: GREEN -> UNHEALTHY")
    assert "ConnectionError upstream retry exhausted" in line
    assert "\n" not in block["reason"]
    # And a pathological reason stays bounded in the state file.
    assert len(health._proxy_verdict_tick(True, "x" * 5000)["reason"]) \
        <= health.PROXY_VERDICT_REASON_MAX


# ── the anchor that motivated the card ──────────────────────────────────────

def test_a_long_green_streak_gets_positive_anchors_not_a_line_per_tick(hp):
    """65 ticks of the 60 s loop (~65 min) = the baseline plus a heartbeat every
    30th tick. `grep "proxy verdict:" daemon.log` answers "was it green at
    21:30?" directly, instead of requiring 65 lines to be absent.
    """
    hp.ticks(65)
    levels_text = hp.verdicts()
    assert [lvl for lvl, _ in levels_text] == ["INFO", "INFO", "INFO"]
    assert "baseline" in levels_text[0][1]
    assert "heartbeat (tick 30)" in levels_text[1][1]
    assert "heartbeat (tick 60)" in levels_text[2][1]
    assert all(t.startswith("proxy verdict: GREEN") for _, t in levels_text)
    # The point of the whole card: an anchor per tick is what we are NOT doing.
    assert len(levels_text) == 3


def test_heartbeats_are_dropped_when_the_interval_is_disabled(hp, monkeypatch):
    """`HSCC_PROXY_VERDICT_HEARTBEAT_TICKS=0` must silence anchors and nothing
    else — transitions still have to be visible."""
    monkeypatch.setattr(health, "PROXY_VERDICT_HEARTBEAT_TICKS", 0)
    hp.ticks(40)
    hp.tick(green=False)
    assert len(hp.verdicts()) == 2                     # baseline + one transition
    assert any("->" in t for _, t in hp.verdicts())


def test_a_restart_mid_outage_names_the_recorded_verdict_and_invents_nothing(hp):
    """The daemon restarted WHILE the proxy was broken. The transition to
    unhealthy happened somewhere the daemon was not watching, so it is reported
    as an observation that NAMES the last recorded verdict — the reader can see
    the gap without the log claiming a change it never saw.
    """
    hp.tick(green=True)
    hp.restart()
    assert hp.tick(green=False) is False
    assert hp.verdicts() == [
        ("WARN", "proxy verdict: UNHEALTHY | baseline (first check since daemon "
                 "start; last recorded verdict GREEN) | reason: missing expected "
                 f"alias(es) ['{ALIAS}']; served: ['other-model']"),
    ]


def test_first_ever_verdict_is_labelled_as_the_first_recorded(hp):
    """A fresh state file has no prior verdict to compare against, so the line
    must not imply one — and the block must land in the state file for the next
    tick to compare against.
    """
    assert hp.state_file.exists() is False
    assert hp.tick(green=True) is True
    assert "first proxy verdict recorded" in hp.verdicts()[0][1]
    block = hp.verdict_block()
    assert block["verdict"] == "GREEN" and block["tick"] == 1
    assert ALIAS in block["reason"]


def test_the_verdict_memory_survives_a_tick_that_makes_no_verdict(hp):
    """`write_state` REPLACES the proxy state file, so the operator-down gate —
    which has no verdict to report — must CARRY the block, not erase it.

    Without the carry, every deliberate fleet down would wipe the check's memory
    and the first tick after `cluster up` would read as "the first verdict ever
    recorded" (tick 1, no record to agree with) instead of a continuation of the
    streak the previous process actually observed.
    """
    hp.tick(green=True)
    hp.restart()                       # so the next tick is not a baseline either
    hp.set_operator_down()
    assert hp.tick(green=True) is True  # the stay-down gate returns early
    carried = hp.verdict_block()
    assert carried["verdict"] == "GREEN" and carried["tick"] == 1
    assert hp.state()["intentional"] == "operator_down"
    # A stay-down tick has no verdict to report, so it says none.
    assert hp.verdicts() == []
    # The gate ran BEFORE the verdict, so this process still has not observed one:
    # the first real tick after the down is a baseline — and the two facts that
    # prove the carry worked are its label (it AGREES with the record, so no
    # "last recorded verdict" clause) and a counter that continued at 2.
    hp.clear_operator_down()
    hp.logs = []
    assert hp.tick(green=True) is True
    assert len(hp.verdicts()) == 1
    level, line = hp.verdicts()[0]
    assert level == "INFO" and "->" not in line
    assert "first check since daemon start" in line
    assert "last recorded" not in line
    assert hp.verdict_block()["tick"] == 2
    # Continuation, not a fresh streak: the next tick has nothing to say.
    hp.logs = []
    assert hp.tick(green=True) is True
    assert hp.verdicts() == []
    assert hp.verdict_block()["tick"] == 3


# ── what must NOT have changed ──────────────────────────────────────────────

def test_logging_only_the_verdicts_and_returns_are_untouched(hp):
    """No new verdict, no new remediation, no change to what `check_proxy`
    returns — and the two lines that were already there are still there.

    `run_cmd`/`ssh_cmd` RAISE in this harness, so "nothing attempted" is proven
    by the absence of an exception rather than by an assertion about a fake.
    """
    assert hp.tick(green=True) is True
    assert "Running proxy check" in [m for _, m in hp.logs]
    assert hp.tick(green=False) is False
    assert "Running proxy check" in [m for _, m in hp.logs]
    assert any(m.startswith("Worker proxy unhealthy on :")
               for _, m in hp.logs)
    assert any("cooldown" in m for _, m in hp.logs)
    # The verdict lines are ADDITIVE: the pre-existing ones are still the only
    # other proxy lines, and none of them is a new verdict in disguise.
    assert all(m.startswith(VERDICT_PREFIX) or m.startswith("Running proxy check")
               or m.startswith("Worker proxy unhealthy") or "cooldown" in m
               or "drift check skipped" in m
               for _, m in hp.logs)


def test_the_verdict_block_is_written_on_both_paths(hp):
    """Healthy and unhealthy writes both carry the block, so a restart mid-outage
    reads the same thing a restart mid-green does (the two restart pins above
    depend on it)."""
    hp.tick(green=True)
    assert hp.verdict_block()["verdict"] == "GREEN"
    hp.tick(green=False)
    block = hp.verdict_block()
    assert block["verdict"] == "UNHEALTHY" and block["tick"] == 2
    assert "missing expected alias" in block["reason"]
    # The unhealthy write still publishes everything the readers depend on.
    state = hp.state()
    assert state["ok"] is False and state["reason"] and state["port"]
