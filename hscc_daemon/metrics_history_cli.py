"""`hscc cluster history` — the terminal side of the recorder's stored series.

WHY THIS MODULE EXISTS (t_eee43fac). ``GET /v1/cluster/history`` answers "what
did the fleet look like at 14:05", and until now nothing on the CLI did:
``CLUSTER_SUBCOMMANDS`` had no ``history``, so an operator staring at a widget
that looked wrong had no way to print the rows it drew. A chart that cannot be
checked from a terminal is a chart nobody can debug.

THE FLAG SURFACE IS THE ROUTE'S, NOT THIS MODULE'S (t_447cc7fc ruling). The
canonical history read is ``/v1/cluster/history`` — ``/v1/metrics/history``
was retired behind it by t_2c629557 — so the CLI mirrors the canonical route's
two axes and its one machine filter:

    hscc cluster history [--window 15m|1h|6h|24h|7d]            # axis A, default 1h
                         [--tier 0|1|2 [--since TS] [--until TS]]   # axis B
                         [--node <host|all>] [--metrics a,b] [--json]

* ``--window`` and ``--tier`` are mutually exclusive, and ``--since``/``--until``
  without ``--tier`` is an error — the route's rules, refused the same way here
  rather than given a precedence the route does not have.
* Neither axis: ``--window 1h``, the route's default.
* ``--node`` is the ONE machine filter. ``host_rollup.host`` and
  ``engine_rollup.node`` are two column names for one machine, so there is no
  ``--host``: two flags for one identity is two ways to ask for two different
  machines by accident.
* ``--tier`` alone answers the tier's own span, clamped to what it retains
  (``TIER0_WINDOW_S`` / ``TIER1_WINDOW_S`` / ``TIER2_WINDOW_S``).

WHAT IT READS — AND WHAT IT MUST NEVER READ. The whole data path is
:mod:`hscc_daemon.metrics_history`'s OWN read API: ``MetricsStore.host_rows`` /
``MetricsStore.engine_rows`` for tiers 1 and 2, ``MetricsRecorder.tier0_points``
for tier 0. Never the HTTP route (this is the CLI, not a client of it), never
``metrics_history.get_sampler()`` — that function CONSTRUCTS a Sampler on first
use, which spawns a second ``sparkrun cluster monitor`` and a second writer on
the roll-up watermarks. A read command that silently started a sampler would
double-write history and add an ssh poller to a box that already has one.
``test_the_cli_constructs_no_sampler_and_shells_out_nothing`` pins it, the same
shape ``hscc-api/tests/test_cluster_history_parity.py`` uses for the route.

FIELD NAMES ARE SPARKRUN'S, NOT THIS MODULE'S. Rows carry
``metrics_history.HOST_KEYS`` / ``HOST_COLUMNS`` / ``ENGINE_KEYS`` verbatim
(``cpu_usage_pct``, ``gpu_temp_c``, ``gpu_power_w``, ``gpu_mem_used_mb``, …),
and the peaks ride beside their averages as separate ``<field>_max`` columns
(``MAX_KEYS``). Nothing here renames, folds or re-averages a value: the human
table is the same rows the route serialises, which is what makes the parity
test meaningful.

``None`` MEANS "NOT REPORTED". A cell the node never answered prints ``—``,
never ``0`` — 0 degrees and no answer are different facts, and a table that
coerced them would tell an operator a silent node was a cold one. In ``--json``
the same cell stays ``null``.

AN EMPTY ANSWER EXPLAINS ITSELF. Tier 0 is the sampler's in-memory ring, which
belongs to the daemon process; a CLI process has no ring, so the answer says so
(``store.tier0_resident`` false, with the route's note wording) instead of
printing zero rows. A store that does not exist yet says that too. Printing an
empty table over either would make "we cannot see" look like "nothing
happened".
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from datetime import datetime

__all__ = ["parse_history_args", "read_history", "render_history",
           "history_json", "usage_error_message", "history_windows",
           "USAGE_ERROR_RC", "MISSING"]

#: Usage/validation failures. The cluster group's other verbs return 1 for a
#: refused flag list (`_parse_cluster_flags`), so this surface does not invent a
#: second convention for "you asked for something the CLI cannot honour".
USAGE_ERROR_RC = 1

#: The cell a value-less field prints. An em dash, never `0` — see the module
#: docstring. `--json` keeps `null` instead; this is a human-table rule only.
MISSING = "\u2014"

#: The flags this verb takes — the canonical route's parameter set (t_447cc7fc).
#: Anything else is refused, never ignored: an unknown token accepted as "fine"
#: is how a typo prints a confident table.
_FLAGS = ("--window", "--tier", "--since", "--until", "--node", "--metrics",
          "--json")

#: The window preset used when neither axis is given — the route's default.
DEFAULT_WINDOW = "1h"

#: Which of the route's two request axes answered, on the wire as `axis`. The
#: route's `AXIS_WINDOW` / `AXIS_TIER`; copied for the same reason as the name
#: rule below. Stated rather than inferred, because on the tier axis `window`
#: is `null` and a reader that guesses from the keys will guess a preset name
#: that was never sent.
AXIS_WINDOW = "window"
AXIS_TIER = "tier"

#: Where the served rows came from, on the wire as `source` — the four codes
#: ``routes_cluster`` writes, copied verbatim. A client must branch on this code
#: and read `note` for the sentence, because the English of `note` is prose that
#: may be reworded while the code stays. A CLI run never owns the daemon's 2 s
#: ring, so tier 0 answers `recorder_ring_absent` here unless a recorder happens
#: to be resident in this very process.
SOURCE_RING = "recorder_ring"
SOURCE_RING_ABSENT = "recorder_ring_absent"
SOURCE_STORE = "sqlite_store"
SOURCE_STORE_ABSENT = "sqlite_store_absent"

#: A host name as the recorder keys it — the same character rule
#: ``routes_cluster._HISTORY_NODE_RE`` applies to ``?node=``, kept as a local
#: copy rather than an import because the daemon leg runs without the API
#: package. A name with a space, a slash or a `?` is a client bug rather than a
#: host, and gets an error naming the rule.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,253}")

#: Epoch seconds: an optional sign, digits, an optional fractional part.
_EPOCH_RE = re.compile(r"^-?\d+(\.\d+)?$")

#: Row keys that identify a row and are never dropped by ``--metrics`` — the
#: route's ``_HISTORY_HOST_IDENTITY`` / ``_HISTORY_ENGINE_IDENTITY``, copied
#: rather than imported for the same reason as the name rule above. Engine
#: identity is ``(node, port)``; `model` is an attribute of the port.
HOST_IDENTITY = ("tier", "ts", "host", "res", "n", "error")
ENGINE_IDENTITY = ("tier", "ts", "node", "port", "model", "res", "n", "error")

#: A row-level error string this long is truncated, not wrapped: a wrapped row
#: is unreadable and the full text is one `--json` away.
_ERROR_CELL_MAX = 48

HOST_HEADING = "hosts"
ENGINE_HEADING = "engines"


# ── the two axes, derived from the recorder's constants ─────────────────────

def history_windows(mh) -> dict:
    """The window presets, built from ``metrics_history``'s own constants.

    The same map ``routes_cluster._history_windows`` builds, from the same
    source — so this surface cannot drift from the cadence that produced the
    data. A client asking for 1 h is served tier 1's 1-minute buckets because
    that is what tier 1 IS.
    """
    return {
        "15m": (0, mh.TIER0_INTERVAL_S, mh.TIER0_WINDOW_S),
        "1h": (1, mh.TIER1_BUCKET_S, 60 * 60),
        "6h": (1, mh.TIER1_BUCKET_S, 6 * 60 * 60),
        "24h": (1, mh.TIER1_BUCKET_S, mh.TIER1_WINDOW_S),
        "7d": (2, mh.TIER2_BUCKET_S, mh.TIER2_WINDOW_S),
    }


def tier_span(mh, tier: int) -> int:
    """What one tier retains — the span ``--tier N`` alone answers."""
    return {0: mh.TIER0_WINDOW_S, 1: mh.TIER1_WINDOW_S,
            2: mh.TIER2_WINDOW_S}[tier]


def tier_bucket(mh, tier: int) -> int:
    return {0: mh.TIER0_INTERVAL_S, 1: mh.TIER1_BUCKET_S,
            2: mh.TIER2_BUCKET_S}[tier]


def resolution_label(mh, tier: int) -> str:
    """The route's own resolution spelling (``2s`` / ``60s`` / ``300s``)."""
    bucket = tier_bucket(mh, tier)
    return mh.RES_TIER0 if tier == 0 else f"{bucket}s"


