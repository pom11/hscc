"""Unit tests for autoscale.py — autoscale decision logic.

Fully isolated: the decision itself is a pure function; the pause-overlay
read goes through node_pauses' ``directory=`` seam into a tmp_path dir and
uses only RFC 5737 documentation hosts (192.0.2.x), never the operator's
``~/.hscc`` and never a real node.
"""
import json

from hscc_daemon import node_pauses
from hscc_daemon.autoscale import decide_scale, host_of, paused_in


def _throughput(waiting=0, running=0, **extra):
    """Helper to build a minimal throughput dict."""
    return {
        "fleet": {
            "waiting": waiting,
            "running": running,
            "nodes_ok": extra.get("nodes_ok", 2),
            "nodes_total": extra.get("nodes_total", 2),
            "prompt_tokens": extra.get("prompt_tokens", 100),
            "generation_tokens": extra.get("generation_tokens", 500),
        },
        "by_node": {},
    }


# ── Node-pause overlay helpers (t_f4bc0540) ────────────────────────────────
#
# Fake hosts only (RFC 5737 documentation range) and a tmp_path overlay dir:
# nothing here can reach the operator's pause record or a real node.

NODE_A = "192.0.2.10"
NODE_B = "192.0.2.11"
NODE_C = "192.0.2.12"


def _metrics_url(host):
    """The by_node key compute_throughput() uses for a node."""
    return f"http://{host}:8000/metrics"


def _snapshot(nodes, **fleet_overrides):
    """A throughput dict from ``{host: (waiting, running)}``.

    The fleet totals are the sum of the per-node numbers, exactly as
    compute_throughput() builds them, so a test that expects a paused node's
    counters to be subtracted is testing the real arithmetic path.
    """
    by_node = {
        _metrics_url(host): {"waiting": waiting, "running": running,
                             "prompt_tokens": 0, "generation_tokens": 0}
        for host, (waiting, running) in nodes.items()
    }
    fleet = {
        "waiting": sum(w for w, _r in nodes.values()),
        "running": sum(r for _w, r in nodes.values()),
        "nodes_ok": len(nodes),
        "nodes_total": len(nodes),
        "prompt_tokens": 0,
        "generation_tokens": 0,
    }
    fleet.update(fleet_overrides)
    return {"by_node": by_node, "fleet": fleet}


# ---------------------------------------------------------------------------
# Scale up
# ---------------------------------------------------------------------------

class TestScaleUp:
    """decide_scale triggers scale_up when queue is backed up."""

    def test_backlog_triggers_scale_up(self):
        tp = _throughput(waiting=5, running=3)
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "scale_up"
        assert result["target"] == 4
        assert "queue depth 5 >= 4" in result["reason"]

    def test_backlog_scale_up_target_is_current_plus_1(self):
        tp = _throughput(waiting=10, running=2)
        result = decide_scale(tp, current_workers=5)
        assert result["target"] == 6

    def test_scale_up_at_high_waiting_boundary(self):
        tp = _throughput(waiting=4, running=0)
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "scale_up"
        assert result["target"] == 4

    def test_no_scale_up_when_below_high_waiting(self):
        tp = _throughput(waiting=3, running=0)
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "none"

    def test_no_scale_up_when_at_max_workers(self):
        tp = _throughput(waiting=10, running=5)
        result = decide_scale(tp, current_workers=8, max_workers=8)
        assert result["action"] == "none"
        assert "within healthy band" in result["reason"]

    def test_scale_up_respects_custom_max(self):
        tp = _throughput(waiting=10, running=2)
        result = decide_scale(tp, current_workers=6, max_workers=7)
        assert result["action"] == "scale_up"
        assert result["target"] == 7

    def test_scale_up_clamps_target_to_max(self):
        tp = _throughput(waiting=10, running=2)
        result = decide_scale(tp, current_workers=7, max_workers=8)
        assert result["action"] == "scale_up"
        assert result["target"] == 8  # min(7+1, 8)


# ---------------------------------------------------------------------------
# Scale down
# ---------------------------------------------------------------------------

