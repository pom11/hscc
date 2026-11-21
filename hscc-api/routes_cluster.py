"""HSCC HTTP API — Phase A2: cluster + fleet READ endpoints.

Registers read-only endpoints on ``api_server.ROUTES`` (see docs/DESIGN-api.md
§A Cluster + §A Fleet 7 health, and §B for the ``speak`` field). Every
response carries a first-class ``speak`` string derived from the actual data
in that response — never hardcoded, never fabricated.

Backing (libraries, never CLI text-parsing): the hscc-cluster engine is loaded
exactly like ``hscc_daemon.hscc._load_cluster_engine()`` — insert
``hscc-cluster/`` on sys.path, import its ``hscc.py`` as a module, call the
``cmd_*`` functions directly. Fleet/verify/stats/throughput/autoscale are the
``hscc_daemon`` package modules.

This module exposes :func:`load`, which appends to ``api_server.ROUTES``.
``api_server.py`` imports and calls it last (after ``ROUTES`` / ``ApiError``
exist), so there is no circular import: ``load`` imports ``api_server`` itself,
and importing this module on its own has no side effects. Tests import
``api_server`` (via conftest), which pulls this module in and registers the
routes.

Test seam: every backing call goes through one of the ``_backing_*`` module
functions so tests can monkeypatch them without touching real SSH/sparkrun/GPU
nodes or a live cluster.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import time
from pathlib import Path

import health_cache

# Resolve the repo root: hscc-api is a sibling of hscc-cluster and hscc_daemon.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Why the last engine load failed. The handlers include this in their error
# payload/speak instead of the old bare "unavailable", which hid the real
# failure (t_8743f98c). Kept module-level so a handler can report it without
# re-running the load.
_LAST_ENGINE_ERROR = ""


# ---------------------------------------------------------------------------
# Cluster snapshot cache — one live sparkrun/SSH read per TTL, single-flight
# ---------------------------------------------------------------------------
#
# `/v1/cluster/monitor` shells out to `sparkrun cluster monitor` and probes the
# serving layer; `/v1/cluster/status` shells out to `sparkrun status`. Several
# widgets plus the macOS menu bar refresh these independently, and before this
# cache every request paid the whole subprocess/SSH cost (W17 in the widget
# audit). The answer is a SNAPSHOT, so serving the same snapshot for a few
# seconds is honest as long as the envelope says when it was taken.
#
# TTL is 20 s: short enough that a widget never shows a stale fleet for longer
# than its own refresh cadence, long enough that a burst of widget reloads
# coalesces. `taken_at` is the cache fill time, not the request time.

CLUSTER_SNAPSHOT_TTL_S = 20.0


def _cluster_cache_now() -> float:
    """The cache's wall clock — a seam tests pin instead of sleeping."""
    return time.time()


