"""Unit tests for the gateway-driver STORE-TAIL live source (t_93f1ba4d).

THE GAP this closes: the driver's ``/api/events`` feed fans out ONLY the
serve's own pty activity (chat_ws.py:622-654 — a pure /api/pub fan-out). When
the operator drives the SAME named session in a SEPARATE CLI REPL, that CLI
writes its frames straight to the shared state.db and they NEVER reach the
serve fan-out — so the per-project store froze at the mount-time backfill
watermark (stuck next_seq 247).

``tail_named_session`` is the §3.3 live source for those externally-written
frames: it re-reads the on-disk messages table for NEW rows (id > the shared
store-tail watermark) and translates them into the store exactly like the
backfill, so the app reflects whatever the CLI writes.

Covered here (hermetic — real temp registry + real temp SessionDB, never the
operator's home):
  * CLI-driven frames are picked up: after backfill seeds the store, new
    on-disk messages (higher id) appear in the store on the next tail.
  * No re-translation of backfilled history (watermark seeded by backfill).
  * Idempotent: a tail with nothing new appends 0; watermark monotonic.
  * `after_id` override drives deterministic sequence for a test/replay.
  * Dedup with serve-driven turns: the events-loop's `_sync_tail_watermark`
    claims messages the native feed already translated, so the tail does not
    duplicate them.
  * Fail-safe: no session / unreachable db report ``skipped``, never raise.
"""
import time
from pathlib import Path

import pytest

import gateway_driver as gd
from gateway_driver import (backfill_named_session, tail_named_session)
from session_event import get_store, reset_stores

try:
    from hermes_state import SessionDB  # noqa: F401
    _HAS_HERMES_STATE = True
except Exception:
    _HAS_HERMES_STATE = False


@pytest.fixture
def hs_state():
    """Skip the whole state-db-backed test when hermes_state is unavailable
    (the p313 interpreter lacks it — same documented env gap as backfill)."""
    if not _HAS_HERMES_STATE:
        pytest.skip("hermes_state not importable on this interpreter "
                    "(needs the hermes-agent runtime / hermes venv)")
    yield


@pytest.fixture(autouse=True)
def clean_state():
    reset_stores()
    gd.reset_backfill_state()
    gd.reset_tail_state()
    yield
    reset_stores()
    gd.reset_backfill_state()
    gd.reset_tail_state()


# --------------------------------------------------------------------------- #
# Helpers (same seams as test_gateway_backfill.py)
# --------------------------------------------------------------------------- #

def _write_registry(tmp_path: Path, projects):
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
    """Redirect the resolver + backfill + tail at a temp state.db."""
    def _open(profile, read_only=False):
        return _make_state_db(state_path)
    import routes_orchestrator
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db", _open)


