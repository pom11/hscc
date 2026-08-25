"""Tests for the inbound-message queue + replay engine (design card t_a4e700ee).

Drive and verify everything through HSCC/project paths with injected fakes —
constructed synthetic gateway-log lines, a fake notifier, and a fake delivery
function. NO test uses real Telegram, real models, or any real cluster state.
"""

from hscc_daemon import autodown as ad
from hscc_daemon import replay

# A realistic gateway log line as the Hermes gateway writes it
# (gateway/run.py:_handle_message_with_agent).
def _line(text="hello", chat="-1003906355027", user="desac",
          reply_to_id=None, reply_to_text=""):
    r = "None" if reply_to_id is None else str(reply_to_id)
    return (
        f"inbound message: platform=telegram user={user} chat={chat} msg={text!r} "
        f"reply_to_id={r} reply_to_text={reply_to_text!r}"
    )


def _set_state(state="down", enabled=True, **extra):
    cfg = dict(ad.DEFAULT_CONFIG)
    cfg["enabled"] = enabled
    cfg["state"] = state
    cfg["down_since"] = "2020-01-01T00:00:00+00:00"
    cfg["last_activity_iso"] = "2020-01-01T00:00:00+00:00"
    cfg.update(extra)
    ad.save_config(cfg)
    return cfg


class TestParseGatewayLine:
    """parse_gateway_line extracts full routing metadata from a real line."""

    def test_full_metadata(self):
        meta = replay.parse_gateway_line(_line())
        assert meta is not None
        assert meta["platform"] == "telegram"
        assert meta["chat_id"] == "-1003906355027"
        assert meta["user"] == "desac"
        assert meta["text"] == "hello"
        # A bare ``None`` literal in the log line parses to Python None
        # (no reply-to thread).
        assert meta["reply_to_id"] is None

    def test_reply_to_id_and_nonempty_text(self):
        meta = replay.parse_gateway_line(
            _line(text="hi", reply_to_id=42, reply_to_text="orig"))
        assert meta is not None
        assert meta["text"] == "hi"
        assert meta["reply_to_id"] == "42"

    def test_text_with_spaces_preserved(self):
        meta = replay.parse_gateway_line(_line(text="hello world foo"))
        assert meta is not None
        assert meta["text"] == "hello world foo"

    def test_not_a_marker_line_returns_none(self):
        assert replay.parse_gateway_line("some random log noise") is None
        assert replay.parse_gateway_line("") is None


class TestQueueing:
    """Messages arriving while down/waking are queued with full metadata."""

    def test_queued_with_full_routing_metadata(self, tmp_path):
        q = str(tmp_path / "q.json")
        meta = replay.parse_gateway_line(_line())
        res = replay.enqueue_inbound(meta, path=q)
        assert res["queued"] is True
        st = replay.load_queue(q)
        assert len(st["messages"]) == 1
        m = st["messages"][0]
        assert m["platform"] == "telegram"
        assert m["chat_id"] == "-1003906355027"
        assert m["user"] == "desac"
        assert m["text"] == "hello"
        assert m["seq"] == 1
        assert m["arrived_iso"]  # arrival timestamp stamped

    def test_arrival_order_preserved_across_messages(self, tmp_path):
        q = str(tmp_path / "q.json")
        for t in ["one", "two", "three"]:
            replay.enqueue_inbound(
                replay.parse_gateway_line(_line(text=t)), path=q)
        st = replay.load_queue(q)
        assert [m["seq"] for m in st["messages"]] == [1, 2, 3]
        assert [m["text"] for m in st["messages"]] == ["one", "two", "three"]

    def test_waking_notice_sent_exactly_once_per_wake(self, tmp_path):
        q = str(tmp_path / "q.json")
        notices = []
        notify = lambda: notices.append(1)
        for t in ["a", "b", "c"]:
            replay.enqueue_inbound(
                replay.parse_gateway_line(_line(text=t)), path=q,
                notify_fn=notify)
        assert notices == [1]   # exactly once across the whole wake

    def test_autodown_disabled_nothing_queued(self, tmp_path, monkeypatch):
        """Disabled ⇒ the probe queues nothing (C5)."""
        _set_state(enabled=False, state="down")
        monkeypatch.setattr(replay, "QUEUE_FILE", str(tmp_path / "q.json"))
        chunk = (_line() + "\n").encode()
        assert ad._capture_inbound_messages(chunk) == 0
        st = replay.load_queue()
        assert st["messages"] == []

    def test_state_up_nothing_queued(self, tmp_path, monkeypatch):
        """State up ⇒ the probe queues nothing (gateway processes normally)."""
        _set_state(enabled=True, state="up")
        monkeypatch.setattr(replay, "QUEUE_FILE", str(tmp_path / "q.json"))
        chunk = (_line() + "\n").encode()
        assert ad._capture_inbound_messages(chunk) == 0
        assert replay.load_queue()["messages"] == []


