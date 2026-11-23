"""F2: the ``hscc profiles`` answer has ONE route and ONE implementation.

Audit ``docs/audits/2026-11-23-agent-surface.md:186`` (F2) recorded that
``hscc profiles`` "is answered by TWO routes — ``GET /v1/profiles``
(``routes_ops``) and ``GET /v1/profiles/list`` (``routes_profiles``)" and asked
for one canonical, the other aliased or deprecated with a pointer.

Measured against the payloads, the audit's premise needed correcting: the two
routes answer two different questions.

  * ``GET /v1/profiles`` -> ``counts`` / ``total_running`` / ``profiles`` = a
    list of profile NAMES. This IS ``hscc profiles``: the CLI verb renders
    ``cluster_engine.cmd_profile_status()`` (``hscc_daemon/hscc.py``) and this
    route's backing calls that same function — one implementation, two doors.
  * ``GET /v1/profiles/list`` -> ``count`` / ``profiles`` = a list of per-profile
    OBJECTS plus the config whitelist, from ``hermes_cli.profiles.list_profiles``.
    It carries no count of anything, and iOS's create-card assignee picker
    (``ProfileListResponse``) cannot be served by the counts route.

So neither route can be deleted or aliased without breaking a shipped decoder,
and the "two answers to one question" F2 feared is actually a shared key name
(``profiles``) with two meanings. The disposition is therefore the card's second
option — deprecate the ambiguity with a pointer — plus the pin that makes the
pointer load-bearing:

  1. exactly one registered route produces the counts payload;
  2. exactly one implementation of that payload exists, and the two doors to it
     are the canonical route and the CLI verb (nothing else reaches it);
  3. the roster can never become a second answer — no count keys, and it never
     references the count implementation;
  4. the pointer is named on BOTH ends and resolves to a real route, so it is
     not decorative (a one-directional pointer is how F2 happened; same rule as
     ``test_kanban_monitor_route_pointer.py`` for F8).

Nolive + hermetic: both backings are monkeypatched on their own module, so no
test opens a kanban DB, imports ``hermes_cli``, enumerates processes, reads
``~/.hermes`` or contacts a host. Requests run over real loopback HTTP (port 0)
so the router and the auth gate are exercised, the same harness as
``test_routes_ops.py`` / ``test_routes_profiles.py``; the leg's autouse
``_isolate_hscc`` fixture keeps ``~/.hscc`` out of it. The AST scans read files
only — top level of each source dir, never a ``tests/`` tree.
"""

import ast
import http.client
import json
import re
import threading
import types
from pathlib import Path

import pytest

import api_server
import routes_ops
import routes_profiles


COUNTS_PATH = "/v1/profiles"
ROSTER_PATH = "/v1/profiles/list"

# The audit row this card closes, quoted so a rewrite of the doc that drops the
# claim is visible next to the test that depends on it.
AUDIT_ROW = "docs/audits/2026-11-23-agent-surface.md:186"

_REPO_ROOT = Path(routes_ops.__file__).resolve().parent.parent

# The one payload the counts question may ever have (the shape
# `profile_status.get_profile_status` returns, plus the read's `speak`).
COUNTS_ANSWER = {
    "counts": {"researcher-a": 2, "architect": 1},
    "total_running": 3,
    "profiles": ["architect", "researcher-a"],
}

# A roster entry: the whitelist shape `_profile_info_to_dict` produces. Note
# `gateway_running` — a per-profile BOOLEAN about the gateway, not a count of
# cards. It is the field that lets a careless reader call this "profiles with
# running work", i.e. a second answer to the counts question.
ROSTER_ANSWER = [
    {"name": "architect", "is_default": False, "gateway_running": True,
     "model": "claude-3.5", "provider": "anthropic", "skill_count": 5,
     "description": "Architecture lead", "description_auto": False,
     "distribution_name": None, "distribution_version": None},
    {"name": "default", "is_default": True, "gateway_running": False,
     "model": "gpt-4o", "provider": "openrouter", "skill_count": 0,
     "description": "", "description_auto": False,
     "distribution_name": None, "distribution_version": None},
]


# --------------------------------------------------------------------------- #
# Harness: one server, both doors, both backings stubbed
# --------------------------------------------------------------------------- #

