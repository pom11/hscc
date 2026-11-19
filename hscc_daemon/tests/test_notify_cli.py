"""``hscc notify`` — parsing, connection resolution and the real dispatch.

The verb changed from "send one desktop notification" to "post one alert to
the HSCC feed" (card N1). The failure this file exists to prevent is the
classic CLI one: unit tests pass because they call the library, while the
argparse/argv path a human types is broken. So the last test drives
``hscc_daemon.hscc.main`` with the exact words an operator would type and
asserts the parsed argv reaches the verb unjoined (the previous dispatcher
``" ".join(args[1:])`` would have destroyed the title/body boundary).

Hermetic: a loopback stub server stands in for the API, the hscc root is a
tmp ``$HSCC_HOME`` with its own ``api-token``, and no request ever leaves
127.0.0.1.
"""

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import hscc_daemon.hscc as unified
from hscc_daemon import notify_cli


# --------------------------------------------------------------------------- #
# A loopback stand-in for POST /v1/alerts
# --------------------------------------------------------------------------- #

class _StubAPI(BaseHTTPRequestHandler):
    seen = []
    response = None
    status = 201

    def do_POST(self):  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        type(self).seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(raw) if raw else None,
        })
        payload = type(self).response or {
            "alert": {"id": 7, "ts": "2026-11-19T00:00:00Z",
                      "title": (type(self).seen[-1]["body"] or {}).get("title"),
                      "body": (type(self).seen[-1]["body"] or {}).get("body"),
                      "level": (type(self).seen[-1]["body"] or {}).get("level"),
                      "key": (type(self).seen[-1]["body"] or {}).get("key"),
                      "acked": False, "acked_at": None},
            "created": True, "deduped": False, "rate_limited": False,
            "reason": "created", "retry_after_s": None,
        }
        data = json.dumps(payload).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub_api():
    _StubAPI.seen = []
    _StubAPI.response = None
    _StubAPI.status = 201
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def hscc_home(tmp_path, monkeypatch):
    root = tmp_path / "hscc"
    root.mkdir()
    (root / "api-token").write_text("test-token\n")
    monkeypatch.setenv("HSCC_HOME", str(root))
    return root


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_two_positionals_are_title_and_body():
    opts, err = notify_cli.parse_notify_args(
        ["Approve LIVE", "example-project wants to deploy",
         "--level", "critical", "--key", "live/example"])
    assert err is None
    assert opts["title"] == "Approve LIVE"
    assert opts["body"] == "example-project wants to deploy"
    assert opts["level"] == "critical"
    assert opts["key"] == "live/example"


def test_flag_equals_form_and_any_position():
    opts, err = notify_cli.parse_notify_args(
        ["--level=action", "T", "--key=k", "B"])
    assert err is None
    assert (opts["title"], opts["body"]) == ("T", "B")
    assert opts["level"] == "action" and opts["key"] == "k"


def test_one_positional_is_the_legacy_message_form():
    opts, err = notify_cli.parse_notify_args(["Hello World"])
    assert err is None
    assert opts["title"] == "HSCC"
    assert opts["body"] == "Hello World"


@pytest.mark.parametrize("argv", [
    [],
    ["T", "B", "C"],
    ["T", "--level", "noisy"],
    ["T", "--key"],
    ["T", "--wat"],
    ["T", "--level=noisy"],
])
def test_bad_argv_is_refused_with_a_sentence(argv):
    opts, err = notify_cli.parse_notify_args(argv)
    assert opts is None
    assert err


def test_help_wins_over_validation():
    opts, err = notify_cli.parse_notify_args(["--help"])
    assert err is None and opts["help"] is True


# --------------------------------------------------------------------------- #
# Connection resolution
# --------------------------------------------------------------------------- #

def test_api_state_is_preferred_and_loopback_comes_first(tmp_path, monkeypatch):
    monkeypatch.delenv("HSCC_API_URL", raising=False)
    (tmp_path / "api-state.json").write_text(json.dumps({
        "host": "100.64.0.9", "hosts": ["100.64.0.9", "127.0.0.1"],
        "port": 9999,
    }))
    urls = notify_cli.resolve_api_urls(hscc_dir=str(tmp_path))
    assert urls[0] == "http://127.0.0.1:9999"
    assert "http://100.64.0.9:9999" in urls