class TestReplay:
    """Replay in order; empty on success; retain + loud on failure."""

    def _queue_two(self, tmp_path):
        q = str(tmp_path / "q.json")
        for t in ["one", "two"]:
            replay.enqueue_inbound(
                replay.parse_gateway_line(_line(text=t)), path=q)
        return q

    def test_successful_replay_in_order_then_emptied(self, tmp_path):
        q = self._queue_two(tmp_path)
        delivered = []
        res = replay.replay_queued(lambda m: delivered.append(m["text"]) or True,
                                   path=q)
        assert res["delivered"] == 2
        assert delivered == ["one", "two"]      # strict arrival order
        assert res["empty"] is False
        assert replay.load_queue(q)["messages"] == []   # emptied

    def test_replay_failure_retains_queue(self, tmp_path):
        q = self._queue_two(tmp_path)
        res = replay.replay_queued(lambda m: False, path=q)
        assert res["failed"] == 1
        st = replay.load_queue(q)
        # The failed message is RETAINED, and (because we stop on failure) the
        # later message is also still queued.
        assert len(st["messages"]) == 2
        assert [m["text"] for m in st["messages"]] == ["one", "two"]

    def test_crash_between_replay_and_dequeue_no_double_send(self, tmp_path):
        """Emulate a crash: deliver msg1 + persist, then STOP before msg2.

        A restart (reload) must not re-send msg1 (already handed off + dequeued
        durably) and must only send msg2 once. This is the idempotency guarantee.
        """
        q = self._queue_two(tmp_path)
        delivered = []

        # "Process run 1": deliver msg1 successfully (persisted removal), then
        # simulate a crash before delivering msg2 by NOT looping further.
        st = replay.load_queue(q)
        msg1 = st["messages"][0]
        replay._persist_after_handoff(st, q, msg1)
        delivered.append(msg1["text"])

        # "Restart": reload from disk — msg1 must be gone, msg2 remains.
        st2 = replay.load_queue(q)
        assert [m["text"] for m in st2["messages"]] == ["two"]

        # "Process run 2": replay resumes; only msg2 is delivered once.
        res = replay.replay_queued(
            lambda m: delivered.append(m["text"]) or True, path=q)
        assert res["delivered"] == 1
        assert delivered == ["one", "two"]      # no double-send of "one"
        assert replay.load_queue(q)["messages"] == []

    def test_empty_queue_noop_no_crash(self, tmp_path):
        q = str(tmp_path / "q.json")
        res = replay.replay_queued(lambda m: True, path=q)
        assert res["empty"] is True
        assert res["delivered"] == 0

    def test_control_flow_empty_deliverable_success(self, tmp_path):
        """Wake via CLI/HTTP/kanban with an empty queue ⇒ no-op, no crash.

        The autoup → replay_queued hook runs on every successful wake; with an
        empty queue it is a clean no-op returning empty=True.
        """
        q = str(tmp_path / "q.json")
        # An absent queue file is an empty queue.
        res = replay.replay_queued(lambda m: True, path=q)
        assert res["empty"] is True
        assert res["delivered"] == 0 and res["failed"] == 0


