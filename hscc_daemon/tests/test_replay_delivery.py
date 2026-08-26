"""Delivery-seam tests for replay.default_deliver_message (design t_f22f9d28).

The production delivery path is: resolve the queued message's chat/topic to an
orchestrator -> run it as ``hermes -p <profile> chat -Q --continue <session>
-q <text>`` -> post the reply back to the original chat/topic via
``telegram.send_message``. Every test drives it with injected fakes: a fake
registry fixture, a fake ``subprocess.run`` that captures argv, and a fake
``telegram.send_message``. NO test uses real Telegram, real models, or any real
cluster state (operator constraint).
"""

import json

from hscc_daemon import replay

# The autouse conftest isolation fixture stubs replay.default_deliver_message to
# a hermetic no-op so NO test can ever reach the live stack by accident. These
# delivery tests deliberately exercise the REAL production delivery path through
# its faked seams, so capture the real function at import time (before fixtures
# run) and restore it under monkeypatch in each test.
_REAL_DELIVER = replay.default_deliver_message


def _line(text="hello", chat="-1003906355027", user="desac",
          reply_to_id=None, reply_to_text="", thread_id=None, platform="telegram"):
    """Build a gateway-log line (thread_id/topic not in the real line by
    default — parse_gateway_line carries them only when present)."""
    r = "None" if reply_to_id is None else str(reply_to_id)
    base = (
        f"inbound message: platform={platform} user={user} chat={chat} "
        f"msg={text!r} reply_to_id={r} reply_to_text={reply_to_text!r}"
    )
    if thread_id is not None:
        base += f" thread_id={thread_id}"
    return base


def _write_registry(tmp_path, projects):
    """Write a registry file mapping project names -> topic ids."""
    p = tmp_path / "registry.yaml"
    p.write_text(json.dumps({"projects": projects}))
    return str(p)


