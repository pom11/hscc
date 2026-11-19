"""Daemon-owned wake-ups for the main orchestrator (M5).

The 2026-11-19 design (``docs/design/2026-11-19-main-orchestrator-design.md``)
gives the cluster ONE main orchestrator that wakes itself on a schedule. The
hscc daemon owns that schedule — not an in-pane cron, not an external timer —
so a restart can neither double-fire a wake-up nor silently forget one.

Four wake-ups ship by default:

==========  ======================  =====================================
name        default schedule        prompt duty (SOUL)
==========  ======================  =====================================
``health``  every 2h                nodes/serving/proxy/daemon/autodown/caps
``sweep``   every 3h                every orchestrator's board + pane
``memory``  daily at 07:00 local    per-project state note
``settings`` weekly Monday 07:30    own skills/config review
==========  ======================  =====================================

Delivery uses the SAME path as ``hscc orch inject``: the prompt is handed to
``repl_inject.inject(profile, text, mode="auto")``, so a busy orchestrator gets
a queued message (never an interrupted turn) and a missing pane is skipped
instead of guessed. The four prompt texts live in :data:`PROMPTS` and are
short on purpose — each names the SOUL duty it serves and asks for live state,
not a memory recital.

Files (both written atomically, both re-read on every tick, so a CLI edit
takes effect without a daemon restart):

* ``~/.hscc/main_wakeups.json`` — operator config: per-wake-up ``enabled`` and
  ``schedule``. Written only through :func:`set_schedule` / :func:`set_enabled`
  (the same writer the CLI calls), preserving unrelated operator keys.
* ``~/.hscc/state/main_wakeups.json`` — daemon state: ``seeded_at``,
  ``last_fired``, ``last_result`` and the stream stamp (``last_tick``). This is
  what makes a restart idempotent: due-ness is always computed from the
  persisted timestamps, never from an in-memory debounce.

Schedule grammar (local time for daily/weekly):

* ``every <N>m`` / ``every <N>h`` — interval from the last fire
* ``daily HH:MM``
* ``weekly <weekday> HH:MM``

The module is pure around injected seams: ``now`` and ``inject`` are arguments,
so every schedule/restart/busy/missing-pane test runs offline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

LOG = logging.getLogger(__name__)

#: Stable wake-up names (also the CLI argument vocabulary). Order is the order
#: the CLI lists and the daemon evaluates them.
NAMES = ("health", "sweep", "memory", "settings")

#: Path TEMPLATES resolved via ``os.path.expanduser`` AT CALL TIME, matching
#: ``orch_waker`` / ``update_check`` — the suite's autouse fixture redirects
#: ``~/.hscc`` at runtime, so baking expanduser in here would defeat it.
DEFAULT_CONFIG_PATH = "~/.hscc/main_wakeups.json"
DEFAULT_STATE_PATH = "~/.hscc/state/main_wakeups.json"

#: The daemon stream name. The state file above is the stream's stamp file, so
#: ``hscc verify``'s per-stream staleness math applies for free.
STREAM_NAME = "main_wakeups"

#: Doctor row name (the verdict lives here, with the feature, so the daemon,
#: the CLI and doctor cannot disagree).
CHECK_NAME = "main wakeups (daemon-owned schedule)"

#: The four defaults the design asks for. ``enabled`` starts true for all four.
DEFAULT_WAKEUPS = {
    "health": {"enabled": True, "schedule": "every 2h"},
    "sweep": {"enabled": True, "schedule": "every 3h"},
    "memory": {"enabled": True, "schedule": "daily 07:00"},
    "settings": {"enabled": True, "schedule": "weekly Monday 07:30"},
}

#: The four prompt texts. English, short, self-contained, and each one names
#: the SOUL duty it exercises (design goal 7 + the operator's short-answer
#: rule). They are constants, not config: the wake-up name owns its prompt.
PROMPTS = {
    "health": (
        "Cluster health check (SOUL duty: health). Check nodes, serving, "
        "proxy errors, the daemon, autodown state and kanban caps. Fix what "
        "you can; report only what needs the operator."),
    "sweep": (
        "Orchestrator sweep (SOUL duty: always watching). Open every "
        "orchestrator's board and pane, check each standing watch card, and "
        "steer stuck, failing or duplicate work."),
    "memory": (
        "Project memory refresh (SOUL duty: knows everything). Refresh your "
        "per-project state note from live boards, panes and PRs; keep it "
        "short and current."),
    "settings": (
        "Settings and skills review (SOUL duty: own settings). Review your "
        "skills, config and these wake-ups; fix drift and keep them current."),
}

#: Weekday names accepted by ``weekly`` (Monday = 0, matching datetime).
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WEEKDAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                   "Saturday", "Sunday")

_INTERVAL_RE = re.compile(
    r"^every\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$",
    re.IGNORECASE)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)
_WEEKLY_RE = re.compile(
    r"^weekly\s+([a-z]+)\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


class WakeupError(ValueError):
    """An unknown wake-up name or an unparseable schedule."""


def _resolve(path) -> str:
    return os.path.expanduser(str(path))


def _real_time() -> float:
    import time
    return time.time()


def _now_iso() -> str:
    """ISO stamp with the local offset (verify parses either aware form)."""
    return datetime.now().astimezone().isoformat()


# --------------------------------------------------------------------------- #
# Schedule grammar + math
# --------------------------------------------------------------------------- #

def _clock(hour: int, minute: int) -> tuple:
    if hour > 23 or minute > 59:
        raise WakeupError(
            f"invalid time {hour:02d}:{minute:02d} (want 00:00-23:59)")
    return hour, minute


def parse_schedule(text) -> dict:
    """Parse a schedule string into its canonical form.

    Returns ``{"kind": "interval"|"daily"|"weekly", ...}`` plus the canonical
    ``"text"``. Raises :class:`WakeupError` with the accepted grammar — the CLI
    reports it verbatim and never stores an unparseable value.
    """
    if not isinstance(text, str) or not text.strip():
        raise WakeupError(
            "empty schedule (want 'every 2h', 'daily 07:00' or "
            "'weekly Monday 07:30')")
    raw = " ".join(text.strip().split())

    match = _INTERVAL_RE.match(raw)
    if match:
        amount = int(match.group(1))
        if amount <= 0:
            raise WakeupError("interval must be greater than zero")
        unit = match.group(2).lower()
        if unit.startswith("h"):
            return {"kind": "interval", "seconds": amount * 3600,
                    "text": f"every {amount}h"}
        return {"kind": "interval", "seconds": amount * 60,
                "text": f"every {amount}m"}

    match = _DAILY_RE.match(raw)
    if match:
        hour, minute = _clock(int(match.group(1)), int(match.group(2)))
        return {"kind": "daily", "hour": hour, "minute": minute,
                "text": f"daily {hour:02d}:{minute:02d}"}

    match = _WEEKLY_RE.match(raw)
    if match:
        day = _WEEKDAYS.get(match.group(1).lower())
        if day is None:
            raise WakeupError(
                f"unknown weekday {match.group(1)!r} (want Monday..Sunday)")
        hour, minute = _clock(int(match.group(2)), int(match.group(3)))
        return {"kind": "weekly", "weekday": day, "hour": hour,
                "minute": minute,
                "text": f"weekly {_WEEKDAY_LABELS[day]} {hour:02d}:{minute:02d}"}

    raise WakeupError(
        f"unrecognised schedule {raw!r} (want 'every 2h', 'daily 07:00' or "
        "'weekly Monday 07:30')")


def normalize_schedule(text) -> str:
    """The canonical schedule string, or :class:`WakeupError`."""
    return parse_schedule(text)["text"]


def next_fire(schedule, after) -> float:
    """Epoch seconds of the first occurrence strictly after ``after``.

    Interval schedules count from ``after`` itself; daily/weekly use LOCAL
    wall-clock time (``datetime.fromtimestamp``), which is what "daily 07:00
    local" and "weekly Monday 07:30" mean to the operator.
    """
    spec = schedule if isinstance(schedule, dict) else parse_schedule(schedule)
    after = float(after)
    if spec["kind"] == "interval":
        return after + spec["seconds"]

    base = datetime.fromtimestamp(after)
    candidate = base.replace(hour=spec["hour"], minute=spec["minute"],
                             second=0, microsecond=0)
    if spec["kind"] == "daily":
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate.timestamp()

    ahead = (spec["weekday"] - base.weekday()) % 7
    candidate += timedelta(days=ahead)
    if candidate <= base:
        candidate += timedelta(days=7)
    return candidate.timestamp()


def is_due(schedule, last_fired, now) -> bool:
    """Has the first occurrence after ``last_fired`` arrived by ``now``?"""
    try:
        last = float(last_fired)
    except (TypeError, ValueError):
        return True
    return float(now) >= next_fire(schedule, last)


# --------------------------------------------------------------------------- #
# Config (operator-owned, written through this module only)
# --------------------------------------------------------------------------- #

def _hscc_root(hscc_dir=None) -> str:
    """The ``~/.hscc`` root, honouring ``hscc_dir`` then ``HSCC_DIR``.

    Same rule as ``main_orch.hscc_root``: an explicit path wins, then the
    environment (sandboxed bootstrap / tests), then the operator's live root.
    """
    return os.path.expanduser(hscc_dir or os.environ.get("HSCC_DIR")
                              or "~/.hscc")


def config_path(path=None, hscc_dir=None) -> str:
    if path:
        return _resolve(path)
    if hscc_dir or os.environ.get("HSCC_DIR"):
        return os.path.join(_hscc_root(hscc_dir),
                            os.path.basename(DEFAULT_CONFIG_PATH))
    return _resolve(DEFAULT_CONFIG_PATH)


def state_path(path=None, hscc_dir=None) -> str:
    if path:
        return _resolve(path)
    if hscc_dir or os.environ.get("HSCC_DIR"):
        return os.path.join(_hscc_root(hscc_dir), "state",
                            os.path.basename(DEFAULT_STATE_PATH))
    return _resolve(DEFAULT_STATE_PATH)


def _default_config() -> dict:
    return {"wakeups": {name: dict(spec)
                        for name, spec in DEFAULT_WAKEUPS.items()}}


def _read_doc(path) -> dict:
    """Read a JSON object; absent/unreadable -> ``{}`` (never raises)."""
    file = config_path(path)
    try:
        with open(file, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        LOG.warning("main-wakeups: config %s unreadable (%s) — using defaults",
                    file, e)
        return {}
    if not isinstance(doc, dict):
        LOG.warning("main-wakeups: config %s is not an object — using defaults",
                    file)
        return {}
    return doc


def load_config(path=None) -> dict:
    """The complete config merged over defaults (a corrupt file => defaults).

    Only known wake-up names are returned; a hand-edited unknown name is
    ignored rather than trusted. A bad per-entry schedule falls back to that
    wake-up's default, so a typo can never stop the others from firing.
    """
    cfg = _default_config()
    doc = _read_doc(path)
    raw = doc.get("wakeups")
    if not isinstance(raw, dict):
        return cfg
    for name in NAMES:
        entry = raw.get(name)
        if not isinstance(entry, dict):
            continue
        default = DEFAULT_WAKEUPS[name]
        cfg["wakeups"][name]["enabled"] = bool(
            entry.get("enabled", default["enabled"]))
        try:
            cfg["wakeups"][name]["schedule"] = normalize_schedule(
                entry.get("schedule", default["schedule"]))
        except WakeupError:
            pass
    return cfg


def _write_doc(doc: dict, path=None) -> None:
    file = config_path(path)
    parent = os.path.dirname(file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{file}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, file)


def _update(name, path=None, **fields) -> dict:
    """Merge ``fields`` into one wake-up entry, preserving operator keys."""
    if name not in DEFAULT_WAKEUPS:
        raise WakeupError(
            f"unknown wake-up {name!r} (want one of {', '.join(NAMES)})")
    file = config_path(path)
    doc = {}
    if os.path.exists(file):
        with open(file, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError(
                f"{file} is not a JSON object — refusing to rewrite it")
        doc = loaded
    raw = doc.get("wakeups")
    wakeups = raw if isinstance(raw, dict) else {}
    entry = wakeups.get(name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(fields)
    wakeups[name] = entry
    # Write the COMPLETE schema so a fresh file is usable and an older partial
    # file is upgraded in place; unrelated top-level keys stay untouched.
    for other in NAMES:
        existing = wakeups.get(other)
        merged = dict(DEFAULT_WAKEUPS[other])
        if isinstance(existing, dict):
            merged.update(existing)
            merged["enabled"] = bool(
                existing.get("enabled", DEFAULT_WAKEUPS[other]["enabled"]))
            try:
                merged["schedule"] = normalize_schedule(
                    existing.get("schedule", DEFAULT_WAKEUPS[other]["schedule"]))
            except WakeupError:
                merged["schedule"] = DEFAULT_WAKEUPS[other]["schedule"]
        wakeups[other] = merged
    doc["wakeups"] = wakeups
    _write_doc(doc, path)
    return {"wakeups": {n: dict(wakeups[n]) for n in NAMES}}


def set_schedule(name, schedule, path=None) -> dict:
    """Set one wake-up's schedule (canonicalised) and return the new block."""
    canonical = normalize_schedule(schedule)
    cfg = load_config(path)
    previous = cfg["wakeups"][name]["schedule"] if name in NAMES else None
    block = _update(name, path, schedule=canonical)
    return {"ok": True, "action": "set", "name": name,
            "schedule": canonical, "previous": previous, **block}


