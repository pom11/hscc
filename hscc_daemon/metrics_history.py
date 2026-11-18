"""Cluster + serving metrics history: a bounded ring buffer on disk.

WHY THIS EXISTS. There is no metrics history anywhere today:
``/v1/daemon/history`` is the automated-action timeline (autodown/autoup
decisions), not telemetry, and the widgets refresh ~every 15 minutes, so
anything that happened between two refreshes is simply gone. History has to be
written by the one process that is always up and always looking at the fleet —
the daemon — and it has to survive the daemon's own restarts, or every
``hscc`` upgrade wipes the chart.

WHERE THE SAMPLES COME FROM — the monitor's OWN code path, deliberately.
``hscc cluster monitor --live`` (``cluster_live.py``) already maintains exactly
the two sources this recorder needs:

* ``cluster_live.HostStream`` — ONE long-running ``sparkrun cluster monitor
  --json`` child that streams a frame per interval over sparkrun's own SSH
  sessions. The newest frame is kept; nothing is re-spawned per tick.
* ``cluster_live.ServingPoller`` — the proxy's ``/v1/model/info`` route map
  plus each backend's vLLM ``/metrics`` (running / waiting / KV use / token
  counters), with the per-tick token *rates* already derived from the
  cumulative In/Out counters by ``cluster_live.rates``.

This module imports those two classes and consumes what they already produce.
It contains **no** SSH, subprocess or sparkrun call of its own — adding a
second poller would double the SSH fan-out for every node on every tick and
give the fleet two disagreeing views of the same sample. That constraint is
pinned by ``tests/test_metrics_history.py`` (the module is AST-swept for
``subprocess`` / ``Popen`` / ``ssh`` / ``sparkrun``, and the default source
factories are compared by IDENTITY to ``cluster_live``'s classes), so a future
edit that reaches for its own poller fails a test instead of quietly shipping.

WHAT IS STORED (one record per 60 s tick, per the card):

    {"ts": 1795030000, "res": "60s", "n": 1,
     "hosts": {"dgx-246": {"cpu_usage_pct": 6.2, "cpu_temp_c": 87.2,
                           "gpu_util_pct": 94.0, "gpu_temp_c": 71.0,
                           "gpu_power_w": 37.88, "mem_used_pct": 85.1,
                           "swap_used_mb": 0.0, "swap_total_mb": 16383.0,
                           "swap_used_pct": 0.0, "gpu_mem_used_mb": 47104.0,
                           "gpu_clock_mhz": 1965.0, "net_recv_bytes_s": 1.2e5,
                           "net_send_bytes_s": 3.4e5, "sparkrun_jobs": 1.0,
                           "error": null}},
     "engines": [{"node": "192.0.2.246", "port": "8001",
                  "model": "org/Model", "run": 3, "wait": 1,
                  "kv": 0.5, "gen_tps": 10.0, "prompt_tps": 200.0}]}

Field names are sparkrun's ``MonitorSample`` names verbatim (plus the derived
``swap_used_pct`` the monitor renders), so a reader never has to translate and
a rename upstream shows up as a missing key rather than a silently wrong one.
Values are floats, not sparkrun's CSV strings — normalised ONCE here so
averaging and every reader downstream can skip the parsing (``None`` means
"not reported", which is never coerced to 0.0).

Serving rows stay a flat list keyed by nothing: the monitor's ``node`` is an
address while the host frame's key is a hostname, and inventing a mapping
between them would be a guess. A reader that wants per-node engines filters
``engines`` by ``node``.

RESOLUTIONS, ON DISK, BOUNDED. Under ``~/.hscc/metrics/`` (override the base
with ``hscc_dir=`` — every path here resolves at CALL time through
``os.path.expanduser``, so ``tests/conftest.py``'s autouse ``~/.hscc``
redirect covers this store without a module constant to forget):

    raw.jsonl   one record per 60 s tick, newest last, ring-trimmed to
                ``RAW_CAPACITY`` = 24 h worth of points (1440).
    down.jsonl  5-minute buckets, newest last, ring-trimmed to
                ``DOWN_CAPACITY`` = 7 d worth of buckets (2016).

Both files are additionally capped in bytes (``RAW_MAX_BYTES`` /
``DOWN_MAX_BYTES``) so a fat record — a node with many engines, a long model
name — can never grow the store past the stated bound. The whole store is
therefore never larger than ``RAW_MAX_BYTES + DOWN_MAX_BYTES`` (32 MiB) plus at
most one not-yet-trimmed record. ``size_bound_bytes()`` is the number the PR
body quotes and ``test_measured_record_size_fits_the_stated_caps`` keeps it
honest by MEASURING a fleet-shaped record (4 nodes, 6 engines with long model
names): 2,333 bytes per raw point and 5,467 per bucket, so a full raw ring is
3.2 MiB and a full 5-minute ring 10.5 MiB — ~14 MiB worst case, inside caps
that leave ~2.5x headroom for a fleet wider than this one.

Aging is one-directional: a raw point older than the raw window is folded into
the 5-minute store and removed from raw, and a bucket older than 7 d is
dropped. A bucket is folded only once its WHOLE span has passed the cutoff
(``_bucket_end(ts) <= cutoff``), so a folded bucket is final — no code path
merges samples into a bucket that was already written, which is what lets both
files stay append-only between trim passes.

RESTART SAFETY. Records are appended and flushed as they are written; a new
process reads the files back before its first append, so nothing recorded
before a restart is lost or overwritten. ``tests/test_metrics_history.py``
proves exactly the experiment the card asks for: write, drop the object,
construct a fresh one, read back.
"""

