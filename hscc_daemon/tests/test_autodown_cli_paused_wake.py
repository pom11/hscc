"""``hscc autodown wake`` against a fully parked fleet (t_411401a0).

THE SEAM THIS FILE OWNS
----------------------
``hscc_daemon/autodown.py`` reports ``result: "all-paused"`` when EVERY unit of
the wake plan is held by ``node_pauses``: no start is issued, no state moves,
the pre-wake posture (down, block latched, watchdog holding) is exactly what a
parked fleet asks for (t_af9901e2, §1c). ``test_autodown_paused_wake.py`` pins
that producer side. This file pins the CONSUMER side — the operator-facing CLI
verb that reads the same dict and decides what to print and what to exit with.

WHY THE EXIT CODE IS THE FINDING
--------------------------------
``_cmd_wake`` had no case for ``all-paused``, so it fell to the catch-all that
prints ``wake did NOT complete (all-paused)`` and returns 1. That is a false
report in both directions:

* nothing failed. The operator parked the nodes themselves and the wake honoured
  that — the same reasoning that made ``hscc-cluster/hscc.py::cmd_cluster_up``
  report ``result: "all-paused"`` with ``success: True`` (t_3b909c61 / #576):
  "there was nothing to do, which is not an error";
* exit 1 is not just cosmetic wording — it is what a script branches on, so a
  wrapper around ``hscc autodown wake`` (a waker, a cron step, a CI gate) treats
  the operator's own pause as a wake failure and pages about it.

The daemon is unaffected either way — ``autodown.cycle()``'s ``_invoke_autoup``
ignores the result dict — so this is the manual CLI's messaging and exit code,
and that is exactly what these tests hold.

THE BOTH-DIRECTIONS RULE
------------------------
A fix that only makes ``all-paused`` green proves nothing: the honest shape is
``all-paused`` succeeds AND every genuinely-failing outcome still fails. Every
other outcome label ``autoup()`` can return is pinned HERE too, at the exit code
and the wording it must keep, so this file fails if the new branch is widened
into a blanket "wake always succeeds".
"""

import json

import pytest

import hscc_daemon.autodown as ad
from hscc_daemon import lifecycle
from hscc_daemon.autodown_cli import cmd_autodown


# ---------------------------------------------------------------------------
# Fixtures — same sealing as test_autodown_cli.py: tmp config + tmp watchdog
# block + no notifications. ``autoup`` is ALWAYS faked: a real one would issue
# real ``sparkrun run --ensure`` starts against the live fleet.
# ---------------------------------------------------------------------------

@pytest.fixture
def autodown_file(tmp_path, monkeypatch):
    path = tmp_path / "hscc" / "autodown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
    return path


@pytest.fixture
def block_file(tmp_path, monkeypatch):
    path = str(tmp_path / "hscc" / "watchdog-block.json")
    monkeypatch.setattr(lifecycle, "WATCHDOG_BLOCK_FILE", path)
    return path


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(ad, "send_macos_notification", lambda *a, **k: True)


def _wake(monkeypatch, capsys, result, **extra):
    """Run ``hscc autodown wake`` with autoup faked to ``result``.

    The payload mirrors the real shape (``started`` / ``ready`` / ``paused``)
    so a test cannot pass by accident against a key the verb never returns.
    """
    def fake(**kwargs):
        payload = {"result": result, "started": [], "ready": [], "paused": []}
        payload.update(extra)
        return payload

    monkeypatch.setattr(ad, "autoup", fake)
    rc = cmd_autodown(["wake"])
    return rc, capsys.readouterr().out