def set_enabled(name, enabled, path=None) -> dict:
    """Enable/disable one wake-up and return the new block."""
    block = _update(name, path, enabled=bool(enabled))
    return {"ok": True, "action": "enable" if enabled else "disable",
            "name": name, "enabled": bool(enabled), **block}


# --------------------------------------------------------------------------- #
# State (daemon-owned: last fired + last result, the restart memory)
# --------------------------------------------------------------------------- #

def load_state(path=None) -> dict:
    """Read the daemon state document; unreadable/corrupt -> empty memory."""
    file = state_path(path)
    try:
        with open(file, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        LOG.warning("main-wakeups: state %s unreadable (%s) — starting empty",
                    file, e)
        return {}
    if not isinstance(doc, dict):
        return {}
    for key in ("last_fired", "last_result"):
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
    for key in ("seeded_at", "last_tick"):
        if isinstance(doc.get(key), bool) or not isinstance(
                doc.get(key), (int, float)):
            doc.pop(key, None)
    return doc


def _save_state(doc: dict, path=None) -> None:
    file = state_path(path)
    parent = os.path.dirname(file)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{file}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, file)


# --------------------------------------------------------------------------- #
# Identity + delivery seams
# --------------------------------------------------------------------------- #

def _load_main_orch():
    """Import ``hscc-roles/main_orch.py`` (the one identity resolver)."""
    roles_dir = Path(__file__).resolve().parent.parent / "hscc-roles"
    if str(roles_dir) not in sys.path:
        sys.path.insert(0, str(roles_dir))
    import main_orch  # noqa: PLC0415 — sibling plugin, path added above
    return main_orch


