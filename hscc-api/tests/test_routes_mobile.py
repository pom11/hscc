"""Tests for GET /v1/mobile/summary — the compact board-status read (t_8580f9a3).

The contract under test is ``ios-app/docs/WIDGET_DATA_CONTRACT.md`` §6, and each
test names the rule it enforces. Hermetic: the two backing seams
(``_backing_list_boards`` / ``_backing_list_cards``) are monkeypatched, so no
test scans or mutates a live kanban DB. Requests go over real loopback HTTP
(port 0) so the central auth gate and the router are exercised end-to-end.

The failure this route exists to kill is a *plausible wrong number*: ``done``
missing from a payload and therefore rendered as ``0``. Tests 3-5 are the
anti-regression core — if someone "optimises" the route back to the
non-archived listing, they go red here.
"""

import contextlib
import http.client
import json
import re
import sqlite3
import sys
import threading
import types
from pathlib import Path

import pytest

import api_server
import routes_mobile


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _card(board, status, idx):
    """One card, deliberately carrying every field a real card has.

    The fat and the dangerous fields (``body``, ``workspace_path``, ``repo``)
    are present on purpose: the redaction tests below prove none of them can
    reach the response. The values are synthetic (documentation-range address,
    non-home path) — the point is the FIELD, not a real path.
    """
    return {
        "id": f"t_{board}{idx}",
        "board": board,
        "title": f"card {idx} on {board}",
        "body": "X" * 400,
        "status": status,
        "assignee": "ios-engineer",
        "workspace_path": "/home/operator/dev/hscc/.worktrees/t_deadbeef",
        "repo": "/home/operator/dev/hscc",
        "branch": "wt/t_deadbeef",
    }


def _fleet(board_names, per_status=2, extra_status=None):
    """A deterministic synthetic fleet: `per_status` cards for each status."""
    statuses = list(routes_mobile.COUNT_STATUSES)
    if extra_status:
        statuses.append(extra_status)
    cards = []
    for board in board_names:
        for status in statuses:
            for i in range(per_status):
                cards.append(_card(board, status, len(cards) + i))
    return cards


@pytest.fixture
def fleet(monkeypatch):
    """Boards + cards fakes, with the calls recorded."""
    calls = {"boards": 0, "cards": []}
    state = {"boards": ["hscc", "radio", "default"],
             "cards": _fleet(["hscc", "radio", "default"], per_status=2,
                             extra_status="review"),
             "boom_boards": False,
             "boom_cards_for": set()}

    def list_boards():
        calls["boards"] += 1
        if state["boom_boards"]:
            raise RuntimeError("board db locked")
        return list(state["boards"])

    def list_cards(board=None, **kwargs):
        calls["cards"].append((board, dict(kwargs)))
        if board in state["boom_cards_for"]:
            raise RuntimeError("board db locked")
        if board is None:
            return list(state["cards"])
        return [c for c in state["cards"] if c["board"] == board]

    monkeypatch.setattr(routes_mobile, "_backing_list_boards", list_boards)
    monkeypatch.setattr(routes_mobile, "_backing_list_cards", list_cards)
    state["calls"] = calls
    return state


@pytest.fixture
def running(tmp_path, fleet):
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


def _raw(running, token, path, method="GET"):
    """One request; returns (status, raw-body-bytes)."""
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    status = resp.status
    conn.close()
    return status, data


def _get(running, token, path):
    status, data = _raw(running, token, path)
    return status, json.loads(data or b"{}"), data


# --------------------------------------------------------------------------- #
# Rule 6 — read-only, one GET, guarded by the ONE central auth gate
# --------------------------------------------------------------------------- #

def test_route_registered_as_get_only():
    """Only a GET is registered: this route cannot mutate anything."""
    matches = [(m, h) for (m, p, h) in api_server.ROUTES
               if p.pattern == r"^/v1/mobile/summary$"]
    assert [m for m, _ in matches] == ["GET"], matches
    assert matches[0][1] is routes_mobile.handle_mobile_summary


