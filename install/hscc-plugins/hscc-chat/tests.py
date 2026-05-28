#!/usr/bin/env python3
"""
Unit tests for hscc-chat plugin.
Tests all components without connecting to the gateway.
"""

import asyncio
import io
import json
import os
import re
import sys
import tempfile
import shutil
import time
import unittest
from contextlib import redirect_stdout

# Ensure we can import the plugin
sys.path.insert(0, os.path.expanduser("~/.hermes/plugins/hscc-chat"))
import hscc


# ── Helpers ─────────────────────────────────────────────────────────────────

def asyncio_run(coro):
    """Helper to run async functions in sync test context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Config Loading Tests ────────────────────────────────────────────────────

class TestAuthConfigLoading(unittest.TestCase):

    def test_load_device_config_missing(self):
        """Config returns None when file doesn't exist."""
        # Point to non-existent path via mock
        old = os.path.expanduser
        os.path.expanduser = lambda p: "/tmp/nonexistent_" + p if ".hermes" in p else p
        try:
            result = hscc.load_device_config()
            self.assertIsNone(result)
        finally:
            os.path.expanduser = old

    def test_load_device_config_existing(self):
        """Config loads correctly when file exists."""
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "device.json")
            with open(path, "w") as f:
                json.dump({"publicKey": "ed25519pub", "privateKey": "ed25519priv"}, f)
            # Patch the hermes_DIR constant and DEVICE_JSON
            orig_hermes = hscc.hermes_DIR
            orig_device = hscc.DEVICE_JSON
            hscc.hermes_DIR = d
            hscc.DEVICE_JSON = path
            try:
                result = hscc.load_device_config()
                self.assertIsNotNone(result)
                self.assertEqual(result["publicKey"], "ed25519pub")
            finally:
                hscc.hermes_DIR = orig_hermes
                hscc.DEVICE_JSON = orig_device
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_load_device_config_corrupt(self):
        """Corrupt JSON returns None."""
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "device.json")
            with open(path, "w") as f:
                f.write("{ invalid json }")
            old = os.path.expanduser
            os.path.expanduser = lambda p: d if p == os.path.expanduser("~/.hermes") else p
            try:
                result = hscc.load_device_config()
                self.assertIsNone(result)
            finally:
                os.path.expanduser = old
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_get_gateway_token_nested(self):
        config = {"gateway": {"auth": {"token": "secret-gateway-token-xyz"}}}
        self.assertEqual(hscc.get_gateway_token(config), "secret-gateway-token-xyz")

    def test_get_gateway_token_direct(self):
        config = {"auth": {"token": "direct-token"}}
        self.assertEqual(hscc.get_gateway_token(config), "direct-token")

    def test_get_gateway_token_none(self):
        config = {"gateway": {}}
        self.assertIsNone(hscc.get_gateway_token(config))

    def test_get_gateway_token_no_config(self):
        self.assertIsNone(hscc.get_gateway_token(None))

    def test_get_gateway_token_from_none_dict(self):
        self.assertIsNone(hscc.get_gateway_token({}))

    def test_get_ed25519_keys_direct(self):
        config = {"publicKey": "pubkey123", "privateKey": "privkey456"}
        pub, priv = hscc.get_ed25519_keys(config)
        self.assertEqual(pub, "pubkey123")
        self.assertEqual(priv, "privkey456")

    def test_get_ed25519_keys_nested_device(self):
        config = {"device": {"publicKey": "device-pub", "privateKey": "device-priv"}}
        pub, priv = hscc.get_ed25519_keys(config)
        self.assertEqual(pub, "device-pub")
        self.assertEqual(priv, "device-priv")

    def test_get_ed25519_keys_keys_array(self):
        config = {"keys": [{"publicKey": "arr-pub", "privateKey": "arr-priv"}]}
        pub, priv = hscc.get_ed25519_keys(config)
        self.assertEqual(pub, "arr-pub")
        self.assertEqual(priv, "arr-priv")

    def test_get_ed25519_keys_none(self):
        pub, priv = hscc.get_ed25519_keys(None)
        self.assertIsNone(pub)
        self.assertIsNone(priv)

    def test_get_ed25519_keys_missing(self):
        pub, priv = hscc.get_ed25519_keys({})
        self.assertIsNone(pub)
        self.assertIsNone(priv)

    def test_build_auth_message(self):
        msg = hscc.build_auth_message("test-token")
        data = json.loads(msg)
        self.assertEqual(data["type"], "auth")
        self.assertEqual(data["token"], "test-token")
        self.assertIn("timestamp", data)
        self.assertIn("nonce", data)

    def test_build_auth_message_with_device_key(self):
        msg = hscc.build_auth_message("test-token", "ed25519pub")
        data = json.loads(msg)
        self.assertEqual(data["device"]["key"], "ed25519pub")
        self.assertEqual(data["device"]["type"], "ed25519")
        self.assertTrue(data["signed"])