def _main_profile(hscc_dir=None) -> str:
    try:
        return _load_main_orch().main_profile(hscc_dir)
    except Exception as exc:  # noqa: BLE001 — default identity still works
        LOG.warning("main-wakeups: main-orchestrator resolver unavailable "
                    "(%s) — using 'general'", exc)
        return "general"


def load_repl_inject():
    """Import ``repl_inject`` from the sibling hscc-api plugin dir, or None."""
    try:
        import repl_inject  # noqa: F401  (already importable)
        return repl_inject
    except ImportError:
        pass
    api_dir = Path(__file__).resolve().parent.parent / "hscc-api"
    if not api_dir.is_dir():
        return None
    if str(api_dir) not in sys.path:
        sys.path.insert(0, str(api_dir))
    try:
        import repl_inject  # noqa: F401
        return repl_inject
    except ImportError:
        return None


def _make_inject(profile, repl=None):
    """The ``auto`` delivery callable used by the daemon tick.

    ``mode="auto"`` is the contract: ``repl_inject`` sends ``/queue add`` while
    the orchestrator is busy and a plain message when idle, so a wake-up never
    interrupts a running turn. ``repl`` is injectable for tests.
    """
    module = repl if repl is not None else load_repl_inject()
    if module is None:
        raise RuntimeError(
            "repl_inject not importable (hscc-api plugin missing)")

    def inject(_profile, text, mode="auto"):
        return module.inject(profile, text, mode=mode)
    return inject