def tier_summary(mh, tier: int) -> str:
    """What the served tier is, in the operator's terms — the route's wording."""
    bucket = tier_bucket(mh, tier)
    if tier == 0:
        return (f"raw {bucket} s samples from the recorder's in-memory ring — "
                "no averaging, and no peak columns")
    step = f"{bucket // 60}-minute" if bucket % 60 == 0 else f"{bucket} s"
    peaks = ", ".join(f"{k}_max" for k in mh.MAX_KEYS)
    return f"{step} averages, with peaks for {peaks}"


def vocab(mh, tier: int) -> tuple:
    """(host field names, engine field names) this tier actually stores.

    Tier 0 holds raw samples, so it has no ``<field>_max`` column: a peak only
    exists once a bucket has been rolled up, and asking for one there is an
    error that says why rather than an empty column.
    """
    host_vocab = tuple(mh.HOST_KEYS) if tier == 0 else tuple(mh.HOST_COLUMNS)
    return host_vocab, tuple(mh.ENGINE_KEYS)


def _now() -> float:
    """The wall clock for a window's edge — the seam the parity test pins."""
    import time
    return time.time()


# ── argv ────────────────────────────────────────────────────────────────────

def parse_history_args(argv, mh=None):
    """``hscc cluster history`` argv (after the verb) -> ``(opts, error)``.

    Only one of the two is meaningful: when ``error`` is not None the caller
    must print it and run nothing. ``opts`` is
    ``{window, tier, since, until, node, metrics, json}`` with the axis already
    resolved (``window`` is the preset name, or None on the tier axis).

    The rules are the canonical route's, in the CLI's voice: the two axes are
    mutually exclusive; ``--since``/``--until`` need ``--tier``; a tier is 0, 1
    or 2 and a window is one of the presets; ``--node`` is a host name or
    ``all`` (an empty value is not a request for every host); ``--metrics``
    names stored fields; an unrecognised token is a refusal.
    """
    if mh is None:
        from hscc_daemon import metrics_history as mh  # noqa: PLC0414
    opts = {"window": None, "tier": None, "since": None, "until": None,
            "node": "all", "metrics": None, "json": False}
    # `--tier=1` is the same request as `--tier 1` (the cluster surface already
    # accepts `--theme=<name>`), so split the pairs here and let one value path
    # handle both spellings.
    args: list = []
    for tok in (argv or []):
        tok = str(tok)
        if tok.startswith("--") and "=" in tok:
            head, _eq, tail = tok.partition("=")
            args.extend([head, tail])
        else:
            args.append(tok)

    raw_window = raw_tier = None
    windows = history_windows(mh)
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--json":
            opts["json"] = True
            i += 1
            continue
        if tok not in _FLAGS:
            return opts, (f"'{tok}' is not a 'hscc cluster history' flag "
                          f"(accepted: {', '.join(_FLAGS)}). Nothing was read.")
        value = _flag_value(args, i, tok)
        i += 2
        if tok == "--window":
            if value is None:
                return opts, (f"'--window' needs one of {', '.join(windows)}. "
                              "Nothing was read.")
            if value not in windows:
                return opts, (f"--window {value!r} is not a window: use "
                              f"{', '.join(windows)}. Each serves a different "
                              "tier, and the answer states which one. Nothing "
                              "was read.")
            raw_window = value
            opts["tier"] = windows[value][0]
            continue
        if tok == "--tier":
            if value is None:
                return opts, ("'--tier' needs a value: 0, 1 or 2. Nothing was "
                              "read.")
            if not _EPOCH_RE.match(value) or "." in value:
                return opts, (f"--tier {value!r} is not a tier: use 0 (2 s "
                              "ring), 1 (1-minute roll-ups) or 2 (5-minute "
                              "roll-ups). Nothing was read.")
            tier = int(float(value))
            if tier not in (0, 1, 2):
                return opts, (f"--tier {tier} is out of range: 0 is the "
                              "sampler's 2 s ring, 1 its 1-minute roll-ups, 2 "
                              "its 5-minute roll-ups. Nothing was read.")
            raw_tier = tier
            opts["tier"] = tier
            continue
        if tok == "--node":
            if value is None:
                return opts, ("'--node' needs a host name or 'all'. Nothing "
                              "was read.")
            if value != "all" and not _NAME_RE.fullmatch(value):
                return opts, (f"--node {value!r} is not a host name: letters, "
                              "digits and . - _ : only, or 'all' — and an "
                              "empty value is not a request for every host. "
                              "Nothing was read.")
            opts["node"] = value
            continue
        if tok == "--metrics":
            if value is None:
                return opts, ("'--metrics' needs at least one field name "
                              "(e.g. --metrics gpu_util_pct,gpu_temp_c); an "
                              "empty list is not a request for all of them. "
                              "Nothing was read.")
            names: list = []
            for part in value.split(","):
                name = part.strip()
                if name and name not in names:
                    names.append(name)
            if not names:
                return opts, ("'--metrics' needs at least one field name; "
                              "commas alone select nothing. Nothing was read.")
            opts["metrics"] = names
            continue
        # --since / --until
        opts[tok[2:]] = _epoch(value, tok)
        if isinstance(opts[tok[2:]], str):
            return opts, opts[tok[2:]]
    # ── the two axes (t_447cc7fc): exactly one, or the default window ──────
    if raw_window is not None and raw_tier is not None:
        return opts, ("--window and --tier are the two ways to pick a "
                      f"resolution and they may not both be given (window "
                      f"{raw_window!r} already implies tier {raw_tier}). "
                      "Nothing was read.")
    if (opts["since"] is not None or opts["until"] is not None) \
            and raw_tier is None:
        return opts, ("--since/--until are an explicit window inside one "
                      "tier, so they need --tier: a bare timestamp does not "
                      "say whether it means 2 s, 1-minute or 5-minute points. "
                      "Nothing was read.")
    if raw_window is None and raw_tier is None:
        raw_window = DEFAULT_WINDOW
        opts["tier"] = windows[raw_window][0]
    opts["window"] = raw_window
    if opts["since"] is not None and opts["until"] is not None \
            and opts["since"] > opts["until"]:
        return opts, (f"--since {opts['since']:g} is after --until "
                      f"{opts['until']:g}: the window runs the wrong way. "
                      "Nothing was read.")
    # `--metrics` is validated against the tier's own vocabulary, so a peak at
    # tier 0 is refused with the reason rather than arriving as a hole.
    if opts["metrics"] is not None:
        err = _check_metrics(opts["metrics"], opts["tier"], mh)
        if err:
            return opts, err
    return opts, None


