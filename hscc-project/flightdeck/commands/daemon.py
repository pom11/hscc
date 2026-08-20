"""daemon.py — `flightdeck daemon` : persistent monitoring/logging, never acting.

The daemon is a persistent background process whose ONLY job is to watch, log,
and (optionally) notify. It mirrors HSCC's ``hscc_daemon`` in SHAPE but is
scoped strictly to READ-ONLY monitoring. This is deliberately NOT the rejected
self-update daemon: the daemon never merges, applies templates, runs ``--apply``,
closes/archives a card, or mutates any project's state. If something needs the
operator's attention, it logs it and optionally notifies — the human decides.
It mirrors HSCC's ``escalate``/``escalate_watcher`` pattern (detect + report,
human decides), NOT its apply-side commands.

Subcommands (matching the hscc_daemon reference):

  * ``start``   — fork the daemon into the background and begin checking.
  * ``stop``    — signal the running daemon to shut down gracefully.
  * ``status``  — running state + last result of every check stream.
  * ``check``   — run one check cycle now (default: all streams, one pass),
                  without needing the daemon to be running.
  * ``watch``   — tail the persisted stream states in real time.
  * ``log``     — show/tail the daemon's log file.
  * ``notify``  — send a macOS notification (the daemon's optional notify path).
  * ``install`` / ``uninstall`` — launchd auto-start installer (NEVER run
                  during verification; it changes what auto-starts at login and
                  needs the operator's explicit go-ahead).

Every check stream here is injectable (``_cards``, ``_projects``, ``_run``,
``_now``, ``_cache``) so tests never touch a real board, repo, network, or
clock.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

from ..core import daemon as d
from ..core import kanban, registry

# --------------------------------------------------------------------------- #
# Check streams — the flightdeck-specific read logic (generic loop in core)
# --------------------------------------------------------------------------- #

# Default fleet ceiling, matching `standup --max-fleet`.
DEFAULT_MAX_FLEET = 3

# Default freshness threshold: a board not read successfully within this many
# seconds is flagged as stale.
DEFAULT_FRESHNESS_THRESHOLD = 3600  # 1 hour

# The set of check streams this daemon runs.
STREAM_NAMES = ("fleet", "freshness", "orphans", "version")


def check_fleet(
    registry_path: str,
    *,
    max_fleet: int = DEFAULT_MAX_FLEET,
    _cards: Optional[Callable] = None,
    _config: Optional[str] = None,
) -> dict:
    """Fleet stream: in-flight cards across all boards vs the cap.

    Computes per-board in-flight counts via ``kanban.in_flight_by_board`` over a
    fleet-wide card read, sums them, and compares the total against the
    ``--max-fleet`` ceiling. The config cap (``kanban.max_in_progress``) is
    reported alongside but enforced per-board by Hermes; the SUM across all
    boards is the real pressure measurement (the six-boards-one-cap failure).

    Returns ``{ok, message, in_flight, cap, ceiling, per_board: {...}}``.
    """
    try:
        cards = _cards() if _cards is not None else kanban.list_cards()
    except kanban.KanbanError as exc:
        return {
            "ok": False,
            "message": f"could not read cards: {exc}",
            "in_flight": -1,
            "cap": kanban.read_max_in_progress(_config),
            "ceiling": int(max_fleet),
            "per_board": {},
        }
    per_board = kanban.in_flight_by_board(cards)
    in_flight = sum(per_board.values())
    cap = kanban.read_max_in_progress(_config)
    ceiling = int(max_fleet)
    over = in_flight > ceiling
    if per_board:
        detail = ", ".join(f"{b}={c}" for b, c in sorted(per_board.items()))
        message = (
            f"{in_flight} in flight across {len(per_board)} board(s) "
            f"(cap {cap}, ceiling {ceiling}): {detail}"
        )
    else:
        message = f"0 in flight (cap {cap}, ceiling {ceiling})"
    return {
        "ok": not over,
        "message": message,
        "in_flight": in_flight,
        "cap": cap,
        "ceiling": ceiling,
        "per_board": per_board,
    }


def check_freshness(
    registry_path: str,
    *,
    threshold: int = DEFAULT_FRESHNESS_THRESHOLD,
    _cards: Optional[Callable] = None,
    _now: Optional[Callable] = None,
) -> dict:
    """Freshness stream: last successful board read per board.

    Records ``now`` for every board successfully read this tick. A board that
    has NOT been read successfully within ``threshold`` seconds is flagged as
    stale — the "cached/stale board is untrustworthy" signal (BRAINSTORM Gap 1).
    Boards are discovered from the cards themselves (every card carries its
    board), so a board with cards is tracked. ``last_read`` maps board -> epoch
    seconds of the most recent successful read (persisted across ticks so the
    staleness accumulator survives daemon restarts).

    Returns ``{ok, message, boards: int, stale: [board,...], last_read: {...}}``.
    """
    now = _now() if _now is not None else time.time()
    try:
        cards = _cards() if _cards is not None else kanban.list_cards()
    except kanban.KanbanError as exc:
        return {
            "ok": False,
            "message": f"could not read cards: {exc}",
            "boards": 0,
            "stale": [],
            "last_read": {},
        }
    prev = d.read_state("freshness") or {}
    last_read: dict[str, float] = dict(prev.get("last_read") or {})

    boards: set[str] = set()
    for card in cards:
        board = card.get("board")
        if board:
            boards.add(str(board))
    for board in boards:
        last_read[str(board)] = float(now)

    stale = sorted(
        board for board, ts in last_read.items() if now - float(ts) > threshold
    )
    if stale:
        message = (
            f"{len(stale)} board(s) not read successfully within {threshold}s: "
            f"{', '.join(stale)}"
        )
    else:
        message = f"{len(boards)} board(s) read fresh (threshold {threshold}s)"
    result = {
        "ok": not stale,
        "message": message,
        "boards": len(boards),
        "stale": stale,
        "last_read": last_read,
    }
    # Persist `last_read` here so the staleness accumulator is durable no
    # matter how the check is invoked (directly, via `check`, or by the loop).
    # Without this, a direct `check freshness` would never record anything to
    # compare on the next run.
    d.write_state("freshness", result)
    return result


def check_orphans(
    registry_path: str,
    *,
    _cards: Optional[Callable] = None,
    _projects: Optional[Callable] = None,
) -> dict:
    """Orphaned-boards stream: legacy/unregistered boards holding cards.

    Reuses ``legacy._registered_boards`` + ``legacy._collect`` verbatim so this
    stream can never disagree with ``flightdeck legacy-cards``. An orphan board
    holds a card whose workspace resolves to no registered project, or sits on a
    board with no registry mapping. Read-only — never re-homes or archives.

    Returns ``{ok, message, orphan_boards: {board: card_count},
    unmanaged_cards: int}``.
    """
    from . import legacy

    projects = (
        _projects() if _projects is not None else registry.load_registry(registry_path)
    )
    try:
        cards = _cards() if _cards is not None else kanban.list_cards()
    except kanban.KanbanError as exc:
        return {
            "ok": False,
            "message": f"could not read cards: {exc}",
            "orphan_boards": {},
            "unmanaged_cards": 0,
        }
    rows = legacy._collect(cards, projects)
    unmanaged_cards = len(rows)
    registered = legacy._registered_boards(projects)
    orphan_boards: dict[str, int] = {}
    for row in rows:
        board = str(row.get("board") or "")
        if not board or board in registered:
            continue
        orphan_boards[board] = orphan_boards.get(board, 0) + 1
    if orphan_boards:
        detail = ", ".join(f"{b}={c}" for b, c in sorted(orphan_boards.items()))
        message = (
            f"{len(orphan_boards)} orphan board(s) holding "
            f"{unmanaged_cards} unmanaged card(s): {detail}"
        )
    else:
        message = "no orphan boards"
    return {
        "ok": not orphan_boards,
        "message": message,
        "orphan_boards": orphan_boards,
        "unmanaged_cards": unmanaged_cards,
    }


# --------------------------------------------------------------------------- #
# Version stream — flightdeck's OWN installed-vs-remote drift (rate-limited)
# --------------------------------------------------------------------------- #
#
# Mirrors the version-drift notice design (BRAINSTORM Gap 2 step 3): compare
# flightdeck's OWN installed version against the newest tag on its OWN git
# remote. Purely a notice — never auto-applies anything. The network-reaching
# ``git ls-remote`` is rate-limited via a cache file, so the daemon never re-hits
# the remote more than once per TTL even though it checks the stream hourly.

UPDATE_CACHE_DEFAULT = "update-check.yaml"
UPDATE_CACHE_TTL_SECONDS = 86400  # once per day
_FLIGHTDECK_PROJECT = "flightdeck"


def _update_state_path(_cache: Optional[str] = None) -> str:
    """Where the version-check cache lives, or an injected override (tests)."""
    if _cache:
        return os.path.expanduser(str(_cache))
    return os.path.join(d.daemon_home(), UPDATE_CACHE_DEFAULT)


def _version_tuple(v) -> tuple:
    """``'0.6.0'`` / ``'v0.6.0'`` -> ``(0, 6, 0)`` for ordering.

    Extracts the leading run of integers from each dot-separated component so a
    ``v`` prefix and any non-numeric suffix are ignored. Unparseable -> ``(0,)``
    (still orderable, never an error).
    """
    import re

    return tuple(int(p) for p in re.findall(r"\d+", str(v))) or (0,)


def _latest_tag(ls_remote_out: str) -> Optional[str]:
    """The newest ``refs/tags/vX.Y.Z`` ref, or None when none exist.

    Skips the peeled ``^{}`` refs and picks the max by :func:`_version_tuple` so
    the answer never depends on ``ls-remote``'s own sort order. No version tags
    -> None (UNKNOWN), never a false positive.
    """
    best: Optional[str] = None
    best_t: tuple = (0,)
    for line in ls_remote_out.splitlines():
        if "refs/tags/v" not in line or line.endswith("^{}"):
            continue
        tag = line.split("refs/tags/")[-1].strip()
        t = _version_tuple(tag)
        if t > best_t:
            best, best_t = tag, t
    return best


def _installed_flightdeck_version(project, *, _run: Optional[Callable] = None) -> Optional[str]:
    """flightdeck's OWN installed version via its registry ``installed_version_cmd``.

    Reuses ``deployment._dispatch`` so a test never executes a real command.
    None when the project declares no such command or it fails — the honest
    UNKNOWN, never folded into a value.
    """
    from ..core import deployment

    cmd = getattr(project, "installed_version_cmd", None)
    if not cmd:
        return None
    cp = deployment._dispatch(cmd, project.repo, _run)
    if cp.returncode != 0:
        return None
    return (cp.stdout or "").strip() or None


def _fresh_version_check(project, *, _run: Optional[Callable] = None):
    """``(remote_latest_tag, installed_version)`` via one cheap ls-remote.

    The ONLY network-reaching call in the module. ``git ls-remote`` touches the
    remote but does not fetch or mutate anything locally. Non-zero exit
    (offline, no origin) degrades to ``(None, installed)`` -> no notice, never a
    fabricated one. Both reads route through the injectable ``_run``.
    """
    from ..core import deployment, git_state

    installed = _installed_flightdeck_version(project, _run=_run)
    cp = git_state._dispatch(
        ["git", "ls-remote", "--tags", "--sort=-v:refname", "origin", "v*"],
        project.repo,
        _run,
    )
    if cp.returncode != 0:
        return (None, installed)
    return (_latest_tag(cp.stdout or ""), installed)


def _load_update_cache(path: str) -> Optional[dict]:
    """The cached verdict, or None when absent/unreadable/ill-formed.

    An absent file, an unparseable file, or one missing a valid ``checked_at``
    are all None -> the next call does a fresh check. A malformed cache never
    raises and never fabricates a verdict.
    """
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("checked_at"), (int, float)):
        return None
    return data


def _save_update_cache(path: str, data: dict) -> bool:
    """Persist the verdict; failure is silent (a notice is never worth a crash)."""
    import yaml

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False)
        return True
    except OSError:
        return False


def check_version(
    registry_path: str,
    *,
    _run: Optional[Callable] = None,
    _now: Optional[Callable] = None,
    _cache: Optional[str] = None,
) -> dict:
    """Version stream: flightdeck's OWN installed-vs-remote drift (rate-limited).

    Compares flightdeck's OWN installed version against the newest tag on its
    own git remote. The ``ls-remote`` fires only when the cache is stale
    (``checked_at`` older than :data:`UPDATE_CACHE_TTL_SECONDS`); every other
    run reads the cached verdict. Never auto-applies — purely a notice pointing
    at ``flightdeck update``.

    Returns ``{ok, message, state, installed, remote, cache_hit}`` where
    ``state`` is OK / DRIFTED / UNKNOWN / NO_ENTRY.
    """
    projects = registry.load_registry(registry_path)
    project = next((p for p in projects if p.name == _FLIGHTDECK_PROJECT), None)
    if project is None or not getattr(project, "repo", None):
        return {
            "ok": True,
            "message": "no self-entry (no registered 'flightdeck' project) — nothing to compare",
            "state": "NO_ENTRY",
            "installed": None,
            "remote": None,
            "cache_hit": False,
        }
    if not getattr(project, "installed_version_cmd", None):
        return {
            "ok": True,
            "message": "no installed_version_cmd for 'flightdeck' — cannot compare",
            "state": "UNKNOWN",
            "installed": None,
            "remote": None,
            "cache_hit": False,
        }

    now = _now() if _now is not None else time.time()
    path = _update_state_path(_cache)

    cached = _load_update_cache(path)
    cache_hit = False
    if cached is not None and now - cached["checked_at"] < UPDATE_CACHE_TTL_SECONDS:
        latest, installed = cached.get("latest"), cached.get("installed")
        cache_hit = True
    else:
        latest, installed = _fresh_version_check(project, _run=_run)
        _save_update_cache(
            path, {"checked_at": now, "latest": latest, "installed": installed}
        )

    if not latest or not installed:
        return {
            "ok": True,
            "message": "version check UNKNOWN (could not determine installed/remote)",
            "state": "UNKNOWN",
            "installed": installed,
            "remote": latest,
            "cache_hit": cache_hit,
        }
    drifted = _version_tuple(latest) > _version_tuple(installed)
    if drifted:
        message = (
            f"flightdeck update available: installed {installed}, "
            f"remote {latest} (run flightdeck update)"
        )
    else:
        message = (
            f"flightdeck up to date (installed {installed}, remote {latest})"
        )
    return {
        "ok": not drifted,
        "message": message,
        "state": "DRIFTED" if drifted else "OK",
        "installed": installed,
        "remote": latest,
        "cache_hit": cache_hit,
    }


CHECK_FNS: dict[str, Callable] = {
    "fleet": check_fleet,
    "freshness": check_freshness,
    "orphans": check_orphans,
    "version": check_version,
}

# --------------------------------------------------------------------------- #
# Subcommand implementations
# --------------------------------------------------------------------------- #


def cmd_start(args: argparse.Namespace, registry_path: str) -> int:
    """Start the daemon in the background (fork), or report it already running."""
    pid = d.get_pid()
    if pid is not None:
        print(f"Daemon already running (PID {pid})")
        return 0

    print("Starting flightdeck daemon…")
    d.log("Daemon starting")

    child = os.fork()
    if child > 0:
        # Parent: save the child PID and return immediately.
        d.save_pid()
        print(f"flightdeck daemon started (PID {child})")
        return 0

    # Child — detach into a session, respond to signals, become the daemon.
    os.setsid()
    stop = threading.Event()
    d.install_signal_handlers(stop)
    os.chdir(os.path.expanduser("~"))
    d.save_pid()  # grandchild/child pid is what's now running
    try:
        d.run_daemon_loop(registry_path, CHECK_FNS, stop_event=stop)
    except Exception as exc:  # noqa: BLE001 - a crash clears the PID and exits
        d.log(f"Daemon crashed: {exc}", "ERROR")
        d.write_stopped()
        os._exit(1)
    d.write_stopped()
    os._exit(0)


def _cmd_start_daemon(registry_path: str) -> int:
    """Foreground daemon entry (used by launchd supervision, or to run without forking).

    Runs ``core.daemon.run_daemon_loop`` directly in THIS process with signal
    handlers wired for graceful stop. Used by the launchd plist (which must
    supervise the actual loop, never a forked child) and by ``run`` when
    ``--start-daemon`` is passed. Writes its own PID so ``status``/``stop``
    work against launchd-supervised instances too.
    """
    d.save_pid()  # record this process as the daemon
    stop = threading.Event()
    d.install_signal_handlers(stop)
    d.log("start-daemon invoked (foreground / service-supervised mode)")
    try:
        d.run_daemon_loop(registry_path, CHECK_FNS, stop_event=stop)
    except Exception as exc:  # noqa: BLE001 - a crash clears the PID and exits
        d.log(f"Daemon crashed: {exc}", "ERROR")
        d.write_stopped()
        return 1
    d.write_stopped()
    return 0


def cmd_stop(args: argparse.Namespace, registry_path: str) -> int:
    """Stop the running daemon (SIGTERM, escalate to SIGKILL after a wait)."""
    pid = d.get_pid()
    if pid is None:
        print("Daemon is not running")
        d.write_stopped()
        return 0
    print(f"Stopping flightdeck daemon (PID {pid})…")
    d.log("Daemon stop requested")
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                print(f"flightdeck daemon stopped (PID {pid})")
                d.write_stopped()
                return 0
        os.kill(pid, signal.SIGKILL)
        print(f"flightdeck daemon force-killed (PID {pid})")
    except ProcessLookupError:
        print("flightdeck daemon already stopped")
    finally:
        d.write_stopped()
    return 0


def _status_line(stream: str, state: Optional[dict]) -> str:
    """One ``status`` row for a stream from its persisted state (or 'never')."""
    if not state:
        return f"  {stream:<12s} — never"
    ok = state.get("ok")
    ok_str = "OK" if ok is True else ("FAIL" if ok is False else "—")
    ts = str(state.get("timestamp") or "?")[:19]
    msg = str(state.get("message") or "")
    return f"  {stream:<12s} {ok_str:<5s} {ts:<22s} {msg}"


def cmd_status(args: argparse.Namespace, registry_path: str) -> int:
    """Show daemon running state and each stream's last check result."""
    pid = d.get_pid()
    states = d.read_all_states()

    print("=" * 60)
    print("  flightdeck daemon status")
    print("=" * 60)
    if pid is not None:
        print(f"  Status:    RUNNING (PID {pid})")
    else:
        stale = os.path.exists(d.PID_FILE)
        if stale:
            print("  Status:    STOPPED (stale PID file — cleared)")
            d.write_stopped()
        else:
            print("  Status:    STOPPED")

    print()
    if not states:
        print("  No check results yet")
    else:
        print("  ── Check Streams ──────────────────────")
        print(f"  {'Stream':<12s} {'':<5s} {'Last Check':<22s} Message")
        print(f"  {'─'*12} {'─'*5} {'─'*22} {'─'*60}")
        for name in STREAM_NAMES:
            print(_status_line(name, states.get(name)))
    print()
    print("=" * 60)
    return 0