def _taken_at_iso(ts: float) -> str:
    """Second-resolution UTC stamp in the house ``...Z`` form."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class _SnapshotFlight:
    """One in-progress build that other requests may wait on."""

    __slots__ = ("done", "value", "at", "exc")

    def __init__(self):
        self.done = threading.Event()
        self.value = None
        self.at = 0.0
        self.exc = None


class _SnapshotCache:
    """A tiny TTL cache with single-flight fills.

    A cache hit returns the stored ``(value, taken_at)``. On a miss exactly one
    caller builds while every other caller for the same key waits for that
    build; they receive the same value and timestamp. A failed build is not
    cached (the next request retries), but every waiter sees the same error.
    """

    def __init__(self, ttl: float):
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._entries = {}
        self._flights = {}

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._flights.clear()

    def get_or_build(self, key, build, now=None):
        now = _cluster_cache_now() if now is None else now
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1], hit[0]
            flight = self._flights.get(key)
            if flight is None:
                flight = _SnapshotFlight()
                self._flights[key] = flight
                leader = True
            else:
                leader = False

        if not leader:
            flight.done.wait()
            if flight.exc is not None:
                raise flight.exc
            return flight.value, flight.at

        try:
            value = build()
            at = _cluster_cache_now()
            flight.value, flight.at = value, at
            with self._lock:
                self._entries[key] = (at, value)
            return value, at
        except BaseException as exc:
            flight.exc = exc
            raise
        finally:
            with self._lock:
                self._flights.pop(key, None)
            flight.done.set()


_CLUSTER_SNAPSHOTS = _SnapshotCache(CLUSTER_SNAPSHOT_TTL_S)


def _cluster_snapshot(key, build):
    """Return ``(value, taken_at)`` from the cache, filling it once."""
    return _CLUSTER_SNAPSHOTS.get_or_build(key, build)


def _reset_cluster_snapshot_cache():
    """Test seam: drop every cached snapshot and in-flight slot."""
    _CLUSTER_SNAPSHOTS.clear()


# ---------------------------------------------------------------------------
# Backing library loading (mirrors hscc_daemon/hscc.py:_load_cluster_engine)
# ---------------------------------------------------------------------------

def _ensure_repo_root_on_path():
    """Put a root that can import ``hscc_daemon`` on sys.path.

    The repo root is first (worktree/tests). If this API was launched from a
    Homebrew Cellar directory that an upgrade deleted, that root is gone; the
    deployed Hermes plugins directory still contains ``hscc_daemon`` and is
    the same fallback the cluster-engine loader uses.
    """
    root = str(_REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    plugins = str(Path.home() / ".hermes" / "plugins")
    if plugins not in sys.path:
        sys.path.append(plugins)
    return root


def _cluster_engine_candidates():
    """Ordered hscc-cluster locations, independent of this module's path.

    The live 2026-11-18 failure was a Homebrew upgrade: the API process had
    been launched from ``/opt/homebrew/Cellar/hscc/2.15.4/...`` and Homebrew
    deleted that Cellar directory while the process kept running. Resolving
    the engine only as ``Path(__file__).parent.parent / "hscc-cluster"`` then
    returned None forever, and every cluster route answered "unavailable".

    A shipped API can therefore be started from a directory that no longer
    exists. Keep the sibling lookup first (repo/worktree tests), then fall
    back to locations that survive an upgrade: the package next to an
    already-imported ``hscc_daemon``, the deployed Hermes plugin, the stable
    Homebrew opt link, and an explicit ``HSCC_CLUSTER_DIR`` override.
    """
    candidates = []
    override = os.environ.get("HSCC_CLUSTER_DIR")
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(_REPO_ROOT / "hscc-cluster")
    try:
        import hscc_daemon
        daemon_root = Path(hscc_daemon.__file__).resolve().parent.parent
        candidates.append(daemon_root / "hscc-cluster")
    except Exception:
        pass
    candidates.append(Path.home() / ".hermes" / "plugins" / "hscc-cluster")
    candidates.append(Path("/opt/homebrew/opt/hscc/libexec/hscc/hscc-cluster"))
    seen = set()
    ordered = []
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            ordered.append(candidate)
    return ordered


def _load_cluster_engine():
    """Load hscc-cluster/hscc.py as a module (mirrors _load_cluster_engine).

    Returns the loaded module, or None if the plugin cannot be found/loaded.
    The engine's own submodules import each other by bare name, so the
    containing directory must be on sys.path while we call into it.
    """
    global _LAST_ENGINE_ERROR
    candidates = _cluster_engine_candidates()
    load_errors = []
    for cluster_dir in candidates:
        cluster_hscc = cluster_dir / "hscc.py"
        if not cluster_hscc.is_file():
            continue
        if str(cluster_dir) not in sys.path:
            sys.path.insert(0, str(cluster_dir))
        try:
            spec = importlib.util.spec_from_file_location(
                "hscc_cluster_engine", str(cluster_hscc))
            if spec is None or spec.loader is None:
                load_errors.append(f"{cluster_hscc}: no import spec")
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _LAST_ENGINE_ERROR = ""
            return mod
        except Exception as exc:  # report the real cause, try the next path
            load_errors.append(f"{cluster_hscc}: {type(exc).__name__}: {exc}")
    looked = ", ".join(str(p) for p in candidates)
    _LAST_ENGINE_ERROR = (
        "hscc-cluster engine not found (looked in: "
        f"{looked}); load errors: {'; '.join(load_errors) or 'none'}")
    return None


# ---------------------------------------------------------------------------
# Backing-call seam (monkeypatch these in tests)
# ---------------------------------------------------------------------------

def _backing_cluster_status():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_cluster_status()


def _backing_cluster_hosts():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_hosts()


def _backing_cluster_monitor():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_monitor()


def _backing_cluster_jobs():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_jobs()


def _backing_cluster_info():
    eng = _load_cluster_engine()
    if eng is None:
        return None
    return eng.cmd_info()


#: tok/s is a counter delta between two scrapes, so the poller must outlive a
#: request: a fresh poller per call had no previous sample and reported
#: ``gen_tps: null`` on every row, forever (every widget read "— tok/s").
_SERVING_POLLER = None
_SERVING_LOCK = threading.Lock()
_SERVING_LAST_AT: float | None = None
#: A previous sample older than this would turn the rate into a long average
#: (a widget polls every 30 min), so the call re-primes instead.
SERVING_RATE_MAX_GAP_S = 60.0
#: The gap between the priming scrape and the measured one.
SERVING_RATE_WINDOW_S = 2.0


def _backing_serving_snapshot(clock=time.monotonic, sleep=time.sleep):
    """The CLI monitor's serving section: one ``ServingPoller`` sample.

    ``cluster_render.render_monitor`` builds exactly this object for
    ``hscc cluster monitor`` (hscc_daemon/cluster_render.py:140-151). Returning
    the rows alongside the sparkrun frame is what gives a widget the model
    served per node, running/waiting, KV, and token totals/rates — the parts of
    the CLI view that the raw sparkrun frame does not carry.

    The poller is process-wide so consecutive calls rate against each other.
    When the last scrape is missing or older than ``SERVING_RATE_MAX_GAP_S``,
    one priming scrape runs ``SERVING_RATE_WINDOW_S`` before the real one, so
    the rate is always a current one.
    """
    global _SERVING_POLLER, _SERVING_LAST_AT
    _ensure_repo_root_on_path()
    from hscc_daemon import cluster_live
    with _SERVING_LOCK:
        if _SERVING_POLLER is None:
            _SERVING_POLLER = cluster_live.ServingPoller()
        poller = _SERVING_POLLER
        if _SERVING_LAST_AT is None or clock() - _SERVING_LAST_AT > SERVING_RATE_MAX_GAP_S:
            poller.poll()
            sleep(SERVING_RATE_WINDOW_S)
        rows = poller.poll()
        _SERVING_LAST_AT = clock()
        return {"rows": rows, "proxy_error": poller.proxy_error}


def _backing_verify():
    """Run ONE real fleet verify right now (t_7babf634).

    This is the seam the health cache and ``GET /v1/verify`` call when a
    verdict must actually be produced — it is NOT a cheap call: it does real
    chat round-trips to every serving GPU unit. ``handle_health`` no longer
    calls it per request; it hands it to :func:`health_cache.get_snapshot`,
    which decides (cache hit / stale revalidate / one inline run) whether a
    verify is needed at all.
    """
    from hscc_daemon import verify
    return verify.run_all()


def _backing_stats(days):
    from hscc_daemon import stats
    return stats.compute_stats(since_days=days)


def _backing_throughput():
    from hscc_daemon import throughput
    return throughput.compute_throughput()


def _backing_usage():
    from hscc_daemon import usage
    return usage.compute_usage()


def _backing_streams():
    from hscc_daemon import state
    return state.read_all_states()


def _backing_autoscale():
    from hscc_daemon import throughput, autoscale
    tp = throughput.compute_throughput()
    current = tp.get("fleet", {}).get("nodes_ok", 0) or len(tp.get("by_node", {}))
    return autoscale.decide_scale(tp, current_workers=current)


def _is_error_dict(data) -> bool:
    """True when a backing call returned a failure-shaped dict."""
    if not isinstance(data, dict):
        return False
    if data.get("error"):
        return True
    # run_cmd-style results: success False with no usable payload.
    if data.get("success") is False and not data.get("json"):
        return True
    return False


def _one_line(value, limit=500) -> str:
    """Collapse a multi-line command error into one readable sentence."""
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _failure_detail(data=None, exc=None) -> str:
    """A specific reason for a degraded route — never the word "unavailable".

    The old handlers swallowed every cause into "… unavailable", which is why
    the live 2026-11-18 outage (a Homebrew upgrade deleted the directory the
    API process was launched from) was undiagnosable from the wire. The detail
    is the backing function's own ``error``/exit status, the engine-load
    reason, or the exception class + message. It is command/stderr text, never
    the auth token.
    """
    if exc is not None:
        return f"{type(exc).__name__}: {_one_line(exc)}"
    if isinstance(data, dict):
        if data.get("error"):
            return _one_line(data["error"])
        rc = data.get("returncode")
        output = data.get("output")
        if rc is not None:
            detail = f"command exited {rc}"
            if output:
                detail += f": {_one_line(output)}"
            return detail
        if output:
            return _one_line(output)
    if data is None and _LAST_ENGINE_ERROR:
        return _one_line(_LAST_ENGINE_ERROR)
    return "backing command returned no data"


def _degraded(lead, data=None, exc=None):
    detail = _failure_detail(data, exc)
    return {"error": detail, "speak": f"{lead}: {detail}"}


# ---------------------------------------------------------------------------
# The node-pause overlay — a parked node reads PAUSED, never down
# ---------------------------------------------------------------------------
# WHY THE ROUTE PROJECTS THIS AND NOT THE BACKING. ``hscc node pause`` stops a
# unit through sparkrun and records the intent in ``~/.hscc/node_pauses.json``
# (hscc_daemon/node_pauses.py). Every read path downstream of that stop — the
# sparkrun host table, the serving poller, the widget tally — then sees the
# same thing a crash sees: no container, no answering engine. Without an
# overlay read at the API edge a paused node is therefore reported as DOWN, and
# the operator's deliberate act is rendered as a fault: the widget paints it
# hot, the Needs-me badge fires, and the fleet looks broken while it is doing
# exactly what they asked. The repair loops already skip parked nodes
# (watchdog auto-heal, keepalive, ``cluster up``, autoscale); this is the same
# overlay on the VISIBILITY side, so the fleet reads the same way everywhere.
#
# THE THREE FIELD NAMES ARE A CONTRACT, NOT A SUGGESTION. ``status``,
# ``paused_reason`` and ``paused_since`` are what the iOS/macOS widgets read
# (t_983f177e), so renaming or reshaping one is a breaking change for shipped
# bundles, not a refactor. They are additive: a row that is not parked keeps
# exactly the keys it had before this existed, which is why the parity gates on
# the monitor payload still pass.
#
# WHY THE OVERLAY IS READ PER REQUEST, NOT CACHED. The sparkrun frame and the
# serving rows behind them cost subprocesses, so they live in the 20 s snapshot
# cache above. The overlay is one small local JSON file, and pausing or
# resuming must be visible on the NEXT request rather than up to a TTL later —
# so it is read at request time and merged onto a COPY of the cached rows.
# Nothing here mutates a cached object: the cache holds the pre-overlay truth.

#: The value ``status`` carries on a parked row. One constant so a route,
#: a test and the widget contract can never drift into three spellings.
PAUSE_STATUS = "paused"

#: The keys a parked row gains. Stable contract — see the block above.
PAUSE_ROW_KEYS = ("status", "paused_reason", "paused_since")


def _backing_node_pauses(directory=None):
    """The durable node-pause overlay as ``{host: entry}``.

    The monkeypatch seam the route tests use, like every other ``_backing_*``
    here. ``directory`` is threaded through to :func:`node_pauses.list_pauses`
    because that module's documented test seam is an explicit root, never an
    environment variable: a root passed in is one root both sides of the call
    share, while a root read from the process env is a root no other process
    (the CLI that wrote the overlay) can ever have had. Handlers pass
    ``ctx.hscc_dir``, which in production resolves to HOME's ``~/.hscc`` — the
    same root the CLI writes — and in a test is that test's own tmp dir.
    """
    from hscc_daemon import node_pauses
    return node_pauses.list_pauses(directory)


def _node_key(value):
    """A comparable node identity, or ``None`` when this value names none.

    Deliberately not clever: trim and case-fold, no address normalisation, no
    hostname suffix stripping. The overlay is keyed by the host string the
    operator passed to ``hscc node pause``, which is the same string sparkrun
    uses in its rows. Guessing that ``node-3`` means ``192.0.2.13`` would
    invent a paused node on a machine nobody parked, which is a worse lie than
    a missing overlay tag on an aliased host.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed.casefold() if trimmed else None


