"""metrics.py — compute fleet quality metrics from kanban cards + git.

This module turns facts flightdeck already has into measured claims about
card quality. Everything we believe about how cards actually perform is
currently anecdotal; this is the number behind the story.

Five metrics, each computed from *injected* card + event + git inputs so the
logic is pure and independently testable (no board, no git, no network here):

  - first-time-pass rate: cards whose branch merged without being sent back
    (never re-blocked after a review) / all cards reviewed in the window.
  - stall rate: cards that started and ran past a threshold with zero commits
    / all cards started in the window.
  - review latency: median and p90 of time from "blocked for review" to
    merged, over cards whose branch merged in the window.
  - throughput: cards merged per day over the window.
  - rework: cards that needed more than one review round.

Every figure that is a rate/percentage/quantile is gated behind a minimum
sample size (:data:`MIN_SAMPLE_SIZE`). A rate computed from a handful of
cards must never be presented with the same authority as one computed from
hundreds; below the minimum the caller renders "insufficient data (n=N)"
rather than a misleading 0% or 100%.

The honesty rule is absolute: a figure computed from zero data is reported as
insufficient, NEVER as 0% or 100%. ``compute`` returns rate fields as ``None``
(never a fabricated 0.0 or 1.0) when the sample is too small, and the command
layer renders that as insufficient-data.
"""

from __future__ import annotations

import statistics
from typing import Optional

# A rate/quantile is only reported once this many observations back it.
# Below this the caller prints "insufficient data (n=N)" — a figure from a
# handful of cards is a data point, not a rate.
MIN_SAMPLE_SIZE = 3

# Event kinds that mark a card ENTERING review (a "round" begins). Hermes
# posts ``submitted_for_review`` when a worker hands work off and ``blocked``
# for a review wait; either opens a review round.
REVIEW_ENTRY_EVENTS = frozenset({"submitted_for_review", "blocked"})

# The event kind that means a review sent the card BACK for rework.
SENT_BACK_EVENT = "unblocked"

# Archive is settled, not part of the live review/stall story. Metrics is the
# deliberate exception: it computes over COMPLETED history, and completed cards
# are archived by the normal review flow — so metrics counts them. The other
# commands (standup/qa/reconcile) still exclude archived cards at their reader.
# ``min_sample``-gated rates are the independence that lets the windowed
# populations include archived cards without a stale card skewing a rate.