def test_post_is_rejected_405(running, token, fleet):
    """No mutating verb exists on the path — 405, not an accepted no-op."""
    status, _, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    before = (fleet["calls"]["boards"], len(fleet["calls"]["cards"]))
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    conn.request("POST", "/v1/mobile/summary",
                 body=json.dumps({"confirm": True}).encode(),
                 headers={"Authorization": "Bearer " + token,
                          "Content-Type": "application/json"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 405, body
    assert body["error"]["code"] == "method_not_allowed"
    # The refused POST touched nothing: no board enumeration, no card read, and
    # therefore nothing that could have mutated a board.
    assert (fleet["calls"]["boards"], len(fleet["calls"]["cards"])) == before


def test_auth_required_401_without_token(running):
    """The central gate covers the new route (same auth surface, rule 6)."""
    status, data = _raw(running, None, "/v1/mobile/summary")
    assert status == 401
    assert json.loads(data)["error"]["code"] == "unauthorized"
    status, data = _raw(running, "wrong-token", "/v1/mobile/summary")
    assert status == 401


# --------------------------------------------------------------------------- #
# Rule 1 — every status key always present as an integer; done is never absent
# --------------------------------------------------------------------------- #

def test_every_board_carries_all_status_keys(running, token, fleet):
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert [b["board"] for b in payload["boards"]] == ["default", "hscc", "radio"]
    for entry in payload["boards"]:
        assert sorted(entry["counts"]) == sorted(routes_mobile.COUNT_STATUSES)
        assert all(isinstance(v, int) for v in entry["counts"].values()), entry
        # total is the honest sum of what is reported, nothing silently dropped.
        assert entry["total"] == sum(entry["counts"].values()) \
            + sum((entry.get("other") or {}).values()), entry


def test_board_with_no_cards_is_real_zeros_not_an_absence(running, token, fleet):
    """`total: 0` and six zeros is a MEASURED fact; a missing counts map is not."""
    fleet["cards"] = [c for c in fleet["cards"] if c["board"] != "radio"]
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    entry = next(b for b in payload["boards"] if b["board"] == "radio")
    assert entry["total"] == 0
    assert entry["counts"] == {k: 0 for k in routes_mobile.COUNT_STATUSES}
    assert "other" not in entry


def test_done_and_archived_are_counted_from_the_full_listing(running, token, fleet):
    """`done`/`archived` show up as real numbers, from cards the client never sees."""
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    hscc = next(b for b in payload["boards"] if b["board"] == "hscc")
    assert hscc["counts"]["done"] == 2, hscc
    assert hscc["counts"]["archived"] == 2, hscc
    assert hscc["total"] >= hscc["counts"]["done"] + hscc["counts"]["archived"]


def test_seam_asks_flightdeck_for_the_archived_inclusive_listing(monkeypatch):
    """THE reason this route exists, asserted at the seam that talks to flightdeck.

    ``done``/``archived`` are visible ONLY through ``include_archived=True``;
    the non-archived listing is exactly what made every existing route answer
    ``done`` = 0 (§5.2 of the contract). A refactor that drops the flag goes red
    here instead of shipping a widget that renders `done` as 0 forever.
    """
    seen = []

    class FakeKanban:
        def list_boards(self):
            return ["hscc"]

        def list_cards(self, board=None, *, include_archived=False):
            seen.append({"board": board, "include_archived": include_archived})
            return []

    monkeypatch.setattr(routes_mobile, "_kanban", FakeKanban())
    assert routes_mobile._backing_list_boards() == ["hscc"]
    routes_mobile._backing_list_cards()
    routes_mobile._backing_list_cards(board="hscc")
    assert len(seen) == 2, seen
    assert [c["board"] for c in seen] == [None, "hscc"]
    for call in seen:
        assert call["include_archived"] is True, call


def test_done_zero_is_present_and_distinguishable(running, token, fleet):
    """A real zero stays in the payload (contract §6 rule 1)."""
    fleet["cards"] = [c for c in fleet["cards"] if c["status"] != "done"]
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    entry = next(b for b in payload["boards"] if b["board"] == "hscc")
    assert "done" in entry["counts"] and entry["counts"]["done"] == 0


def test_unknown_status_is_reported_not_dropped(running, token, fleet):
    """A status the widget does not draw still counts towards `total`."""
    fleet["cards"] = [_card("hscc", "triage", 1), _card("hscc", "triage", 2),
                      _card("hscc", "todo", 3)]
    status, payload, _ = _get(running, token, "/v1/mobile/summary?boards=hscc")
    entry = payload["boards"][0]
    assert entry["other"] == {"triage": 2}, entry
    assert entry["total"] == 3


# --------------------------------------------------------------------------- #
# Rule 2 — one request for every board; ?boards= narrows it
# --------------------------------------------------------------------------- #

def test_default_is_one_cards_read_for_all_boards(running, token, fleet):
    """No N+1: five boards must not cost five reads."""
    fleet["boards"] = [f"b{i}" for i in range(5)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=1)
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert payload["board_count"] == 5
    assert len(fleet["calls"]["cards"]) == 1, fleet["calls"]["cards"]
    assert fleet["calls"]["cards"][0][0] is None  # board=None == every board


def test_boards_param_narrows_dedups_and_sorts(running, token, fleet):
    status, payload, _ = _get(
        running, token, "/v1/mobile/summary?boards=radio,hscc,radio")
    assert payload["board_count"] == 2
    assert [b["board"] for b in payload["boards"]] == ["hscc", "radio"]
    assert payload["total_boards"] == 2
    # Narrowing really is server-side: the unread board is not in the payload.
    assert "default" not in json.dumps(payload)


def test_unknown_board_404_with_error_envelope(running, token, fleet):
    status, payload, _ = _get(running, token, "/v1/mobile/summary?boards=nope")
    assert status == 404
    err = payload["error"]
    assert set(err) == {"code", "message", "speak"}
    assert err["code"] == "not_found"
    assert "nope" in err["speak"]


# --------------------------------------------------------------------------- #
# Rule 3 — the unauthorised optional blocks are refused, not ignored
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("param,expected", [
    ("rows=5", "rows"),
    ("with=cluster", "with"),
    ("with=kanban,projects", "with"),
])
def test_unimplemented_blocks_are_400_not_silently_ignored(running, token,
                                                            param, expected):
    """A client that asked for rows and got none must be told why (rule 3)."""
    status, payload, _ = _get(running, token, f"/v1/mobile/summary?{param}")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert expected in payload["error"]["message"]


@pytest.mark.parametrize("raw", [
    "../etc/passwd",
    "100.64.0.1",
    "a/b",            # a path separator, however the client encodes it
    "board name",     # whitespace
    "x" * 65,         # over the slug length cap
    "tok=abcdef1234567890abcdef",   # key=value material cannot ride a slug
])
def test_malformed_board_name_400(running, token, raw):
    """The board name is the only string echoed, so it is validated at the gate."""
    from urllib.parse import quote
    status, payload, _ = _get(
        running, token, f"/v1/mobile/summary?boards={quote(raw, safe='')}")
    assert status == 400, raw
    assert payload["error"]["code"] == "bad_request"


def test_too_many_boards_400(running, token, fleet):
    raw = ",".join(f"b{i}" for i in range(routes_mobile.MAX_REQUESTED_BOARDS + 1))
    status, payload, _ = _get(running, token, f"/v1/mobile/summary?boards={raw}")
    assert status == 400
    assert "too many boards" in payload["error"]["message"].lower()


# --------------------------------------------------------------------------- #
# Rule 5 — the 4 KB ceiling, asserted with a synthetic 50-board / 5,000-card host
# --------------------------------------------------------------------------- #

def test_budget_ceiling_with_50_boards_and_5000_cards(running, token, fleet):
    fleet["boards"] = [f"board{i:02d}" for i in range(50)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=833)  # ~5,000 cards
    assert len(fleet["cards"]) >= 5000
    status, _, raw = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert len(raw) <= routes_mobile.MAX_RESPONSE_BYTES, len(raw)
    payload = json.loads(raw)
    assert payload["truncated"] is True
    assert payload["omitted_boards"] == 50 - payload["board_count"] > 0
    assert payload["total_boards"] == 50
    # Trimming must not break rule 1 on the boards that ARE served.
    for entry in payload["boards"]:
        assert sorted(entry["counts"]) == sorted(routes_mobile.COUNT_STATUSES)
    # And the 5,000 cards must not be in the payload in any form.
    text = raw.decode()
    for forbidden in ("workspace_path", "branch", "body", "t_board", "assignee"):
        assert forbidden not in text, forbidden


def test_boards_scoping_caps_the_size_too(running, token, fleet):
    fleet["boards"] = [f"board{i:02d}" for i in range(50)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=833)
    status, wide, _ = _get(running, token, "/v1/mobile/summary")
    status, _, narrow_raw = _get(running, token, "/v1/mobile/summary?boards=board07")
    narrow = json.loads(narrow_raw)
    assert narrow["board_count"] == 1
    assert "truncated" not in narrow
    assert len(narrow_raw) < 700, len(narrow_raw)
    assert len(narrow_raw) < len(
        json.dumps(wide).encode()), "scoping did not reduce the response"


# --------------------------------------------------------------------------- #
# Rule 4 — redaction is structural: no path, address, tenant id or token
# --------------------------------------------------------------------------- #

def test_no_path_address_or_token_can_reach_the_payload(running, token, fleet):
    """Cards full of forbidden fields; none of it may appear in the response.

    This is the structural guarantee expressed as a test: the handler never
    reads those fields, so they cannot leak — even when a card body carries a
    LAN address and a bearer token, and the host also has a tailnet-address-
    shaped and a path-shaped "board" in its registry.

    What IS allowed by contract §6 rule 4 is the board *name*, which the app's
    own UI already shows; that is why the slug shape gate (not a secret-scrub)
    is the redaction here.
    """
    fleet["boards"] = ["hscc", "100.64.0.1", "10.0.0.x",
                       "/home/operator/dev/x", "token=supersecretvalue12345"]
    fat = _fleet(["hscc"], per_status=1)
    for c in fat:
        c["body"] += (" node 203.0.113.9 token=supersecretvalue12345 "
                      "Bearer abc.def")
    fleet["cards"] = fat
    status, _, raw = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    text = raw.decode()
    # Every IP-shaped token the API's own redactor recognises must be ABSENT,
    # not merely rewritten — nothing was scrubbed because nothing was read.
    # (Addresses here are the documented placeholders / TEST-NET-3, so a match
    # would be the payload echoing something it never reads.)
    import routes_logs
    for name in ("_TAILNET_RE", "_RFC1918_RE", "_OTHER_IP_RE"):
        assert not getattr(routes_logs, name).search(text), name
    for needle in ("/home/operator", "workspace_path", "supersecretvalue12345",
                   "Bearer", "repo", "branch", "body", "title", "assignee"):
        assert needle not in text, needle
    payload = json.loads(raw)
    # The four malformed slugs are not echoed; only their COUNT is honest.
    assert payload["unreadable_boards"] == 4
    assert [b["board"] for b in payload["boards"]] == ["hscc"]


def test_unreadable_slug_count_is_exact(running, token, fleet):
    """`unreadable_boards` counts slugs the shape gate refused — nothing else."""
    fleet["boards"] = ["ok", "bad slug", "10.0.0.x", "/tmp/x", ""]
    fleet["cards"] = []
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert payload["board_count"] == 1
    assert payload["unreadable_boards"] == 4
    assert "bad slug" not in json.dumps(payload)
    assert "/tmp/x" not in json.dumps(payload)


# --------------------------------------------------------------------------- #
# Rule 7 — honest degradation
# --------------------------------------------------------------------------- #

def test_board_listing_failure_omits_boards_entirely(running, token, fleet):
    """No `boards` key at all: an empty counts map reads as "quiet", which is a lie."""
    fleet["boom_boards"] = True
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert "boards" not in payload
    assert "counts" not in json.dumps(payload)
    assert payload["speak"] == "Board listing unavailable."
    assert payload["generated_at"]


def test_cards_failure_degrades_without_fabricating_zeros(running, token, fleet):
    """The cards read is what fails here — `boards` must not be served as zeros."""
    fleet["boom_cards_for"] = {None}     # the all-boards read
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert "boards" not in payload          # the key itself, not an empty list
    assert "counts" not in payload          # speak says "counts", the payload has none
    assert payload["speak"] == "Board counts unavailable."


def test_one_unreadable_board_does_not_poison_the_others(running, token, fleet):
    """`?boards=` reads each board separately, so failure is per-board."""
    fleet["boom_cards_for"] = {"radio"}
    status, payload, _ = _get(
        running, token, "/v1/mobile/summary?boards=hscc,radio")
    assert status == 200
    by_board = {b["board"]: b for b in payload["boards"]}
    assert "counts" in by_board["hscc"]
    assert "counts" not in by_board["radio"]
    assert by_board["radio"]["error"] == routes_mobile.COUNTS_UNAVAILABLE
    # The speak line says one board was unavailable, and does not present its
    # absence as a zero.
    assert "1 board unavailable." in payload["speak"]


# --------------------------------------------------------------------------- #
# speak (design §B) + freshness stamp
# --------------------------------------------------------------------------- #

def test_speak_matches_the_contract_example_math(running, token, fleet):
    """`open` = total - done - archived, exactly the §6 example (17+0+3+0 = 20)."""
    fleet["boards"] = ["hscc"]
    fleet["cards"] = (
        [_card("hscc", "todo", i) for i in range(17)]
        + [_card("hscc", "running", i) for i in range(3)]
        + [_card("hscc", "done", i) for i in range(750)])
    status, payload, _ = _get(running, token, "/v1/mobile/summary?boards=hscc")
    assert payload["speak"] == "hscc: 20 open, 3 running, 750 done."


def test_speak_names_the_omitted_boards_but_keeps_true_totals(running, token, fleet):
    fleet["boards"] = [f"board{i:02d}" for i in range(50)]
    fleet["cards"] = _fleet(fleet["boards"], per_status=833)
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert payload["truncated"] is True
    # The spoken totals cover ALL 50 boards, not just the served slice.
    assert payload["speak"].startswith("50 boards: ")
    assert "omitted for size." in payload["speak"]


def test_speak_present_and_generated_at_is_utc_z(running, token, fleet):
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert payload["speak"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                        payload["generated_at"]), payload["generated_at"]


def test_no_boards_at_all_says_so(running, token, fleet):
    fleet["boards"] = []
    fleet["cards"] = []
    status, payload, _ = _get(running, token, "/v1/mobile/summary")
    assert status == 200
    assert payload["boards"] == []
    assert payload["board_count"] == 0
    assert payload["speak"] == "No boards."


# --------------------------------------------------------------------------- #
# CLI parity — the route and `hscc kanban monitor` on the SAME fixture state
# --------------------------------------------------------------------------- #

def test_counts_match_hscc_kanban_monitor_on_the_same_board(tmp_path, monkeypatch):
    """One real board DB, two entry points: route counts == CLI board counts.

    ``hscc kanban monitor --json`` is the CLI's board view and is served by
    ``hscc_daemon.kanban_live.collect`` (``kanban_cli._cmd_monitor``). This
    builds the SAME tiny board for both: the route reads it through its
    flightdeck seam, the CLI through its own SQLite pass. Any future divergence
    in status handling, archived inclusion, or the ``other`` bucket goes red.
    """
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from hscc_daemon import kanban_live

    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT, title TEXT, status TEXT, assignee TEXT, priority INTEGER,
            created_at INTEGER, started_at INTEGER, completed_at INTEGER,
            last_heartbeat_at INTEGER, current_run_id TEXT);
        CREATE TABLE task_runs (
            id TEXT, profile TEXT, started_at INTEGER,
            last_heartbeat_at INTEGER);
        CREATE TABLE task_events (
            task_id TEXT, kind TEXT, created_at INTEGER);
        """
    )
    statuses = ["todo", "todo", "ready", "running", "blocked", "done", "done",
                "archived", "review"]
    for i, status in enumerate(statuses):
        conn.execute(
            "INSERT INTO tasks (id, title, status, assignee, priority, "
            "created_at, started_at, completed_at, last_heartbeat_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (f"t_{i}", f"card {i}", status, "worker", 0, 1000 + i, None,
             None, None),
        )
    conn.commit()
    conn.close()

    class FakeKdb:
        """Single-board kanban lib shape: no ``list_boards`` => legacy board."""

        def connect_closing(self, board=None):
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            return contextlib.closing(conn)

    def list_cards(board=None, include_archived=False):
        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM tasks").fetchall()
        return [{"id": r["id"], "board": "default", "status": r["status"],
                 "title": r["title"], "created_at": r["created_at"],
                 "started_at": r["started_at"],
                 "completed_at": r["completed_at"],
                 "last_heartbeat_at": r["last_heartbeat_at"]}
                for r in rows]

    monkeypatch.setattr(routes_mobile, "_backing_list_boards",
                        lambda: ["default"])
    monkeypatch.setattr(routes_mobile, "_backing_list_cards", list_cards)

    status, payload = routes_mobile.handle_mobile_summary(None, None, {}, b"")
    assert status == 200
    route = payload["boards"][0]
    route_counts = dict(route["counts"])
    route_counts.update(route.get("other") or {})

    cli = kanban_live.collect(kanban_db=FakeKdb(), read_caps=lambda: (None, None))
    cli_counts = {b["name"]: b["counts"] for b in cli["boards"]}["default"]

    assert route_counts == cli_counts
    assert route["total"] == sum(cli_counts.values()) == len(statuses)
    assert route["counts"]["done"] == 2
    assert route["counts"]["archived"] == 1