from __future__ import annotations

import json
import os
import threading
import time

# ── cadence + retention (the numbers the card fixes) ──────────────────────
#: one sample per minute, the recorder's tick.
RAW_INTERVAL_S = 60
#: 60 s resolution is kept for a day.
RAW_RETENTION_S = 24 * 60 * 60
#: points in the raw ring: 1440 = 24 h at 60 s.
RAW_CAPACITY = RAW_RETENTION_S // RAW_INTERVAL_S
#: the coarser resolution older history is folded down to.
DOWN_BUCKET_S = 5 * 60
#: 5-minute buckets are kept for a week.
DOWN_RETENTION_S = 7 * 24 * 60 * 60
#: buckets in the downsampled ring: 2016 = 7 d at 5 min.
DOWN_CAPACITY = DOWN_RETENTION_S // DOWN_BUCKET_S

# Appending one line at a time is what makes the common tick cheap, so the
# ring is allowed to run this far PAST capacity before a trim pass rewrites the
# file: 64 lines is ~4 minutes of slack at 60 s, i.e. the rewrite happens once
# every ~144 ticks instead of every tick, and the file is still bounded by
# capacity + slack lines.
TRIM_SLACK_LINES = 64

# Byte caps are the second, independent bound: capacity alone does not bound a
# record (a node can expose any number of engines, and model names are
# arbitrary text). A trim pass that trips the byte cap keeps the newest records
# that fit, so the file is never more than one record over these numbers.
# 8 MiB holds ~3600 records of the measured shape (2,333 bytes) against the
# 1440 the raw ring keeps, and 24 MiB holds ~4700 buckets (5,467 bytes) against
# the 2016 kept, so ordinary operation never trims by bytes at all; it is the
# guard for the fat case. Both margins are pinned by
# test_measured_record_size_fits_the_stated_caps.
RAW_MAX_BYTES = 8 * 1024 * 1024
DOWN_MAX_BYTES = 24 * 1024 * 1024

#: The store directory, relative to the HSCC state dir.
STORE_SUBDIR = "metrics"
RAW_FILE = "raw.jsonl"
DOWN_FILE = "down.jsonl"

#: Which ``MonitorSample`` fields a record carries, in sparkrun's own names.
#: The card's list, plus ``swap_total_mb`` (``swap_used_pct`` is meaningless
#: without it) and ``sparkrun_jobs`` (what the monitor's Jobs column reads —
#: a node at 0% GPU with 2 jobs is a different story from one with none).
HOST_KEYS = (
    "cpu_usage_pct", "cpu_temp_c",
    "gpu_util_pct", "gpu_temp_c", "gpu_power_w",
    "mem_used_pct", "swap_used_mb", "swap_total_mb",
    "gpu_mem_used_mb", "gpu_clock_mhz",
    "net_recv_bytes_s", "net_send_bytes_s",
    "sparkrun_jobs",
)

