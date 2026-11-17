"""HSCC HTTP API — the compact read-only summary for the iOS widget/Live Activity client.

    GET /v1/mobile/summary[?boards=<slug,slug>]  ->  200 per-board status counts

WHY THIS ROUTE EXISTS
=====================
``ios-app/docs/WIDGET_DATA_CONTRACT.md`` measured every widget dataset against
the live daemon. Four of the five fit routes that already exist. One did not:
the board-status tile wants five integers per board, and no existing route can
produce them honestly. ``GET /v1/cards`` costs 81,855 B for 40 open cards and
does not contain ``done`` at all; ``?status=done`` returns 0 rows;
``?include_archived=1`` costs 8.6 MB. A client-side derivation therefore renders
``done`` as ``0`` forever — a wrong number, not a missing one. The audit's
verdict for that dataset was ``needs compact server endpoint`` and this module
is the whole of that authorisation (§6 of the contract).

THE CONTRACT (contract §6 — each rule below is enforced, and tested)
====================================================================
1. Every status key is ALWAYS present, as an integer. Absence must never be
   renderable as ``0``, so ``done``/``archived`` come from the
   archived-inclusive listing, counted HERE, server-side: the 2,541 cards /
   8.6 MB that a client would have to download never cross the wire.
2. Default = every board on the host, ONE request (a widget must not pay N+1
   reads for N board tiles). ``?boards=`` narrows it.
3. No rows. The contract's optional ``rows``/``with`` blocks are explicitly NOT
   authorised by the audit, so they are rejected (400) rather than silently
   ignored — a client that asked for rows and got none must know why.
4. Redaction is STRUCTURAL, not a scrub: this handler emits board slugs and
   integers, and nothing else. A slug that does not match the board-slug shape
   is never echoed and never counted as a board. No ``repo``,
   ``workspace_path``, ``branch``, address, absolute path or token material can
   reach the payload because no such value is ever read.
5. Budget: the serialized response is under 4 KB so a Live Activity can carry
   it. Deterministic tail-trim with ``truncated`` / ``omitted_boards`` rather
   than a silent cap.
6. Read-only, no mutation, and no new auth surface: one GET registration, so
   the single central gate in ``ApiHandler._route`` -> ``_authorize()`` is what
   guards it, exactly like every other route.
7. Degrade honestly: if the board listing fails, the response carries ``speak``
   and NO ``boards`` key — an empty ``counts`` map would be read as "the board
   is quiet". A board whose cards could not be read carries
   ``{"board": slug, "error": ...}`` and no ``counts``.

Test seam: the two backing calls are ``_backing_*`` module functions, so tests
never scan a live kanban DB.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from api_server import ApiError, ROUTES

# Make the relocated flightdeck available exactly like routes_project.py /
# routes_actions.py do: insert ``hscc-project/`` on sys.path once, then import
# the module directly. Board enumeration and card reading stay OWNED by
# flightdeck — this module never re-implements board access.
_PROJECT_DIR = None
for _candidate in (
    Path(__file__).resolve().parent.parent / "hscc-project",  # live checkout
):
    if _candidate.is_dir():
        _PROJECT_DIR = _candidate
        break
if _PROJECT_DIR is not None and str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))

from flightdeck.core import kanban as _kanban  # noqa: E402


# --------------------------------------------------------------------------- #
# Contract constants
# --------------------------------------------------------------------------- #

#: The status keys that are ALWAYS present on a successful ``counts`` map
#: (contract §6 rule 1). Zero is a real value; absence is a bug.
COUNT_STATUSES = ("todo", "ready", "running", "blocked", "done", "archived")

#: Serialized-response ceiling (contract §6 rule 5). Measured the way
#: ``ApiHandler._send_json`` measures it: ``json.dumps(payload).encode()``.
MAX_RESPONSE_BYTES = 4096

#: Most boards one request may ask for. Bounds the work AND the payload.
MAX_REQUESTED_BOARDS = 64

#: A Hermes board slug: lowercase/uppercase alphanumerics, ``-`` and ``_``,
#: starting with an alphanumeric, at most 64 chars. This shape is the structural
#: redaction gate for contract §6 rule 4: it cannot contain ``/``, ``.``, ``:``
#: or whitespace, so no path, no IP and no token material can pass through it.
_BOARD_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: ``rows``/``with`` are the contract's optional blocks. The audit authorised
#: NONE of them, so asking is a contract error, not a silent no-op.
_UNIMPLEMENTED_PARAMS = ("rows", "with")

#: Surfaced on a board whose cards could not be read (contract §6 rule 7).
COUNTS_UNAVAILABLE = "counts unavailable"


# --------------------------------------------------------------------------- #
# Backing-call seam (monkeypatch these in tests)
# --------------------------------------------------------------------------- #

def _backing_list_boards() -> list:
    """Every board slug known to Hermes on this host."""
    return list(_kanban.list_boards())


def _backing_list_cards(board=None) -> list:
    """Cards of one board (``board``) or of every board (``board=None``).

    ``include_archived=True`` is the whole point of this route: it is the only
    read that can see ``done`` and ``archived``, and the counting happens here
    so the cards themselves never travel to the phone.
    """
    return _kanban.list_cards(board=board, include_archived=True)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def _now_utc_z() -> str:
    """Second-resolution UTC stamp in the house ``...Z`` form (routes_ws style)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_slug(slug) -> bool:
    """True when ``slug`` is safe to echo (see ``_BOARD_SLUG_RE``)."""
    return isinstance(slug, str) and bool(_BOARD_SLUG_RE.match(slug))


