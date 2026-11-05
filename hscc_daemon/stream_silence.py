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
   running card is by definition not a legitimately-idle session.
2. **Configured orchestrators** are the per-board ``<board>-orch`` profiles the
   rest of HSCC already resolves through :func:`orch_waker.resolve_orch`.
3. **Last completed model call** is read from hermes' own per-profile OUTPUT
   log (``~/.hermes/profiles/<p>/agent.log``, plus the profile's ``logs/``
   variant; the default profile uses ``~/.hermes/logs/agent.log``). The incident
   proved those lines carry ``latency=`` on completion. This is read-only
   inspection of a log hermes already writes; no hermes state DB is parsed and
   nothing under ``~/.hermes`` is written.
4. **Orchestrator heartbeat** is the log's mtime: any log write (a completion,
   a start line, or a ``Stream stale for ...s`` warning) proves the REPL is
   alive. A genuinely idle orchestrator stops writing, so its mtime tracks its
   last completion and the check stays quiet. A hung orchestrator that keeps
   emitting stale warnings has a fresh mtime with no new completion — exactly
   the alive-but-starved shape this stream exists for.

Age is measured from the last COMPLETION (the portable signal), not from the
start of an in-flight attempt: a start timestamp is not reliably present in
the log tail. N therefore defaults generously to 600 s; the incident report
may tune the env knob once it lands.

Anti-noise contract
-------------------
* No running card / no live orchestrator log -> no candidate, never an alert.
* A stale heartbeat is suppressed: dead panes remain ``orch_waker``'s job.
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

# A completion line (incident evidence: `latency=...`) at the start of a log
# record. Timestamps seen in hermes logs are ISO-ish, local time, optionally
# with brackets / milliseconds / an explicit zone.
_COMPLETION_RE = re.compile(r"\blatency\s*=")
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


def last_completion_age(profile, now=None, hermes_home=None, path=None):
    """``(age_s, source)`` from the newest ``latency=`` line, or ``(None, why)``.

    Reads only the bounded tail of hermes' existing output log. A log that has
    ``Stream stale`` lines but no completion marker is UNKNOWN (○), never
    silently treated as fresh — that would erase precisely the incident shape.
    """
    now = time.time() if now is None else float(now)
    path = path or _pick_log(profile, hermes_home)
    if not path:
        return None, "no agent.log"
    try:
        size = os.path.getsize(path)
        start = max(0, size - LOG_TAIL_BYTES)
        with open(path, "rb") as fh:
            fh.seek(start)
            text = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return None, f"log unreadable: {type(exc).__name__}"
    lines = text.splitlines()
    if start:
        lines = lines[1:]  # first line may be a partial record
    for line in reversed(lines):
        if not _COMPLETION_RE.search(line):
            continue
        m = _TS_RE.search(line[:200])
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if ts is None:
            continue
        return max(0.0, now - ts), os.path.basename(path)
    return None, "no completion marker in tail"


def _log_age(profile, now, hermes_home=None):
    path = _pick_log(profile, hermes_home)
    if not path:
        return None
    try:
        return max(0.0, float(now) - os.path.getmtime(path))
    except OSError:
        return None


def _default_roster(now, hermes_home=None):
    """Running cards + configured orchestrators, all from sanctioned surfaces.

    Cards: ``kanban_live.collect`` (read-only board monitor; the same heartbeat
    the monitor renders). Orchestrators: every board's resolved
    ``<board>-orch``/``orch_map`` profile, with agent.log mtime as heartbeat.
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
            "heartbeat_age_s": r.get("hb_s"),
        })
    errors = list(data.get("errors") or [])
    try:
        from . import orch_waker
        for b in data.get("boards") or []:
            board = b.get("name")
            profile = orch_waker.resolve_orch(board, hermes_home=hermes_home)
            if not profile:
                continue
            sessions.append({
                "key": f"orch:{profile}",
                "kind": "orchestrator",
                "profile": profile,
                "heartbeat_age_s": _log_age(profile, now, hermes_home),
            })
    except Exception as exc:  # noqa: BLE001
        errors.append(f"orchestrator roster: {exc}")
    return {"sessions": sessions, "errors": errors}


def _default_completion(profile, now, hermes_home=None):
    age, source = last_completion_age(profile, now=now,
                                      hermes_home=hermes_home)
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
        comp = completion_fn(profile, now) if profile else {}
        comp = comp or {}
        age = comp.get("last_completed_age_s")
        source = comp.get("source") or ""

        if age is None:
            status = "unknown"
            unknown.append(key)
            age = None
        elif hb is None:
            status = "unknown"
            unknown.append(key)
        elif float(hb) > heartbeat_fresh_s:
            status = "suppressed"
            suppressed.append(key)
        elif float(age) > threshold_s:
            status = "silent"
        else:
            status = "healthy"

        row = {
            "key": key, "kind": kind, "profile": profile,
            "last_completed_age_s": None if age is None else round(float(age)),
            "heartbeat_age_s": None if hb is None else round(float(hb)),
            "status": status, "source": source,
        }
        sessions.append(row)
        if status == "silent":
            violations.append(row)
            log(f"Stream-silence: {key} ({profile}) has not completed a model "
                f"call for {float(age):.0f}s while its heartbeat is fresh "
                f"({float(hb):.0f}s) — threshold {int(threshold_s)}s", "WARN")
            if key not in alerted:
                try:
                    notify_fn(
                        "HSCC: model stream silent",
                        f"{key} ({profile}) has no completed model call for "
                        f"{int(float(age))}s while heartbeating "
                        f"({int(float(hb))}s). The stream may be hung.",
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
        message = (f"all {len(sessions)} running session(s) completed a model "
                   f"call within {int(threshold_s)}s" if sessions
                   else "no running cards or live orchestrators")

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
