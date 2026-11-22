"""t_3b909c61 — `hscc cluster up` skips a node the operator parked.

``node_pauses`` (t_28caea71) is the operator's durable "this machine is not
mine to schedule, and nothing config-backed says so", and the wake seam
(t_af9901e2) closed the daemon's side of it. This file closes the CLI side:
every entry ``cmd_cluster_up`` issues is ``sparkrun run … --ensure``, so a
fleet up that did not ask the overlay would walk a parked unit straight back
onto the node it was just stopped from — and the post-boot relaunch and
engine-wedge recovery both route through this same verb, so they inherit it.

Pinned here, in the order the contract states them:

1. a unit with even ONE parked node is absent from ``plan``/``issued`` and is
   reported under the additive ``paused`` key, with a line naming the unit, the
   node, the reason and ``hscc node resume <host>``;
2. a skip is NOT a failure — ``success`` stays True. Load-bearing, not
   cosmetic: ``post_boot.py:745`` and ``recover.py:627`` read that one boolean,
   and a False there takes the ``launch-failed`` branch → restore baselines,
   burn a per-boot attempt, high-priority desktop page, about the operator's own
   pause;
3. with NOTHING but parked units the outcome is ``result: "all-paused"`` + exit
   success, and NOT the "serving.json missing or empty" config error (the empty
   guard sits ahead of the partition);
4. an un-parked fleet gets the pre-change dict — every pre-existing key
   deep-equal, the only addition being ``paused: []`` and no ``result``;
5. the partition asks ``paused_nodes()`` (keyed ∪ ``span_nodes``), never keyed
   ``paused()``, so a pause whose key lives OUTSIDE the plan still parks the
   whole span. That test also asserts the premise (``paused()`` says None),
   because it is the only shape that tells the two calls apart;
6. ``nodes=`` narrowing runs BEFORE the partition and is not an opt-out, and it
   does not shrink the ``expected_up`` claim — the pause does;
7. ``--dry-run`` keeps its polarity and previews the skip, writing nothing;
8. an unreadable overlay FAILS OPEN (to the full plan) and says so loudly.

Hermetic: 192.0.2.x placeholders throughout (scripts/address_guard.py), and the
overlay root is sealed to the per-test tmp dir — ``node_pauses.HSCC_DIR`` is
patched (``overlay_path`` resolves it per call, so reads and writes seal
together) and every pause is written through the real ``node_pauses.pause()``
with its explicit ``directory=`` seam. Nothing here reads or writes the
operator's ``~/.hscc/node_pauses.json``; the suite has no autouse ``~/.hscc``
isolation of its own, so the seal below is what makes that true.
"""

import json
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from hscc_daemon import daemon_ops, node_pauses, post_boot  # noqa: E402

from test_cluster_up_launch import (  # noqa: E402
    HOST_ORCH, HOST_WK1, HOST_WK2, _load_cli, _units,
)

#: A host that lives in NO unit of the fixture fleet. The span test parks a
#: span node through a record keyed on this host, which is the only shape where
#: ``paused()`` and ``paused_nodes()``/``covering()`` disagree — and therefore
#: the only shape that catches a keyed-only lookup.
HOST_OUTSIDE = "192.0.2.99"

REASON = "training run on this box until Friday"


# ── the hermetic seams ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _seal_pause_overlay(tmp_path, monkeypatch):
    """Seal the pause overlay AND the daemon log for every test in this file.

    ``node_pauses.HSCC_DIR`` is a module constant computed at import, but
    ``overlay_path()`` resolves it per call, so patching it redirects reads and
    writes together (the shipped precedent is the sibling
    ``test_node_pause_tools.py:75``). ``daemon_ops.log`` is pinned too:
    ``node_pauses._warn()`` routes a corrupt-overlay WARN through it, and this
    suite (unlike ``hscc_daemon``'s) has no autouse ``~/.hscc`` isolation —
    unpatched, a green corrupt-overlay test would append to the operator's live
    ``~/.hscc/daemon.log``.
    """
    root = str(tmp_path / "hscc")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(node_pauses, "HSCC_DIR", root)
    monkeypatch.setattr(daemon_ops, "log", lambda *a, **k: None)
    return root


