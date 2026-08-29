"""Unit tests for hscc-api memory viewer (routes_memory.py).

Card t_e8ffd787: "iOS + api: memory viewer — show what a bot remembers about
a profile and let the operator correct or delete a wrong memory."

The suite is hermetic: the profile's memories directory is a REAL temp dir the
test populates with ``MEMORY.md`` / ``USER.md`` in the exact ``\\n§\\n``
delimited format the Hermes ``memory`` tool uses, and ``_memory_dir`` is
monkeypatched so no test touches a real ``~/.hermes/profiles`` or real Hermes
state. Because the file I/O and index math run on genuine files, the tests
exercise the actual parse/serialize paths end-to-end. Handlers are driven over
real loopback HTTP (port 0) like the neighbouring suites, so auth + the route
dispatcher are exercised too.

Coverage required by the card:
  * GET /v1/memory?profile=X lists memory cards with their graph node ids,
    source, title, body and timestamp;
  * list orders MEMORY.md cards first, then USER.md, with the combined global
    index encoded in each node id;
  * a profile with no memory store -> 200 with an empty list + honest speak;
  * POST delete requires confirm (409 otherwise); deletes the right card by
    node id, rewrites the right file, leaves siblings intact;
  * POST edit requires confirm (409); corrects the right card, refuses empty
    content;
  * unknown / stale / malformed node id -> 404 / 400; missing profile -> 400;
  * auth enforced (401) on all three endpoints.
"""
import json
import sys
import threading
import types

import pytest

import api_server
import routes_memory


@pytest.fixture
def running(tmp_path):
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


