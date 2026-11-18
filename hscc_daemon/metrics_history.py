"""Cluster + serving metrics history: three bounded tiers under ``~/.hscc``.

WHY THIS EXISTS. There is no metrics history anywhere today:
``/v1/daemon/history`` is the automated-action timeline (autodown/autoup
decisions), not telemetry, and the widgets refresh ~every 15 minutes, so
anything that happened between two refreshes is simply gone. History has to be
written by the one process that is always up and always looking at the fleet —
the daemon — and it has to survive that daemon's own restarts.

THE THREE TIERS (the operator's spec; each coarser tier ROLLS UP from the finer
one and is never sampled separately):

    tier 0   every 2 s, last 15 min (~450 points per node) — IN MEMORY ONLY.
             Loss on restart is accepted by design: the reason to keep it out of
             the store is that a 2 s cadence then never touches the disk.
    tier 1   1-minute averages + max for the temperatures and power, last 24 h,
             on disk.
    tier 2   5-minute averages + max, last 7 days, on disk.

WHERE THE SAMPLES COME FROM — the monitor's OWN code path, deliberately.
``hscc cluster monitor --live`` (``cluster_live.py``) already maintains exactly
the two sources this recorder needs, at exactly this cadence
(``cluster_live.DEFAULT_INTERVAL`` is 2 s):

* ``cluster_live.HostStream`` — ONE long-running ``sparkrun cluster monitor
  --json`` child. sparkrun holds its ssh channels open for the life of that
  child and streams one CSV row per host per interval, so a tick here is a read
  of the newest line that already arrived: **zero new ssh per tick.**
* ``cluster_live.ServingPoller`` — the proxy's ``/v1/model/info`` route map
  (refreshed on its own 30 s timer) plus each backend's vLLM ``/metrics``,
  giving requests running/waiting, KV-cache use and the token counters, with the
  per-tick *rates* already derived from those cumulative counters by
  ``cluster_live.rates``.

This module imports those two classes and consumes what they already produce.
It contains no SSH, subprocess or sparkrun call of its own; adding a second
poller would double the SSH fan-out per node per tick and give the fleet two
disagreeing views of one sample. ``tests/test_metrics_history.py`` pins it both
ways — an AST sweep for process/socket use, and an IDENTITY check that the
sampler's source classes are ``cluster_live``'s.

WHAT A POINT CARRIES. Field names are sparkrun's ``MonitorSample`` names
verbatim (plus the derived ``swap_used_pct`` the monitor renders), so a reader
never has to translate and a rename upstream shows up as a missing key rather
than a silently wrong one. Values are floats — sparkrun's CSV strings are parsed
exactly once, here, and an unreported reading stays ``None``: coercing ``""`` to
``0.0`` would put a fake "GPU idle, 0 W" into history for a node that never
answered.

ON DISK: ONE sqlite FILE, not JSON per sample. ``~/.hscc/metrics/history.db``,
two ``WITHOUT ROWID`` tables keyed ``(tier, ts, host)`` / ``(tier, ts, node,
port, model)`` — the row lives IN its primary key, so there is no second index
copying it and no per-sample text serialisation. Retention is a ``DELETE`` past
each tier's window, and ``size_bound_bytes()`` is the number the PR body quotes
(measured, pinned by ``test_the_store_stays_inside_the_stated_bound``).

RESTART BEHAVIOUR, stated plainly: tiers 1 and 2 are durable (that is what the
file is for — proven by ``test_tier1_and_tier2_survive_a_restart``), and
**tier 0 is empty immediately after a restart** — the 2 s ring is memory-only by
spec, so the first minute after a restart has no in-memory points until samples
arrive again. Nothing pretends otherwise: ``stats()`` reports tier 0's depth and
a fresh recorder reports 0.
"""

from __future__ import annotations

import collections
import os
import sqlite3
import threading
import time

# ── cadence + retention (the operator's spec) ──────────────────────────────
#: the ONE sampler's tick. 2 s is ``cluster_live.DEFAULT_INTERVAL`` — the live
#: monitor's own cadence — which is what makes "one sampler at 2 s" mean "reuse
#: the monitor" rather than "run a second thing".
TIER0_INTERVAL_S = 2
#: tier 0 window: 15 minutes, kept in memory only.
TIER0_WINDOW_S = 15 * 60
#: ring depth: 450 points per node at 2 s. One record per tick, and every record
#: carries every node, so the depth IS the per-node depth.
TIER0_POINTS = TIER0_WINDOW_S // TIER0_INTERVAL_S

TIER1_BUCKET_S = 60                        # tier 1: 1-minute rollups
TIER1_WINDOW_S = 24 * 60 * 60              # ... kept 24 h  -> 1440 buckets
TIER2_BUCKET_S = 5 * 60                    # tier 2: 5-minute rollups
TIER2_WINDOW_S = 7 * 24 * 60 * 60          # ... kept 7 d   -> 2016 buckets

#: A wedged monitor child must not be re-spawned every 2 s: an exit is retried
#: at this interval at the earliest, and the reason is stamped into state.
STREAM_RESTART_MIN_S = 60