@pytest.fixture
def calls(monkeypatch):
    """Stub both backings and count how often each door was reached."""
    seen = {"counts": 0, "roster": 0}

    def _counts():
        seen["counts"] += 1
        return dict(COUNTS_ANSWER)

    def _roster():
        seen["roster"] += 1
        return [dict(p) for p in ROSTER_ANSWER]

    monkeypatch.setattr(routes_ops, "_backing_profiles", _counts)
    monkeypatch.setattr(routes_profiles, "_backing_list_profiles", _roster)
    return seen


@pytest.fixture
def running(tmp_path, calls):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _get(running, token, path):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    conn.request("GET", path, headers={"Authorization": "Bearer " + token})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, (json.loads(data) if data else {})


def _matching(path, method=None):
    """Every (method, pattern, handler) in the LIVE table matching ``path``."""
    return [(m, p, h) for (m, p, h) in api_server.ROUTES
            if p.match(path) and (method is None or m == method)]


def _names_route(text, route):
    """Does ``text`` name ``route``'s path as ITSELF, not as a prefix of a longer one?

    ``/v1/profiles`` is a literal prefix of ``/v1/profiles/list``, so a plain
    ``in`` test on the roster's own docstring would pass on a docstring that
    never mentions the canonical route — the same name-collision that let F2
    read two routes as one answer. The lookahead is the whole point.
    """
    path = route.split(" ", 1)[1]
    return re.search(re.escape(path) + r"(?!/)", text) is not None


def _first_match(path, method="GET"):
    """The handler the real dispatcher would run: first (method, regex) match."""
    hits = _matching(path, method)
    assert hits, f"nothing registered matches {method} {path}"
    return hits[0][2]


# --------------------------------------------------------------------------- #
# 1. Exactly one route answers the counts question
# --------------------------------------------------------------------------- #

class TestOneRouteAnswersTheCountsQuestion:
    def test_only_the_canonical_route_produces_a_counts_payload(
            self, running, token):
        """Drive BOTH doors; only one may answer with counts.

        This is the F2 claim itself, executed: a future edit that adds
        ``counts`` to the roster payload (the drift the audit warned about)
        turns this red because the answered-set grows to two paths.
        """
        answered = set()
        for path in (COUNTS_PATH, ROSTER_PATH):
            status, payload = _get(running, token, path)
            assert status == 200, f"{path} did not answer 200"
            if "counts" in payload and "total_running" in payload:
                answered.add(path)
        assert answered == {COUNTS_PATH}, (
            f"{AUDIT_ROW}: exactly one route may answer `hscc profiles`; "
            f"the counts question was answered by {sorted(answered)}")

    def test_the_roster_payload_key_set_is_closed(self, running, token):
        """The roster may never grow a count key — strict key set, on purpose.

        ``count`` (how many profiles exist) is not ``counts`` (how many cards
        each profile is running) and the names are one letter apart. Listing
        the roster's keys exactly is what stops that pair from being conflated
        by the next person to touch either handler.
        """
        status, payload = _get(running, token, ROSTER_PATH)
        assert status == 200
        assert set(payload) == {"profiles", "count", "speak"}
        assert "counts" not in payload
        assert "total_running" not in payload
        assert payload["count"] == len(ROSTER_ANSWER)

    def test_the_shared_key_means_different_things_in_the_two_answers(
            self, running, token):
        """``profiles`` is NAMES here and OBJECTS there — pinned, not folklore.

        The same key name with two meanings is why F2 read two routes as two
        answers to one question. Swift's two decoders make it loud; a Python
        client doing ``.get("profiles")`` has nothing but this test.
        """
        _, counts = _get(running, token, COUNTS_PATH)
        _, roster = _get(running, token, ROSTER_PATH)
        assert all(isinstance(n, str) for n in counts["profiles"])
        assert all(isinstance(p, dict) for p in roster["profiles"])
        assert set(counts["profiles"]) == {"architect", "researcher-a"}
        assert [p["name"] for p in roster["profiles"]] == ["architect", "default"]

    def test_exactly_one_route_is_registered_on_the_counts_path(self):
        """One registration, one method, and it is the canonical handler."""
        hits = _matching(COUNTS_PATH)
        assert [(m, p.pattern) for (m, p, _h) in hits] == [
            ("GET", routes_ops.PROFILES_STATUS_PATTERN)]
        assert hits[0][2] is routes_ops.handle_profiles

    def test_the_roster_path_resolves_to_the_roster_not_to_show(self):
        """The pointer is only honest if the dispatcher really reaches it.

        ``^/v1/profiles/(?P<name>[^/]+)$`` matches ``/v1/profiles/list`` too;
        the roster is registered first so the exact match wins. Reverse those
        two lines and every caller the pointer sends off to "the roster" would
        get ``handle_show`` with ``name='list'`` — a 404, not a roster.
        """
        assert _first_match(ROSTER_PATH) is routes_profiles.handle_list


