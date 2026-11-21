"""Doctor visibility for the node-pause overlay (feature 2.17.3, card t_ba1fec3e).

WHY THIS ROW EXISTS
-------------------
``hscc node pause`` parks ONE node on purpose: it stops the node's units through
sparkrun and records the intent in ``~/.hscc/node_pauses.json``
(hscc_daemon/node_pauses.py, #560). Every repair path then skips that node
(watchdog auto-heal #565, keepalive, ``cluster up``, autoscale #564, the heal
breaker #568, autodown #566) and the fleet views read it ``paused`` (the API
projection #569, ``hscc cluster status`` / ``monitor`` t_480cee2d) — but the
preflight doctor said nothing at all. An operator running ``hscc doctor`` to ask
"why is this machine not serving" got no answer here, and a reviewer reading
doctor's output could not tell a machine that was parked from one that was
broken. This module is the doctor half of that same overlay.

AN ACTIVE PAUSE IS INFO, NEVER A FAULT
--------------------------------------
A pause is an operator decision, so the row is an info row — ``ok=True``,
``fatal=False`` — naming host, reason, since and until. It must not make the
preflight red: ``hscc node pause`` on a healthy fleet followed by ``hscc
doctor`` is the normal case, and a doctor that calls the operator's own action a
failure trains them to ignore the row (and, being Stage 1 of bootstrap, a FATAL
here would hard-stop an install over a parked worker). The row goes non-ok for
exactly one reason: the file it is reading cannot be trusted.

THE ONE CASE THAT MUST BE LOUD
------------------------------
:func:`hscc_daemon.node_pauses.load` never raises. A truncated, non-JSON,
non-object or entry-shaped-wrong overlay answers "no pauses" and reports the
problem only through its own ``_warn()`` — fail-open on *absence*, fail-loud on
*corruption*, where the fail-open half is what ``heal_breaker`` / ``autoscale``
/ ``alerts`` deliberately inherit because a repair path must not stop healing
over a bad file. For a REPORT that asymmetry is the dangerous half: an operator
who parked a node and then corrupted the overlay would read "no node pauses"
from doctor while the fleet healed their node back to life under them. So a
malformed overlay is a WARN row (``ok=False``, ``fatal=False`` — a warning the
renderer prints as ○, never the ✗ of a fatal check) naming the file and quoting
what the reader could not use.

WHY THE WARNINGS ARE CAPTURED AND NOT RE-PARSED
-----------------------------------------------
``load()`` gives a caller no way to tell corruption from absence, so the doctor
either re-implements the overlay's validation (a second parser that drifts from
the first — the exact failure mode the overlay's own docstring warns about) or
listens to the diagnostics ``load()`` already produces. This module does the
latter: ``node_pauses._warn`` is swapped for a collector for the duration of one
read and restored in a ``finally``. The doctor's idea of "malformed" is
therefore *the overlay's* idea of it, entry by entry, with no duplicated rules.
The swap is global to the module for microseconds — doctor is single-threaded
and every consumer of ``_warn`` only logs.

WHY THE ROW NEVER VANISHES
--------------------------
Doctor's own rule (stated in run_doctor for the fleet checks and in
``doctor_rows.build_row`` for feature rows): a check that did not run prints
that it did not run; a missing check never renders as absence-of-output.
"Silent when none" therefore means the *pause information* is silent — no host,
no reason, no window, no warning — not that the line disappears.

TEST SEAM
---------
``doctor_check(directory=...)`` threads an explicit root to the overlay module's
own ``directory=`` seam, and nothing else decides the root. There is
deliberately no ``HSCC_DIR`` (or any other environment) read here: the overlay
module resolves its default root from HOME at import and has no env override, so
that root IS the root ``hscc node pause`` writes. Handing this row the ambient
``HSCC_DIR`` would recreate the t_adb66608 / t_be690204 root split — a directory
no pause can ever reach, reported as a fact about the fleet ("nothing is
paused"). The registry entry in doctor.py declares ``row_kwargs=()`` so the
wiring cannot hand a root to a row that must not choose one.

Inside a pytest process a ``directory=None`` read is REFUSED (the same fence
every operator-state row has): it would read the operator's real overlay, which
makes the suite's verdict depend on whether a node happens to be parked.
"""