def _msg(text="hello", chat_id="-1003906355027", thread_id="2046",
         reply_to_id=None, platform="telegram", topic=None):
    """A queued-message dict (the shape parse_gateway_line produces).

    ``arrived_iso`` is stamped to NOW so ``replay_queued``'s age bound never
    drops these fresh messages (the deliver tests exercise delivery, not age).
    """
    import datetime
    return {
        "platform": platform,
        "user": "desac",
        "chat_id": chat_id,
        "thread_id": thread_id,
        "topic": topic,
        "reply_to_id": reply_to_id,
        "text": text,
        "seq": 1,
        "arrived_iso": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

class TestMapping:
    """_resolve_identity maps chat/topic -> orchestrator deliberately."""

    def test_registry_project_topic_maps_to_its_orchestrator(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [
                {"name": "hscc", "topic": 2046},
                {"name": "ecofire-app", "topic": 2257},
            ]))
        ident = replay._resolve_identity(_msg(thread_id="2046"))
        assert ident == {"profile": "hscc-orch", "session": "hscc",
                         "project": "hscc"}

    def test_topic_field_used_as_fallback_for_thread_id(self, tmp_path,
                                                        monkeypatch):
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "sphoin", "topic": 2}]))
        # thread_id absent but topic present -> still resolved.
        msg = _msg(thread_id=None, topic="2")
        assert replay._resolve_identity(msg)["profile"] == "sphoin-orch"

    def test_unmatched_topic_or_no_topic_goes_to_general(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        # No thread/topic at all -> general catch-all (deliberate, not a guess).
        assert replay._resolve_identity(_msg(thread_id=None)) == {
            "profile": "general-orch", "session": "general", "project": None}
        # A topic that matches no registry project -> general.
        assert replay._resolve_identity(_msg(thread_id="999999"))["profile"] \
            == "general-orch"

    def test_non_telegram_unmappable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        assert replay._resolve_identity(_msg(platform="discord")) is None

    def test_missing_platform_unmappable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        msg = _msg(platform="")
        assert replay._resolve_identity(msg) is None

    def test_missing_registry_degrades_to_general_not_failure(self, tmp_path,
                                                              monkeypatch):
        """A missing/unreadable registry still maps a telegram message — to the
        general catch-all (never a failure)."""
        assert replay._resolve_identity(_msg(thread_id="2046")) == {
            "profile": "general-orch", "session": "general", "project": None}


class TestInvokeOrchestrator:
    """The orchestrator subprocess arg vector is exactly the API convention."""

    def test_argv_is_exact_hermes_invocation(self, monkeypatch):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            class _P:
                returncode = 0
                stdout = "the reply\n"
                stderr = ""
            return _P()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(replay, "HERMES_BIN", "hermes")
        reply = replay._invoke_orchestrator("hscc-orch", "hscc", "do a thing")
        assert reply == "the reply"
        assert captured["argv"] == [
            "hermes", "-p", "hscc-orch", "chat", "-Q", "--continue", "hscc",
            "-q", "do a thing",
        ]
        assert captured["argv"][2] == "hscc-orch"      # profile
        assert captured["argv"][6] == "hscc"           # session
        assert captured["argv"][8] == "do a thing"     # prompt is a discrete arg

    def test_prompt_is_list_element_not_shell_string(self, monkeypatch):
        """The user's text is a plain argv element — never interpolated into a
        shell command (no shell-injection). A "; rm -rf" prompt must be inert."""
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            class _P:
                returncode = 0
                stdout = "ok"
                stderr = ""
            return _P()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        multipart = 'hi; echo pwned > /tmp/x'
        replay._invoke_orchestrator("g-orch", "general", multipart)
        assert captured["argv"][-1] == multipart
        assert ";" not in captured["argv"][-2]  # nothing joined into a shell

    def test_session_not_found_raises(self, monkeypatch):
        def fake_run(argv, **kwargs):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "Error: Session not found: hscc"
            return _P()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        try:
            replay._invoke_orchestrator("hscc-orch", "hscc", "x")
            raised = False
        except RuntimeError:
            raised = True
        assert raised

    def test_empty_reply_raises(self, monkeypatch):
        def fake_run(argv, **kwargs):
            class _P:
                returncode = 0
                stdout = ""
                stderr = ""
            return _P()

        import subprocess
        monkeypatch.setattr(subprocess, "run", fake_run)
        try:
            replay._invoke_orchestrator("hscc-orch", "hscc", "x")
            raised = False
        except RuntimeError:
            raised = True
        assert raised


class TestDefaultDeliver:
    """default_deliver_message: the full orchestrator round-trip."""

    def test_full_round_trip_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        import subprocess
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        sent = {}

        def fake_run(argv, **kwargs):
            sent["argv"] = list(argv)
            class _P:
                returncode = 0
                stdout = "the reply"
                stderr = ""
            return _P()

        monkeypatch.setattr(subprocess, "run", fake_run)
        posted = []

        def fake_send(chat_id, text, **kw):
            posted.append((chat_id, text, kw))
            return True

        monkeypatch.setattr(replay, "send_message", fake_send)
        # The message's own topic is NOT a registry topic, but it's a telegram
        # message so it is mappable (general catch-all here). chat_id is the
        # original chat; the reply must go back there.
        msg = _msg(thread_id=None, reply_to_id=77)
        msg["topic"] = None
        assert replay.default_deliver_message(msg) is True
        # Reply routed back to the original chat/topic.
        assert posted and posted[0][0] == msg["chat_id"]
        assert posted[0][2]["thread_id"] is None
        assert posted[0][2]["reply_to_id"] == 77
        # Orchestrator was invoked with the project-agnostic general identity
        # for an unbound topic, carrying the user's text as the prompt.
        assert sent["argv"][2] == "general-orch"
        assert sent["argv"][6] == "general"
        assert sent["argv"][8] == "hello"
        assert posted[0][1] == "the reply"  # the orchestrator's reply is posted

    def test_project_topic_routes_reply_to_project_chat(self, tmp_path,
                                                        monkeypatch):
        """A message from a project's topic (thread_id matches a registry
        topic) is run by that project's orchestrator and replied into the same
        topic."""
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        import subprocess
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "ecofire-app", "topic": 2257}]))
        sent = {}

        def fake_run(argv, **kwargs):
            sent["argv"] = list(argv)
            class _P:
                returncode = 0
                stdout = "the reply"
                stderr = ""
            return _P()

        monkeypatch.setattr(subprocess, "run", fake_run)
        posted = []

        def fake_send(chat_id, text, **kw):
            posted.append((chat_id, text, kw))
            return True

        monkeypatch.setattr(replay, "send_message", fake_send)
        msg = _msg(chat_id="-100999", thread_id="2257", reply_to_id=5)
        assert replay.default_deliver_message(msg) is True
        assert sent["argv"][2] == "ecofire-app-orch"
        assert sent["argv"][6] == "ecofire-app"
        assert posted[0][0] == "-100999"          # original chat
        assert posted[0][2]["thread_id"] == "2257"  # original topic
        assert posted[0][2]["reply_to_id"] == 5

    def test_unmappable_non_telegram_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))

        def boom(*a, **k):
            raise AssertionError("must not invoke orchestrator")

        import subprocess
        monkeypatch.setattr(subprocess, "run", boom)
        assert replay.default_deliver_message(_msg(platform="discord")) is False

    def test_orchestrator_failure_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))

        def boom(argv, **k):
            class _P:
                returncode = 1
                stdout = ""
                stderr = "boom"
            return _P()

        import subprocess
        monkeypatch.setattr(subprocess, "run", boom)
        posted = []

        def fake_send(*a, **k):
            posted.append(a)
            return True

        monkeypatch.setattr(replay, "send_message", fake_send)
        assert replay.default_deliver_message(_msg(thread_id="2046")) is False
        assert posted == []  # no reply posted on orchestrator failure

    def test_reply_post_failure_returns_false(self, tmp_path, monkeypatch):
        """The orchestrator ran but the reply could not be posted -> NOT a
        faithful delivery -> False (retain)."""
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        import subprocess
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        sent = []

        def fake_run(argv, **kwargs):
            sent.append(list(argv))
            class _P:
                returncode = 0
                stdout = "the reply"
                stderr = ""
            return _P()

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(replay, "send_message", lambda *a, **k: False)
        assert replay.default_deliver_message(_msg(thread_id="2046")) is False
        assert len(sent) == 1  # orchestrator DID run — but reply failed

    def test_empty_text_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))

        def boom(*a, **k):
            raise AssertionError("must not run")

        import subprocess
        monkeypatch.setattr(subprocess, "run", boom)
        assert replay.default_deliver_message(_msg(text="  ")) is False