class TestScaleDown:
    """decide_scale triggers scale_down only when fleet is fully idle."""

    def test_fully_idle_triggers_scale_down(self):
        tp = _throughput(waiting=0, running=0)
        result = decide_scale(tp, current_workers=5)
        assert result["action"] == "scale_down"
        assert result["target"] == 4
        assert "fleet idle" in result["reason"]

    def test_scale_down_target_is_current_minus_1(self):
        tp = _throughput(waiting=0, running=0)
        result = decide_scale(tp, current_workers=3)
        assert result["target"] == 2

    def test_no_scale_down_at_min_workers(self):
        tp = _throughput(waiting=0, running=0)
        result = decide_scale(tp, current_workers=1, min_workers=1)
        assert result["action"] == "none"

    def test_no_scale_down_when_running_above_zero(self):
        tp = _throughput(waiting=0, running=3)
        result = decide_scale(tp, current_workers=5)
        assert result["action"] == "none"
        assert "within healthy band" in result["reason"]

    def test_no_scale_down_when_waiting_above_low(self):
        tp = _throughput(waiting=2, running=0)
        result = decide_scale(tp, current_workers=5)
        assert result["action"] == "none"

    def test_scale_down_with_custom_min(self):
        tp = _throughput(waiting=0, running=0)
        result = decide_scale(tp, current_workers=3, min_workers=2)
        assert result["action"] == "scale_down"
        assert result["target"] == 2

    def test_scale_down_clamps_target_to_min(self):
        tp = _throughput(waiting=0, running=0)
        result = decide_scale(tp, current_workers=2, min_workers=2)
        assert result["action"] == "none"  # already at min


# ---------------------------------------------------------------------------
# No action (healthy band)
# ---------------------------------------------------------------------------

class TestNoAction:
    """decide_scale returns 'none' when fleet is in a healthy state."""

    def test_busy_but_no_queue(self):
        tp = _throughput(waiting=0, running=5)
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "none"
        assert "within healthy band" in result["reason"]

    def test_light_queue_not_at_threshold(self):
        tp = _throughput(waiting=2, running=2)
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "none"

    def test_low_waiting_boundary(self):
        """waiting exactly at low_waiting but still no action when not idle."""
        tp = _throughput(waiting=0, running=2)
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "none"


# ---------------------------------------------------------------------------
# Robustness — bad input
# ---------------------------------------------------------------------------

class TestRobustness:
    """decide_scale never raises on bad or missing input."""

    def test_none_throughput(self):
        result = decide_scale(None, current_workers=3)
        assert result["action"] == "none"
        assert "no throughput data" in result["reason"]

    def test_empty_dict_throughput(self):
        result = decide_scale({}, current_workers=3)
        assert result["action"] == "none"

    def test_missing_fleet_key(self):
        result = decide_scale({"by_node": {}}, current_workers=3)
        assert result["action"] == "none"

    def test_none_fleet(self):
        result = decide_scale({"fleet": None}, current_workers=3)
        assert result["action"] == "none"

    def test_missing_waiting_key_defaults_to_zero(self):
        """Missing waiting defaults to 0 — idle fleet scales down."""
        tp = {"fleet": {"running": 0}, "by_node": {}}
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "scale_down"

    def test_missing_running_key_defaults_to_zero(self):
        """Missing running defaults to 0 — idle fleet scales down."""
        tp = {"fleet": {"waiting": 0}, "by_node": {}}
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "scale_down"

    def test_empty_fleet_dict_treated_as_no_data(self):
        """An empty fleet dict {} is treated as no throughput data."""
        tp = {"fleet": {}, "by_node": {}}
        result = decide_scale(tp, current_workers=3)
        assert result["action"] == "none"
        assert "no throughput data" in result["reason"]

    def test_non_dict_throughput(self):
        result = decide_scale("garbage", current_workers=3)
        assert result["action"] == "none"

    def test_list_throughput(self):
        result = decide_scale([], current_workers=3)
        assert result["action"] == "none"


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------

class TestCustomThresholds:
    """decide_scale respects custom high_waiting and low_waiting."""

    def test_custom_high_waiting(self):
        tp = _throughput(waiting=10, running=2)
        result = decide_scale(tp, current_workers=3, high_waiting=10)
        assert result["action"] == "scale_up"

    def test_custom_high_waiting_not_met(self):
        tp = _throughput(waiting=9, running=2)
        result = decide_scale(tp, current_workers=3, high_waiting=10)
        assert result["action"] == "none"

    def test_custom_low_waiting(self):
        tp = _throughput(waiting=0, running=0)
        result = decide_scale(tp, current_workers=3, low_waiting=0)
        assert result["action"] == "scale_down"

    def test_low_waiting_above_zero(self):
        """Scale down when waiting <= custom low_waiting (e.g. 2) and running is 0."""
        tp = _throughput(waiting=1, running=0)
        result = decide_scale(tp, current_workers=3, low_waiting=2)
        assert result["action"] == "scale_down"


