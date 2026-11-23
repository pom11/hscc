"""Bounded API connections (t_c24521ef).

The live API hit EMFILE (258 FDs against launchd's 256 soft limit) and every
route failed until it was restarted. HTTP/1.1 keep-alive was enabled with no
idle timeout, so a client that walked away left its accepted socket (and the
handler thread) pinned forever.

These tests drive the real threaded server over loopback: idle keep-alives must
be reclaimed after ``ApiHandler.timeout`` and the process FD count must return
to baseline, and the concurrency cap must refuse excess connections instead of
opening an unbounded number of sockets.
"""
import os
import select
import socket
import time

import pytest

import api_server
from tests.test_api import RunningServer

# The real idle timeout is 15 s; give the OS a little room to deliver the FIN.
_TIMEOUT_WAIT_S = api_server.ApiHandler.timeout + 5


def _fd_count():
    """Open FDs for this process, cross-platform and dependency-free."""
    for root in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(root))
        except OSError:
            continue
    pytest.skip("no /proc/self/fd or /dev/fd on this platform")


def _open_idle_keepalive(host, port, token):
    """Send one authenticated GET and keep the connection open (keep-alive)."""
    sock = socket.create_connection((host, port), timeout=5)
    request = (
        f"GET /v1/ping HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Connection: keep-alive\r\n"
        f"\r\n"
    )
    sock.sendall(request.encode("ascii"))
    data = b""
    deadline = time.time() + 5
    # Headers, then the exact Content-Length body. Reading both is what puts
    # the handler back into its idle read() loop with the socket still open.
    while b"\r\n\r\n" not in data:
        assert time.time() < deadline, "server did not answer an idle probe"
        chunk = sock.recv(4096)
        assert chunk, "server closed before answering the probe"
        data += chunk
    header, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in header.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        chunk = sock.recv(4096)
        assert chunk, "server closed mid-body"
        body += chunk
    assert b"200" in header.split(b"\r\n")[0], header
    return sock


def _peer_closed(sock):
    """True once the server's FIN has arrived (the client side is still open)."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    try:
        return sock.recv(1) == b""
    except (ConnectionResetError, OSError):
        return True


def test_idle_keepalives_are_closed_and_fds_return_to_baseline(tmp_path):
    """The regression test for the EMFILE incident itself.

    Before the handler timeout existed this test failed: the server held every
    one of these sockets (and one thread each) forever, so no FIN ever arrived
    and the FD count never dropped.
    """
    assert api_server.ApiHandler.timeout == 15

    server = RunningServer(hscc_dir=str(tmp_path))
    socks = []
    try:
        token = api_server.load_token(server.server.ctx.hscc_dir)
        baseline = _fd_count()
        count = 24
        socks = [_open_idle_keepalive(server.host, server.port, token)
                 for _ in range(count)]
        peak = _fd_count()
        # Both sides are in this process: one FD per client socket + one per
        # accepted socket.
        assert peak >= baseline + count, (baseline, peak)

        deadline = time.time() + _TIMEOUT_WAIT_S
        while time.time() < deadline:
            if all(_peer_closed(sock) for sock in socks):
                break
            time.sleep(0.05)
        assert all(_peer_closed(sock) for sock in socks), (
            "the server did not close idle keep-alive connections after "
            f"{api_server.ApiHandler.timeout}s")

        # The server-side FDs are released while the clients are still open.
        after = _fd_count()
        fd_deadline = time.time() + 5
        while time.time() < fd_deadline and after > peak - count:
            time.sleep(0.05)
            after = _fd_count()
        assert after <= peak - count, (peak, after)

        # Closing the clients must now return the whole process to baseline.
        for sock in socks:
            sock.close()
        socks = []
        deadline = time.time() + 5
        while time.time() < deadline and _fd_count() > baseline + 1:
            time.sleep(0.05)
        assert _fd_count() <= baseline + 1, (baseline, _fd_count())
    finally:
        for sock in socks:
            sock.close()
        server.close()


def test_connection_cap_refuses_excess_without_opening_more_sockets(
        tmp_path, monkeypatch):
    """A burst must not be able to consume one FD per client without bound."""
    monkeypatch.setattr(api_server._ApiServer, "max_connections", 2)
    server = RunningServer(hscc_dir=str(tmp_path))
    socks = []
    try:
        token = api_server.load_token(server.server.ctx.hscc_dir)
        socks = [_open_idle_keepalive(server.host, server.port, token)
                 for _ in range(2)]

        refused = socket.create_connection((server.host, server.port), timeout=5)
        try:
            refused.sendall(
                f"GET /v1/ping HTTP/1.1\r\nHost: {server.host}\r\n"
                f"Authorization: Bearer {token}\r\n\r\n".encode("ascii"))
            answer = refused.recv(4096)
        finally:
            refused.close()
        assert answer.startswith(b"HTTP/1.1 503"), answer
        assert b"Connection: close" in answer
    finally:
        for sock in socks:
            sock.close()
        server.close()


# ---------------------------------------------------------------------------
# The kernel's accept queue (t_5d6ea1c4)
# ---------------------------------------------------------------------------
#
# ``max_connections`` above is an *application* limit: it answers an excess
# client with 503 + close. It can only do that for a connection the kernel was
# willing to hold. ``socketserver.TCPServer`` calls ``listen(5)``, so the sixth
# simultaneous connection is reset by XNU before ``process_request`` is ever
# reached — the client gets ECONNRESET instead of the 503 the cap promises, and
# a widget reload burst (menu bar + a few panels) is already six.


def test_accept_queue_is_never_shallower_than_the_connection_cap():
    """The 503 admission path must be reachable before the kernel gives up."""
    assert (api_server._ApiServer.request_queue_size
            >= api_server._ApiServer.max_connections), (
        "the OS accept queue is shallower than max_connections, so excess "
        "connections are reset by the kernel instead of being answered with 503")


def test_unserved_burst_queues_instead_of_being_reset(tmp_path):
    """Connections arriving before anything accepts must survive.

    The accept loop is deliberately never started, so every one of these sockets
    is still sitting in the kernel's queue when it is probed: a socket that
    times out was queued (correct), a socket that was reset is the defect — and
    at ``listen(5)`` the kernel resets them outright, some in ``connect()``
    before they are ever written to. Measured before the fix: 19 of 24.
    """
    burst = 24                     # 4x socketserver's inherited listen(5)
    srv = api_server.create_server(hscc_dir=str(tmp_path),
                                   addr=("127.0.0.1", 0))
    host, port = srv.server_address[:2]
    socks, reset = [], []
    try:
        for _ in range(burst):
            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=5)
                sock.sendall(b"GET /v1/ping HTTP/1.1\r\n"
                             b"Host: 127.0.0.1\r\n\r\n")
                socks.append(sock)
                sock = None
            except (ConnectionResetError, BrokenPipeError) as exc:
                # The kernel reset it instead of queueing it: ECONNRESET in
                # connect(), EPIPE if it died before the request was taken.
                reset.append(exc)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        for sock in socks:
            sock.settimeout(0.2)
            try:
                sock.recv(1)       # nothing accepts: timing out is the pass
            except socket.timeout:
                pass
            except OSError as exc:     # ECONNRESET 54 is the defect reporting
                reset.append(exc)
        assert not reset, f"{len(reset)}/{burst} queued connections were reset"
    finally:
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
        srv.server_close()