class TestReplayWithProductionSeam:
    """replay_queued driven with the REAL default_deliver_message (faked
    through its seams) — order, retain-on-failure, and dequeue-on-success."""

    def test_order_and_dequeue_via_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        import subprocess
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        q = str(tmp_path / "q.json")
        # Two telegram messages, both in an unmatched topic -> general-orch.
        for t in ["one", "two"]:
            replay.enqueue_inbound(_msg(text=t, thread_id=None), path=q)
        orders = []
        replies = iter(["R1", "R2"])

        def fake_run(argv, **kwargs):
            orders.append(argv[8])
            class _P:
                returncode = 0
                stdout = next(replies)
                stderr = ""
            return _P()

        monkeypatch.setattr(subprocess, "run", fake_run)
        posted = []

        def fake_send(chat_id, text, **kw):
            posted.append(text)
            return True

        monkeypatch.setattr(replay, "send_message", fake_send)
        res = replay.replay_queued(deliver_message=replay.default_deliver_message,
                                   path=q)
        assert res["delivered"] == 2
        assert orders == ["one", "two"]           # arrival order preserved
        assert posted == ["R1", "R2"]             # replies posted in order
        assert replay.load_queue(q)["messages"] == []  # emptied

    def test_unmappable_stays_queued_via_replay(self, tmp_path, monkeypatch):
        """A non-telegram message cannot be mapped -> stays queued, loud error,
        never silently dropped."""
        monkeypatch.setattr(replay, "default_deliver_message", _REAL_DELIVER)
        import subprocess
        monkeypatch.setattr(
            replay, "REGISTRY_PATH",
            _write_registry(tmp_path, [{"name": "hscc", "topic": 2046}]))
        q = str(tmp_path / "q.json")
        replay.enqueue_inbound(_msg(text="hi", platform="discord", thread_id=None),
                               path=q)
        replay.enqueue_inbound(_msg(text="second", platform="telegram",
                                    thread_id=None), path=q)
        boom = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not invoke orchestrator for a discord msg"))
        monkeypatch.setattr(replay, "_invoke_orchestrator", boom)
        res = replay.replay_queued(deliver_message=replay.default_deliver_message,
                                   path=q)
        # The unmappable (discord) message is retained.
        assert res["failed"] == 1
        assert res["delivered"] == 0
        st = replay.load_queue(q)
        assert [m["text"] for m in st["messages"]] == ["hi", "second"]