# --------------------------------------------------------------------------- #
# 2. One implementation of that payload
# --------------------------------------------------------------------------- #

def _source_files(*dirs):
    """Top-level ``*.py`` of each named dir (never a ``tests/`` tree)."""
    out = []
    for d in dirs:
        out.extend(sorted((_REPO_ROOT / d).glob("*.py")))
    return out


def _modules_referencing(symbol, files):
    """(module path, kind) for every AST reference to ``symbol`` in ``files``.

    Counts a call ``x.symbol()``, a bare-name use, and an ``import`` alias — a
    second implementation can arrive as any of the three. A ``def symbol`` is
    not a reference and is reported separately by the definition scan.
    """
    hits = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == symbol):
                hits.append((path, "call"))
            elif isinstance(node, ast.Name) and node.id == symbol:
                hits.append((path, "name"))
            elif isinstance(node, ast.Attribute) and node.attr == symbol:
                hits.append((path, "attr"))
            elif isinstance(node, ast.alias) and node.name == symbol:
                hits.append((path, "import"))
    return hits


def _definitions(symbol, files):
    out = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == symbol):
                out.append(path)
    return out


ALL_SOURCE_DIRS = ("hscc-api", "hscc_daemon", "hscc-cluster")


class TestOneImplementationOfThePayload:
    def test_the_counts_implementation_is_defined_once(self):
        defs = _definitions("get_profile_status", _source_files(*ALL_SOURCE_DIRS))
        assert [p.name for p in defs] == ["profile_status.py"], (
            f"{AUDIT_ROW}: `get_profile_status` must have ONE definition; "
            f"found {[str(p) for p in defs]}")
        assert defs[0] == _REPO_ROOT / "hscc-cluster" / "profile_status.py"

    def test_only_the_engine_wrapper_reaches_the_implementation(self):
        """Only ``hscc-cluster/hscc.py::cmd_profile_status`` imports/calls it."""
        refs = _modules_referencing(
            "get_profile_status",
            [p for p in _source_files(*ALL_SOURCE_DIRS)
             if p.name != "profile_status.py"])
        assert {p.name for p, _kind in refs} == {"hscc.py"}, (
            f"{AUDIT_ROW}: the counts implementation may be reached only by "
            f"the cluster engine wrapper; found {sorted({str(p) for p, _ in refs})}")

    def test_the_two_doors_to_the_implementation_are_the_route_and_the_cli(self):
        """``cmd_profile_status`` is CALLED from exactly two non-test places.

        ``hscc-api/routes_ops.py`` (the canonical route) and
        ``hscc_daemon/hscc.py`` (the ``hscc profiles`` verb). That pair IS the
        single source of truth F2 asked for: CLI and HTTP cannot disagree, and
        a third door — a second route, a tool, a daemon projection — reddens
        this test before it can drift.
        """
        hits = _modules_referencing("cmd_profile_status",
                                    _source_files(*ALL_SOURCE_DIRS))
        calls = sorted({str(p.relative_to(_REPO_ROOT))
                        for p, kind in hits if kind == "call"})
        assert calls == [
            str(Path("hscc-api") / "routes_ops.py"),
            str(Path("hscc_daemon") / "hscc.py"),
        ], f"{AUDIT_ROW}: expected one route + one CLI door, found {calls}"

    def test_the_roster_never_touches_the_counts_implementation(self):
        """The roster may not reach the counts answer at all — by any name.

        ``import profile_status`` would be the smallest way for
        ``GET /v1/profiles/list`` to quietly start answering ``hscc profiles``.
        """
        tree = ast.parse(Path(routes_profiles.__file__).read_text(),
                         filename=routes_profiles.__file__)
        forbidden = {"profile_status", "get_profile_status", "cmd_profile_status"}
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name for a in node.names if a.name in forbidden}
            elif isinstance(node, ast.ImportFrom):
                if node.module in forbidden:
                    found.add(node.module)
                found |= {a.name for a in node.names if a.name in forbidden}
            elif isinstance(node, (ast.Name, ast.Attribute)):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name in forbidden:
                    found.add(name)
        assert not found, (
            f"{AUDIT_ROW}: routes_profiles.py must not reach the counts "
            f"implementation; found {sorted(found)}")

    def test_the_canonical_backing_is_the_single_door_in_the_api(self):
        """Inside ``hscc-api``, exactly one function calls the implementation."""
        hits = [p for p, kind in _modules_referencing(
            "cmd_profile_status", sorted((_REPO_ROOT / "hscc-api").glob("*.py")))
            if kind == "call"]
        assert [p.name for p in hits] == ["routes_ops.py"]
        # ...and it is the seam tests patch, not a call buried in a handler.
        tree = ast.parse(Path(routes_ops.__file__).read_text(),
                         filename=routes_ops.__file__)
        owners = {n.name for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and any(isinstance(c, ast.Call)
                          and isinstance(c.func, ast.Attribute)
                          and c.func.attr == "cmd_profile_status"
                          for c in ast.walk(n))}
        assert owners == {"_backing_profiles"}, (
            f"the counts implementation must be reached only through the "
            f"_backing_profiles seam; found call sites in {sorted(owners)}")

    def test_the_canonical_route_adds_nothing_but_speak(self, running, token,
                                                        monkeypatch):
        """The handler is a pass-through, so it cannot be a second answer.

        ``handle_profiles`` may add ``speak`` and nothing else: the moment it
        recomputes or re-derives a number, the payload has two producers. The
        backing below is deliberately NOT self-consistent — ``total_running``
        disagrees with ``sum(counts.values())`` and ``profiles`` names a profile
        with no count row. The real
        ``profile_status.get_profile_status`` always agrees with itself, and a
        self-consistent fixture cannot tell a pass-through from a handler that
        recomputes the total (measured: with a consistent fixture that mutation
        passed this test unnoticed). The handler's contract is to pass the
        payload through untouched, so the fake must make any recomputation
        observable.
        """
        answer = {"counts": {"researcher-a": 2, "architect": 1},
                  "total_running": 5,
                  "profiles": ["architect", "researcher-a", "worker"]}
        monkeypatch.setattr(routes_ops, "_backing_profiles", lambda: dict(answer))
        status, payload = _get(running, token, COUNTS_PATH)
        assert status == 200
        assert {k: v for k, v in payload.items() if k != "speak"} == answer
        assert payload["speak"] == "2 profiles running 5 tasks."
        assert payload["total_running"] != sum(payload["counts"].values()), (
            "the fixture lost its bite: total now equals the counts sum, so a "
            "recomputing handler would pass this test again")

    def test_the_canonical_backing_is_reached_once(self, running, token, calls):
        """One request, one pass through the seam.

        The F8 rule for the same reason: a handler that reads the counts twice
        (or reads them somewhere else as well) has two answers to reconcile
        even while both look right on a quiet fixture.
        """
        status, payload = _get(running, token, COUNTS_PATH)
        assert status == 200 and payload["speak"]
        assert calls["counts"] == 1
        assert calls["roster"] == 0, "the counts door must not reach the roster"


