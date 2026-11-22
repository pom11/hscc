"""The node-pause seam on the autodown TEARDOWN INTERLOCK (§6.4).

The wake seam (t_af9901e2, ``test_autodown_paused_wake.py``) stopped a parked
unit from being *started*. This file covers the other half, which the wake card
discovered and deliberately did not fold in because it changes a fail-safe gate:
``_default_keepalive_ok`` — the interlock that ABORTS an idle teardown when a
keepalive unit is unhealthy or unverifiable — read a *parked* keepalive unit as
down, so parking the keepalive node disabled the fleet's only automatic
power-down until ``hscc node resume``.

The decision this file pins (t_eec97ea8, reading (b)): a keepalive unit whose
span the pause overlay holds is **not unhealthy** — it is parked. ``node_pauses``
is an affirmative operator statement, and §6.4's own rationale ("do not tear the
fleet down mid-problem while a worker is mid-flight relying on it") does not
describe a park: the operator stopped the unit, so nothing relies on it.

Both directions are pinned, and so is everything the (b) reading must NOT
loosen:

  * every UNVERIFIABLE signal still fails closed — unparseable serving, an
    unresolvable unit set, failed TP-peer resolution, an unhealthy or raising
    probe on a unit that is NOT parked;
  * an UNREADABLE overlay exempts NOTHING (the opposite polarity to the wake
    seam's fail-open, deliberately — see ``_PARKED_READ_RISK``);
  * a park on a node no keepalive unit spans exempts nothing, and a parked unit
    never exempts a sick sibling;
  * the exemption keys on the unit's FULL SPAN (``paused_nodes()``, spans
    included), never the single ``node`` key;
  * the STOP path still stops a parked unit (the plan and the verify set keep
    it), and ``teardown()``'s step-1 re-verify reads the SAME overlay the timer
    read.

Every case injects its fakes and points the overlay at a temp dir through
``node_pauses``' ``directory=`` seam, so the operator's real ``~/.hscc`` and the
real cluster are never read, written, or commanded. Hosts are all in the
192.0.2.0/24 documentation range.
"""

import datetime as _dt
import json

import pytest

import hscc_daemon.autodown as ad
import hscc_daemon.health as _health
import hscc_daemon.lifecycle as _lifecycle
import hscc_daemon.serving as _serving
import hscc_daemon.util as _util
from hscc_daemon import node_pauses

# Imported, never copied: the kanban lib the idle-conjunction tests use, already
# proven idle by the module that owns it (test_autodown.py's own "all clear ⇒
# teardown" cases drive it).
from hscc_daemon.tests.test_autodown import _FakeKb

FLEET_STOP_CMD = _serving.fleet_down_cmd()
NOW = _dt.datetime(2026, 8, 23, 12, 0, 0, tzinfo=_dt.timezone.utc)

# The same three-unit fleet the wake-seam file uses: a two-node TP
# orchestrator, a plain worker, and a KEEPALIVE worker — the unit class this
# card names.
ORCH_NODES = ["192.0.2.244", "192.0.2.246"]
WK1_NODE = "192.0.2.247"
KEEP_NODE = "192.0.2.248"
KEEP_PEER = "192.0.2.249"
UNRELATED_NODE = "192.0.2.99"
SPAN_KEY_NODE = "192.0.2.250"  # a host no unit lives on, used as a pause KEY

#: A positively-clear PR/CI screen — the shape ``_screen_prci`` returns. Passed
#: explicitly (not via test_autodown's autouse stub, which my import does not
#: inherit) so nothing here can reach a real ``gh``.
_CLEAR_PRCI = lambda: {"open_prs": 0, "active_runs": 0, "repos": []}  # noqa: E731


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def overlay(tmp_path):
    """A temp node-pause overlay root (an absent file means "no pauses")."""
    return str(tmp_path / "pauses")


@pytest.fixture
def autodown_file(tmp_path, monkeypatch):
    """Point AUTODOWN_FILE at a tmp path and return the path."""
    path = tmp_path / "hscc" / "autodown.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ad, "AUTODOWN_FILE", str(path))
    return path