def cmd_check(args: argparse.Namespace, registry_path: str) -> int:
    """Run one check cycle now (optionally one stream), print + persist results.

    Runs the named stream, or every stream in one pass when ``stream`` is None
    or ``all``. Each result is persisted to state (so ``status`` reflects it)
    and printed. Does NOT require the daemon to be running — this is the
    one-shot probe an operator (or cron) can invoke by hand.
    """
    stream = getattr(args, "stream", None)
    if stream and stream != "all":
        if stream not in CHECK_FNS:
            print(
                f"error: unknown stream {stream!r}; known: {', '.join(STREAM_NAMES)}",
                file=sys.stderr,
            )
            return 2
        result = _run_one_sync(stream, registry_path, args)
        _print_result(stream, result)
        return 0

    results: dict[str, dict] = {}
    for name in STREAM_NAMES:
        results[name] = _run_one_sync(name, registry_path, args)
    for name in STREAM_NAMES:
        _print_result(name, results[name])
    return 0


def _run_one_sync(name: str, registry_path: str, args: argparse.Namespace) -> dict:
    """Run one stream with the injectables carried on ``args`` (tests)."""
    fn = CHECK_FNS[name]
    kwargs: dict[str, Any] = {}
    if name == "fleet":
        kwargs["max_fleet"] = int(getattr(args, "max_fleet", DEFAULT_MAX_FLEET))
        if getattr(args, "cards", None) is not None:
            kwargs["_cards"] = args.cards
    if name == "freshness":
        kwargs["threshold"] = int(getattr(args, "threshold", DEFAULT_FRESHNESS_THRESHOLD))
        if getattr(args, "cards", None) is not None:
            kwargs["_cards"] = args.cards
        if getattr(args, "now", None) is not None:
            kwargs["_now"] = args.now
    if name == "orphans":
        if getattr(args, "cards", None) is not None:
            kwargs["_cards"] = args.cards
        if getattr(args, "projects", None) is not None:
            kwargs["_projects"] = args.projects
    if name == "version":
        if getattr(args, "run", None) is not None:
            kwargs["_run"] = args.run
        if getattr(args, "now", None) is not None:
            kwargs["_now"] = args.now
        if getattr(args, "cache", None) is not None:
            kwargs["_cache"] = args.cache
    try:
        result = fn(registry_path, **kwargs)
    except Exception as exc:  # noqa: BLE001 - same degradation as the loop
        result = {"ok": False, "message": f"check raised: {exc}"}
    d.write_state(name, result)
    return result


