"""Translation-contract tests for the store-tail/backfill (iOS-chat-e2e C+D).

Pins the frame contract of ``_translate_history_to_store`` directly (dicts in,
store frames out — no state.db, so it runs on BOTH interpreters):

  C3  compaction summary rows are NOT user messages (system/context_summary);
  C2  a user row duplicating a very recent app echo is skipped (the seq 116 +
      233 double user message);
  D   assistant rows emit reasoning (when persisted) + tool_call start frames
      (from the row's ``tool_calls``), and EMPTY-content assistant rows emit
      no message frame (the tool-calling turn shape verified live in the
      operator's session: assistant rows with content='' finish_reason=
      'tool_calls');
  D   tool rows still emit the finish half (regression pin).
"""
import time

import pytest

import gateway_driver as gd
from gateway_driver import _translate_history_to_store
from session_event import MessagePayload, TYPE_MESSAGE, TYPE_SYSTEM, \
    TYPE_TOOL_CALL, get_store, reset_stores


@pytest.fixture(autouse=True)
def clean_store():
    reset_stores()
    yield
    reset_stores()


def _store():
    return get_store("hscc")


def _evs(store):
    return store.history()["events"]


def _now_epoch():
    return time.time()


# --------------------------------------------------------------------------- #
# C3 — compaction summaries
# --------------------------------------------------------------------------- #

def test_compaction_summary_is_not_a_user_message():
    store = _store()
    n = _translate_history_to_store(store, [
        {"id": 1, "role": "user", "_compressed_summary": True,
         "content": "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns...",
         "timestamp": 1759200000},
    ])
    assert n == 1
    ev = _evs(store)[0]
    assert ev["type"] == TYPE_SYSTEM
    assert ev["payload"]["kind"] == "context_summary"


def test_compaction_detected_by_prefix_without_flag():
    store = _store()
    _translate_history_to_store(store, [
        {"id": 1, "role": "user",
         "content": "[CONTEXT COMPACTION — REFERENCE ONLY] body",
         "timestamp": 1759200000},
    ])
    assert _evs(store)[0]["payload"]["kind"] == "context_summary"


def test_real_user_message_is_not_flagged():
    store = _store()
    _translate_history_to_store(store, [
        {"id": 1, "role": "user", "content": "/goal make it work",
         "timestamp": 1759200000},
    ])
    ev = _evs(store)[0]
    assert ev["type"] == TYPE_MESSAGE and ev["payload"]["role"] == "user"


# --------------------------------------------------------------------------- #
# C2 — user echo dedupe
# --------------------------------------------------------------------------- #

def test_user_row_matching_recent_app_echo_is_skipped():
    """The exact 116+233 shape: the WS send echoed the message into the store;
    the tail must not append the same text again."""
    store = _store()
    # The app-send echo (production stamps ts=now via make_event).
    store.append(TYPE_MESSAGE, MessagePayload(
        role="user", delta="hello from iphone", done=True))
    n = _translate_history_to_store(store, [
        {"id": 5, "role": "user", "content": "hello from iphone",
         "timestamp": _now_epoch()},
    ])
    assert n == 0
    users = [e for e in _evs(store)
             if e["type"] == TYPE_MESSAGE and e["payload"]["role"] == "user"]
    assert len(users) == 1


def test_user_row_with_old_echo_is_kept():
    """Same text outside the dedupe window is a genuine terminal-typed
    message — kept."""
    store = _store()
    store.append(TYPE_MESSAGE, MessagePayload(
        role="user", delta="ping", done=True),
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(_now_epoch() - 3600)))
    n = _translate_history_to_store(store, [
        {"id": 6, "role": "user", "content": "ping",
         "timestamp": _now_epoch()},
    ])
    assert n == 1


def test_different_user_text_is_never_suppressed():
    store = _store()
    store.append(TYPE_MESSAGE, MessagePayload(
        role="user", delta="one thing", done=True))
    n = _translate_history_to_store(store, [
        {"id": 7, "role": "user", "content": "another thing",
         "timestamp": _now_epoch()},
    ])
    assert n == 1


# --------------------------------------------------------------------------- #
# D — assistant rows: reasoning, tool starts, empty content
# --------------------------------------------------------------------------- #

