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
* tokens — each running card's worker session (input/output) and the 24 h
  total over every profile's kanban sessions, read from the profiles'
  ``state.db`` (``attach_tokens``; the dispatcher tags worker sessions
  ``source='kanban'`` and they open ~2 s after the run claims the card);
  plus 24 h / 7 d totals for workers and orchestrators (``_token_windows``);
* orchestrators — every live ``hermes -p <profile> chat`` REPL with its open
  session's title, last activity and lifetime tokens (``attach_orchestrators``).

The header shows the global ``kanban.max_in_progress`` slots in use, because
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
import subprocess
import time

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hscc_daemon import autodown
from hscc_daemon.cli_theme import esc
from hscc_daemon.cluster_live import fmt_tokens

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
SESSION_MATCH_S = 900        # worker session opens within this of the run claim


def _read_caps():
    """``(max_in_progress, max_in_progress_per_profile)`` from Hermes config.

    Either may be None (unset or unreadable). Read-only; never raises.
    """
    try:
        from hermes_cli.config import load_config
        k = (load_config() or {}).get("kanban") or {}
    except Exception:  # noqa: BLE001
        return None, None

    def _int(v):
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None
    return _int(k.get("max_in_progress")), _int(k.get("max_in_progress_per_profile"))


def _q(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _collect_board(conn, label, now_s, since_s):
    counts = {r["status"]: r["n"] for r in _q(
        conn, "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
    done_24h = _q(conn, "SELECT COUNT(*) AS n FROM tasks WHERE status='done' "
                        "AND completed_at >= ?", (now_s - 86400,))[0]["n"]
    running = _q(conn, """
        SELECT t.id, t.title, t.assignee, t.last_heartbeat_at AS hb_task,
               r.profile, r.started_at, r.last_heartbeat_at AS hb_run
        FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id
        WHERE t.status = 'running'""")
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
        out_run.append({
            "board": label, "id": r["id"], "title": r["title"],
            "worker": r["profile"] or r["assignee"] or "?",
            "started_at": r["started_at"],
            "run_s": now_s - r["started_at"] if r["started_at"] else None,
            "hb_s": now_s - hb if hb else None,
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

    Each running card gets ``tok_in``/``tok_out`` from the first
    ``source='kanban'`` session its worker profile opened within
    ``SESSION_MATCH_S`` of the run's claim (None when not found), and
    ``data["tokens_24h"]`` is ``[in, out]`` summed over every profile's kanban
    sessions started in the last 24 h (None when no state.db was readable).
    ``data["tokens"]`` adds 24h/7d windows for workers and orchestrators
    (``_token_windows``). The databases are read-only; the one write is the
    orchestrator token ledger. An unreadable database is skipped, never raised.
    """
    home = hermes_home or HERMES_HOME
    now_s = int(now if now is not None else time.time())
    for r in data["running"]:
        r["tok_in"] = r["tok_out"] = None
        if not r.get("started_at"):
            continue
        path = _state_db_path(home, r["worker"])
        if not os.path.exists(path):
            continue
        try:
            with _open_ro(path) as conn:
                row = conn.execute(
                    "SELECT input_tokens, output_tokens FROM sessions "
                    "WHERE source='kanban' AND started_at BETWEEN ? AND ? "
                    "ORDER BY started_at LIMIT 1",
                    (r["started_at"] - 5, r["started_at"] + SESSION_MATCH_S)).fetchone()
        except sqlite3.Error:
            continue
        if row:
            r["tok_in"], r["tok_out"] = row["input_tokens"], row["output_tokens"]
    paths = [_state_db_path(home, None)]
    try:
        paths += [os.path.join(e.path, "state.db")
                  for e in os.scandir(os.path.join(home, "profiles")) if e.is_dir()]
    except OSError:
        pass
    total, seen = [0, 0], False
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with _open_ro(path) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
                    "FROM sessions WHERE source='kanban' AND started_at >= ?",
                    (now_s - 86400,)).fetchone()
        except sqlite3.Error:
            continue
        seen = True
        total[0] += row[0]
        total[1] += row[1]
    data["tokens_24h"] = total if seen else None
    data["tokens"] = _token_windows(paths, now_s, ledger_path)
    return data


# ── orchestrators ──────────────────────────────────────────────────────────
#
# Worker (kanban) and cron sessions are short, so a started_at window counts
# them exactly. Orchestrator REPL sessions live for weeks and Hermes stores only
# their lifetime totals (messages.token_count is never filled), so their 24 h /
# 7 d use is the growth of those totals between samples the monitor itself
# keeps in a small ledger — "since <first sample>" until it has a week.

WINDOWS = (("24h", 86400), ("7d", 7 * 86400))
LEDGER_PATH = os.path.expanduser("~/.hscc/state/kmon_orch_tokens.json")
LEDGER_STEP_S = 300          # one sample per 5 min is plenty for a 24 h window
SHORT_SOURCES = ("kanban", "cron")


def _sum_tokens(paths, sql, args=()):
    total, seen = [0, 0], False
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with _open_ro(path) as conn:
                row = conn.execute(sql, args).fetchone()
        except sqlite3.Error:
            continue
        seen = True
        total[0] += row[0] or 0
        total[1] += row[1] or 0
    return total if seen else None


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
    """``{"workers": {...}, "orchestrators": {...}}`` with 24h/7d ``[in, out]``.

    workers = kanban sessions by started_at; orchestrators = cron sessions by
    started_at plus the ledger growth of every long-lived (cli/gateway) session,
    with ``since`` = the ledger's first sample when it covers less than 7 days.
    """
    marks = ",".join("?" * len(SHORT_SOURCES))
    out = {"workers": {}, "orchestrators": {}}
    for name, span in WINDOWS:
        out["workers"][name] = _sum_tokens(
            paths, "SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions "
                   "WHERE source='kanban' AND started_at >= ?", (now_s - span,))
        out["orchestrators"][name] = _sum_tokens(
            paths, "SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions "
                   "WHERE source='cron' AND started_at >= ?", (now_s - span,))
    cum = _sum_tokens(paths, "SELECT SUM(input_tokens), SUM(output_tokens) FROM sessions "
                             f"WHERE source NOT IN ({marks})", SHORT_SOURCES)
    since = None
    if cum is not None:
        samples = _update_ledger(ledger_path or LEDGER_PATH, now_s, cum)
        if samples[0][0] > now_s - WINDOWS[-1][1]:
            since = samples[0][0]
        for name, span in WINDOWS:
            grew = _ledger_delta(samples, now_s - span)
            base = out["orchestrators"][name] or [0, 0]
            out["orchestrators"][name] = [base[0] + grew[0], base[1] + grew[1]]
    out["orchestrators"]["since"] = since
    return out


def _chat_profiles(ps_lines):
    """Profiles with a live ``hermes -p <profile> chat`` REPL, in ps order.

    Kanban workers run ``hermes_cli.main -p X --cli`` (no ``chat``), and the
    ``zsh -c 'hermes … chat'`` pane wrapper has a shell as argv[0], so both are
    skipped.
    """
    seen = []
    for line in ps_lines:
        argv = line.split()
        if "chat" not in argv or not any(
                a.endswith("/hermes") or a == "hermes" for a in argv[:2]):
            continue
        for flag in ("-p", "--profile"):
            if flag in argv[:-1]:
                prof = argv[argv.index(flag) + 1]
                if prof not in seen:
                    seen.append(prof)
    return seen


_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _known_profile(home, prof):
    """True only for a real profile name: the ps argv is any local process's,
    so a name never becomes a path unless it is a plain token naming an
    existing ``<home>/profiles/<name>`` directory (or ``default``)."""
    if prof == "default":
        return True
    return bool(_PROFILE_RE.fullmatch(prof)) and os.path.isdir(
        os.path.join(home, "profiles", prof))


def _clean(text):
    """Strip control characters (ANSI escapes included) from argv/db strings;
    rich markup escaping alone does not neutralise them."""
    return None if text is None else _CONTROL_RE.sub("", str(text))


def _ps_lines():
    try:
        return subprocess.run(["ps", "-Ao", "args="], capture_output=True, text=True,
                              timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def attach_orchestrators(data, hermes_home=None, now=None, ps_lines=None):
    """Add ``data["orchestrators"]``: one row per live orchestrator REPL.

    Each row is the profile's ``cli`` session with the latest message — title,
    last activity, lifetime input/output tokens. ``ended_at`` is not trusted: a
    live REPL can keep writing to a session already stamped ended. A profile
    whose state.db can't be read still gets a row (dashes). Read-only.
    """
    home = hermes_home or HERMES_HOME
    now_s = int(now if now is not None else time.time())
    rows = []
    for prof in _chat_profiles(_ps_lines() if ps_lines is None else ps_lines):
        if not _known_profile(home, prof):
            continue
        row = {"profile": prof, "title": None, "last_s": None,
               "tok_in": None, "tok_out": None}
        path = _state_db_path(home, prof)
        if os.path.exists(path):
            try:
                with _open_ro(path) as conn:
                    hit = conn.execute(
                        "SELECT s.title, s.input_tokens, s.output_tokens, "
                        "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id=s.id) AS last "
                        "FROM sessions s WHERE s.source='cli' "
                        "ORDER BY last DESC LIMIT 1").fetchone()
            except sqlite3.Error:
                hit = None
            if hit:
                row.update(title=_clean(hit["title"]), tok_in=hit["input_tokens"],
                           tok_out=hit["output_tokens"],
                           last_s=None if hit["last"] is None else now_s - int(hit["last"]))
        rows.append(row)
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
    for col, just in (("Profile", "left"), ("Last", "right"), ("Tokens", "right")):
        t.add_column(col, justify=just, no_wrap=True)
    t.add_column("Session", no_wrap=True, overflow="ellipsis",
                 max_width=max(20, (width or 120) - 60))
    for r in rows:
        tok = ("-" if r.get("tok_in") is None
               else f"{fmt_tokens(r['tok_in'])}/{fmt_tokens(r['tok_out'])}")
        busy = r["last_s"] is not None and r["last_s"] < 120
        t.add_row(esc(_clean(r["profile"])),
                  Text(fmt_age(r["last_s"]), style="ok" if busy else "dim"),
                  Text(tok, style="dim"), esc(r["title"] or "-"))
    return t


def _tok_pair(pair):
    return "-" if pair is None else f"{fmt_tokens(pair[0])}/{fmt_tokens(pair[1])}"


def token_lines(tokens) -> list:
    """Header lines: fleet total per window, then the workers/orchestrators split."""
    w, o = tokens["workers"], tokens["orchestrators"]
    head = Text("tokens", style="dim")
    for name, _ in WINDOWS:
        tot = [(w[name] or [0, 0])[i] + (o[name] or [0, 0])[i] for i in (0, 1)]
        head.append(f"  {name} in {fmt_tokens(tot[0])} · out {fmt_tokens(tot[1])}", style="dim")
    head.append("  (workers + orchestrators, all profiles)", style="dim")
    split = Text("  workers " + " · ".join(f"{n} {_tok_pair(w[n])}" for n, _ in WINDOWS)
                 + "    orchestrators " + " · ".join(f"{n} {_tok_pair(o[n])}" for n, _ in WINDOWS),
                 style="dim")
    if o.get("since"):
        stamp = datetime.datetime.fromtimestamp(o["since"]).strftime("%m-%d %H:%M")
        split.append(f"  (orchestrator history since {stamp})", style="dim")
    return [head, split]


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
    head = Text(f"hscc kanban monitor — {len(data['boards'])} board(s) — {when} — {stamp}",
                style="title")
    slots = Text("slots in use ")
    slots.append(used, style="error" if full else "ok")
    if per:
        slots.append(f"  (per profile ≤ {per})", style="dim")
    slots.append(f"   ready waiting {len(data['ready'])}",
                 style="warn" if full and data["ready"] else "dim")
    if full and data["ready"]:
        slots.append("  ← global cap full: other boards' ready cards wait", style="warn")
    parts = [head, slots]
    tok24 = data.get("tokens_24h")
    tok_lines = []
    if data.get("tokens"):
        # own lines: appended to `slots` they wrap in a ~105-col tmux split
        tok_lines = token_lines(data["tokens"])
    elif tok24:
        tok_lines = [Text(f"tokens 24h in {fmt_tokens(tok24[0])} · out {fmt_tokens(tok24[1])}"
                          "  (kanban worker sessions, all profiles)", style="dim")]
    parts += tok_lines
    parts.append(Text(""))
    parts.append(Text(f"running ({n_run})", style="title"))
    parts.append(running_table(data["running"], width) if data["running"]
                 else Text("no worker is running", style="dim"))
    used_lines = (4 + len(tok_lines)
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