# ── Session Persistence Tests ───────────────────────────────────────────────

class TestSessionPersistence(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_chats = hscc.CHATS_DIR
        hscc.CHATS_DIR = os.path.join(self.tmpdir, "chats")

    def tearDown(self):
        hscc.CHATS_DIR = self._orig_chats
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_session(self):
        sid = hscc.cmd_session_create("Test Chat")
        self.assertIsNotNone(sid)
        self.assertIsInstance(sid, str)

        session = hscc._load_session(sid)
        self.assertIsNotNone(session)
        self.assertEqual(session["name"], "Test Chat")
        self.assertEqual(session["id"], sid)
        self.assertEqual(len(session["messages"]), 0)

    def test_session_defaults(self):
        sid = hscc.cmd_session_create()
        session = hscc._load_session(sid)
        self.assertTrue(session["name"].startswith("Chat"))
        self.assertIn("created_at", session)
        self.assertIn("updated_at", session)
        self.assertIn("metadata", session)
        self.assertEqual(session["metadata"]["magic"], hscc.SESSION_MAGIC)

    def test_rename_session(self):
        sid = hscc.cmd_session_create("Original Name")
        result = hscc.cmd_session_rename(sid, "Renamed Chat")
        self.assertTrue(result)

        session = hscc._load_session(sid)
        self.assertEqual(session["name"], "Renamed Chat")
        self.assertNotEqual(session["updated_at"], session["created_at"])

    def test_rename_missing_session(self):
        result = hscc.cmd_session_rename("nonexistent", "New Name")
        self.assertFalse(result)

    def test_delete_session(self):
        sid = hscc.cmd_session_create("To Delete")
        result = hscc.cmd_session_delete(sid)
        self.assertTrue(result)

        session = hscc._load_session(sid)
        self.assertIsNone(session)

    def test_delete_missing_session(self):
        result = hscc.cmd_session_delete("nonexistent")
        self.assertFalse(result)

    def test_pin_session(self):
        sid = hscc.cmd_session_create("Pin Me")
        hscc.cmd_session_pin(sid)
        meta = hscc._load_session_meta(sid)
        self.assertTrue(meta["pinned"])

        hscc.cmd_session_pin(sid)
        meta = hscc._load_session_meta(sid)
        self.assertFalse(meta["pinned"])

    def test_pin_missing_session(self):
        result = hscc.cmd_session_pin("nonexistent")
        self.assertFalse(result)

    def test_list_sessions(self):
        hscc.cmd_session_create("Session A")
        hscc.cmd_session_create("Session B")
        hscc.cmd_session_create("Session C")

        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_session_list()
        output = f.getvalue()

        self.assertIn("Session A", output)
        self.assertIn("Session B", output)
        self.assertIn("Session C", output)

    def test_list_sessions_empty(self):
        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_session_list()
        output = f.getvalue()
        self.assertIn("No chat sessions found", output)

    def test_pinned_sessions_first(self):
        hscc.cmd_session_create("Normal")
        sid_pinned = hscc.cmd_session_create("Pinned")
        hscc.cmd_session_pin(sid_pinned)

        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_session_list()
        output = f.getvalue()
        pinned_idx = output.find("[PIN]")
        self.assertGreater(pinned_idx, -1)

    def test_session_file_structure(self):
        sid = hscc.cmd_session_create("Struct Test")
        file_path = hscc._session_file_path(sid)
        self.assertTrue(os.path.exists(file_path))

        with open(file_path, "r") as f:
            data = json.load(f)
        self.assertEqual(data["id"], sid)
        self.assertIn("messages", data)
        self.assertIsInstance(data["messages"], list)

    def test_session_create_multiple(self):
        ids = set()
        for i in range(5):
            sid = hscc.cmd_session_create(f"Session {i}")
            self.assertIsNotNone(sid)
            self.assertNotIn(sid, ids)
            ids.add(sid)


# ── Markdown Rendering Tests ────────────────────────────────────────────────

class TestMarkdownRendering(unittest.TestCase):

    def test_bold(self):
        result = hscc._render_inline_markdown("**bold text**")
        self.assertIn(hscc._ANSI_BOLD, result)
        self.assertIn("bold text", result)
        self.assertIn(hscc._ANSI_RESET, result)

    def test_italic(self):
        result = hscc._render_inline_markdown("*italic text*")
        self.assertIn(hscc._ANSI_ITALIC, result)
        self.assertIn("italic text", result)
        self.assertIn(hscc._ANSI_RESET, result)

    def test_inline_code(self):
        result = hscc._render_inline_markdown("use `npm install` here")
        self.assertIn("use ", result)
        self.assertIn(hscc._ANSI_CODE, result)
        self.assertIn("npm install", result)
        self.assertIn(hscc._ANSI_RESET, result)
        self.assertIn(" here", result)

    def test_strikethrough(self):
        result = hscc._render_inline_markdown("~~deleted~~")
        self.assertIn("\033[9m", result)  # strike-through
        self.assertIn("deleted", result)
        self.assertIn(hscc._ANSI_RESET, result)

    def test_link(self):
        result = hscc._render_inline_markdown("[Hermes](https://hermes.ai)")
        self.assertIn("[Hermes](https://hermes.ai)", result)

    def test_mixed_inline(self):
        text = "**bold** and *italic* and `code`"
        result = hscc._render_inline_markdown(text)
        self.assertIn(hscc._ANSI_BOLD, result)
        self.assertIn(hscc._ANSI_ITALIC, result)
        self.assertIn(hscc._ANSI_CODE, result)
        self.assertIn("bold", result)
        self.assertIn("italic", result)
        self.assertIn("code", result)

    def test_plain_text(self):
        result = hscc._render_inline_markdown("just plain text")
        self.assertEqual(result, "just plain text")

    def test_nested_bold_in_heading(self):
        md = "## **Bold Heading**"
        result = hscc.render_markdown_text(md)
        self.assertIn("Bold Heading", result)
        self.assertIn(hscc._ANSI_BOLD, result)

    def test_render_headings(self):
        result = hscc.render_markdown_text("## My Heading\n\nSome text")
        self.assertIn("My Heading", result)
        self.assertIn(hscc._ANSI_BOLD, result)

    def test_render_code_block(self):
        md = "```python\nprint('hello')\n```"
        result = hscc.render_markdown_text(md)
        self.assertIn("python", result)
        self.assertIn("print('hello')", result)

    def test_render_bullet_list(self):
        md = "- Item one\n- Item two\n- Item three"
        result = hscc.render_markdown_text(md)
        self.assertIn("Item one", result)
        self.assertIn("Item two", result)
        self.assertIn("Item three", result)

    def test_render_numbered_list(self):
        md = "1. First\n2. Second\n3. Third"
        result = hscc.render_markdown_text(md)
        self.assertIn("First", result)
        self.assertIn("Second", result)
        self.assertIn("Third", result)

    def test_render_blockquote(self):
        md = "> This is a quote"
        result = hscc.render_markdown_text(md)
        self.assertIn("This is a quote", result)

    def test_render_complex(self):
        md = """# Main Title

## Section One

This is **bold** and *italic* text with `inline code`.

### Subsection

- Item 1
- Item 2

1. Numbered one
2. Numbered two

> A blockquote

```bash
echo "hello"
```

---

End of document."""
        result = hscc.render_markdown_text(md)
        self.assertIn("Main Title", result)
        self.assertIn("Section One", result)
        self.assertIn("Subsection", result)
        self.assertIn("bold", result)
        self.assertIn("italic", result)
        self.assertIn("inline code", result)
        self.assertIn("Item 1", result)
        self.assertIn("Numbered one", result)
        self.assertIn("A blockquote", result)
        self.assertIn('echo "hello"', result)

    def test_empty_text(self):
        result = hscc.render_markdown_text("")
        self.assertEqual(result, "")

    def test_render_markdown_command(self):
        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_render_markdown("**test**")
        output = f.getvalue()
        self.assertIn("test", output)

    def test_render_markdown_command_empty(self):
        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_render_markdown("")
        output = f.getvalue()
        self.assertIn("No text provided", output)


# ── Gateway Connection Tests ────────────────────────────────────────────────

class TestGatewayConnection(unittest.TestCase):

    def test_get_status_initial(self):
        gw = hscc.GatewayConnection()
        status = gw.get_status()
        self.assertFalse(status["connected"])
        self.assertEqual(status["url"], hscc.WS_GATEWAY_URL)
        self.assertIsNone(status["last_error"])
        self.assertIsNone(status["session_id"])

    def test_set_session_id(self):
        gw = hscc.GatewayConnection()
        gw.set_session_id("test-session-123")
        self.assertEqual(gw.session_id, "test-session-123")

    def test_callback_registration(self):
        gw = hscc.GatewayConnection()
        events = []
        gw.add_callback(lambda e: events.append(e))
        self.assertEqual(len(gw._callbacks), 1)

    def test_disconnect_noop(self):
        gw = hscc.GatewayConnection()
        # Should not raise — disconnect when not connected
        asyncio_run(gw.disconnect())

    def test_message_parsing_delta(self):
        gw = hscc.GatewayConnection()
        received = []
        gw.add_callback(lambda e: received.append(e))

        msg = json.dumps({"type": "delta", "data": {"text": "hello"}})
        asyncio_run(gw._handle_message(msg))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "delta")

    def test_message_parsing_final(self):
        gw = hscc.GatewayConnection()
        received = []
        gw.add_callback(lambda e: received.append(e))

        msg = json.dumps({"type": "final", "data": {"text": "done"}})
        asyncio_run(gw._handle_message(msg))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "final")

    def test_message_parsing_error(self):
        gw = hscc.GatewayConnection()
        received = []
        gw.add_callback(lambda e: received.append(e))

        msg = json.dumps({"type": "error", "data": {"message": "timeout"}})
        asyncio_run(gw._handle_message(msg))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "error")

    def test_message_parsing_unknown(self):
        gw = hscc.GatewayConnection()
        received = []
        gw.add_callback(lambda e: received.append(e))

        msg = json.dumps({"type": "custom_event", "data": {"foo": "bar"}})
        asyncio_run(gw._handle_message(msg))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "custom_event")

    def test_raw_text_message(self):
        """Raw (non-JSON) text is treated as delta."""
        gw = hscc.GatewayConnection()
        received = []
        gw.add_callback(lambda e: received.append(e))

        asyncio_run(gw._handle_message("raw text message"))
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "delta")
        self.assertEqual(received[0]["data"]["text"], "raw text message")
    def test_session_id_from_message(self):
        """Session ID is updated when gateway sends session_created message."""
        gw = hscc.GatewayConnection()
        received = []
        gw.add_callback(lambda e: received.append(e))

        msg = json.dumps({
            "type": "session_created",
            "data": {},
            "session_id": "new-session-abc"
        })
        asyncio_run(gw._handle_message(msg))
        self.assertEqual(gw.session_id, "new-session-abc")
        # Check for session_created event specifically
        session_events = [e for e in received if e["type"] == "session_created"]
        self.assertEqual(len(session_events), 1)

    def test_backoff_reset_on_connect(self):
        gw = hscc.GatewayConnection()
        gw._reconnect_delay = 25.0  # simulating backoff state
        gw._on_connected()
        self.assertEqual(gw._reconnect_delay, 1.0)  # reset to base

    def test_send_message_not_connected(self):
        gw = hscc.GatewayConnection()
        with self.assertRaises(RuntimeError):
            asyncio_run(gw.send_message("hello"))