@pytest.fixture
def logs(monkeypatch):
    """Collect every autodown log line so a test can assert on what it said."""
    seen = []
    monkeypatch.setattr(ad, "log",
                        lambda msg, level="INFO", **kw: seen.append(
                            (level, msg)))
    return seen


def serving_dict(keep_nodes=(KEEP_NODE,), keepalive_second=None):
    """The 3-unit fleet, with the keepalive unit spanning ``keep_nodes``.

    ``keepalive_second`` adds a SECOND keepalive unit, so a test can prove a
    parked unit does not exempt an unhealthy sibling.
    """
    units = [
        {"id": "orch", "role": "orchestrator", "nodes": ORCH_NODES,
         "port": 8000, "recipe": "~/.sparkrun-local/recipes/orch.yaml"},
        {"id": "wk1", "role": "worker", "keepalive": False,
         "nodes": [WK1_NODE], "port": 8000,
         "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
        {"id": "wk-keep", "role": "worker", "keepalive": True,
         "nodes": list(keep_nodes), "port": 8000,
         "recipe": "~/.sparkrun-local/recipes/wk.yaml"},
    ]
    if keepalive_second:
        units.append({"id": "wk-keep-2", "role": "worker", "keepalive": True,
                      "nodes": [keepalive_second], "port": 8001,
                      "recipe": "~/.sparkrun-local/recipes/wk.yaml"})
    return {"port": 8000, "units": units}


def _wire(monkeypatch, serving, tp_peers=(), down=(), probe_raises=None):
    """Route the interlock's world through fakes; return the probe's URL log.

    ``serving`` is handed to ``load_serving`` for BOTH the interlock and
    ``teardown()``'s plan builder, so one dict is one fleet. The real
    ``serving.keepalive_units()`` still runs on it — this fakes the *source*,
    never the unit-flattening contract under test.
    """
    monkeypatch.setattr(_serving, "load_serving", lambda *a, **k: serving)
    monkeypatch.setattr(_health, "_tp_peer_nodes", lambda: set(tp_peers))
    urls = []
    down_hosts = list(down)

    def _probe(url, timeout=5):
        urls.append(url)
        if probe_raises:
            raise probe_raises
        return {"ok": not any(h in url for h in down_hosts),
                "status": 200 if not any(h in url for h in down_hosts) else 0}

    monkeypatch.setattr(_util, "http_check", _probe)
    return urls


def _park(overlay, node, span_nodes=(), reason="training run"):
    """Record a real pause through the overlay's own write API (temp root)."""
    node_pauses.pause(node, reason=reason, directory=overlay,
                      span_nodes=list(span_nodes))
    return overlay


def _idle_cfg(**over):
    """An enabled, up, window-elapsed autodown config (teardown-able)."""
    cfg = dict(ad.DEFAULT_CONFIG)
    cfg["enabled"] = True
    cfg["state"] = "up"
    cfg["idle_minutes"] = 10
    cfg["last_activity_iso"] = (
        NOW - _dt.timedelta(minutes=15)).isoformat()
    cfg["cancel_requested"] = False
    cfg.update(over)
    ad.save_config(cfg)
    return cfg


def _agents_file(tmp_path):
    p = tmp_path / "agents.json"
    p.write_text(json.dumps({"agents": [{"name": "a", "status": "idle"}]}))
    return str(p)


class TestParkedKeepaliveUnitIsNotUnhealthy:
    """The (b) reading, and the fail-closed world it must not widen into."""

    def test_a_parked_keepalive_unit_is_not_counted_unhealthy(self, monkeypatch,
                                                              overlay, logs):
        """The card's bug, pinned fixed: park the keepalive node, leave the
        fleet idle ⇒ the interlock clears instead of aborting teardown forever.

        The probe reports the unit's head DOWN, so with no pause this same call
        returns False (see the reverse case below). A parked unit is not a sick
        one, and the probe is not even issued — the unit is absent by
        intention, so probing it would only measure the park.
        """
        urls = _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        _park(overlay, KEEP_NODE)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is True
        assert urls == []
        # And the exemption says so, at INFO (a park lasts hours; a per-tick
        # WARN would train the operator to ignore WARNs).
        assert [("INFO", m) for level, m in logs if level == "INFO"
                and "wk-keep" in m and "parked" in m], logs

    def test_the_same_unit_down_with_no_pause_still_aborts(self, monkeypatch,
                                                           overlay):
        """Reverse direction: with an EMPTY overlay nothing is exempt, so a
        down keepalive head still fails the gate closed. The seam must not be
        able to clear a teardown on its own existence."""
        urls = _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False
        assert urls == [f"http://{KEEP_NODE}:8000/health"]

    def test_a_parked_tp_peer_exempts_the_whole_span(self, monkeypatch,
                                                     overlay):
        """A span is ONE workload: park the TP peer and the head is down too,
        so the whole unit must be exempt. Keying the exemption on the entry's
        own ``node`` would keep probing the head and abort forever — the exact
        bug this seam closes, one node over."""
        urls = _wire(monkeypatch, serving_dict(keep_nodes=[KEEP_NODE,
                                                           KEEP_PEER]),
                     tp_peers=[KEEP_PEER], down=[KEEP_NODE])
        _park(overlay, KEEP_PEER)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is True
        assert urls == []

    def test_a_pause_keyed_on_another_host_still_covers_the_span(
            self, monkeypatch, overlay):
        """The seam must ask ``paused_nodes()`` (keyed hosts ∪ span members),
        never ``paused(host)`` — ``node_pauses``' "WHAT A SKIP SEAM MUST ASK".

        The record is keyed against a host no keepalive unit lives on and names
        the keepalive node in its ``span_nodes``. A seam that asked only
        "is THIS host paused?" would keep aborting teardown here.
        """
        urls = _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        _park(overlay, SPAN_KEY_NODE, span_nodes=[KEEP_NODE])

        assert ad._default_keepalive_ok(pauses_directory=overlay) is True
        assert urls == []

    def test_a_park_on_an_unrelated_node_exempts_nothing(self, monkeypatch,
                                                         overlay):
        """The exemption cannot be widened by ANY pause: park a node no unit of
        this fleet spans and the sick keepalive head still aborts."""
        urls = _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        _park(overlay, UNRELATED_NODE)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False
        assert urls == [f"http://{KEEP_NODE}:8000/health"]

    def test_a_parked_unit_never_exempts_a_sick_sibling(self, monkeypatch,
                                                        overlay):
        """Per-unit, not per-fleet: park one keepalive unit and a SECOND sick
        one still aborts. Otherwise the exemption would be a teardown master
        switch — one park and every keepalive failure becomes invisible."""
        urls = _wire(monkeypatch,
                     serving_dict(keepalive_second="192.0.2.251"),
                     down=[KEEP_NODE, "192.0.2.251"])
        _park(overlay, KEEP_NODE)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False
        assert urls == ["http://192.0.2.251:8001/health"]

    def test_an_unreadable_overlay_exempts_nothing_and_says_so(
            self, monkeypatch, overlay, logs):
        """Fail-CLOSED polarity — the deliberate opposite of the wake seam.

        ``node_pauses.load()`` never raises by construction, so an exception
        here is a bug in the overlay module rather than a pause signal. For a
        START the conservative action is to proceed (fail open — one bad start
        versus a permanently unwakeable fleet). For an INTERLOCK the
        conservative action is the other way: exempt NOTHING, i.e. behave
        exactly as this gate did before the seam existed. Never permit a
        teardown on an unverified signal.
        """
        def boom(_directory=None):
            raise RuntimeError("overlay exploded")

        monkeypatch.setattr(node_pauses, "paused_nodes", boom)
        urls = _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False
        assert urls == [f"http://{KEEP_NODE}:8000/health"]
        errors = [m for level, m in logs if level == "ERROR"]
        assert any("keepalive interlock" in m and "exempting NOTHING" in m
                   for m in errors), errors
        # The wake seam's wording must not leak onto this seam's line — one
        # message would misstate which risk actually ran.
        assert not any("FULL wake plan" in m for m in errors), errors

    def test_fail_closed_on_unparseable_serving_even_when_parked(
            self, monkeypatch, overlay):
        """A parked unit does not rescue an unverifiable unit set: if serving
        cannot be read at all the gate still aborts (§6.4 fail-safe)."""
        def boom(*a, **k):
            raise ValueError("serving.json is not parseable")

        monkeypatch.setattr(_serving, "load_serving", boom)
        monkeypatch.setattr(_health, "_tp_peer_nodes", lambda: set())
        _park(overlay, KEEP_NODE)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False

    def test_fail_closed_on_tp_peer_resolution_failure_even_when_parked(
            self, monkeypatch, overlay):
        """Same for TP-peer resolution: an unverifiable span layout aborts even
        when every unit in it is parked."""
        _wire(monkeypatch, serving_dict(keep_nodes=[KEEP_NODE, KEEP_PEER]),
              down=[KEEP_NODE])
        _park(overlay, KEEP_NODE)
        _park(overlay, KEEP_PEER)

        def boom():
            raise RuntimeError("scoreboard unavailable")

        monkeypatch.setattr(_health, "_tp_peer_nodes", boom)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False

    def test_a_raising_probe_on_a_non_parked_unit_still_aborts(
            self, monkeypatch, overlay):
        """A network error on a unit the operator did NOT park is still an
        unverifiable signal ⇒ abort."""
        _wire(monkeypatch, serving_dict(), probe_raises=OSError("conn refused"))
        _park(overlay, UNRELATED_NODE)

        assert ad._default_keepalive_ok(pauses_directory=overlay) is False

    def test_the_named_overlay_is_the_one_read_both_ways(self, monkeypatch,
                                                         tmp_path):
        """The ``directory=`` seam must be the root that decides — not HOME.

        Both directions: a pause written ONLY at the named root exempts (so the
        seam is really read), and a pause written ONLY at the default root does
        not (so a stray record in the operator's ``~/.hscc`` cannot exempt a
        teardown that the caller's overlay says is sick). ``conftest`` seals the
        default root for the whole leg; this pins the *routing*, not the seal.
        """
        named = str(tmp_path / "named")
        default = str(tmp_path / "default")
        monkeypatch.setattr(node_pauses, "HSCC_DIR", default)
        urls = _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])

        _park(default, KEEP_NODE)
        assert ad._default_keepalive_ok(pauses_directory=named) is False
        assert urls == [f"http://{KEEP_NODE}:8000/health"]

        _park(named, KEEP_NODE)
        urls.clear()
        assert ad._default_keepalive_ok(pauses_directory=named) is True
        assert urls == []


