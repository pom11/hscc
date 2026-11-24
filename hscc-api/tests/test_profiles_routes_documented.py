"""F2 (residual): the two profile GET routes are NAMED, and NAMED APART, in the
API reference -- ``docs/API.md``.

Audit ``docs/audits/2026-11-23-agent-surface.md:186`` (F2) claimed ``hscc
profiles`` "is answered by TWO routes" and prescribed "pick one canonical, alias
the other". Measured against the payloads that prescription is wrong, and this
card re-measured it from scratch instead of trusting the earlier pass:

  * ``GET /v1/profiles`` (``routes_ops``) -> ``{counts, total_running,
    profiles, speak}`` with ``profiles`` a list of NAMES -- the running-card
    counts, one implementation (``cluster_engine.cmd_profile_status()``), the
    exact call the CLI verb ``hscc profiles`` renders;
  * ``GET /v1/profiles/list`` (``routes_profiles``) -> ``{count, profiles,
    speak}`` with ``profiles`` a list of OBJECTS -- the roster plus the config
    whitelist, and it never reaches the counts implementation.

Two questions, not two answers to one. Both have shipped iOS decoders
(``ProfilesResponse`` reads the counts, ``ProfileListResponse`` is the
create-card assignee picker), so neither route is removable and neither is
deprecable: a sunset note on the roster would tell app developers to migrate to
a route that cannot answer their question. The payload boundary itself is
pinned by ``test_profiles_route_canonical.py`` (19 tests, landed by the
general-board twin of this finding, ``t_34804e61``, as PR #664 / ``15d3cad6``).

What was still false -- and what this module owns -- is the docs half of the
card ("marked ... in docs"). ``docs/API.md``'s Endpoint reference documented
NEITHER route (measured on ``origin/main`` @ ``52494e03``:
``grep -c "/v1/profiles" docs/API.md`` -> ``0``), so an integrator reading the
canonical API reference could not learn that ``GET /v1/profiles`` is the
canonical `hscc profiles` door, nor that the roster is a different question.
A boundary that lives only in module docstrings and a test file is a boundary
only its authors know.

So these tests pin three things:

  * the rows EXIST, with a Path cell naming each route as ITSELF (the Path cell
    is compared for equality, never with ``in`` -- ``/v1/profiles`` is a literal
    prefix of ``/v1/profiles/list``, the trap that let F2 read one answer);
  * the key sets the rows promise are the key sets the HANDLERS actually
    return, computed by calling the handlers over stubbed backings -- a doc that
    drifts from the payload, or a payload that grows a key the doc never
    promised, reddens here rather than in the field;
  * the DISPOSITION is recorded where a reader will meet it: the roster is
    documented as counting nothing, neither row is marked deprecated, each names
    the shipped decoder that makes it load-bearing, and the audit finding itself
    carries the resolution so the pair does not get cut a third time.

Nolive: no server, no socket, no state dir, no subprocess. Both handlers are
called directly with their ``_backing_*`` seams monkeypatched, so nothing reads
a live kanban DB or a real ``~/.hermes/profiles``. ``~/.hscc`` is kept out by
the leg's autouse ``_isolate_hscc`` fixture.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import api_server
import routes_ops
import routes_profiles


AUDIT_DOC = "docs/audits/2026-11-23-agent-surface.md"
API_DOC = "docs/API.md"

#: The heading the new rows sit under, so a rewrite that renames or drops the
#: section is a test failure rather than a silently un-pinned doc.
SECTION = "### Profiles (read)"

COUNTS_PATH = "/v1/profiles"
ROSTER_PATH = "/v1/profiles/list"
SHOW_PATH = "/v1/profiles/{name}"

COUNTS_ANSWER = {
    "counts": {"researcher-a": 2, "architect": 1},
    "total_running": 3,
    "profiles": ["architect", "researcher-a"],
}

ROSTER_ANSWER = [
    {"name": "architect", "is_default": False, "gateway_running": True},
]

_REPO_ROOT = Path(routes_ops.__file__).resolve().parent.parent


def _doc(rel_path: str) -> str:
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"missing {rel_path}"
    return path.read_text(encoding="utf-8")


def _profiles_section(doc: str) -> str:
    """The Profiles section: from its heading to the next ``###`` heading."""
    start = doc.find(SECTION)
    assert start != -1, f"{API_DOC} has no {SECTION!r} section"
    rest = doc[start + len(SECTION):]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


def _rows(section: str) -> dict[str, dict[str, str]]:
    """Markdown table rows keyed by their Path cell, as ``{method, path, desc,
    mutates}``.

    The Path cell is returned verbatim (backticks included) so a caller can
    compare it for EQUALITY -- the whole point of parsing rows instead of
    grepping the section.
    """
    rows: dict[str, dict[str, str]] = {}
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("Method",) or set(cells[0]) <= {"-"}:
            continue
        rows[cells[1]] = {"method": cells[0], "path": cells[1],
                          "desc": cells[2], "mutates": cells[3]}
    return rows


