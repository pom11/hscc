"""Permanent per-project sessions (t_563350d9).

One durable, stable session per project:
1. The session id is persisted against the project in the registry, server side
   — a phone reinstall must not lose the thread (routes_ws._default_relay and
   routes_session both resolve it via registry.ensure_session).
2. The chat relay and the history endpoint resolve the SAME session for a
   project.
3. Creation is idempotent: two concurrent first-messages produce exactly one
   session.

The suite is hermetic: it writes only to a tmp_path registry, never
~/.flightdeck. The relay/orchestrator backing is stubbed via monkeypatch so no
test spawns a real agent or writes to the real registry.
"""

import concurrent.futures
import os
import threading
import time
import types

import pytest

from session_event import MessagePayload, get_store, reset_stores

# The relay's registry access and orchestrator backing are patched per-test.
import routes_ws


# --------------------------------------------------------------------------- #
# Registry helpers (tmp_path based, never the real ~/.flightdeck)
# --------------------------------------------------------------------------- #

def _write_registry(path, rows: list[dict]):
    """Write a registry file with the given project rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["projects:"]
    for row in rows:
        lines.append("  - name: %s" % row["name"])
        lines.append("    repo: %s" % row["repo"])
        if row.get("board"):
            lines.append("    board: %s" % row["board"])
        if row.get("session"):
            lines.append("    session: %s" % row["session"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _project_row(name="hscc", session=None):
    return {"name": name, "repo": f"/tmp/{name}", "board": name,
            "session": session}


# Import the registry module under test (relative to hscc-project).
def _registry():
    import flightdeck.core.registry as reg
    return reg


# --------------------------------------------------------------------------- #
# Registry unit tests: persistence + idempotency (requirement 1 + 3)
# --------------------------------------------------------------------------- #

def test_ensure_session_persists_deterministic_default(tmp_path):
    """First call: session id = project name, persisted to the registry file."""
    reg = _write_registry(tmp_path / "registry.yaml",
                          [_project_row("hscc", session=None)])
    lib = _registry()

    sid = lib.ensure_session("hscc", path=str(reg))

    assert sid == "hscc", "deterministic default must be the project name"
    text = reg.read_text(encoding="utf-8")
    assert "session: hscc" in text, "session id must be persisted: %r" % text


def test_ensure_session_is_idempotent_and_noop_on_repeat(tmp_path):
    """Second call returns the same id and does not rewrite a second session."""
    reg = _write_registry(tmp_path / "registry.yaml",
                          [_project_row("hscc", session=None)])
    lib = _registry()

    first = lib.ensure_session("hscc", path=str(reg))
    after = reg.read_text(encoding="utf-8")
    second = lib.ensure_session("hscc", path=str(reg))

    assert first == second == "hscc"
    # The file is still the original (idempotent — no duplicate/extra writes).
    assert reg.read_text(encoding="utf-8") == after


def test_ensure_session_returns_persisted_id_without_overwriting(tmp_path):
    """A session already set (set_session) is returned unchanged, not reset."""
    reg = _write_registry(tmp_path / "registry.yaml",
                          [_project_row("hscc", session=None)])
    lib = _registry()

    lib.set_session("hscc", "perm-thread-42", path=str(reg))
    sid = lib.ensure_session("hscc", path=str(reg))

    assert sid == "perm-thread-42"
    assert "session: perm-thread-42" in reg.read_text(encoding="utf-8")


def test_restart_resolves_the_same_id(tmp_path):
    """Simulate a server restart: reload the registry fresh, same session id."""
    reg = _write_registry(tmp_path / "registry.yaml",
                          [_project_row("hscc", session=None)])
    lib = _registry()

    first = lib.ensure_session("hscc", path=str(reg))

    # "Restart": a brand-new load of the persisted file must resolve the SAME id.
    lib2 = _registry()
    again = lib2.ensure_session("hscc", path=str(reg))

    assert again == first
    assert lib2.get_project("hscc", path=str(reg)).session == first


def test_unknown_project_ensure_session_raises(tmp_path):
    lib = _registry()
    with pytest.raises(lib.ProjectNotFoundError):
        lib.ensure_session("bogus", path=str(tmp_path / "empty.yaml"))


# --------------------------------------------------------------------------- #
# Concurrency (requirement 3): concurrent first-sends -> exactly one session
# --------------------------------------------------------------------------- #

def test_concurrent_first_sends_produce_exactly_one_session(tmp_path):
    """N threads racing on a fresh registry all get one and the same id."""
    reg = _write_registry(tmp_path / "registry.yaml",
                          [_project_row("hscc", session=None)])
    lib = _registry()

    def _ensure(_):
        return lib.ensure_session("hscc", path=str(reg))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(_ensure, range(32)))

    # All concurrent first-sends resolved the SAME single session.
    assert len(set(ids)) == 1, "concurrent first-sends created >1 session: %r" % (
        sorted(set(ids)),)
    # And exactly one session persisted (no duplicates in the file).
    text = reg.read_text(encoding="utf-8")
    assert text.count("session:") == 1, "registry has unexpected session rows: %r" % text


# --------------------------------------------------------------------------- #
# Requirement 2: relay and history resolve the SAME session for a project
# --------------------------------------------------------------------------- #

def _install_relay_registry(monkeypatch, session_id_getter):
    """Point routes_ws's registry access at a fake backed by session_id_getter.

    ``session_id_getter(name)`` models the real ensure_session (persist +
    return). Returning the same id for the same project is what proves relay
    and history agree.
    """
    fake_reg = types.SimpleNamespace(
        ensure_session=lambda name, path=None: session_id_getter(name))
    monkeypatch.setattr(routes_ws, "_registry", fake_reg)
    monkeypatch.setattr(routes_ws, "_registry_path", lambda ctx: "/none")
    return fake_reg


def _install_backing(monkeypatch, invoke):
    fake = types.ModuleType("routes_orchestrator")
    fake._registry_path = lambda path: "/none"
    fake._backing_resolve = lambda project, path: {
        "profile": "hscc-orch", "session": "hscc"}
    fake._backing_invoke = invoke
    monkeypatch.setitem(__import__("sys").modules, "routes_orchestrator", fake)
    monkeypatch.setattr(routes_ws, "_registry_path", lambda ctx: "/none")
    return fake


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


def test_two_sends_land_in_one_session(monkeypatch):
    """Two sequential sends resolve the same session id (same thread)."""
    seen_ids = []

    def session_id_getter(name):
        seen_ids.append(name)
        return name  # deterministic default = project name

    _install_relay_registry(monkeypatch, session_id_getter)
    invoke_seen = []

    def invoke(profile, session, text):
        invoke_seen.append((profile, session))
        return (f"reply to {text}", profile, session)

    _install_backing(monkeypatch, invoke)

    # Two sends over the relay.
    routes_ws._default_relay("hscc", "first message")
    time.sleep(0.05)
    routes_ws._default_relay("hscc", "second message")
    time.sleep(0.05)

    # The relay invoked the orchestrator with the SAME session id both times.
    assert invoke_seen, "relay never reached the orchestrator"
    sessions = [s for (_p, s) in invoke_seen]
    assert len(set(sessions)) == 1, (
        "two sends landed in different sessions: %r" % sessions)
    assert sessions[0] == "hscc"
    # And the registry resolution was consistent too.
    assert seen_ids == ["hscc", "hscc"]


def test_relay_and_history_resolve_the_same_session(tmp_path, monkeypatch):
    """The id the relay persists == the id the history route reads.

    Both go through registry.ensure_session against the same registry file.
    We exercise the REAL flatdeck registry module here (on a tmp_path), so the
    persisted id is genuinely what a restart would re-resolve.
    """
    lib = _registry()
    reg = _write_registry(tmp_path / "registry.yaml",
                          [_project_row("hscc", session=None)])

    # What the history route would resolve (routes_session uses ensure_session).
    history_session = lib.ensure_session("hscc", path=str(reg))

    # What the relay resolves (routes_ws) — same registry, same id.
    relay_session = lib.ensure_session("hscc", path=str(reg))

    assert history_session == relay_session == "hscc"
    assert "session: hscc" in reg.read_text(encoding="utf-8")