from __future__ import annotations

import os
import re
import sys

#: The row's printed name. Kept in this module so the doctor row, the refusal
#: and the UNVERIFIABLE fallback can never print three spellings of one check.
CHECK_NAME = "node pauses (operator overlay)"

#: What the row says when the overlay holds nothing — the silent case. No host,
#: no reason, no window, and not a word about a fault.
SILENT_DETAIL = "no node pauses active"

#: How many of the reader's own complaints the WARN row quotes in full.
MAX_QUOTED_WARNINGS = 3

#: Fractional seconds only, same pattern (and same reasoning as)
#: ``cluster_render._FRACTIONAL_SECONDS``: ``isoformat()`` writes microseconds
#: when the writer had any and a human should not have to read them, while the
#: ``+00:00`` offset MUST survive — ``split(".")`` would eat it and quietly turn
#: a UTC stamp into something that looks local.
_FRACTIONAL_SECONDS = re.compile(r"\.\d+")


def refused_check() -> dict:
    """The in-suite verdict when no root was supplied and no seam was injected.

    An ERROR naming the seam, never a silent pass and never a read of the
    operator's overlay: inside a suite a default-root read is a read of
    ``$HOME/.hscc/node_pauses.json``, i.e. of whatever the operator happens to
    have parked.
    """
    return {
        "name": CHECK_NAME, "ok": False, "fatal": False,
        "detail": "node-pause check refused inside a pytest process — "
                  "inject _node_pauses_check or pass directory=",
        "fix": "Call doctor_node_pauses.doctor_check(directory=<tmp overlay "
               "root>) for the real read path, or inject "
               "_node_pauses_check=lambda: {...} to pin the row's routing; a "
               "suite must never read the operator's pause overlay.",
    }


def _load_core():
    """``hscc_daemon/node_pauses.py``, or ``None`` when it is not deployed.

    Same two-step as ``doctor._load_kanban_pause``: a plain import first (the
    daemon plugin deployed next to bootstrap, or an installed package), then the
    repo root on ``sys.path`` for a run from a checkout. Never raises — a
    missing overlay module must reach the row as an honest "cannot verify".
    """
    try:
        from hscc_daemon import node_pauses
        return node_pauses
    except Exception:  # noqa: BLE001 - any import failure means "not deployed"
        pass
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from hscc_daemon import node_pauses
        return node_pauses
    except Exception:  # noqa: BLE001
        return None


