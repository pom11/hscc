"""Unit tests for the gateway-driver full-history backfill (design §3.2)
and the same-session-pinned PTY relay wiring (design §3.1/§3.3).

Card t_f731de64. Building on the parent's same-session pinning resolver
(t_fd031303, ``resolve_named_session_id``), these tests cover:

  * Acceptance 1 — backfill seeds the store from the named session's REAL
    history in state.db, so history is present before any live event.
  * Acceptance 2 — backfill is IDEMPOTENT (keyed on session id + store
    high-water mark): re-backfilling to an unchanged store (e.g. a reconnect)
    does not duplicate frames and seq stays contiguous.
  * Acceptance 4 (unit half) — a backfill failure (unreachable state.db /
    read failure / no session) logs + reports a ``skipped`` reason and never
    breaks the live feed; a later (re)connect retries and succeeds.

Test 3 (App→session + session→CLI two-way) is the live probe — the
``tests_gateway/`` harness — not a unit test, because it needs a real
``hermes serve``. Those are manual/integration (see the card body).

Hermetic: every test writes ONLY into a tmp_path registry + a tmp_path
state.db; ``_open_profile_session_db`` is redirected at the temp state.db via
monkeypatch, exactly like test_session_resolver.py, so no test touches the
operator's real profile tree, registry, or hermes serve.
"""
import json
import time
import types
from pathlib import Path

import pytest

import gateway_driver as gd
from session_event import get_store, reset_stores
from gateway_driver import GatewayConfig, GatewayDriver, backfill_named_session

# ``hermes_state`` ships only with the hermes-agent runtime (the hermes venv).
# The alternate p313 interpreter (scripts/run_tests.sh HSCC_TEST_PY=conda p313)
# lacks it, so tests that build a real SessionDB state.db must SKIP there —
# same honest-optional pattern as test_gateway_driver's corpus skip — while
# running fully under the hermes venv. (The parent's test_session_resolver has
# the identical dependency; this is a documented env gap, not a regression.)
try:
    from hermes_state import SessionDB as _SessionDB  # noqa: F401
    _HAS_HERMES_STATE = True
except Exception:
    _HAS_HERMES_STATE = False


@pytest.fixture
def hs_state():
    """Skip the whole state-db-backed test when hermes_state is unavailable."""
    if not _HAS_HERMES_STATE:
        pytest.skip("hermes_state not importable on this interpreter "
                    "(needs the hermes-agent runtime / hermes venv)")
    yield


# --------------------------------------------------------------------------- #
# Helpers: build a real temp registry + a real temp SessionDB state.db with a
# known message history (the same seams the resolver + backfill use).
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
    from hermes_state import SessionDB
    return SessionDB(db_path=state_path)


def _seed_history(state_path: Path, session_id: str, title: str,
                  profile: str, messages: list) -> None:
    """Create a populated named session: session row + a known message history.

    ``messages`` are real Hermes message rows (role/content/timestamp + tool
    fields where relevant), written via SessionDB so ``backfill_named_session``
    reads a genuine store — the same seam ``hermes -p <profile> chat`` persists
    to. Closes the DB afterwards (mirrors _seed_session).
    """
    db = _make_state_db(state_path)
    try:
        db.create_session(session_id, source="cli", model="orchestrator-model",
                          profile_name=profile)
        db.set_session_title(session_id, title)
        if messages:
            db.append_messages_batch(session_id, messages)
    finally:
        db.close()


def _point_profile_db_at(monkeypatch, state_path: Path):
    """Redirect both the resolver and the backfill at the temp state.db.

    The real ``_open_profile_session_db`` resolves the profile's dir through
    ``hermes_cli.profiles`` — the operator's tree, which tests must not touch.
    We swap it for a SessionDB bound to ``state_path`` (the same live seam);
    it honours ``read_only``. ``resolve_named_session_id`` calls this too, so
    one redirect covers the resolver + the backfill's history read.
    """
    def _open(profile, read_only=False):
        return _make_state_db(state_path)
    import routes_orchestrator
    # ``backfill_named_session`` and ``_resolve_named_session`` import
    # ``_open_profile_session_db`` / ``resolve_named_session_id`` from
    # routes_orchestrator at call time, so patching the RO module's symbol
    # is the one redirect both the resolver and the backfill's read use.
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db", _open)