def test_explicit_flag_beats_every_other_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HSCC_API_URL", "http://127.0.0.1:1111")
    urls = notify_cli.resolve_api_urls("http://127.0.0.1:2222/",
                                       hscc_dir=str(tmp_path))
    assert urls[0] == "http://127.0.0.1:2222"


# --------------------------------------------------------------------------- #
# The POST
# --------------------------------------------------------------------------- #

def test_main_posts_title_body_level_and_key(stub_api, hscc_home, capsys):
    rc = notify_cli.main([
        "Approve LIVE", "example-project wants to deploy",
        "--level", "action", "--key", "live/example",
        "--api", stub_api,
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "alert 7 posted (action) key=live/example" in out

    assert len(_StubAPI.seen) == 1
    request = _StubAPI.seen[0]
    assert request["path"] == "/v1/alerts"
    assert request["authorization"] == "Bearer test-token"
    assert request["body"] == {
        "title": "Approve LIVE",
        "body": "example-project wants to deploy",
        "level": "action",
        "key": "live/example",
    }


def test_json_flag_prints_the_server_payload(stub_api, hscc_home, capsys):
    rc = notify_cli.main(["T", "B", "--json", "--api", stub_api])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True
    assert payload["alert"]["id"] == 7


def test_dedupe_and_rate_limit_are_success_with_a_plain_sentence(
        stub_api, hscc_home, capsys):
    _StubAPI.status = 200
    _StubAPI.response = {
        "alert": {"id": 7}, "created": False, "deduped": True,
        "rate_limited": False, "reason": "duplicate", "retry_after_s": None,
    }
    assert notify_cli.main(["T", "B", "--api", stub_api]) == 0
    assert "unchanged (deduped)" in capsys.readouterr().out

    _StubAPI.response = {
        "alert": None, "created": False, "deduped": False,
        "rate_limited": True, "reason": "rate_limited_global",
        "retry_after_s": 42.0,
    }
    assert notify_cli.main(["T", "B", "--api", stub_api]) == 0
    assert "suppressed (rate_limited_global, retry in 42s)" in capsys.readouterr().out


def test_missing_token_names_the_remedy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HSCC_HOME", str(tmp_path))
    rc = notify_cli.main(["T", "B", "--api", "http://127.0.0.1:1"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "hscc api start" in captured.err


def test_unreachable_api_exits_1_without_a_traceback(
        tmp_path, monkeypatch, capsys):
    (tmp_path / "api-token").write_text("tok\n")
    monkeypatch.setenv("HSCC_HOME", str(tmp_path))
    rc = notify_cli.main(["T", "B", "--api", "http://127.0.0.1:1"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "hscc api start" in captured.err
    assert "Traceback" not in captured.err


def test_api_refusal_surfaces_the_server_sentence(stub_api, hscc_home, capsys):
    _StubAPI.status = 400
    _StubAPI.response = {"error": {"code": "bad_request",
                                   "message": "level must be one of info, action, critical"}}
    rc = notify_cli.main(["T", "B", "--api", stub_api])
    captured = capsys.readouterr()
    assert rc == 1
    assert "API refused the alert (400)" in captured.err
    assert "level must be one of" in captured.err


# --------------------------------------------------------------------------- #
# The real dispatch (the "green pytest != working CLI" leg)
# --------------------------------------------------------------------------- #

def test_main_dispatches_the_verb_with_unjoined_argv(monkeypatch):
    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(notify_cli, "main", fake_main)
    monkeypatch.setattr(sys, "argv", [
        "hscc", "notify", "Approve LIVE", "example-project wants to deploy",
        "--level", "action",
    ])
    with pytest.raises(SystemExit) as exc:
        unified.main()
    assert exc.value.code == 0
    assert calls == [[
        "Approve LIVE", "example-project wants to deploy", "--level", "action",
    ]]


def test_help_token_never_dispatches_the_verb(monkeypatch):
    calls = []
    monkeypatch.setattr(notify_cli, "main", lambda argv: calls.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["hscc", "notify", "msg", "-h"])
    out = io.StringIO()

    from contextlib import redirect_stdout
    with redirect_stdout(out), pytest.raises(SystemExit) as exc:
        unified.main()
    assert exc.value.code == 0
    assert "hscc notify" in out.getvalue()
    assert calls == []