def _stamp(value) -> str | None:
    """An ISO stamp for display: fractional seconds trimmed, offset intact."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return _FRACTIONAL_SECONDS.sub("", trimmed) if trimmed else None


def _read(core, directory):
    """``(pauses, warnings)`` — the overlay plus the WARN lines that read it.

    ``warnings`` is why this wrapper exists: :func:`node_pauses.load` normalises
    a broken overlay down to "no pauses" and says so only through ``_warn``, so
    the caller cannot otherwise tell corruption from absence. Capturing the
    module's own logger keeps "what counts as malformed" defined in exactly one
    place.
    """
    collected: list[str] = []
    real = getattr(core, "_warn", None)
    if not callable(real):
        # A core without a _warn hook cannot be listened to; say nothing rather
        # than pretend the absence of captured lines proves the file is sound.
        return core.load(directory).get("pauses", {}), collected

    def _capture(msg):
        collected.append(str(msg))

    core._warn = _capture
    try:
        pauses = core.load(directory).get("pauses", {})
    except Exception as e:  # noqa: BLE001 - load() is documented never to raise
        collected.append(f"unreadable ({type(e).__name__}: {e})")
        pauses = {}
    finally:
        core._warn = real
    return pauses, collected


def _phrase(host, entry) -> str:
    """``192.0.2.5 "gpu training" (since 2026-…+00:00, until open-ended)``.

    The four fields the operator needs to recognise the pause: which machine,
    why it is parked, since when, and whether they have to remember to put it
    back. A missing reason or ``since`` degrades to wording, never to a blank —
    an empty parenthetical reads like a bug in the report.
    """
    reason = entry.get("reason")
    reason = reason.strip() if isinstance(reason, str) and reason.strip() \
        else "no reason given"
    since = _stamp(entry.get("since"))
    since = f"since {since}" if since else "since unknown"
    until = _stamp(entry.get("until"))
    window = f"until {until}" if until else "until open-ended"
    return f"{host} \"{reason}\" ({since}, {window})"


def _summary(pauses) -> str:
    """The info sentence for the readable pauses, or "" when there are none."""
    hosts = sorted(h for h in pauses if isinstance(h, str) and h.strip())
    if not hosts:
        return ""
    phrases = [_phrase(h, pauses[h]) for h in hosts
               if isinstance(pauses[h], dict)]
    if not phrases:
        return ""
    noun = "node" if len(phrases) == 1 else "nodes"
    return f"{len(phrases)} {noun} paused: " + "; ".join(phrases)


def doctor_check(directory=None) -> dict:
    """The node-pause row: an info row for a parked node, a WARN for a bad file.

    Three postures, one shape (the five doctor ``Check`` fields)::

        nothing parked   -> ok=True   "no node pauses active"
        N nodes parked   -> ok=True   "N nodes paused: <host> "<reason>"
                                        (since …, until …|open-ended); …"
        overlay broken   -> ok=False  WARN naming the file and quoting what the
                                      reader could not use — never fatal

    A partially readable overlay (some entries usable, some not) is the loud
    posture too: the readable pauses stay listed, because hiding them behind the
    warning would cost the operator the very information they are looking for.

    ``directory`` is the ONLY way a root reaches this row (see the module
    docstring); ``None`` means the overlay module's own default root, HOME's
    ``~/.hscc``, which is what ``hscc node pause`` writes.
    """
    if directory is None and "pytest" in sys.modules:
        return refused_check()
    core = _load_core()
    if core is None:
        return {
            "name": CHECK_NAME, "ok": False, "fatal": False,
            "detail": "hscc_daemon/node_pauses.py not importable — the "
                      "operator's node-pause overlay cannot be read",
            "fix": "Deploy the hscc_daemon plugin next to hscc-bootstrap "
                   "(re-run bootstrap); until then a parked node is invisible "
                   "to this row.",
        }

    path = core.overlay_path(directory)
    pauses, warnings = _read(core, directory)
    listed = _summary(pauses)

    if not warnings:
        # The info posture: a pause is reported, never flagged.
        return {"name": CHECK_NAME, "ok": True, "fatal": False,
                "detail": listed or SILENT_DETAIL, "fix": ""}

    # The fail-loud posture: the reader could not trust what it read. Non-fatal
    # by construction — doctor is Stage 1 of bootstrap and a broken overlay is
    # work to steer, not a reason to hard-stop an install.
    quoted = "; ".join(warnings[:MAX_QUOTED_WARNINGS])
    if len(warnings) > MAX_QUOTED_WARNINGS:
        quoted += f" (+{len(warnings) - MAX_QUOTED_WARNINGS} more)"
    detail = (f"OVERLAY UNREADABLE: {path} — {quoted}. Every pause this reader "
              f"lost looks like 'nothing parked' to doctor AND to the repair "
              f"paths, so a parked node is eligible to be healed back to life.")
    if listed:
        detail += f" Still readable: {listed}."
    return {
        "name": CHECK_NAME, "ok": False, "fatal": False, "detail": detail,
        "fix": f"repair or move aside {path}, then re-pause whatever is "
               f"parked (`hscc node pause <host> --reason ...`); a pause "
               f"nobody can read is a node the fleet will wake up",
    }
