"""Durable node-pause overlay: park one chosen node without touching any config.

WHY THIS EXISTS
---------------
The operator needs to take a single worker GPU out of the fleet for a while
(first real use: a training run) and put it back afterwards, WITHOUT editing
anything the cluster is built from. The existing knobs all fail that test:
templates, ``serving.json``, the applied-template record, kanban caps, autodown
and the proxy config are the cluster's source of truth, and every one of them
has a watcher or an apply path that would either fight the edit or restore the
node from it. A pause must therefore be an OVERLAY — a separate record the
repair paths consult — and nothing else.

DURABLE, NOT IN-MEMORY
----------------------
The flaw this file exists to remove: pause state held in a module-level dict is
lost when the daemon restarts, and the restart is exactly what happens in the
worst case (an operator parks a node, a crash or an upgrade cycles the daemon,
and the watchdog then "heals" the paused unit back to life on top of whatever
the node is now running). So the single source of truth is the file
``~/.hscc/node_pauses.json``, written atomically (tmp file in the SAME directory
+ ``os.replace``), and read again on every query. There is deliberately no
in-process cache: the writer is usually a *different* process (the CLI) from the
reader (the daemon loop), and a cache would reintroduce the same
wrongly-stale-pause bug in a new place.

THE OVERLAY NEVER WRITES ANYTHING ELSE
-------------------------------------
This module writes exactly one file, its own overlay. It does not import
serving/templates and has no path that mutates any other state, so "pause a node
without disturbing the cluster settings" holds by construction. Stopping and
relaunching units is NOT this module's job — the CLI/exec layer does that
through sparkrun and then records the result here (``stopped``).

EXPiry IS EXPLICIT, ``paused()`` IS TRUTH-ACCURATE
--------------------------------------------------
``paused(host)`` answers "is there a pause record holding this node?" and
ignores the clock. An entry whose ``until`` has passed is still a pause: the
resume has not happened yet, and a repair path that decided the node was free
the moment the deadline passed would relaunch it a few seconds before the
daemon's own auto-resume did. Expiry is acted on only by the explicit
:func:`pending_resume` / :func:`expiry_pass` pair, which is what the daemon tick
runs (resume the units first, then ``expiry_pass``/``clear``). Conservative in
both directions: an unparseable ``until`` is treated as *open-ended*, never as
"expired", because the failure we must never have is resuming a node behind the
operator's back.

WHAT A SKIP SEAM MUST ASK
-------------------------
A unit can span several nodes (tensor-parallel across hosts), and pausing one of
its nodes takes the whole span down. A pause record is keyed by the host it was
issued against, and the rest of the span is listed in its ``span_nodes``. So a
seam that only asks ``paused(host)`` will happily heal the *other* node of the
span. Skip seams (watchdog auto-heal, keepalive, ``cluster up``, autoscale, the
heal breaker) must ask :func:`covering` (this node, or any span it belongs to)
or :func:`paused_nodes` (every node the overlay currently holds).

TEST SEAM
---------
Every read AND every write takes ``directory=`` — the explicit-root seam
``cron_watchers.py`` uses, and for the same reason: a root threaded through as a
parameter is one root shared by both sides of a call, while a root read out of
an environment variable is per-process and is not a root a different process can
ever have written. There is deliberately NO environment override for the default
root; the default is HOME's ``~/.hscc``, resolved at import like every other
consumer in this package. Tests point ``directory=`` at a temp dir and never
touch the operator's real ``~/.hscc``.

Stdlib-only (same discipline as ``state.py`` / ``cron_watchers.py``): the CLI,
the daemon and the doctor all import this, and none of them should be able to
break it — or be broken by it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# ── The overlay file ───────────────────────────────────────────────────────
#
# One file, one root, no env override (see the TEST SEAM note in the module
# docstring). Module-level constant so the test conftest can seal the default
# root exactly as it seals every other ``~/.hscc`` consumer.
HSCC_DIR = os.path.expanduser("~/.hscc")
OVERLAY_NAME = "node_pauses.json"

#: The keys a pause entry always carries, whatever the caller passed.
ENTRY_KEYS = ("reason", "since", "until", "units", "span_nodes", "stopped")


# ── Diagnostics ────────────────────────────────────────────────────────────

def _warn(msg):
    """Route a WARN through the daemon's log convention; never raise.

    A broken logger must not turn a *readable* overlay into an exception: every
    caller of this module is a repair decision or a CLI print, and none of them
    should die because ``daemon_ops`` could not open its log. Falls back to
    stderr (never stdout — stdout is the machine channel for ``--json``).
    """
    try:
        from .daemon_ops import log
        log(msg, "WARN")
    except Exception:  # noqa: BLE001 - logging must never be the failure
        try:
            print(f"[node_pauses] WARN: {msg}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


# ── Time helpers ───────────────────────────────────────────────────────────

def _utc(value):
    """A timezone-aware UTC datetime for ``value`` (``None`` -> now).

    Accepts a datetime or an ISO-8601 string. A NAIVE datetime is read as UTC,
    the same way ``lifecycle.cleanup_old_failures`` reads the timestamps it
    wrote: the overlay is written and read on one host, and the danger being
    guarded against is a comparison that raises (or silently picks local time)
    and makes a pause look expired.
    """
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = _parse_iso(value)
        if value is None:
            return datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso(text):
    """Parse an ISO-8601 timestamp; ``None`` when it is not one.

    Handles the trailing ``Z`` form for interpreters whose ``fromisoformat``
    predates it, and returns a UTC-aware datetime either way.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _utc(parsed) if isinstance(parsed, datetime) else None