def _pause_row(reason, since):
    """The three contract keys, always all three, in the documented order."""
    return {"status": PAUSE_STATUS,
            "paused_reason": reason if isinstance(reason, str) else "",
            "paused_since": since if isinstance(since, str) else None}


def _pause_overlay(directory=None):
    """``{node: {status, paused_reason, paused_since}}`` for parked nodes.

    Span-aware on purpose. A pause record is keyed by the host the operator
    named, and a unit that spans several hosts is parked as a whole — the
    machines named only in ``span_nodes`` are just as out of service as the
    keyed one, and painting them down would fault the operator's own action
    twice. That is ``node_pauses.covering``'s rule; this is its projection.

    A host's OWN record wins over a span record that happens to name it
    (``covering`` prefers the keyed entry, and the operator's own reason is the
    truthful one to show).

    Fails open to ``{}``: an unreadable overlay is already ``{"pauses": {}}``
    inside :func:`node_pauses.load`, and the one thing left that could raise is
    the import. A route must never turn a fleet read into an error because the
    overlay could not be parsed — the overlay WARNs about itself.
    """
    try:
        pauses = _backing_node_pauses(directory)
    except Exception:  # noqa: BLE001 - a visibility tag is never worth a 500
        return {}
    overlay = {}
    if not isinstance(pauses, dict):
        return overlay
    for host, entry in pauses.items():
        if not isinstance(entry, dict):
            continue
        row = _pause_row(entry.get("reason"), entry.get("since"))
        keyed = _node_key(host)
        if keyed:
            overlay[keyed] = dict(row)
        for node in entry.get("span_nodes") or []:
            spanned = _node_key(node)
            if spanned:
                overlay.setdefault(spanned, dict(row))
    return overlay


def _paused_nodes(overlay):
    """The overlay as the payload's own ``paused_nodes`` list, sorted by node.

    Why a list and not only the annotated rows: a parked host can be MISSING
    from the monitor frame entirely — sparkrun does not list a machine whose
    workloads are stopped, and an absent row is a row there is nothing to tag.
    A client that only walked the rows would count such a node as gone, or
    subtract nothing from its down tally. This is the authoritative "these
    nodes are parked, by choice, since then" set, independent of what the
    sample managed to reach.
    """
    return [dict({"node": node}, **overlay[node]) for node in sorted(overlay)]


def _pause_overlay_for(ctx):
    """The pause overlay for the root THIS request carries, or nothing at all.

    Two failures this refuses, in both directions:

    * A request whose context has no root must not fall back to the default
      ``~/.hscc``. A route reading a root nobody named for it is the root-split
      bug class this repo has already been bitten by — the reader ends up
      pointed at a tree the writer (the CLI, another profile, a test's sealed
      home) can never have written. No named root therefore means no tags, not
      a guessed root.
    * It must not raise. ``GET /v1/health`` is the connection probe the apps
      poll every 15 s; a missing context turning that into a 500 would take the
      app's "the API is alive" signal down with it.

    Handlers pass the request context they already have (``ctx.hscc_dir``),
    which is the root the server was started with and — in production — the
    same ``~/.hscc`` the CLI writes the overlay into.
    """
    root = getattr(ctx, "hscc_dir", None)
    if root is None:
        return {}
    return _pause_overlay(root)


def _annotate_paused_rows(rows, overlay, keys=("host", "node", "name")):
    """A copy of ``rows`` with the pause keys added to parked-node rows.

    ``keys`` is the row's identity field, tried in order, because the two row
    families this route carries name their machine differently: the sparkrun
    host table uses ``host``, the serving rows use ``node``.

    Rows the overlay does not hold come back as the SAME objects, so an
    unpaused payload is byte-identical to the pre-overlay response — including
    its key order and its ``error`` text. Rows it does hold come back as new
    dicts, because the rows in hand belong to the shared snapshot cache and
    writing into them there would leak one request's pause state into every
    later request for the rest of the TTL.

    A parked row keeps its ``error``: "could not sample this host" may still be
    true, and a node the operator parked is not a reason to lose a real
    diagnostic. What the row promises instead is that ``status`` says
    ``"paused"`` — and a consumer that derives ``down`` from ``error`` or from
    a missing sample MUST ask ``status`` first. That is the widget rule
    (t_983f177e), and it is why ``paused`` and ``down`` can never collide.
    """
    if not overlay or not isinstance(rows, list):
        return rows
    out = []
    for row in rows:
        fields = None
        if isinstance(row, dict):
            for key in keys:
                node = _node_key(row.get(key))
                if node and node in overlay:
                    fields = overlay[node]
                    break
        out.append({**row, **fields} if fields else row)
    return out


def _with_pause_overlay(payload, ctx):
    """The monitor payload with parked nodes tagged, as a NEW payload dict.

    Tags both row families the route carries — the sparkrun host table under
    ``json.hosts``/``json.nodes`` and the serving rows under ``serving`` — and
    states the parked set at the top level. The payload handed in (the cached
    snapshot's) is never modified.
    """
    overlay = _pause_overlay_for(ctx)
    out = dict(payload)
    frame = payload.get("json")
    if isinstance(frame, dict):
        frame = dict(frame)
        for key in ("hosts", "nodes"):
            if key in frame:
                frame[key] = _annotate_paused_rows(frame[key], overlay)
        out["json"] = frame
    if "serving" in payload:
        out["serving"] = _annotate_paused_rows(payload["serving"], overlay,
                                               keys=("node", "host", "name"))
    out["paused_nodes"] = _paused_nodes(overlay)
    return out


# ---------------------------------------------------------------------------
# speak helpers (pure — take the computed dict, return the sentence)
# ---------------------------------------------------------------------------

def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _speak_cluster_status(data):
    """§B: "{total} hosts up. {n} workload(s) running, {i} idle."."""
    total = data.get("total_hosts", 0)
    workloads = data.get("workloads", [])
    idle = data.get("idle_hosts", [])
    return (
        f"{total} hosts up. "
        f"{len(workloads)} workload{'s' if len(workloads) != 1 else ''} running, "
        f"{len(idle)} idle."
    )


def _count_saved_clusters(saved):
    """Count saved clusters defensively (list -> len; run_cmd dict -> best-effort)."""
    if isinstance(saved, list):
        return len(saved)
    if isinstance(saved, dict):
        # run_cmd result carrying parsed json (e.g. {"name": ..., "hosts": [...]}).
        parsed = saved.get("json")
        if isinstance(parsed, list):
            return len(parsed)
        if isinstance(parsed, dict) and parsed:
            return 1
    return None


def _speak_cluster_hosts(data):
    """§B: "{len(hosts)} hosts registered. {N} cluster(s) saved."."""
    hosts = data.get("hosts", [])
    n = _count_saved_clusters(data.get("saved_clusters"))
    base = f"{len(hosts)} hosts registered."
    if n is not None:
        return f"{base} {_plural(n, 'cluster')} saved."
    return base


def _speak_cluster_monitor(data):
    """§B: compact aggregate from the JSON, else "fleet monitor unavailable"."""
    parsed = data.get("json") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or not parsed:
        return "fleet monitor unavailable"
    # Prefer a host/node count if the sample names them; else count metrics.
    for key in ("nodes", "hosts"):
        val = parsed.get(key)
        if isinstance(val, (list, dict)) and val:
            return f"Fleet snapshot: {len(val)} {'host' if len(val) == 1 else 'hosts'} sampled."
    return f"Fleet snapshot: {len(parsed)} metric{'s' if len(parsed) != 1 else ''} reported."


def _speak_cluster_jobs(data):
    """§B: "{count} job(s) running." from the output, else "job list unavailable"."""
    output = data.get("output") if isinstance(data, dict) else None
    if output is None:
        return "job list unavailable"
    count = 0
    for line in str(output).splitlines():
        line = line.strip()
        if not line:
            continue
        if "Total:" in line and "job" in line.lower():
            # e.g. "Total: 3 job(s) across ..." — extract the leading integer.
            tokens = line.split()
            if tokens and tokens[0].lstrip("-").isdigit():
                count = int(tokens[0])
                break
        elif line.startswith(("Job:", "Pending:", "Running:", "Queued:")):
            count += 1
    return _plural(count, "job") + " running."