class TestIdleConjunctionAndCycleThreadTheSeam:
    """The seam has to reach the gate; a defaulted-out kwarg is the same bug."""

    def _cycle(self, overlay, tmp_path, monkeypatch):
        """One cycle() through the real conjunction; returns teardown calls."""
        issued = []
        monkeypatch.setattr(ad, "teardown",
                            lambda *a, **k: issued.append(k) or
                            {"result": "down"})
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE",
                            str(tmp_path / "watchdog-block.json"))
        _idle_cfg()
        ad.cycle(probes=[], kanban_db=_FakeKb([]),
                 agents_file=_agents_file(tmp_path), now=NOW,
                 prci_checker=_CLEAR_PRCI, pauses_directory=overlay)
        return issued

    def test_the_conjunction_clears_for_a_parked_unit(self, monkeypatch,
                                                      overlay, tmp_path):
        """``_is_idle`` reaches the gate with the seam — otherwise a caller
        that threads the overlay still gets the parked unit read off HOME."""
        _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        _park(overlay, KEEP_NODE)
        cfg = _idle_cfg()

        assert ad._is_idle(cfg, kanban_db=_FakeKb([]),
                           agents_file=_agents_file(tmp_path), now=NOW,
                           prci_checker=_CLEAR_PRCI,
                           pauses_directory=overlay) is True

    def test_the_conjunction_still_blocks_with_no_park(self, monkeypatch,
                                                       overlay, tmp_path):
        _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        cfg = _idle_cfg()

        assert ad._is_idle(cfg, kanban_db=_FakeKb([]),
                           agents_file=_agents_file(tmp_path), now=NOW,
                           prci_checker=_CLEAR_PRCI,
                           pauses_directory=overlay) is False

    def test_cycle_teardowns_with_the_keepalive_unit_parked(self, monkeypatch,
                                                            overlay, tmp_path):
        """End to end on the decision side: park the keepalive node, leave the
        fleet idle ⇒ ``cycle()`` tears down instead of logging
        interlock-aborted every tick forever."""
        _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        _park(overlay, KEEP_NODE)

        assert len(self._cycle(overlay, tmp_path, monkeypatch)) == 1

    def test_cycle_does_not_teardown_when_it_is_sick_not_parked(
            self, monkeypatch, overlay, tmp_path):
        """Reverse: the same cycle with an empty overlay must NOT tear down."""
        _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])

        assert self._cycle(overlay, tmp_path, monkeypatch) == []

    def test_an_injected_interlock_still_outranks_the_seam(self, monkeypatch,
                                                           overlay, tmp_path):
        """``keepalive_ok=`` is the caller's whole gate and stays zero-argument
        — the seam it replaces must not get to demand one of its own."""
        _wire(monkeypatch, serving_dict(), down=[KEEP_NODE])
        _park(overlay, KEEP_NODE)
        cfg = _idle_cfg()
        seen = []

        def _injected():
            seen.append(1)
            return False

        assert ad._is_idle(cfg, kanban_db=_FakeKb([]),
                           agents_file=_agents_file(tmp_path), now=NOW,
                           keepalive_ok=_injected, prci_checker=_CLEAR_PRCI,
                           pauses_directory=overlay) is False
        assert seen == [1]


