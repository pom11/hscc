"""The route-completeness gate: every registered route is documented or registered-missing.

Audit F2 (``docs/audits/2026-11-23-agent-surface.md``) could sit "unresolved in the docs"
while the code was already right, for one plain reason: ``hscc-api`` registers routes
from 24 ``routes_*.py`` modules into ``api_server.ROUTES`` (plus one WebSocket upgrade in
``api_server.WS_ROUTES``), while ``docs/API.md``'s Endpoint reference documented a curated
subset -- and nothing compared the two. Measured on ``fd15ecc6`` + ``1e024992`` (the F2
docs commit plus ``main`` at the time this card started): **98 registered routes, 25 of
them named by a Path cell.** 15 of the 73 unnamed ones are routes the SHIPPED iOS client
calls. An integrator reading the canonical reference could not learn those doors exist --
exactly the "boundary only its authors know" failure F2 recorded.

So this module owns one rule and its two classic escape hatches:

  * **Coverage.** Each ``(method, display path)`` in the live table must appear as a Path
    cell in the Endpoint reference, or as an entry in
    ``hscc-api/tests/route_coverage_allowlist.py``. A route that is neither is a failure
    with instructions, not a warning.
  * **Equality, never prefix.** A Path cell covers exactly its own path, segment for
    segment. ``/v1/foo`` documented does not cover ``/v1/foo/list`` -- the trap that let
    F2 read two answers as one. ``test_a_documented_row_never_covers_a_sibling_route``
    proves the matcher refuses the prefix, on the real profile pair.
  * **Handler identity, not path strings.** Every documented cell is resolved through the
    LIVE dispatcher (``^/v1/profiles/(?P<name>[^/]+)$`` also matches ``/v1/profiles/list``,
    and registration order decides the winner), and the row must name the handler the
    dispatcher would actually run.
  * **A reason, not a rename.** Each register entry carries ``see <file>`` -- asserted to
    exist AND to name the route -- plus ``deferred: <card>``. A register of bare paths is
    the gap with a longer name, so an entry with no pointer or no owner reddens here.
  * **The rows that promise a payload keep the promise.** For every read row this card
    documented, the first ``{...}`` group in its Description is compared with the key set
    the handler actually returns, measured by calling the handler in a sandboxed child
    process with a subprocess+socket tripwire installed (``_route_shape_probe.py``).
    ``SHAPE_UNCHECKED`` says which rows are excluded and why, so the exception is a
    record rather than a hole.

Hermetic: no server, no socket, no fleet, no real ``~/.hscc``. The payload measurement
runs in a child interpreter whose ``HOME``/``HSCC_DIR``/``HERMES_HOME`` are a ``tmp_path``
-style sandbox set BEFORE ``api_server`` is imported, and whose every subprocess and
socket entry point raises -- a route that tries to reach the cluster fails the test
instead of touching the fleet.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import api_server
from _route_shape_probe import SHAPE_CHECKED
from route_coverage_allowlist import UNDOCUMENTED_ROUTES

API_DOC = "docs/API.md"
REGISTER = "hscc-api/tests/route_coverage_allowlist.py"
REFERENCE_HEADING = "## Endpoint reference"
METHOD_CELLS = {"GET", "POST", "PATCH", "PUT", "DELETE"}
#: Stand-in for a path placeholder: never a real route segment, so a probe cannot be
#: captured by a literal route registered earlier (``/v1/profiles/list`` for ``{name}``).
PROBE_SEGMENT = "zz"

#: Documented on purpose, payload deliberately NOT compared: the top-level shape is
#: conditional, so no single key set is the truth and pretending otherwise would pin a
#: degraded answer as the contract.
SHAPE_UNCHECKED = {
    "GET /v1/cron/list":
        "answers {jobs, speak}, or {speak} alone when the store is unreadable",
    "GET /v1/mobile/summary":
        "carries no per-board key at all when the board listing is unavailable",
    # routes_orchestrator._job_dict builds its keys per job state: {job_id,
    # project, status, elapsed} always; +notice while live; +reply/profile/
    # session/speak on `done`; +error on any job that carries one. No single
    # key set is the truth, so pinning one would publish a fiction.
    "GET /v1/orchestrator/chat/{id}":
        "key set varies with the job's status -- see the Orchestrator chat "
        "table; no single top-level key set is the contract",
    # routes_logs.handle_logs returns `entries[-limit:]` -- a list, so there is
    # no top-level dict to compare keys against. The entry shape
    # {timestamp, level, source, line} is stated in the row's prose and pinned
    # by tests/test_routes_logs.py instead.
    "GET /v1/logs":
        "answers a bare ARRAY of {timestamp, level, source, line} entries -- "
        "an array has no top-level key set to compare",
}

_REPO_ROOT = Path(api_server.__file__).resolve().parent.parent
_HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# The live dispatch table, the only source of truth about what is served
# --------------------------------------------------------------------------- #

def _live_routes() -> list[tuple[str, str, object]]:
    """``(method, pattern, handler)`` for everything ``api_server`` would dispatch.

    Read from the LIVE lists after the full ``routes_*`` import chain, the way
    ``api_server`` builds them at import -- not from a hand-kept roster, which is the
    thing that goes stale.
    """
    return (list(api_server.ROUTES)
            + [("WS", pattern, handler) for pattern, handler in api_server.WS_ROUTES])


def _display(pattern: str) -> str:
    """``^/v1/profiles/(?P<name>[^/]+)$`` -> ``/v1/profiles/{name}``."""
    out = pattern.lstrip("^").rstrip("$")
    out = re.sub(r"\(\?P<(\w+)>[^()]*\)", r"{\1}", out)
    out = out.replace("[^/]+", "{param}")
    return out.replace("\\.", ".").replace("\\/", "/")


def _segments(display: str) -> tuple[str, ...]:
    """Path compared segment-wise, every placeholder collapsed to one marker.

    Two routes are the same route only when their segment tuples are EQUAL -- this is
    what makes a documented prefix unable to cover a longer path.
    """
    return tuple("{*}" if seg.startswith("{") else seg
                 for seg in (display.rstrip("/") or "/").split("/"))


def _probe(display: str) -> str:
    """A concrete path that only the route itself can answer."""
    return "/".join(PROBE_SEGMENT if seg.startswith("{") else seg
                    for seg in display.split("/"))


# --------------------------------------------------------------------------- #
# The Endpoint reference, parsed as rows
# --------------------------------------------------------------------------- #

def _reference() -> str:
    doc = (_REPO_ROOT / API_DOC).read_text(encoding="utf-8")
    start = doc.find(REFERENCE_HEADING)
    assert start != -1, f"{API_DOC} lost its {REFERENCE_HEADING!r} heading"
    rest = doc[start + len(REFERENCE_HEADING):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _rows() -> dict[tuple[str, str], dict[str, str]]:
    """Endpoint-reference table rows keyed by ``(METHOD, Path cell)``.

    The Path cell is kept VERBATIM (backticks and query string included) so callers can
    compare it for equality, and a row only counts when its Method cell is a real verb --
    that is what keeps the payload/error tables elsewhere in the section out of the set.
    """
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for line in _reference().splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] not in METHOD_CELLS:
            continue
        path = next((c for c in cells[1:] if c.strip("`").startswith("/v1/")), None)
        if path is None:
            continue
        rows[(cells[0], path)] = {"method": cells[0], "path": path,
                                  "desc": cells[2] if len(cells) > 2 else "",
                                  "mutates": cells[-1]}
    return rows


def _documented() -> dict[tuple[str, tuple[str, ...]], dict[str, str]]:
    return {(method, _segments(cell.strip("`").split("?")[0])): row
            for (method, cell), row in _rows().items()}


def _brace_group(text: str) -> str | None:
    """The FIRST depth-0 ``{...}`` group -- the payload a row promises."""
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            assert depth >= 0, f"unbalanced brace in doc row: {text[:60]!r}"
            if depth == 0 and start is not None:
                return text[start + 1:i]
    return None


def _key_set(group: str) -> set[str]:
    """Top-level key NAMES of one brace group (``a, b: [..], c`` -> ``{a, b, c}``)."""
    items, depth, buf = [], 0, ""
    for ch in group:
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(buf)
            buf = ""
        else:
            buf += ch
    items.append(buf)
    keys = set()
    for item in items:
        key = item.split(":", 1)[0].strip().strip("`*_ ").strip()
        if key:
            keys.add(key)
    return keys


def _uncovered(documented, register) -> list[str]:
    """Live routes named by NEITHER the reference NOR the register."""
    missing = []
    for method, pattern, handler in _live_routes():
        key = (method, _segments(_display(pattern.pattern)))
        if key in documented or (method, _display(pattern.pattern)) in register:
            continue
        display = _display(pattern.pattern)
        missing.append(f"{method:5} {display:52} "
                       f"{getattr(handler, '__module__', '?')}.{getattr(handler, '__name__', '?')}")
    return sorted(missing)


# --------------------------------------------------------------------------- #
# 1. Coverage: registered == documented + registered-missing
# --------------------------------------------------------------------------- #

def test_every_registered_route_is_documented_or_in_the_register():
    missing = _uncovered(_documented(), UNDOCUMENTED_ROUTES)
    assert not missing, (
        "docs/API.md's Endpoint reference names neither of these, and "
        f"{REGISTER} does not carry them either. Registered-but-undocumented is "
        "the audit F2 failure class -- an endpoint only its author knows. Fix by "
        "adding a row (with its payload key set) or, if the reference is genuinely "
        "not its door, add an entry with `see <file>` + `deferred: <card>`:\n"
        + "\n".join(missing))


def test_the_endpoint_reference_names_no_route_that_is_not_registered():
    """The same comparison the other way: a row for a route nobody serves is a lie."""
    live = {(method, _segments(_display(pattern.pattern)))
            for method, pattern, _ in _live_routes()}
    ghosts = sorted(f"{m} {cell}" for (m, cell) in _rows()
                    if (m, _segments(cell.strip("`").split("?")[0])) not in live)
    assert not ghosts, (
        "these Path cells name nothing in api_server.ROUTES -- the row is stale "
        f"or the route was removed without it: {ghosts}")


def test_the_reference_and_the_register_do_not_both_name_a_route():
    """The register stays minimal: documenting a route means deleting its entry."""
    documented = set(_documented())
    overlap = sorted(f"{m} {p}" for (m, p) in UNDOCUMENTED_ROUTES
                     if (m, _segments(p)) in documented)
    assert not overlap, (
        "documented AND registered-missing at once: delete the register entry in "
        f"the same commit that added the row: {overlap}")


# --------------------------------------------------------------------------- #
# 2. The two traps that let the first pass of this gate pass a mutant
# --------------------------------------------------------------------------- #

def test_a_documented_row_never_covers_a_sibling_route():
    """The prefix trap, proved on the real pair rather than asserted in prose.

    ``/v1/profiles`` is a literal prefix of ``/v1/profiles/list``. Drop the roster's own
    row from the parsed set and the roster must show up as uncovered -- if coverage were
    satisfied by prefix/``in`` matching, the same mutation would stay green.
    """
    roster = ("GET", _segments("/v1/profiles/list"))
    counts = ("GET", _segments("/v1/profiles"))
    full = _documented()
    assert roster in full and counts in full, (
        "both profile reads must keep their own row; this test is the guard "
        "against the pair collapsing back into one")
    narrowed = {k: v for k, v in full.items() if k != roster}
    uncovered = _uncovered(narrowed, UNDOCUMENTED_ROUTES)
    assert any("/v1/profiles/list" in line for line in uncovered), (
        "coverage matched the roster through the shorter /v1/profiles row -- the "
        "matcher is matching prefixes, not paths")


def test_each_documented_row_names_the_handler_the_dispatcher_would_run():
    """The identity trap: patterns overlap, registration order decides the winner.

    ``^/v1/profiles/(?P<name>[^/]+)$`` matches ``/v1/profiles/list``, so a row can name a
    path whose request the dispatcher hands to a DIFFERENT handler. Resolve each cell
    through the live table and compare handlers, not strings.
    """
    live = _live_routes()
    for (method, cell), row in _rows().items():
        display = cell.strip("`").split("?")[0]
        hits = [(p, h) for m, p, h in live if m == method and p.match(_probe(display))]
        assert hits, f"{method} {cell}: nothing registered answers the probe path"
        winner = _display(hits[0][0].pattern)
        assert _segments(winner) == _segments(display), (
            f"{method} {cell} documents a path whose live handler is "
            f"{hits[0][1].__module__}.{hits[0][1].__name__} (pattern {winner!r}) -- "
            "the row names a route the dispatcher does not reach for it")


def test_an_undocumented_route_with_no_reason_is_a_failure_not_a_warning():
    """Every register entry carries a pointer that exists and names the route."""
    bad = []
    for (method, path), reason in UNDOCUMENTED_ROUTES.items():
        text = str(reason)
        if len(text) < 40 or "deferred: t_" not in text:
            bad.append(f"{method} {path}: no owning card in {text!r}")
            continue
        match = re.search(r"see (\S+?);", text)
        if not match:
            bad.append(f"{method} {path}: reason names no file -- {text!r}")
            continue
        rel = match.group(1)
        target = _REPO_ROOT / rel
        if not target.is_file():
            bad.append(f"{method} {path}: pointer {rel!r} is not a file")
            continue
        literal = path.split("{")[0].rstrip("/")
        if literal not in target.read_text(encoding="utf-8", errors="replace"):
            bad.append(f"{method} {path}: pointer {rel!r} does not name the route")
    assert not bad, "an allowlist entry without a verified reason is the gap " \
                    "renamed: " + "; ".join(bad)


# --------------------------------------------------------------------------- #
# 3. Rows that promise a payload keep the promise
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=1)
def _measured() -> dict:
    proc = subprocess.run(
        [sys.executable, str(_HERE / "_route_shape_probe.py")],
        capture_output=True, text=True, timeout=300)
    lines = [ln for ln in proc.stdout.splitlines()
             if ln.startswith("@@ROUTE_SHAPE@@")]
    assert lines, (
        f"the payload probe produced no measurement (rc {proc.returncode}): "
        f"{(proc.stderr or '')[-500:]}")
    return json.loads(lines[-1][len("@@ROUTE_SHAPE@@"):])["routes"]


def test_the_shape_checked_rows_are_documented():
    measured = _measured()
    assert set(SHAPE_CHECKED) | set(SHAPE_UNCHECKED) <= {
        f"{m} {c.strip('`').split('?')[0]}" for (m, c) in _rows()}, (
        "SHAPE_CHECKED/SHAPE_UNCHECKED name rows that are not in the reference")
    assert measured, "the probe measured nothing"


def test_documented_key_sets_are_the_key_sets_the_handlers_return():
    """Every read row this card documented promises exactly what its handler returns."""
    rows = _rows()
    measured = _measured()
    drift = []
    for key in SHAPE_CHECKED:
        method, display = key.split(" ", 1)
        cell = next((c for (m, c) in rows
                     if m == method and c.strip("`").split("?")[0] == display), None)
        assert cell, f"{key} is in SHAPE_CHECKED but has no row"
        group = _brace_group(rows[(method, cell)]["desc"])
        assert group, f"{key}'s row promises no payload key set"
        got = measured[key]
        if got.get("keys") is None:
            drift.append(f"{key}: handler {got.get('handler')} did not answer "
                         f"a dict ({got.get('error')}) -- the row is unverifiable")
            continue
        promised = _key_set(group)
        if promised != set(got["keys"]):
            drift.append(f"{key}: row promises {sorted(promised)}, handler "
                         f"{got['handler']} returns {sorted(got['keys'])}")
    assert not drift, (
        "docs/API.md and the handlers disagree -- fix the row and the payload "
        "together, never one alone:\n" + "\n".join(drift))


def test_the_rows_that_decline_a_shape_check_say_why():
    """The exception is a record, not a hole."""
    rows = {f"{m} {c.strip('`').split('?')[0]}" for (m, c) in _rows()}
    assert set(SHAPE_UNCHECKED) <= rows, "SHAPE_UNCHECKED names a row that vanished"
    for key, why in SHAPE_UNCHECKED.items():
        assert len(why) > 20, f"{key}: unchecked without a real reason"
        assert key not in SHAPE_CHECKED, f"{key}: checked AND unchecked"
