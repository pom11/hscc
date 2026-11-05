"""Stream-silence watchdog (t_291ced43, incident 2026-11-05).

The 2026-11-05 incident was invisible to every existing health stream: model
streams hung for ~20 minutes across workers and orchestrators while heartbeats
stayed fresh, the proxy answered new requests, and the engines were busy or
idle in the usual shapes. The daemon watched engines and the streams it owns,
but nothing watched "an agent that is alive and heartbeating yet has not
COMPLETED a model call in N minutes".

Signal sources (chosen, deliberately)
-------------------------------------
1. **Running kanban cards** come from the sanctioned, read-only board monitor
   (:func:`kanban_live.collect`): ``task_runs.last_heartbeat_at`` /
   ``tasks.last_heartbeat_at`` are the dispatcher's own worker heartbeat, and a
   running card is by definition not a legitimately-idle session. Each card is
   matched to ITS OWN worker session by the ``[session_id]`` on its log lines
   and the session-id spawn timestamp versus the run's ``started_at`` (the same
   profile+spawn-time rule ``~/fleet-check/card-stats.py`` uses). A profile can
   run several cards at once; another session's completion must never mask a
   silent one, and an ambiguous match is reported UNKNOWN, never fresh.
2. **Configured orchestrators** are the per-board ``<board>-orch`` profiles the
   rest of HSCC already resolves through :func:`orch_waker.resolve_orch`.
3. **Last completed model call** is read from hermes' own per-profile OUTPUT
   log (``~/.hermes/profiles/<p>/agent.log``, plus the profile's ``logs/``
   variant; the default profile uses ``~/.hermes/logs/agent.log``). The incident
   proved those lines carry ``latency=`` on completion. Card sessions are
   filtered to the matched ``[session_id]``; orchestrators use the newest
   completion in their profile. This is read-only inspection of a log hermes
   already writes; no hermes state DB is parsed and nothing under ``~/.hermes``
   is written.
4. **Orchestrator alive/busy state** comes from the tmux pane, through the one
   pane matcher the repo already uses (:mod:`repl_inject`, shared with
   ``~/bin/hermes-inject`` and ``orch_waker``): exactly one ``hermes -p X chat``
   pane means alive, ``msg=interrupt`` in its prompt means a turn is running,
   and a bare prompt means idle. Log mtime is NOT a heartbeat — housekeeping
   lines freshen it without a call, and a hung stream writes nothing at all.
   An idle orchestrator at its prompt is never starved, however old its last
   call; a live pane that is busy in a turn with no completion for N seconds is
   the alive-but-starved shape this stream exists for. No pane is the dead-pane
   case and stays suppressed (``orch_waker`` owns it).

Age is measured from the last COMPLETION (the portable signal), not from the
start of an in-flight attempt: a start timestamp is not reliably present in
the log tail. N therefore defaults generously to 600 s; the incident report
may tune the env knob once it lands.

Anti-noise contract
-------------------
* No running card / no live orchestrator pane -> no candidate, never an alert.
* An idle orchestrator at its prompt is never an alert, however old its last
  call; housekeeping log lines cannot make it look busy.
* A stale card heartbeat is suppressed: dead panes remain ``orch_waker``'s job.
  An orchestrator whose pane is absent is suppressed for the same reason.
* A card whose session cannot be matched unambiguously is UNKNOWN, never
  fresh and never an alert.
* Per-session cooldown: one desktop notify per ``NOTIFY_COOLDOWN_S`` (1800 s,
  persisted in the stream state so a daemon restart cannot re-page), while
  every tick still writes a silent WARN log line for the ongoing silence.
* The stream is visibility, NOT a failing gate: it always stamps ``ok: True``.
  ``hscc verify`` gets a separate informational row listing last-completion
  ages (``○`` when unknown) that is never red by itself.

Tests are hermetic: injectable roster/completion/notify/state seams and
fabricated tmp log files only. No live hermes, board, desktop or network touch
happens in this module's test suite.
"""

import json
import os
import re
import time

from .daemon_ops import log
from .state import now_iso