def _speak_cluster_info(data):
    """§B: "Cluster configuration loaded." / "cluster info unavailable"."""
    if not isinstance(data, dict):
        return "cluster info unavailable"
    return "Cluster configuration loaded."


def _humanize_age(seconds) -> str:
    """A spoken age: ``45s`` / ``7m`` / ``3h12m`` / ``2d04h``."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def _speak_health(data, age_seconds=None, stale=False):
    """§B: ok ? "All checks passed." : "{N} of {total} checks have problems."."""
    checks = data.get("checks", [])
    ok = data.get("ok")
    if ok:
        sentence = "All checks passed."
    else:
        failed = [c for c in checks if not c.get("ok")]
        total = len(checks)
        names = ", ".join(str(c.get("name", "?")) for c in failed)
        sentence = f"{len(failed)} of {total} checks have problems."
        if names:
            sentence += f" ({names})"
    if stale:
        # t_7babf634: this verdict is a cached one older than the TTL. Say how
        # old it is rather than reading like a check that just ran.
        sentence += f" Health from {_humanize_age(age_seconds)} ago."
    return sentence


def _speak_stats(data):
    """§B: "Last {days} days: {key stat}." — the single most useful number."""
    days = data.get("since_days", 7)
    total = (data.get("completions") or {}).get("total", 0)
    return f"About {total} work items across the last {days} days."


def _speak_throughput(data):
    """§B: "{nodes_ok} of {nodes_total} nodes healthy". """
    fleet = data.get("fleet", {})
    ok = fleet.get("nodes_ok", 0)
    total = fleet.get("nodes_total", 0)
    return f"{ok} of {total} nodes healthy."


def _speak_usage(data):
    """§B: budget/activity sentence from the real usage dict.

    Prefers a cost sentence when cost is actually tracked; otherwise reports
    the real token activity across bots and projects. Never fabricated.
    """
    budget = data.get("budget") or {}
    total = data.get("total") or {}
    spent = budget.get("spent_usd", 0.0)
    configured = budget.get("configured", False)
    n_bots = len(data.get("per_bot", {}))
    n_projects = len(data.get("per_project", {}))
    activity = f"{n_bots} bots across {n_projects} projects"
    if data.get("cost_tracked"):
        pct = budget.get("pct", 0.0)
        if budget.get("exceeded"):
            return f"Budget exceeded: ${spent:.2f} over budget ({pct:.0f}%). {activity}."
        if configured:
            return (f"{activity}: ${spent:.2f} spent "
                    f"({pct:.0f}% of ${budget.get('budget_usd', 0):.2f} budget).")
        return f"{activity}: ${spent:.2f} tracked spend."
    # Cost is not tracked — report the real token activity honestly.
    total_tokens = total.get("total_tokens", 0)
    if total_tokens:
        return (f"{activity}: {total_tokens:,} tokens used; cost not tracked "
                "on this cluster.")
    return f"{activity}; cost not tracked on this cluster."


def _speak_streams(data):
    """§B: "Daemon streams: all ok." / enumerate blocked/failed ones."""
    if not isinstance(data, dict):
        return "Daemon streams: status unavailable."
    blocked = [
        name for name, s in data.items()
        if isinstance(s, dict) and s.get("ok") is not True
    ]
    if not blocked:
        return "Daemon streams: all ok."
    return f"Daemon streams: {len(blocked)} blocked: {', '.join(blocked)}."


def _speak_autoscale(data):
    """§B: humanize the decision."""
    action = data.get("action") if isinstance(data, dict) else None
    if action == "scale_up":
        tgt = data.get("target")
        if tgt is not None:
            return f"Autoscale suggests scaling up to {tgt} workers."
        return "Autoscale suggests scaling up."
    if action == "scale_down":
        tgt = data.get("target")
        if tgt is not None:
            return f"Autoscale suggests scaling down to {tgt} workers."
        return "Autoscale suggests scaling down."
    if action == "none":
        return "Autoscale: nothing to change."
    return "Autoscale: no decision available."


# ---------------------------------------------------------------------------
# Handlers (read-only; 200 with degraded speak on a backing failure, except
# /v1/cluster/monitor which answers 503 + success:false so the app cannot read
# a failed sample as a serving fleet)
# ---------------------------------------------------------------------------

def _cluster_status_snapshot():
    """The cached status read: the backing payload or a failure envelope."""
    try:
        data = _backing_cluster_status()
    except Exception as exc:
        return {"ok": False, "detail": _failure_detail(exc=exc)}
    if data is None or _is_error_dict(data):
        return {"ok": False, "detail": _failure_detail(data)}
    return {
        "ok": True,
        "payload": {
            "workloads": data.get("workloads", []),
            "idle_hosts": data.get("idle_hosts", []),
            "total_hosts": data.get("total_hosts", 0),
            "speak": _speak_cluster_status(data),
        },
    }


def handle_cluster_status(server, ctx, query, body):
    snapshot, taken_at = _cluster_snapshot("status", _cluster_status_snapshot)
    taken = _taken_at_iso(taken_at)
    if not snapshot["ok"]:
        detail = snapshot["detail"]
        return 200, {
            "workloads": [], "idle_hosts": [], "total_hosts": 0,
            "error": detail,
            "speak": f"Cluster status failed: {detail}",
            "taken_at": taken,
        }
    return 200, {**snapshot["payload"], "taken_at": taken}


def _cluster_monitor_snapshot():
    """The cached monitor read: sparkrun frame + the CLI's serving section."""
    try:
        data = _backing_cluster_monitor()
    except Exception as exc:
        return {"ok": False, "detail": _failure_detail(exc=exc)}
    if data is None or _is_error_dict(data):
        return {"ok": False, "detail": _failure_detail(data)}
    payload = {**data, "speak": _speak_cluster_monitor(data)}
    # Add the same serving rows `hscc cluster monitor` prints beside the host
    # table (model per node, run/wait, KV, token totals and rates). Additive:
    # older clients decode only `json`, new ones may read `serving`.
    try:
        serving = _backing_serving_snapshot()
    except Exception as exc:
        payload["serving"] = []
        payload["serving_error"] = _failure_detail(exc=exc)
    else:
        payload["serving"] = serving.get("rows", [])
        payload["serving_proxy_error"] = serving.get("proxy_error")
    return {"ok": True, "payload": payload}


def handle_cluster_monitor(server, ctx, query, body):
    snapshot, taken_at = _cluster_snapshot("monitor", _cluster_monitor_snapshot)
    taken = _taken_at_iso(taken_at)
    if not snapshot["ok"]:
        detail = snapshot["detail"]
        # A degraded monitor is not a 200 success: HTTP 200 with no `json` was
        # indistinguishable from a healthy empty fleet, and the app counted the
        # daemon host as "serving". Non-2xx + success:false makes every client
        # take its offline/degraded path instead.
        return 503, {
            "success": False,
            "degraded": True,
            "error": detail,
            "speak": f"Fleet monitor failed: {detail}",
            "taken_at": taken,
        }
    return 200, _with_pause_overlay({
        **snapshot["payload"],
        "success": True,
        "degraded": False,
        "taken_at": taken,
    }, ctx)


def handle_cluster_hosts(server, ctx, query, body):
    try:
        data = _backing_cluster_hosts()
    except Exception as exc:
        return 200, _degraded("Host list failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Host list failed", data)
    payload = {
        "hosts": data.get("hosts", []),
        "saved_clusters": data.get("saved_clusters", {}),
        "live_status": data.get("live_status", {}),
    }
    payload["speak"] = _speak_cluster_hosts(payload)
    return 200, payload


def handle_cluster_jobs(server, ctx, query, body):
    try:
        data = _backing_cluster_jobs()
    except Exception as exc:
        return 200, _degraded("Job list failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Job list failed", data)
    return 200, {**data, "speak": _speak_cluster_jobs(data)}