def _print_result(name: str, result: dict) -> None:
    ok = result.get("ok")
    status = "OK" if ok is True else ("FAIL" if ok is False else "—")
    print(f"[{name}] {status} — {result.get('message')}")


def stream_watcher(stream: Optional[str], interval: int = 2) -> int:
    """Tail the persisted stream states, printing a line when one changes.

    Prints each stream's current result (or prints its state the first time it
    appears), then polls every ``interval`` seconds printing lines only when a
    stream's state changed. Ctrl-C exits cleanly with code 0. This is the
    ``watch`` subcommand's loop — it reads state under ``daemon/state/`` written
    by the daemon (or by a ``check`` run), never a live board.
    """
    print(f"Watching {stream or 'all'} stream(s) (Ctrl-C to stop)…\n")
    last_state: dict[str, dict] = {}
    target = [stream] if stream and stream != "all" else None
    try:
        while True:
            states = d.read_all_states()
            names = target if target is not None else list(states.keys())
            for name in names:
                current = states.get(name)
                if not current:
                    continue
                if current != last_state.get(name):
                    last_state[name] = current
                    ts = str(current.get("timestamp") or "?")[:19]
                    ok = current.get("ok")
                    ok_str = "OK" if ok is True else ("FAIL" if ok is False else "—")
                    print(f"[{ts}] {name:<12s} {ok_str:<5s} {current.get('message')}")
            d_log_sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")
    return 0