STREAM = "stream_silence"

# N: age of the last COMPLETED model call that counts as starvation while the
# heartbeat is fresh. 600 s is the card's default; the 2026-11-05 incident
# report (docs/audits/) may tune it when it lands. Env-injectable for ops.
SILENCE_THRESHOLD_S = float(
    os.environ.get("HSCC_STREAM_SILENCE_THRESHOLD_S", "600"))

# A heartbeat older than this is the dead-pane/dispatcher case, covered by
# orch_waker; suppress here rather than double-report. Kept equal to N: a
# process whose own heartbeat has not advanced for as long as the starvation
# threshold is not "alive but starved".
HEARTBEAT_FRESH_S = float(
    os.environ.get("HSCC_STREAM_SILENCE_HEARTBEAT_FRESH_S", "600"))

# One operator notify per session per window; persisted in stream state (the
# proxy-stall / cooldowns precedent) so a daemon restart does not re-page.
NOTIFY_COOLDOWN_S = float(
    os.environ.get("HSCC_STREAM_SILENCE_NOTIFY_COOLDOWN_S", "1800"))

# Bounded tail read: agent.log can be large; only the newest completion marker
# matters and the daemon must never burn a tick scanning a multi-GB log.
LOG_TAIL_BYTES = 256 * 1024

# A worker session id embeds its spawn local time (YYYYMMDD_HHMMSS_suffix).
# Match that against the card run's started_at the way card-stats.py does.
SESSION_MATCH_SLACK_S = float(
    os.environ.get("HSCC_STREAM_SILENCE_SESSION_MATCH_S", "180"))

# A completion line (incident evidence: `latency=...`) at the start of a log
# record. Timestamps seen in hermes logs are ISO-ish, local time, optionally
# with brackets / milliseconds / an explicit zone.
_COMPLETION_RE = re.compile(r"\blatency\s*=")
_SESSION_RE = re.compile(r"\[(\d{8}_\d{6}_\w+)\]")
_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")

_HERMES_HOME = os.path.expanduser("~/.hermes")


def _fmt_age(age):
    if age is None:
        return "○"
    try:
        age = max(0, int(round(float(age))))
    except (TypeError, ValueError):
        return "○"
    if age < 120:
        return f"{age}s"
    if age < 7200:
        return f"{age // 60}m"
    return f"{age // 3600}h{(age % 3600) // 60:02d}m"


def _parse_ts(text):
    """Epoch seconds for a log-line timestamp, or None when unparseable.

    Naive timestamps are interpreted as local time (hermes logs in local
    time); explicit ``Z``/offset forms keep their zone. This is only ever
    compared to wall-clock ``now``, never used for ordering across zones.
    """
    try:
        raw = text.replace(",", ".")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        import datetime
        dt = datetime.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _log_candidates(profile, hermes_home=None):
    home = os.path.expanduser(hermes_home or _HERMES_HOME)
    profile = str(profile or "").strip()
    if not profile:
        return []
    if profile in ("default", "main"):
        return [os.path.join(home, "logs", "agent.log"),
                os.path.join(home, "agent.log")]
    base = os.path.join(home, "profiles", profile)
    return [os.path.join(base, "agent.log"),
            os.path.join(base, "logs", "agent.log")]


def _pick_log(profile, hermes_home=None):
    for path in _log_candidates(profile, hermes_home):
        if os.path.isfile(path):
            return path
    return None


def _read_tail(path, max_bytes=None):
    """(lines, truncated) from the bounded tail of ``path``."""
    max_bytes = LOG_TAIL_BYTES if max_bytes is None else int(max_bytes)
    size = os.path.getsize(path)
    start = max(0, size - max_bytes)
    with open(path, "rb") as fh:
        fh.seek(start)
        text = fh.read().decode("utf-8", "replace")
    lines = text.splitlines()
    if start:
        lines = lines[1:]  # first line may be a partial record
    return lines, bool(start)