#: Serving fields as ``(stored name, name the monitor returns)``.
#: ``run`` / ``wait`` are what the monitor's table CALLS them (its columns read
#: "Run" / "Wait"), while ``ServingPoller.poll()`` returns ``running`` /
#: ``waiting`` — an explicit pair, because a shared name for two different keys
#: stores None for every engine and looks like an idle fleet.
#: ``gen_tps`` is the card's "generated tok/s": the per-tick delta of vLLM's
#: cumulative Out counter, derived by cluster_live.rates — not re-derived here.
#: The cumulative ``prompt_total`` / ``gen_total`` counters are deliberately
#: NOT stored: they reset when an engine restarts, so a history of them is a
#: history of two different baselines.
ENGINE_FIELDS = (("run", "running"), ("wait", "waiting"), ("kv", "kv"),
                 ("gen_tps", "gen_tps"), ("prompt_tps", "prompt_tps"))
ENGINE_KEYS = tuple(dst for dst, _src in ENGINE_FIELDS)

#: Keys of a downsampled bucket that are not host/engine fields.
_BUCKET_META = ("ts", "res", "n")
#: Per-field peak/floor sidecars, present on 5-minute buckets only. A raw point
#: needs no sidecar — the point IS the value.
MAX_KEY = "_max"
MIN_KEY = "_min"


# ── paths ──────────────────────────────────────────────────────────────────

def hscc_root(hscc_dir=None) -> str:
    """The HSCC state dir. Resolved at CALL time, never at import.

    Import-time ``expanduser`` constants are the leak seam this suite has been
    bitten by twice (see ``tests/conftest.py::_isolate_hscc``): a constant is
    baked before the autouse fixture can redirect it, so it has to be listed by
    hand and a forgotten entry writes into the operator's real ``~/.hscc``.
    Resolving here means the redirect covers this store with nothing to
    register.

    Named ``hscc_root`` (not ``hscc_dir``) on purpose: the callers all pass a
    ``hscc_dir=`` kwarg, and a function of the same name would be shadowed by
    its own caller's parameter — a TypeError that only fires at runtime.
    """
    if hscc_dir:
        return os.path.expanduser(hscc_dir)
    return os.path.expanduser("~/.hscc")


def store_dir(hscc_dir=None) -> str:
    return os.path.join(hscc_root(hscc_dir), STORE_SUBDIR)


def size_bound_bytes() -> int:
    """The stated bound for the whole store, in bytes.

    The PR body quotes this number rather than a hand-typed one, and a test
    pins that the two caps are what it adds up to — so the bound in the prose
    cannot drift away from the bound in the code.
    """
    return RAW_MAX_BYTES + DOWN_MAX_BYTES


# ── sample -> record (pure; no I/O, no clock) ──────────────────────────────

