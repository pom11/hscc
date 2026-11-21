"""Node-pause skip seam on the autodown WAKE path (hscc_daemon/autodown.py).

The feature (2.17.3, core in ``hscc_daemon/node_pauses.py``) lets the operator
park one node for a while without editing anything the cluster is built from. A
parked node is only parked if every repair path honours the record — and the
wake path is the one that would undo it silently, because the wake command is
``sparkrun run … --ensure``, whose entire purpose is to make the declared
workload exist again. These tests pin the seam in both directions:

  * with a pause in force, the parked unit is NEVER ensured — not by the start
    loop, not by the readiness wait, not through the post-boot footprint that
    would relaunch it after a reboot;
  * with no pause (or a pause on a node this plan does not need) EVERY unit is
    still started, so the seam cannot quietly disable the fleet's wake.

Every case drives the real ``autodown.autoup()`` with injected fakes: fake hosts
in the 192.0.2.0/24 documentation range, a fake ``run_cmd`` (no sparkrun is ever
executed), an injected health probe and clock, and the overlay read through
node_pauses' ``directory=`` seam pointed at a temp dir — so the operator's real
``~/.hscc`` (and the real cluster) is never read or written.
"""

import json

import pytest

import hscc_daemon.autodown as ad
import hscc_daemon.lifecycle as _lifecycle
import hscc_daemon.post_boot as _post_boot
from hscc_daemon import node_pauses

# Three units mirroring the shape the live serving.json has: a two-node
# tensor-parallel orchestrator, a plain worker, and a KEEPALIVE worker (the unit
# class this card names — keepalive units are exactly the ones a wake restores).
ORCH_NODES = ["192.0.2.244", "192.0.2.246"]
WK1_NODE = "192.0.2.247"
KEEP_NODE = "192.0.2.248"
UNRELATED_NODE = "192.0.2.99"
# A node no unit of this serving.json lives on, used as the KEY of a pause
# record whose span_nodes reach into the plan (see the span test below).
SPAN_HEAD_NODE = "192.0.2.250"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def overlay(tmp_path):
    """A temp node-pause overlay root (absent file == no pauses)."""
    return str(tmp_path / "pauses")


@pytest.fixture
def autodown_file(tmp_path, monkeypatch):
    """Point AUTODOWN_FILE at a tmp path and return the path."""
    path = tmp_path / "hscc" / "autodown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
    return path