def d_log_sleep(interval: int) -> None:
    """Pause between watch polls."""
    time.sleep(interval)


def cmd_watch(args: argparse.Namespace, registry_path: str) -> int:
    """Tail persisted stream states in real time (Ctrl-C to stop)."""
    interval = max(1, int(getattr(args, "interval", 2)))
    return stream_watcher(getattr(args, "stream", None), interval=interval)


def cmd_log(args: argparse.Namespace, registry_path: str) -> int:
    """Show the daemon's log file (tail)."""
    lines = d.get_daemon_log_tail(int(getattr(args, "lines", 50)))
    if not lines:
        print("No daemon log entries yet.")
        return 0
    for line in lines:
        print(line.rstrip())
    return 0


def _osascript_notify(title: str, message: str) -> bool:
    """Send a macOS notification via osascript. Returns success.

    This is the daemon's optional notify path — a human notification, not a
    state mutation. Failure (no osascript, non-macOS, notification denied) is a
    False, never a raise.
    """
    script = (
        'display notification '
        + json.dumps(message)
        + ' with title '
        + json.dumps(title)
    )
    try:
        cp = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return cp.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_notify(args: argparse.Namespace, registry_path: str) -> int:
    """Send a macOS notification with a timestamped title (manual notify path)."""
    message = getattr(args, "message", None) or "flightdeck daemon notification"
    ts = d.now_iso()[:19]
    title = f"flightdeck {ts}"
    print(f"Sending notification: {title}")
    ok = _osascript_notify(title, message)
    print(f"  {'Sent' if ok else 'Failed (osascript unavailable)'}")
    d.log(f"notify: {'sent' if ok else 'failed'} — {message}")
    return 0 if ok else 1