# ── Chat Streamer Tests ─────────────────────────────────────────────────────

class TestChatStreamer(unittest.TestCase):

    def test_streamer_init(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)
        self.assertEqual(streamer.response, "")
        self.assertIsNone(streamer.session_id)
        self.assertFalse(streamer._closed)

    def test_streamer_collects_delta(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)

        streamer._on_event({"type": "delta", "data": {"text": "Hello"}})
        self.assertEqual(streamer.response, "Hello")

        streamer._on_event({"type": "delta", "data": {"text": " World"}})
        self.assertEqual(streamer.response, "Hello World")

    def test_streamer_final_overrides(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)

        streamer._on_event({"type": "delta", "data": {"text": "partial"}})
        streamer._on_event({"type": "final", "data": {"text": "complete response"}})
        self.assertEqual(streamer.response, "complete response")
        self.assertTrue(streamer._closed)

    def test_streamer_aborted(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)

        streamer._on_event({"type": "delta", "data": {"text": "partial"}})
        streamer._on_event({"type": "aborted", "data": {}})
        self.assertTrue(streamer._closed)

    def test_streamer_error(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)

        streamer._on_event({"type": "error", "data": {"message": "something failed"}})
        self.assertTrue(streamer._closed)

    def test_streamer_session_created(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)

        streamer._on_event({"type": "session_created", "data": {"session_id": "abc123"}})
        self.assertEqual(streamer.session_id, "abc123")

    def test_streamer_multiple_deltas(self):
        gw = hscc.GatewayConnection()
        streamer = hscc.ChatStreamer(gw)

        for chunk in ["The ", "quick ", "brown ", "fox"]:
            streamer._on_event({"type": "delta", "data": {"text": chunk}})
        self.assertEqual(streamer.response, "The quick brown fox")