# --------------------------------------------------------------------------- #
# 3. The degraded payload must not fabricate the other answer
# --------------------------------------------------------------------------- #

class TestDegradedAnswersStayHonest:
    def test_counts_route_failure_does_not_invent_zeroes(self, running, token,
                                                         monkeypatch):
        def _boom():
            raise RuntimeError("board unreadable")

        monkeypatch.setattr(routes_ops, "_backing_profiles", _boom)
        status, payload = _get(running, token, COUNTS_PATH)
        assert status == 200
        assert "counts" not in payload and "total_running" not in payload
        assert payload["speak"] == "Profile status unavailable."

    def test_roster_failure_does_not_borrow_the_counts_shape(self, running,
                                                              token, monkeypatch):
        def _boom():
            raise RuntimeError("hermes_cli unimportable")

        monkeypatch.setattr(routes_profiles, "_backing_list_profiles", _boom)
        status, payload = _get(running, token, ROSTER_PATH)
        assert status == 200
        assert set(payload) == {"profiles", "count", "speak"}
        assert payload["profiles"] == [] and payload["count"] == 0


# --------------------------------------------------------------------------- #
# 4. The pointer is named on both ends, and resolves
# --------------------------------------------------------------------------- #

class TestPointerNamedOnBothEnds:
    def test_the_pair_agrees_on_which_route_is_canonical(self):
        """One literal per module, and they cannot drift."""
        assert routes_ops.PROFILES_STATUS_ROUTE == "GET /v1/profiles"
        assert routes_profiles.PROFILES_STATUS_POINTER == routes_ops.PROFILES_STATUS_ROUTE
        assert routes_ops.PROFILES_ROSTER_POINTER == routes_profiles.PROFILES_ROSTER_ROUTE
        assert routes_profiles.PROFILES_ROSTER_ROUTE == "GET /v1/profiles/list"

    def test_each_pointer_resolves_to_the_handler_it_claims(self):
        """A pointer that 404s is worse than none — check it lands somewhere."""
        for label in ("GET /v1/profiles", "GET /v1/profiles/list"):
            method, path = label.split(" ", 1)
            assert _matching(path, method), f"{label} is not registered"
        assert _first_match("/v1/profiles") is routes_ops.handle_profiles
        assert _first_match("/v1/profiles/list") is routes_profiles.handle_list

    def test_the_named_route_and_the_registered_pattern_are_one_name(self):
        """The prose constant and the compiled pattern cannot drift apart."""
        for route, pattern in ((routes_ops.PROFILES_STATUS_ROUTE,
                                routes_ops.PROFILES_STATUS_PATTERN),
                               (routes_profiles.PROFILES_ROSTER_ROUTE,
                                routes_profiles.PROFILES_ROSTER_PATTERN)):
            method, path = route.split(" ", 1)
            compiled = re.compile(pattern)
            assert compiled.match(path), f"{route} does not match {pattern!r}"
            assert pattern == f"^{path}$", (
                f"{route!r} and pattern {pattern!r} name different paths")
            assert method == "GET"

    def test_both_handlers_name_the_other_end_on_their_summary_lines(self):
        """A one-directional pointer is how F2 happened in the first place.

        Standing at either route you must be able to learn the other exists and
        which one answers ``hscc profiles``. The pin is on the SUMMARY BLOCK
        (everything before the first blank line — what ``help()``, an IDE hover
        and a `git grep -A1` show) rather than the body, for the reason F8
        measured: body prose that already mentions the pairing did not stop
        F8, and the same rule applies here
        (``test_kanban_monitor_route_pointer.py``).

        The route names are matched by :func:`_names_route`, so the roster
        cannot satisfy "names the canonical" by containing its OWN longer path.
        """
        for doc, other in ((routes_ops.handle_profiles.__doc__,
                            routes_profiles.PROFILES_ROSTER_ROUTE),
                           (routes_profiles.handle_list.__doc__,
                            routes_ops.PROFILES_STATUS_ROUTE)):
            assert doc, "a handler lost its docstring — nothing to point at"
            summary = doc.split("\n\n", 1)[0]
            assert "hscc profiles" in summary, (
                f"{AUDIT_ROW}: the summary must name the CLI verb the pair is "
                f"about, not just the path: {summary!r}")
            assert _names_route(summary, other), (
                f"{AUDIT_ROW}: the summary must name {other!r} as itself; "
                f"found {summary!r}")

    def test_the_pair_is_documented_at_module_level_too(self):
        """The docstring an agent reads before writing a caller, not just the
        handler docstring it reads after."""
        assert _names_route(routes_ops.__doc__ or "", routes_ops.PROFILES_ROSTER_POINTER)
        assert _names_route(routes_profiles.__doc__ or "",
                            routes_ops.PROFILES_STATUS_ROUTE)