# ---------------------------------------------------------------------------
# Node-pause overlay: a paused node is not capacity
# ---------------------------------------------------------------------------

class TestPausedNodeIsNotCapacity:
    """A node the overlay holds is out of the worker count and out of the totals."""

    def test_paused_slot_leaves_the_worker_count(self, tmp_path):
        """Two idle nodes with one parked: the decision sees ONE worker.

        Without the seam autoscale would shed another worker (it believes two
        are running); with it the fleet is already at min_workers counting
        only real workers, so the answer is to leave it alone.
        """
        node_pauses.pause(NODE_B, reason="training", directory=tmp_path)
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory=tmp_path)
        assert result["action"] == "none"
        assert result["capacity"] == 1
        assert result["paused_excluded"] == [NODE_B]
        assert "1 paused node excluded" in result["reason"]

    def test_same_input_without_the_pause_is_the_old_decision(self, tmp_path):
        """The other half of every test above: no overlay, no exclusion."""
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory=tmp_path)
        assert result["action"] == "scale_down"
        assert result["target"] == 1
        assert result["capacity"] == 2
        assert result["paused_excluded"] == []
        assert result["reason"] == "fleet idle (no running/queued requests)"

    def test_paused_nodes_leftover_queue_does_not_trigger_scale_up(self, tmp_path):
        """A draining engine's queue depth is not fleet load.

        Without the pause the summed ``waiting`` of 12 trips the scale-up;
        with it the queue came from the parked node and the answer is not
        "add a worker".
        """
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (12, 0)})
        baseline = decide_scale(tp, current_workers=2)
        assert baseline["action"] == "scale_up"

        node_pauses.pause(NODE_B, directory=tmp_path)
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory=tmp_path)
        assert result["action"] == "none"
        assert result["capacity"] == 1
        assert result["paused_excluded"] == [NODE_B]
        assert "queue depth" not in result["reason"]

    def test_paused_nodes_running_does_not_block_scale_down(self, tmp_path):
        """The other direction: a parked node's in-flight requests are idle too."""
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 3), NODE_C: (0, 0)})
        baseline = decide_scale(tp, current_workers=3, paused=set())
        assert baseline["action"] == "none"  # running=3 keeps it steady

        node_pauses.pause(NODE_B, directory=tmp_path)
        result = decide_scale(tp, current_workers=3, min_workers=1,
                              directory=tmp_path)
        assert result["action"] == "scale_down"
        assert result["capacity"] == 2
        assert result["target"] == 1

    def test_two_paused_nodes_shrink_the_capacity(self, tmp_path):
        node_pauses.pause(NODE_B, directory=tmp_path)
        node_pauses.pause(NODE_C, directory=tmp_path)
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0), NODE_C: (0, 0)})
        result = decide_scale(tp, current_workers=3, min_workers=1,
                              directory=tmp_path)
        assert result["capacity"] == 1
        assert result["paused_excluded"] == [NODE_B, NODE_C]
        assert "2 paused nodes excluded" in result["reason"]

    def test_capacity_never_goes_negative(self, tmp_path):
        """A caller that counted fewer workers than there are pauses."""
        node_pauses.pause(NODE_B, directory=tmp_path)
        node_pauses.pause(NODE_C, directory=tmp_path)
        tp = _snapshot({NODE_B: (0, 0), NODE_C: (0, 0)})
        result = decide_scale(tp, current_workers=1, min_workers=1,
                              directory=tmp_path)
        assert result["capacity"] == 0
        assert result["action"] == "none"


# ---------------------------------------------------------------------------
# Node-pause overlay: a paused node is not a candidate
# ---------------------------------------------------------------------------

