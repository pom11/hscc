"""Unit tests for the same-session pinning resolver (design §3.1).

Card t_fd031303: a function that maps a registered HSCC project to its NAMED
Hermes session id, reading the owning <project>-orch profile's state.db the
SAME way routes_orchestrator already does for ``--continue``. Both the CLI
chat path and the WS-relay path (and, in the follow-on card t_f731de64, the
gateway driver) resolve a project's session through this primitive so they
always agree on which session a project is.

These tests are hermetic but REAL where it matters: they build an actual
``hermes_state.SessionDB`` against a temp state.db (a genuine sqlite sessions
table with a known session title -> id), and a real temp flightdeck registry
describing a project, then exercise :func:`resolve_named_session_id` and the
``GET /v1/projects/{name}/session`` endpoint over loopback HTTP. No test
touches the operator's real registry, profile tree, or hermes serve.

Acceptance covered:
  * known session -> the correct session id is returned;
  * unknown project -> a distinct "not found" (UnknownProjectError on the
    function; 404 not_found on the endpoint, matching every other
    /v1/projects/{name} endpoint);
  * project with no session yet -> an HONEST no-session result (None on the
    function; has_session:false on the endpoint — never a fake id, never a
    crash).
"""
import os
import types
from pathlib import Path

import pytest

import api_server
import routes_orchestrator
import routes_session  # noqa: F401  (registers the new /session route)
from routes_orchestrator import (
    OrchestratorError,
    UnknownProjectError,
    resolve_named_session_id,
)


# --------------------------------------------------------------------------- #
# Helpers: build a real temp registry + a real temp SessionDB state.db
# --------------------------------------------------------------------------- #

def _write_registry(tmp_path: Path, projects):
    """Write a temp flightdeck registry; return its path string."""
    lines = ["projects:"]
    for p in projects:
        lines.append(f"  - name: {p['name']}")
        lines.append(f"    repo: {p.get('repo')}")
        if p.get("board"):
            lines.append(f"    board: {p['board']}")
        if p.get("session"):
            lines.append(f"    session: {p['session']}")
    reg = tmp_path / "registry.yaml"
    reg.write_text("\n".join(lines) + "\n")
    return str(reg)


def _make_state_db(state_path: Path):
    """Return a fresh hermes_state.SessionDB against ``state_path``."""
    from hermes_state import SessionDB
    return SessionDB(db_path=state_path)


def _seed_session(state_path: Path, title: str, session_id: str,
                  profile: str) -> None:
    """Create + title a real session in the temp state.db, then close it."""
    db = _make_state_db(state_path)
    try:
        db.create_session(session_id, source="cli", model="orchestrator-model",
                          profile_name=profile)
        db.set_session_title(session_id, title)
    finally:
        db.close()


def _point_profile_db_at(monkeypatch, state_path: Path):
    """Make ``resolve_named_session_id`` read the temp state.db.

    The real ``_open_profile_session_db`` resolves the profile's directory via
    ``hermes_cli.profiles`` — the operator's tree, which tests must not touch.
    We swap it for a SessionDB bound to ``state_path`` (the same live seam the
    resolver uses; that boundary function is itself covered by the rest of the
    suite). ``read_only=True`` is honoured by passing it through.
    """
    def _open(profile, read_only=False):
        return _make_state_db(state_path)
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db", _open)


def _configure_registry(running, registry_path: str) -> None:
    """Add the temp registry to the server's api.json so ``_registry_path``
    resolves to it (mirroring how a real deployment configures the registry)."""
    hscc_dir = Path(running.server.ctx.hscc_dir)
    hscc_dir.mkdir(parents=True, exist_ok=True)
    if hscc_dir.joinpath("api.json").exists():
        # merge to avoid clobbering an arbitrary existing config
        import json
        cfg = json.loads(hscc_dir.joinpath("api.json").read_text())
    else:
        cfg = {}
    cfg["registry"] = registry_path
    hscc_dir.joinpath("api.json").write_text(
        __import__("json").dumps(cfg))


# --------------------------------------------------------------------------- #
# Resolver function — direct unit tests
# --------------------------------------------------------------------------- #

def test_resolve_returns_real_session_id(tmp_path, monkeypatch):
    """Known session in a real temp state.db -> the correct session id."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    _seed_session(state, title="hscc", session_id="20260930_real_aaa",
                  profile="hscc-orch")
    _point_profile_db_at(monkeypatch, state)

    profile, title, sid = resolve_named_session_id("hscc", registry_path=reg)
    assert profile == "hscc-orch"
    assert title == "hscc"
    assert sid == "20260930_real_aaa"


def test_resolve_unknown_project_not_found(tmp_path):
    """Unknown project -> distinct UnknownProjectError (the 404 signal)."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    with pytest.raises(UnknownProjectError):
        resolve_named_session_id("bogus", registry_path=reg)