def handle_cluster_info(server, ctx, query, body):
    try:
        data = _backing_cluster_info()
    except Exception as exc:
        return 200, _degraded("Cluster info failed", exc=exc)
    if data is None or _is_error_dict(data):
        return 200, _degraded("Cluster info failed", data)
    return 200, {**data, "speak": _speak_cluster_info(data)}


def handle_health(server, ctx, query, body):
    """GET /v1/health — the LAST verify snapshot, never a verify run.

    t_7babf634. This route used to call :func:`_backing_verify` — every check
    of the fleet smoke test, real GPU chat round-trips included — on the
    request thread, once per poll, with no cache and no dedupe. It now serves
    the snapshot :mod:`health_cache` keeps: fresh → answered immediately with
    the verify untouched; older than the TTL → answered immediately with the
    stale verdict while ONE background refresh runs; nothing cached anywhere →
    exactly one inline verify, which every concurrent caller waits for instead
    of starting its own.

    The payload stays a strict superset of what it was ({ok, checks, speak}) so
    the iOS ``HealthResponse`` keeps decoding; ``age_seconds``, ``generated_at``,
    ``stale`` and ``source`` are additive.

    So is ``paused_nodes`` (t_824b617f): this route answers "is the fleet
    healthy", and a node the operator parked on purpose is not a fault. The
    overlay is read per request — the same read the monitor route makes, so the
    two routes can never disagree about which nodes are parked — and it is the
    only node-level information here, because the checks are named fleet checks
    rather than per-machine rows. Clients that show a down count subtract this
    list from it. The verdict itself (``ok``, ``checks``) is NOT edited here:
    suppressing a parked unit inside the checks is the verify layer's business,
    and quietly greying out a red check at the HTTP edge would launder a real
    signal into a reassuring one.
    """
    try:
        snap = health_cache.get_snapshot(verify=_backing_verify)
    except Exception:
        return 200, {"speak": "health check unavailable"}
    if snap is None:
        return 200, {"speak": "health check unavailable"}
    data = snap["result"]
    return 200, {
        "ok": data.get("ok", False),
        "checks": data.get("checks", []),
        "speak": _speak_health(data, age_seconds=snap["age_seconds"],
                               stale=snap["stale"]),
        "age_seconds": snap["age_seconds"],
        "generated_at": snap["generated_at"],
        "stale": snap["stale"],
        "source": snap["source"],
        "paused_nodes": _paused_nodes(_pause_overlay_for(ctx)),
    }


def _parse_days(query):
    """parse ?days=N mirroring _handle_stats: default 7, int, clamped >= 0."""
    raw = query.get("days")
    if raw is None:
        return 7
    try:
        days = int(raw)
    except (ValueError, TypeError):
        return 7
    return max(days, 0)


def handle_fleet_stats(server, ctx, query, body):
    days = _parse_days(query)
    try:
        data = _backing_stats(days)
    except Exception:
        return 200, {"speak": "fleet stats unavailable"}
    if not isinstance(data, dict):
        return 200, {"speak": "fleet stats unavailable"}
    return 200, {**data, "speak": _speak_stats(data)}


def handle_fleet_throughput(server, ctx, query, body):
    try:
        data = _backing_throughput()
    except Exception as exc:
        return 200, _degraded("Fleet throughput failed", exc=exc)
    if not isinstance(data, dict) or data.get("fleet") is None:
        return 200, _degraded("Fleet throughput failed", data)
    return 200, {**data, "speak": _speak_throughput(data)}


def handle_fleet_usage(server, ctx, query, body):
    try:
        data = _backing_usage()
    except Exception:
        return 200, {"speak": "fleet usage unavailable"}
    if not isinstance(data, dict):
        return 200, {"speak": "fleet usage unavailable"}
    return 200, {**data, "speak": _speak_usage(data)}


def handle_fleet_streams(server, ctx, query, body):
    try:
        data = _backing_streams()
    except Exception:
        return 200, {"speak": "Daemon streams: status unavailable."}
    if not isinstance(data, dict):
        return 200, {"speak": "Daemon streams: status unavailable."}
    return 200, {"streams": data, "speak": _speak_streams(data)}


def handle_autoscale(server, ctx, query, body):
    try:
        data = _backing_autoscale()
    except Exception:
        return 200, {"speak": "Autoscale: no decision available."}
    if not isinstance(data, dict):
        return 200, {"speak": "Autoscale: no decision available."}
    return 200, {**data, "speak": _speak_autoscale(data)}


# ---------------------------------------------------------------------------
# GET /v1/cluster/history — the recorder's stored series, READ-ONLY
# ---------------------------------------------------------------------------
#
# WHY THIS ROUTE DOES NOT LOOK AT THE FLEET. Every other route here runs a live
# read (`cmd_monitor` ssh-es to every host). This one answers "what did the
# fleet look like at 14:05", and the only thing that knows is the recorder
# (``hscc_daemon/metrics_history.py``). So the entire data path is the
# recorder's OWN read API — ``MetricsStore.host_rows`` / ``engine_rows`` for
# tiers 1-2, ``MetricsRecorder.tier0_points`` for tier 0 — and nothing else: no
# sampling, no ssh, no sparkrun, and deliberately no ``get_sampler()``, which
# constructs a Sampler on first use and would spawn a
# ``sparkrun cluster monitor`` child from a GET.
# tests/test_cluster_history_parity.py pins that three ways (AST sweep of this
# handler, every live-backing seam poisoned, and a missing store left missing).
#
# THE TIER IS PART OF THE ANSWER, NOT AN IMPLEMENTATION DETAIL. The three tiers
# have different resolutions (2 s / 1 min / 5 min) and different columns (a
# peak only exists after a roll-up), so a response that just said "here is your
# series" would leave a chart guessing whether a point is 2 seconds or 5 minutes
# wide. Every response therefore states `tier`, `resolution_s`, `resolution`,
# `tier_summary` and `max_fields` — the same fields for every window.

#: A host name as the recorder keys it: sparkrun host, DNS name, or a
#: tailnet/IPv6 address. Anything with a space, a slash or a `?` is a client
#: bug rather than a host, and gets a 400 naming the rule.
_HISTORY_NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,253}")

#: the window a client may ask for, mapped to the recorder's own (tier,
#: bucket_s, span_s). Built from ``metrics_history``'s constants rather than
#: literals so this route cannot drift from the cadence that produced the data:
#: a client asking for 1 h is being served tier 1's 1-minute buckets because
#: that is what tier 1 IS.
def _history_windows(mh) -> dict:
    return {
        "15m": (0, mh.TIER0_INTERVAL_S, mh.TIER0_WINDOW_S),
        "1h": (1, mh.TIER1_BUCKET_S, 60 * 60),
        "6h": (1, mh.TIER1_BUCKET_S, 6 * 60 * 60),
        "24h": (1, mh.TIER1_BUCKET_S, mh.TIER1_WINDOW_S),
        "7d": (2, mh.TIER2_BUCKET_S, mh.TIER2_WINDOW_S),
    }


#: Keys that identify a row and are never dropped by `metrics=`.
_HISTORY_HOST_IDENTITY = ("tier", "ts", "host", "res", "n", "error")
_HISTORY_ENGINE_IDENTITY = ("tier", "ts", "node", "port", "model", "res", "n",
                            "error")

#: Every parameter the route accepts, in request-axis order. An unknown key is
#: a 400 that names this whole set — `host=` deliberately is NOT in it:
#: `host_rollup.host` and `engine_rollup.node` are two column names for one
#: identity, so `node=` stays the single "which machine" filter for both series.
_HISTORY_QUERY_KEYS = ("node", "metrics", "window", "tier", "since", "until")

#: The two request axes, stated on the response as `axis` so a client never
#: infers "which axis was asked" from which keys happen to be non-null.
AXIS_WINDOW = "window"
AXIS_TIER = "tier"

#: Where the served rows came from, as a CODE. A client must branch on this, not
#: on the English of `note` (the same reason `error.code` exists).
SOURCE_RING = "recorder_ring"
SOURCE_RING_ABSENT = "recorder_ring_absent"
SOURCE_STORE = "sqlite_store"
SOURCE_STORE_ABSENT = "sqlite_store_absent"


def _metrics_history():
    """The recorder module, imported on first use (never at import time)."""
    _ensure_repo_root_on_path()
    from hscc_daemon import metrics_history
    return metrics_history