def _epoch(value, tok):
    """``--since``/``--until`` -> a float, or the refusal string."""
    if value is None:
        return (f"'{tok}' needs epoch seconds (e.g. --since 1795000000). "
                "Nothing was read.")
    if not _EPOCH_RE.match(value):
        return (f"{tok} {value!r} is not epoch seconds: pass a UNIX timestamp "
                "such as --since 1795000000. Nothing was read.")
    return float(value)


def _check_metrics(names, tier, mh):
    """Refuse a metric the tier does not store, naming the recorder's own set."""
    host_vocab, engine_vocab = vocab(mh, tier)
    valid = ", ".join(sorted(set(host_vocab) | set(engine_vocab)))
    for name in names:
        if name in host_vocab or name in engine_vocab:
            continue
        if name in mh.HOST_COLUMNS:        # a peak: valid only once rolled up
            return (f"{name} is not stored for tier {tier}: tier 0 keeps raw "
                    "2 s samples, so a peak column exists only after a "
                    "roll-up (--tier 1 or --tier 2). Nothing was read.")
        return (f"unknown metric {name!r} for this tier; the recorder stores: "
                f"{valid}. Nothing was read.")
    return None


def _flag_value(args, i, tok):
    """The value of ``--flag value`` or ``--flag=value``, else None.

    A token that starts with ``-`` is NOT eaten as a value: `--tier --json` is
    a missing value, not a request for a tier named "--json".
    """
    if i + 1 < len(args) and not str(args[i + 1]).startswith("-"):
        return str(args[i + 1])
    return None