def _coerce_ts(value) -> Optional[int]:
    """Coerce a timestamp to int, or None when absent/unparseable."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _derive(card: dict) -> dict:
    """Per-card derived facts from events + card fields (no I/O)."""
    events = card.get("events") or []
    review_entries = [
        _coerce_ts(e.get("created_at"))
        for e in events
        if isinstance(e, dict) and e.get("kind") in REVIEW_ENTRY_EVENTS
    ]
    review_entries = [ts for ts in review_entries if ts is not None]
    sent_back_ts = [
        _coerce_ts(e.get("created_at"))
        for e in events
        if isinstance(e, dict) and e.get("kind") == SENT_BACK_EVENT
    ]
    sent_back_ts = [ts for ts in sent_back_ts if ts is not None]

    # ``rework`` (needed more than one round) is ``review_entries > 1`` — the
    # card was blocked/reviewed again after a first round. ``sent_back`` is the
    # distinct, earlier signal: an ``unblocked`` event after the first review —
    # it fails first-time-pass even before it re-enters review.
    rework = len(review_entries) > 1
    sent_back_after_first = bool(
        sent_back_ts and review_entries and sent_back_ts[0] > review_entries[0]
    )
    return {
        "review_entries": review_entries,
        "rework": rework,
        "sent_back": sent_back_after_first,
        "first_review_ts": review_entries[0] if review_entries else None,
        "last_review_ts": review_entries[-1] if review_entries else None,
    }


def _started_at(card: dict) -> Optional[int]:
    return _coerce_ts(card.get("started_at"))


def _completed_at(card: dict) -> Optional[int]:
    return _coerce_ts(card.get("completed_at"))


def compute(
    cards: list[dict],
    *,
    since_ts: float,
    now: float,
    stale_threshold_seconds: int = 2700,
    min_sample: int = MIN_SAMPLE_SIZE,
) -> dict:
    """Compute quality metrics over ``cards`` for the window ``[since_ts, now]``.

    ``cards`` is a list of prepared per-card dicts carrying at least ``id``,
    ``status``, ``started_at``, ``completed_at``, plus ``is_merged``,
    ``commits_ahead`` and ``events`` (a list of ``{kind, created_at}`` dicts in
    any order) — the facts resolved by the caller from git + the board. This
    function is pure: it never reads a board, never runs git, never touches the
    network.

    Metrics computes over COMPLETED history, so archived cards are deliberately
    INCLUDED — a merged/closed card is archived by the normal review flow, and
    excluding it would make every figure n=0 (the exact bug this fixes). Only
    cards whose ``started_at``/``completed_at``/review-conclusion falls inside
    the window count; a card outside the window never appears regardless of
    status.

    The window is honoured per population:

      * merged cards  — ``completed_at`` inside ``[since_ts, now]``;
      * started cards — ``started_at`` inside the window;
      * reviewed cards — the review CONCLUDED in the window: the card merged
        inside the window, or was sent back (an ``unblocked`` event) inside
        the window.

    Returns a structured dict (see the renderers for the string/JSON mirror)
    where every rate field is ``None`` when the sample is below ``min_sample``
    — never a fabricated 0% or 100%.
    """
    since = float(since_ts)
    end = float(now)

    merged_in_window: list[dict] = []
    started_in_window: list[dict] = []
    for card in cards:
        # Decorate the SAME card object in place so the derived facts travel
        # with it into whatever population list it lands in (each list holds
        # the original objects, not copies — identity is what keeps ``_m``
        # reachable from every list).
        card["_m"] = _derive(card)

        comp = _completed_at(card)
        if comp is not None and since <= comp <= end:
            merged_in_window.append(card)

        start = _started_at(card)
        if start is not None and since <= start <= end:
            started_in_window.append(card)

    # --- reviewed: merged in window, or sent back in window ----------------- #
    # A card is "reviewed in the window" when its review CONCLUDED inside the
    # window: either the branch merged (completed within the window), or it was
    # sent back for rework (an ``unblocked`` event within the window). Both are
    # resolved from the card itself — not from ``started_in_window`` — so a
    # card that started before the window but was reviewed/sent back inside it
    # still counts.
    reviewed: list[dict] = []
    seen: set = set()
    for card in cards:
        comp = _completed_at(card)
        merged_here = comp is not None and since <= comp <= end
        sent_here = any(
            since <= (ts or -1) <= end
            for e in (card.get("events") or [])
            for ts in [_coerce_ts(e.get("created_at"))]
            if isinstance(e, dict) and e.get("kind") == SENT_BACK_EVENT and ts is not None
        )
        if (merged_here or sent_here) and id(card) not in seen:
            reviewed.append(card)
            seen.add(id(card))

    # --- first-time-pass ---------------------------------------------------- #
    # Numerator: cards that merged and were NEVER sent back after a review.
    ftp_cards = [
        c for c in reviewed
        if c.get("is_merged") and not c["_m"]["sent_back"]
    ]

    # --- stalled ------------------------------------------------------------ #
    stalled: list[dict] = []
    for card in started_in_window:
        if card.get("is_merged"):
            continue  # merged in the window is never stalled — work landed
        if int(card.get("commits_ahead") or 0) != 0:
            continue  # making progress, not stalled
        start = _started_at(card)
        if start is None:
            continue
        comp = _completed_at(card)
        effective_end = comp if comp is not None else end
        elapsed = effective_end - start
        if elapsed > stale_threshold_seconds:
            stalled.append(card)

    # --- review latency ----------------------------------------------------- #
    latencies: list[float] = []
    for card in merged_in_window:
        comp = _completed_at(card)
        last_rev = card["_m"]["last_review_ts"]
        if comp is None or last_rev is None:
            continue
        latencies.append(float(max(0, comp - last_rev)))

    days = (end - since) / 86400.0
    n_merged = len(merged_in_window)
    n_started = len(started_in_window)
    n_reviewed = len(reviewed)

    def rate(count: int, n: int) -> Optional[float]:
        if n < min_sample or n == 0:
            return None
        return count / n

    return {
        "window": {"since": since, "now": end, "days": days},
        "reviewed": n_reviewed,
        "first_time_pass": {
            "n": n_reviewed,
            "count": len(ftp_cards),
            "rate": rate(len(ftp_cards), n_reviewed),
        },
        "started": n_started,
        "stalled": {
            "n": n_started,
            "count": len(stalled),
            "rate": rate(len(stalled), n_started),
        },
        "review_latency": {
            "n": len(latencies),
            "median": _quantile_or_none(latencies, 0.5, min_sample),
            "p90": _quantile_or_none(latencies, 0.9, min_sample),
        },
        "throughput": {
            "n": n_merged,
            "per_day": (n_merged / days) if (days > 0 and n_merged >= min_sample) else None,
        },
        "rework": {
            "n": n_reviewed,
            "count": len([c for c in reviewed if c["_m"]["rework"]]),
            "share": rate(
                len([c for c in reviewed if c["_m"]["rework"]]), n_reviewed
            ),
        },
        "merged_count": n_merged,
    }


def _quantile_or_none(
    values: list[float], q: float, min_sample: int
) -> Optional[float]:
    """The ``q`` quantile of ``values``, or None below ``min_sample``."""
    if len(values) < min_sample:
        return None
    return float(statistics.quantiles(values, n=100)[int(q * 100) - 1])