def _setup(tmp_path, monkeypatch, initial_messages, session_id="20261002_tail_aaa"):
    """Build the registry + state.db, redirect the DB seam, and return
    (state_path, session_id, profile)."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "state.db"
    _seed_history(state, session_id, title="hscc", profile="hscc-orch",
                  messages=initial_messages)
    _point_profile_db_at(monkeypatch, state)
    return state, session_id


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------------- #
# CLI-driven frames are picked up AFTER backfill seeds the store
# --------------------------------------------------------------------------- #

def test_tail_appends_cli_driven_frames_after_backfill(tmp_path, monkeypatch,
                                                       hs_state):
    """Backfill seeds history, THEN the operator's CLI writes new frames; the
    next tail picks up ONLY the new frames and the store advances past the
    backfill watermark (the reported freeze)."""
    base = [
        {"role": "user", "content": "hello", "timestamp": 1759200000},
        {"role": "assistant", "content": "hi there", "timestamp": 1759200050},
    ]
    state, sid = _setup(tmp_path, monkeypatch, base)

    backfill_named_session("hscc", registry_path=str(tmp_path / "registry.yaml"))
    store = get_store("hscc")
    assert store.next_seq == 3       # 2 history frames + 1 (watermark)
    assert [e["payload"]["delta"] for e in store.history()["events"]] \
           == ["hello", "hi there"]

    # The operator's SEPARATE CLI REPL writes new frames to the SAME state.db.
    _append_real(state, sid, [
        {"role": "user", "content": "CLI second message", "timestamp": 1759201000},
        {"role": "assistant", "content": "CLI reply", "timestamp": 1759201050},
    ])

    result = tail_named_session("hscc", registry_path=str(tmp_path / "registry.yaml"))

    assert result["tail_appended"] == 2
    assert result["session"] == sid
    assert result.get("skipped") is None
    # The store advanced past the old watermark — the freeze is gone.
    assert store.next_seq == 5
    evs = store.history()["events"]
    # Newest frames carry the CLI's content.
    assert evs[2]["payload"] == {"role": "user", "delta": "CLI second message",
                                 "done": True}
    assert evs[3]["payload"] == {"role": "assistant", "delta": "CLI reply",
                                 "done": True}
    assert [e["seq"] for e in evs] == [1, 2, 3, 4]


def _append_real(state_path: Path, session_id: str, messages: list) -> None:
    """Write NEW on-disk messages as the CLI REPL does (a fresh SessionDB)."""
    db = _make_state_db(state_path)
    try:
        db.append_messages_batch(session_id, messages)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# No re-translation of backfilled history / no double-append
# --------------------------------------------------------------------------- #

def test_tail_does_not_retranslate_backfilled_history(tmp_path, monkeypatch,
                                                      hs_state):
    """Right after backfill, the watermark is seeded to the tail of the
    backfilled history — a tail call appends 0 (no duplicate of the history)."""
    base = [
        {"role": "user", "content": "a", "timestamp": 1},
        {"role": "assistant", "content": "b", "timestamp": 2},
    ]
    state, sid = _setup(tmp_path, monkeypatch, base)
    reg = str(tmp_path / "registry.yaml")

    backfill_named_session("hscc", registry_path=reg)
    assert get_store("hscc").next_seq == 3

    result = tail_named_session("hscc", registry_path=reg)
    assert result["tail_appended"] == 0
    assert result.get("skipped") is None
    assert result["high_water"] > 0     # watermark seeded by backfill
    assert get_store("hscc").next_seq == 3
    assert len(get_store("hscc").history()["events"]) == 2


# --------------------------------------------------------------------------- #
# Idempotent / watermark monotonic
# --------------------------------------------------------------------------- #

def test_tail_is_idempotent_with_nothing_new(tmp_path, monkeypatch, hs_state):
    """Two tails with no new on-disk messages append 0 both times; a message id
    advanced by a manual claim is kept (monotonic), preventing re-translation."""
    base = [{"role": "user", "content": "a", "timestamp": 1}]
    state, sid = _setup(tmp_path, monkeypatch, base)
    reg = str(tmp_path / "registry.yaml")

    gd._mark_tail_seen("hscc", 100)   # simulate an already-claimed high watermark
    result1 = tail_named_session("hscc", registry_path=reg)
    assert result1["tail_appended"] == 0
    assert result1["high_water"] == 100
    result2 = tail_named_session("hscc", registry_path=reg)
    assert result2["tail_appended"] == 0
    assert get_store("hscc").next_seq == 1


# --------------------------------------------------------------------------- #
# after_id override (deterministic cursor for replay/test)
# --------------------------------------------------------------------------- #

def test_tail_after_id_override(tmp_path, monkeypatch, hs_state):
    """An explicit after_id cursor drives a deterministic sequence, translating
    only rows strictly above it — tanking watermark semantics for a replay."""
    base = [
        {"role": "user", "content": "u1", "timestamp": 1},
        {"role": "assistant", "content": "a1", "timestamp": 2},
        {"role": "user", "content": "u2", "timestamp": 3},
        {"role": "assistant", "content": "a2", "timestamp": 4},
    ]
    state, sid = _setup(tmp_path, monkeypatch, base)
    reg = str(tmp_path / "registry.yaml")

    # Translate only rows after message id=1 (the 2nd..4th messages).
    result = tail_named_session("hscc", registry_path=reg, after_id=1)
    assert result["tail_appended"] == 3
    evs = get_store("hscc").history()["events"]
    assert [e["payload"]["delta"] for e in evs] == ["a1", "u2", "a2"]


# --------------------------------------------------------------------------- #
# Dedup with serve-driven turns (events-loop watermark sync)
# --------------------------------------------------------------------------- #

def test_sync_tail_watermark_claims_serve_messages(tmp_path, monkeypatch,
                                                   hs_state):
    """A serve-driven turn's messages (translated by the native feed) are
    claimed via the events-loop sync so the tail does NOT re-translate them."""
    base = [{"role": "user", "content": "a", "timestamp": 1}]
    state, sid = _setup(tmp_path, monkeypatch, base)
    reg = str(tmp_path / "registry.yaml")

    backfill_named_session("hscc", registry_path=reg)
    # The serve's dispatcher writes a full turn to state.db AND the native
    # feed has already translated it into the store. The events-loop sync
    # ("_sync_tail_watermark") claims those ids, so the tail must skip them.
    _append_real(state, sid, [
        {"role": "user", "content": "app msg", "timestamp": 100},
        {"role": "assistant", "content": "app reply", "timestamp": 101},
    ])

    from gateway_driver import GatewayConfig, GatewayDriver
    cfg = GatewayConfig(host="127.0.0.1", port=1, token="t", project="hscc",
                        session_id=sid, profile="hscc-orch")
    drv = GatewayDriver(cfg)
    drv._session_id = sid
    drv._session_profile = "hscc-orch"
    drv._sync_tail_watermark()    # the events-loop would call this at turn-done

    result = tail_named_session("hscc", registry_path=reg)
    # The serve-written messages are claimed — NOT re-translated.
    assert result["tail_appended"] == 0
    assert get_store("hscc").next_seq == 2   # only the original backfill frame


# --------------------------------------------------------------------------- #
# Fail-safe: no session / unreachable db / read failure never raises
# --------------------------------------------------------------------------- #

def test_tail_no_session_skips_cleanly(tmp_path, monkeypatch, hs_state):
    """A registered project with no named session yet is a clean no-op."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])
    state = tmp_path / "empty.db"
    _make_state_db(state).close()
    _point_profile_db_at(monkeypatch, state)

    result = tail_named_session("hscc", registry_path=reg)
    assert result["tail_appended"] == 0
    assert result["session"] is None
    assert result["skipped"] == "no_session"


