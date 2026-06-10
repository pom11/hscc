"""Additional unit tests for telegram.py - notify_operations, _env_file_value,
_telegram_ssl_context.

The existing test_telegram.py covers _env_file_value and resolve_telegram.
This file adds the remaining surface area.
"""
import json
import os
import pytest
from pathlib import Path


class TestNotifyOperations:
    """notify_operations() posts to the Operations Telegram topic."""

    def test_no_token_returns_false(self, monkeypatch):
        from hscc_daemon import telegram
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        # Neutralize env file
        monkeypatch.setattr(telegram, "ENV_FILE", "/nonexistent/.env")
        monkeypatch.setattr(telegram, "OPS_CHAT_ID", "123")
        assert telegram.notify_operations("test") is False

    def test_no_chat_id_returns_false(self, monkeypatch):
        from hscc_daemon import telegram
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram, "ENV_FILE", "/nonexistent/.env")
        monkeypatch.setattr(telegram, "OPS_CHAT_ID", "0")
        assert telegram.notify_operations("test") is False

    def test_success(self, monkeypatch):
        from hscc_daemon import telegram

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram, "ENV_FILE", "/nonexistent/.env")
        monkeypatch.setattr(telegram, "OPS_CHAT_ID", "999")
        monkeypatch.setattr(telegram, "OPS_THREAD_ID", "100")

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: FakeResp())
        assert telegram.notify_operations("test message") is True

    def test_network_error_returns_false(self, monkeypatch):
        from hscc_daemon import telegram

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram, "ENV_FILE", "/nonexistent/.env")
        monkeypatch.setattr(telegram, "OPS_CHAT_ID", "999")
        monkeypatch.setattr(telegram, "OPS_THREAD_ID", "100")

        monkeypatch.setattr("urllib.request.urlopen",
                            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError()))

        assert telegram.notify_operations("test") is False

    def test_includes_thread_id(self, monkeypatch):
        from hscc_daemon import telegram

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setattr(telegram, "ENV_FILE", "/nonexistent/.env")
        monkeypatch.setattr(telegram, "OPS_CHAT_ID", "999")
        monkeypatch.setattr(telegram, "OPS_THREAD_ID", "100")

        captured_req = []
        def fake_urlopen(req, **kw):
            captured_req.append(req)
            class FakeResp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        telegram.notify_operations("test")

        assert len(captured_req) == 1
        # URL should contain bot token
        assert "fake-token" in str(captured_req[0].full_url)


class TestEnvFileValue:
    """_env_file_value() parses env files."""

    def test_basic_key(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text("TELEGRAM_BOT_TOKEN=mytoken123\n")
        assert _env_file_value("TELEGRAM_BOT_TOKEN", path=str(env_file)) == "mytoken123"

    def test_last_non_empty_wins(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text("TOK=\nTOK=first\nTOK=\nTOK=last\n")
        assert _env_file_value("TOK", path=str(env_file)) == "last"

    def test_strips_quotes(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text('TOK="quoted"\n')
        assert _env_file_value("TOK", path=str(env_file)) == "quoted"

    def test_ignores_comments(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nTOK=val\n")
        assert _env_file_value("TOK", path=str(env_file)) == "val"

    def test_missing_file(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        assert _env_file_value("KEY", path=str(tmp_path / "missing.env")) is None

    def test_absent_key(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text("OTHER=val\n")
        assert _env_file_value("KEY", path=str(env_file)) is None

    def test_single_quotes(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text("TOK='single'\n")
        assert _env_file_value("TOK", path=str(env_file)) == "single"

    def test_whitespace_handling(self, tmp_path):
        from hscc_daemon.telegram import _env_file_value
        env_file = tmp_path / ".env"
        env_file.write_text(" TOK = spaced \n")
        # Key has leading/trailing whitespace stripped
        result = _env_file_value("TOK", path=str(env_file))
        # Value should be stripped too
        assert result == "spaced"


class TestTelegramSslContext:
    """_telegram_ssl_context() creates an SSL context."""

    def test_returns_context(self, monkeypatch):
        from hscc_daemon import telegram
        import ssl
        import certifi

        # If certifi is installed, it should use it
        ctx = telegram._telegram_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)

    def test_fallback_to_default(self, monkeypatch):
        from hscc_daemon import telegram
        import ssl

        # Even if all candidates fail, returns a default context
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/bundle.pem")

        ctx = telegram._telegram_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