def _session_spawn_epoch(session_id):
    """Epoch seconds encoded in a hermes session id (local time), or None."""
    m = re.match(r"^(\d{8})_(\d{6})_", str(session_id or ""))
    if not m:
        return None
    try:
        import datetime
        dt = datetime.datetime.strptime(
            f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        return dt.timestamp()
    except ValueError:
        return None


def _matched_session(lines, started_at, slack_s=SESSION_MATCH_SLACK_S):
    """Session id(s) that could belong to a run started at ``started_at``.

    More than one candidate is AMBIGUOUS: the caller must report unknown, never
    let one sibling's completion stand in for another. Returns
    ``(session_id or None, why)``.
    """
    seen = {}
    for line in lines:
        m = _SESSION_RE.search(line)
        if not m:
            continue
        sid = m.group(1)
        if sid in seen:
            continue
        spawn = _session_spawn_epoch(sid)
        if spawn is None:
            continue
        if abs(spawn - float(started_at)) <= float(slack_s):
            seen[sid] = spawn
    if not seen:
        return None, "no session matched run start"
    if len(seen) > 1:
        return None, (f"ambiguous session match ({len(seen)} sessions within "
                      f"{int(float(slack_s))}s of run start)")
    return next(iter(seen)), ""


def last_completion_age(profile, now=None, hermes_home=None, path=None,
                        session_id=None, started_at=None,
                        match_slack_s=None):
    """``(age_s, source)`` from the newest matching ``latency=`` line.

    Returns ``(None, why)`` when there is no signal. With ``session_id`` the
    search is restricted to that session; with ``started_at`` (and no explicit
    id) the session is resolved by profile+spawn time, and an ambiguous match
    is UNKNOWN. The orchestrator path passes neither and uses the profile's
    newest completion. Reads only the bounded tail of hermes' existing output
    log. A log that has ``Stream stale`` lines but no completion marker is
    UNKNOWN (○), never silently treated as fresh — that would erase precisely
    the incident shape.
    """
    now = time.time() if now is None else float(now)
    match_slack_s = (SESSION_MATCH_SLACK_S if match_slack_s is None
                     else float(match_slack_s))
    path = path or _pick_log(profile, hermes_home)
    if not path:
        return None, "no agent.log"
    try:
        lines, _ = _read_tail(path)
    except OSError as exc:
        return None, f"log unreadable: {type(exc).__name__}"

    if session_id is None and started_at is not None:
        session_id, why = _matched_session(lines, started_at,
                                           slack_s=match_slack_s)
        if session_id is None:
            return None, why

    for line in reversed(lines):
        if not _COMPLETION_RE.search(line):
            continue
        if session_id is not None:
            m_sid = _SESSION_RE.search(line)
            if not m_sid or m_sid.group(1) != session_id:
                continue
        m = _TS_RE.search(line[:200])
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        source = os.path.basename(path)
        if session_id:
            source = f"{source}:{session_id}"
        return max(0.0, now - ts), source
    if session_id:
        return None, f"no completion marker for {session_id} in tail"
    return None, "no completion marker in tail"


def _pane_states():
    """``profile -> {pane_alive, pane_busy}`` from the repo's one pane matcher.

    Uses ``repl_inject`` exactly as ``orch_waker`` does (never raw tmux here):
    exactly one ``hermes -p X chat`` pane is alive, and ``msg=interrupt`` in its
    prompt means a turn is running (idle otherwise). A pane list failure is
    UNKNOWN, not "dead": the check must not turn its own blindness into a
    suppression.
    """
    try:
        from . import orch_waker
        repl = orch_waker.load_repl_inject()
    except Exception as exc:  # noqa: BLE001
        return {}, f"repl_inject: {type(exc).__name__}: {exc}"
    if repl is None:
        return {}, "repl_inject not importable"
    try:
        panes = repl.hermes_panes()
    except Exception as exc:  # noqa: BLE001 — no tmux server, etc.
        return {}, f"pane discovery: {type(exc).__name__}: {exc}"
    by_profile = {}
    for pane in panes:
        by_profile.setdefault(str(pane.get("profile") or ""), []).append(pane)
    states = {}
    for profile, matches in by_profile.items():
        if not profile:
            continue
        if len(matches) != 1:
            # Alive but which pane is "the" REPL is ambiguous: busy unknown.
            states[profile] = {"pane_alive": True, "pane_busy": None}
            continue
        busy = None
        try:
            busy = bool(repl.busy_from_screen(
                repl.screen(matches[0]["pane"], 12)))
        except Exception:  # noqa: BLE001 — alive pane, unreadable prompt
            pass
        states[profile] = {"pane_alive": True, "pane_busy": busy}
    return states, ""


def _default_roster(now, hermes_home=None, pane_states_fn=None):
    """Running cards + configured orchestrators, all from sanctioned surfaces.

    Cards: ``kanban_live.collect`` (read-only board monitor; the same heartbeat
    the monitor renders) plus the run's ``started_at`` for session matching.
    Orchestrators: every board's resolved ``<board>-orch``/``orch_map`` profile,
    with alive/busy read from its tmux pane (never log mtime).
    """
    from . import kanban_live
    try:
        data = kanban_live.collect(now=now)
    except Exception as exc:  # noqa: BLE001 — fail open, named in state
        return {"sessions": [], "errors": [f"board monitor: {exc}"]}
    sessions = []
    for r in data.get("running") or []:
        sessions.append({
            "key": f"card:{r.get('board')}:{r.get('id')}",
            "kind": "card",
            "profile": r.get("worker") or "",
            "started_at": r.get("started_at"),
            "session_id": r.get("session_id"),
            "heartbeat_age_s": r.get("hb_s"),
        })
    errors = list(data.get("errors") or [])
    try:
        if pane_states_fn is not None:
            pane_states, pane_err = pane_states_fn()
        else:
            pane_states, pane_err = _pane_states()
        if pane_err:
            errors.append(pane_err)
        from . import orch_waker
        for b in data.get("boards") or []:
            board = b.get("name")
            profile = orch_waker.resolve_orch(board, hermes_home=hermes_home)
            if not profile:
                continue
            state = pane_states.get(profile)
            if state is None and pane_err:
                state = {"pane_alive": None, "pane_busy": None}
            elif state is None:
                state = {"pane_alive": False, "pane_busy": False}
            sessions.append({
                "key": f"orch:{profile}",
                "kind": "orchestrator",
                "profile": profile,
                "heartbeat_age_s": None,
                "pane_alive": state.get("pane_alive"),
                "pane_busy": state.get("pane_busy"),
            })
    except Exception as exc:  # noqa: BLE001
        errors.append(f"orchestrator roster: {exc}")
    return {"sessions": sessions, "errors": errors}


def _default_completion(item, now, hermes_home=None):
    """Last completion for one roster item (card session or orchestrator)."""
    if isinstance(item, str):  # legacy seam shape: profile only
        item = {"kind": "orchestrator", "profile": item}
    profile = item.get("profile") or ""
    kind = item.get("kind") or "session"
    session_id = item.get("session_id")
    started_at = item.get("started_at") if kind == "card" else None
    if kind == "card" and session_id is None and started_at is None:
        return {"last_completed_age_s": None,
                "source": "no run start for session match"}
    age, source = last_completion_age(
        profile, now=now, hermes_home=hermes_home,
        session_id=session_id, started_at=started_at)
    return {"last_completed_age_s": age, "source": source}


def _default_notify(title, body, priority="normal"):
    from .desktop import send_macos_notification
    send_macos_notification(title, body, priority=priority)


def _prior_alerts(prior, wall_now, cooldown_s):
    """Persisted per-session notify stamps, pruned to the live cooldown window."""
    out = {}
    for key, stamp in ((prior or {}).get("alerted") or {}).items():
        try:
            stamp = float(stamp)
        except (TypeError, ValueError):
            continue
        if 0 <= wall_now - stamp <= cooldown_s:
            out[str(key)] = stamp
    return out


def check_stream_silence(roster_fn=None, completion_fn=None, notify_fn=None,
                         write_state_fn=None, read_state_fn=None, emit_fn=None,
                         now=None, wall_now=None, threshold_s=None,
                         heartbeat_fresh_s=None, notify_cooldown_s=None,
                         hermes_home=None):
    """One stream-silence tick (the ``stream_silence`` CHECK STREAM).

    Returns True always: this stream detects and surfaces, it never fails the
    fleet gate. Every path stamps state so ``hscc verify`` can show it is live.
    A raising seam is contained by this wrapper (fail open, no notify).
    """
    try:
        return _tick(roster_fn, completion_fn, notify_fn, write_state_fn,
                     read_state_fn, emit_fn, now, wall_now, threshold_s,
                     heartbeat_fresh_s, notify_cooldown_s, hermes_home)
    except Exception as exc:  # pragma: no cover - defensive, stream never dies
        log(f"Stream-silence check: unexpected {type(exc).__name__}: {exc} — "
            "fail open (no notify)", "ERROR")
        return True


def _tick(roster_fn, completion_fn, notify_fn, write_state_fn, read_state_fn,
          emit_fn, now, wall_now, threshold_s, heartbeat_fresh_s,
          notify_cooldown_s, hermes_home):
    from .state import read_state, write_state
    roster_fn = roster_fn or _default_roster
    completion_fn = completion_fn or _default_completion
    notify_fn = notify_fn or _default_notify
    write_state_fn = write_state_fn or write_state
    read_state_fn = read_state_fn or read_state
    if emit_fn is None:
        from .desktop import emit_event
        emit_fn = emit_event
    wall_now = time.time() if wall_now is None else float(wall_now)
    if now is None:
        now = wall_now
    threshold_s = SILENCE_THRESHOLD_S if threshold_s is None else float(threshold_s)
    heartbeat_fresh_s = (HEARTBEAT_FRESH_S if heartbeat_fresh_s is None
                         else float(heartbeat_fresh_s))
    notify_cooldown_s = (NOTIFY_COOLDOWN_S if notify_cooldown_s is None
                         else float(notify_cooldown_s))

    prior = read_state_fn(STREAM) or {}
    alerted = _prior_alerts(prior, wall_now, notify_cooldown_s)

    roster = roster_fn(now) or {}
    sessions = []
    violations = []
    unknown = []
    suppressed = []

    for item in roster.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        profile = str(item.get("profile") or "")
        kind = str(item.get("kind") or "session")
        hb = item.get("heartbeat_age_s")
        comp = completion_fn(item, now) if profile else {}
        comp = comp or {}
        age = comp.get("last_completed_age_s")
        source = comp.get("source") or ""
        pane_alive = item.get("pane_alive")
        pane_busy = item.get("pane_busy")

        if kind == "orchestrator":
            # Log mtime is not a heartbeat. The pane is: idle at its prompt is
            # never starved (however old the last call), busy in a turn is the
            # only shape that can be stream-silent, absent is the dead-pane
            # case already owned by orch_waker.
            if pane_alive is False:
                status = "suppressed"
                suppressed.append(key)
            elif pane_alive is None:
                status = "unknown"
                unknown.append(key)
            elif pane_busy is False:
                status = "idle"
            elif pane_busy is None:
                status = "unknown"
                unknown.append(key)
            elif age is None:
                status = "unknown"
                unknown.append(key)
            elif float(age) > threshold_s:
                status = "silent"
            else:
                status = "healthy"
        else:
            if hb is None:
                status = "unknown"
                unknown.append(key)
            elif float(hb) > heartbeat_fresh_s:
                status = "suppressed"
                suppressed.append(key)
            elif age is None:
                status = "unknown"
                unknown.append(key)
            elif float(age) > threshold_s:
                status = "silent"
            else:
                status = "healthy"

        row = {
            "key": key, "kind": kind, "profile": profile,
            "last_completed_age_s": None if age is None else round(float(age)),
            "heartbeat_age_s": None if hb is None else round(float(hb)),
            "pane_alive": pane_alive if kind == "orchestrator" else None,
            "pane_busy": pane_busy if kind == "orchestrator" else None,
            "status": status, "source": source,
        }
        sessions.append(row)
        if status == "silent":
            violations.append(row)
            if kind == "orchestrator":
                beat = "its pane is busy in a turn"
            else:
                beat = f"its heartbeat is fresh ({float(hb):.0f}s)"
            log(f"Stream-silence: {key} ({profile}) has not completed a model "
                f"call for {float(age):.0f}s while {beat} — threshold "
                f"{int(threshold_s)}s", "WARN")
            if key not in alerted:
                try:
                    notify_fn(
                        "HSCC: model stream silent",
                        f"{key} ({profile}) has no completed model call for "
                        f"{int(float(age))}s while {beat}. The stream may be "
                        "hung.",
                        priority="high")
                except Exception as exc:  # noqa: BLE001 — notify is best-effort
                    log(f"Stream-silence: notify failed: "
                        f"{type(exc).__name__}: {exc}", "WARN")
                alerted[key] = wall_now
                emit_fn("stream_silence.warn", {
                    "key": key, "kind": kind, "profile": profile,
                    "last_completed_age_s": row["last_completed_age_s"],
                    "heartbeat_age_s": row["heartbeat_age_s"],
                    "threshold_s": int(threshold_s),
                }, severity="warning")
            else:
                log(f"Stream-silence: {key} still silent — notify suppressed "
                    f"by {int(notify_cooldown_s)}s cooldown", "WARN")

    if violations:
        status = "silent"
        message = (f"{len(violations)} session(s) silent while heartbeating; "
                   f"threshold {int(threshold_s)}s")
    elif unknown:
        status = "unknown"
        message = f"no alert; {len(unknown)} session(s) without a completion signal"
    elif suppressed:
        status = "suppressed"
        message = f"no alert; {len(suppressed)} session(s) had a stale heartbeat"
    else:
        status = "healthy"
        message = (f"no silent sessions among {len(sessions)} running card(s) "
                   f"or orchestrator(s); threshold {int(threshold_s)}s"
                   if sessions else "no running cards or live orchestrators")

    write_state_fn(STREAM, {
        "ok": True,
        "status": status,
        "threshold_s": int(threshold_s),
        "heartbeat_fresh_s": int(heartbeat_fresh_s),
        "notify_cooldown_s": int(notify_cooldown_s),
        "checked": len(sessions),
        "violations": [r["key"] for r in violations],
        "unknown": unknown,
        "suppressed": suppressed,
        "sessions": sessions,
        "alerted": alerted,
        "errors": list(roster.get("errors") or []),
        "last_check": now_iso(),
        "message": message,
    })
    return True


def stream_silence_row(state_dir=None, now=None):
    """Informational ``hscc verify`` row: last-completion ages, never red.

    Reads the daemon's stream state only (no board/log/hermes access). ``○``
    means no completion signal for that session — visibility, not a verdict.
    """
    state_dir = os.path.expanduser(state_dir or "~/.hscc/state")
    path = os.path.join(state_dir, f"{STREAM}.json")
    if not os.path.isfile(path):
        return {"name": "stream_silence", "ok": True,
                "detail": "skipped: no stream_silence state yet"}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"name": "stream_silence", "ok": True,
                "detail": f"skipped: state unreadable ({type(exc).__name__})"}
    if not isinstance(data, dict):
        return {"name": "stream_silence", "ok": True,
                "detail": "skipped: unexpected state schema"}
    rows = [r for r in data.get("sessions") or [] if isinstance(r, dict)]
    if not rows:
        return {"name": "stream_silence", "ok": True,
                "detail": data.get("message") or "no running sessions"}
    shown = rows[:12]
    parts = []
    for r in shown:
        age = _fmt_age(r.get("last_completed_age_s"))
        flag = " WARN" if r.get("status") == "silent" else ""
        parts.append(f"{r.get('key')}={age}{flag}")
    more = f" (+{len(rows) - len(shown)} more)" if len(rows) > len(shown) else ""
    status = data.get("status") or "unknown"
    return {"name": "stream_silence", "ok": True,
            "detail": f"{status}; " + ", ".join(parts) + more}