def _num(value):
    """sparkrun's CSV value -> float, or None for anything unparseable.

    Every sample field arrives as a string (``host_monitor.sh`` CSV), and an
    unavailable reading is the EMPTY string, not a zero. Coercing '' to 0.0
    would put a fake "GPU idle" into history for a node that never answered,
    so it stays None and the downsampler averages around it.
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # A NaN/Inf from a broken probe would poison every average it joins.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def host_metrics(sample: dict | None, error: str | None = None) -> dict:
    """One sparkrun host row -> the stored per-node metrics dict.

    ``sample`` is the nested dict the monitor frame carries
    (``{host, error, sample, ...}``); it is None until a host's first reading
    lands, and ``error`` carries the collection failure. Both are kept: a
    missing sample recorded as all-None plus the error string is the honest
    point, and dropping it would leave a hole a reader cannot tell apart from
    "the daemon was down".
    """
    out: dict = {k: _num((sample or {}).get(k)) for k in HOST_KEYS}
    used, total = out.get("swap_used_mb"), out.get("swap_total_mb")
    out["swap_used_pct"] = (round(100.0 * used / total, 2)
                            if used is not None and total else None)
    out["error"] = error or None   # the one string in an otherwise numeric row
    return out


def engine_metrics(rows: list | None) -> list:
    """``ServingPoller.poll()`` rows -> the stored per-engine serving list.

    Only the fields a chart needs are kept. A row whose backend was unreachable
    carries ``error`` and no load numbers; it is recorded with the error so a
    backend that went down is visible in history as a down backend, not as a
    zero-load backend.
    """
    out = []
    for r in rows or []:
        row = {"node": r.get("node"), "port": r.get("port"),
               "model": r.get("model")}
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
    """The monitor's two sources -> one stored record, or None if there is
    nothing to record.

    ``frame`` is the newest ``sparkrun cluster monitor --json`` frame from
    ``HostStream.frame`` and ``rows`` the newest ``ServingPoller.poll()`` list.
    None is returned only when the frame has no hosts at all: a tick with no
    host row is a tick that learned nothing, and writing it would put a row of
    nulls into the ring in place of the real reading that will follow. A frame
    whose individual hosts failed IS recorded (per-host ``error``).

    ``host_error`` is the stream-level error (``HostStream.error`` — e.g.
    "sparkrun monitor exited"), stored so the gap is explained in the data
    rather than only in the daemon log.
    """
    hosts = (frame or {}).get("hosts") or []
    if not hosts and not host_error:
        return None
    rec = {"ts": int(ts), "res": "60s", "n": 1,
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
    """A fingerprint of a frame's *freshness*, or None when it cannot be told.

    ``HostStream`` keeps the newest frame it has seen; when the sparkrun child
    stops emitting (wedged, or exited between restarts) that newest frame goes
    stale but stays readable. Appending it again would record the same reading
    as a fresh sample, and a chart of a dead poller then looks like a fleet that
    flatlined calmly — the exact misreading this guard exists for.

    ``host_monitor.sh`` stamps every sample with its own ``timestamp``, so two
    frames carrying identical stamps are the same reading. Returns None when no
    host in the frame carries a stamp: then freshness is unknowable and the
    caller must record every tick (the pre-existing behaviour) rather than
    "detect" staleness it cannot detect.
    """
    hosts = (frame or {}).get("hosts") or []
    stamps = [(h.get("host"), (h.get("sample") or {}).get("timestamp"))
              for h in hosts if h.get("host")]
    if not stamps or not any(s for _h, s in stamps):
        return None
    return tuple(stamps)


# ── downsampling ───────────────────────────────────────────────────────────

def _bucket_start(ts: float, bucket_s: int = DOWN_BUCKET_S) -> int:
    """Floor an epoch onto a bucket boundary.

    Epoch seconds are UTC, so ``ts % bucket_s`` aligns buckets to the wall
    clock in every timezone at once — a 5-minute bucket means :00/:05/:10
    everywhere, which is what makes two daemon hosts' files comparable.
    """
    return int(ts) - (int(ts) % bucket_s)


def _bucket_end(ts: float, bucket_s: int = DOWN_BUCKET_S) -> int:
    """The exclusive end of ts's bucket. A bucket is final once the cutoff has
    passed this, because no later sample can belong to it."""
    return _bucket_start(ts, bucket_s) + bucket_s


def _stats(values: list):
    """(avg, min, max) of the non-None values, or (None, None, None)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None, None
    return (round(sum(vals) / len(vals), 6), min(vals), max(vals))


def _fold_hosts(records: list) -> dict:
    """Per-host field averages/peaks/floors over a bucket's records."""
    out: dict = {}
    for rec in records:
        for host, m in (rec.get("hosts") or {}).items():
            dst = out.setdefault(host, {})
            for k, v in m.items():
                if k == "error":
                    if v:
                        # A host that failed for ANY sample in the bucket is
                        # reported as having failed in the bucket: the count of
                        # good samples would hide a 2-minute ssh outage.
                        dst["error"] = v
                    continue
                src = dst.setdefault(k, [])
                src.append(v)
    folded: dict = {}
    for host, cols in out.items():
        entry: dict = {}
        peaks: dict = {}
        floors: dict = {}
        for k, vals in cols.items():
            if k == "error":
                entry[k] = vals
                continue
            avg, lo, hi = _stats(vals)
            entry[k] = avg
            if hi is not None:
                peaks[k] = hi
            if lo is not None:
                floors[k] = lo
        if peaks:
            entry[MAX_KEY] = peaks
        if floors:
            entry[MIN_KEY] = floors
        folded[host] = entry
    return folded