def _iso(value):
    """ISO string for a datetime/``None``; an invalid type becomes ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        parsed = _parse_iso(value)
        return parsed.isoformat() if parsed else None
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    return None


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text):
    """``"90m"``/``"2h"``/``"1d3h"``/``"45"`` -> :class:`timedelta`; else ``None``.

    The single duration grammar for ``--until`` (t_611198bf's CLI verbs reuse it
    so the CLI and the overlay cannot disagree about what ``--until 2h`` means).
    A bare number is seconds. Suffixes: s, m, h, d, w. Composed parts are added.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return timedelta(seconds=float(text))
    if not isinstance(text, str):
        return None
    raw = text.strip().lower().replace(" ", "")
    if not raw:
        return None
    if raw.isdigit():
        return timedelta(seconds=int(raw))
    total = 0.0
    i = 0
    while i < len(raw):
        j = i
        while j < len(raw) and (raw[j].isdigit() or raw[j] in "._"):
            j += 1
        if j == i or j == len(raw):
            return None  # no digits, or a number with no unit
        try:
            amount = float(raw[i:j])
        except ValueError:
            return None
        unit = _DURATION_UNITS.get(raw[j])
        if unit is None:
            return None
        total += amount * unit
        i = j + 1
    return timedelta(seconds=total)


def parse_until(value, now=None):
    """Normalise a caller's ``until`` into an ISO string (or ``None``).

    Accepts ``None`` (open-ended), a timedelta (relative to ``now``), a datetime,
    an ISO string, or a duration string such as ``"2h"``. An unparseable value
    becomes ``None`` — open-ended — never a past instant, because a malformed
    deadline that reads as "already expired" would auto-resume the node the
    operator just parked.
    """
    if value is None:
        return None
    if isinstance(value, timedelta):
        return _iso(_utc(now) + value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        span = parse_duration(value)  # a bare number is seconds
        return (_utc(now) + span).isoformat() if span is not None else None
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, str):
        direct = _parse_iso(value)
        if direct is not None:
            return direct.isoformat()
        span = parse_duration(value)
        if span is not None:
            return (_utc(now) + span).isoformat()
        _warn(f"unparseable until={value!r}; pause recorded as open-ended "
              f"(no auto-resume)")
        return None
    return None


# ── Path / normalisation ───────────────────────────────────────────────────

def overlay_path(directory=None):
    """Absolute path of the overlay for ``directory`` (default: HOME's ~/.hscc)."""
    root = HSCC_DIR if directory is None else os.path.expanduser(directory)
    return os.path.join(root, OVERLAY_NAME)