def _brace_groups(text: str) -> list[str]:
    """Every depth-0 ``{...}`` group in ``text``, in order.

    Nested groups (a per-entry shape inside a payload shape) are returned as
    their own group, so a row may legitimately carry ``{count, profiles,
    speak}`` and then ``{name, is_default, ...}``.
    """
    groups: list[str] = []
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
                groups.append(text[start + 1:i])
                start = None
    return groups


def _key_set(group: str) -> set[str]:
    """The top-level key NAMES of one brace group (``a, b: [..], c`` -> {a,b,c})."""
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


def _first_match(path: str, method: str = "GET"):
    """The handler the live dispatcher would run for ``path`` (first match wins)."""
    hits = [(m, p, h) for (m, p, h) in api_server.ROUTES
            if p.match(path) and m == method]
    assert hits, f"nothing registered matches {method} {path}"
    return hits[0][2]


@pytest.fixture
def stubbed(monkeypatch):
    """Stub both backings so the handlers can be called directly and hermetically."""
    monkeypatch.setattr(routes_ops, "_backing_profiles",
                        lambda: dict(COUNTS_ANSWER))
    monkeypatch.setattr(routes_profiles, "_backing_list_profiles",
                        lambda: [dict(p) for p in ROSTER_ANSWER])
    return monkeypatch


# --------------------------------------------------------------------------- #
# 1. The rows exist, and name each route as itself
# --------------------------------------------------------------------------- #

class TestTheRoutesAreInTheApiReference:
    def test_every_profile_read_route_has_its_own_row(self):
        rows = _rows(_profiles_section(_doc(API_DOC)))
        assert set(rows) == {f"`{COUNTS_PATH}`",
                             f"`{ROSTER_PATH}`",
                             f"`{SHOW_PATH}`"}, (
            "the Profiles section of docs/API.md must carry exactly one row per "
            "profile read route; a route added to the dispatch table without a "
            "row here is the F2 failure class (an answer only its author knows)"
        )

    def test_all_three_reads_are_documented_as_reads(self):
        """No mutation door was smuggled into the read table beside them."""
        for row in _rows(_profiles_section(_doc(API_DOC))).values():
            assert row["method"] == "GET", row
            assert row["mutates"] == "read", row

    def test_the_canonical_path_is_named_as_itself_not_as_a_prefix(self):
        """The prefix trap: ``/v1/profiles`` is a prefix of ``/v1/profiles/list``.

        Cell equality is the whole test -- a section that mentioned only the
        roster would satisfy any ``in`` assertion.
        """
        section = _profiles_section(_doc(API_DOC))
        assert _rows(section).get(f"`{COUNTS_PATH}`"), (
            f"{COUNTS_PATH} must appear as its OWN Path cell")
        # And the roster must be named too, so the pair cannot collapse to one.
        assert f"`{ROSTER_PATH}`" in _rows(section)


# --------------------------------------------------------------------------- #
# 2. What the rows promise is what the handlers return
# --------------------------------------------------------------------------- #

class TestTheDocumentedShapesAreTheRealShapes:
    def test_the_counts_row_promises_exactly_the_canonical_payload_keys(
            self, stubbed):
        status, payload = routes_ops.handle_profiles(None, None, {}, None)
        assert status == 200
        row = _rows(_profiles_section(_doc(API_DOC)))
        documented = _key_set(_brace_groups(row[f"`{COUNTS_PATH}`"]["desc"])[0])
        assert documented == set(payload), (
            "docs/API.md promises a different top-level key set than "
            "GET /v1/profiles returns -- update the row and the reason together"
        )

    def test_the_roster_row_promises_exactly_the_roster_payload_keys(
            self, stubbed):
        status, payload = routes_profiles.handle_list(None, None, {}, None)
        assert status == 200
        row = _rows(_profiles_section(_doc(API_DOC)))
        groups = _brace_groups(row[f"`{ROSTER_PATH}`"]["desc"])
        assert len(groups) >= 2, (
            "the roster row must document BOTH the payload keys and the "
            "per-profile entry keys")
        assert _key_set(groups[0]) == set(payload)

    def test_the_roster_entry_keys_are_the_config_whitelist_keys(self, stubbed):
        """The per-entry key set is ``_profile_info_to_dict``'s -- the whitelist.

        A key added to that whitelist (a new field on ``ProfileInfo``) must be
        documented here, because it is a change to what leaves the host.
        """
        real = set(routes_profiles._profile_info_to_dict(
            SimpleNamespace(name="architect"), object()))
        row = _rows(_profiles_section(_doc(API_DOC)))
        documented = _key_set(
            _brace_groups(row[f"`{ROSTER_PATH}`"]["desc"])[1])
        assert documented == real

    def test_the_two_documented_payloads_are_still_different_answers(self):
        """The key-name collision that made F2 read as one question stays stated.

        ``profiles`` means NAMES in one row and OBJECTS in the other; the doc
        must keep saying so, because that is the sentence that stops the next
        reader from opening a "collapse the duplicate" card.
        """
        section = _profiles_section(_doc(API_DOC))
        assert re.search(r"\bNAMES\b.*\bOBJECTS\b", section, re.S), (
            "the section must state that `profiles` is a list of NAMES on one "
            "route and a list of OBJECTS on the other")