def _history_now() -> float:
    """The wall clock for a window's edge — the seam the parity test pins."""
    return time.time()


def _history_vocab(mh, tier: int) -> tuple:
    """(host field names, engine field names) this tier actually stores.

    Tier 0 holds raw samples, so it has no ``<field>_max`` column: a peak only
    exists once a bucket has been rolled up. Asking for `gpu_temp_c_max` at
    window=15m is therefore a 400, not an empty column.
    """
    host_vocab = tuple(mh.HOST_KEYS) if tier == 0 else tuple(mh.HOST_COLUMNS)
    return host_vocab, tuple(mh.ENGINE_KEYS)


def _history_tier_summary(mh, tier: int, bucket_s: int) -> str:
    """What the served tier is, in the client's own terms."""
    if tier == 0:
        return (f"raw {bucket_s} s samples from the recorder's in-memory ring — "
                "no averaging, and no peak columns")
    step = f"{bucket_s // 60}-minute" if bucket_s % 60 == 0 else f"{bucket_s} s"
    peaks = ", ".join(f"{k}_max" for k in mh.MAX_KEYS)
    return f"{step} averages, with peaks for {peaks}"


def _parse_history_window(query, mh) -> tuple:
    """?window= -> (window, tier, bucket_s, span_s). 400 on anything else."""
    from api_server import ApiError
    windows = _history_windows(mh)
    raw = (query.get("window") or "1h").strip()
    if raw not in windows:
        raise ApiError(
            400, "bad_request",
            f"window must be one of {', '.join(windows)} (got {raw!r}); each "
            "serves a different tier, and the response states which one",
            "window must be 15m, 1 hour, 6 hours, 24 hours or 7 days.",
        )
    tier, bucket_s, span_s = windows[raw]
    return raw, tier, bucket_s, span_s


def _parse_history_node(query) -> str:
    """?node= -> a host name, or 'all'. 400 on an empty or impossible name."""
    from api_server import ApiError
    node = (query.get("node") or "all").strip()
    if not node:
        raise ApiError(400, "bad_request",
                       "node must be 'all' or one host name (an empty value is "
                       "not a request for every host)",
                       "node must be all or one host name.")
    if node != "all" and not _HISTORY_NODE_RE.fullmatch(node):
        raise ApiError(
            400, "bad_request",
            f"node {node!r} is not a host name: use 'all' or a single sparkrun "
            "host / DNS name / tailnet address (letters, digits, . - _ :)",
            "node must be all or a single host name.",
        )
    return node


def _parse_history_metrics(query, mh, tier: int):
    """?metrics=a,b -> the ordered, de-duplicated names to keep, or None for all.

    The vocabulary is the recorder's own (``HOST_KEYS`` / ``HOST_COLUMNS`` /
    ``ENGINE_KEYS``) rather than a list copied into this module, so a rename in
    the recorder shows up here as a rejected name rather than as a silently
    missing column — and a client that asks for a peak at tier 0 is told why,
    instead of receiving a hole.
    """
    from api_server import ApiError
    raw = query.get("metrics")
    if raw is None:
        return None
    names: list = []
    for part in str(raw).split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise ApiError(
            400, "bad_request",
            "metrics must name at least one field (e.g. "
            "metrics=gpu_util_pct,gpu_temp_c); an empty list is not a request "
            "for all of them",
            "metrics must name at least one field.",
        )
    host_vocab, engine_vocab = _history_vocab(mh, tier)
    valid = ", ".join(sorted(set(host_vocab) | set(engine_vocab)))
    for name in names:
        if name in host_vocab or name in engine_vocab:
            continue
        if name in mh.HOST_COLUMNS:      # a peak, valid only once rolled up
            raise ApiError(
                400, "bad_request",
                f"{name} is not stored for a 15 m window: tier 0 keeps raw 2 s "
                "samples, so a peak column exists only after a roll-up "
                "(window=1h, 6h, 24h or 7d)",
                f"{name} is only available for rolled-up windows.",
            )
        raise ApiError(
            400, "bad_request",
            f"unknown metric {name!r} for this window; the recorder stores: "
            f"{valid}",
            f"{name} is not a metric the recorder stores.",
        )
    return names


# --------------------------------------------------------------------------- #
# The two request axes
# --------------------------------------------------------------------------- #

def _history_retention_s(mh, tier: int) -> int:
    """How deep one tier actually goes, read from the module that owns it.

    Hardcoding 900/86400/604800 here would let the response lie the day the
    recorder's cadence changes, so the numbers come from ``metrics_history``.
    """
    if tier == 0:
        return mh.TIER0_WINDOW_S
    if tier == 1:
        return mh.TIER1_WINDOW_S
    return mh.TIER2_WINDOW_S


def _history_bucket_s(mh, tier: int) -> int:
    """The bucket width of one tier, again the recorder's own number."""
    if tier == 0:
        return mh.TIER0_INTERVAL_S
    return mh.TIER1_BUCKET_S if tier == 1 else mh.TIER2_BUCKET_S


def _parse_history_tier(query, mh) -> int:
    """?tier= -> 0, 1 or 2. 400 on anything else.

    Required on the tier axis rather than defaulted: "which resolution am I
    looking at" is the one thing a chart may not guess, and a silent default is
    how a 2 s widget starts rendering 5-minute averages.
    """
    from api_server import ApiError
    raw = str(query.get("tier") or "").strip()
    try:
        tier = int(raw)
    except (TypeError, ValueError):
        tier = -1
    if tier not in (0, 1, 2):
        raise ApiError(
            400, "bad_request",
            f"tier must be 0, 1 or 2 (got {raw!r}): 0 is the 2 s ring (15 min), "
            "1 the 1-minute roll-ups (24 h), 2 the 5-minute roll-ups (7 d)",
            "tier must be 0, 1 or 2.",
        )
    return tier


def _parse_history_epoch(query, name) -> int | None:
    """?since= / ?until= -> an inclusive epoch-second bucket start, or None.

    Ported from the retired ``/v1/metrics/history`` parser, including the
    floor-not-round: the store's buckets are integer second offsets aligned to
    the wall clock, so flooring keeps the "inclusive bucket start" meaning the
    store documents, where rounding would silently admit a bucket the client
    did not ask for.
    """
    from api_server import ApiError
    raw = query.get(name)
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


