"""Multi-address bind coverage for the HSCC API.

The API can listen on SEVERAL addresses at once (LAN + Tailscale), each with
the same port/token/handlers. These tests use only the documented placeholders
(192.0.2.x / 100.64.0.x) and loopback; no live LAN/tailnet address is touched.
"""
import http.client
import json
import socket
import threading

import pytest

import api_server


def _request(host, port, token=None):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Authorization": "Bearer " + token} if token else {}
    conn.request("GET", "/v1/ping", headers=headers)
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    return resp.status, body


def _free_ipv6_port():
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.bind(("::1", 0))
        sock.listen(1)
        return sock, sock.getsockname()[1]
    except OSError:
        sock.close()
        raise


def test_two_binds_are_served_with_one_token_and_port(hscc_dir):
    server = api_server.create_server(
        hscc_dir=hscc_dir,
        bind_overrides=["127.0.0.1", "::1"],
        port_override=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.ctx.config["port"]
        token = api_server.load_token(hscc_dir)
        assert server.ctx.config["hosts"] == ["127.0.0.1", "::1"]
        assert server.ctx.config["host"] == "127.0.0.1"
        for host in ("127.0.0.1", "::1"):
            status, body = _request(host, port, token)
            assert status == 200 and body["service"] == "hscc-api"
            # Same token is enforced on every listener.
            assert _request(host, port, "wrong")[0] == 401
    finally:
        server.shutdown()
        server.server_close()


def test_first_bind_is_primary_and_duplicates_are_deduped(hscc_dir, monkeypatch):
    monkeypatch.setattr(
        api_server, "local_interface_addresses",
        lambda: {"127.0.0.1", "::1", "192.0.2.10"})
    cfg = api_server.resolve_config(
        hscc_dir,
        bind_overrides=["192.0.2.10", "127.0.0.1", "192.0.2.10"],
    )
    assert cfg["hosts"] == ["192.0.2.10", "127.0.0.1"]
    assert cfg["host"] == "192.0.2.10"       # legacy field = primary
    assert cfg["unavailable"] == []


def test_tailscale_combines_with_bind_primary_first(hscc_dir, monkeypatch):
    monkeypatch.setattr(api_server, "_find_tailnet_ip", lambda: "100.64.0.1")
    monkeypatch.setattr(
        api_server, "local_interface_addresses",
        lambda: {"127.0.0.1", "::1", "192.0.2.10"})
    cfg = api_server.resolve_config(
        hscc_dir, bind_overrides=["192.0.2.10"], tailscale=True)
    assert cfg["hosts"] == ["192.0.2.10", "100.64.0.1"]
    assert cfg["host"] == "192.0.2.10"


def test_tailscale_down_keeps_other_bind_up_and_reports_it(hscc_dir, monkeypatch):
    monkeypatch.setattr(api_server, "_find_tailnet_ip", lambda: None)
    cfg = api_server.resolve_config(
        hscc_dir, bind_overrides=["127.0.0.1"], tailscale=True)
    assert cfg["hosts"] == ["127.0.0.1"]
    assert cfg["unavailable"]
    assert cfg["unavailable"][0]["bind"] == "tailscale"


def test_tailscale_only_down_still_fails_closed(hscc_dir, monkeypatch):
    monkeypatch.setattr(api_server, "_find_tailnet_ip", lambda: None)
    with pytest.raises(RuntimeError, match="[Tt]ailscale|tailnet"):
        api_server.resolve_config(hscc_dir, tailscale=True)


def test_zero_bind_in_a_list_is_refused(hscc_dir):
    for bad in ("0.0.0.0", "::"):
        with pytest.raises(RuntimeError, match="must never be reachable"):
            api_server.resolve_config(
                hscc_dir, bind_overrides=["127.0.0.1", bad])


def test_non_local_address_is_refused(hscc_dir):
    """192.0.2.5 is a documented placeholder and is not on this host."""
    with pytest.raises(RuntimeError, match="not an address on this host"):
        api_server.resolve_config(hscc_dir, bind_overrides=["192.0.2.5"])


def test_one_bind_fails_then_the_whole_start_fails_and_closes_earlier_ones(
        hscc_dir):
    """No half-up state: a later address failing must close the earlier bind."""
    holder, port = _free_ipv6_port()
    try:
        with pytest.raises(OSError, match="cannot bind"):
            api_server.create_server(
                hscc_dir=hscc_dir,
                bind_overrides=["127.0.0.1", "::1"],
                port_override=port,
            )
        # The first listener really was released by the failed start.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
    finally:
        holder.close()


def test_state_file_keeps_primary_host_and_lists_all(hscc_dir):
    entry = api_server.write_api_state(
        {"host": "192.0.2.10", "hosts": ["192.0.2.10", "100.64.0.1"],
         "port": 8787, "unavailable": []},
        hscc_dir,
    )
    assert entry["host"] == "192.0.2.10"     # old consumers
    assert entry["hosts"] == ["192.0.2.10", "100.64.0.1"]
    on_disk = api_server.read_api_state(hscc_dir)
    assert on_disk == entry
    api_server.clear_api_state(hscc_dir)
    assert api_server.read_api_state(hscc_dir) == {}


def test_serve_argv_accepts_repeated_bind_plus_tailscale():
    cmd, binds, tailscale, port = api_server._parse_serve_argv([
        "serve", "--bind", "127.0.0.1", "--tailscale",
        "--bind", "::1", "--port", "8788", "--no-qr",
    ])
    assert cmd == "serve"
    assert binds == ["127.0.0.1", "::1"]
    assert tailscale is True
    assert port == 8788