def _fold_engines(records: list) -> dict:
    """Per-engine averages/peaks over a bucket, keyed by node:port|model.

    Raw serving rows are a list; a bucket keys them so the same engine's
    samples land in one place across the tick set. Keying on
    ``node:port|model`` (not just port) keeps two engines on one host apart,
    and the row keeps its own node/port/model so a bucket row still reads like
    a raw row.
    """
    out: dict = {}
    for rec in records:
        for e in rec.get("engines") or []:
            key = f"{e.get('node')}:{e.get('port')}|{e.get('model')}"
            dst = out.setdefault(key, {"node": e.get("node"),
                                       "port": e.get("port"),
                                       "model": e.get("model"),
                                       "_cols": {}})
            if e.get("error"):
                dst["error"] = e["error"]
                continue
            cols = dst["_cols"]
            for k in ENGINE_KEYS:
                cols.setdefault(k, []).append(e.get(k))
    folded: dict = {}
    for key, dst in out.items():
        entry = {k: dst[k] for k in ("node", "port", "model")}
        if "error" in dst:
            entry["error"] = dst["error"]
        peaks: dict = {}
        floors: dict = {}
        for k, vals in dst["_cols"].items():
            avg, lo, hi = _stats(vals)
            entry[k] = avg
            if hi is not None:
                peaks[k] = hi
            if lo is not None:
                floors[k] = lo
        if peaks:
            entry[MAX_KEY] = peaks
        if floors:
            entry[MIN_KEY] = floors
        folded[key] = entry
    return folded


def downsample(records: list, bucket_s: int = DOWN_BUCKET_S) -> list:
    """60 s records -> one record per ``bucket_s`` bucket, oldest first.

    The bucket is the card's 5-minute row: ``ts`` is the bucket's start, ``n``
    how many raw points went into it, and every field is the AVERAGE of that
    field's non-None samples, with ``_max`` / ``_min`` sidecars carrying the
    peak and floor the average would erase (an 85 °C spike inside a 45 °C
    average is the thing an operator needs to see). Averages are not rounded to
    the raw resolution: GPU power at 12.4 W is real information.

    Bucket boundaries come from the samples themselves, so a partial bucket (a
    daemon that was down for four of the five minutes) folds what exists and
    says so with ``n`` rather than inventing the missing minutes.
    """
    groups: dict[int, list] = {}
    for rec in records:
        if rec.get("res") not in (None, "60s"):
            # Already a bucket: a fold is never fed its own output.
            continue
        ts = rec.get("ts")
        if ts is None:
            continue
        groups.setdefault(_bucket_start(ts, bucket_s), []).append(rec)
    out = []
    for start in sorted(groups):
        recs = groups[start]
        out.append({"ts": start, "res": "5m", "n": len(recs),
                    "hosts": _fold_hosts(recs),
                    "engines": _fold_engines(recs)})
    return out


# ── the store ──────────────────────────────────────────────────────────────

def _read_lines(path: str) -> tuple[list, int]:
    """(records, corrupt-line count) of a JSONL file, oldest first.

    A torn last line is the expected shape after a kill -9 mid-append, and a
    corrupt line in the middle is possible after a full disk; neither may
    crash the daemon's reader, so undecodable lines are skipped and counted
    (``stats()`` surfaces the count — silently dropping them would make a
    damaged store look healthy).
    """
    out, bad = [], 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    bad += 1
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("ts"), (int, float)):
                    out.append(rec)
                else:
                    bad += 1
    except (FileNotFoundError, NotADirectoryError):
        return [], 0
    except OSError:
        return [], 0
    return out, bad


