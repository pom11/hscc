"""t_1244b3b4 — `cluster down` output: benign "already down" + stay-down line.

D6: for a `down` verb, "nothing was running" is a SUCCESS. sparkrun exits 1 on
several already-down paths, so the operator saw `ERROR` on a run that had just
printed `Stopped 4 job(s), 4 container(s) across 4 host(s).`. The fix is
RENDER-ONLY: `success` keeps meaning "sparkrun exited 0" (the daemon, scripts
and the iOS console dump this dict raw via `_emit_result`/`--json`) and the
verdict rides in the ADDITIVE `benign_no_workloads` / `benign_note` keys.

D1 (t_ab75e987): a real (non-dry-run) down now also SAYS what it claimed —
one confirming stay-down line after the marker clear succeeded.

Pins (fakes only — `run_cmd` is always a fake, never a subprocess):
  1. rc 0 + `Stopped 4 job(s)…`  -> OK, no benign key;
  2. nonzero + an already-down shape (`No sparkrun containers running.` and,
     separately, `No running workload matches intent …`) -> OK + the note, and
     the dict still reports success False with NO key lost from the pre-change
     shape (the `--json` contract);
  3. nonzero + `Error: failed to stop containers on 10.0.0.x: ssh: Permission
     denied` -> ERROR (a real failure is never softened);
  4. a real down calls `clear_expected_up` EXACTLY once and prints the
     stay-down line (also when the stop was benign); `--dry-run` does neither.

Address doctrine: sanctioned 10.0.0.x placeholder range only.
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hscc_daemon import post_boot  # noqa: E402

# The existing cluster-down harness, kept as the single source of truth. It is
# bound to a leading-underscore name so pytest does not re-collect the imported
# class from this module (its 4 tests already run in their own file).
from test_cluster_up_post_boot_hooks import (  # noqa: E402
    TestDownClearsMarker as _DownHarness,
)


def _wire_down(monkeypatch):
    """(module, calls) with run_cmd + serving.fleet_down_cmd faked."""
    return _DownHarness._wire_down(None, monkeypatch)

# What one real (non-dry-run) `cluster down` reported BEFORE this card: the
# `_run_or_print` shape. The benign path may only ADD keys to it.
_PREVIOUS_KEYS = {"success", "returncode", "command", "output", "error"}

STOPPED_OK = "Stopped 4 job(s), 4 container(s) across 4 host(s)."
ALREADY_DOWN_NONE = "No sparkrun containers running."
ALREADY_DOWN_INTENT = ("Error: failed to stop containers on 10.0.0.247: "
                       "No running workload matches intent "
                       "sparkrun_7f2a on hosts ['10.0.0.247']")
REAL_FAILURE = ("Error: failed to stop containers on 10.0.0.248: "
                "ssh: Permission denied (publickey)")


class TestDownOutputRendering:
    """The renderer half of D6, driven by the same `_wire_down` harness."""

    def _sink(self, monkeypatch, mod, returncode, output, error=""):
        def sink(cmd, timeout=30, as_json=False):
            return {"success": returncode == 0, "returncode": returncode,
                    "output": output, "error": error}
        monkeypatch.setattr(mod, "run_cmd", sink)

    def _down(self, monkeypatch, returncode, output, error=""):
        mod, calls = _wire_down(monkeypatch)
        self._sink(monkeypatch, mod, returncode, output, error)
        res = mod.cmd_cluster_down(dry_run=False)
        return mod, res, calls

    # ── 1. the clean stop renders OK ──────────────────────────────────────
    def test_clean_stop_renders_ok(self, monkeypatch):
        mod, res, _ = self._down(monkeypatch, 0, STOPPED_OK)
        assert res["success"] is True
        text = "\n".join(mod._human_lines("down", res))
        assert "[ok]OK[/]" in text
        assert "ERROR" not in text

    # ── 2. the two already-down shapes render OK + the note ──────────────
    @pytest.mark.parametrize("shape,where", [
        (ALREADY_DOWN_NONE, "output"),
        (ALREADY_DOWN_INTENT, "error"),
    ])
    def test_already_down_renders_ok_with_note(self, monkeypatch, shape,
                                               where):
        mod, res, _ = self._down(monkeypatch, 1, shape if where == "output"
                                 else STOPPED_OK,
                                 "" if where == "output" else shape)
        # the machine contract is untouched...
        assert res["success"] is False
        assert res["returncode"] == 1
        # ...and the verdict is ADDITIVE: every key the dict had before is
        # still there (nothing lost for the --json consumers).
        assert _PREVIOUS_KEYS <= set(res)
        assert res["benign_no_workloads"] is True
        assert res["benign_note"]

        text = "\n".join(mod._human_lines("down", res))
        assert "[ok]OK[/]" in text
        assert "ERROR" not in text
        assert "already down" in text

    def test_no_key_lost_against_the_unchanged_run_or_print_shape(
            self, monkeypatch):
        """The strongest form of the --json pin: compare against the same
        command run through `_run_or_print` itself, so a rewrite that dropped
        `error`/`returncode` would fail here."""
        mod, _calls = _wire_down(monkeypatch)
        self._sink(monkeypatch, mod, 1, ALREADY_DOWN_NONE)
        # the SAME command the verb itself would issue, so only the dict
        # shape is under test here, not the argv
        cmd = mod._load_daemon_serving().fleet_down_cmd()
        baseline = mod._run_or_print(cmd, dry_run=False)
        res = mod.cmd_cluster_down(dry_run=False)
        assert set(baseline) <= set(res)
        assert res["success"] == baseline["success"]
        assert res["returncode"] == baseline["returncode"]
        assert res["output"] == baseline["output"]
        assert res["error"] == baseline["error"]
        assert res["command"] == baseline["command"]

    # ── 3. a real failure is never softened ──────────────────────────────
    def test_ssh_failure_still_renders_error(self, monkeypatch):
        mod, res, _ = self._down(monkeypatch, 1, STOPPED_OK, REAL_FAILURE)
        assert res["success"] is False
        assert "benign_no_workloads" not in res
        text = "\n".join(mod._human_lines("down", res))
        assert "ERROR" in text
        assert "[ok]OK[/]" not in text

    def test_mixed_stopped_and_unqueryable_host_still_error(self, monkeypatch):
        """"Nothing found on the hosts that could be queried" is benign ONLY
        when nothing real rode along (an unqueryable host may be running
        containers we never saw)."""
        mod, res, _ = self._down(
            monkeypatch, 1,
            "No sparkrun containers found on the hosts that could be queried.",
            "Error: could not query 10.0.0.246: ssh: Permission denied")
        assert "benign_no_workloads" not in res
        assert "ERROR" in "\n".join(mod._human_lines("down", res))

    def test_unknown_nonzero_output_stays_error(self, monkeypatch):
        """No known already-down marker => the exit code still rules."""
        mod, res, _ = self._down(monkeypatch, 1,
                                 "some future sparkrun wording we do not know")
        assert "benign_no_workloads" not in res
        assert "ERROR" in "\n".join(mod._human_lines("down", res))

    def test_benign_keys_never_appear_for_a_nonzero_up(self, monkeypatch):
        """The renderer honours the keys, it does not invent them: an `up`
        result without them still renders ERROR."""
        mod, _calls = _wire_down(monkeypatch)
        text = "\n".join(mod._human_lines("up", {"success": False,
                                                 "output": "boom"}))
        assert "ERROR" in text


class TestStayDownConfirmation:
    """D1: a real down SAYS it claimed the stay-down; --dry-run says nothing."""

    def _counting_clear(self, monkeypatch):
        seen = []

        def fake_clear(reason, path=None):
            seen.append(reason)
        monkeypatch.setattr(post_boot, "clear_expected_up", fake_clear)
        return seen

    def test_real_down_prints_stay_down_once(self, monkeypatch, capsys):
        mod, calls = _wire_down(monkeypatch)
        seen = self._counting_clear(monkeypatch)
        mod.cmd_cluster_down(dry_run=False)
        assert seen == ["hscc cluster down"]          # exactly once
        assert calls                                   # stop was issued
        out = capsys.readouterr().out
        assert "fleet marked deliberately down" in out
        assert "hscc cluster up" in out

    def test_stay_down_printed_even_when_benign(self, monkeypatch, capsys):
        """The stay-down is claimed even when nothing needed stopping —
        that is already the documented intent of the verb."""
        mod, calls = _wire_down(monkeypatch)

        def sink(cmd, timeout=30, as_json=False):
            calls.append(list(cmd))
            return {"success": False, "returncode": 1,
                    "output": ALREADY_DOWN_NONE, "error": ""}
        monkeypatch.setattr(mod, "run_cmd", sink)
        seen = self._counting_clear(monkeypatch)
        res = mod.cmd_cluster_down(dry_run=False)
        assert res["success"] is False
        assert res["benign_no_workloads"] is True
        assert seen == ["hscc cluster down"]
        assert "fleet marked deliberately down" in capsys.readouterr().out

    def test_dry_run_prints_nothing_extra_and_clears_nothing(
            self, monkeypatch, capsys):
        mod, calls = _wire_down(monkeypatch)
        seen = self._counting_clear(monkeypatch)
        mod.cmd_cluster_down(dry_run=True)
        assert calls == []
        assert seen == []
        out = capsys.readouterr().out
        assert "fleet marked deliberately down" not in out
        assert "not executing" in out                 # the pre-existing line

    def test_warn_path_prints_no_stay_down_claim(self, monkeypatch, capsys):
        """If the clear FAILED the operator must not be told the fleet is
        marked down — the WARN stays, the confirmation does not appear."""
        mod, calls = _wire_down(monkeypatch)

        def boom(reason, path=None):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(post_boot, "clear_expected_up", boom)
        res = mod.cmd_cluster_down(dry_run=False)
        out = capsys.readouterr().out
        assert "WARN: expected_up marker clear failed" in out
        assert "fleet marked deliberately down" not in out
        assert res["success"] is True                  # the stop itself worked

    def test_stay_down_line_is_the_documented_wording(self, monkeypatch):
        mod, _calls = _wire_down(monkeypatch)
        assert "deliberately down" in mod.STAY_DOWN_CONFIRMATION
        assert "the daemon will not relaunch" in mod.STAY_DOWN_CONFIRMATION