@pytest.fixture
def overlay(_seal_pause_overlay):
    """The sealed overlay root (the autouse fixture's dir), for ``pause()``."""
    return _seal_pause_overlay


def _park(overlay, host, span_nodes=(), units=(), reason=REASON):
    """Park through the REAL overlay API, into the sealed root."""
    return node_pauses.pause(host, reason=reason, units=list(units),
                             span_nodes=list(span_nodes), stopped=True,
                             directory=overlay)


def _marker_units():
    """The unit ids the post-boot marker currently claims.

    Reads ``post_boot.EXPECTED_UP_FILE`` at CALL time: the suite's autouse
    ``_pin_expected_up_marker_to_tmp`` rebinds it per test, so a default
    argument (evaluated at import, before the fixture ran) would point every
    assertion at the operator's live marker.
    """
    path = post_boot.EXPECTED_UP_FILE
    if not os.path.exists(path):
        return None
    return {u["unit_id"] for u in json.load(open(path))["units"]}


# ── 1 + 2: parked unit skipped, and a skip is not a failure ────────────────
class TestParkedUnitIsSkipped:
    def test_a_parked_units_unit_is_absent_from_the_plan_and_reported(
            self, monkeypatch, overlay):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        res = mod.cmd_cluster_up(dry_run=False)
        # Not in the plan the verb claims it started…
        assert [e["unit_id"] for e in res["plan"]] == ["orch", "wk2"]
        assert res["units"] == 2
        # …not in what it issued…
        assert [i["unit_id"] for i in res["issued"]] == ["orch", "wk2"]
        # …reported under the one additive key, with the same element type
        # autodown.autoup returns.
        assert res["paused"] == ["wk1"]
        # A skip is not a failure, and no partial park means no outcome label.
        assert res["success"] is True
        assert "result" not in res
        assert "error" not in res
        # sparkrun was never asked to --ensure it.
        assert all(HOST_WK1 not in " ".join(c) for c in calls)
        assert len(calls) == 2

    def test_the_parked_unit_is_not_even_probed(self, monkeypatch, overlay):
        """The partition sits BEFORE the already-serving pre-flight, so a
        parked host is not dialed out to at all (and cannot fail the pass as
        "host status unavailable", which is how a parked host used to be able
        to sink a wake)."""
        probed = []

        def probe(url, timeout=5):
            probed.append(url)
            return {"ok": False, "models": [], "error": "stub"}

        mod, _calls, _ = _load_cli(monkeypatch, _units(), probe=probe)
        _park(overlay, HOST_WK1, units=["wk1"])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["paused"] == ["wk1"]
        assert all(HOST_WK1 not in u for u in probed)
        assert any(HOST_WK2 in u for u in probed)

    def test_the_skip_line_names_the_unit_node_reason_and_resume_command(
            self, monkeypatch, overlay, capsys):
        mod, _, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        mod.cmd_cluster_up(dry_run=False)
        out = capsys.readouterr().out
        assert "wk1" in out
        assert HOST_WK1 in out
        # The reason token exists ONLY inside the interpolated record, so this
        # pins that the reason actually reached the line (not the word "reason").
        assert REASON in out
        # The hint must name the verb that ends the pause — and must not send
        # the operator to `--apply`, which the 11-22 rule retired: resume now
        # prints its plan and asks.
        assert "hscc node resume" in out and "--apply" not in out

    def test_a_parked_unit_does_not_make_the_launch_failing_verdict_lie(
            self, monkeypatch, overlay):
        """``success`` stays True for a SKIP and still goes False for a REAL
        failure — the two must not be conflated in either direction
        (post_boot.py:745 / recover.py:627 read exactly this boolean)."""
        def sink(cmd, timeout=30, as_json=False):
            if HOST_WK2 in " ".join(cmd):
                return {"success": False, "returncode": 1,
                        "output": "sparkrun died", "error": "boom"}
            return {"success": True, "returncode": 0, "output": "ok",
                    "error": ""}

        mod, _calls, _ = _load_cli(monkeypatch, _units(), run_cmd_fn=sink)
        _park(overlay, HOST_WK1, units=["wk1"])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["paused"] == ["wk1"]
        assert res["success"] is False, "a real failure still fails the verdict"

    def test_parking_every_unit_reports_all_paused_not_an_error(
            self, monkeypatch, overlay, capsys):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        for h in (HOST_ORCH, HOST_WK1, HOST_WK2):
            _park(overlay, h, units=["x"])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["result"] == "all-paused"
        assert res["success"] is True, "nothing to do is not an error"
        assert res["units"] == 0 and res["plan"] == [] and res["issued"] == []
        assert res["paused"] == ["orch", "wk1", "wk2"]
        assert calls == []
        out = capsys.readouterr().out
        assert "every unit in the plan is parked" in out
        assert "nothing launched" in out

    def test_an_all_parked_plan_is_not_reported_as_a_config_error(
            self, monkeypatch, overlay):
        """The empty-plan guard must sit AHEAD of the partition: a fleet whose
        every unit happens to be parked must not masquerade as a broken
        serving.json."""
        mod, _, _ = _load_cli(monkeypatch, _units())
        for h in (HOST_ORCH, HOST_WK1, HOST_WK2):
            _park(overlay, h, units=["x"])
        res = mod.cmd_cluster_up(dry_run=False)
        assert "error" not in res
        assert res.get("result") == "all-paused"

    def test_a_genuinely_empty_serving_still_reports_its_own_error(
            self, monkeypatch, overlay):
        mod, _, _ = _load_cli(monkeypatch, [])
        res = mod.cmd_cluster_up(dry_run=False)
        assert "serving.json" in res.get("error", "")
        assert res.get("result") is None


