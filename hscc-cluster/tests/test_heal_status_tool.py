"""Tests for the `heal_status` agent tool (card t_14b86753, audit D4).

D4's split is the whole contract, and these tests are pinned to it:

1. ONE STATE SOURCE. The tool calls ``heal_breaker.snapshot`` — the module the
   daemon writes and ``hscc heal-status`` reads — and that function shares its
   window/suspension arithmetic with ``heal_breaker.suspended`` (the predicate
   every heal path consults). A tool that re-derived a count from the JSON file
   would be the second source of truth audit §5 exists to prevent.
2. READ-ONLY. Nothing here may write ``~/.hscc/heal-breaker.json``, reset an
   alert, touch a node or reach the clear path. ``heal-clear`` stays CLI-only.
3. THE RESULT NAMES THE FACTS AN OPERATOR NEEDS: strikes, their ages, whether
   the node is suspended and for how long, and the clear verb the tool
   deliberately does not expose.

Hermetic: the ledger path and the clock are patched per test. ``hscc-cluster``
tests have no ``~/.hscc`` isolation of their own, and reading the breaker is
not inert enough to trust the operator's live ledger.
"""

import ast
import inspect
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import heal_tools as ht
from hscc_daemon import heal_breaker as hb

NODE = "192.0.2.11"
OTHER = "192.0.2.12"

#: Frozen clock so ages and suspensions are exact rather than wall-clock races.
NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    """Point the durable ledger at this test's tmp dir and freeze the clock.

    The path is patched on the module object, not through ``expanduser``,
    because ``HEAL_BREAKER_FILE`` is baked at import time — the same seam the
    daemon suite uses (``test_heal_breaker.py::ledger``).
    """
    path = tmp_path / "heal-breaker.json"
    monkeypatch.setattr(hb, "HEAL_BREAKER_FILE", str(path))
    monkeypatch.setattr(hb, "_now", lambda: NOW)
    return path