def _write_lines(path: str, records: list) -> None:
    """Rewrite a whole file atomically (tmp + ``os.replace``).

    Same discipline as ``state.write_state``: a per-writer tmp name (two trims
    cannot fight over one tmp) and an atomic replace, so a crash mid-rewrite
    leaves the PREVIOUS file intact rather than a half-written ring.
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, separators=(",", ":"),
                                    default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _append_line(path: str, rec: dict) -> None:
    """Append one record and flush it.

    Flushing (not fsync) is the bar "survives a daemon restart" requires: the
    bytes are in the OS cache and readable by the next process immediately,
    and an uncapped fsync per tick buys nothing against a power cut that this
    store is not sized to defend (a chart, not a ledger).
    """
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        fh.flush()


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


class MetricsHistory:
    """The two on-disk rings: ``raw.jsonl`` (60 s, 24 h) and ``down.jsonl``
    (5 min, 7 d).

    The records are cached in memory after the first load, which is what makes
    a tick O(1): append one line, and only pay for a rewrite when a ring has
    actually run past its capacity or its byte cap. The cache is trusted because
    the daemon holds ``~/.hscc/daemon.lock`` — this process is the store's only
    writer, and readers open the files read-only. Call ``reload()`` after any
    external write (a test, or a human's ``rm``).
    """

    def __init__(self, hscc_dir=None, raw_capacity: int = RAW_CAPACITY,
                 down_capacity: int = DOWN_CAPACITY,
                 raw_max_bytes: int = RAW_MAX_BYTES,
                 down_max_bytes: int = DOWN_MAX_BYTES,
                 trim_slack: int = TRIM_SLACK_LINES):
        self.dir = store_dir(hscc_dir)
        self.raw_path = os.path.join(self.dir, RAW_FILE)
        self.down_path = os.path.join(self.dir, DOWN_FILE)
        self.raw_capacity = raw_capacity
        self.down_capacity = down_capacity
        self.raw_max_bytes = raw_max_bytes
        self.down_max_bytes = down_max_bytes
        self.trim_slack = trim_slack
        self.corrupt_lines = 0
        # ``_loaded`` (not ``is None`` sentinels) keeps the caches plain lists:
        # the narrowable-None version makes every method read like a None
        # check, and a reader has to re-prove what the type checker cannot.
        self._loaded = False
        self._raw: list = []
        self._down: list = []
        self.error: str | None = None

    # ── loading ────────────────────────────────────────────────────────────
    def _load(self):
        if self._loaded:
            return
        self._raw, bad = _read_lines(self.raw_path)
        self.corrupt_lines += bad
        self._down, bad = _read_lines(self.down_path)
        self.corrupt_lines += bad
        self._loaded = True

    def reload(self) -> None:
        """Drop the in-memory cache and re-read both files from disk."""
        self._loaded = False
        self._load()

    # ── writing ────────────────────────────────────────────────────────────
    def append(self, rec: dict) -> None:
        """Add one 60 s record to the raw ring (and age it if it is already old).

        Ordering is enforced on write, not on read: a record older than what is
        already in the ring (a replay, or a clock step back) is inserted into
        the cached list in position and the file is rewritten, so both files are
        always sorted by ``ts`` and every reader stays a scan with no sort.
        """
        self._load()
        rec.setdefault("res", "60s")
        rec.setdefault("n", 1)
        self.error = None
        if self._raw and rec["ts"] < self._raw[-1]["ts"]:
            lo, hi = 0, len(self._raw)
            while lo < hi:                      # bisect without the import
                mid = (lo + hi) // 2
                if self._raw[mid]["ts"] <= rec["ts"]:
                    lo = mid + 1
                else:
                    hi = mid
            self._raw.insert(lo, rec)
            _write_lines(self.raw_path, self._raw)
        else:
            self._raw.append(rec)
            try:
                _append_line(self.raw_path, rec)
            except OSError as e:
                self.error = f"raw append failed: {e}"
                self._raw.pop()                 # keep memory == disk
                raise
        self._trim(self.raw_path, self._raw, self.raw_capacity,
                   self.raw_max_bytes)

    def _trim(self, path: str, cache: list, capacity: int, max_bytes: int) -> None:
        """Enforce capacity and bytes on one ring, newest-first.

        Cheap path first: while the file sits within ``capacity + slack`` lines
        and ``max_bytes`` there is nothing to do, so the common tick is one
        append. When it does run, the newest records win — dropping the OLDEST
        is the whole point of a ring, so an operator's 24-hour window is never
        eaten by a burst of new samples.
        """
        keep = max(1, capacity)
        over_lines = len(cache) > keep + self.trim_slack
        if not over_lines and _size(path) <= max_bytes:
            return
        if len(cache) > keep:
            del cache[:len(cache) - keep]
        # Byte cap: drop from the front until the serialised size fits.
        while len(cache) > 1:
            total = sum(len(json.dumps(r, separators=(",", ":"),
                                       default=str)) + 1 for r in cache)
            if total <= max_bytes:
                break
            del cache[0]
        _write_lines(path, cache)

    # ── aging ──────────────────────────────────────────────────────────────
    def compact(self, now: float | None = None) -> dict:
        """Fold aged raw points into 5-minute buckets; prune both rings.

        Called every tick, and normally it folds nothing: the work happens once
        a bucket has aged completely out of the raw window. The rule that makes
        the fold safe is ``_bucket_end(ts) <= cutoff`` — a bucket is only
        folded when its whole span is older than the raw window, so a folded
        bucket can never receive another sample and the two files stay
        append-only between trim passes.

        Returns what it did, for the state stamp and for tests.
        """
        self._load()
        now = int(time.time()) if now is None else int(now)
        cutoff = now - RAW_RETENTION_S
        # ``_bucket_end(ts) <= cutoff`` is bucket-complete by construction:
        # every record sharing that bucket shares its end, so either the whole
        # bucket is foldable or none of it is. A bucket that still straddles
        # the cutoff simply stays in raw until its last minute ages out too —
        # which is why a fold never has to merge into a bucket already written.
        foldable = [r for r in self._raw if _bucket_end(r["ts"]) <= cutoff]
        folded_buckets = 0
        if foldable:
            new_buckets = downsample(foldable, DOWN_BUCKET_S)
            self._down.extend(new_buckets)
            self._down.sort(key=lambda r: r["ts"])
            folded_buckets = len(new_buckets)
            del self._raw[:len(foldable)]      # raw is sorted; foldable is a prefix
            _write_lines(self.raw_path, self._raw)
            _write_lines(self.down_path, self._down)

        # Prune the downsampled ring to its own 7 d window.
        down_cutoff = now - DOWN_RETENTION_S
        before = len(self._down)
        if before and self._down[0]["ts"] < down_cutoff:
            self._down = [b for b in self._down if b["ts"] >= down_cutoff]
            _write_lines(self.down_path, self._down)
        self._trim(self.raw_path, self._raw, self.raw_capacity,
                   self.raw_max_bytes)
        self._trim(self.down_path, self._down, self.down_capacity,
                   self.down_max_bytes)
        return {"folded_buckets": folded_buckets,
                "raw_points": len(self._raw), "buckets": len(self._down),
                "dropped_buckets": before - len(self._down)}

    # ── reading ────────────────────────────────────────────────────────────
    def raw_points(self, since: float | None = None, until: float | None = None,
                   host: str | None = None) -> list:
        """60 s records, oldest first, inside the window (and for one host)."""
        self._load()
        return _select(self._raw, since, until, host)

    def buckets(self, since: float | None = None, until: float | None = None,
                host: str | None = None) -> list:
        """5-minute records, oldest first, inside the window."""
        self._load()
        return _select(self._down, since, until, host)

    def series(self, since: float, until: float | None = None,
               host: str | None = None) -> list:
        """One continuous timeline: raw where raw exists, buckets before it.

        A 7-day request would otherwise hit the 24 h raw wall and come back
        with a day of points and no explanation. Every returned record carries
        ``res``, so a reader can label the resolution it is drawing instead of
        assuming one. Buckets are filtered to those that END before the raw
        window starts, so the two never overlap and no sample appears twice.
        """
        self._load()
        until = int(time.time()) if until is None else int(until)
        raw_start = until - RAW_RETENTION_S
        pts = _select(self._raw, max(since, raw_start), until, host)
        # Non-overlap is by END, not by start: a bucket that ENDS after the raw
        # window starts covers minutes that are also in raw, and a chart built
        # from both would draw those minutes twice (once as a mean, once as the
        # real points).
        b = [r for r in _select(self._down, since, until, host)
             if r["ts"] + DOWN_BUCKET_S <= raw_start]
        return b + pts

    def stats(self) -> dict:
        """What a stream stamp (and an operator) needs about the store."""
        self._load()
        newest = self._raw[-1]["ts"] if self._raw else None
        return {"raw_points": len(self._raw), "buckets": len(self._down),
                "raw_capacity": self.raw_capacity,
                "down_capacity": self.down_capacity,
                "raw_bytes": _size(self.raw_path),
                "down_bytes": _size(self.down_path),
                "newest_ts": newest,
                "oldest_ts": self._raw[0]["ts"] if self._raw else None,
                "corrupt_lines": self.corrupt_lines,
                "bound_bytes": size_bound_bytes()}


def _select(records: list, since, until, host) -> list:
    """The shared filter for every read path: window, then optional host."""
    out = []
    for r in records:
        ts = r.get("ts")
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        if host is not None and host not in (r.get("hosts") or {}):
            continue
        out.append(r)
    return out