class _MemoryFixture:
    """A temp memories dir behind routes_memory._memory_dir (hermetic)."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "profiles" / "hscc-orch" / "memories"
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, fname, entries):
        (self.dir / fname).write_text(
            "\n§\n".join(e.strip() for e in entries) if entries else "",
            encoding="utf-8",
        )

    def read(self, fname):
        path = self.dir / fname
        if not path.exists():
            return []
        return [e for e in path.read_text(encoding="utf-8").split("\n§\n") if e.strip()]


@pytest.fixture
def memory(tmp_path, monkeypatch):
    m = _MemoryFixture(tmp_path)
    monkeypatch.setattr(routes_memory, "_memory_dir",
                        lambda profile: m.dir)
    return m


def _request(running, token, method, path, body=None, timeout=5):
    import http.client
    conn = http.client.HTTPConnection(running.host, running.port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        payload: dict = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# GET /v1/memory — list
# --------------------------------------------------------------------------- #

def test_list_reports_cards_with_node_ids_and_order(memory, running, token):
    memory.write("MEMORY.md", [
        "Note A about the cluster",
        "# Note B — project layout",
        "Line one\nLine two\nLine three",
    ])
    memory.write("USER.md", [
        "User prefers concise replies",
    ])
    status, payload = _request(running, token, "GET", "/v1/memory?profile=hscc-orch")
    assert status == 200
    assert payload["profile"] == "hscc-orch"
    assert payload["count"] == 4
    assert payload["memory_count"] == 3
    assert payload["profile_count"] == 1
    cards = payload["memories"]
    # MEMORY.md cards first, then USER.md
    assert [c["source"] for c in cards] == ["memory", "memory", "memory", "profile"]
    # combined global index encoded in each node id
    assert [c["node_id"] for c in cards] == [
        "memory:memory:0", "memory:memory:1", "memory:memory:2", "memory:profile:3",
    ]
    # titles come from the first line (leading '#' stripped, truncated to 80)
    assert cards[0]["title"] == "Note A about the cluster"
    assert cards[1]["title"] == "Note B — project layout"
    # body is the full entry, not truncated (the viewer shows everything)
    assert cards[2]["body"] == "Line one\nLine two\nLine three"
    # timestamp present (real file mtime + chunk index)
    assert isinstance(cards[0]["timestamp"], int)
    assert cards[0]["kind"] == "memory"


def test_list_no_store_200_empty(memory, running, token, monkeypatch):
    monkeypatch.setattr(routes_memory, "_memory_dir",
                        lambda profile: None)
    status, payload = _request(running, token, "GET", "/v1/memory?profile=nope")
    assert status == 200
    assert payload["memories"] == []
    assert payload["count"] == 0
    assert "no memory store" in payload["speak"]


def test_list_missing_profile_400(running, token):
    status, payload = _request(running, token, "GET", "/v1/memory")
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "profile" in payload["error"]["message"]


def test_list_auth_401(running):
    status, _ = _request(running, None, "GET", "/v1/memory?profile=hscc-orch")
    assert status == 401


# --------------------------------------------------------------------------- #
# POST delete — confirm gate + correctness
# --------------------------------------------------------------------------- #

def test_delete_requires_confirm(memory, running, token):
    memory.write("MEMORY.md", ["Note A", "Note B"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:0/delete",
                               body={"profile": "hscc-orch"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert memory.read("MEMORY.md") == ["Note A", "Note B"]  # intact


def test_delete_removes_right_card_and_rewrites_file(memory, running, token):
    memory.write("MEMORY.md", ["Note A", "Note B", "Note C"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:1/delete",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 200
    assert payload["node_id"] == "memory:memory:1"
    assert payload["kind"] == "memory"
    # 'Note B' removed, siblings intact, order preserved
    assert memory.read("MEMORY.md") == ["Note A", "Note C"]


def test_delete_profile_card_uses_global_index(memory, running, token):
    memory.write("MEMORY.md", ["Note A"])
    memory.write("USER.md", ["User pref one", "User pref two"])
    # user pref one is global index 1 (after MEMORY.md's single card)
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:profile:1/delete",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 200
    assert memory.read("USER.md") == ["User pref two"]
    assert memory.read("MEMORY.md") == ["Note A"]  # untouched


def test_delete_exactly_one_entry_when_last(memory, running, token):
    memory.write("MEMORY.md", ["Only note"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:0/delete",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 200
    assert memory.read("MEMORY.md") == []  # emptied cleanly


# --------------------------------------------------------------------------- #
# POST edit — confirm gate + correctness
# --------------------------------------------------------------------------- #

def test_edit_requires_confirm(memory, running, token):
    memory.write("MEMORY.md", ["Note A"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:0/edit",
                               body={"profile": "hscc-orch", "content": "Changed"})
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert memory.read("MEMORY.md") == ["Note A"]  # intact


def test_edit_requires_content(memory, running, token):
    memory.write("MEMORY.md", ["Note A"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:0/edit",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"
    assert "content" in payload["error"]["message"]
    assert memory.read("MEMORY.md") == ["Note A"]


def test_edit_corrects_right_card(memory, running, token):
    memory.write("MEMORY.md", ["Note A", "Wrong gateway IP", "Note C"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:1/edit",
                               body={"profile": "hscc-orch",
                                     "content": "Right gateway IP",
                                     "confirm": True})
    assert status == 200
    assert payload["node_id"] == "memory:memory:1"
    assert payload["previous_title"] == "Wrong gateway IP"
    assert memory.read("MEMORY.md") == ["Note A", "Right gateway IP", "Note C"]


def test_edit_profile_card_uses_global_index(memory, running, token):
    memory.write("USER.md", ["User likes X"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:profile:0/edit",
                               body={"profile": "hscc-orch",
                                     "content": "User likes Y",
                                     "confirm": True})
    assert status == 200
    assert memory.read("USER.md") == ["User likes Y"]


# --------------------------------------------------------------------------- #
# Node id / profile validation
# --------------------------------------------------------------------------- #

def test_delete_malformed_node_id_400(memory, running, token):
    status, payload = _request(running, token, "POST",
                               "/v1/memory/not-a-memory/delete",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_delete_unknown_index_404(memory, running, token):
    memory.write("MEMORY.md", ["Note A"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:5/delete",
                               body={"profile": "hscc-orch", "confirm": True})
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_delete_profile_without_store_404(memory, running, token, monkeypatch):
    monkeypatch.setattr(routes_memory, "_memory_dir", lambda profile: None)
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:0/delete",
                               body={"profile": "nope", "confirm": True})
    assert status == 404
    assert payload["error"]["code"] == "profile_unreachable"


def test_delete_requires_profile_in_body(memory, running, token):
    memory.write("MEMORY.md", ["Note A"])
    status, payload = _request(running, token, "POST",
                               "/v1/memory/memory:memory:0/delete",
                               body={"confirm": True})
    assert status == 400
    assert payload["error"]["code"] == "bad_request"


def test_mutate_auth_401(running):
    status, _ = _request(running, None, "POST",
                         "/v1/memory/memory:memory:0/delete",
                         body={"profile": "hscc-orch", "confirm": True})
    assert status == 401