@pytest.fixture(autouse=True)
def clean_state():
    reset_stores()
    gd.reset_backfill_state()
    yield
    reset_stores()
    gd.reset_backfill_state()


def _known_history():
    """A representative named-session history: user, assistant, tool, user.

    Returns (session_id, messages). Fixed epoch timestamps (NOT time.time())
    make the ts-preservation assertion deterministic across runs/interpreters.
    Mirror the shapes hermes persists so the translation is exercising real
    rows, not fabricated ones.
    """
    sid = "20260930_backfill_aaa"
    # Fixed epoch seconds far in the past (no clock dependence in assertions).
    t_user = 1759200000
    t_assistant = 1759200050
    t_tool = 1759200100
    t_user2 = 1759200200
    messages = [
        {"role": "user", "content": "hello from phone", "timestamp": t_user},
        {"role": "assistant", "content": "hello phone!",
         "timestamp": t_assistant},
        {"role": "tool", "content": '{"output": "42"}',
         "tool_call_id": "call_seed_1", "tool_name": "terminal",
         "tool_calls": [{"id": "call_seed_1", "name": "terminal",
                         "input": {"command": "echo 42"}}],
         "timestamp": t_tool},
        {"role": "user", "content": "again", "timestamp": t_user2},
    ]
    return sid, messages


# --------------------------------------------------------------------------- #
# Acceptance 1 — backfill seeds the FULL history into the store
# --------------------------------------------------------------------------- #

def test_backfill_seeds_full_history(tmp_path, monkeypatch, hs_state):
    """Connecting a project backfills the store from the named session's real
    history in state.db → store contains the whole conversation immediately,
    before any live event."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    sid, messages = _known_history()
    _seed_history(state, sid, title="hscc", profile="hscc-orch",
                  messages=messages)
    _point_profile_db_at(monkeypatch, state)

    result = backfill_named_session("hscc", registry_path=reg)

    assert result["session"] == sid
    assert result["backfilled"] == 4
    assert result.get("skipped") is None

    store = get_store("hscc")
    # Full history present in the store (the whole conversation).
    evs = store.history()["events"]
    assert len(evs) == 4
    # One contiguous seq space starting at 1.
    assert [e["seq"] for e in evs] == [1, 2, 3, 4]
    # Message frames carry the full text + done=True (complete turns).
    assert evs[0]["type"] == "message"
    assert evs[0]["payload"] == {"role": "user", "delta": "hello from phone",
                                 "done": True}
    assert evs[1]["payload"] == {"role": "assistant",
                                 "delta": "hello phone!", "done": True}
    # Tool frame is a finished tool_call with name + result + args preview.
    assert evs[2]["type"] == "tool_call"
    assert evs[2]["payload"]["status"] == "finish"
    assert evs[2]["payload"]["name"] == "terminal"
    assert evs[2]["payload"]["call_id"] == "call_seed_1"
    assert evs[2]["payload"]["args"] == {"command": "echo 42"}
    assert '\\"output\\": \\"42\\"' in json.dumps(evs[2]["payload"]["result"])
    # Timestamps preserved (history frames show real message times).
    expected_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1759200000))
    assert evs[0]["ts"] == expected_ts

    # High-water matches the history length (acceptance 1 phrasing).
    assert store.next_seq == 5


# --------------------------------------------------------------------------- #
# Acceptance 2 — idempotent: reconnect with unchanged store does not duplicate
# --------------------------------------------------------------------------- #

def test_backfill_is_idempotent_same_session_same_highwater(tmp_path,
                                                            monkeypatch,
                                                            hs_state):
    """Re-backfilling the SAME session to the SAME store high-water (a
    reconnect with an unchanged store) is a no-op — no duplicate frames, seq
    stays contiguous."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    sid, messages = _known_history()
    _seed_history(state, sid, title="hscc", profile="hscc-orch",
                  messages=messages)
    _point_profile_db_at(monkeypatch, state)

    first = backfill_named_session("hscc", registry_path=reg)
    assert first["backfilled"] == 4

    # Reconnect: same session id, store high-water unchanged.
    second = backfill_named_session("hscc", registry_path=reg)
    assert second["backfilled"] == 0
    assert second["skipped"] == "already_backfilled"

    store = get_store("hscc")
    evs = store.history()["events"]
    # Exactly the original 4 frames — no duplication.
    assert len(evs) == 4
    assert [e["seq"] for e in evs] == [1, 2, 3, 4]
    assert store.next_seq == 5