# ── 4: an un-parked fleet is the dict it was before this change ────────────
class TestUnparkedFleetUnchanged:
    def test_with_no_pauses_the_report_is_the_pre_change_dict(
            self, monkeypatch):
        """The additive-change proof: every key that existed before this card
        deep-equals what it did, and the only difference is the one new key
        (empty) plus no ``result``. If this goes red, the filter is not
        additive."""
        mod, calls, _ = _load_cli(monkeypatch, _units())
        res = mod.cmd_cluster_up(dry_run=False)
        assert set(res) == {"success", "dry_run", "units", "plan", "issued",
                            "paused"}
        assert res["paused"] == []
        assert res["success"] is True and res["dry_run"] is False
        assert res["units"] == 3
        assert res["plan"] == [
            {"kind": u["role"], "nodes": list(u["nodes"]), "port": u["port"],
             "unit_id": u["id"], "keepalive": False,
             "cmd": ["sparkrun", "run", u["recipe"], "--hosts",
                     ",".join(u["nodes"]), "--port", str(u["port"]),
                     "--no-follow", "--ensure"]}
            for u in _units()]
        assert [i["unit_id"] for i in res["issued"]] == ["orch", "wk1", "wk2"]
        assert all(i["success"] for i in res["issued"])
        assert len(calls) == 3


# ── 5: span is a whole unit, and the reader is paused_nodes() ──────────────
class TestSpanIsOneUnit:
    def _tp_units(self):
        return [
            {"id": "orch", "role": "orchestrator", "nodes": [HOST_ORCH],
             "port": 8000, "recipe": "~/recipes/orch.yaml", "tp": 1},
            {"id": "tp", "role": "worker", "nodes": [HOST_WK1, HOST_WK2],
             "port": 8001, "recipe": "~/recipes/tp.yaml", "tp": 2},
        ]

    def test_a_span_record_keyed_outside_the_plan_parks_the_whole_unit(
            self, monkeypatch, overlay):
        """The span pin, in the shape that has teeth.

        The pause is keyed on a host NO unit of the plan lives on and reaches
        the span only through ``span_nodes`` — so ``paused(WK2)`` says None
        while ``paused_nodes()``/``covering()`` hold it. A seam that asked
        ``paused()`` per plan node would ``--ensure`` the whole TP group around
        its parked peer: a half-started unit, which is worse than a skip.
        """
        units = self._tp_units()
        mod, calls, _ = _load_cli(monkeypatch, units)
        _park(overlay, HOST_OUTSIDE, span_nodes=[HOST_WK2], units=["tp"])
        # The premise, asserted, because the test means nothing without it:
        # the keyed lookup a naive seam would use sees NOTHING parked here.
        assert node_pauses.paused(HOST_WK2) is None
        assert node_pauses.covering(HOST_WK2) is not None
        assert HOST_WK2 in node_pauses.paused_nodes()

        res = mod.cmd_cluster_up(dry_run=False)
        assert [e["unit_id"] for e in res["plan"]] == ["orch"]
        assert res["paused"] == ["tp"]
        assert res["success"] is True
        assert all(HOST_WK1 not in " ".join(c) and HOST_WK2 not in " ".join(c)
                   for c in calls), "a TP span must never come up in halves"
        assert any(HOST_ORCH in " ".join(c) for c in calls)

    def test_one_parked_span_node_parks_the_unit_whole(
            self, monkeypatch, overlay):
        units = self._tp_units()
        mod, calls, _ = _load_cli(monkeypatch, units)
        _park(overlay, HOST_WK2, span_nodes=[HOST_WK1, HOST_WK2], units=["tp"])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["paused"] == ["tp"] and res["units"] == 1
        assert all(HOST_WK2 not in " ".join(c) for c in calls)