def test_resolve_no_session_honest_none(tmp_path, monkeypatch):
    """Registered project, but the named session row doesn't exist yet ->
    honest no-session (None), never a fake id, never a crash."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "empty-state.db"
    _make_state_db(state).close()   # an existing but empty sessions table
    _point_profile_db_at(monkeypatch, state)

    profile, title, sid = resolve_named_session_id("hscc", registry_path=reg)
    assert profile == "hscc-orch"
    assert title == "hscc"
    assert sid is None


def test_resolve_general_sentinel_is_honest(tmp_path, monkeypatch):
    """None / 'general' resolve to the catch-all orchestrator (no session)."""
    reg = _write_registry(tmp_path, [])
    state = tmp_path / "state.db"
    _make_state_db(state).close()
    _point_profile_db_at(monkeypatch, state)

    for proj in (None, "general"):
        profile, title, sid = resolve_named_session_id(proj, registry_path=reg)
        assert profile == "general-orch"
        assert title == "general"
        assert sid is None


def test_resolve_uses_registry_persisted_session_as_title(tmp_path, monkeypatch):
    """When the registry persists a non-default session id, it is used as the
    named-session TITLE to look up in state.db (the ``--continue`` arg)."""
    # Registry persists session id "hscc-thread-42" for the project.
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc",
         "session": "hscc-thread-42"},
    ])
    state = tmp_path / "state.db"
    _seed_session(state, title="hscc-thread-42", session_id="sid-999",
                  profile="hscc-orch")
    _point_profile_db_at(monkeypatch, state)

    profile, title, sid = resolve_named_session_id("hscc", registry_path=reg)
    assert profile == "hscc-orch"
    assert title == "hscc-thread-42"
    assert sid == "sid-999"


def test_resolve_registry_no_board_is_orchestrator_error(tmp_path):
    """A registry project with no board cannot resolve -> OrchestratorError."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc"},   # no board
    ])
    with pytest.raises(OrchestratorError):
        resolve_named_session_id("hscc", registry_path=reg)


def test_resolve_read_failure_is_no_session_not_fake(tmp_path, monkeypatch):
    """A state.db read failure is reported as no-session (None), never a fake
    id (defaulting to the project name would be exactly the fake the card
    bans)."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])

    def _open_raises(profile, read_only=False):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db",
                        _open_raises)

    profile, title, sid = resolve_named_session_id("hscc", registry_path=reg)
    assert sid is None
    assert profile == "hscc-orch"
    assert title == "hscc"


# --------------------------------------------------------------------------- #
# Endpoint — GET /v1/projects/{name}/session over loopback HTTP
# --------------------------------------------------------------------------- #

@pytest.fixture
def running(tmp_path):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    import threading
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _get(running, token, path):
    import http.client
    conn = http.client.HTTPConnection(running.host, running.port, timeout=5)
    conn.request("GET", path, headers={"Authorization": "Bearer " + token})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    import json
    return resp.status, json.loads(raw) if raw else None


def test_endpoint_unknown_project_404(tmp_path, running, token):
    """Unknown project -> 404 not_found, matching /v1/projects/{name}."""
    _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    _configure_registry(running, str(tmp_path / "registry.yaml"))
    status, payload = _get(running, token, "/v1/projects/bogus/session")
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_endpoint_no_session_honest(tmp_path, running, token, monkeypatch):
    """Known project with no session yet -> has_session false, no fake id."""
    _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    _configure_registry(running, str(tmp_path / "registry.yaml"))
    state = tmp_path / "empty.db"
    _make_state_db(state).close()
    _point_profile_db_at(monkeypatch, state)

    status, payload = _get(running, token, "/v1/projects/hscc/session")
    assert status == 200
    assert payload["project"] == "hscc"
    assert payload["profile"] == "hscc-orch"
    assert payload["session"] is None
    assert payload["has_session"] is False
    assert "not started" in payload["speak"]


def test_endpoint_known_session_returns_id(tmp_path, running, token,
                                           monkeypatch):
    """Known project with a session -> the REAL session id, has_session true."""
    _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    _configure_registry(running, str(tmp_path / "registry.yaml"))
    state = tmp_path / "state.db"
    _seed_session(state, title="hscc", session_id="20260930_ep_xyz",
                  profile="hscc-orch")
    _point_profile_db_at(monkeypatch, state)

    status, payload = _get(running, token, "/v1/projects/hscc/session")
    assert status == 200
    assert payload["project"] == "hscc"
    assert payload["profile"] == "hscc-orch"
    assert payload["session"] == "20260930_ep_xyz"
    assert payload["has_session"] is True