def test_backfill_reruns_when_store_advanced(tmp_path, monkeypatch, hs_state):
    """A reconnect AFTER the store advanced (new live frames since the last
    backfill) is NOT the unchanged-high-water skip — it reruns and appends the
    full history. (This is the retry pathway; in production a reconnect usually
    comes with a fresh store, so the realistic no-dup case is the one above.)"""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    sid, messages = _known_history()
    _seed_history(state, sid, title="hscc", profile="hscc-orch",
                  messages=messages)
    _point_profile_db_at(monkeypatch, state)

    backfill_named_session("hscc", registry_path=reg)
    # Simulate a live event landing in the store after the backfill (so the
    # high-water advanced past the last recorded backfill point).
    from session_event import MessagePayload, TYPE_MESSAGE
    get_store("hscc").append(TYPE_MESSAGE, MessagePayload(
        role="assistant", delta="live", done=True))

    # Store high-water no longer matches the recorded backfill point -> reruns.
    result = backfill_named_session("hscc", registry_path=reg)
    assert result["backfilled"] == 4
    assert result.get("skipped") is None


# --------------------------------------------------------------------------- #
# Acceptance 4 (unit half) — backfill failure logs + continues, never breaks
# --------------------------------------------------------------------------- #

def test_backfill_no_session_skips_cleanly(tmp_path, monkeypatch, hs_state):
    """A registered project with no named session yet is a clean no-op
    (nothing to backfill), never an error, never a fake id."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "empty.db"
    _make_state_db(state).close()
    _point_profile_db_at(monkeypatch, state)

    result = backfill_named_session("hscc", registry_path=reg)
    assert result["backfilled"] == 0
    assert result["session"] is None
    assert result["skipped"] == "no_session"


def test_backfill_state_db_unreachable_is_fail_safe(tmp_path, monkeypatch,
                                                    hs_state):
    """An unreachable state.db (opener returns None) reports skipped, does not
    raise, and does not touch the store."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    _make_state_db(state).close()
    import routes_orchestrator
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db",
                        lambda profile, read_only=False: None)

    result = backfill_named_session("hscc", registry_path=reg)
    assert result["backfilled"] == 0
    assert result["session"] is None
    assert result["skipped"] == "no_session"
    # The store is untouched — no frames appended.
    assert get_store("hscc").next_seq == 1