def _resolve_history_axis(query, mh) -> dict:
    """Decide which axis this request is, and the span it will actually serve.

    Two ways to pick a span, and they are not interchangeable:

      * ``window=15m|1h|6h|24h|7d`` — a **preset anchored to `now`**. It implies
        the tier, and its span is fixed.
      * ``tier=0|1|2&since=&until=`` — a **raw tier with an explicit span**, for
        "the hour around last night's slow run". It anchors to nothing.

    Because the two anchors are different instants, exactly one axis may be
    asked for at a time, and that is enforced here rather than resolved by
    precedence — a client that gets `window=1h` served when it also sent
    `since`/`until` would have no way to learn its span was ignored:

      * `window` + `tier` together -> 400 naming both;
      * `since`/`until` without `tier` -> 400 (a preset anchors to `now`, a raw
        tier does not, so naming both anchors asks for two different spans);
      * neither -> `window=1h`, which is #407's default and stays it, so the
        callers shipped before this axis are unaffected.

    The tier axis's span is clamped to that tier's own retention, and the clamp
    is *stated*: the returned `since`/`until` are what was served, `window_s` is
    the served span, and `clamp` says what moved and why. Both axes therefore
    obey the same rule — the response always states the window it really served.

    Returns ``{axis, window, tier, bucket_s, since, until, window_s, clamp}``.
    """
    from api_server import ApiError
    asked_window = bool(str(query.get("window") or "").strip())
    asked_tier = bool(str(query.get("tier") or "").strip())
    asked_span = bool(str(query.get("since") or "").strip()) or \
        bool(str(query.get("until") or "").strip())

    if asked_window and asked_tier:
        raise ApiError(
            400, "bad_request",
            "'window' and 'tier' pick a span two different ways and cannot be "
            "asked together: 'window' is a preset anchored to now (15m, 1h, 6h, "
            "24h, 7d), 'tier' is a raw tier with its own since/until. Send one "
            "or the other.",
            "Ask for either window or tier, not both.",
        )
    if asked_span and not asked_tier:
        raise ApiError(
            400, "bad_request",
            "'since'/'until' need 'tier': a 'window' preset always runs back from "
            "now, while an explicit since/until does not, so a request naming "
            "both states two different spans and the route would have to guess "
            "which one you meant.",
            "since and until need tier.",
        )

    now = int(_history_now())
    if not asked_tier:
        window, tier, bucket_s, span_s = _parse_history_window(query, mh)
        return {"axis": AXIS_WINDOW, "window": window, "tier": tier,
                "bucket_s": bucket_s, "since": now - span_s, "until": now,
                "window_s": span_s, "clamp": ""}

    tier = _parse_history_tier(query, mh)
    bucket_s = _history_bucket_s(mh, tier)
    retention = _history_retention_s(mh, tier)
    asked_since = _parse_history_epoch(query, "since")
    asked_until = _parse_history_epoch(query, "until")

    # `until` defaults to now (the same anchor the preset uses); `since` then
    # defaults to the deepest bucket this tier can still hold.
    until = now if asked_until is None else asked_until
    since = (until - retention) if asked_since is None else asked_since

    clamps = []
    if until > now:
        clamps.append(f"until={asked_until} is in the future; the newest bucket "
                      f"that can exist is {now}")
        until = now
    floor = until - retention
    if since < floor:
        clamps.append(f"since={asked_since} is deeper than tier {tier} keeps "
                      f"({retention} s); its oldest bucket is {floor}")
        since = floor
    if since < 0:
        clamps.append(f"since={asked_since} is before the epoch; serving from 0")
        since = 0
    if since > until:
        raise ApiError(
            400, "bad_request",
            f"since={since} is after until={until}: an inclusive span cannot run "
            "backwards, and answering it empty would read as 'nothing happened' "
            "rather than as a bad range",
            "since must not be after until.",
        )
    return {"axis": AXIS_TIER, "window": f"{until - since}s", "tier": tier,
            "bucket_s": bucket_s, "since": since, "until": until,
            "window_s": until - since,
            "clamp": ("; ".join(clamps) + f" — served {until - since} s of "
                      f"tier {tier}") if clamps else ""}


def _history_host_rows_from_points(points, node: str) -> list:
    """Tier-0 records -> per-node rows, key names unchanged.

    One ring record is one 2 s tick carrying EVERY host, while tiers 1 and 2
    store one row per (tier, ts, host). A chart cannot consume two shapes, so
    the ring is transposed into the row shape — and it is only a transpose:
    every metric key and value comes out as the recorder holds it (a ``None``
    stays ``None``, nothing is averaged, summed or otherwise re-derived), with
    `tier`/`ts`/`n` from the same record.
    """
    rows = []
    for p in points or []:
        for host, metrics in (p.get("hosts") or {}).items():
            if node != "all" and host != node:
                continue
            row = {"tier": 0, "ts": p.get("ts"), "res": p.get("res")}
            row.update(metrics)
            row["host"] = host
            row.setdefault("n", p.get("n", 1))
            rows.append(row)
    return rows


def _history_engine_rows_from_points(points, node: str) -> list:
    """Tier-0 records -> per-engine rows, keyed (node, port) as the recorder keys them."""
    rows = []
    for p in points or []:
        for e in p.get("engines") or []:
            if node != "all" and e.get("node") != node:
                continue
            row = {"tier": 0, "ts": p.get("ts"), "res": p.get("res")}
            row.update(e)
            row.setdefault("n", p.get("n", 1))
            rows.append(row)
    return rows


def _history_project(rows, identity, keep):
    """Keep the row's identity keys plus the requested metrics (None = all)."""
    if keep is None:
        return rows
    allowed = set(identity) | set(keep)
    return [{k: v for k, v in row.items() if k in allowed} for row in rows]


def _backing_history_tier0(node):
    """Tier 0 — the in-memory ring, but ONLY if this process owns it.

    None means "this process is not the sampler's" (the deployed case: the API
    runs as its own ``python -m api_server serve``, so the daemon's ring is
    unreachable from here) and an empty list means "this process does sample and
    holds nothing yet" (the stated post-restart state). The route reports both
    distinctly — an empty list answered as if it were data is how a chart draws
    "nothing happened" over "we cannot see".
    """
    recorder = _metrics_history().resident_recorder()
    if recorder is None:
        return None
    return recorder.tier0_points(None if node == "all" else node)


def _backing_history_rows(hscc_dir, tier, since, until, node):
    """Tiers 1 and 2 — straight out of the recorder's store.

    Returns ``(host_rows, engine_rows, store_info)``. The store is opened only
    when the file already exists: the recorder owns that file, and a widget poll
    against a fresh install must not leave an empty database behind (an empty
    file looks like a data source; a missing one says what it means).

    ``store_info`` also carries the store's own size facts — `bound_bytes`,
    `tier1_rows`, `tier2_rows` — because they belong to the same object the block
    already describes. They are read while the handle is open and only where the
    store was really opened: `tier1_rows`/`tier2_rows` are `None` at tier 0, which
    never touches the file, and a `0` from a branch that did not count would be
    the same fabricated default `store.tier0_resident` used to carry.
    """
    mh = _metrics_history()
    store = mh.MetricsStore(hscc_dir=hscc_dir)
    exists = os.path.exists(store.path)
    info = {"path": store.path, "exists": exists,
            "db_bytes": os.path.getsize(store.path) if exists else 0,
            "bound_bytes": mh.size_bound_bytes()}
    if not exists:
        # Not a default: there is no store, so there is nothing to count. That is
        # a measured absence, and it costs no file handle to know.
        info["tier1_rows"] = info["tier2_rows"] = 0
        return [], [], info
    name = None if node == "all" else node
    try:
        host_rows = store.host_rows(tier, since=since, until=until, host=name)
        engine_rows = store.engine_rows(tier, since=since, until=until, node=name)
        counts = store.stats()
    finally:
        store.close()
    info["error"] = store.error
    info["tier1_rows"] = counts.get("tier1_rows")
    info["tier2_rows"] = counts.get("tier2_rows")
    info["bound_bytes"] = counts.get("bound_bytes", mh.size_bound_bytes())
    return host_rows, engine_rows, info


def _speak_cluster_history(payload) -> str:
    """One sentence naming the window, the resolution and how much is there."""
    if payload.get("error"):
        return f"Cluster history failed: {payload['error']}"
    if payload["tier"] == 0 and not payload["store"]["tier0_resident"]:
        return ("The 2 s tier is the daemon's in-memory ring and this server "
                "process does not own it. Ask for window=1h for 1-minute points.")
    counts = payload["counts"]
    plural = lambda n, thing: f"{n} {thing}{'s' if n != 1 else ''}"
    span = (f"Last {payload['window']}" if payload["axis"] == AXIS_WINDOW else
            f"Tier {payload['tier']} over {payload['window_s']} s")
    return (f"{span} at {payload['resolution']} resolution: "
            f"{plural(counts['hosts'], 'node point')}, "
            f"{plural(counts['engines'], 'engine point')}.")