# ── 6: nodes= narrows the launch, not the pause rule, not the claim ────────
class TestNodesNarrowing:
    def test_naming_a_parked_host_in_nodes_does_not_start_it(
            self, monkeypatch, overlay, capsys):
        """Naming is scoping, not an opt-out — and this is the post-boot
        relaunch shape: a stale marker asks for a node the operator parked."""
        mod, calls, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        res = mod.cmd_cluster_up(dry_run=False, nodes=[HOST_WK1])
        # Narrowing ran first (non-empty narrowed plan), the partition emptied it.
        assert res["plan"] == [] and res["issued"] == []
        assert res["paused"] == ["wk1"]
        assert res["result"] == "all-paused"
        assert res["success"] is True, "post_boot must not take launch-failed"
        assert calls == []
        out = capsys.readouterr().out
        assert "hscc node resume" in out and "--apply" not in out

    def test_a_scoped_up_still_claims_the_whole_unparked_fleet(
            self, monkeypatch, overlay):
        """Two independent axes, one read: ``nodes=`` scopes the LAUNCH, the
        pause shrinks the CLAIM. The marker is what post-boot relaunches from,
        so a parked unit must stop being a candidate there too."""
        mod, calls, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        mod.cmd_cluster_up(dry_run=False, nodes=[HOST_WK2])
        assert _marker_units() == {"orch", "wk2"}
        assert any(HOST_WK2 in " ".join(c) for c in calls)
        assert all(HOST_ORCH not in " ".join(c) for c in calls)

    def test_the_expected_up_marker_excludes_a_parked_unit(
            self, monkeypatch, overlay):
        """``record_expected_up`` receives the STARTABLE plan (rule 8). This is
        what makes a parked node stop being a post-boot relaunch candidate at
        its source, not only at the launch seam."""
        mod, _, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["success"] is True
        assert _marker_units() == {"orch", "wk2"}


# ── 7: dry-run keeps its polarity and previews the skip ────────────────────
class TestDryRunPreviewsTheSkip:
    def test_dry_run_shows_the_skip_and_writes_nothing(
            self, monkeypatch, overlay, capsys):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        res = mod.cmd_cluster_up(dry_run=True)
        out = capsys.readouterr().out
        assert "wk1" in out and HOST_WK1 in out and "hscc node resume" in out
        assert res["dry_run"] is True
        assert res["paused"] == ["wk1"]
        assert [e["unit_id"] for e in res["plan"]] == ["orch", "wk2"]
        assert calls == [], "a preview must not launch"
        assert not os.path.exists(post_boot.EXPECTED_UP_FILE)

    def test_dry_run_previews_an_all_parked_plan(self, monkeypatch, overlay):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        for h in (HOST_ORCH, HOST_WK1, HOST_WK2):
            _park(overlay, h, units=["x"])
        res = mod.cmd_cluster_up(dry_run=True)
        assert res["result"] == "all-paused"
        assert res["paused"] == ["orch", "wk1", "wk2"]
        assert calls == []
        assert not os.path.exists(post_boot.EXPECTED_UP_FILE)