def usage_error_message(err, width: int = 78) -> str:
    """Wrap a validation error the way the cluster group wraps its refusals."""
    return "\n".join(textwrap.wrap(err, width=width, initial_indent="Error: ",
                                   subsequent_indent="  ",
                                   break_long_words=False))


# ── the read ────────────────────────────────────────────────────────────────

def resolve_span(opts, mh, now=None) -> tuple:
    """``opts`` -> ``(since, until, span_s)`` for the axis actually chosen.

    Window axis: ``until`` is now and ``since`` is ``until - span`` (the
    route's arithmetic, so the two edges land identically). Tier axis with no
    explicit bounds: the tier's own retention, which is the honest widest
    answer — asking for tier 2 is not a request for 2 s data. Explicit
    ``--since``/``--until`` are INCLUSIVE bucket starts, the recorder's rule.
    """
    now = int(_now() if now is None else now)
    if opts["window"] is not None:
        span = history_windows(mh)[opts["window"]][2]
        return now - span, now, span
    span = tier_span(mh, opts["tier"])
    since = int(opts["since"]) if opts["since"] is not None else now - span
    until = int(opts["until"]) if opts["until"] is not None else now
    return since, until, span


def read_history(opts, hscc_dir=None, mh=None, now=None) -> dict:
    """``opts`` (from :func:`parse_history_args`) -> the payload the CLI prints.

    Same keys, same rows and same ``None``s as the canonical route's — the
    recorder's own read API is the only thing touched. The store is opened only
    when the file already exists: a read against a fresh install must not leave
    an empty database behind (an empty file looks like a data source; a missing
    one says what it means).
    """
    if mh is None:
        from hscc_daemon import metrics_history as mh  # noqa: PLC0414
    tier = opts["tier"]
    since, until, span = resolve_span(opts, mh, now=now)
    host_vocab, engine_vocab = vocab(mh, tier)
    metrics = opts["metrics"]
    keep_host = keep_engine = None
    if metrics is not None:
        keep_host = [m for m in metrics if m in host_vocab]
        keep_engine = [m for m in metrics if m in engine_vocab]
    payload = {
        "window": opts["window"],
        "window_s": span,
        # Which axis answered, stated rather than left to be inferred: on the
        # tier axis `window` is None, and a reader that guesses from the keys
        # would guess a preset name that was never asked for.
        "axis": AXIS_WINDOW if opts["window"] is not None else AXIS_TIER,
        # Written by the one branch that reads anything, on every path out of
        # here. A code, not prose: `note` says it in English and may be reworded.
        "source": None,
        "tier": tier,
        "resolution_s": tier_bucket(mh, tier),
        "resolution": resolution_label(mh, tier),
        # The four byte-budget keys (t_d732d50d). The route folds when asked to;
        # the CLI has no `--max-bytes` and never folds, because a byte budget is a
        # property of a WIRE response and a terminal has no ceiling to fit — and
        # a budget of the same number would not even mean the same thing on the
        # two surfaces, since the route's body carries `speak` and the CLI's does
        # not. These values are therefore the never-folded facts, not defaults:
        # `fold` 1 says nothing was folded, `native_resolution_s` is this tier's
        # own stored step (so it equals `resolution_s` here), and `budget_met` is
        # None because no budget was asked and so nothing was measured against one
        # — the same not-a-default convention as `store.tier0_resident` below.
        "max_bytes": None,
        "fold": 1,
        "native_resolution_s": tier_bucket(mh, tier),
        "budget_met": None,
        "tier_summary": tier_summary(mh, tier),
        "max_fields": [c for c in host_vocab
                       if c.endswith("_max")
                       and (metrics is None or c in metrics)],
        "since": since,
        "until": until,
        "node": opts["node"],
        "metrics": (list(metrics) if metrics is not None
                    else list(host_vocab) + list(engine_vocab)),
        # A series with no requested field is REPORTED as excluded rather than
        # returned as an empty list, so a reader cannot take "no engine points"
        # for "the fleet served nothing".
        "hosts_included": keep_host is None or bool(keep_host),
        "engines_included": keep_engine is None or bool(keep_engine),
        # `tier0_resident`, `tier1_rows` and `tier2_rows` start as None because
        # nothing has been measured yet — the route does the same, and an
        # initial `True`/`0` here would be a default wearing a measurement's
        # name (the exact defect PR #417 removed from the route's copy of this
        # block). Each is written only by the branch that reads it.
        "store": {"path": mh.db_path(hscc_dir), "exists": False,
                  "db_bytes": 0, "bound_bytes": mh.size_bound_bytes(),
                  "tier1_rows": None, "tier2_rows": None,
                  "tier0_resident": None},
        "hosts": [], "engines": [],
        "counts": {"hosts": 0, "engines": 0},
        "note": "",
    }

    name = None if opts["node"] == "all" else opts["node"]
    if tier == 0:
        recorder = mh.resident_recorder()
        payload["store"]["tier0_resident"] = recorder is not None
        # The store was never opened on this path, so its row counts stay None:
        # a `0` here would claim the file was counted and found empty.
        payload["source"] = SOURCE_RING if recorder is not None \
            else SOURCE_RING_ABSENT
        if recorder is None:
            payload["note"] = (
                "tier 0 is the sampler's in-memory ring and lives in the "
                "daemon process; this hscc process cannot read it, so no 2 s "
                "points are served here. Use --window 1h (or --tier 1) for the "
                "durable 1-minute tier.")
            return payload
        points = recorder.tier0_points(name)
        payload["hosts"] = _hosts_from_points(points, name, mh)
        payload["engines"] = _engines_from_points(points, name)
    else:
        store = mh.MetricsStore(hscc_dir=hscc_dir)
        try:
            exists = os.path.exists(store.path)
            payload["store"]["exists"] = exists
            payload["store"]["db_bytes"] = (
                os.path.getsize(store.path) if exists else 0)
            payload["source"] = (SOURCE_STORE if exists
                                 else SOURCE_STORE_ABSENT)
            if not exists:
                # Not a default: there is no store, so there is nothing to
                # count. That is a measured absence, and it costs no handle.
                payload["store"]["tier1_rows"] = 0
                payload["store"]["tier2_rows"] = 0
                payload["note"] = (
                    "the recorder has not written its store yet "
                    f"({store.path}); the durable tiers are empty until it "
                    "closes its first minute")
                return payload
            try:
                payload["hosts"] = store.host_rows(tier, since=since,
                                                   until=until, host=name)
                payload["engines"] = store.engine_rows(tier, since=since,
                                                       until=until, node=name)
                # The store's own counts, read while this handle is open — the
                # route folds the same two facts into this same block rather
                # than publish a second top-level `stats`.
                stats = store.stats()
                payload["store"]["tier1_rows"] = stats.get("tier1_rows")
                payload["store"]["tier2_rows"] = stats.get("tier2_rows")
                payload["store"]["bound_bytes"] = stats.get(
                    "bound_bytes", mh.size_bound_bytes())
            except Exception as exc:                # a broken store is stated
                payload["error"] = f"{type(exc).__name__}: {exc}"
                payload["note"] = f"history read failed: {payload['error']}"
        finally:
            store.close()

    if not payload["hosts_included"]:
        payload["hosts"] = []
    if not payload["engines_included"]:
        payload["engines"] = []
    payload["hosts"] = _project(payload["hosts"], HOST_IDENTITY, keep_host)
    payload["engines"] = _project(payload["engines"], ENGINE_IDENTITY,
                                  keep_engine)
    payload["counts"] = {"hosts": len(payload["hosts"]),
                         "engines": len(payload["engines"])}
    return payload