class TestTeardownStillStopsAParkedUnit:
    """``teardown()`` must honour the overlay for the INTERLOCK only.

    The stop path skips nothing: refusing to stop a parked unit would leave it
    RUNNING on a node the operator parked — the mirror image of the bug the wake
    seam closes. These are the behavioural pins for that (the sibling file's
    pin is a source-substring one, which cannot see a plan that quietly drops
    the unit).
    """

    def _teardown(self, overlay, tmp_path, monkeypatch):
        serving = serving_dict()
        _wire(monkeypatch, serving, down=[KEEP_NODE])
        _park(overlay, KEEP_NODE)
        block_file = str(tmp_path / "watchdog-block.json")
        monkeypatch.setattr(_lifecycle, "WATCHDOG_BLOCK_FILE", block_file)
        monkeypatch.setattr(ad, "send_macos_notification",
                            lambda *a, **k: True)
        calls = []

        def _run(cmd, timeout=30):
            calls.append(list(cmd))
            return {"ok": True, "output": ""}

        _idle_cfg()
        res = ad.teardown(serving_path=str(tmp_path / "serving.json"),
                          run_cmd_fn=_run, kanban_db=_FakeKb([]),
                          agents_file=_agents_file(tmp_path), now=NOW,
                          prci_checker=_CLEAR_PRCI,
                          http_check_fn=lambda *a, **k: {"ok": False},
                          pauses_directory=overlay)
        return res, calls

    def test_teardown_comes_down_with_the_keepalive_unit_parked(
            self, monkeypatch, overlay, tmp_path):
        """And it goes all the way: one fleet stop, state recorded down.

        This is also the pin that ``teardown()``'s step-1 re-verify reads the
        SAME overlay the timer read — drop the threading and the re-check falls
        back to the default root, sees the unit as sick, and this reddens with
        ``result == "aborted"``.
        """
        res, calls = self._teardown(overlay, tmp_path, monkeypatch)

        assert res["result"] == "down"
        assert calls == [FLEET_STOP_CMD]

    def test_the_parked_unit_is_never_dropped_from_the_stop_plan(
            self, monkeypatch, overlay, tmp_path):
        """Interlock exemption ≠ stop exemption: the parked keepalive unit is
        still in the plan and still verified down by the teardown."""
        res, _calls = self._teardown(overlay, tmp_path, monkeypatch)

        verify_ids = {v["unit_id"] for v in res["plan"][0]["verify"]}
        assert verify_ids == {"orch", "wk1", "wk-keep"}
        assert res["issued"][0]["kind"] == "fleet"