STORE_SUBDIR = "metrics"
DB_FILE = "history.db"
#: the state file this stream stamps (``~/.hscc/state/<name>.json``), and the key
#: it has in ``PERIODIC_INTERVALS`` — one name, so verify's per-stream staleness
#: rule finds it by state-file name.
STREAM_NAME = "metrics_history"
#: The stated bound for the whole store. Measured headroom is large (see
#: ``test_the_store_stays_inside_the_stated_bound``), so this is a ceiling for a
#: much wider fleet than today's 4 nodes, not the expected size.
MAX_DB_BYTES = 10 * 1024 * 1024

#: Per-node fields a point carries, in sparkrun's own ``MonitorSample`` names.
#: The card's list, plus ``swap_total_mb`` (a swap percentage is meaningless
#: without a total) and ``sparkrun_jobs`` (the monitor's Jobs column — 0% GPU
#: utilisation with two jobs running is a different story from 0% with none).
#: ``swap_used_pct`` is DERIVED (used/total) rather than read: it is in the
#: monitor's rendered table but not in its sample CSV, and a percentage of swap
#: is the field an operator actually reads.
HOST_KEYS = (
    "cpu_usage_pct", "cpu_temp_c",
    "gpu_util_pct", "gpu_temp_c", "gpu_power_w",
    "mem_used_pct", "swap_used_mb", "swap_total_mb", "swap_used_pct",
    "gpu_mem_used_mb", "gpu_clock_mhz",
    "net_recv_bytes_s", "net_send_bytes_s",
    "sparkrun_jobs",
)

#: The fields a rollup ALSO stores as a peak. The spec is "averages + max for
#: temps/power": a 1-minute average of GPU temperature erases the spike that
#: matters, and the average alone cannot tell "cooled to 45" from "peaked at 88
#: then idled". Everything else is honest as an average.
MAX_KEYS = ("cpu_temp_c", "gpu_temp_c", "gpu_power_w")

#: Serving fields as ``(stored name, name the poller returns)``. ``run`` /
#: ``wait`` are what the monitor's TABLE calls them (its columns read "Run" /
#: "Wait") while ``ServingPoller.poll()`` returns ``running`` / ``waiting`` — an
#: explicit pair, because a shared name for two different keys stores None for
#: every engine and an idle fleet is exactly the wrong default.
#: ``gen_tps`` is the "generated tok/s": the per-tick delta of vLLM's cumulative
#: Out counter, derived by ``cluster_live.rates`` — not re-derived here. The
#: cumulative ``prompt_total`` / ``gen_total`` counters are NOT stored: they
#: reset when an engine restarts, so a history of them is a history of two
#: different baselines.
ENGINE_FIELDS = (("run", "running"), ("wait", "waiting"), ("kv", "kv"),
                 ("gen_tps", "gen_tps"), ("prompt_tps", "prompt_tps"))
ENGINE_KEYS = tuple(dst for dst, _src in ENGINE_FIELDS)

#: Column order for the sqlite rows: averages (``HOST_KEYS``), then the peaks
#: (``<field>_max``), then ``n`` and the error. ONE tuple shared by the schema,
#: the writer and the reader, so a field cannot be added in one place and
#: forgotten in another.
HOST_COLUMNS = HOST_KEYS + tuple(f"{k}_max" for k in MAX_KEYS)
ENGINE_COLUMNS = ENGINE_KEYS + ("n", "error")
#: What identifies an engine inside a bucket. NOT the model: a ``WITHOUT ROWID``
#: primary key cannot hold NULL, and the route map legitimately has no model for
#: a backend it has not resolved yet — a NULL there is an IntegrityError that
#: takes the whole minute's roll-up with it. The port IS the backend (one vLLM
#: process per port on a node), so ``(node, port)`` separates two GPUs on one
#: host, and the served model rides along as a column. A model swapped on that
#: port mid-bucket is an operator event; the row reports the most recent one seen
#: and the load is the port's, which is what the widgets read.
ENGINE_KEY_COLUMNS = ("node", "port")

#: resolution label on a tier-0 point (the tier number says it for stored rows).
RES_TIER0 = "2s"


def size_bound_bytes() -> int:
    """The stated bound for the whole store, in bytes.

    The PR body quotes this rather than a hand-typed number, and a test pins
    both directions: that the measured store fits inside it, and that it is not
    so loose as to be meaningless.
    """
    return MAX_DB_BYTES


# ── paths ──────────────────────────────────────────────────────────────────

def hscc_root(hscc_dir=None) -> str:
    """The HSCC state dir, resolved at CALL time — never at import.

    Import-time ``expanduser`` constants are the leak seam this suite has been
    bitten by more than once (see ``tests/conftest.py::_isolate_hscc``): a
    constant is baked before the autouse fixture can redirect it, so it has to
    be listed by hand, and a forgotten entry writes into the operator's real
    ``~/.hscc``. Resolving per call means the redirect covers this store with
    nothing to register.

    Named ``hscc_root`` rather than ``hscc_dir`` on purpose: callers pass a
    ``hscc_dir=`` kwarg, and a function of that name would be shadowed by its own
    caller's parameter — a TypeError that only shows up at runtime.
    """
    if hscc_dir:
        return os.path.expanduser(hscc_dir)
    return os.path.expanduser("~/.hscc")


