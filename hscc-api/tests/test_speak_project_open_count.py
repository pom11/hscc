"""`open cards` must not count the `total` summary key.

_project_detail builds board_counts as per-status counts PLUS a summary
``total`` key (board_counts["total"] = len(cards)). _speak_project_detail then
summed every key that was not done/archived/blocked — including ``total`` — so
the spoken "N open cards" was inflated by the size of the whole board.

On the live hscc board (2 running, 3 ready, 6 blocked, 180 done, 191 total)
that reported 196 open cards when the true answer is 5.
"""

import sys

import api_server  # noqa: F401  (import first: routes_project imports from it)
import routes_project as rp


def _detail(counts, name="hscc", board="hscc"):
    return {"name": name, "board": board, "board_counts": counts}


def test_total_key_is_not_counted_as_open():
    counts = {"running": 2, "ready": 3, "blocked": 6, "done": 180, "total": 191}
    speak = rp._speak_project_detail(_detail(counts))
    assert "5 open cards" in speak, speak
    assert "196" not in speak, "the total summary key leaked into the open count"
    assert "2 running" in speak


def test_closed_statuses_stay_excluded():
    counts = {"running": 1, "done": 40, "archived": 12, "blocked": 3, "total": 56}
    speak = rp._speak_project_detail(_detail(counts))
    assert "1 open cards" in speak, speak


def test_unknown_open_status_still_counts():
    """The filter names what is NOT open, so a new open status still counts."""
    counts = {"running": 1, "review": 2, "total": 3}
    speak = rp._speak_project_detail(_detail(counts))
    assert "3 open cards" in speak, speak


def test_empty_board_reads_zero():
    speak = rp._speak_project_detail(_detail({"total": 0}))
    assert "0 running, 0 open cards" in speak, speak