def pane_status(profile=None, hscc_dir=None, repl=None) -> dict:
    """Read-only pane verdict: running / missing / ambiguous / unknown."""
    profile = profile or _main_profile(hscc_dir)
    module = repl if repl is not None else load_repl_inject()
    if module is None:
        return {"state": "unknown", "profile": profile,
                "detail": "repl_inject not importable"}
    try:
        panes = [p for p in module.hermes_panes()
                 if p.get("profile") == profile]
    except Exception as exc:  # noqa: BLE001 — honest unknown, never a guess
        return {"state": "unknown", "profile": profile, "detail": str(exc)}
    if not panes:
        return {"state": "missing", "profile": profile, "panes": []}
    if len(panes) > 1:
        return {"state": "ambiguous", "profile": profile,
                "panes": [p.get("target") for p in panes]}
    return {"state": "running", "profile": profile,
            "pane": panes[0].get("pane"), "target": panes[0].get("target")}


def _attempt(name, profile, inject, now) -> dict:
    """Deliver one prompt; never raise. The result is what state persists."""
    try:
        receipt = inject(profile, PROMPTS[name], mode="auto")
    except Exception as exc:  # noqa: BLE001 — a wake-up never kills the daemon
        kind = getattr(exc, "kind", "")
        reason = "pane-missing" if kind in ("no_panes", "ambiguous") \
            else "delivery-failed"
        detail = " ".join(str(exc or reason).split())[:300]
        LOG.warning("main-wakeups: skipped %s — %s (%s)",
                    name, reason, detail)
        return {"ts": now, "ok": False, "skipped": reason, "detail": detail}
    receipt = receipt if isinstance(receipt, dict) else {}
    mode = receipt.get("mode")
    LOG.info("main-wakeups: fired %s -> %s (%s)", name, profile,
             mode or "sent")
    return {"ts": now, "ok": True, "skipped": None,
            "mode": mode, "busy": receipt.get("busy"), "detail": "sent"}