class TestPausedNodeIsNotACandidate:
    """A scale-up may not aim at a slot only a parked GPU could fill."""

    def test_scale_up_is_blocked_when_the_only_free_node_is_paused(self, tmp_path):
        """max_workers says there is room; the overlay says there is nowhere."""
        tp = _snapshot({NODE_A: (10, 4)})
        baseline = decide_scale(tp, current_workers=1, max_workers=2)
        assert baseline["action"] == "scale_up"
        assert baseline["target"] == 2

        node_pauses.pause(NODE_B, directory=tmp_path)
        result = decide_scale(tp, current_workers=1, max_workers=2,
                              directory=tmp_path)
        assert result["action"] == "none"
        assert "no unpaused node" in result["reason"]
        # The operator must be able to see WHY the backlog gets no worker.
        assert "1 paused" in result["reason"]

    def test_scale_up_target_is_clamped_by_the_paused_ceiling(self, tmp_path):
        """One parked node of four slots: the target cannot reach the config max."""
        tp = _snapshot({NODE_A: (10, 4), NODE_B: (0, 0)})
        baseline = decide_scale(tp, current_workers=3, max_workers=4)
        assert baseline["target"] == 4

        node_pauses.pause(NODE_B, directory=tmp_path)
        result = decide_scale(tp, current_workers=3, max_workers=4,
                              directory=tmp_path)
        assert result["action"] == "scale_up"
        assert result["capacity"] == 2  # NODE_B was one of the three
        assert result["target"] == 3    # ceiling 4-1, not 4

    def test_span_member_is_not_a_candidate_either(self, tmp_path):
        """The node named only in span_nodes is just as parked.

        The pause is keyed to NODE_A (which the snapshot never measured);
        NODE_B appears only in its span_nodes. A seam that asked
        ``paused(host)`` alone would count B as a free worker and place a
        second worker on half of a paused span.
        """
        node_pauses.pause(NODE_A, reason="tp unit",
                          span_nodes=[NODE_A, NODE_B], directory=tmp_path)
        tp = _snapshot({NODE_B: (0, 0), NODE_C: (12, 2)})
        result = decide_scale(tp, current_workers=2, max_workers=2,
                              directory=tmp_path)
        assert result["paused_excluded"] == [NODE_B]
        assert result["capacity"] == 1
        assert result["action"] == "none"
        assert "no unpaused node" in result["reason"]

    def test_scale_down_ignores_the_candidate_rule(self, tmp_path):
        """An idle fleet with a parked node is still idle — shedding a worker
        needs no candidate, so the pause must not freeze the fleet."""
        node_pauses.pause(NODE_B, directory=tmp_path)
        tp = _snapshot({NODE_A: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              max_workers=4, directory=tmp_path)
        assert result["action"] == "scale_down"
        assert result["target"] == 1


# ---------------------------------------------------------------------------
# Node-pause overlay: the seam itself
# ---------------------------------------------------------------------------

class TestPauseSeam:
    """How the overlay is consulted, and what a broken one must not do."""

    def test_paused_argument_skips_the_read(self, tmp_path):
        """An explicit ``paused`` is authoritative — the file is not consulted."""
        node_pauses.pause(NODE_B, directory=tmp_path)
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              paused=set(), directory=tmp_path)
        assert result["capacity"] == 2
        assert result["paused_excluded"] == []

    def test_paused_argument_accepts_a_single_host(self):
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              paused=NODE_B)
        assert result["capacity"] == 1

    def test_paused_argument_that_is_not_a_host_is_warned_not_swallowed(self, capsys):
        """A caller handing over junk gets the unpaused decision AND a WARN.

        The silent version of this is the shape that has burned this fleet
        before: a seam that quietly degrades to the old behaviour and looks
        green. An empty collection is the legitimate "nothing paused" and must
        NOT warn, or the warning is noise nobody reads.
        """
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        for junk in (42, object(), 0):
            result = decide_scale(tp, current_workers=2, min_workers=1,
                                  paused=junk)
            assert result["capacity"] == 2
            assert result["paused_excluded"] == []
            err = capsys.readouterr().err
            assert "[autoscale] WARN" in err and "not a collection" in err

        for legitimate_empty in (set(), [], {}, frozenset(), ""):
            result = decide_scale(tp, current_workers=2, min_workers=1,
                                  paused=legitimate_empty)
            assert result["capacity"] == 2
            assert capsys.readouterr().err == ""

    def test_paused_argument_accepts_a_pause_mapping(self):
        """list_pauses()/paused_nodes()-shaped input, not just a bare set."""
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        mapping = {NODE_B: node_pauses.pause(NODE_B)}
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              paused=mapping)
        assert result["capacity"] == 1
        assert result["paused_excluded"] == [NODE_B]

    def test_malformed_overlay_decides_as_unpaused(self, tmp_path):
        """A corrupt overlay is node_pauses' loud problem, not a raised here."""
        (tmp_path / "node_pauses.json").write_text("{ not json")
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory=tmp_path)
        assert result == decide_scale(tp, current_workers=2, min_workers=1,
                                      paused=set())

    def test_expired_pause_still_holds_the_node(self, tmp_path):
        """paused() is clock-independent: an overdue resume is still a pause.

        The record's ``until`` is in the past but the daemon's auto-resume has
        not run yet, so the node is still promised to the operator — autoscale
        must not treat it as free capacity a few seconds before the resume.
        """
        past = "2000-01-01T00:00:00+00:00"
        entry = node_pauses.pause(NODE_B, until=past, directory=tmp_path)
        assert node_pauses.is_expired(entry)  # overdue...
        assert node_pauses.paused(NODE_B, directory=tmp_path) is not None  # ...unresumed
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory=tmp_path)
        assert result["paused_excluded"] == [NODE_B]
        assert result["capacity"] == 1

    def test_decision_shape_is_uniform(self):
        """Every action carries capacity + paused_excluded, pause or no pause."""
        empty = set()
        for tp, workers in ((_snapshot({NODE_A: (10, 4)}), 1),
                            (_snapshot({NODE_A: (0, 0), NODE_B: (0, 0)}), 2),
                            (_snapshot({NODE_A: (1, 1)}), 1)):
            for result in (decide_scale(tp, current_workers=workers),
                           decide_scale(tp, current_workers=workers,
                                        paused=empty)):
                assert result["capacity"] == workers
                assert result["paused_excluded"] == []
                assert result["reason"]

    def test_paused_in_reports_only_measured_hosts(self, tmp_path):
        """A parked node the snapshot never saw is not 'excluded capacity'."""
        node_pauses.pause(NODE_B, directory=tmp_path)
        tp = _snapshot({NODE_A: (0, 0)})
        assert paused_in(tp, directory=tmp_path) == set()
        assert paused_in(_snapshot({NODE_A: (0, 0), NODE_B: (0, 0)}),
                         directory=tmp_path) == {NODE_B}

    def test_overlay_is_read_from_the_directory_seam_only(self, tmp_path):
        """A pause in another root cannot reach this decision."""
        other = tmp_path / "other"
        node_pauses.pause(NODE_B, directory=other)
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory=tmp_path / "empty")
        assert result["capacity"] == 2
        assert result["paused_excluded"] == []

    def test_unreadable_overlay_warns_instead_of_500ing(self, monkeypatch, capsys):
        """An overlay that cannot be read at all is a WARN, not a traceback.

        ``node_pauses.load()`` swallows malformed content itself, so this is
        the remaining shape: the read exploding. The read-only route must
        still answer, and must say the pause was not consulted.
        """
        from hscc_daemon import node_pauses as np_mod

        def explode(directory=None):
            raise OSError("overlay unreadable")

        monkeypatch.setattr(np_mod, "paused_nodes", explode)
        tp = _snapshot({NODE_A: (0, 0), NODE_B: (0, 0)})
        result = decide_scale(tp, current_workers=2, min_workers=1,
                              directory="somewhere")
        assert result["capacity"] == 2
        assert result["paused_excluded"] == []
        err = capsys.readouterr().err
        assert "[autoscale] WARN" in err and "overlay unreadable" in err

    def test_overlay_file_is_never_written(self, tmp_path):
        """Autoscale is a reader. A decision that paused a node would be a bug."""
        before = json.dumps(sorted(p.name for p in tmp_path.iterdir()))
        decide_scale(_snapshot({NODE_A: (0, 0)}), current_workers=1,
                     directory=tmp_path)
        assert json.dumps(sorted(p.name for p in tmp_path.iterdir())) == before


# ---------------------------------------------------------------------------
# host_of — the endpoint/host normalisation the seam depends on
# ---------------------------------------------------------------------------

class TestHostOf:
    """by_node keys are URLs, pause records are hosts: one comparable key."""

    def test_metrics_url_yields_the_host(self):
        assert host_of(_metrics_url(NODE_A)) == NODE_A

    def test_host_without_scheme_or_port(self):
        assert host_of(NODE_A) == NODE_A

    def test_host_with_port_and_no_scheme(self):
        assert host_of(f"{NODE_A}:8000") == NODE_A

    def test_case_is_not_part_of_a_host(self):
        assert host_of("http://DGX-01:8000/metrics") == "dgx-01"
        assert host_of("DGX-01") == "dgx-01"

    def test_bracketed_ipv6(self):
        """RFC 3849 documentation prefix, the IPv6 twin of the 192.0.2.x hosts."""
        assert host_of("http://[2001:db8::1]:8000/metrics") == "2001:db8::1"

    def test_usable_values(self):
        assert host_of(None) is None
        assert host_of("") is None
        assert host_of("   ") is None
        assert host_of(42) is None
        assert host_of("http://") is None