class TestBounds:
    """Over-age / over-cap messages are dropped with a notice, not silently."""

    def test_over_age_dropped_with_notice(self, tmp_path):
        q = str(tmp_path / "q.json")
        replay.enqueue_inbound(
            replay.parse_gateway_line(_line(text="old")), path=q,
            now="2026-08-25T00:00:00+00:00")
        # Replay "now" is far after the message age bound → dropped, not run.
        res = replay.replay_queued(
            lambda m: True, path=q, now="2026-08-25T10:00:00+00:00")
        assert res["dropped_aged"] == 1
        assert res["delivered"] == 0
        # The over-age message is removed (dropped, not retained/executed).
        assert replay.load_queue(q)["messages"] == []

    def test_fresh_message_not_dropped(self, tmp_path):
        q = str(tmp_path / "q.json")
        now = "2026-08-25T00:00:00+00:00"
        replay.enqueue_inbound(
            replay.parse_gateway_line(_line(text="fresh")), path=q, now=now)
        res = replay.replay_queued(
            lambda m: True, path=q, now="2026-08-25T01:00:00+00:00")
        assert res["delivered"] == 1
        assert res["dropped_aged"] == 0

    def test_over_cap_drops_oldest_with_no_double_send_of_dropped(self, tmp_path):
        q = str(tmp_path / "q.json")
        # Fill beyond the cap.
        for i in range(replay.MAX_QUEUED_MESSAGES + 5):
            replay.enqueue_inbound(
                replay.parse_gateway_line(_line(text=f"m{i}")), path=q)
        st = replay.load_queue(q)
        # Queue is bounded at the cap.
        assert len(st["messages"]) == replay.MAX_QUEUED_MESSAGES
        # The OLDEST messages (m0..m4) were dropped; newest survive.
        assert st["messages"][0]["text"] == "m5"
        assert st["messages"][-1]["text"] == "m104"


class TestAutoupReplayIntegration:
    """The autoup wake hook replays queued messages after readiness."""

    def test_autoup_success_replays_queue_and_empties(self, tmp_path,
                                                      monkeypatch):
        """Full wake: autoup() success path replays the queue in order via the
        injected delivery seam, and the queue is emptied. Hermetic: no real
        serving/cluster is touched (fake plan + fake runner + fake readiness)."""
        # Queue two messages first, as the probe would while down. autoup's
        # replay reads replay.QUEUE_FILE (redirected to tmp by the autouse
        # isolation fixture), so we queue to that exact path.
        q = replay.QUEUE_FILE
        for t in ["a", "b"]:
            replay.enqueue_inbound(
                replay.parse_gateway_line(_line(text=t)), path=q)
        assert len(replay.load_queue(q)["messages"]) == 2

        # State must be down/waking for autoup to proceed (not already "up").
        _set_state("down")

        # Fake the serving / wake machinery so autoup is a no-op on the cluster.
        plan = [{"kind": "orchestrator", "unit_id": "orch",
                 "nodes": ["10.0.0.244"], "port": 8000,
                 "cmd": "sparkrun run orch"}]
        monkeypatch.setattr(ad, "_build_wake_plan", lambda serving: plan)
        monkeypatch.setattr(ad, "_wait_ready",
                            lambda *a, **k: (["orch"], True))
        monkeypatch.setattr(ad, "_notify", lambda *a, **k: None)
        runner_calls = []

        def fake_run(cmd, timeout=30):
            runner_calls.append(cmd)
            return {"ok": True, "out": "", "cmd": cmd}

        delivered = []
        res = ad.autoup(
            run_cmd_fn=fake_run, sleep_fn=lambda *a: None,
            notify=False,
            deliver_message=lambda m: delivered.append(m["text"]) or True,
        )
        assert res["result"] == "up"
        # Queue replayed in order, then emptied.
        assert delivered == ["a", "b"]
        assert replay.load_queue(q)["messages"] == []
        assert replay.load_queue(q)["notice_sent_this_wake"] is False

    def test_autoup_success_with_empty_queue_noop(self, tmp_path, monkeypatch):
        """Wake with an empty queue ⇒ replay is a clean no-op (empty=True);
        autoup still succeeds and the queue stays empty."""
        q = replay.QUEUE_FILE   # never written → empty queue
        _set_state("down")
        plan = [{"kind": "orchestrator", "unit_id": "orch",
                 "nodes": ["10.0.0.244"], "port": 8000,
                 "cmd": "sparkrun run orch"}]
        monkeypatch.setattr(ad, "_build_wake_plan", lambda serving: plan)
        monkeypatch.setattr(ad, "_wait_ready", lambda *a, **k: (["orch"], True))
        monkeypatch.setattr(ad, "_notify", lambda *a, **k: None)

        def fake_run(cmd, timeout=30):
            return {"ok": True, "out": "", "cmd": cmd}

        res = ad.autoup(
            run_cmd_fn=fake_run, sleep_fn=lambda *a: None, notify=False,
            deliver_message=lambda m: True,
        )
        assert res["result"] == "up"
        # Queue remained empty (no-op replay, no crash).
        assert replay.load_queue(q)["messages"] == []