class _FakeRunner:
    """Records every start command; never executes anything."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])
        self._i = 0

    def __call__(self, cmd, timeout=30):
        ok = self.results[self._i] if self._i < len(self.results) else True
        self._i += 1
        self.calls.append({"cmd": list(cmd), "ok": ok})
        return {"ok": ok, "output": "" if ok else "start command failed"}


class _Probe:
    """Health probe that reports ``down_hosts`` unhealthy and records every URL."""

    def __init__(self, down_hosts=()):
        self.down = list(down_hosts)
        self.urls = []

    def __call__(self, url, timeout=5):
        self.urls.append(url)
        return {"ok": not any(h in url for h in self.down)}


def _write_serving(tmp_path):
    data = {
        "port": 8000,
        "units": [
            {"id": "orch", "role": "orchestrator", "nodes": ORCH_NODES,
             "port": 8000, "recipe": "~/.sparkrun-local/recipes/orch.yaml"},
            {"id": "wk1", "role": "worker", "keepalive": False,
             "nodes": [WK1_NODE], "port": 8000,
             "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
            {"id": "wk-keep", "role": "worker", "keepalive": True,
             "nodes": [KEEP_NODE], "port": 8000,
             "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
        ],
    }
    path = tmp_path / "serving.json"
    path.write_text(json.dumps(data))
    return str(path)


def _seed_down(tmp_path, monkeypatch, autodown_file):
    """The posture autoup is called with: state down + latched block."""
    block_file = str(tmp_path / "watchdog-block.json")
    monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
    _lifecycle.save_watchdog_block(
        {"blocked": True, "intentional": "autodown",
         "reason": ad.WATCHDOG_TEARDOWN_REASON,
         "blocked_at": "2026-01-01T00:00:00+00:00", "failures": []})
    cfg = dict(ad.DEFAULT_CONFIG)
    cfg["enabled"] = True
    cfg["state"] = "down"
    cfg["down_since"] = "2026-01-01T00:00:00+00:00"
    ad.save_config(cfg)
    return block_file


def _wake(overlay, serving, runner, http_check=None):
    """One autoup() through every seam: fake runner, fake probe, no sleeps."""
    return ad.autoup(
        serving_path=serving, run_cmd_fn=runner,
        http_check_fn=http_check or _Probe(), clock=lambda: 0.0,
        sleep_fn=lambda _s: None, notify=False, pauses_directory=overlay)


def _all_cmds(runner):
    return [" ".join(call["cmd"]) for call in runner.calls]


def _started_ids(runner):
    """The unit ids a start command was issued for, via the --hosts argument."""
    return [cmd.split("--hosts")[1].split()[0] for cmd in _all_cmds(runner)]


# ---------------------------------------------------------------------------
# The skip, in the direction the operator cares about
# ---------------------------------------------------------------------------

class TestParkedUnitsAreNeverEnsured:

    def test_a_parked_keepalive_node_gets_no_ensure(self, tmp_path, monkeypatch,
                                                    autodown_file, overlay):
        """The wake must not resurrect a paused unit with ``sparkrun run --ensure``.

        The card's headline: park the keepalive worker's node, trigger a wake,
        and ``192.0.2.248`` must appear in NO start command — while the rest of
        the fleet still comes up normally (the next test pins that half).
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(KEEP_NODE, reason="training run", directory=overlay)
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["result"] == "up"
        assert not any(KEEP_NODE in cmd for cmd in _all_cmds(runner)), \
            "a parked node was ensured: " + " | ".join(_all_cmds(runner))
        assert res["paused"] == ["wk-keep"]
        assert sorted(_started_ids(runner)) == sorted(["192.0.2.244,192.0.2.246",
                                                       WK1_NODE])

    def test_the_surviving_starts_are_still_full_ensure_commands(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """Skipping one unit must not weaken the starts that ARE issued."""
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(KEEP_NODE, directory=overlay)
        runner = _FakeRunner()

        _wake(overlay, _write_serving(tmp_path), runner)

        assert len(runner.calls) == 2
        for call in runner.calls:
            cmd = call["cmd"]
            assert cmd[0] == "sparkrun" and cmd[1] == "run"
            assert "--ensure" in cmd and "--no-follow" in cmd
            assert "--served-model-name" in cmd

    def test_readiness_never_waits_for_a_parked_unit(self, tmp_path, monkeypatch,
                                                     autodown_file, overlay):
        """A parked unit must not time the wake out.

        ``_wait_ready`` polls every unit in the plan; if the parked unit stayed in
        it, a unit the operator deliberately stopped could never be healthy and
        every wake would end in ``not-ready``. Here the parked unit's port is
        probed NOT AT ALL and the wake still reports ``up``.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(KEEP_NODE, directory=overlay)
        probe = _Probe()
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner,
                    http_check=probe)

        assert res["result"] == "up"
        assert not any(KEEP_NODE in url for url in probe.urls), probe.urls
        assert res["ready"] == ["orch", "wk1"]

    def test_a_pause_keyed_on_a_span_peer_parks_the_whole_unit(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """Pausing a TP PEER takes the unit down — so the unit must be skipped.

        The orchestrator spans .244+.246 and stops as one workload. A seam that
        asked ``node_pauses.paused()`` for the unit's head node only would see
        .244 un-paused and re-issue ``--ensure`` on a two-host span whose second
        host is parked. This is why the seam asks ``paused_nodes()``/``covering``.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(ORCH_NODES[1], reason="training run", directory=overlay)
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["paused"] == ["orch"]
        assert not any("192.0.2.244" in cmd for cmd in _all_cmds(runner))
        assert sorted(_started_ids(runner)) == sorted([WK1_NODE, KEEP_NODE])

    def test_span_nodes_in_one_record_cover_the_other_host(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """A record naming the peer in ``span_nodes`` holds the whole span too."""
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(ORCH_NODES[1], span_nodes=ORCH_NODES,
                          directory=overlay)
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["paused"] == ["orch"]
        assert not any("192.0.2.244" in cmd for cmd in _all_cmds(runner))

    def test_a_span_record_holds_a_unit_it_was_never_keyed_against(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """The record's ``span_nodes`` must be followed, not just its host key.

        The discriminating case for ``paused_nodes()`` over ``paused()``: the
        operator parked a span whose head is 192.0.2.250, and the keepalive
        worker lives on the span's other member. No pause record is keyed on
        .248 at all, so a seam that looked only for a record keyed on the unit's
        own node would see nothing and ``--ensure`` the parked host anyway.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(SPAN_HEAD_NODE, span_nodes=[SPAN_HEAD_NODE, KEEP_NODE],
                          reason="training run", directory=overlay)
        assert node_pauses.paused(KEEP_NODE, directory=overlay) is None
        assert KEEP_NODE in node_pauses.paused_nodes(overlay)
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["paused"] == ["wk-keep"]
        assert not any(KEEP_NODE in cmd for cmd in _all_cmds(runner))
        assert sorted(_started_ids(runner)) == sorted(["192.0.2.244,192.0.2.246",
                                                       WK1_NODE])

    def test_a_parked_unit_is_absent_from_the_post_boot_footprint(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """The marker that relaunches units after a reboot must not list it.

        ``post_boot.record_expected_up(plan)`` is written BEFORE the starts and
        is what the post-boot pass launches on the next boot. Feeding it the
        unfiltered plan would re-arm a launch for the node the operator parked —
        the same resurrection, one boot later.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(KEEP_NODE, directory=overlay)
        seen = {}
        monkeypatch.setattr(
            _post_boot, "record_expected_up",
            lambda plan, reason=None, **kw: seen.update(plan=list(plan)))
        runner = _FakeRunner()

        _wake(overlay, _write_serving(tmp_path), runner)

        assert [e["unit_id"] for e in seen["plan"]] == ["orch", "wk1"]
        assert "wk-keep" not in [e["unit_id"] for e in seen["plan"]]


# ---------------------------------------------------------------------------
# The other direction: the seam must not disable the wake
# ---------------------------------------------------------------------------

class TestUnpausedWakeIsUntouched:

    def test_an_empty_overlay_starts_every_unit(self, tmp_path, monkeypatch,
                                                autodown_file, overlay):
        """No pause file at all ⇒ the wake is exactly what it was before."""
        _seed_down(tmp_path, monkeypatch, autodown_file)
        assert node_pauses.paused_nodes(overlay) == set()
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["result"] == "up"
        assert res["paused"] == []
        assert len(runner.calls) == 3
        assert KEEP_NODE in " ".join(_all_cmds(runner))
        assert sorted(res["ready"]) == ["orch", "wk-keep", "wk1"]

    def test_a_pause_on_an_unrelated_node_skips_nothing(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """A record for a node this plan never uses must not hold the wake back."""
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(UNRELATED_NODE, directory=overlay)
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["result"] == "up"
        assert res["paused"] == []
        assert len(runner.calls) == 3

    def test_an_expired_but_unresumed_pause_still_holds_the_unit(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """``paused()`` is clock-independent, so the wake must be too.

        An entry whose ``until`` has passed is still a pause until the daemon's
        own resume path clears it; treating it as free here would relaunch the
        node seconds before the auto-resume does, which is the failure
        node_pauses' module docstring rules out.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        node_pauses.pause(KEEP_NODE, until="1s", directory=overlay,
                          now="2000-01-01T00:00:00+00:00")
        assert node_pauses.is_expired(
            node_pauses.paused(KEEP_NODE, directory=overlay))
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["paused"] == ["wk-keep"]
        assert not any(KEEP_NODE in cmd for cmd in _all_cmds(runner))


# ---------------------------------------------------------------------------
# The all-parked fleet: an honest no-op, not a fake wake
# ---------------------------------------------------------------------------

class TestEveryUnitParked:

    def _park_all(self, overlay):
        for node in ORCH_NODES + [WK1_NODE, KEEP_NODE]:
            node_pauses.pause(node, reason="training run", directory=overlay)

    def test_reports_all_paused_with_no_starts(self, tmp_path, monkeypatch,
                                               autodown_file, overlay):
        _seed_down(tmp_path, monkeypatch, autodown_file)
        self._park_all(overlay)
        runner = _FakeRunner()

        res = _wake(overlay, _write_serving(tmp_path), runner)

        assert res["result"] == "all-paused"
        assert runner.calls == []
        assert sorted(res["paused"]) == ["orch", "wk-keep", "wk1"]
        assert res["started"] == [] and res["ready"] == []

    def test_changes_no_state_and_no_records(self, tmp_path, monkeypatch,
                                             autodown_file, overlay):
        """A wake that starts nothing must leave no trace.

        The three records a wake writes each have a consequence: ``state`` and
        the watchdog block decide who supervises the fleet, and the post-boot
        footprint decides what launches on the next boot. None of them may move
        for a wake that started nothing — leaving the pre-wake posture (down,
        blocked, watchdog holding) is what keeps the parked nodes parked.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        self._park_all(overlay)
        marker = []
        monkeypatch.setattr(_post_boot, "record_expected_up",
                            lambda plan, reason=None, **kw: marker.append(plan))
        runner = _FakeRunner()

        _wake(overlay, _write_serving(tmp_path), runner)

        cfg = ad.load_config()
        assert cfg["state"] == "down"
        assert cfg["wake_at"] is None and cfg["wake_source"] is None
        assert cfg["down_since"] == "2026-01-01T00:00:00+00:00"
        assert marker == []
        block = _lifecycle.load_watchdog_block()
        assert block.get("blocked") is True
        assert block.get("intentional") == "autodown"

    def test_an_empty_plan_is_still_the_empty_plan_failure(
            self, tmp_path, monkeypatch, autodown_file, overlay):
        """§1a must not be confused with an all-parked fleet.

        A missing serving.json is a FAILURE to know what to start (state error,
        stay-down ended, watchdog released). A parked fleet is neither: this test
        pins that the emptiness check still runs on the UNFILTERED plan.
        """
        _seed_down(tmp_path, monkeypatch, autodown_file)
        runner = _FakeRunner()

        res = _wake(overlay, str(tmp_path / "no-serving-here.json"), runner)

        assert res["result"] == "no-units"
        assert ad.load_config()["state"] == "error"
        assert runner.calls == []


# ---------------------------------------------------------------------------
# The seam's own contract
# ---------------------------------------------------------------------------

class TestPartitionContract:

    def _plan(self):
        return [{"unit_id": "orch", "nodes": ORCH_NODES, "kind": "orchestrator"},
                {"unit_id": "wk1", "nodes": [WK1_NODE], "kind": "worker"},
                {"unit_id": "wk-keep", "nodes": [KEEP_NODE], "kind": "worker"}]

    def test_no_overlay_holds_nothing(self, overlay):
        startable, parked = ad._partition_wake_plan(self._plan(),
                                                    pauses_directory=overlay)
        assert [e["unit_id"] for e in startable] == ["orch", "wk1", "wk-keep"]
        assert parked == []

    def test_a_parked_unit_carries_the_node_that_holds_it(self, overlay):
        node_pauses.pause(KEEP_NODE, directory=overlay)
        startable, parked = ad._partition_wake_plan(self._plan(),
                                                    pauses_directory=overlay)
        assert [e["unit_id"] for e in startable] == ["orch", "wk1"]
        assert [(e["unit_id"], nodes) for e, nodes in parked] == \
               [("wk-keep", [KEEP_NODE])]

    def test_an_unreadable_overlay_fails_open_to_the_full_plan(
            self, monkeypatch, overlay):
        """A broken overlay file must not disable the fleet's only wake path.

        ``node_pauses.load()`` never raises by construction, so an exception here
        is a bug in the overlay module rather than a pause signal; skipping every
        unit on it would turn a corrupt file into a permanently unwakeable fleet.
        The plan therefore proceeds in full — loudly.
        """
        def boom(_directory=None):
            raise RuntimeError("overlay exploded")

        monkeypatch.setattr(node_pauses, "paused_nodes", boom)
        startable, parked = ad._partition_wake_plan(self._plan(),
                                                    pauses_directory=overlay)
        assert [e["unit_id"] for e in startable] == ["orch", "wk1", "wk-keep"]
        assert parked == []

    def test_only_the_wake_path_consults_the_overlay(self):
        """Teardown must NOT skip stops (pin, not preference).

        Refusing to stop a parked unit would leave it RUNNING on a node the
        operator parked — the mirror image of the bug this seam closes. If
        someone routes the pause check into the teardown plan builder, this pin
        reddens.
        """
        import inspect

        wake = inspect.getsource(ad._build_wake_plan) + \
            inspect.getsource(ad._partition_wake_plan) + \
            inspect.getsource(ad._autoup_locked)
        teardown = inspect.getsource(ad._build_teardown_plan) + \
            inspect.getsource(ad._teardown_locked)
        assert "node_pauses" in wake
        assert "node_pauses" not in teardown and "_parked_nodes" not in teardown