# --------------------------------------------------------------------------- #
# 3. The disposition is recorded where a reader meets it
# --------------------------------------------------------------------------- #

class TestDispositionIsRecorded:
    def test_the_doc_names_which_route_is_canonical_for_hscc_profiles(self):
        """A reader at the docs must learn the canonical door, not guess.

        Matched on sentence chunks so the claim is "the same sentence names the
        path and calls it canonical for the counts" -- two unrelated sentences
        in the section would not satisfy it. The path is compared backtick-quoted
        (`` `GET /v1/profiles` ``), which also dodges the prefix trap: the
        roster's own `` `GET /v1/profiles/list` `` cannot match it.
        """
        section = _profiles_section(_doc(API_DOC))
        assert "hscc profiles" in section
        chunks = re.split(r"(?<=[.!?])\s+", section)
        canon = [c for c in chunks
                 if "`GET /v1/profiles`" in c and "canonical" in c.lower()]
        assert canon, (
            "the Profiles section must contain a sentence naming "
            "`GET /v1/profiles` as the canonical route")
        assert any("counts" in c for c in canon), (
            "that sentence must say WHICH question it is canonical for -- the "
            "running-card counts, not the roster")

    def test_the_doc_never_offers_the_roster_as_the_counts_answer(self):
        row = _rows(_profiles_section(_doc(API_DOC)))[f"`{ROSTER_PATH}`"]
        assert "hscc profiles" not in row["desc"], (
            "the roster row must not be described as the hscc profiles answer")
        assert "counts nothing" in row["desc"]
        assert _key_set(_brace_groups(row["desc"])[0]) == {
            "count", "profiles", "speak"}

    def test_neither_route_is_documented_as_deprecated(self, stubbed):
        """F2's disposition is keep-both, and it must stay that way in the docs.

        Both routes have a shipped decoder (``ProfilesResponse`` for the
        counts, ``ProfileListResponse`` for the roster), so a sunset note on
        either would send an integrator to a route that cannot answer their
        question. Each row must keep naming the decoder that makes it
        load-bearing -- that is the evidence, not an opinion.
        """
        rows = _rows(_profiles_section(_doc(API_DOC)))
        # The guard is on the ROWS, not the section prose: a row that says
        # "deprecated, use X" is the deprecation; prose that explains why
        # neither route is one (the F2 disposition) must stay writable.
        for path, row in rows.items():
            for word in ("deprecat", "sunset", "use instead", "end of life"):
                assert word not in row["desc"].lower(), (
                    f"{word!r} marks {path} as retiring -- F2's measured "
                    "disposition is keep-both, and both rows carry a shipped "
                    "decoder that the other route cannot satisfy")
        assert "ProfilesResponse" in rows[f"`{COUNTS_PATH}`"]["desc"]
        assert "ProfileListResponse" in rows[f"`{ROSTER_PATH}`"]["desc"]
        # And they are really still serving -- a doc claim without a route is
        # how a pointer becomes a lie. Resolved through the LIVE dispatch table
        # (first match wins), on the handler identity rather than the path
        # string: `^/v1/profiles/(?P<name>[^/]+)$` also matches
        # /v1/profiles/list, so a path-only check could pass on the wrong route.
        assert _first_match(COUNTS_PATH) is routes_ops.handle_profiles
        assert _first_match(ROSTER_PATH) is routes_profiles.handle_list

    def test_the_audit_finding_records_its_disposition(self):
        """The F2 row in the audit must carry the outcome, or the pair gets cut again.

        The audit's own ownership note (§9) already warned that F2 was cut
        twice -- ``t_34804e61`` (general board) and ``t_3df8595c`` (this card).
        A row that still reads "pick one canonical, alias the other" invites a
        third card to delete a load-bearing route.
        """
        doc = _doc(AUDIT_DOC)
        row = next((ln for ln in doc.splitlines()
                    if ln.startswith("| **F2**")), "")
        assert row, "the F2 row vanished from the findings table"
        assert re.search(r"not a duplicate|premise (is )?false", row, re.I), (
            "the F2 row must record the measured disposition, not the "
            "original prescription")
        for card in ("t_34804e61", "t_3df8595c"):
            assert card in row, f"{card} must be named in the F2 row"