# --------------------------------------------------------------------------- #
# The daemon tick
# --------------------------------------------------------------------------- #

def tick(now=None, config_path=None, state_path=None, inject=None,
         profile=None, hscc_dir=None, repl=None) -> dict:
    """One daemon pass: seed on first sight, fire what is due, persist.

    Never raises. A missing pane is a SKIP (logged and stamped in
    ``last_result``) and still advances the schedule, so a dead pane cannot
    produce a retry storm every 30 s; ``hscc doctor`` reads the stamp and says
    the pane is missing. A successful fire records the resolved mode (``queue``
    when the orchestrator was busy).
    """
    now = _real_time() if now is None else float(now)
    if config_path is None:
        config_path = os.path.join(_hscc_root(hscc_dir), "main_wakeups.json")
    if state_path is None:
        state_path = os.path.join(_hscc_root(hscc_dir), "state",
                                  "main_wakeups.json")
    st = load_state(state_path)
    last_fired = st.setdefault("last_fired", {})
    last_result = st.setdefault("last_result", {})
    entry = {"ok": True, "last_tick": now, "fired": [], "skipped": [],
             "seeded": []}
    try:
        cfg = load_config(config_path)
        if profile is None:
            profile = _main_profile(hscc_dir)
        if inject is None:
            inject = _make_inject(profile, repl=repl)

        # First sight: start every clock now instead of dumping four prompts
        # into a freshly bootstrapped pane. ``seeded_at`` makes that a
        # ONE-TIME event (it is persisted with the last-fired map).
        if not st.get("seeded_at"):
            st["seeded_at"] = now
        for name in NAMES:
            if name not in last_fired:
                last_fired[name] = now
                entry["seeded"].append(name)

        for name in NAMES:
            spec = cfg["wakeups"][name]
            if not spec["enabled"]:
                continue
            if not is_due(spec["schedule"], last_fired[name], now):
                continue
            result = _attempt(name, profile, inject, now)
            # advance even on a skip: the occurrence was handled, and the next
            # one is computed from here (no 30 s retry storm on a dead pane)
            last_fired[name] = now
            last_result[name] = result
            if result["ok"]:
                entry["fired"].append(name)
            else:
                entry["skipped"].append({
                    "name": name, "reason": result["skipped"],
                    "detail": result["detail"]})

        entry["last_fired"] = dict(last_fired)
        entry["last_result"] = dict(last_result)
        entry["profile"] = profile
        entry["message"] = _tick_message(entry["fired"], entry["skipped"],
                                         entry["seeded"])
    except Exception as exc:  # noqa: BLE001 — the stream must survive anything
        LOG.exception("main-wakeups: tick failed")
        entry.update(ok=False, message=f"tick failed: {exc}")

    st.update(entry)
    st["stream"] = STREAM_NAME
    st["timestamp"] = _now_iso()
    try:
        _save_state(st, state_path)
    except Exception:  # pragma: no cover — the daemon must not die here
        LOG.exception("main-wakeups: state write failed")
    return entry


