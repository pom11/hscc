#!/usr/bin/env python3
"""Unit tests for daemon Telegram credential resolution (pure, no network).

Run: cd hscc_daemon && python3 -m unittest tests.test_telegram -v
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


def _load():
    p = Path(__file__).resolve().parent.parent / "daemon.py"
    spec = importlib.util.spec_from_file_location("daemon_tg_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


H = _load()


class TestEnvFileValue(unittest.TestCase):
    def _write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_missing_file_none(self):
        self.assertIsNone(H._env_file_value("X", path="/nonexistent/.env"))

    def test_last_non_empty_wins(self):
        p = self._write("TOK=\nTOK=first\nTOK=\nTOK=last\n")
        self.assertEqual(H._env_file_value("TOK", path=p), "last")

    def test_strips_quotes_and_ignores_comments(self):
        p = self._write('# c\nTOK="quoted"\nOTHER=x\n')
        self.assertEqual(H._env_file_value("TOK", path=p), "quoted")

    def test_absent_key_none(self):
        p = self._write("A=1\nB=2\n")
        self.assertIsNone(H._env_file_value("TOK", path=p))


class TestResolveTelegram(unittest.TestCase):
    def setUp(self):
        # Isolate from the real process env for deterministic precedence.
        for k in ("TELEGRAM_BOT_TOKEN", "HSCC_NOTIFY_CHAT", "TELEGRAM_CHAT_ID"):
            self.addCleanup(os.environ.pop, k, None)
            os.environ.pop(k, None)
        self._orig_env_file = H.ENV_FILE
        H.ENV_FILE = "/nonexistent/.env"  # neutralize file fallback
        self.addCleanup(setattr, H, "ENV_FILE", self._orig_env_file)

    def test_config_wins(self):
        cfg = {"telegram": {"bot_token": "cfgtok", "chat_id": "999"}}
        self.assertEqual(H.resolve_telegram(cfg), ("cfgtok", "999"))

    def test_env_token_and_chat(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "envtok"
        os.environ["HSCC_NOTIFY_CHAT"] = "777"
        self.assertEqual(H.resolve_telegram(None), ("envtok", "777"))

    def test_chat_falls_back_to_operator(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "envtok"
        token, chat = H.resolve_telegram(None)
        self.assertEqual(token, "envtok")
        self.assertEqual(chat, H.OPERATOR_CHAT_FALLBACK)

    def test_no_token_returns_none(self):
        token, chat = H.resolve_telegram(None)
        self.assertIsNone(token)
        self.assertEqual(chat, H.OPERATOR_CHAT_FALLBACK)


class TestTelegramPostGuards(unittest.TestCase):
    def test_no_token_or_chat_false(self):
        self.assertFalse(H.telegram_post(None, "1", "hi"))
        self.assertFalse(H.telegram_post("t", None, "hi"))


if __name__ == "__main__":
    unittest.main()