def test_assistant_with_tools_emits_starts_not_empty_bubble():
    """The verified live tool-calling shape: assistant row with content='' +
    finish_reason='tool_calls'. Expect: NO message frame, one tool_call start
    per call with call_id/name/args."""
    store = _store()
    _translate_history_to_store(store, [{
        "id": 10, "role": "assistant", "content": "",
        "finish_reason": "tool_calls",
        "tool_calls": [{
            "id": "call_abc", "type": "function",
            "function": {"name": "terminal",
                         "arguments": '{"command": "ls -la"}'},
        }],
        "timestamp": 1759200000,
    }])
    evs = _evs(store)
    assert not [e for e in evs if e["type"] == TYPE_MESSAGE]
    starts = [e for e in evs if e["type"] == TYPE_TOOL_CALL]
    assert len(starts) == 1
    p = starts[0]["payload"]
    assert p["status"] == "start"
    assert p["call_id"] == "call_abc"
    assert p["name"] == "terminal"
    assert p["args"] == {"command": "ls -la"}


def test_tool_finish_pairs_with_start_by_call_id():
    store = _store()
    _translate_history_to_store(store, [
        {"id": 10, "role": "assistant", "content": "",
         "finish_reason": "tool_calls",
         "tool_calls": [{"id": "call_abc", "function": {
             "name": "terminal", "arguments": '{"command": "ls"}'}}],
         "timestamp": 1759200000},
        {"id": 11, "role": "tool", "tool_call_id": "call_abc",
         "tool_name": "terminal", "content": "total 42",
         "timestamp": 1759200010},
    ])
    tcs = [e["payload"] for e in _evs(store) if e["type"] == TYPE_TOOL_CALL]
    assert [p["status"] for p in tcs] == ["start", "finish"]
    assert {p["call_id"] for p in tcs} == {"call_abc"}
    assert tcs[1]["result"] == "total 42"


def test_assistant_reasoning_emits_reasoning_frame_then_message():
    """ORDER contract: reasoning frame precedes the message frame."""
    store = _store()
    _translate_history_to_store(store, [{
        "id": 20, "role": "assistant", "content": "the answer",
        "reasoning": "think think think", "reasoning_content": None,
        "timestamp": 1759200000,
    }])
    evs = _evs(store)
    assert evs[0]["type"] == TYPE_SYSTEM
    assert evs[0]["payload"]["kind"] == "reasoning"
    assert evs[0]["payload"]["details"]["text"] == "think think think"
    assert evs[1]["type"] == TYPE_MESSAGE
    assert evs[1]["payload"]["role"] == "assistant"
    assert evs[1]["payload"]["delta"] == "the answer"


def test_assistant_without_reasoning_emits_only_message():
    store = _store()
    _translate_history_to_store(store, [{
        "id": 21, "role": "assistant", "content": "plain",
        "reasoning": None, "reasoning_content": "",
        "timestamp": 1759200000,
    }])
    evs = _evs(store)
    assert len(evs) == 1 and evs[0]["type"] == TYPE_MESSAGE


def test_empty_assistant_with_no_tools_emits_nothing():
    """A content-less, tool-less assistant row is not renderable — skip."""
    store = _store()
    n = _translate_history_to_store(store, [{
        "id": 22, "role": "assistant", "content": "",
        "timestamp": 1759200000,
    }])
    assert n == 0 and _evs(store) == []


def test_full_turn_ordering_reasoning_start_message():
    """One row can carry reasoning + tool_calls + text: order = reasoning,
    tool-start, message (the contract the iOS timeline renders)."""
    store = _store()
    _translate_history_to_store(store, [{
        "id": 30, "role": "assistant", "content": "done it",
        "reasoning": "plan first",
        "tool_calls": [{"id": "c1", "function": {
            "name": "patch", "arguments": "{}"}}],
        "timestamp": 1759200000,
    }])
    kinds = [(e["type"], e.get("payload", {}).get("kind")
              or e.get("payload", {}).get("status")
              or e.get("payload", {}).get("role")) for e in _evs(store)]
    assert kinds == [("system", "reasoning"), ("tool_call", "start"),
                     ("message", "assistant")]


# --------------------------------------------------------------------------- #
# Regression pins (behavior the old code had, must survive)
# --------------------------------------------------------------------------- #

def test_plain_user_assistant_pair_still_translates():
    store = _store()
    n = _translate_history_to_store(store, [
        {"id": 1, "role": "user", "content": "hello",
         "timestamp": 1759200000},
        {"id": 2, "role": "assistant", "content": "hi there",
         "timestamp": 1759200050},
    ])
    assert n == 2
    evs = _evs(store)
    assert [(e["payload"]["role"], e["payload"]["delta"]) for e in evs] == [
        ("user", "hello"), ("assistant", "hi there")]


def test_system_row_translates_as_session_history():
    store = _store()
    _translate_history_to_store(store, [
        {"id": 2, "role": "system", "content": "banner",
         "timestamp": 1759200000},
    ])
    ev = _evs(store)[0]
    assert ev["type"] == TYPE_SYSTEM and ev["payload"]["kind"] == "session_history"