def _requested_boards(raw) -> list:
    """Parse ``?boards=a,b`` into a deduplicated, sorted slug list.

    Raises 400 on a malformed slug — a board name is the only string this route
    echoes, so it is validated at the boundary rather than scrubbed later.
    Raises 400 when more than ``MAX_REQUESTED_BOARDS`` are named.
    """
    if raw is None or not str(raw).strip():
        return []
    slugs = []
    for part in str(raw).split(","):
        slug = part.strip()
        if not slug:
            continue
        if not _valid_slug(slug):
            raise ApiError(
                400, "bad_request",
                "boards must be comma-separated board slugs "
                "(letters, digits, '-' and '_', at most 64 chars)",
                "Boards must be comma-separated board names.",
            )
        slugs.append(slug)
    if not slugs:
        return []
    if len(slugs) > MAX_REQUESTED_BOARDS:
        raise ApiError(
            400, "bad_request",
            f"too many boards requested (max {MAX_REQUESTED_BOARDS})",
            f"Too many boards requested; the limit is {MAX_REQUESTED_BOARDS}.",
        )
    return sorted(set(slugs))


def _reject_unimplemented(query) -> None:
    """400 on the contract's unauthorised optional blocks (rule 3).

    Silently ignoring ``rows=5`` would hand a client an empty ``rows`` and let
    it conclude "there are no cards". Saying no out loud is the honest answer.
    """
    for name in _UNIMPLEMENTED_PARAMS:
        if query.get(name) is not None:
            raise ApiError(
                400, "bad_request",
                f"'{name}' is not implemented by /v1/mobile/summary: it serves "
                f"board status counts only (WIDGET_DATA_CONTRACT.md section 6)",
                f"{name} is not supported. This route returns board counts.",
            )


def _count_by_status(cards) -> dict:
    """Fold a card list into ``{counts, total}`` — the compact projection.

    Every key in :data:`COUNT_STATUSES` is present as an int. A status outside
    that set (a Hermes status this widget does not draw) is reported under
    ``other`` rather than dropped, so ``total`` always equals the sum of what is
    reported — a number that quietly went missing is the failure mode this
    route exists to kill.
    """
    counts = {status: 0 for status in COUNT_STATUSES}
    other: dict = {}
    total = 0
    for card in cards or []:
        status = str((card or {}).get("status") or "unknown")
        total += 1
        if status in counts:
            counts[status] += 1
        else:
            other[status] = other.get(status, 0) + 1
    entry = {"counts": counts, "total": total}
    if other:
        entry["other"] = other
    return entry


def _open_total(entry: dict) -> int:
    """Everything that is not ``done``/``archived`` (contract §6 speak example).

    Derived from ``total`` rather than by summing the open keys, so a status
    reported under ``other`` is still counted.
    """
    counts = entry.get("counts") or {}
    return int(entry.get("total") or 0) - int(counts.get("done") or 0) \
        - int(counts.get("archived") or 0)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _speak(entries: list, board_total: int, omitted: int,
           unusable: int = 0) -> str:
    """One TTS-safe line built only from board slugs and integers (rule 4).

    It describes EVERY board that was read, not just the ones that survived the
    4 KB trim, and then says how many were omitted — the operator hears the true
    totals even when the tile list is short.
    """
    if not entries and not board_total:
        if unusable:
            return f"{_plural(unusable, 'board')} unreadable."
        return "No boards."
    open_total = sum(_open_total(e) for e in entries)
    running = sum((e.get("counts") or {}).get("running", 0) for e in entries)
    done = sum((e.get("counts") or {}).get("done", 0) for e in entries)
    unavailable = sum(1 for e in entries if "error" in e)
    if len(entries) == 1:
        head = (f"{entries[0]['board']}: {open_total} open, "
                f"{running} running, {done} done.")
    else:
        head = (f"{_plural(board_total, 'board')}: {open_total} open, "
                f"{running} running, {done} done.")
    if omitted:
        head += f" {_plural(omitted, 'board')} omitted for size."
    if unavailable:
        head += f" {_plural(unavailable, 'board')} unavailable."
    if unusable:
        head += f" {_plural(unusable, 'board')} unreadable."
    return head