def store_dir(hscc_dir=None) -> str:
    return os.path.join(hscc_root(hscc_dir), STORE_SUBDIR)


def db_path(hscc_dir=None) -> str:
    return os.path.join(store_dir(hscc_dir), DB_FILE)


# ── one tick of the monitor -> one tier-0 point (pure: no I/O, no clock) ───

def _num(value):
    """sparkrun's CSV value -> float, or None for anything unparseable.

    Every sample field arrives as a string (``host_monitor.sh`` CSV) and an
    unavailable reading is the EMPTY string, not a zero: ``""`` -> 0.0 would
    write a fake "GPU idle" for a node that never answered, and a bucket of
    those averages into a calm-looking fleet. A NaN/Inf from a broken probe is
    dropped for the same reason — one of them poisons every mean it joins.
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def host_metrics(sample: dict | None, error: str | None = None) -> dict:
    """One sparkrun host row -> the stored per-node metrics dict.

    ``sample`` is the nested dict the monitor frame carries
    (``{host, error, sample, ...}``); it is None until a host's first reading
    lands, and ``error`` carries the collection failure. Both are kept: a point
    whose numbers are None plus its error string is the honest record, and
    dropping the host would leave a hole a reader cannot tell apart from "the
    daemon was not running".
    """
    out: dict = {k: _num((sample or {}).get(k)) for k in HOST_KEYS}
    used, total = out.get("swap_used_mb"), out.get("swap_total_mb")
    if used is not None and total:
        # Derived, and only when it can be derived — otherwise the value the
        # frame carried (None here) stands rather than being replaced by a zero.
        out["swap_used_pct"] = round(100.0 * used / total, 2)
    out["error"] = error or None   # the one string in an otherwise numeric row
    return out


def engine_metrics(rows: list | None) -> list:
    """``ServingPoller.poll()`` rows -> the stored per-engine serving list.

    A row whose backend was unreachable carries ``error`` and no load numbers;
    it is stored with the error, so a backend that went DOWN shows up in history
    as a down backend rather than as a zero-load one.

    A row with no node or no port is DROPPED: ``(node, port, model)`` is the
    engine's primary key, and a NULL in it is not a stored row but an
    ``IntegrityError`` that would take the whole minute's roll-up down with it.
    An unattributable row is also useless to a reader — it says "some engine"
    without saying which.
    """
    out = []
    for r in rows or []:
        node, port = r.get("node"), r.get("port")
        if not node or not port:
            continue
        row = {"node": node, "port": port, "model": r.get("model")}
        if r.get("error"):
            row["error"] = str(r["error"])
            out.append(row)
            continue
        for dst, src in ENGINE_FIELDS:
            v = r.get(src)
            if dst in ("run", "wait"):
                row[dst] = int(v) if v is not None else None
            else:
                row[dst] = _num(v)
        out.append(row)
    return out


def record_from_monitor(frame: dict | None, rows: list | None, ts: float,
                        host_error: str | None = None) -> dict | None:
    """The monitor's two sources -> one tier-0 point, or None if nothing was
    learned.

    ``frame`` is the newest ``sparkrun cluster monitor --json`` frame
    (``HostStream.frame``) and ``rows`` the newest ``ServingPoller.poll()``
    list. None comes back only when the frame has no hosts and no stream error:
    a tick that learned nothing must not occupy a ring slot, because the ring is
    bounded and it would push a real sample out to store a row of nulls. A frame
    whose individual hosts failed IS recorded (per-host ``error``), and
    ``host_error`` (``HostStream.error``, e.g. "sparkrun monitor exited") is
    stamped on the point so a gap in the data explains itself without the daemon
    log — which rotates.
    """
    hosts = (frame or {}).get("hosts") or []
    if not hosts and not host_error:
        return None
    rec = {"ts": int(ts), "res": RES_TIER0, "n": 1,
           "hosts": {}, "engines": engine_metrics(rows)}
    for h in hosts:
        name = h.get("host")
        if not name:
            continue
        rec["hosts"][str(name)] = host_metrics(h.get("sample"), h.get("error"))
    if host_error:
        rec["error"] = str(host_error)
    return rec


def frame_identity(frame: dict | None):
    """A fingerprint of a frame's FRESHNESS, or None when it cannot be told.

    ``HostStream`` keeps the newest frame it has seen. When the sparkrun child
    wedges or exits, that newest frame goes stale but stays readable, and
    recording it again would write the same reading as a new sample — a chart of
    a dead poller then looks like a fleet that flatlined calmly, which is exactly
    the misreading this guard exists for. ``host_monitor.sh`` stamps every sample
    with its own ``timestamp``, so two frames with identical stamps are the same
    reading.

    Returns None when no host in the frame carries a stamp: freshness is then
    unknowable, and the caller must record every tick (the pre-existing
    behaviour) rather than "detect" staleness it cannot detect.
    """
    hosts = (frame or {}).get("hosts") or []
    stamps = [(h.get("host"), (h.get("sample") or {}).get("timestamp"))
              for h in hosts if h.get("host")]
    if not stamps or not any(s for _h, s in stamps):
        return None
    return tuple(stamps)


# ── roll-up: finer tier -> coarser tier, never sampled separately ──────────

def _bucket_start(ts: float, bucket_s: int) -> int:
    """Floor an epoch onto a bucket boundary.

    Epoch seconds are UTC, so ``ts % bucket_s`` aligns buckets to the wall clock
    in every timezone at once: a 1-minute bucket starts at :00 and a 5-minute one
    at :00/:05/:10 everywhere, which is what makes two writers' files comparable.
    """
    return int(ts) - (int(ts) % bucket_s)


def _avg(values: list):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def _max(values: list):
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _host_samples(point: dict):
    """Yield ``(host, metrics)`` from either shape a roll-up can be handed.

    A tier-0 point is a RECORD (``hosts`` map, one per 2 s tick). A tier-1 row
    read back out of sqlite is FLAT (one row = one host, ``host`` + the columns).
    Both feed the SAME fold, which is how "tier 2 rolls up from tier 1, never
    from the raw samples" is guaranteed by construction instead of by a promise:
    there is only one roll-up function, and tier 2 hands it tier 1's rows.
    """
    if "hosts" in point:
        for host, metrics in (point.get("hosts") or {}).items():
            yield host, metrics
    elif point.get("host") is not None:
        yield point["host"], point


def _engine_entries(point: dict):
    """``(node, port) -> metrics`` for either input shape. See
    ``_host_samples`` for why both shapes arrive here."""
    if "engines" in point:
        for e in point.get("engines") or []:
            yield (e.get("node"), e.get("port")), e
    elif point.get("node") is not None:
        yield (point.get("node"), point.get("port")), point


def _mean_over(items: list, weights: list, weighted: bool):
    """Mean of ``items`` — plain, or weighted by the finer tier's sample count.

    The weighting is the difference between a correct and a plausible 5-minute
    average: a minute in which the node answered 4 samples is not worth the same
    as a minute in which it answered 30, and an unweighted mean of minute
    averages quietly pretends it is.
    """
    pairs = [(v, w) for v, w in zip(items, weights) if v is not None]
    if not pairs:
        return None
    if weighted:
        wsum = sum(w for _v, w in pairs)
        if wsum:
            return round(sum(v * w for v, w in pairs) / wsum, 6)
    return round(sum(v for v, _w in pairs) / len(pairs), 6)


def _fold_hosts(points: list, tier: int, bucket_s: int) -> list:
    """Per-host rows for ONE bucket of ``points`` (all sharing a bucket start).

    Raw 2 s samples give a plain mean and a max of the samples. Rows from the
    tier below give an ``n``-weighted mean of the sub-averages and the
    max of the sub-maxes — and that second one is EXACT, not approximate,
    because the largest 2 s reading inside a minute is that minute's stored max.
    A peak is therefore never lost by averaging, which is the whole reason the
    ``*_max`` columns exist.
    """
    start = _bucket_start(points[0]["ts"], bucket_s)
    by_host: dict = {}
    for p in points:
        for host, m in _host_samples(p):
            by_host.setdefault(host, []).append(m)
    rows = []
    for host in sorted(by_host):
        samples = by_host[host]
        weighted = all(s.get("n") is not None for s in samples)
        weights = [s.get("n") for s in samples] if weighted else [1] * len(samples)
        row: dict = {"tier": tier, "ts": start, "host": host}
        for k in HOST_KEYS:
            row[k] = _mean_over([s.get(k) for s in samples], weights, weighted)
        for k in MAX_KEYS:
            # The peak lives in `<k>_max` once a tier has been rolled up; in raw
            # samples it is the max of the samples. A stored max that is None
            # (nothing reported all minute) stays None — it must not fall back to
            # the average, which would report an average AS a peak.
            vals = [s[f"{k}_max"] if f"{k}_max" in s else s.get(k) for s in samples]
            row[f"{k}_max"] = _max(vals)
        row["n"] = int(sum(weights)) if weighted else len(samples)
        row["error"] = next((s["error"] for s in samples if s.get("error")), None)
        rows.append(row)
    return rows


def _fold_engines(points: list, tier: int, bucket_s: int) -> list:
    """Per-engine roll-up, keyed (node, port) so backends stay separate.

    Keying by node + port is what keeps a two-GPU node's two vLLM backends from
    being averaged into one imaginary backend serving twice the load (see
    ``ENGINE_KEY_COLUMNS`` for why the served model is not part of the key). The
    mean is ``n``-weighted for a rolled-up tier and plain for raw samples,
    exactly as for hosts.
    """
    start = _bucket_start(points[0]["ts"], bucket_s)
    by_engine: dict = {}
    for p in points:
        for key, e in _engine_entries(p):
            by_engine.setdefault(key, []).append(e)
    out = []
    for key in sorted(by_engine, key=lambda k: [str(x) for x in k]):
        node, port = key
        rows = by_engine[key]
        weighted = all(r.get("n") is not None for r in rows)
        weights = [r.get("n") for r in rows] if weighted else [1] * len(rows)
        row = {"tier": tier, "ts": start, "node": node, "port": port,
               # the most recent model this port reported in the bucket, or None
               "model": next((r["model"] for r in reversed(rows)
                              if r.get("model")), None)}
        for k in ENGINE_KEYS:
            row[k] = _mean_over([r.get(k) for r in rows], weights, weighted)
        row["n"] = int(sum(weights)) if weighted else len(rows)
        row["error"] = next((r["error"] for r in rows if r.get("error")), None)
        out.append(row)
    return out


def rollup(points: list, tier: int, bucket_s: int) -> tuple[list, list]:
    """Roll one bucket of points up to ``tier`` -> (host rows, engine rows).

    The pure core the two named gates test: ``rollup(tier0_points, 1, 60)`` is
    the 2 s -> 1 min fold (pinning that the MAX is the peak inside the minute,
    not the peak of the averages) and ``rollup(tier1_rows, 2, 300)`` is the
    1 min -> 5 min fold.

    A partial bucket — a daemon that was down for 40 of 60 seconds — rolls up
    what exists and says so with ``n`` instead of inventing the missing samples.
    Callers must hand points from ONE bucket (equal ``_bucket_start``); that is
    what makes a rolled row final, and why the writer can be a plain upsert with
    no merge path anywhere.
    """
    points = [p for p in points if p.get("ts") is not None]
    if not points:
        return [], []
    return _fold_hosts(points, tier, bucket_s), _fold_engines(points, tier, bucket_s)


# ── the store: one bounded sqlite file for tiers 1 and 2 ───────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS host_rollup (
    tier     INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    host     TEXT    NOT NULL,
    {host_cols},
    n        INTEGER NOT NULL,
    error    TEXT,
    PRIMARY KEY (tier, ts, host)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS engine_rollup (
    tier     INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    node     TEXT    NOT NULL,
    port     TEXT    NOT NULL,
    model    TEXT,
    {engine_cols},
    n        INTEGER NOT NULL,
    error    TEXT,
    PRIMARY KEY (tier, ts, node, port)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;
""".format(host_cols=", ".join(f"{c} REAL" for c in HOST_COLUMNS),
           engine_cols=", ".join(f"{c} REAL" for c in ENGINE_KEYS))

