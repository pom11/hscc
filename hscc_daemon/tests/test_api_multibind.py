"""Multi-address start/status/QR/launchd coverage for `hscc api`.

All addresses are documented placeholders (192.0.2.x / 100.64.0.x) or
loopback; nothing here binds a real LAN/tailnet address.
"""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout

import pytest

import hscc_daemon.api_cli as api_cli
import hscc_daemon.api_launchd as api_launchd


def test_parse_start_flags_repeatable_bind_and_tailscale():
    binds, tailscale, port, no_qr = api_cli._parse_start_flags([
        "--bind", "192.0.2.10", "--tailscale",
        "--bind", "127.0.0.1", "--port", "8788", "--no-qr",
    ])
    assert binds == ["192.0.2.10", "127.0.0.1"]
    assert tailscale is True
    assert port == 8788 and no_qr is True


def test_serve_args_carries_every_bind_then_tailscale():
    extra = api_launchd.serve_args(
        binds=["192.0.2.10", "100.64.0.1"], tailscale=True, port=8788)
    assert extra == ["--bind", "192.0.2.10", "--bind", "100.64.0.1",
                     "--tailscale", "--port", "8788"]


def test_plist_program_arguments_carry_all_bind_flags():
    extra = api_launchd.serve_args(
        binds=["192.0.2.10", "100.64.0.1"], tailscale=True, port=8788)
    plist = api_launchd.build_api_plist(
        "/opt/venv/bin/python3", "/repo/hscc-api", serve_extra_args=extra)
    assert plist["ProgramArguments"] == [
        "/opt/venv/bin/python3", "-m", "api_server", "serve",
        "--bind", "192.0.2.10", "--bind", "100.64.0.1",
        "--tailscale", "--port", "8788",
    ]


def test_qr_payload_carries_all_hosts_primary_first_and_old_prefix():
    payload = api_cli._build_qr_payload(
        "192.0.2.10", 8787, "TOK123",
        hosts=["192.0.2.10", "100.64.0.1"])
    assert payload == (
        '{"v":1,"host":"192.0.2.10","port":8787,"token":"TOK123",'
        '"hosts":["192.0.2.10","100.64.0.1"]}'
    )
    # Old apps read these exact keys/order from the prefix.
    assert payload.startswith(
        '{"v":1,"host":"192.0.2.10","port":8787,"token":"TOK123"')
    assert not payload.endswith("\n")


def test_qr_payload_without_hosts_is_still_the_legacy_contract():
    assert api_cli._build_qr_payload("100.64.0.3", 8787, "TOK123") == (
        '{"v":1,"host":"100.64.0.3","port":8787,"token":"TOK123"}')


class _FakeApi:
    def resolve_config(self, **kw):
        return {
            "host": "192.0.2.10",
            "hosts": ["192.0.2.10", "100.64.0.1"],
            "port": 8787,
            "unavailable": [{"bind": "tailscale",
                             "reason": "no tailnet IP could be found"}],
        }

    def load_token(self):
        raise RuntimeError("token unreadable (test)")


def test_status_lists_every_address_and_the_tailnet_miss(monkeypatch, tmp_path):
    monkeypatch.setattr(api_cli, "_load_api_server", lambda: _FakeApi())
    monkeypatch.setattr(api_cli.sys, "platform", "linux")
    pid_file = tmp_path / "api.pid"
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(pid_file))

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli._handle_status(["--no-qr"])
    text = out.getvalue()
    assert rc == 0
    assert "192.0.2.10:8787" in text
    assert "100.64.0.1:8787" in text
    assert "tailscale" in text and "unavailable" in text.lower()


def test_status_reads_runtime_state_for_start_flags(monkeypatch, tmp_path):
    """Config alone cannot know --bind/--tailscale flags; the state snapshot can."""
    class _ConfigOnlyApi(_FakeApi):
        def resolve_config(self, **kw):
            return {"host": "127.0.0.1", "hosts": ["127.0.0.1"],
                    "port": 8787, "unavailable": []}

        def read_api_state(self):
            return {"host": "192.0.2.10",
                    "hosts": ["192.0.2.10", "100.64.0.1"],
                    "port": 8787, "unavailable": []}

    monkeypatch.setattr(api_cli, "_load_api_server", lambda: _ConfigOnlyApi())
    monkeypatch.setattr(api_cli.sys, "platform", "linux")
    pid_file = tmp_path / "api.pid"
    pid_file.write_text(str(os.getpid()))
    monkeypatch.setattr(api_cli, "API_PID_FILE", str(pid_file))

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        assert api_cli._handle_status(["--no-qr"]) == 0
    text = out.getvalue()
    assert "192.0.2.10:8787" in text
    assert "100.64.0.1:8787" in text
    assert "127.0.0.1:8787" not in text