def _tick_message(fired, skipped, seeded) -> str:
    if fired:
        return f"fired {', '.join(fired)}"
    if skipped:
        return "skipped " + ", ".join(
            f"{s['name']} ({s['reason']})" for s in skipped)
    if seeded:
        return f"seeded {len(seeded)} wake-up(s)"
    return "no wake-up due"


def check_main_wakeups() -> bool:
    """The daemon's ``main_wakeups`` stream check (PERIODIC_INTERVALS body)."""
    try:
        return bool(tick().get("ok"))
    except Exception as exc:  # pragma: no cover — tick swallows internally
        LOG.exception("main-wakeups: stream check raised")
        try:
            from .daemon_ops import log
            log(f"main-wakeups check error: {exc}", "ERROR")
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------- #
# Receipts for the CLI + the doctor row
# --------------------------------------------------------------------------- #

def _last_fired_ts(st, name):
    value = (st.get("last_fired") or {}).get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def list_receipt(now=None, config_path=None, state_path=None,
                 hscc_dir=None) -> dict:
    """Everything ``hscc main wakeups list [--json]`` prints."""
    now = _real_time() if now is None else float(now)
    if config_path is None:
        config_path = os.path.join(_hscc_root(hscc_dir), "main_wakeups.json")
    if state_path is None:
        state_path = os.path.join(_hscc_root(hscc_dir), "state",
                                  "main_wakeups.json")
    cfg = load_config(config_path)
    st = load_state(state_path)
    profile = _main_profile(hscc_dir)
    items = []
    for name in NAMES:
        spec = cfg["wakeups"][name]
        last = _last_fired_ts(st, name)
        result = (st.get("last_result") or {}).get(name)
        items.append({
            "name": name,
            "enabled": spec["enabled"],
            "schedule": spec["schedule"],
            "last_fired": last,
            "last_result": result if isinstance(result, dict) else None,
            "next_due": next_fire(spec["schedule"], last) if last is not None
            else None,
            "prompt": PROMPTS[name],
        })
    return {"ok": True, "profile": profile, "now": now,
            "enabled_count": sum(1 for i in items if i["enabled"]),
            "seed_at": st.get("seeded_at"), "last_tick": st.get("last_tick"),
            "wakeups": items}


