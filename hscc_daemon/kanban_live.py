"""`hscc kanban monitor [--live]`: what the workers on every board are doing.

The kanban twin of `hscc cluster monitor --live`. One read-only pass over every
board (the SAME enumeration `hscc kanban stale` and the autodown C3 interlock
use: ``autodown._load_kanban_db_or_default`` + ``autodown._enum_board_names``)
collects:

* running cards — board, worker profile, run time, heartbeat age, title;
* ready cards — waiting for a slot (global cap or per-profile cap);
* per-board counts by status, plus cards finished in the last 24 h;
* recent outcomes — completed / blocked / gave_up / crashed / timed_out /
  spawn_failed / reclaimed events across all boards.

The header shows the global ``kanban.max_in_progress`` slots in use, because
when every slot is taken the ready cards on other boards strand silently.
Nothing here writes: no claim, no promote, no archive. The kanban lib, the
config reader and the clock are injectable so the collector and renderer test
offline.
"""

from __future__ import annotations

import datetime
import time

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hscc_daemon import autodown
from hscc_daemon.cli_theme import esc

DEFAULT_INTERVAL = 5
RECENT_WINDOW_S = 3600
RECENT_LIMIT = 12
STALE_HEARTBEAT_S = 600      # a running card silent this long is suspicious
LONG_RUN_S = 4 * 3600        # fleet-check rule: >4h on an atomic card = look at it
OUTCOME_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out",
                 "spawn_failed", "reclaimed", "block_loop_detected",
                 "decomposed", "spawned")
STATUS_COLS = ("running", "ready", "todo", "review", "blocked", "triage")


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
                 max_width=max(20, (width or 120) - 70))


_EVENT_STYLE = {"completed": "ok", "spawned": "dim", "blocked": "warn",
                "decomposed": "warn", "reclaimed": "warn",
                "block_loop_detected": "warn", "gave_up": "error",
                "crashed": "error", "timed_out": "error", "spawn_failed": "error"}


def running_table(rows, width=None) -> Table:
    t = _plain_table()
    for col, just in (("Board", "left"), ("Card", "left"), ("Worker", "left"),
                      ("Run", "right"), ("Beat", "right")):
        t.add_column(col, justify=just, no_wrap=True)
    _title_col(t, width)
    for r in rows:
        run_style = "warn" if (r["run_s"] or 0) > LONG_RUN_S else "ok"
        hb_style = "error" if (r["hb_s"] or 0) > STALE_HEARTBEAT_S else "dim"
        t.add_row(esc(r["board"]), esc(r["id"]), esc(r["worker"]),
                  Text(fmt_age(r["run_s"]), style=run_style),
                  Text(fmt_age(r["hb_s"]), style=hb_style), esc(r["title"]))
    return t


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
    parts = [head, slots, Text("")]
    parts.append(Text(f"running ({n_run})", style="title"))
    parts.append(running_table(data["running"], width) if data["running"]
                 else Text("no worker is running", style="dim"))
    used_lines = 4 + (len(data["running"]) + 1 if data["running"] else 1)
    # Optional sections in priority order; in a short pane (`--live` in a tmux
    # split) the lower ones are dropped instead of scrolling the view.
    live_boards = [b for b in data["boards"]
                   if any(b["counts"].get(c) for c in STATUS_COLS) or b["done_24h"]]
    optional = []
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
                data = collect(kanban_db=kanban_db, board=board)
                live.update(render(data, interval, width=console.width, height=console.height),
                            refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0
