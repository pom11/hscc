"""`hscc kanban monitor [--live]`: what the workers on every board are doing.

The kanban twin of `hscc cluster monitor --live`. One read-only pass over every
board (the SAME enumeration `hscc kanban stale` and the autodown C3 interlock
use: ``autodown._load_kanban_db_or_default`` + ``autodown._enum_board_names``)
collects:

* running cards — board, worker profile, run time, heartbeat age, title;
* ready cards — waiting for a slot (global cap or per-profile cap);
* per-board counts by status, plus cards finished in the last 24 h;
* recent outcomes — completed / blocked / gave_up / crashed / timed_out /
  spawn_failed / reclaimed events across all boards;
* tokens — each running card's OWN worker session (input/output), read from the
  profiles' ``state.db``. A run's session comes from the ``owner_admitted`` event
  hermes records for ``tasks.current_run_id`` (``_session_links``), so cards
  claimed in the same second under one profile stay distinct; the ``started_at``
  window is the labelled fallback for a run whose event has not landed yet
  (``attach_tokens``). Plus 24 h / 7 d totals per group (``_token_windows``):
  workers / scheduled / orchestrators / total, where a session is charged whole to
  every window it was **active** in — the store keeps no per-call time, so nothing
  can be split across windows, and the table says so rather than implying a split;
* orchestrators — every session that holds a live turn lease in its own profile
  ``state.db`` (``attach_orchestrators``): profile, session id, turn age,
  lifetime tokens and the session's ``last_activity_description``. The store is
  the authority, not ``ps`` — v0.23 starts a REPL behind an inline ``-c``
  bootstrap, so no argv shape reliably identifies one.

The title line shows the global ``kanban.max_in_progress`` slots in use, because
when every slot is taken the ready cards on other boards strand silently.
Nothing here writes to a board: no claim, no promote, no archive. The one
write is the orchestrator token ledger (``LEDGER_PATH``, 5-min samples, 8 days). The kanban lib, the
config reader and the clock are injectable so the collector and renderer test
offline.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import sqlite3
import sys
import time

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hscc_daemon import autodown
from hscc_daemon.cli_theme import esc
from hscc_daemon.cluster_live import fmt_tokens
from hscc_daemon.daemon_ops import log

DEFAULT_INTERVAL = 5
RECENT_WINDOW_S = 3600
RECENT_LIMIT = 12
STALE_HEARTBEAT_S = 600      # a running card silent this long is suspicious
LONG_RUN_S = 4 * 3600        # fleet-check rule: >4h on an atomic card = look at it
OUTCOME_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out",
                 "spawn_failed", "reclaimed", "block_loop_detected",
                 "decomposed", "spawned")
STATUS_COLS = ("running", "ready", "todo", "review", "blocked", "triage")
HERMES_HOME = os.path.expanduser("~/.hermes")
SESSION_MATCH_S = 900        # fallback only: window for a run with no session id

# Nominal Hermes agent source tree — the same location (and the same env
# override) autodown._load_kanban_db_or_default resolves kanban_db from.
_HERMES_AGENT_PATH = "~/.hermes/hermes-agent"

# Last caps-read failure, mirroring autodown._KANBAN_LOAD / _PRCI_LOAD:
# ``reason`` says what went wrong, ``warned`` makes the WARN line once per
# process instead of once per 5 s monitor tick. Empty ``reason`` means the
# config read fine — so blank caps with an empty reason is "the operator never
# set a cap", which is a DIFFERENT fact from blank caps with a reason.
_CAPS_READ = {"reason": "", "warned": False}


def _note_caps_unreadable(reason):
    """Record why the caps config could not be read and log it ONCE.

    Does not raise — the caller still returns ``(None, None)`` (no cap
    configured and cap unreadable render the same way, so the operator must be
    able to tell them apart from the log rather than from the value).
    """
    _CAPS_READ["reason"] = reason
    if not _CAPS_READ["warned"]:
        _CAPS_READ["warned"] = True
        try:
            log(f"kanban monitor: fleet caps unreadable: {reason}", "WARN")
        except Exception:  # noqa: BLE001
            pass  # logging must never break the monitor


def _load_hermes_config():
    """``hermes_cli.config.load_config()`` result. Raises on any real failure.

    ``hermes_cli`` lives under the Hermes agent tree and is NOT on the daemon's
    ``sys.path``, so this resolves that tree and inserts it here — the same
    thing ``autodown._load_kanban_db_or_default`` does for ``kanban_db``.
    Doing it HERE rather than assuming somebody else already did it is what
    makes ``_read_caps`` independent of call order (GAP-D-4): previously the
    import below only worked once the kanban loader had run, and a caller that
    read the caps first silently got ``(None, None)``.
    """
    hermes_path = os.path.expanduser(
        os.environ.get("HERMES_AGENT_PATH", _HERMES_AGENT_PATH))
    if os.path.isdir(hermes_path) and hermes_path not in sys.path:
        sys.path.insert(0, hermes_path)
    from hermes_cli.config import load_config  # noqa: PLC0415
    return load_config()


def _read_caps():
    """``(max_in_progress, max_in_progress_per_profile)`` from Hermes config.

    Either may be None when unset. Read-only; never raises. A config that
    cannot be read at all is logged once (``_CAPS_READ``) instead of being
    silently reported as "no caps configured".
    """
    try:
        cfg = _load_hermes_config() or {}
    except Exception as e:  # noqa: BLE001
        # Deliberately broad: this runs every 5 s inside `--live`, and the
        # module's contract is that the monitor stays renderable — a Hermes
        # tree that raises at import time must not kill the view. The point of
        # GAP-D-4 was never to narrow this handler, it was to stop the handler
        # from making a broken read LOOK like "no caps configured": that is what
        # _CAPS_READ + the warn-once log carry now.
        _note_caps_unreadable(f"{type(e).__name__}: {e}")
        return None, None
    read_error = getattr(cfg, "read_error", None)
    if read_error is not None:
        # hermes' own fail-open fallback (FailedConfigRead): config.yaml exists
        # but could not be read/parsed, so these are defaults or the
        # last-known-good copy, not the operator's current settings.
        _note_caps_unreadable(
            "hermes served its fail-open config fallback (config.yaml unreadable:"
            f" {read_error})")
    elif _CAPS_READ["warned"]:
        _CAPS_READ["reason"] = ""
        _CAPS_READ["warned"] = False

    def _int(v):
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None
    k = cfg.get("kanban") or {}
    return _int(k.get("max_in_progress")), _int(k.get("max_in_progress_per_profile"))


def _q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _owner_db_path(value):
    """The ``db`` the session owner recorded in its own ``owner_admitted``
    payload — the authoritative place that run's session lives, which is not
    necessarily ``<home>/profiles/<worker>/state.db`` (the owner may run Hermes
    from another root).

    It is board DATA, not operator config, so it only becomes a path when it is
    a plain absolute path to an existing regular file; anything else returns
    None and the caller falls back to the profile default. Same reasoning as
    ``_known_profile`` for a ps argv: an unvalidated string from a DB never
    becomes a filesystem path here.
    """
    if not isinstance(value, str):
        return None
    path = _CONTROL_RE.sub("", value).strip()
    return path if path and os.path.isabs(path) and os.path.isfile(path) else None


def _session_links(conn, run_ids):
    """``{run_id: {"session_id": str|None, "session_db": str|None}}`` for the
    running cards, from the board's own ``owner_admitted`` events.

    Under Hermes v0.23 the dispatcher spawns ``hermes_cli.kanban_worker_client``,
    which only dials the profile owner; the agent then runs INSIDE the gateway as
    one session per run, and the owner records that session on the board as the
    ``owner_admitted`` event for ``tasks.current_run_id``. ``task_runs`` has no
    session column, so this event is the only board-side run -> session link —
    and the only thing that can tell two cards claimed in the same second apart.
    The latest event per run wins (a retried attempt re-admits); an unparseable
    payload is skipped rather than failing the board.
    """
    ids = [i for i in dict.fromkeys(run_ids) if i is not None]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    links = {}
    for e in _q(conn, f"SELECT run_id, payload FROM task_events "
                      f"WHERE kind='owner_admitted' AND run_id IN ({marks}) ORDER BY id", ids):
        try:
            payload = json.loads(e["payload"]) if e["payload"] else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        sid = payload.get("session_id")
        links[e["run_id"]] = {
            "session_id": sid.strip() if isinstance(sid, str) and sid.strip() else None,
            "session_db": _owner_db_path(payload.get("db")),
        }
    return links


def _collect_board(conn, label, now_s, since_s):
    counts = {r["status"]: r["n"] for r in _q(
        conn, "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
    done_24h = _q(conn, "SELECT COUNT(*) AS n FROM tasks WHERE status='done' "
                        "AND completed_at >= ?", (now_s - 86400,))[0]["n"]
    running = _q(conn, """
        SELECT t.id, t.title, t.assignee, t.last_heartbeat_at AS hb_task,
               t.current_run_id, r.profile, r.started_at, r.last_heartbeat_at AS hb_run
        FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id
        WHERE t.status = 'running'""")
    links = _session_links(conn, [r["current_run_id"] for r in running])
    ready = _q(conn, """
        SELECT t.id, t.title, t.assignee, t.priority,
               (SELECT MAX(e.created_at) FROM task_events e
                 WHERE e.task_id = t.id) AS last_event_at, t.created_at
        FROM tasks t WHERE t.status = 'ready'""")
    marks = ",".join("?" * len(OUTCOME_KINDS))
    events = _q(conn, f"""
        SELECT e.task_id, e.kind, e.created_at, t.title, t.assignee
        FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id
        WHERE e.created_at >= ? AND e.kind IN ({marks})
        ORDER BY e.created_at DESC LIMIT ?""",
                (since_s, *OUTCOME_KINDS, RECENT_LIMIT))
    out_run = []
    for r in running:
        beats = [x for x in (r["hb_run"], r["hb_task"], r["started_at"]) if x]
        hb = max(beats) if beats else None
        link = links.get(r["current_run_id"]) or {}
        out_run.append({
            "board": label, "id": r["id"], "title": r["title"],
            "worker": r["profile"] or r["assignee"] or "?",
            "started_at": r["started_at"],
            "run_s": now_s - r["started_at"] if r["started_at"] else None,
            "hb_s": now_s - hb if hb else None,
            # The run's own session, from its `owner_admitted` event. session_db
            # is the path the OWNER named; None means it named none (or named an
            # unreadable one) and the reader uses _state_db_path(home, worker).
            # Consumed by attach_tokens and by the cards-monitor transcript.
            "session_id": link.get("session_id"),
            "session_db": link.get("session_db"),
            "tok_match": None,
        })
    out_ready = [{
        "board": label, "id": r["id"], "title": r["title"],
        "worker": r["assignee"] or "?", "priority": r["priority"] or 0,
        "wait_s": now_s - (r["last_event_at"] or r["created_at"] or now_s),
    } for r in ready]
    out_ev = [{"board": label, "id": e["task_id"], "kind": e["kind"],
               "at": e["created_at"], "title": e["title"] or "",
               "worker": e["assignee"] or "?"} for e in events]
    return counts, done_24h, out_run, out_ready, out_ev


def collect(kanban_db=None, now=None, board=None, read_caps=None):
    """One read-only pass over every board (or just ``board``).

    Returns ``{boards: [{name, counts, done_24h}], running: [...], ready: [...],
    events: [...], caps: (global, per_profile), errors: [...], at: epoch}``.
    Unreadable boards are reported in ``errors`` and never stop the pass.
    """
    now_s = int(now if now is not None else time.time())
    result = {"boards": [], "running": [], "ready": [], "events": [],
              "caps": (None, None), "errors": [], "at": now_s}
    if kanban_db is None:
        kanban_db = autodown._load_kanban_db_or_default()
    if kanban_db is None:
        result["errors"].append(autodown._KANBAN_LOAD.get("reason")
                                or "kanban lib unreachable")
        return result
    result["caps"] = (read_caps or _read_caps)()
    try:
        boards, multi = autodown._enum_board_names(kanban_db)
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"could not enumerate kanban boards: {e}")
        return result
    for b in boards:
        label = b if b else "default"
        if board is not None and label != board:
            continue
        try:
            ctx = kanban_db.connect_closing(board=b) if multi else kanban_db.connect_closing()
            with ctx as conn:
                counts, done_24h, run, ready, ev = _collect_board(
                    conn, label, now_s, now_s - RECENT_WINDOW_S)
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"board {label!r} unreadable: {e}")
            continue
        result["boards"].append({"name": label, "counts": counts, "done_24h": done_24h})
        result["running"] += run
        result["ready"] += ready
        result["events"] += ev
    result["running"].sort(key=lambda r: -(r["run_s"] or 0))
    result["ready"].sort(key=lambda r: (-r["priority"], -r["wait_s"]))
    result["events"].sort(key=lambda e: -e["at"])
    del result["events"][RECENT_LIMIT:]
    return result


def _state_db_path(hermes_home, profile):
    if profile in (None, "", "default"):
        return os.path.join(hermes_home, "state.db")
    return os.path.join(hermes_home, "profiles", profile, "state.db")


def _open_ro(path):
    # closing(): sqlite3's own context manager only ends a transaction, it
    # never closes — a 5 s --live loop over ~50 profiles would leak handles.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return contextlib.closing(conn)


def attach_tokens(data, hermes_home=None, now=None, ledger_path=None):
    """Add worker-session token counts to a ``collect()`` result, in place.

    Each running card gets ``tok_in``/``tok_out`` from ITS OWN worker session and
    ``tok_match`` says how that session was found:

    ``"session"`` — the run's ``owner_admitted`` event named the session, so it
    is read by identity (``WHERE id=?``). This is the normal v0.23 path and the
    window query does not run at all: several cards claimed in the same second
    under one profile are otherwise indistinguishable, and were reported as one
    shared token pair.

    ``"window"`` — labelled FALLBACK only, for a run with no ``owner_admitted``
    event yet (the owner admits ~2 s after the claim): the first
    ``source='kanban'`` session its profile opened within ``SESSION_MATCH_S`` of
    the run's claim. A guess, and it is only ever used when identity is absent.

    Either way the row is honestly blank (``None`` → dash, ``tok_match`` None)
    when nothing matched. ``data["tokens"]`` holds the 24 h / 7 d totals per group
    (``_token_windows``: workers / scheduled / orchestrators / total, each session
    charged to the windows it was *active* in) and ``data["tokens_24h"]`` is that
    table's workers/24 h cell — the same list object, not a second measurement.
    The databases are read-only; the one write is the orchestrator token ledger.
    An unreadable database is skipped, never raised.
    """
    home = hermes_home or HERMES_HOME
    now_s = int(now if now is not None else time.time())
    for r in data["running"]:
        r["tok_in"] = r["tok_out"] = r["tok_match"] = None
        # The owner's own path when its event named one, else the profile default.
        path = r.get("session_db") or _state_db_path(home, r["worker"])
        if not os.path.exists(path):
            continue
        sid = r.get("session_id")
        if sid:
            sql, args, match = ("SELECT input_tokens, output_tokens FROM sessions "
                                "WHERE id=?", (sid,), "session")
        elif r.get("started_at"):
            sql = ("SELECT input_tokens, output_tokens FROM sessions "
                   "WHERE source='kanban' AND started_at BETWEEN ? AND ? "
                   "ORDER BY started_at LIMIT 1")
            args = (r["started_at"] - 5, r["started_at"] + SESSION_MATCH_S)
            match = "window"
        else:
            continue
        try:
            with _open_ro(path) as conn:
                row = conn.execute(sql, args).fetchone()
        except sqlite3.Error:
            continue
        if row:
            r["tok_in"], r["tok_out"] = row["input_tokens"], row["output_tokens"]
            r["tok_match"] = match
    paths = [_state_db_path(home, None)]
    try:
        paths += [os.path.join(e.path, "state.db")
                  for e in os.scandir(os.path.join(home, "profiles")) if e.is_dir()]
    except OSError:
        pass
    data["tokens"] = _token_windows(paths, now_s, ledger_path)
    # One number, one measurement. `tokens_24h` is the table's own workers/24 h cell
    # rather than a second query with its own window semantics: it stays because
    # `--json` has carried it since 2.14.0, and it can no longer disagree with the
    # table the way the old one did — that version summed `source='kanban'` rows by
    # `started_at` here while the table summed them over a different source set, so
    # the same window had two totals on one screen.
    data["tokens_24h"] = data["tokens"]["workers"]["24h"]
    return data


# ── token windows ──────────────────────────────────────────────────────────
#
# What the store actually holds, measured on this host against Hermes
# v0.23.0+7.g0458811 (50 stores, every one opened read-only):
#
# * `messages.token_count` is NULL in all **595,776** message rows — the column
#   exists next to `timestamp` and has never been written. So there is no way to
#   attribute a token to a moment inside a session; a per-message windowed sum
#   would be a fabricated split, not a measurement.
# * `session_model_usage` (all 50 stores, 7,810 rows) does carry input/output/
#   cache/reasoning per `(session_id, model)`, but its only time columns are
#   `first_seen`/`last_seen`: one UPSERTed aggregate row per session and model,
#   no per-call time. It cannot answer "what did this weeks-old REPL spend in the
#   last 24 h" either.
#
# So Hermes stores lifetime totals per session and nothing time-resolved, and the
# 24 h / 7 d figures for anything long-lived stay the *growth of those totals
# between samples the monitor itself keeps in a small ledger* — "since <first
# sample>" until it has a week of them. What a window can honestly mean for the
# rest is a rule, not a split: a session's lifetime total is charged whole to
# every window it was **active** in, `COALESCE(last_activity_at, ended_at,
# started_at) >= window_start`. That predicate is a strict superset of the old
# `started_at >= window_start`, so it only ever adds a session that really did
# work inside the window: measured here, the old one missed 7 kanban sessions
# (58,501,630 in / 684,357 out) that started 7.06 d ago and were active inside
# 7 d. A still-running session can appear in both windows — nothing splits it —
# which is why the table says so out loud (`token_table`) and `--json` carries it
# in `tokens.meta.attribution`.

WINDOWS = (("24h", 86400), ("7d", 7 * 86400))
LEDGER_PATH = os.path.expanduser("~/.hscc/state/kmon_orch_tokens.json")
LEDGER_STEP_S = 300          # one sample per 5 min is plenty for a 24 h window

#: Machine work a card asked for: the dispatched card itself, the subagents it
#: delegated, and one-shots. `cron` is deliberately NOT here — a scheduled job is
#: nobody's card. It used to be billed as orchestrator use, together with every
#: other source (`source NOT IN ('kanban','cron')` swept `subagent`, `telegram`
#: and anything unrecognised into that one row), which is why the two rows did not
#: add up to what they claimed.
WORKER_SOURCES = ("kanban", "subagent", "oneshot")
#: Scheduler runs: neither a card nor an operator, so they get a row of their own
#: instead of hiding inside one of the other two.
SCHEDULED_SOURCES = ("cron",)
#: The third group is the complement, never a list: `cli`/`local` REPLs,
#: `telegram` chats, a NULL source, and any source hermes invents later. A
#: complement keeps `total` honest — the old catch-all lost NULL-source rows
#: because SQL `NOT IN` yields NULL (not TRUE) for them, i.e. they vanished from
#: every row rather than being billed anywhere. Census over the 51 stores on this
#: host: cron 3620, kanban 1895, cli 1526, telegram 103, subagent 54, oneshot 3,
#: local 2, NULL 0 — no other value exists here today.
GROUPS = ("workers", "scheduled", "orchestrators")

#: A session counts in a window when it moved inside it, not when it began.
_ACTIVE_SINCE = "COALESCE(last_activity_at, ended_at, started_at) >= ?"


def group_of(source):
    """The one token-table group a session source is billed to.

    A partition, not a preference: every source — NULL included — lands in
    exactly one group, and `total` in the renderer is the sum of these three
    rows. The suite walks the measured census through this function, so a future
    source cannot quietly join `workers` or disappear.
    """
    if source in WORKER_SOURCES:
        return "workers"
    if source in SCHEDULED_SOURCES:
        return "scheduled"
    return "orchestrators"


def _sum_tokens(paths, queries):
    """Run every ``(sql, args)`` over each readable store, one connection each.

    One open per database for the whole window set: this scans ~50 stores every
    tick and the four window queries used to open each of them four separate
    times. Returns one ``[in, out]`` per query — ``None`` for a query no store
    could answer, which is how "there is no state.db here" reaches the renderer as
    a dash rather than a zero. A store that raises is skipped, never raised.
    """
    total = [[0, 0] for _ in queries]
    seen = [False] * len(queries)
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with _open_ro(path) as conn:
                for i, (sql, args) in enumerate(queries):
                    row = conn.execute(sql, args).fetchone()
                    total[i][0] += row[0] or 0
                    total[i][1] += row[1] or 0
                    seen[i] = True
        except sqlite3.Error:
            continue
    return [t if s else None for t, s in zip(total, seen)]


def _ledger_delta(samples, since_s):
    """Token growth over the samples at/after ``since_s`` (plus the one before).

    Summed step by step and floored at 0 per step, so a deleted session (the
    lifetime total drops) costs nothing instead of making the window negative.
    """
    start = 0
    for i, s in enumerate(samples):
        if s[0] <= since_s:
            start = i
    window = samples[start:]
    delta = [0, 0]
    for prev, cur in zip(window, window[1:]):
        delta[0] += max(0, cur[1] - prev[1])
        delta[1] += max(0, cur[2] - prev[2])
    return delta


def _update_ledger(path, now_s, cum):
    """Append ``[now, in, out]`` at most every LEDGER_STEP_S; keep 8 days.

    Best effort: an unreadable or unwritable ledger just means no history.
    """
    try:
        with open(path) as fh:
            samples = [s for s in json.load(fh) if len(s) == 3]
    except (OSError, ValueError, TypeError):
        samples = []
    if not samples or now_s - samples[-1][0] >= LEDGER_STEP_S:
        samples.append([now_s, cum[0], cum[1]])
        samples = [s for s in samples if s[0] >= now_s - 8 * 86400]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(samples, fh)
            os.replace(tmp, path)
        except OSError:
            pass
    return samples


def _token_windows(paths, now_s, ledger_path=None):
    """``{"workers", "scheduled", "orchestrators", "meta"}`` with 24 h/7 d ``[in, out]``.

    ``workers`` and ``scheduled`` are windowed sums over the sources ``group_of``
    puts in that group, and a session is in a window when it was **active** in it
    (``_ACTIVE_SINCE``) rather than when it began — see the block above
    ``WINDOWS``. ``orchestrators`` is the growth of the remaining sources between
    the monitor's own ledger samples, with ``since`` = the ledger's first sample
    when it covers less than 7 days; those are the sessions that outlive the
    windows and whose per-call time the store does not keep.

    ``total`` is these three rows added and nothing else is in them: the ledger's
    cumulative deliberately excludes ``WORKER_SOURCES`` + ``SCHEDULED_SOURCES`` so
    a card's tokens cannot be billed twice, and it includes NULL-source rows so
    nothing falls between the rows. Cache/reasoning tokens are tracked by v0.23
    and deliberately not shown — the columns stay input and output.
    """
    marks = "(" + ",".join("?" * len(WORKER_SOURCES + SCHEDULED_SOURCES)) + ")"
    queries, keys = [], []
    for group, sources in (("workers", WORKER_SOURCES), ("scheduled", SCHEDULED_SOURCES)):
        sql = ("SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions "
               "WHERE source IN (" + ",".join("?" * len(sources)) + ") AND "
               + _ACTIVE_SINCE)
        for name, span in WINDOWS:
            queries.append((sql, sources + (now_s - span,)))
            keys.append((group, name))
    queries.append(("SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions "
                    "WHERE source IS NULL OR source NOT IN " + marks,
                    WORKER_SOURCES + SCHEDULED_SOURCES))
    keys.append(("orchestrators", "cum"))
    vals = dict(zip(keys, _sum_tokens(paths, queries)))

    out = {"workers": {}, "scheduled": {}}
    for group in ("workers", "scheduled"):
        for name, _ in WINDOWS:
            out[group][name] = vals[(group, name)]
    orch = {}
    cum = vals[("orchestrators", "cum")]
    if cum is None:                                   # no readable store at all
        for name, _ in WINDOWS:
            orch[name] = None
    else:
        samples = _update_ledger(ledger_path or LEDGER_PATH, now_s, cum)
        if samples[0][0] > now_s - WINDOWS[-1][1]:
            orch["since"] = samples[0][0]
        for name, span in WINDOWS:
            orch[name] = _ledger_delta(samples, now_s - span)
    orch.setdefault("since", None)                    # full 7 d of history: no caveat
    out["orchestrators"] = orch
    out["meta"] = {
        "attribution": "a session's lifetime total, charged whole to every window it "
                       "was active in (COALESCE(last_activity_at, ended_at, started_at)); "
                       "the store has no per-call time, so nothing is split across windows",
        "groups": {"workers": list(WORKER_SOURCES),
                   "scheduled": list(SCHEDULED_SOURCES),
                   "orchestrators": "every other source, NULL included (cli, local, "
                                    "telegram) — growth between ledger samples"},
        "columns": "input and output only; cache_read/cache_write/reasoning exist in "
                   "v0.23 and are not counted here",
    }
    return out


# ── live turns (the orchestrators section) ─────────────────────────────────
#
# Hermes v0.23 starts a REPL as `python -I -c '<~10 KB bootstrap>' -p <profile>
# chat`, so `-p <profile> chat` sits at the far end of an argv that begins with
# the interpreter. The gate this section used to have — an argv[:2] entry naming
# `hermes` — matched nothing: measured 2026-11-09 with seven REPL processes alive
# (hscc-orch, ecofire-app-orch, ecofire-bc-orch, ecofire-flows-orch, sphoin-orch,
# micro-notify-orch, headless-display-orch) it returned [], and the section
# silently disappeared instead of showing a wrong answer. Decision D3: the
# session store is the authority, not `ps`, and no argv heuristic is kept.
#
# `session_turn_leases(conversation_id, holder, acquired_at, expires_at)` is one
# row per in-flight turn. Measured on this host: 52 stores are yielded (the root
# db plus 51 `profiles/` directories), 51 of them have a `state.db` and 50 of
# those carry the table — the exception is `profiles/default/state.db`, a 0-byte
# file. It is renewed while the turn runs (a 6238 s-old lease was still 254 s from
# expiry) and it self-expires, so a process that died mid-turn stops being listed
# after one lease TTL rather than forever. A `sessions` row alone proves nothing:
# 871 `source='cli'` sessions here have `ended_at IS NULL` purely because they
# died before anything stamped it, which is why `ended_at` is not consulted below.

#: Sessions that are a dispatched worker, a scheduler or a subagent run — never
#: an operator at a REPL. Census over the 51 stores on this host: `cron` 3617,
#: `kanban` 1893, `cli` 1526, `telegram` 103, `subagent` 54, `oneshot` 3,
#: `local` 2 — and both `local` rows are orchestrator REPL sessions (hscc-orch,
#: ecofire-flows-orch). A live REPL turn is `local` with `created_source='cli'`;
#: older REPL sessions are recorded as plain `cli`. So this is deliberately an
#: EXCLUDE list: an unknown or NULL source renders rather than vanishing, because
#: a false negative here is an empty panel that looks like an idle fleet.
NON_ORCHESTRATOR_SOURCES = ("kanban", "cron", "subagent", "oneshot")

# Cheap indexed probe first: this runs every DEFAULT_INTERVAL over ~50 stores,
# and the join only earns its keep where a live lease actually exists.
_LIVE_LEASE = "SELECT 1 FROM session_turn_leases WHERE expires_at > ? LIMIT 1"

_LIVE_TURNS = (
    "SELECT s.id AS id, s.input_tokens AS tok_in, s.output_tokens AS tok_out, "
    "       s.last_activity_description AS state, l.acquired_at AS acquired_at "
    "FROM session_turn_leases l JOIN sessions s ON s.id = l.conversation_id "
    "WHERE l.expires_at > ? "
    "  AND (s.source IS NULL OR s.source NOT IN (%s)) "
    "ORDER BY l.acquired_at" % ",".join("?" * len(NON_ORCHESTRATOR_SOURCES)))

_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _profile_stores(home):
    """Yield ``(profile, state.db path)`` for every session store: the root
    database is ``default``, every other one is a ``profiles/<name>/`` directory's
    own ``state.db``.

    The path is built from the scanned directory, not from ``_state_db_path``,
    for the same reason ``attach_tokens`` builds it that way: that helper maps
    the NAME ``default`` onto the root db, so a profile directory literally named
    ``default`` (which exists on this host) would otherwise be resolved to the
    root file — opening the root store twice and never opening that directory's
    own database at all.

    ``_known_profile`` still decides what may become a path. The name is now a
    scanned directory entry rather than a stranger's argv, but a profile store is
    only ever addressed by a plain token naming an existing profile directory.
    """
    yield "default", _state_db_path(home, None)
    try:
        with contextlib.closing(os.scandir(os.path.join(home, "profiles"))) as scan:
            entries = sorted(scan, key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        if is_dir and _known_profile(home, e.name):
            yield e.name, os.path.join(e.path, "state.db")


def _known_profile(home, prof):
    """True only for a real profile name: a name never becomes a path unless it
    is a plain token naming an existing ``<home>/profiles/<name>`` directory (or
    ``default``), whoever or whatever produced the name."""
    if prof == "default":
        return True
    return bool(_PROFILE_RE.fullmatch(prof)) and os.path.isdir(
        os.path.join(home, "profiles", prof))


def _clean(text):
    """Strip control characters (ANSI escapes included) from argv/db strings;
    rich markup escaping alone does not neutralise them."""
    return None if text is None else _CONTROL_RE.sub("", str(text))


def attach_orchestrators(data, hermes_home=None, now=None):
    """Add ``data["orchestrators"]``: one row per orchestrator holding a live turn.

    A row is a session whose ``session_turn_leases`` entry has not expired, read
    from the store that owns it: ``profile``, ``session`` (the session id),
    ``turn_s`` (age of the lease's ``acquired_at``), lifetime ``tok_in`` /
    ``tok_out`` and ``state`` (``sessions.last_activity_description``). A REPL
    with no lease row is idle and is NOT listed — the section claims *in a turn
    right now*, which is what its ``(N running)`` header says. Turn liveness
    comes from the lease alone: ``ended_at`` is not consulted, because a live
    REPL keeps writing to a session already stamped ended (seen on
    ecofire-bc-orch) while 871 dead sessions on this host were never stamped at
    all.

    Worker-family sources (``NON_ORCHESTRATOR_SOURCES``) never render here, so a
    dispatched card's turn cannot masquerade as an orchestrator — a live kanban
    lease in a worker profile is exactly what a running card looks like from the
    store side.

    Read-only: every database goes through ``_open_ro`` and nothing here writes.
    A store that is missing, unreadable, locked, or predates
    ``session_turn_leases`` contributes no row and never raises — the rest of the
    view still renders.
    """
    home = hermes_home or HERMES_HOME
    now_s = int(now if now is not None else time.time())
    rows = []
    for prof, path in _profile_stores(home):
        if not os.path.exists(path):
            continue
        try:
            with _open_ro(path) as conn:
                if not conn.execute(_LIVE_LEASE, (now_s,)).fetchone():
                    continue          # nothing in flight in this store
                hits = conn.execute(_LIVE_TURNS,
                                    (now_s, *NON_ORCHESTRATOR_SOURCES)).fetchall()
        except sqlite3.Error:
            continue                  # old store, garbage, or locked: no row
        for h in hits:
            rows.append({
                "profile": prof,
                "session": _clean(h["id"]),
                "turn_s": None if h["acquired_at"] is None
                          else now_s - int(h["acquired_at"]),
                "tok_in": h["tok_in"], "tok_out": h["tok_out"],
                "state": _clean(h["state"])})
    rows.sort(key=lambda r: -(r["turn_s"] if r["turn_s"] is not None else -1))
    data["orchestrators"] = rows
    return data



# ── rendering ──────────────────────────────────────────────────────────────

def fmt_age(s) -> str:
    if s is None:
        return "-"
    s = max(0, int(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def _plain_table() -> Table:
    # same borderless grid as `hscc cluster monitor --live`
    return Table(box=None, pad_edge=False, header_style="accent", padding=(0, 2, 0, 0))


def _title_col(t: Table, width):
    t.add_column("Title", no_wrap=True, overflow="ellipsis",
                 max_width=max(20, (width or 120) - 82))


_EVENT_STYLE = {"completed": "ok", "spawned": "dim", "blocked": "warn",
                "decomposed": "warn", "reclaimed": "warn",
                "block_loop_detected": "warn", "gave_up": "error",
                "crashed": "error", "timed_out": "error", "spawn_failed": "error"}


def running_table(rows, width=None) -> Table:
    t = _plain_table()
    for col, just in (("Board", "left"), ("Card", "left"), ("Worker", "left"),
                      ("Run", "right"), ("Beat", "right"), ("Tokens", "right")):
        t.add_column(col, justify=just, no_wrap=True)
    _title_col(t, width)
    for r in rows:
        run_style = "warn" if (r["run_s"] or 0) > LONG_RUN_S else "ok"
        hb_style = "error" if (r["hb_s"] or 0) > STALE_HEARTBEAT_S else "dim"
        tok = ("-" if r.get("tok_in") is None
               else f"{fmt_tokens(r['tok_in'])}/{fmt_tokens(r['tok_out'])}")
        t.add_row(esc(r["board"]), esc(r["id"]), esc(r["worker"]),
                  Text(fmt_age(r["run_s"]), style=run_style),
                  Text(fmt_age(r["hb_s"]), style=hb_style),
                  Text(tok, style="dim"), esc(r["title"]))
    return t


def orchestrators_table(rows, width=None) -> Table:
    t = _plain_table()
    for col, just in (("Profile", "left"), ("Session", "left"),
                      ("Turn", "right"), ("Tokens", "right")):
        t.add_column(col, justify=just, no_wrap=True)
    t.add_column("state", no_wrap=True, overflow="ellipsis",
                 max_width=max(20, (width or 120) - 72))
    for r in rows:
        tok = ("-" if r.get("tok_in") is None
               else f"{fmt_tokens(r['tok_in'])}/{fmt_tokens(r['tok_out'])}")
        t.add_row(esc(_clean(r["profile"])), esc(r.get("session") or "-"),
                  Text(fmt_age(r.get("turn_s")), style="ok"),
                  Text(tok, style="dim"), esc(_clean(r.get("state")) or "-"))
    return t


#: Two lines the operator cannot infer from the numbers: which source went into
#: which row (the old table gave no hint that `subagent` and `telegram` were being
#: billed as orchestrator use, or that `cron` was not a REPL), and that nothing is
#: split across windows because the store keeps no per-call time. Kept to two short
#: lines on purpose — this pane lives in a tmux split and the running table below it
#: outranks a footnote.
TOKEN_NOTES = (
    "workers = kanban+subagent+oneshot · scheduled = cron "
    "· orchestrators = the rest (cli/local/telegram/NULL)",
    "lifetime totals charged whole to each window a session was active in "
    "· in+out only (cache/reasoning not shown)",
)


def token_rows(tokens) -> list:
    """``[(group, [[in, out] per window]), …]`` — the table's rows as plain numbers.

    This is the only place the arithmetic lives, so the rendered `total` row and any
    check that "the three groups add up" read the same definition. A cell is ``None``
    when nothing was readable behind it (no ``state.db``, no ledger); `total` is
    ``None`` only when every group is, so a dash never becomes a zero. A payload from
    an older producer that carries fewer groups renders the groups it has.
    """
    groups = {g: (tokens.get(g) or {}) for g in GROUPS}

    def window_total(name):
        parts = [groups[g].get(name) for g in GROUPS]
        if all(p is None for p in parts):
            return None
        return [sum((p or [0, 0])[i] for p in parts) for i in (0, 1)]

    rows = [(g, [groups[g].get(name) for name, _ in WINDOWS]) for g in GROUPS]
    rows.append(("total", [window_total(name) for name, _ in WINDOWS]))
    return rows


def token_table(tokens) -> list:
    """The token block: one row per group, in/out per window, two notes, blank row.

    Three groups that add up to `total`, not two that overlap. The old table billed
    every source that was not `kanban` or `cron` — `subagent`, `telegram`, anything
    unrecognised — as orchestrator use and let `total` absorb them, so the rows
    could not be reconciled against the sources they were named after. `total` here
    is the sum of exactly these three rows (``token_rows``), and the notes say which
    source went into which row and what the cells are made of.
    """
    t = _plain_table()
    t.add_column("Tokens", no_wrap=True)
    for name, _ in WINDOWS:
        t.add_column(f"{name} in", justify="right", no_wrap=True)
        t.add_column(f"{name} out", justify="right", no_wrap=True)
    for group, pairs in token_rows(tokens):
        cells = []
        for pair in pairs:
            cells += ["-", "-"] if pair is None else [fmt_tokens(pair[0]),
                                                      fmt_tokens(pair[1])]
        t.add_row(group, *cells, style="" if group == "total" else "dim")
    notes: list[str] = list(TOKEN_NOTES)
    since = (tokens.get("orchestrators") or {}).get("since")
    if since:      # the 7 d orchestrator cell is really "since <when>", so say when
        notes[-1] += " · orch growth since " + datetime.datetime.fromtimestamp(
            since).strftime("%a %H:%M")
    return [t] + [Text(n, style="dim") for n in notes] + [Text("")]


def ready_table(rows, width=None) -> Table:
    t = _plain_table()
    for col, just in (("Board", "left"), ("Card", "left"), ("Worker", "left"),
                      ("Waiting", "right")):
        t.add_column(col, justify=just, no_wrap=True)
    _title_col(t, width)
    for r in rows:
        t.add_row(esc(r["board"]), esc(r["id"]), esc(r["worker"]),
                  Text(fmt_age(r["wait_s"]), style="warn" if r["wait_s"] > 1800 else "dim"),
                  esc(r["title"]))
    return t


def boards_table(boards) -> Table:
    t = _plain_table()
    t.add_column("Board", no_wrap=True)
    for col in STATUS_COLS + ("done 24h",):
        t.add_column(col.capitalize() if col != "done 24h" else "Done 24h",
                     justify="right", no_wrap=True)
    style = {"running": "ok", "ready": "warn", "blocked": "error", "triage": "warn"}
    for b in sorted(boards, key=lambda b: (-b["counts"].get("running", 0), b["name"])):
        c = b["counts"]
        if not any(c.get(s) for s in STATUS_COLS) and not b["done_24h"]:
            continue  # idle board: nothing open, nothing finished today
        cells = [Text(str(c.get(s, 0)), style=style.get(s, "") if c.get(s) else "dim")
                 for s in STATUS_COLS]
        t.add_row(esc(b["name"]), *cells, Text(str(b["done_24h"]), style="dim"))
    return t


def events_table(events, now_s) -> Table:
    t = _plain_table()
    for col, just in (("Ago", "right"), ("Board", "left"), ("Card", "left"),
                      ("Event", "left"), ("Worker", "left")):
        t.add_column(col, justify=just, no_wrap=True)
    t.add_column("Title", no_wrap=True, overflow="ellipsis", max_width=40)
    for e in events:
        t.add_row(fmt_age(now_s - e["at"]), esc(e["board"]), esc(e["id"]),
                  Text(e["kind"], style=_EVENT_STYLE.get(e["kind"], "")),
                  esc(e["worker"]), esc(e["title"]))
    return t


def render(data, interval=DEFAULT_INTERVAL, snapshot=False, width=None, height=None) -> Group:
    cap, per = data["caps"]
    n_run = len(data["running"])
    used = f"{n_run}/{cap}" if cap else str(n_run)
    full = cap is not None and n_run >= cap
    when = "snapshot (use --live to refresh)" if snapshot else f"every {interval}s"
    stamp = datetime.datetime.fromtimestamp(data["at"]).strftime("%H:%M:%S")
    head = Text(f"hscc kanban monitor — {len(data['boards'])} board(s) — ", style="title")
    head.append(f"{used} slots", style="error" if full else "title")
    if per:
        head.append(f" (≤{per}/profile)", style="dim")
    head.append(f" — {when} — {stamp}", style="title")
    parts = [head]
    # One token surface, not two. The old view could print a `tokens 24h in …` line
    # *and* the table, from two different queries over two different source sets, and
    # leave the operator to reconcile them; the table is the only token output now and
    # the `tokens_24h` key is one of its own cells (see `attach_tokens`).
    tok_lines = token_table(data["tokens"]) if data.get("tokens") else []
    parts += tok_lines
    # when every slot is taken the ready cards on other boards strand
    # silently — the one case worth a line of its own
    warn = full and bool(data["ready"])
    if warn:
        parts.append(Text("global cap full: other boards' ready cards wait", style="warn"))
    parts.append(running_table(data["running"], width) if data["running"]
                 else Text("no worker is running", style="dim"))
    tok_height = sum(r.row_count + 1 if isinstance(r, Table) else 1 for r in tok_lines)
    used_lines = (1 + (1 if warn else 0) + tok_height
                  + (len(data["running"]) + 1 if data["running"] else 1))
    # Optional sections in priority order; in a short pane (`--live` in a tmux
    # split) the lower ones are dropped instead of scrolling the view.
    live_boards = [b for b in data["boards"]
                   if any(b["counts"].get(c) for c in STATUS_COLS) or b["done_24h"]]
    optional = []
    orchs = data.get("orchestrators")
    if orchs:
        optional.append((len(orchs) + 3, [
            Text(""), Text(f"orchestrators ({len(orchs)} running)", style="title"),
            orchestrators_table(orchs, width)]))
    if data["ready"]:
        optional.append((len(data["ready"]) + 3, [
            Text(""), Text(f"ready, waiting for a slot ({len(data['ready'])})", style="title"),
            ready_table(data["ready"], width)]))
    optional.append((len(live_boards) + 3, [
        Text(""), Text("boards", style="title"), boards_table(data["boards"])]))
    if data["events"]:
        optional.append((len(data["events"]) + 3, [
            Text(""), Text("last hour", style="title"), events_table(data["events"], data["at"])]))
    budget = None if height is None else height - used_lines - len(data["errors"]) - 1
    for need, section in optional:
        if budget is not None and need > budget:
            break
        parts += section
        if budget is not None:
            budget -= need
    for err in data["errors"]:
        parts.append(Text(esc(err), style="error"))
    if not snapshot:
        parts.append(Text("Ctrl-C to quit", style="dim"))
    return Group(*parts)


def run_live(console, interval: int = DEFAULT_INTERVAL, board: str | None = None,
             kanban_db=None) -> int:
    from rich.live import Live

    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                data = attach_orchestrators(attach_tokens(collect(kanban_db=kanban_db, board=board)))
                live.update(render(data, interval, width=console.width, height=console.height),
                            refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0