# ── 8: fail open on an unreadable overlay, loudly ──────────────────────────
class TestFailOpen:
    def test_no_overlay_file_at_all_launches_everything(self, monkeypatch):
        mod, calls, _ = _load_cli(monkeypatch, _units())
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["paused"] == [] and res["success"] is True
        assert len(calls) == 3

    def test_a_corrupt_overlay_launches_the_full_plan(self, monkeypatch,
                                                     overlay, capsys):
        """``node_pauses.load()`` cannot raise: a malformed file is no pauses
        plus a WARN. Inherited here, so a half-written overlay degrades to
        today's behaviour rather than to a fleet that cannot boot itself."""
        path = os.path.join(overlay, "node_pauses.json")
        with open(path, "w") as f:
            f.write("{\"pauses\": { this is not json")
        mod, calls, _ = _load_cli(monkeypatch, _units())
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["paused"] == [] and res["success"] is True
        assert len(calls) == 3

    def test_a_reader_that_raises_fails_open_and_names_the_risk(
            self, monkeypatch, overlay, capsys):
        """Anything escaping the read is a bug in the overlay module, not a
        pause signal: log the risk, proceed UNFILTERED. Failing closed would
        let one bad file disable the fleet's ability to boot itself."""
        def boom(directory=None):
            raise RuntimeError("overlay module bug")
        monkeypatch.setattr(node_pauses, "paused_nodes", boom)
        _park(overlay, HOST_WK1, units=["wk1"])
        mod, calls, _ = _load_cli(monkeypatch, _units())
        res = mod.cmd_cluster_up(dry_run=False)
        assert res["paused"] == [] and res["success"] is True
        assert len(calls) == 3, "fail-OPEN means the FULL plan"
        out = capsys.readouterr().out
        # The exception's own text, so the line proves a real read failed.
        assert "overlay module bug" in out
        assert "FULL plan" in out and "node_pauses.json" in out


# ── the consumers, proven through the call (no post_boot.py edit) ──────────
class TestConsumersInheritTheFilter:
    def test_post_boot_default_up_fn_inherits_the_filter(self, monkeypatch,
                                                         overlay):
        """``post_boot._default_up_fn`` calls ``cmd_cluster_up(dry_run=False,
        nodes=…)`` and reads back ``success``. With a parked target it must get
        success=True + ``all-paused`` — no launch, no page. Deliberately driven
        through the real closure so the claim "no post_boot.py edit needed" is
        tested, not asserted."""
        from hscc_daemon import recover
        mod, calls, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK1, units=["wk1"])
        monkeypatch.setattr(recover, "_load_cluster_engine", lambda: mod)
        up_fn = post_boot._default_up_fn()
        assert up_fn is not None
        res = up_fn([HOST_WK1]) or {}
        assert res.get("success") is True
        assert res.get("result") == "all-paused"
        assert calls == []

    def test_recovery_style_full_fleet_up_keeps_ok_true(self, monkeypatch,
                                                       overlay):
        """``recover.py`` calls ``up_fn()`` with no narrowing and reads
        ``up.ok``: a partial park must not turn a successful recovery into a
        failed one."""
        mod, calls, _ = _load_cli(monkeypatch, _units())
        _park(overlay, HOST_WK2, units=["wk2"])
        res = mod.cmd_cluster_up()
        assert bool(res.get("success", res.get("ok", False))) is True
        assert res["paused"] == ["wk2"]
        assert len(calls) == 2


# ── the seam's own shape ───────────────────────────────────────────────────
class TestFrozenSurface:
    def test_the_frozen_signature_takes_no_new_argument(self):
        """Three production callers pass this positionally/by keyword
        (post_boot.py:811, recover.py:518, routes_ops.py:263); none may need an
        edit, so no ``pauses_directory=`` / ``include_paused=`` kwarg may appear
        on the verb. ``--include-paused`` is refused by decision, not by
        omission: it would start a unit under a LIVE pause record and make the
        overlay false for the watchdog, the heal breaker, autoscale and the
        health API at once."""
        import inspect
        import importlib.util
        path = os.path.join(_REPO_ROOT, "hscc-cluster", "hscc.py")
        spec = importlib.util.spec_from_file_location("hscc_cli_sig_probe", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        params = inspect.signature(mod.cmd_cluster_up).parameters
        assert list(params) == ["dry_run", "nodes"]
        src = open(path).read()
        assert "include_paused" not in src
