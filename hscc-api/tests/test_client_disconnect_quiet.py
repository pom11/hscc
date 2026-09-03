"""A client hanging up must not print a stack trace.

The operator reported a full ConnectionResetError traceback in the API log.
Nothing was wrong: socketserver prints a traceback for ANY exception escaping
a handler, and a phone backgrounding the app or leaving wifi mid-response
raises exactly that. An alarming trace for a non-event trains the operator to
ignore the log, which is where real faults appear.

These assert the filter is narrow: disconnects go quiet, everything else still
prints.
"""

import io
import sys
import types

import pytest

import api_server


class _Recorder:
    """Stands in for the base class handle_error so we can see if it ran."""

    def __init__(self):
        self.called = False

    def __call__(self, request, client_address):
        self.called = True


def _server_with_stub(monkeypatch, recorder):
    """A bare _ApiServer instance with the parent handle_error stubbed.

    Built without __init__ so no socket is ever opened.
    """
    srv = api_server._ApiServer.__new__(api_server._ApiServer)
    monkeypatch.setattr(api_server.http.server.ThreadingHTTPServer,
                        "handle_error", recorder, raising=False)
    return srv


@pytest.mark.parametrize("exc_cls", [
    BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_disconnect_prints_no_traceback(monkeypatch, capsys, exc_cls):
    rec = _Recorder()
    srv = _server_with_stub(monkeypatch, rec)
    try:
        raise exc_cls("peer went away")
    except exc_cls:
        srv.handle_error(None, ("10.0.0.7", 51234))

    err = capsys.readouterr().err
    assert "Traceback" not in err, "a disconnect must not print a stack trace"
    assert rec.called is False, "must not delegate to socketserver's printer"
    # Still visible, just quiet: one line naming the peer and the condition.
    assert "10.0.0.7" in err
    assert exc_cls.__name__ in err


def test_a_real_error_still_prints(monkeypatch):
    """The filter must be narrow. A genuine bug still reaches the printer."""
    rec = _Recorder()
    srv = _server_with_stub(monkeypatch, rec)
    try:
        raise ValueError("a real bug")
    except ValueError:
        srv.handle_error(None, ("10.0.0.7", 51234))

    assert rec.called is True, (
        "a non-disconnect error was swallowed — this filter must never hide "
        "a real fault")