def _project(rows, identity, keep):
    """Keep a row's identity keys plus the requested metrics (None = all)."""
    if keep is None:
        return rows
    allowed = set(identity) | set(keep)
    return [{k: v for k, v in row.items() if k in allowed} for row in rows]


def _hosts_from_points(points, node, mh) -> list:
    """Tier-0 records -> per-node rows, in the recorder's own field names.

    One ring record is one 2 s tick carrying EVERY host, while tiers 1 and 2
    store one row per (tier, ts, host) — so this is a transpose and nothing
    more: every key and value comes out as the recorder holds it.
    """
    rows = []
    for p in points or []:
        for name, metrics in (p.get("hosts") or {}).items():
            if node is not None and name != node:
                continue
            row = {"tier": 0, "ts": p.get("ts"), "res": p.get("res")}
            row.update(metrics or {})
            row["host"] = name
            row.setdefault("n", p.get("n", 1))
            rows.append(row)
    return rows


def _engines_from_points(points, node) -> list:
    """Tier-0 records -> per-engine rows, keyed ``(node, port)`` as the recorder
    keys them; the served model rides along as an attribute of the port."""
    rows = []
    for p in points or []:
        for e in p.get("engines") or []:
            if node is not None and e.get("node") != node:
                continue
            row = {"tier": 0, "ts": p.get("ts"), "res": p.get("res")}
            row.update(e)
            row.setdefault("n", p.get("n", 1))
            rows.append(row)
    return rows


