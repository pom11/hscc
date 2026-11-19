"""HSCC HTTP API — the metrics-history read route (``GET /v1/metrics/history``).

Exposes the three tiers ``hscc_daemon/metrics_history.py`` already keeps, so a
widget can draw a chart instead of re-reading one 15-minute snapshot:

  * ``tier=0`` — the 2 s ring (15 min), the recorder's own
    ``MetricsRecorder.tier0_points()``.
  * ``tier=1`` — 1-minute roll-ups, 24 h.
  * ``tier=2`` — 5-minute roll-ups, 7 d.

    ``GET /v1/metrics/history?tier=&host=&node=&since=&until=``

Field names are the recorder's — sparkrun's ``MonitorSample`` names verbatim
(``cpu_usage_pct``, ``gpu_temp_c``, ``gpu_power_w``, ``gpu_mem_used_mb`` …) plus
the roll-up's own ``<field>_max`` peak columns. This module **never renames and
never coalesces**: a field the node did not report stays ``None``/absent rather
than becoming ``0``, because "0 °C" and "no answer" are different facts and a
chart that draws the second as the first shows an idle node where there is a
dead one. Same reason the peaks ride along instead of being folded into the
averages: a 1-minute average erases the throttle spike the peak column exists
for.

TIER 0 ACROSS PROCESSES — read this before writing a chart.
-------------------------------------------------------------------------
Tier 0 lives in *memory*, and memory belongs to a process. The recorder runs in
the daemon (``hscc.py start-daemon``, launchd ``com.hermes.hscc_daemon``); this
API is a different process (``python -m api_server serve``, launchd
``com.hscc.api``). So the route can hand back the ring **only when this process
holds it**, and in today's deployment it does not. What it can hand back is the
daemon's own answer: the ``metrics_history`` stream stamp carries the
recorder's ``stats()`` fields, so the response always says
``tier0: {points: N, readable_here: false, reason: …}``. A widget that gets an
empty tier-0 window therefore learns "this process cannot see the ring (or the
daemon just restarted, which is the same empty window by spec)" instead of
rendering "nothing happened" — the misreading the card names.

That gap is the recorder's design decision (memory-only tier 0, one disk
transaction a minute), not something this route may quietly work around. The
smallest addition that closes it is filed as a follow-up card; until then the
chart's live 2 s arm must be drawn from tiers 1/2, and the widget says so.

Tiers 1 and 2 need no such caveat: they live in one sqlite file
(``~/.hscc/metrics/history.db``) that any process may read.

Nothing here samples the fleet: no ssh, no ``sparkrun`` child, no
``ServingPoller``. The one function that could accidentally start all three is
``metrics_history.get_sampler()`` — it CONSTRUCTS on first use — so this module
reads the process-wide sampler and deliberately never calls it.
``tests/test_routes_metrics.py`` pins that with a seam that raises.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from api_server import ApiError, ROUTES

# Make hscc_daemon importable (sibling of hscc-api/) — same seam as
# routes_cluster.py::_ensure_repo_root_on_path / routes_autodown.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

#: The request axis. ``tier`` is REQUIRED rather than defaulted: "which
#: resolution am I looking at" is the one thing a chart may not guess, and a
#: silent default is how a 2 s widget starts rendering 5-minute averages.
TIERS = (0, 1, 2)

#: Why the stamp can go stale/absent, in the widget's words. Kept here (not in
#: Swift) so both clients say the same thing.
REASON_RING_IN_OTHER_PROCESS = (
    "tier 0 is an in-memory ring and this API process does not hold it — the "
    "recorder runs in the daemon process. The point count reported here comes "
    "from the daemon's own stream stamp."
)
REASON_NO_STAMP = (
    "no metrics_history stream stamp exists yet — the daemon has not run the "
    "recorder, so tier 0 has no depth to report."
)
REASON_FRESH_RECORDER = (
    "the recorder holds no points yet: it fills from the first sample after "
    "the daemon starts, so an empty tier 0 right after a restart is expected."
)


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_metrics_history():
    """``hscc_daemon.metrics_history`` — the ONLY source of the tier shapes."""
    from hscc_daemon import metrics_history
    return metrics_history


def _backing_store():
    """A fresh ``MetricsStore``.

    ``hscc_dir`` is left at None on purpose: the store resolves ``~/.hscc`` at
    CALL time (``metrics_history.hscc_root``) so the suite's autouse redirect
    covers it with nothing to register here.
    """
    return _backing_metrics_history().MetricsStore()


def _backing_recorder():
    """The process-wide recorder IF this process already has one, else None.

    Deliberately NOT ``metrics_history.get_sampler()``: that CONSTRUCTS a
    ``Sampler`` on first use, and a Sampler spawns ``sparkrun cluster monitor``
    and opens a second writer on the roll-up watermarks — the exact hazard
    ``get_sampler``'s own docstring warns about. A read route must never be able
    to start sampling.
    """
    mh = _backing_metrics_history()
    sampler = getattr(mh, "_sampler", None)       # READ, never construct
    return getattr(sampler, "recorder", None)


def _backing_stream_state():
    """The daemon's ``metrics_history`` stamp, or None.

    This is how a second process learns tier 0's DEPTH (``tier0_points``) when
    it cannot read the ring itself — the same cross-process channel
    ``routes_cluster.handle_fleet_streams`` uses.
    """
    from hscc_daemon import state
    return state.read_state(_backing_metrics_history().STREAM_NAME)


# --------------------------------------------------------------------------- #
# Query parsing — every rejection is a 400 the client can act on
# --------------------------------------------------------------------------- #

def _parse_tier(query) -> int:
    raw = (query or {}).get("tier")
    if raw is None or str(raw).strip() == "":
        raise ApiError(
            400, "bad_request",
            "tier is required and must be 0, 1 or 2 "
            "(0 = 2 s ring / 15 min, 1 = 1-minute rollups / 24 h, "
            "2 = 5-minute rollups / 7 d)",
            "History needs a tier: 0, 1 or 2.",
        )
    try:
        tier = int(str(raw).strip())
    except (TypeError, ValueError):
        tier = -1
    if tier not in TIERS:
        raise ApiError(
            400, "bad_request",
            f"tier must be 0, 1 or 2 (got {raw!r})",
            "History tier must be 0, 1 or 2.",
        )
    return tier


def _parse_epoch(query, name) -> int | None:
    """``?since=`` / ``?until=`` — inclusive epoch SECONDS, as int bucket starts.

    The store's own buckets are integer second offsets aligned to the wall
    clock, so a float is floored rather than rounded: flooring keeps the
    "inclusive bucket start" meaning the store documents.
    """
    raw = (query or {}).get(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        raise ApiError(
            400, "bad_request",
            f"{name} must be epoch seconds (got {raw!r})",
            f"History {name} must be epoch seconds.",
        )


def _parse_name(query, name) -> str | None:
    raw = (query or {}).get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip()


# --------------------------------------------------------------------------- #
# Shape helpers — pass the recorder's fields through, do not rename them
# --------------------------------------------------------------------------- #

def _tier_meta(mh, tier: int) -> dict:
    """Resolution + retention of one tier, read from the module that owns them.

    Hardcoding "60" and "86400" here would let the response lie the day the
    cadence changes, so the numbers come from the recorder and a test pins the
    pair.
    """
    if tier == 0:
        return {"tier": 0, "resolution_s": mh.TIER0_INTERVAL_S,
                "window_s": mh.TIER0_WINDOW_S, "capacity": mh.TIER0_POINTS}
    if tier == 1:
        return {"tier": 1, "resolution_s": mh.TIER1_BUCKET_S,
                "window_s": mh.TIER1_WINDOW_S, "capacity": None}
    return {"tier": 2, "resolution_s": mh.TIER2_BUCKET_S,
            "window_s": mh.TIER2_WINDOW_S, "capacity": None}


def _field_names(mh) -> dict:
    """The field vocabulary, so a client never has to hardcode a column list.

    ``host_peaks`` is the part a chart must not miss: every name there is an
    AVERAGE's peak, stored beside it (``gpu_temp_c`` is the mean of the bucket,
    ``gpu_temp_c_max`` is what it actually reached).
    """
    return {
        "host": list(mh.HOST_KEYS),
        "host_peaks": [f"{k}_max" for k in mh.MAX_KEYS],
        "engine": list(mh.ENGINE_KEYS),
        "engine_key": list(mh.ENGINE_KEY_COLUMNS),
        "engine_identity": "node+port (model is an attribute of the port)",
    }


def _flatten_tier0(points, host, node) -> tuple[list, list]:
    """The ring's records -> the same flat row shape tiers 1 and 2 return.

    ``MetricsRecorder.tier0_points()`` hands back RECORDS (one per 2 s tick,
    each carrying a ``hosts`` map and an ``engines`` list), while the store
    hands back FLAT rows (one per subject, one per bucket). A chart wants one
    row shape, so the record is opened up here — and only opened up: the keys
    inside it (``cpu_usage_pct``, ``run``, ``kv`` …) are copied unchanged, and
    ``ts`` / ``res`` / ``n`` come from the record as they are. A tier-0 row has
    no ``<field>_max`` because a single sample has no peak to report; inventing
    ``x_max = x`` would be the route writing data the recorder never sampled.
    """
    host_rows, engine_rows = [], []
    for p in points or []:
        base = {"tier": 0, "ts": p.get("ts"), "res": p.get("res"),
                "n": p.get("n")}
        for name, metrics in (p.get("hosts") or {}).items():
            if host is not None and str(name) != host:
                continue
            row = dict(base)
            row["host"] = name
            row.update(metrics or {})          # verbatim; no rename, no default
            host_rows.append(row)
        for e in p.get("engines") or []:
            if node is not None and str(e.get("node")) != node:
                continue
            row = dict(base)
            row.update(e or {})                # node, port, model, run, wait…
            engine_rows.append(row)
    return host_rows, engine_rows


def _stamp_age_s(stamp) -> float | None:
    """How old the daemon's stamp is, in seconds (None when unknowable).

    The recorder stamps at the 2 s cadence, so this is the difference between
    "the daemon is alive and its ring is empty" and "the daemon is gone and this
    number is a fossil". ``state.write_state`` stamps every entry with
    ``now_iso()``.
    """
    raw = (stamp or {}).get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    import datetime
    try:
        when = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, round(
        (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds(), 1))


def _tier0_block(mh, recorder, stamp, host) -> dict:
    """Tier 0's depth + whether THIS process can hand back its points.

    Three states, and the response distinguishes all three: the ring is here and
    filled; the ring is here and empty (fresh daemon); or the ring is in another
    process and only its depth is knowable. Collapsing them is the "nothing
    happened" misreading.
    """
    if recorder is not None:
        stats = recorder.stats()
        points = stats.get("tier0_points", 0)
        block = {
            "points": points,
            "capacity": stats.get("tier0_capacity", mh.TIER0_POINTS),
            "readable_here": True,
            "source": "recorder_ring",
            "reason": (REASON_FRESH_RECORDER if not points else None),
            "sampler_error": stats.get("sampler_error"),
            "store_error": stats.get("error"),
            "stamp_age_s": None,
        }
        return block
    if stamp is None:
        return {"points": None, "capacity": mh.TIER0_POINTS,
                "readable_here": False, "source": "no_stream_stamp",
                "reason": REASON_NO_STAMP, "sampler_error": None,
                "store_error": None, "stamp_age_s": None}
    return {
        "points": stamp.get("tier0_points"),
        "capacity": mh.TIER0_POINTS,
        "readable_here": False,
        "source": "daemon_stream_stamp",
        "reason": REASON_RING_IN_OTHER_PROCESS,
        "sampler_error": stamp.get("error"),
        "store_error": stamp.get("error"),
        "stamp_age_s": _stamp_age_s(stamp),
    }


def _speak_history(payload: dict) -> str:
    tier = payload["tier"]
    hosts = payload["counts"]["host_rows"]
    engines = payload["counts"]["engine_rows"]
    res = payload["tier_meta"]["resolution_s"]
    window = payload["tier_meta"]["window_s"]
    hours = round(window / 3600, 1)
    lead = (f"History tier {tier}: {res}-second buckets over {hours} hours, "
            f"{hosts} node rows and {engines} engine rows.")
    t0 = payload["tier0"]
    if tier == 0 and not t0["readable_here"]:
        lead += (" The 2-second ring lives in the daemon process, so its points"
                 " are not served from here; the depth reported is the"
                 " recorder's own.")
    elif tier == 0:
        lead += ("" if t0["points"] else
                 " The ring is empty — the daemon has not sampled since it"
                 " started.")
    if payload.get("error"):
        lead += f" Store problem: {payload['error']}."
    return lead


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_metrics_history(server, ctx, query, body):
    """GET /v1/metrics/history — the three tiers, oldest bucket first.

    Params: ``tier`` (required, 0|1|2), ``host`` (filter node rows),
    ``node`` (filter engine rows), ``since`` / ``until`` (inclusive epoch-second
    bucket starts). ``host`` and ``node`` are the two names the same subject has
    in the two tables — ``host_rollup.host`` and ``engine_rollup.node`` — so
    they filter their own table, and passing both narrows each half.
    """
    mh = _backing_metrics_history()
    tier = _parse_tier(query)
    since = _parse_epoch(query, "since")
    until = _parse_epoch(query, "until")
    host = _parse_name(query, "host")
    node = _parse_name(query, "node")

    payload: dict = {
        "tier": tier,
        "tier_meta": _tier_meta(mh, tier),
        "since": since,
        "until": until,
        "host": host,
        "node": node,
        "source": None,
        "hosts": [],
        "engines": [],
        "counts": {"host_rows": 0, "engine_rows": 0},
        "fields": _field_names(mh),
        "tier0": None,
        "stats": None,
        "error": None,
    }

    recorder = _backing_recorder()
    payload["tier0"] = _tier0_block(mh, recorder, _backing_stream_state(), host)

    store = None
    try:
        if tier == 0:
            if recorder is None:
                # Nothing to read: tier 0 is memory and this process has none of
                # it. Rows stay empty and `tier0.reason` says why — the caller
                # must not have to infer it from a zero-length list.
                payload["source"] = "recorder_ring_absent"
            else:
                payload["source"] = "recorder_ring"
                points = recorder.tier0_points(host=host)
                payload["hosts"], payload["engines"] = _flatten_tier0(
                    points, host, node)
        else:
            store = _backing_store()
            if not os.path.exists(store.path):
                # Report rather than open: `MetricsStore.connect()` creates the
                # file and its schema, and a GET must not be able to write to
                # the operator's state dir.
                payload["source"] = "sqlite_store_absent"
                payload["stats"] = {"path": store.path, "db_bytes": 0,
                                    "bound_bytes": mh.size_bound_bytes(),
                                    "tier1_rows": 0, "tier2_rows": 0,
                                    "error": None, "exists": False}
            else:
                payload["source"] = "sqlite_store"
                payload["hosts"] = store.host_rows(tier, since=since,
                                                   until=until, host=host)
                payload["engines"] = store.engine_rows(tier, since=since,
                                                       until=until, node=node)
                payload["stats"] = store.stats()
                payload["stats"]["exists"] = True
                payload["error"] = payload["stats"].get("error")
    except Exception as exc:
        # A read route reports a problem, it does not 500 on one: the widget's
        # fallback is its snapshot, and a status code is not what tells it the
        # history is unavailable (routes_autodown.handle_autodown_status shape).
        payload["error"] = f"{type(exc).__name__}: {exc}"

    payload["counts"] = {"host_rows": len(payload["hosts"]),
                         "engine_rows": len(payload["engines"])}
    payload["speak"] = _speak_history(payload)
    return 200, payload


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/metrics/history$"),
               handle_metrics_history))