def test_backfill_read_failure_continues_and_retries_after(tmp_path,
                                                           monkeypatch,
                                                           hs_state):
    """A backfill that FAILS mid-read (get_messages raises) is caught, reported
    as skipped, and — critically — does NOT break the live feed: the store is
    still writable for live frames. The next (re)connect retries + succeeds."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    sid, messages = _known_history()
    _seed_history(state, sid, title="hscc", profile="hscc-orch",
                  messages=messages)

    # First connect: the history read fails (e.g. a transient DB lock).
    import routes_orchestrator

    class _BrokenDB:
        # The RESOLVER calls this to map title -> session id; it must
        # SUCCEED so resolution reaches the history read. Only the
        # backfill's ``get_messages`` read fails (the transient DB lock).
        def resolve_session_by_title(self, title):
            return sid
        def get_messages(self, session_id):
            raise RuntimeError("database is locked")
        def close(self):
            pass

    def _broken_open(profile, read_only=False):
        return _BrokenDB()
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db",
                        _broken_open)

    result = backfill_named_session("hscc", registry_path=reg)
    assert result["backfilled"] == 0
    assert result["session"] == sid
    assert result["skipped"] == "backfill_failed"

    # The live feed must still work: the store is intact and appendable.
    from session_event import MessagePayload, TYPE_MESSAGE
    get_store("hscc").append(TYPE_MESSAGE, MessagePayload(
        role="assistant", delta="live reply", done=True))
    assert get_store("hscc").next_seq == 2   # live frame landed despite the fail

    # Reconnect: the DB is healthy again -> retry succeeds and backfills.
    _point_profile_db_at(monkeypatch, state)
    retry = backfill_named_session("hscc", registry_path=reg)
    assert retry["backfilled"] == 4
    assert retry.get("skipped") is None
    evs = get_store("hscc").history()["events"]
    # live frame + 4 backfilled history frames = 5, one contiguous seq space.
    assert len(evs) == 5
    assert [e["seq"] for e in evs] == [1, 2, 3, 4, 5]


def test_backfill_unknown_project_never_raises(tmp_path):
    """Backfilling an unknown project is fail-safe: reported, never raised."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    result = backfill_named_session("bogus", registry_path=reg)
    assert result["backfilled"] == 0
    assert result["skipped"] == "resolve_failed"


# --------------------------------------------------------------------------- #
# Driver integration — start() backfills BEFORE the live feed, and the PTY is
# pinned to the named session (§3.1/§3.3)
# --------------------------------------------------------------------------- #

def test_start_resolves_and_backfills_before_live(tmp_path, monkeypatch,
                                                  hs_state):
    """start() resolves the project's named session + backfills the store first,
    and the /api/pty path is pinned to that session (resume+profile) so an app
    message reaches the SAME named session the CLI continues (§3.3)."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    sid, messages = _known_history()
    _seed_history(state, sid, title="hscc", profile="hscc-orch",
                  messages=messages)
    _point_profile_db_at(monkeypatch, state)

    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc",
                        channel="chan-driver-test")
    drv = GatewayDriver(cfg)
    cfg.registry_path = reg

    # Fake the transport so start() never touches a real gateway.
    seen = {"paths": []}

    class _FakeClient:
        def __init__(self, host, port, path):
            seen["paths"].append(path)
        def connect(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(gd, "_WSClient", _FakeClient)
    monkeypatch.setattr(drv, "_run_events_loop", lambda: None)
    monkeypatch.setattr(drv, "_run_pty_loop", lambda: None)

    drv.start()

    # Backfill ran BEFORE the live feed: the store already has the history.
    evs = get_store("hscc").history()["events"]
    assert len(evs) == 4

    # The /api/pty path is pinned to the named session (resume + profile) —
    # the first client _connect_upstreams opens; the events one stays plain.
    pty_path, events_path = seen["paths"][0], seen["paths"][1]
    assert pty_path.startswith("/api/pty?")
    assert "token=t" in pty_path
    assert "channel=chan-driver-test" in pty_path
    assert "profile=hscc-orch" in pty_path
    assert f"resume={sid}" in pty_path
    # The events path stays plain token+channel (no resume needed there).
    assert events_path.startswith("/api/events?")
    assert "resume=" not in events_path and "profile=" not in events_path

    drv.stop()


def test_pty_path_pins_named_session_when_config_supplies(tmp_path):
    """pty_path() uses config-supplied session_id/profile (§3.1 config path)."""
    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc",
                        profile="hscc-orch", session_id="sid-abc")
    path = cfg.pty_path()
    assert "resume=sid-abc" in path
    assert "profile=hscc-orch" in path

    # Fallback: no session/profile -> just token+channel (session not pinned).
    bare = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc")
    assert "resume=" not in bare.pty_path()
    assert "profile=" not in bare.pty_path()