def _daemon_running(hscc_dir=None) -> bool:
    root = os.path.expanduser(hscc_dir or os.environ.get("HSCC_DIR")
                              or "~/.hscc")
    try:
        with open(os.path.join(root, "daemon.pid"), encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def doctor_check(now=None, config_path=None, state_path=None, hscc_dir=None,
                 pane_check=None, daemon_running=None) -> dict:
    """``hscc doctor``'s wake-up verdict (five Check fields as a dict).

    The failure that matters is a missing main-orchestrator pane: wake-ups are
    skipped, so the row says so with the one-line fix. Everything else is a
    warning (``fatal`` is always False — wake-ups are drift to steer, never a
    reason to abort an install).
    """
    now = _real_time() if now is None else float(now)
    if config_path is None:
        config_path = os.path.join(_hscc_root(hscc_dir), "main_wakeups.json")
    if state_path is None:
        state_path = os.path.join(_hscc_root(hscc_dir), "state",
                                  "main_wakeups.json")
    try:
        cfg = load_config(config_path)
        st = load_state(state_path)
    except Exception as exc:  # noqa: BLE001
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": f"cannot read main wake-up state: {exc}",
                "fix": "Re-run `hscc main wakeups list` and check ~/.hscc."}
    enabled = [name for name in NAMES if cfg["wakeups"][name]["enabled"]]
    if not enabled:
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": "all wake-ups disabled", "fix": ""}

    profile = _main_profile(hscc_dir)
    try:
        pane = pane_check() if pane_check is not None else pane_status(
            profile=profile, hscc_dir=hscc_dir)
    except Exception as exc:  # noqa: BLE001 — an honest unknown
        pane = {"state": "unknown", "detail": str(exc)}
    state = pane.get("state")
    if state == "missing":
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": (f"main orchestrator pane missing (profile "
                           f"{profile!r}) — wake-ups will be skipped"),
                "fix": "hscc main start"}
    if state == "ambiguous":
        return {"name": CHECK_NAME, "ok": False, "fatal": False,
                "detail": (f"{len(pane.get('panes') or [])} panes run profile "
                           f"{profile!r} — wake-ups cannot pick one"),
                "fix": "close the extra panes, then `hscc main status`"}

    results = []
    for name, result in (st.get("last_result") or {}).items():
        if name in NAMES and isinstance(result, dict):
            try:
                ts = float(result.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            results.append((ts, name, result))
    if results:
        _ts, name, latest = max(results, key=lambda row: row[0])
        if latest.get("skipped") == "pane-missing":
            return {"name": CHECK_NAME, "ok": False, "fatal": False,
                    "detail": (f"{name} wake-up skipped: pane missing "
                               f"({latest.get('detail') or ''})".strip()),
                    "fix": "hscc main start"}
        if latest.get("skipped"):
            return {"name": CHECK_NAME, "ok": False, "fatal": False,
                    "detail": (f"{name} wake-up skipped: "
                               f"{latest.get('skipped')} "
                               f"({latest.get('detail') or ''})".strip()),
                    "fix": "check the daemon log, then `hscc main wakeups list`"}

    if st.get("last_tick") is None:
        running = (daemon_running() if daemon_running is not None
                   else _daemon_running(hscc_dir))
        if running:
            return {"name": CHECK_NAME, "ok": False, "fatal": False,
                    "detail": (f"{len(enabled)} wake-ups enabled but the "
                               "daemon has not ticked them yet"),
                    "fix": "hscc daemon restart"}

    last_bits = []
    for name in enabled:
        last = _last_fired_ts(st, name)
        if last is None:
            last_bits.append(f"{name} never")
            continue
        age = max(0.0, now - last)
        last_bits.append(f"{name} {_age_text(age)} ago")
    schedules = ", ".join(f"{name} {cfg['wakeups'][name]['schedule']}"
                          for name in enabled)
    return {"name": CHECK_NAME, "ok": True, "fatal": False,
            "detail": (f"{len(enabled)} enabled ({schedules}); last fired: "
                       f"{', '.join(last_bits)}"),
            "fix": ""}


def _age_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