def test_tail_state_db_unreachable_is_fail_safe(tmp_path, monkeypatch, hs_state):
    """An unreachable state.db (opener returns None) reports skipped and does
    not raise nor touch the store."""
    reg = _write_registry(tmp_path, [
        {"name": "hscc", "repo": "/tmp/hscc", "board": "hscc"},
    ])

    def _open(profile, read_only=False):
        return None
    import routes_orchestrator
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db", _open)

    result = tail_named_session("hscc", registry_path=reg)
    assert result["tail_appended"] == 0
    assert result["skipped"] == "no_session"


def test_tail_read_failure_is_fail_safe(tmp_path, monkeypatch, hs_state):
    """A get_messages read that fails (transient lock) is caught + reported as
    skipped; the poller loop would simply try again next tick."""
    base = [{"role": "user", "content": "a", "timestamp": 1}]
    state, sid = _setup(tmp_path, monkeypatch, base)
    reg = str(tmp_path / "registry.yaml")

    real = _make_state_db(state)

    class _FlakyDB:
        """Real resolvable DB whose get_messages read fails mid-poll."""
        resolve_session_by_title = real.resolve_session_by_title
        def get_messages(self, *a, **k):
            raise RuntimeError("database is locked")
        def close(self):
            pass

    def _open(profile, read_only=False):
        return _FlakyDB()
    import routes_orchestrator
    monkeypatch.setattr(routes_orchestrator, "_open_profile_session_db", _open)

    result = tail_named_session("hscc", registry_path=reg)
    assert result["tail_appended"] == 0
    assert result["skipped"] == "tail_failed"