# ── the human table ─────────────────────────────────────────────────────────

def host_columns(mh, rows=None) -> tuple:
    """The host columns to print: identity, then every stored field, a peak
    immediately after the average it is the peak of.

    Derived from ``mh.HOST_KEYS`` / ``mh.MAX_KEYS`` rather than listed, so a
    field added to the recorder shows up here instead of being silently
    unprintable — and ``test_the_host_table_prints_every_column_the_store_holds``
    fails if the set ever stops covering ``HOST_COLUMNS``.
    """
    cols = ["ts", "host"]
    for k in mh.HOST_KEYS:
        cols.append(k)
        if k in mh.MAX_KEYS:
            cols.append(f"{k}_max")
    cols += ["n", "error"]
    return tuple(cols)


def engine_columns(rows) -> tuple:
    keys = ["ts", "node", "port", "model"]
    seen = set(keys)
    for r in rows or []:
        for k in r:
            if k not in seen:
                keys.append(k)
                seen.add(k)
    return tuple(keys)


def _cell(value) -> str:
    """One cell. ``None`` (and an empty string) is an em dash — never ``0``."""
    if value is None or value == "":
        return MISSING
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.1f}"
    text = str(value)
    if len(text) > _ERROR_CELL_MAX:
        return text[:_ERROR_CELL_MAX - 1] + "\u2026"
    return text