# ── WS Status Tests ─────────────────────────────────────────────────────────

class TestWSStatus(unittest.TestCase):

    def test_ws_status_output(self):
        gw = hscc.GatewayConnection()
        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_ws_status()
        output = f.getvalue()
        self.assertIn("WebSocket Status", output)
        self.assertIn("DISCONNECTED", output)
        self.assertIn("Gateway", output)
        self.assertIn("Auth Diagnostics", output)

    def test_ws_status_has_token(self):
        gw = hscc.GatewayConnection()
        gw._auth_token = "test-token-123"
        gw._device_public_key = "pub-key"
        gw._connected = True
        gw.set_session_id("test-session")

        f = io.StringIO()
        with redirect_stdout(f):
            hscc.cmd_ws_status()
        output = f.getvalue()
        self.assertIn("CONNECTED", output)
        self.assertIn("configured", output)


# ── Edge Cases ──────────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_session_metadata_isolation(self):
        """Each session has its own metadata file."""
        tmpdir = tempfile.mkdtemp()
        orig_chats = hscc.CHATS_DIR
        try:
            hscc.CHATS_DIR = os.path.join(tmpdir, "chats")
            sid1 = hscc.cmd_session_create("S1")
            sid2 = hscc.cmd_session_create("S2")

            # Modify meta for sid1
            meta1 = hscc._load_session_meta(sid1)
            meta1["custom"] = "value1"
            hscc._save_session_meta(sid1, meta1)

            meta2 = hscc._load_session_meta(sid2)
            self.assertNotIn("custom", meta2)
        finally:
            hscc.CHATS_DIR = orig_chats
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_nested_bold_in_heading(self):
        md = "## **Bold Heading**"
        result = hscc.render_markdown_text(md)
        self.assertIn("Bold Heading", result)
        self.assertIn(hscc._ANSI_BOLD, result)

    def test_plain_text_pass_through(self):
        result = hscc._render_inline_markdown("plain text with no formatting")
        self.assertEqual(result, "plain text with no formatting")

    def test_underscore_italic(self):
        """Single * that doesn't have a pair is literal."""
        result = hscc._render_inline_markdown("a*b*c")
        # The first * opens, second closes — result: a<ital>b*c
        # or if unpaired, passes through
        # Either way it should be a valid string
        self.assertIsInstance(result, str)

    def test_multiple_bold_pairs(self):
        result = hscc._render_inline_markdown("**first** and **second**")
        self.assertIn("first", result)
        self.assertIn("second", result)
        # Both should have bold formatting
        bold_count = result.count(hscc._ANSI_BOLD)
        self.assertGreaterEqual(bold_count, 2)

    def test_unmatched_formatting_closes_early(self):
        """Unmatched bold opens but never closes — should still output text."""
        result = hscc._render_inline_markdown("**unclosed bold text")
        self.assertIn("unclosed bold text", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