_INSERT_HOST = ("INSERT OR REPLACE INTO host_rollup (tier, ts, host, "
                + ", ".join(HOST_COLUMNS) + ", n, error) VALUES ("
                + ", ".join(["?"] * (3 + len(HOST_COLUMNS) + 2)) + ")")
_INSERT_ENGINE = ("INSERT OR REPLACE INTO engine_rollup (tier, ts, "
                  + ", ".join(ENGINE_KEY_COLUMNS) + ", model, "
                  + ", ".join(ENGINE_KEYS) + ", n, error) VALUES ("
                  + ", ".join(["?"] * (2 + len(ENGINE_KEY_COLUMNS) + 1
                                       + len(ENGINE_KEYS) + 2)) + ")")


class MetricsStore:
    """Tiers 1 and 2: two ``WITHOUT ROWID`` tables in one bounded sqlite file.

    Deliberately NOT JSON per sample. A rollup row is ~20 fixed columns of
    numbers keyed by ``(tier, ts, host)``, so the row IS its index and a week of
    history is a few megabytes (measured and asserted, not assumed). sqlite also
    supplies what the card asks for outright: durability across a daemon restart,
    a bounded file (``DELETE`` past the window, freelist reclaimed when it
    grows), and reads that filter by time and node without parsing a line per
    record.

    The connection is opened lazily and held, so a rollup is one short
    transaction rather than an open/close per write. ``journal_mode=DELETE`` is
    pinned rather than WAL so no ``-wal`` sidecar grows beside the store.
    """

    def __init__(self, hscc_dir=None, max_bytes: int = MAX_DB_BYTES):
        self.path = db_path(hscc_dir)
        self.max_bytes = max_bytes
        self._conn: sqlite3.Connection | None = None
        self.error: str | None = None

    # ── connection ─────────────────────────────────────────────────────────
    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        conn.commit()
        self._conn = conn
        self.error = None
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ── writing ────────────────────────────────────────────────────────────
    def write_rollup(self, host_rows: list, engine_rows: list) -> None:
        """Insert one bucket's rows. ``INSERT OR REPLACE`` makes a replay a
        no-op rather than a duplicate, which is what lets a rollup that died
        half-way be retried on the next pass."""
        if not host_rows and not engine_rows:
            return
        conn = self.connect()
        conn.executemany(_INSERT_HOST, [
            (r["tier"], r["ts"], r["host"],
             *[r.get(c) for c in HOST_COLUMNS], r.get("n"), r.get("error"))
            for r in host_rows])
        if engine_rows:
            conn.executemany(_INSERT_ENGINE, [
                (r["tier"], r["ts"], r["node"], r["port"], r.get("model"),
                 *[r.get(c) for c in ENGINE_KEYS], r.get("n"), r.get("error"))
                for r in engine_rows])
        conn.commit()

    def prune(self, tier: int, cutoff: int) -> int:
        """Drop this tier's rows older than its own window; returns how many.

        This is the retention half of the bound. Deleting leaves the pages on
        sqlite's freelist for the NEXT bucket to reuse, so the file reaches a
        steady-state size and stops — it does not grow with uptime, which is the
        property a ring has and an append-only log does not.
        """
        conn = self.connect()
        cur = conn.execute("DELETE FROM host_rollup WHERE tier = ? AND ts < ?",
                           (tier, cutoff))
        n = cur.rowcount or 0
        conn.execute("DELETE FROM engine_rollup WHERE tier = ? AND ts < ?",
                     (tier, cutoff))
        conn.commit()
        return n

    def vacuum_if_fragmented(self) -> bool:
        """Reclaim the freelist once it is big enough to be worth the rewrite.

        Pruning reuses pages, so this is not what bounds the file; it only stops
        a one-off burst (a node added, then removed) from leaving the store
        permanently larger than the data it holds.
        """
        conn = self.connect()
        free_n = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if free_n * conn.execute("PRAGMA page_size").fetchone()[0] < self.max_bytes // 4:
            return False
        conn.execute("VACUUM")
        return True

    # ── reading ────────────────────────────────────────────────────────────
    def host_rows(self, tier: int, since: float | None = None,
                  until: float | None = None, host: str | None = None) -> list:
        """Stored per-node rows for one tier, oldest first. ``since``/``until``
        are both INCLUSIVE bucket starts."""
        return self._query("host_rollup", tier, since, until, host, "host")

    def engine_rows(self, tier: int, since: float | None = None,
                    until: float | None = None, node: str | None = None) -> list:
        return self._query("engine_rollup", tier, since, until, node, "node")

    def _query(self, table: str, tier: int, since, until, name, name_col) -> list:
        sql = f"SELECT * FROM {table} WHERE tier = ?"
        args: list = [tier]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(int(since))
        if until is not None:
            sql += " AND ts <= ?"
            args.append(int(until))
        if name is not None:
            sql += f" AND {name_col} = ?"
            args.append(name)
        sql += " ORDER BY ts"
        conn = self.connect()
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.row_factory = None

    def count(self, tier: int) -> int:
        return self.connect().execute(
            "SELECT count(*) FROM host_rollup WHERE tier = ?", (tier,)).fetchone()[0]

    def earliest(self, tier: int):
        """Oldest stored bucket start for a tier, or None when it is empty.

        Read out of the primary key, so it costs nothing — and it is what the
        recorder clamps its catch-up scan to, instead of asking sqlite about
        1970-to-now on every pass.
        """
        row = self.connect().execute(
            "SELECT min(ts) FROM host_rollup WHERE tier = ?", (tier,)).fetchone()
        return row[0] if row and row[0] is not None else None

    # ── watermarks (survive a restart, so a rollup is never lost or redone) ─
    def watermark(self, key: str, default: int = 0) -> int:
        try:
            row = self.connect().execute(
                "SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return int(row[0]) if row else default
        except (sqlite3.Error, TypeError, ValueError):
            return default

    def set_watermark(self, key: str, value: int) -> None:
        conn = self.connect()
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                     (key, str(int(value))))
        conn.commit()

    # ── size ───────────────────────────────────────────────────────────────
    def size_bytes(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def stats(self) -> dict:
        out = {"path": self.path, "db_bytes": self.size_bytes(),
               "bound_bytes": size_bound_bytes(), "error": self.error}
        try:
            out["tier1_rows"] = self.count(1)
            out["tier2_rows"] = self.count(2)
        except sqlite3.Error as e:
            self.error = f"{type(e).__name__}: {e}"
            out["error"] = self.error
        return out


# ── the recorder: tier 0 in memory, tiers 1 and 2 on disk ──────────────────

class MetricsRecorder:
    """The three tiers, fed by ONE tick of the monitor.

    ``tick()`` is the whole data path and runs at the 2 s cadence: append the
    point to the tier-0 ring (memory, O(1), no disk), then — only when a minute
    has actually closed — roll it into tier 1, roll five closed minutes into
    tier 2, and prune. So the disk sees one short transaction a minute rather
    than one every 2 s, which is the reason tier 0 exists.

    Rollups are watermark-driven rather than count-driven, so a daemon that was
    down for twenty minutes catches up on its first tick from whatever tier 0
    still holds, and a restart never re-rolls a bucket that was already written.
    """

    def __init__(self, store: MetricsStore | None = None, hscc_dir=None,
                 tier0_points: int = TIER0_POINTS):
        self.store = store if store is not None else MetricsStore(hscc_dir)
        self.tier0: collections.deque = collections.deque(maxlen=tier0_points)
        self.last_error: str | None = None
        self._last_closed_minute: int | None = None

    # ── tier 0 ─────────────────────────────────────────────────────────────
    def add(self, rec: dict) -> None:
        self.tier0.append(rec)

    def tier0_points(self, host: str | None = None) -> list:
        """The in-memory ring, oldest first. EMPTY right after a restart — by
        spec tier 0 is memory-only, and nothing here pretends otherwise."""
        if host is None:
            return list(self.tier0)
        return [p for p in self.tier0 if host in (p.get("hosts") or {})]

    # ── the tick ───────────────────────────────────────────────────────────
    def tick(self, frame: dict | None, rows: list | None, ts: float,
             host_error: str | None = None) -> dict | None:
        """One 2 s tick: record it, then do whatever rollups have come due.

        Returns the tier-0 point appended, or None when the tick learned nothing
        (no hosts and no stream error) and so was deliberately not recorded.
        """
        rec = record_from_monitor(frame, rows, ts, host_error=host_error)
        if rec is None:
            return None
        self.add(rec)
        now = rec["ts"]
        closed = now - (now % TIER1_BUCKET_S)
        # The disk pass runs once per closed minute (and again on any tick after
        # a failure, because the watermark did not advance). At the 2 s cadence
        # that is 30 in-memory appends per transaction — the "zero disk churn"
        # half of the tier-0 spec, in code.
        if closed != self._last_closed_minute or self.last_error:
            self._last_closed_minute = closed
            try:
                self.rollups(now)
            except (sqlite3.Error, OSError) as e:
                # A store failure must never take the sampler down: tier 0 keeps
                # filling, the next tick retries the rollup, and the state stamp
                # carries the error loudly.
                self.last_error = f"{type(e).__name__}: {e}"
                self.store.error = self.last_error
        return rec

    # ── rollups ────────────────────────────────────────────────────────────
    def rollups(self, now: int) -> dict:
        """Close and roll every bucket whose whole span has passed.

        A bucket is closed only when ``ts + bucket_s <= closed`` — a minute is
        not averageable until its last sample has arrived. That is also what
        makes a rolled row FINAL.
        """
        did = {"tier1_buckets": 0, "tier2_buckets": 0, "pruned": 0}
        closed1 = now - (now % TIER1_BUCKET_S)          # start of the open minute
        # ── tier 1 from tier 0 (memory only) ──
        last1 = self.store.watermark("tier1_closed_through")
        groups: dict = {}
        for p in self.tier0:
            groups.setdefault(_bucket_start(p["ts"], TIER1_BUCKET_S), []).append(p)
        for start in sorted(groups):
            if start < last1 or start + TIER1_BUCKET_S > closed1:
                continue
            self.store.write_rollup(*rollup(groups[start], 1, TIER1_BUCKET_S))
            self.store.set_watermark("tier1_closed_through",
                                     start + TIER1_BUCKET_S)
            did["tier1_buckets"] += 1
        # ── tier 2 from tier 1 (on disk) — never from tier 0 ──
        closed5 = now - (now % TIER2_BUCKET_S)
        last2 = self.store.watermark("tier2_closed_through")
        first = self.store.earliest(1)
        if first is not None and last2 < first:
            last2 = first - (first % TIER2_BUCKET_S)     # nothing stored before this
        for start in range(last2, closed5, TIER2_BUCKET_S):
            hi = start + TIER2_BUCKET_S - TIER1_BUCKET_S  # last bucket start inside
            rows = self.store.host_rows(1, since=start, until=hi)
            if not rows:
                break        # this bucket has not rolled up yet; stop, retry later
            engines = self.store.engine_rows(1, since=start, until=hi)
            self.store.write_rollup(*rollup(rows, 2, TIER2_BUCKET_S))
            self.store.set_watermark("tier2_closed_through", start + TIER2_BUCKET_S)
            did["tier2_buckets"] += 1
        # ── retention: each tier prunes by its OWN window ──
        did["pruned"] = self.store.prune(1, now - TIER1_WINDOW_S)
        self.store.prune(2, now - TIER2_WINDOW_S)
        if self.store.vacuum_if_fragmented():
            did["vacuumed"] = True
        return did

    def stats(self) -> dict:
        s = self.store.stats()
        s.update({"tier0_points": len(self.tier0),
                  "tier0_capacity": self.tier0.maxlen,
                  "sampler_error": self.last_error})
        return s


# ── the sampler: the monitor's own two sources, ticked at 2 s ──────────────

def host_stream_cls():
    """``cluster_live.HostStream``, resolved at CALL time.

    A function rather than a module-level import for one reason: the test that
    pins "the recorder reuses the monitor's code path" asserts IDENTITY
    (``host_stream_cls() is cluster_live.HostStream``), and it has to be able to
    do that without constructing anything — building a ``HostStream`` spawns
    ``sparkrun cluster monitor``, which no test may do. Importing the class is
    free; instantiating it is not.
    """
    from .cluster_live import HostStream
    return HostStream


def serving_poller_cls():
    """``cluster_live.ServingPoller``, for the same lazy-resolution reason."""
    from .cluster_live import ServingPoller
    return ServingPoller


class Sampler:
    """The ONE sampler at the 2 s cadence: the monitor's stream + its serving
    poller + the recorder. Nothing here samples the fleet independently.

    ``HostStream.frame`` is the newest frame already delivered over sparkrun's
    persistent ssh channels, and ``ServingPoller.poll()`` is the proxy route map
    plus each backend's ``/metrics`` — the same two sources
    ``hscc cluster monitor --live`` renders, at its own default interval. "No new
    ssh per tick" is therefore a property of reusing ``HostStream`` (one child,
    connections held open for its life) rather than something this class does.

    The stream is restarted on a bounded interval when sparkrun exits: a
    permanent failure must not fork a new sparkrun every 2 s, and the reason is
    kept in ``self.error`` so it reaches the state stamp.
    """

    def __init__(self, interval: int = TIER0_INTERVAL_S, cluster=None,
                 host_stream=None, serving=None, recorder=None, hscc_dir=None,
                 clock=time.time, restart_min_s: int = STREAM_RESTART_MIN_S):
        self.interval = interval
        self.cluster = cluster
        self.clock = clock
        self.restart_min_s = restart_min_s
        self.recorder = recorder if recorder is not None else MetricsRecorder(
            hscc_dir=hscc_dir)
        self._stream = host_stream if host_stream is not None else \
            host_stream_cls()(interval, cluster=cluster)
        self._serving = serving if serving is not None else serving_poller_cls()()
        self._identity = None
        self._last_start = self.clock()
        self._errored = False            # an error point has been recorded once
        self.error: str | None = None

    # ── the stream, bounded restart ────────────────────────────────────────
    def _restart_if_due(self, now: float) -> None:
        if now - self._last_start < self.restart_min_s:
            return
        self._last_start = now
        try:
            self._stream.close()
        except Exception:               # a dead child's close() is not our failure
            pass
        self._stream = host_stream_cls()(self.interval, cluster=self.cluster)

    def close(self) -> None:
        for closer in (getattr(self._stream, "close", None),
                       getattr(self._serving, "close", None)):
            if closer:
                try:
                    closer()
                except Exception:
                    pass

    # ── one tick ───────────────────────────────────────────────────────────
    def tick(self, now=None):
        """Sample once -> the tier-0 point stored, or None when nothing new came.

        Two guards matter here:

        * a **stale frame is not a sample**. ``frame_identity`` says whether the
          newest frame is a reading we already stored; repeating it would fill the
          history with one frozen instant, which is worse than a gap. The one
          exception is the first tick after the stream breaks: that gets a single
          point stamped with the stream error, so the tail of the history says
          "the sampler lost the fleet here" instead of ending silently.
        * a **serving failure must not blank the host numbers**. The two sources
          are independent; if the poller raises, the tick still records the frame
          and carries the serving error separately.
        """
        now = self.clock() if now is None else now
        stream = self._stream
        frame, stream_error = stream.frame, stream.error
        if stream_error:
            self.error = str(stream_error)
            self._restart_if_due(now)
            ident = frame_identity(frame)
            if self._errored or ident == self._identity:
                return None              # already recorded; do not repeat a dead stream
            self._errored = True
            return self.recorder.tick(None, None, now, host_error=self.error)
        ident = frame_identity(frame)
        if ident is not None and ident == self._identity:
            return None                  # same reading: not a new sample
        self._identity = ident
        self._errored = False
        rows: list = []
        try:
            rows = self._serving.poll() or []
        except Exception as e:           # a poller's bug must not stop the recording
            self.error = f"serving poll failed: {type(e).__name__}: {e}"
        else:
            self.error = None
        return self.recorder.tick(frame, rows, now, host_error=self.error)


# ── daemon stream body ─────────────────────────────────────────────────────

_sampler: Sampler | None = None
_sampler_lock = threading.Lock()


def get_sampler(**kwargs) -> Sampler:
    """The process-wide sampler, built on first use.

    One sampler per daemon process is the point of the amendment ("one sampler
    at 2 s"), so this is a singleton rather than something each caller constructs
    — two samplers would mean two ``sparkrun cluster monitor`` children and two
    writers racing on the roll-up watermarks.
    """
    global _sampler
    with _sampler_lock:
        if _sampler is None:
            _sampler = Sampler(**kwargs)
        return _sampler


def set_sampler(sampler) -> None:
    """Install (or with None, clear) the process-wide sampler — the test seam."""
    global _sampler
    with _sampler_lock:
        _sampler = sampler


def close_sampler() -> None:
    """Tear the sampler down (daemon shutdown, and every test that installs one)."""
    global _sampler
    with _sampler_lock:
        s, _sampler = _sampler, None
    if s is not None:
        s.close()


def check_metrics_history(now=None, hscc_dir=None) -> dict:
    """The daemon's ``metrics_history`` stream body (cadence: the SOT entry in
    ``PERIODIC_INTERVALS``).

    Returns the state dict and STAMPS IT ON EVERY PATH, including the one where
    the sampler raised — a stream that goes dark is indistinguishable from a
    stream that died, and verify's per-stream staleness rule is the difference
    between those two. The recorder's real work is bounded by the closed-minute
    check inside ``tick()``, so this costs one frame read plus one sqlite pass a
    minute at the 2 s cadence.
    """
    from . import state as state_mod
    detail: dict = {"ok": True, "tier0_points": 0, "tier1_rows": 0,
                    "tier2_rows": 0, "db_bytes": 0, "error": None}
    try:
        sampler = get_sampler(hscc_dir=hscc_dir)
        rec = sampler.tick(now=now)
        stats = sampler.recorder.stats()
        detail.update({
            "tier0_points": stats.get("tier0_points", 0),
            "tier1_rows": stats.get("tier1_rows", 0),
            "tier2_rows": stats.get("tier2_rows", 0),
            "db_bytes": stats.get("db_bytes", 0),
            "bound_bytes": stats.get("bound_bytes", size_bound_bytes()),
            "recorded": rec is not None,
            "error": sampler.error or stats.get("error"),
        })
    except Exception as e:              # never propagate: the loop must keep ticking
        detail.update({"ok": False,
                       "error": f"{type(e).__name__}: {e}"})
    detail["ok"] = detail["error"] is None
    try:
        state_mod.write_state(STREAM_NAME, detail)
    except Exception:
        pass                            # a state-write failure must not kill the tick
    return detail