def _serialize(payload: dict) -> bytes:
    """Serialize exactly as ``ApiHandler._send_json`` does — the budget meter."""
    return json.dumps(payload).encode("utf-8")


def _assemble(entries: list, speak: str, board_total: int, omitted: int,
              unusable: int = 0) -> dict:
    """The payload envelope for ``entries``, with the truncation flags set."""
    payload = {
        "generated_at": _now_utc_z(),
        "boards": entries,
        "board_count": len(entries),
        "total_boards": board_total,
    }
    if omitted:
        payload["truncated"] = True
        payload["omitted_boards"] = omitted
    if unusable:
        # Count ONLY: a slug that failed the shape gate is never echoed, so a
        # widget can see that boards exist which it is not being shown.
        payload["unreadable_boards"] = unusable
    payload["speak"] = speak
    return payload


def _fit_budget(entries: list, board_total: int,
                unusable: int = 0) -> dict:
    """Trim from the tail until the response fits :data:`MAX_RESPONSE_BYTES`.

    Deterministic (entries arrive sorted by slug), and never a silent cap: the
    response says ``truncated: true`` and how many boards were left out. One
    board plus the envelope is ~200 B, so the loop always terminates with a
    useful payload. ``speak`` is built INSIDE the loop because it names the
    omission count, and the measurement must include it.
    """
    served = list(entries)
    while True:
        omitted = len(entries) - len(served)
        speak = _speak(entries, board_total, omitted, unusable)
        payload = _assemble(served, speak, board_total, omitted, unusable)
        if len(_serialize(payload)) <= MAX_RESPONSE_BYTES or not served:
            return payload
        served.pop()
    # unreachable: the `not served` guard above exits first


# --------------------------------------------------------------------------- #
# Handler
# --------------------------------------------------------------------------- #

def handle_mobile_summary(server, ctx, query, body):
    """``GET /v1/mobile/summary`` — per-board status counts, aggregated here.

    Read-only; the body is never read. ``?boards=a,b`` narrows the response to
    those boards (404 when one of them does not exist).
    """
    _reject_unimplemented(query)
    requested = _requested_boards(query.get("boards"))

    try:
        known = [str(x) for x in _backing_list_boards()]
    except Exception:
        # Contract §6 rule 7: NO `boards` key at all. Empty counts here would
        # be rendered as "every board is quiet", which is a lie.
        return 200, {"generated_at": _now_utc_z(),
                     "speak": "Board listing unavailable."}

    # Structural redaction (rule 4): a slug that fails the shape gate is never
    # echoed and never counted as a board. Only its NUMBER is reported.
    slugs = [s for s in known if _valid_slug(s)]
    unusable = len(known) - len(slugs)
    if requested:
        missing = [s for s in requested if s not in slugs]
        if missing:
            raise ApiError(
                404, "not_found",
                f"unknown board(s): {', '.join(missing)}",
                f"Board {missing[0]} does not exist.",
            )
        slugs = requested

    entries: list = []
    if slugs:
        if requested:
            # One read per named board (bounded by MAX_REQUESTED_BOARDS), so a
            # single unreadable board degrades on its own instead of taking the
            # whole response down.
            for slug in slugs:
                try:
                    cards = _backing_list_cards(board=slug)
                except Exception:
                    entries.append({"board": slug, "error": COUNTS_UNAVAILABLE})
                    continue
                entries.append({"board": slug, **_count_by_status(cards)})
        else:
            # One archived-inclusive read for the whole host, grouped by the
            # board each card came from: the default is ONE request, not N+1.
            try:
                cards = _backing_list_cards()
            except Exception:
                return 200, {"generated_at": _now_utc_z(),
                             "speak": "Board counts unavailable."}
            by_board: dict = {}
            for card in cards or []:
                by_board.setdefault(
                    str((card or {}).get("board") or ""), []).append(card)
            for slug in slugs:
                # A board with no cards gets a real all-zero entry: `total: 0`
                # is a measured fact, not an absence.
                entries.append({"board": slug,
                                **_count_by_status(by_board.get(slug, []))})

    entries.sort(key=lambda e: e["board"])
    return 200, _fit_budget(entries, len(entries), unusable)


# --------------------------------------------------------------------------- #
# Route registration (import side-effect; loaded by api_server.py)
# --------------------------------------------------------------------------- #

ROUTES.append(("GET", re.compile(r"^/v1/mobile/summary$"),
               handle_mobile_summary))
