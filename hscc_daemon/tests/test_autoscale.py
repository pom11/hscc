"""Unit tests for autoscale.py — autoscale decision logic.

Fully isolated: pure functions, no I/O or subprocess calls.
"""
from hscc_daemon.autoscale import decide_scale


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