class TestAllPausedIsNotAFailure:
    """The whole parked fleet is a no-op the operator asked for."""

    def test_the_verb_exits_zero_and_names_the_parked_units(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "all-paused",
                        paused=["wk-keep", "wk1"])
        assert rc == 0, (
            "a wake that started nothing because the OPERATOR parked every unit "
            "must not exit as a failure — a script branches on this")
        assert "all-paused" in out, "the outcome label must stay greppable"
        # Naming WHICH units, so the operator is not left guessing which park
        # is holding the fleet (same reason cmd_cluster_up prints each skip).
        assert "wk-keep" in out and "wk1" in out

    def test_the_message_points_at_the_resume_path(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "all-paused",
                        paused=["wk1"])
        assert rc == 0
        # The ONLY resume seam is the overlay's own verb (docs/ops/node-pause.md
        # — there is deliberately no --include-paused), so the message must
        # route the operator there rather than invent a second path.
        assert "hscc node resume" in out
        assert "hscc node paused" in out, "where to find the holding node"

    def test_no_wording_reads_as_a_failure(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "all-paused",
                        paused=["wk1"])
        assert rc == 0
        # The old shape was "wake did NOT complete (all-paused)". Pinning the
        # absence is what stops a rewording from restoring the lie.
        for lie in ("did NOT complete", "did not complete", "FAILED", "failed",
                    "Error", "error"):
            assert lie not in out, f"parked-fleet wake must not read as {lie!r}"

    def test_it_asks_for_no_start_and_changes_nothing_it_should_not(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        # autoup owns the no-op decision; the CLI must not retry, must not
        # clear anything, and must not re-print a wake-completed panel.
        rc, out = _wake(monkeypatch, capsys, "all-paused",
                        paused=["wk1"])
        assert rc == 0
        assert "wake complete" not in out
        assert not ad.load_config().get("cancel_requested")

    def test_json_reports_the_outcome_and_the_parked_units(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        monkeypatch.setattr(ad, "autoup", lambda **k: {
            "result": "all-paused", "started": [], "ready": [],
            "paused": ["wk-keep", "wk1"]})
        rc = cmd_autodown(["wake", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0, "same verdict on the machine path as on the human one"
        assert data["result"] == "all-paused"
        assert data["paused"] == ["wk-keep", "wk1"], (
            "a script cannot name the parked units without them")
        # A wake that started nothing is NOT waking: claiming otherwise makes
        # every stay-down consumer read a wake in flight that does not exist.
        assert data["state"] != "waking"

    def test_a_parenthesis_in_a_unit_id_cannot_break_the_panel(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        # Unit ids come from serving.json — operator DATA, and the panel body is
        # markup-parsed (t_716cf37c). An unescaped id would eat the row.
        rc, out = _wake(monkeypatch, capsys, "all-paused",
                        paused=["wk[bold]1"])
        assert rc == 0
        assert "wk[bold]1" in out


class TestEveryOtherOutcomeStillMeansWhatItMeant:
    """The other six labels ``autoup()`` returns keep their exit code.

    Without these the previous class is satisfiable by making ``wake`` always
    exit 0 — which would be the same bug wearing the opposite hat: a genuinely
    failed wake that scripts cannot detect.
    """

    def test_up_is_a_success_and_says_so(self, monkeypatch, capsys,
                                         autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "up")
        assert rc == 0
        assert "UP" in out

    def test_already_waking_is_a_no_op_but_not_a_failure(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "already-waking")
        assert rc == 0
        assert "already in" in out

    def test_busy_still_exits_non_zero(self, monkeypatch, capsys,
                                       autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "busy")
        assert rc == 1
        assert "in progress" in out

    def test_no_units_still_exits_non_zero(self, monkeypatch, capsys,
                                           autodown_file, block_file, quiet):
        # The empty-plan label is the CONFIG failure all-paused must not be
        # confused with (autodown.py §1a: state "error", watchdog released).
        rc, out = _wake(monkeypatch, capsys, "no-units")
        assert rc == 1
        assert "empty wake plan" in out

    def test_not_ready_still_exits_non_zero(self, monkeypatch, capsys,
                                            autodown_file, block_file, quiet):
        rc, out = _wake(monkeypatch, capsys, "not-ready",
                        reason="readiness timed out")
        assert rc == 1
        assert "NOT complete" in out
        assert "not-ready" in out

    def test_start_failed_still_exits_non_zero(self, monkeypatch, capsys,
                                               autodown_file, block_file,
                                               quiet):
        rc, out = _wake(monkeypatch, capsys, "start-failed",
                        reason="launch refused by guard")
        assert rc == 1
        assert "NOT complete" in out

    def test_an_unknown_label_fails_closed(self, monkeypatch, capsys,
                                           autodown_file, block_file, quiet):
        # A label this CLI has never seen must NOT be folded into the success
        # path: the catch-all stays a failure.
        rc, out = _wake(monkeypatch, capsys, "something-new")
        assert rc == 1
        assert "NOT complete" in out

    def test_the_machine_path_keeps_its_shape_for_the_other_labels(
            self, monkeypatch, capsys, autodown_file, block_file, quiet):
        # --json has always reported the label and exited 0 for every result.
        # Pinned (not endorsed) so changing it is a deliberate, tested act
        # rather than a side effect of this card's all-paused branch.
        for label in ("up", "busy", "no-units", "all-paused"):
            monkeypatch.setattr(ad, "autoup",
                                lambda **k: {"result": label, "started": [],
                                             "ready": [], "paused": []})
            rc = cmd_autodown(["wake", "--json"])
            data = json.loads(capsys.readouterr().out)
            assert rc == 0
            assert data["result"] == label