def cmd_install(args: argparse.Namespace, registry_path: str) -> int:
    """Install the launchd auto-start service (gated; never auto-run)."""
    from .daemon_install import cmd_install as _install

    return _install(args, registry_path)


def cmd_uninstall(args: argparse.Namespace, registry_path: str) -> int:
    """Remove the launchd auto-start service (gated; never auto-run)."""
    from .daemon_install import cmd_uninstall as _uninstall

    return _uninstall(args, registry_path)


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def _build_check(stream: str) -> Callable[[str], dict]:
    """A zero-arg ``check_fn(registry_path)`` bound to one stream with defaults."""
    return CHECK_FNS[stream]


def build_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "daemon",
        help="persistent monitoring/logging daemon (watch + log + notify, never acts)",
        epilog=(
            "examples:\n"
            "  flightdeck daemon start               start the background daemon\n"
            "  flightdeck daemon status              show running state + last checks\n"
            "  flightdeck daemon check fleet         run one check cycle for 'fleet'\n"
            "  flightdeck daemon check               run every stream once now\n"
            "  flightdeck daemon log                 show the daemon log\n"
            "  flightdeck daemon watch               tail stream states in real time\n"
            "  flightdeck daemon stop                stop the daemon\n"
            "  flightdeck daemon install             install launchd auto-start (needs your go-ahead)\n"
        ),
    )
    p.add_argument(
        "--registry",
        default=registry.DEFAULT_REGISTRY,
        metavar="PATH",
        help=argparse.SUPPRESS,  # registry comes from cli.py's global --registry
    )
    # Internal (launchd-supervised) entry: run the loop in the FOREGROUND so
    # launchd supervises the actual daemon process. Also a plain way to run the
    # loop without forking.
    p.add_argument(
        "--start-daemon",
        action="store_true",
        help=argparse.SUPPRESS,  # internal; use `daemon start` to fork in background
    )
    subcmd = p.add_subparsers(dest="daemon_command", metavar="SUBCOMMAND")

    s = subcmd.add_parser(
        "start", help="start the daemon in the background",
        epilog="example: flightdeck daemon start",
    )
    s.set_defaults(func=cmd_start)

    st = subcmd.add_parser(
        "stop", help="stop the running daemon",
        epilog="example: flightdeck daemon stop",
    )
    st.set_defaults(func=cmd_stop)

    status = subcmd.add_parser(
        "status", help="show running state and last check results",
        epilog="example: flightdeck daemon status",
    )
    status.set_defaults(func=cmd_status)

    chk = subcmd.add_parser(
        "check",
        help="run one check cycle now (one stream, or all when omitted)",
        epilog="example: flightdeck daemon check fleet",
    )
    chk.add_argument(
        "stream",
        nargs="?",
        choices=list(STREAM_NAMES) + ["all"],
        help="stream to check (default: all)",
    )
    chk.add_argument(
        "--max-fleet",
        type=int,
        default=DEFAULT_MAX_FLEET,
        metavar="N",
        help=f"fleet ceiling for the fleet stream (default: {DEFAULT_MAX_FLEET})",
    )
    chk.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_FRESHNESS_THRESHOLD,
        metavar="N",
        help=f"freshness staleness threshold in seconds (default: {DEFAULT_FRESHNESS_THRESHOLD})",
    )
    chk.set_defaults(func=cmd_check)

    wt = subcmd.add_parser(
        "watch",
        help="tail persisted stream states in real time",
        epilog="example: flightdeck daemon watch freshness",
    )
    wt.add_argument(
        "stream",
        nargs="?",
        choices=list(STREAM_NAMES) + ["all"],
        help="stream to watch (default: all)",
    )
    wt.add_argument(
        "--interval",
        type=int,
        default=2,
        metavar="N",
        help="seconds between state polls (default: 2)",
    )
    wt.set_defaults(func=cmd_watch)

    lg = subcmd.add_parser(
        "log",
        help="show the daemon log (tail)",
        epilog="example: flightdeck daemon log",
    )
    lg.add_argument(
        "--lines",
        type=int,
        default=50,
        metavar="N",
        help="number of log lines to show (default: 50)",
    )
    lg.set_defaults(func=cmd_log)

    nt = subcmd.add_parser(
        "notify",
        help="send a macOS notification (the daemon's optional notify path)",
        epilog="example: flightdeck daemon notify 'fleet over cap'",
    )
    nt.add_argument(
        "message",
        nargs="?",
        help="notification message (default: generic)",
    )
    nt.set_defaults(func=cmd_notify)

    ins = subcmd.add_parser(
        "install",
        help="install launchd auto-start (requires explicit --apply)",
        epilog="example: flightdeck daemon install --apply",
    )
    ins.add_argument(
        "--apply",
        action="store_true",
        help="perform the install (dry-run by default; nothing is written without this)",
    )
    ins.set_defaults(func=cmd_install)

    un = subcmd.add_parser(
        "uninstall",
        help="remove the launchd auto-start service",
        epilog="example: flightdeck daemon uninstall",
    )
    un.set_defaults(func=cmd_uninstall)


def run(args: argparse.Namespace, registry_path: str) -> int:
    """Entry from cli.py: attach injectable handles, then dispatch."""
    # Internal launchd/foreground entry: run the loop in-process.
    if getattr(args, "start_daemon", False):
        return _cmd_start_daemon(registry_path)

    func = getattr(args, "func", None)
    if func is None:
        # No subcommand matched — fall through to a helpful status.
        print("usage: flightdeck daemon <start|stop|status|check|watch|log|notify|install|uninstall>")
        print("Try `flightdeck daemon <SUBCOMMAND> --help` for details.")
        return 2

    # Attach the injectable handles each subcommand's tests expect (default to
    # None so production uses the real readers).
    if func in (cmd_check, cmd_watch):
        args.cards = getattr(args, "cards", None)
        args.projects = getattr(args, "projects", None)
        args.now = getattr(args, "now", None)
        args.run = getattr(args, "run", None)
        args.cache = getattr(args, "cache", None)
    return func(args, registry_path)