def handle_cluster_history(server, ctx, query, body):
    """GET /v1/cluster/history?node=<host|all>&metrics=a,b
                    &window=15m|1h|6h|24h|7d
                    &tier=0|1|2&since=<epoch>&until=<epoch>

    The recorder's stored per-node and per-engine series, read-only: a request
    here never triggers a sample, an ssh call or a sparkrun child.

    TWO REQUEST AXES, exactly one per request. `window` is a preset anchored to
    `now` and implies its tier (15m -> tier 0's 2 s ring; 1h/6h/24h -> tier 1's
    1-minute roll-ups; 7d -> tier 2's 5-minute roll-ups). `tier`+`since`/`until`
    is a raw tier over an explicit span — "the hour around last night's slow
    run" — and anchors to nothing. Asking for both is a 400 rather than a
    precedence, because the two names denote two different spans; `since`/`until`
    without `tier` is one too, since only the raw tier has no anchor of its own.
    Neither key is `window=1h`, unchanged from #407. `since`/`until` are
    INCLUSIVE epoch-second bucket starts and the span is clamped to the tier's own
    retention — and the response always states the window it actually served, on
    either axis: `since`/`until` are the served pair, `window_s` the served span,
    `axis` which axis answered and `note` says when a clamp moved the span.

    Rows come back with the recorder's own field names and its ``None``s
    intact — ``None`` means "not reported", and coercing it to 0 would draw a
    node that never answered as an idle one. `source` says where the rows came
    from as a code, so a client branches on that and not on the English of
    `note`.
    """
    from api_server import ApiError
    mh = _metrics_history()
    for unsupported in sorted(set(query) - set(_HISTORY_QUERY_KEYS)):
        raise ApiError(
            400, "bad_request",
            f"'{unsupported}' is not supported by /v1/cluster/history: it "
            f"accepts {', '.join(repr(k) for k in _HISTORY_QUERY_KEYS)} only",
            f"{unsupported} is not supported by the cluster history route.",
        )
    axis = _resolve_history_axis(query, mh)
    window, tier, bucket_s = axis["window"], axis["tier"], axis["bucket_s"]
    since, until, span_s = axis["since"], axis["until"], axis["window_s"]
    node = _parse_history_node(query)
    metrics = _parse_history_metrics(query, mh, tier)

    host_vocab, engine_vocab = _history_vocab(mh, tier)
    keep_host = keep_engine = None
    if metrics is not None:
        keep_host = [m for m in metrics if m in host_vocab]
        keep_engine = [m for m in metrics if m in engine_vocab]
    # A series with no requested field is REPORTED as excluded rather than
    # returned as an empty list, so a client cannot read "no engine points" as
    # "the fleet served nothing".
    hosts_included = keep_host is None or bool(keep_host)
    engines_included = keep_engine is None or bool(keep_engine)

    payload = {
        "window": window,
        "window_s": span_s,
        # Which axis the CLIENT asked on, stated rather than left to be inferred
        # from which of `since`/`until`/`window` happen to be non-null. On the
        # tier axis `window` is a derived span label (`"<n>s"`), never a preset
        # name the request did not use.
        "axis": axis["axis"],
        # Where the rows came from, as a code: a client must branch on this, not
        # on the English of `note`. None only when the read itself failed, which
        # `error` then states.
        "source": None,
        "tier": tier,
        "resolution_s": bucket_s,
        "resolution": mh.RES_TIER0 if tier == 0 else f"{bucket_s}s",
        "tier_summary": _history_tier_summary(mh, tier, bucket_s),
        "max_fields": [c for c in host_vocab
                       if c.endswith("_max")
                       and (metrics is None or c in metrics)],
        "since": since,
        "until": until,
        "node": node,
        "metrics": list(metrics) if metrics is not None else list(host_vocab) + list(engine_vocab),
        "hosts_included": hosts_included,
        "engines_included": engines_included,
    }

    # `tier0_resident` is a MEASUREMENT, not a default. It answers exactly one
    # question — "could THIS process read the 2 s ring?" — and that question only
    # exists when tier 0 is the tier being served. So it starts as None
    # ("nobody measured") and is written only by the tier-0 branch. It used to
    # start as True, which let a `window=6h`/`7d` payload claim a resident ring in
    # a process that hosts no recorder at all (measured on #413's fixtures: the
    # tier-1 and tier-2 bytes said `true`, the `window=15m` bytes from the same
    # generator in the same process said `false`). `note`/`speak` stay the honest
    # signal for an empty tier-0 answer; this key only says what its name says,
    # and only where it was actually asked.
    tier0_resident = None
    error = None
    try:
        if tier == 0:
            points = _backing_history_tier0(node)
            tier0_resident = points is not None
            payload["source"] = SOURCE_RING if points is not None \
                else SOURCE_RING_ABSENT
            host_rows = _history_host_rows_from_points(points, node)
            engine_rows = _history_engine_rows_from_points(points, node)
            store_info = {"path": mh.db_path(ctx.hscc_dir),
                          "exists": os.path.exists(mh.db_path(ctx.hscc_dir)),
                          "db_bytes": 0}
            store_info["db_bytes"] = (os.path.getsize(store_info["path"])
                                      if store_info["exists"] else 0)
            # This branch never opens the store, so it cannot count its rows:
            # `None` says "nobody measured", which is what #417 established for
            # `tier0_resident` and is the reason there is no top-level `stats`.
            # `bound_bytes` is the recorder's stated ceiling, knowable without
            # the file.
            store_info["bound_bytes"] = mh.size_bound_bytes()
            store_info["tier1_rows"] = store_info["tier2_rows"] = None
        else:
            host_rows, engine_rows, store_info = _backing_history_rows(
                ctx.hscc_dir, tier, since, until, node)
            payload["source"] = SOURCE_STORE if store_info["exists"] \
                else SOURCE_STORE_ABSENT
    except Exception as exc:
        host_rows, engine_rows = [], []
        store_info = {"path": mh.db_path(ctx.hscc_dir), "exists": False,
                      "db_bytes": 0, "bound_bytes": mh.size_bound_bytes(),
                      "tier1_rows": None, "tier2_rows": None}
        error = _failure_detail(exc=exc)

    if not hosts_included:
        host_rows = []
    if not engines_included:
        engine_rows = []
    host_rows = _history_project(host_rows, _HISTORY_HOST_IDENTITY, keep_host)
    engine_rows = _history_project(engine_rows, _HISTORY_ENGINE_IDENTITY,
                                   keep_engine)

    store_info["tier0_resident"] = tier0_resident
    payload["store"] = store_info
    payload["hosts"] = host_rows
    payload["engines"] = engine_rows
    payload["counts"] = {"hosts": len(host_rows), "engines": len(engine_rows)}
    if error:
        payload["error"] = error
    payload["note"] = _history_note(tier, tier0_resident, store_info, error,
                                    axis["clamp"])
    payload["speak"] = _speak_cluster_history(payload)
    return 200, payload


def _history_note(tier, tier0_resident, store_info, error, clamp="") -> str:
    """What the payload says about its own emptiness, so a gap explains itself.

    The clamp statement rides in the same field: a request whose span the route
    shortened has to say so where a client looks for "why is this answer not what
    I asked for", and a silent shortening is indistinguishable from a fleet that
    was quiet for the rest of the range.
    """
    parts = []
    if clamp:
        parts.append(clamp)
    if error:
        parts.append(f"history read failed: {error}")
    elif tier == 0 and not tier0_resident:
        parts.append("tier 0 is the sampler's in-memory ring and lives in the "
                     "daemon process; this server process cannot read it, so no "
                     "2 s points are served here. Use window=1h for the durable "
                     "1-minute tier.")
    elif tier != 0 and not store_info.get("exists"):
        parts.append("the recorder has not written its store yet "
                     f"({store_info['path']}); the durable tiers are empty until "
                     "it closes its first minute")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Route registration (import side-effect; loaded by api_server.py)
# ---------------------------------------------------------------------------

def load():
    """Register all cluster + fleet read routes on api_server.ROUTES.

    Imported last by api_server.py (after ROUTES / ApiError exist); api_server
    is imported here so this module never creates a circular import when
    imported on its own. Each route is ``(method, compiled path regex,
    handler)`` — handlers are plain functions
    ``(server, ctx, query, body) -> (status, payload_dict)``.
    """
    import api_server

    routes = [
        ("GET", r"^/v1/cluster/status$", handle_cluster_status),
        ("GET", r"^/v1/cluster/hosts$", handle_cluster_hosts),
        ("GET", r"^/v1/cluster/monitor$", handle_cluster_monitor),
        ("GET", r"^/v1/cluster/history$", handle_cluster_history),
        ("GET", r"^/v1/cluster/jobs$", handle_cluster_jobs),
        ("GET", r"^/v1/cluster/info$", handle_cluster_info),
        ("GET", r"^/v1/health$", handle_health),
        ('GET', r'^/v1/fleet/stats$', handle_fleet_stats),
        ('GET', r'^/v1/fleet/throughput$', handle_fleet_throughput),
        ('GET', r'^/v1/fleet/usage$', handle_fleet_usage),
        ('GET', r'^/v1/fleet/streams$', handle_fleet_streams),
        ("GET", r"^/v1/autoscale$", handle_autoscale),
    ]
    for method, pattern, handler in routes:
        api_server.ROUTES.append((method, re.compile(pattern), handler))