def _clean_str_list(value, host, field):
    """A list-of-strings overlay field; anything else degrades to ``[]`` + WARN."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        _warn(f"pause for {host}: {field}={value!r} is not a list; "
              f"treated as empty")
        return []
    kept = []
    for item in value:
        if isinstance(item, str) and item.strip():
            kept.append(item.strip())
        else:
            _warn(f"pause for {host}: dropping non-string {field} entry {item!r}")
    return kept


def _normalize_entry(host, raw):
    """One pause entry, coerced to the documented shape; ``None`` when unusable.

    Unknown keys are PRESERVED (dropping a field this module has not learned
    about yet would silently lose data a newer writer put there); the six
    documented keys are always present and always the right type.
    """
    if not isinstance(raw, dict):
        _warn(f"pause for {host} is {type(raw).__name__}, not an object; "
              f"ignored")
        return None
    entry = dict(raw)
    entry["reason"] = raw.get("reason") if isinstance(raw.get("reason"), str) else ""
    since = _parse_iso(raw.get("since")) if isinstance(raw.get("since"), str) else None
    if raw.get("since") is not None and since is None:
        _warn(f"pause for {host}: unparseable since={raw.get('since')!r}; "
              f"dropped")
    entry["since"] = since.isoformat() if since else None
    until = raw.get("until")
    if until is None:
        entry["until"] = None
    elif not isinstance(until, str) or _parse_iso(until) is None:
        # Fail toward "still paused": a deadline we cannot read is not a deadline
        # that has passed, so the node stays parked instead of waking up.
        _warn(f"pause for {host}: unparseable until={until!r}; treated as "
              f"open-ended (no auto-resume)")
        entry["until"] = None
    else:
        entry["until"] = until
    entry["units"] = _clean_str_list(raw.get("units"), host, "units")
    entry["span_nodes"] = _clean_str_list(raw.get("span_nodes"), host,
                                          "span_nodes")
    entry["stopped"] = bool(raw.get("stopped"))
    return entry


def _normalize(data, source):
    """The whole overlay document -> ``{"pauses": {host: entry}}``.

    Never raises: a missing, unreadable, non-JSON, non-object or non-object
    ``pauses`` document all yield "no pauses" (a bad overlay must not take the
    fleet's repair paths down with it) but each one is WARNED about, because a
    pause the operator believes is in force and cannot be read is exactly the
    situation they must find out about.
    """
    if data is None:
        return {"pauses": {}}
    if not isinstance(data, dict):
        _warn(f"{source}: expected a JSON object, got "
              f"{type(data).__name__}; treating as no pauses")
        return {"pauses": {}}
    raw_pauses = data.get("pauses")
    if raw_pauses is None:
        return {"pauses": {}}
    if not isinstance(raw_pauses, dict):
        _warn(f"{source}: 'pauses' is {type(raw_pauses).__name__}, not an "
              f"object; treating as no pauses")
        return {"pauses": {}}
    pauses = {}
    for host, raw_entry in raw_pauses.items():
        if not isinstance(host, str) or not host.strip():
            _warn(f"{source}: dropping pause with non-string host key {host!r}")
            continue
        entry = _normalize_entry(host, raw_entry)
        if entry is not None:
            pauses[host.strip()] = entry
    return {"pauses": pauses}


# ── Read / write ───────────────────────────────────────────────────────────

def load(directory=None):
    """The overlay document, always normalised, never raising.

    Missing or unreadable file -> ``{"pauses": {}}`` (fail-open on ABSENCE: no
    overlay is genuinely "nothing is paused"). Present-but-malformed -> also no
    pauses, but loudly WARNed (fail-loud on CORRUPTION: silence here would mean
    a parked node gets healed with nobody the wiser).
    """
    path = overlay_path(directory)
    if not os.path.exists(path):
        return {"pauses": {}}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        # ValueError covers json.JSONDecodeError; a truncated or half-written
        # file must not raise into a repair decision.
        _warn(f"{path} is unreadable ({type(e).__name__}: {e}); treating as no "
              f"pauses")
        return {"pauses": {}}
    return _normalize(data, path)


def save(data, directory=None):
    """Write the overlay atomically: tmp file in the SAME dir, fsync, replace.

    Same-directory is what makes ``os.replace`` atomic on the filesystem (a
    cross-device replace would fall back to a non-atomic copy — the failure the
    tmp+replace idiom exists to avoid). A reader therefore sees either the whole
    previous document or the whole new one, never a half-written overlay.

    Unlike the other ``~/.hscc`` state files (``heal_breaker._save`` swallows
    ``OSError``) this one PROPAGATES write failures, and callers must treat them
    as fatal to the operation: for a pause, a silently lost write is not a lost
    detail, it is a node that walks back into service under the operator.
    Returns the path written.
    """
    path = overlay_path(directory)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    payload = data if isinstance(data, dict) else _normalize(data, path)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{OVERLAY_NAME}.",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray tmp behind for the next reader to trip over.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ── The pause API ──────────────────────────────────────────────────────────

def _mutate(host, build, directory=None, now=None):
    """Read-modify-write one host. ``build(existing|None, now)`` -> entry|None.

    Re-reads before writing so a pause added by another process (the CLI while
    the daemon ticks) is not clobbered. There is no cross-process lock: two
    writers racing on the SAME host are last-writer-wins on that host, and the
    overlay is small enough that the practical risk is a lost reason string, not
    a lost pause. A caller that needs stop-the-units + record to be one atomic
    act belongs in the exec layer, which records only after the stop succeeded.
    """
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    host = host.strip()
    data = load(directory)
    stamp = _utc(now)
    entry = build(data["pauses"].get(host), stamp)
    if entry is None:
        data["pauses"].pop(host, None)
    else:
        data["pauses"][host] = entry
    save(data, directory)
    return entry


def pause(host, reason="", until=None, units=None, span_nodes=None,
          stopped=False, directory=None, now=None):
    """Record a durable pause for ``host``; return the stored entry.

    ``until``      — None (open-ended), a duration string (``"2h"``), an ISO
                     timestamp, a datetime or a timedelta (relative to ``now``).
    ``units``      — the unit ids stopped by this pause (recorded for resume).
    ``span_nodes`` — every node the stopped span covers, including ``host``.
    ``stopped``    — whether the units really are down; the CLI records the
                     overlay AFTER the sparkrun stop, so ``False`` means "pause
                     is in force, units not confirmed down yet".

    Re-pausing an already-paused host REFRESHES the record and keeps the earlier
    ``since`` — the node has been parked since the first pause, and a second
    ``pause`` must not reset the clock the operator reads.

    Raises ``OSError`` if the overlay cannot be persisted (see :func:`save`) —
    a pause that did not reach the disk did not happen.
    """
    def build(existing, stamp):
        entry = {
            "reason": reason if isinstance(reason, str) else "",
            "since": (existing or {}).get("since") or _iso(stamp),
            "until": parse_until(until, stamp),
            "units": _clean_str_list(units, host, "units"),
            "span_nodes": _clean_str_list(span_nodes, host, "span_nodes"),
            "stopped": bool(stopped),
        }
        return entry

    return _mutate(host, build, directory=directory, now=now)


def clear(host, directory=None):
    """Drop the pause for ``host``; return the removed entry, or ``None``.

    The only way a pause ends besides an expiry the daemon acted on. Callers that
    also clear heal strikes or relaunch units do it themselves — this module only
    owns the overlay. A host that is not paused returns ``None`` WITHOUT rewriting
    the file, so a resume retry cannot churn the overlay.
    """
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be a non-empty string")
    data = load(directory)
    removed = data["pauses"].pop(host.strip(), None)
    if removed is None:
        return None
    save(data, directory)
    return removed


def paused(host, directory=None):
    """The entry holding ``host``, or ``None`` — clock-independent.

    Truth-accurate: it reports the record, including one whose ``until`` has
    already passed (the resume has not run yet, so the node is still promised to
    the operator). Only :func:`expiry_pass` / :func:`clear` end a pause. Does NOT
    follow ``span_nodes`` — use :func:`covering` or :func:`paused_nodes` in a
    repair path.
    """
    if not isinstance(host, str) or not host.strip():
        return None
    return load(directory)["pauses"].get(host.strip())


def list_pauses(directory=None):
    """``{host: entry}`` for every recorded pause (host-keyed, like the file)."""
    return load(directory)["pauses"]


def covering(host, directory=None):
    """The entry that holds ``host`` — keyed to it, or spanning it.

    What a skip seam must ask. A tensor-parallel unit across three hosts stops as
    one thing, so the two hosts named only in ``span_nodes`` are just as parked as
    the keyed host; a watchdog that asked :func:`paused` alone would relaunch them.
    """
    if not isinstance(host, str) or not host.strip():
        return None
    host = host.strip()
    pauses = load(directory)
    entry = pauses["pauses"].get(host)
    if entry is not None:
        return entry
    for _host, candidate in pauses["pauses"].items():
        if host in candidate["span_nodes"]:
            return candidate
    return None


def paused_nodes(directory=None):
    """Every node the overlay currently holds: keyed hosts ∪ their span nodes.

    The one-call form for a skip seam that is about to loop over the fleet.
    """
    out = set()
    for host, entry in list_pauses(directory).items():
        out.add(host)
        out.update(entry["span_nodes"])
    return out


def is_expired(entry, now=None):
    """True when ``entry`` has a readable ``until`` at or before ``now``."""
    if not isinstance(entry, dict):
        return False
    until = _parse_iso(entry.get("until"))
    return until is not None and until <= _utc(now)


def pending_resume(now=None, directory=None):
    """``[(host, entry)]`` whose ``until`` has passed — the daemon's to-do list.

    Reports only; changes nothing. A pause with no ``until`` never appears (it is
    the operator's to end). Sorted by ``until`` ascending, then host, so a tick
    resumes the longest-waiting node first and the order is reproducible.
    """
    stamp = _utc(now)
    found = []
    for host, entry in list_pauses(directory).items():
        until = _parse_iso(entry.get("until"))
        if until is not None and until <= stamp:
            found.append((until, host, entry))
    found.sort(key=lambda row: (row[0], row[1]))
    return [(host, entry) for _until, host, entry in found]


def expiry_pass(now=None, directory=None):
    """Drop every expired pause from the overlay; return ``[(host, entry)]``.

    This is the ONLY place the clock ends a pause, and it is deliberately not
    called by :func:`paused`: the daemon tick resumes the expired units (exec
    layer) and only then runs this, so the overlay never claims a node is free
    before the relaunch has been issued. Returns the same shape as
    :func:`pending_resume`; saves only when something expired, so a tick with
    nothing to do does not rewrite the file.
    """
    expired = pending_resume(now, directory)
    if not expired:
        return []
    data = load(directory)
    for host, _entry in expired:
        data["pauses"].pop(host, None)
    save(data, directory)
    return expired