def _seed(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ── 1. one state source ─────────────────────────────────────────────────────
class TestSameStateAsTheBreaker:
    def test_an_absent_ledger_is_an_ordinary_empty_answer(self):
        out = ht.heal_status({})
        assert out["ok"] is True
        assert out["count"] == 0 and out["nodes"] == []
        assert out["suspended_nodes"] == []
        assert out["file_age_s"] is None
        assert out["source"] == hb.HEAL_BREAKER_FILE
        # Not "healthy": the window and cap are still reported, so an agent
        # can quote the policy even when the ledger is missing.
        assert out["window_s"] == hb.HEAL_BREAKER_WINDOW_S
        assert out["max_strikes"] == hb.HEAL_BREAKER_MAX

    def test_strike_counts_match_the_breakers_own_read(self, _ledger):
        _seed(_ledger, {NODE: [NOW - 300, NOW - 100, NOW - 50]})
        out = ht.heal_status({})
        row = out["nodes"][0]
        assert row["node"] == NODE
        assert row["strikes"] == hb.strike_count(NODE)
        assert row["suspended_s"] == hb.suspended(NODE)

    def test_a_suspended_node_is_named_with_the_time_left(self, _ledger,
                                                          monkeypatch):
        monkeypatch.setattr(hb, "HEAL_BREAKER_MAX", 2)
        _seed(_ledger, {NODE: [NOW - 300, NOW - 200, NOW - 100]})
        out = ht.heal_status({})
        row = out["nodes"][0]
        assert row["suspended"] is True
        assert row["suspended_s"] == hb.suspended(NODE) > 0
        assert out["suspended_nodes"] == [NODE]
        # Suspension expires from the OLDEST in-window strike.
        assert row["oldest_age_s"] == 300
        assert row["suspended_s"] == hb.HEAL_BREAKER_WINDOW_S - 300

    def test_only_strikes_inside_the_window_count(self, _ledger):
        """The breaker measures a rate. The raw ledger keeps aged entries until
        the next strike prunes them, so the tool reports both numbers rather
        than letting a stale row read as a live strike count."""
        window = hb.HEAL_BREAKER_WINDOW_S
        _seed(_ledger, {NODE: [NOW - window - 10, NOW - 20]})
        row = ht.heal_status({})["nodes"][0]
        assert row["strikes"] == 1
        assert row["recorded"] == 2
        assert row["strike_ages_s"] == [20]

    def test_a_node_with_only_aged_strikes_stays_visible(self, _ledger):
        window = hb.HEAL_BREAKER_WINDOW_S
        _seed(_ledger, {NODE: [NOW - window - 10]})
        out = ht.heal_status({})
        assert out["count"] == 1
        row = out["nodes"][0]
        assert row["strikes"] == 0 and row["recorded"] == 1
        assert row["strike_ages_s"] == [] and row["oldest_age_s"] is None
        assert row["suspended"] is False

    def test_private_ledger_keys_are_not_nodes(self, _ledger):
        _seed(_ledger, {"_cleared": {NODE: {"at": NOW, "reason": "reviewed"}},
                        "_alerted": {NODE: NOW},
                        NODE: [NOW - 10]})
        out = ht.heal_status({})
        assert [r["node"] for r in out["nodes"]] == [NODE]

    def test_a_corrupt_entry_does_not_pin_a_node(self, _ledger):
        """``_recent`` drops the whole record on a bad timestamp; the tool must
        report that as zero in-window strikes, not as a suspension."""
        _seed(_ledger, {NODE: ["not-a-timestamp"]})
        row = ht.heal_status({})["nodes"][0]
        assert row["strikes"] == 0 and row["suspended"] is False

    def test_file_age_is_measured_from_the_ledger_mtime(self, _ledger):
        _seed(_ledger, {NODE: [NOW - 10]})
        out = ht.heal_status({})
        assert out["file_age_s"] is not None
        assert out["file_age_s"] >= 0.0

    def test_the_cli_panel_counts_the_same_in_window_strikes(self, _ledger):
        """The probe is not merely 'tool has a snapshot': the CLI verb now
        renders from the SAME snapshot, so an aged-out row cannot print as a
        live strike in one door and not the other."""
        from hscc_daemon import cli
        window = hb.HEAL_BREAKER_WINDOW_S
        _seed(_ledger, {NODE: [NOW - window - 10, NOW - 20]})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_heal(status_only=True)
        out = buf.getvalue()
        assert "1 action(s) in window" in out
        assert NODE in out and "healing allowed" in out
        assert ht.heal_status({})["nodes"][0]["strikes"] == 1


# ── 2. read-only ────────────────────────────────────────────────────────────
class TestReadOnly:
    def test_a_status_read_writes_nothing(self, _ledger):
        _seed(_ledger, {NODE: [NOW - 100, NOW - 50]})
        before = _ledger.read_bytes()
        ht.heal_status({})
        assert _ledger.read_bytes() == before

    def test_the_handler_has_no_path_to_a_mutation(self):
        """Structural pin: no clear/save/alert/nudge call may appear in the
        module. The only state door is ``snapshot``."""
        src = inspect.getsource(ht)
        tree = ast.parse(src)
        touched = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        touched |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        banned = {"clear", "reset_alert", "note_heal", "_save", "should_alert",
                  "run_cmd", "subprocess", "sparkrun", "ssh", "os"}
        assert not (touched & banned), (
            f"{sorted(touched & banned)} in heal_tools.py means a second door "
            "(or a mutation) — the handler must only ask heal_breaker.snapshot")
        assert "snapshot" in touched, (
            "the handler no longer reads the breaker's own snapshot, so it is "
            "deriving state by hand again")

    def test_the_schema_accepts_no_arguments(self):
        """A `confirm` key on a read-only tool would advertise a mutation door;
        `node` would promise a filter the ledger read does not implement."""
        assert ht.HEAL_STATUS_SCHEMA == {
            "type": "object", "properties": {}, "additionalProperties": False}
        assert "confirm" not in ht.HEAL_STATUS_SCHEMA["properties"]

    def test_arguments_the_schema_forbids_are_inert(self, _ledger):
        _seed(_ledger, {NODE: [NOW - 10]})
        before = _ledger.read_bytes()
        plain = ht.heal_status({})
        assert ht.heal_status({"node": NODE, "confirm": True}) == plain
        assert _ledger.read_bytes() == before

    def test_no_heal_clear_tool_is_registered_anywhere(self):
        """D4 closed the write door for agents. Assert it at the registry and
        the manifest, where a "just add clear" edit would land."""
        plugin = Path(inspect.getfile(ht)).parent
        init = (plugin / "__init__.py").read_text(encoding="utf-8")
        manifest = (plugin / "plugin.yaml").read_text(encoding="utf-8")
        for text in (init, manifest):
            assert "heal_clear" not in text, (
                "heal_clear is an operator mutation — CLI-only per audit D4")
        assert '"heal_status"' in init
        assert "(_READ_TOOLS + " in init, "_READ_TOOLS is not wired into register()"
        assert "  - heal_status" in manifest


# ── 3. the contract of the surface itself ──────────────────────────────────
class TestTheSurfaceContract:
    def test_the_result_names_the_clear_verb_it_does_not_expose(self, _ledger):
        _seed(_ledger, {NODE: [NOW - 10], OTHER: [NOW - 5]})
        out = ht.heal_status({})
        assert out["clear_verb"] == "hscc heal-clear <node>"
        assert out["clear_tool"] is None
        assert "operator" in out["clear_note"]
        for row in out["nodes"]:
            assert row["clear_verb"] == f"hscc heal-clear {row['node']}"

    def test_every_node_carries_its_own_age_and_strikes(self, _ledger):
        _seed(_ledger, {NODE: [NOW - 120, NOW - 60], OTHER: [NOW - 30]})
        out = ht.heal_status({})
        rows = {r["node"]: r for r in out["nodes"]}
        assert rows[NODE]["strike_ages_s"] == [60, 120]
        assert rows[NODE]["newest_age_s"] == 60
        assert rows[NODE]["oldest_age_s"] == 120
        assert rows[OTHER]["strike_ages_s"] == [30]
        assert out["count"] == 2

    def test_the_result_is_json_serialisable(self, _ledger):
        """register() wraps every handler in `_stringify`, which json.dumps-es a
        dict for the tool-role message."""
        _seed(_ledger, {NODE: [NOW - 10]})
        out = ht.heal_status({})
        assert isinstance(out, dict)
        assert isinstance(json.dumps(out, default=str), str)