def _ts_cell(value) -> str:
    """A bucket start as local time: ``MM-DDTHH:MM:SS``, one token.

    The T separator is not decoration. In a fixed-width table a cell that
    contains a space cannot be read back by splitting the line — the row would
    carry one more token than the header has columns, and every cell after
    ``ts`` would be off by one for anything that parses the output (a chart
    check, a test, an awk one-liner). An unparseable stamp prints verbatim
    rather than as a plausible date.
    """
    if value is None:
        return MISSING
    try:
        dt = datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):
        return str(value)
    return f"{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"


def _table(rows, columns) -> list[str]:
    """Fixed-width text table. Every column is present in every row; a column
    no row carries prints as the em dash."""
    cells = [[(_ts_cell(r.get(c)) if c == "ts" else _cell(r.get(c)))
              for c in columns] for r in (rows or [])]
    widths = [max([len(c)] + [len(row[i]) for row in cells])
              for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)).rstrip()]
    lines += ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
              for row in cells]
    return lines


def render_history(payload) -> str:
    """The payload as text an operator reads. ``--json`` is the same rows raw."""
    out: list[str] = []
    axis = (f"window {payload['window']}" if payload.get("window")
            else f"tier {payload['tier']}")
    out.append(f"cluster history \u2014 {axis} \u00b7 tier {payload['tier']} "
               f"\u00b7 {payload['resolution']} \u00b7 {payload['tier_summary']}")
    out.append(f"window: {_ts_cell(payload.get('since'))} \u2192 "
               f"{_ts_cell(payload.get('until'))} "
               f"({payload['window_s']} s; --since/--until are inclusive "
               "bucket starts)")
    if payload.get("node") not in (None, "all"):
        out.append(f"node: {payload['node']} (filters the node rows and the "
                   "engine rows)")
    if payload.get("max_fields"):
        out.append("peaks: " + ", ".join(payload["max_fields"]))
    store = payload.get("store") or {}
    if store.get("path"):
        state = (f"{store['db_bytes']} bytes" if store.get("exists")
                 else "no such file")
        out.append(f"store: {store['path']} ({state})")
    if payload.get("error"):
        out.append(f"error: {payload['error']}")
    if payload.get("note"):
        out.append(f"note: {payload['note']}")
        if payload["tier"] == 0 and not store.get("tier0_resident", True):
            out.append("This is not an empty history: the ring lives in the "
                       "process that samples.")
            return "\n".join(out)

    out.append("")
    for heading, key, included in ((HOST_HEADING, "hosts", "hosts_included"),
                                   (ENGINE_HEADING, "engines",
                                    "engines_included")):
        rows = payload.get(key) or []
        count = (payload.get("counts") or {}).get(key, len(rows))
        if not payload.get(included, True):
            out.append(f"{heading}: excluded by --metrics (no requested "
                       "field) \u2014 not an empty series")
            out.append("")
            continue
        out.append(f"{heading} ({count} {'row' if count == 1 else 'rows'})")
        if key == HOST_HEADING:
            out += _table(rows, host_columns(_mh(), rows))
        else:
            out.append("  \u2014 identity (node, port); model is an attribute "
                       "of the port")
            out += _table(rows, engine_columns(rows))
        out.append("")
    if not (payload.get("hosts") or payload.get("engines")):
        out.append("No rows in that window. An empty window is a fact about "
                   "the window, not a sign nothing was ever sampled: widen it, "
                   "or check the store path above.")
    return "\n".join(out).rstrip() + "\n"


def history_json(payload) -> str:
    """The payload as the machine contract: raw values, ``null`` preserved."""
    return json.dumps(payload, indent=2, default=str)


def _mh():
    from hscc_daemon import metrics_history
    return metrics_history
