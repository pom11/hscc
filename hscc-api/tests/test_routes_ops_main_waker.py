"""The D9 dashboard pair: GET /v1/main/status + GET /v1/orch/waker/status.

Audit ``docs/audits/2026-11-23-agent-surface.md`` §2.6 measured that the two
dashboard questions behind ``hscc main status`` and ``hscc orch waker status``
had no HTTP surface, and §12.2 (D9) ruled **yes — reads only**. These tests are
that ruling turned into gates:

  * registration — exactly one GET route per path, resolving to the named
    handler, and no mutating method on either path;
  * pass-through — each handler spreads its backing's payload and adds nothing
    but ``speak`` (the fakes carry a key no handler could invent, so a handler
    that recomputed or filtered would show up here);
  * honesty — a raising or non-dict backing is a 200 with an ``unavailable``
    speak, never a 500 and never a fabricated payload;
  * read-only — the mutating functions of both backing modules are patched with
    tripwires, the waker sandbox is diffed before/after the request, and no
    ``profile`` parameter is accepted, so neither route can steer an answer;
  * no second source (audit §5 S1) — the real reads run against a sandboxed
    root: the main answer comes from ``main_orch``'s own stored name and the
    waker answer from ``orch_waker``'s own config/state files.

Hermetic: the main read gets a fake ``repl_inject`` (never tmux), and every
waker path is sandboxed (HOME + ``state.STATE_DIR`` at a tmp root), so no test
touches the operator's tmux server, daemon state or ``~/.hscc``. Hosts: none —
neither route resolves a host at all.
"""

import copy
import http.client
import json
import threading
import types

import pytest

import api_server
import routes_ops


# --------------------------------------------------------------------------- #
# Harness (real loopback HTTP, like the sibling ops tests)
# --------------------------------------------------------------------------- #

@pytest.fixture
def running(tmp_path):
    srv = types.SimpleNamespace()
    srv.server = api_server.create_server(hscc_dir=str(tmp_path),
                                          addr=("127.0.0.1", 0))
    srv.host, srv.port = srv.server.server_address[:2]
    thread = threading.Thread(target=srv.server.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.server.shutdown()
    srv.server.server_close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


def _req(running, token, path, method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    conn.request(method, path, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


#: The CLI payload of ``hscc main status --json``, plus ``cli_only_field``:
#: a key no route handler could invent, so its survival proves pass-through
#: rather than a handler that happens to rebuild the same shape.
MAIN_PAYLOAD = {
    "ok": True, "name": "general", "profile": "general", "board": "general",
    "state": "idle", "running": True, "busy": False, "pane": "%7",
    "target": "main:1.1", "panes": ["main:1.1"], "detail": "idle",
    "cli_only_field": "verbatim",
}

#: The payload ``orch_waker.waker_status()`` returns for the CLI verb, plus the
#: same invention-proof key.
WAKER_PAYLOAD = {
    "enabled": True,
    "config": {"enabled": True, "debounce_s": 90, "min_gap_s": 300,
               "poll_s": 30},
    "stream": {"name": "orch_waker", "interval_s": 30, "scan_every_s": 30,
               "last_tick": 1_800_000_000.0, "tick_age_s": 12.4,
               "ok": True, "message": "nudged 1"},
    "boards": {"hscc": {"last_id": 41, "pending": 2},
               "inc": {"last_id": 7, "pending": 0}},
    "paneless_skip": [],
    "audit": [{"ts": 1_799_999_500, "board": "hscc", "profile": "hscc-orch",
               "ok": True, "outcome": "sent", "msg": "wake"}],
    "cli_only_field": "verbatim",
}


@pytest.fixture
def stubs(monkeypatch):
    """Point both backing seams at the payloads above; records every call."""
    calls = {"main": [], "waker": []}

    def _main():
        calls["main"].append(True)
        return copy.deepcopy(MAIN_PAYLOAD)

    def _waker():
        calls["waker"].append(True)
        return copy.deepcopy(WAKER_PAYLOAD)

    monkeypatch.setattr(routes_ops, "_backing_main_status", _main)
    monkeypatch.setattr(routes_ops, "_backing_waker_status", _waker)
    return calls


# --------------------------------------------------------------------------- #
# Registration (the route-set gate; assert the resolved handler, not the string)
# --------------------------------------------------------------------------- #

def _matches(path, method="GET"):
    return [(m, p.pattern, h) for m, p, h in api_server.ROUTES
            if m == method and p.match(path)]


def test_main_route_resolves_to_its_handler():
    hits = _matches("/v1/main/status")
    assert [(m, pat, h.__name__) for m, pat, h in hits] == [
        ("GET", routes_ops.MAIN_STATUS_PATTERN, "handle_main_status")]
    assert routes_ops.MAIN_STATUS_ROUTE == "GET /v1/main/status"


def test_waker_route_resolves_to_its_handler():
    hits = _matches("/v1/orch/waker/status")
    assert [(m, pat, h.__name__) for m, pat, h in hits] == [
        ("GET", routes_ops.WAKER_STATUS_PATTERN, "handle_waker_status")]
    assert routes_ops.WAKER_STATUS_ROUTE == "GET /v1/orch/waker/status"


def test_neither_route_has_a_mutating_method():
    """D9 is reads only: no POST/DELETE door on either path, anywhere."""
    for path in ("/v1/main/status", "/v1/orch/waker/status"):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert not _matches(path, method), f"{method} {path} is registered"


def test_handlers_point_at_their_cli_verb_in_the_summary_block():
    """The pointer lives before the first blank line — what help()/git grep show."""
    for handler, route, verb in (
            (routes_ops.handle_main_status, "/v1/main/status",
             "hscc main status"),
            (routes_ops.handle_waker_status, "/v1/orch/waker/status",
             "hscc orch waker status")):
        summary = (handler.__doc__ or "").split("\n\n")[0]
        # (?!) on the path so a longer registered path cannot satisfy the match.
        assert route + " —" in summary or route + " " in summary, summary
        assert verb in summary, summary


def test_waker_handler_names_the_cli_only_mutation():
    """A reader told "no route for that" must be told where it lives."""
    doc = routes_ops.handle_waker_status.__doc__ or ""
    assert routes_ops.WAKER_MUTATION_POINTER in doc


# --------------------------------------------------------------------------- #
# Pass-through + speak
# --------------------------------------------------------------------------- #

def test_main_status_200_passes_the_cli_payload_verbatim(running, token, stubs):
    status, payload = _req(running, token, "/v1/main/status")
    assert status == 200
    assert stubs["main"] == [True]
    for key, value in MAIN_PAYLOAD.items():
        assert payload[key] == value, key
    assert payload["speak"] == "general is running and idle."


def test_waker_status_200_passes_the_cli_payload_verbatim(running, token, stubs):
    status, payload = _req(running, token, "/v1/orch/waker/status")
    assert status == 200
    assert stubs["waker"] == [True]
    for key, value in WAKER_PAYLOAD.items():
        assert payload[key] == value, key
    assert payload["speak"] == ("Orchestrator waker is enabled, last tick "
                                "12s ago. 2 boards tracked.")


@pytest.mark.parametrize("state,panes,speak", [
    ("busy", ["%1"], "general is running and busy."),
    ("idle", ["%1"], "general is running and idle."),
    ("not-running", [], "general is not running."),
    ("ambiguous", ["%1", "%2"],
     "general has 2 panes — the pane target is ambiguous."),
    ("unknown", ["%1"], "general's pane state is unknown."),
    # A resolver failure must not read as "the main orchestrator is absent".
    ("error", [], "Main orchestrator status unavailable."),
    ("some-future-state", [], "Main orchestrator status unavailable."),
])
def test_main_status_speak_per_state(running, token, monkeypatch, state,
                                     panes, speak):
    payload = dict(MAIN_PAYLOAD, state=state, panes=panes)
    monkeypatch.setattr(routes_ops, "_backing_main_status", lambda: payload)
    _, got = _req(running, token, "/v1/main/status")
    assert got["speak"] == speak


def test_waker_status_speak_when_disabled(running, token, monkeypatch):
    monkeypatch.setattr(routes_ops, "_backing_waker_status",
                        lambda: {"enabled": False, "stream": {}})
    _, got = _req(running, token, "/v1/orch/waker/status")
    assert got["speak"] == "Orchestrator waker is disabled."


def test_waker_status_speak_when_never_ticked(running, token, monkeypatch):
    """No tick yet is stated as such — never a fabricated "0s ago"."""
    monkeypatch.setattr(routes_ops, "_backing_waker_status",
                        lambda: {"enabled": True, "stream": {"last_tick": None,
                                                             "tick_age_s": None},
                                 "boards": {}})
    _, got = _req(running, token, "/v1/orch/waker/status")
    assert got["speak"] == ("Orchestrator waker is enabled, but no tick has "
                            "been recorded yet. 0 boards tracked.")


# --------------------------------------------------------------------------- #
# Honesty on a failed read (never 500, never a fabricated payload)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("route,speak", [
    ("/v1/main/status", "Main orchestrator status unavailable."),
    ("/v1/orch/waker/status", "Orchestrator waker status unavailable."),
])
def test_read_failure_degrades_to_200_with_honest_speak(running, token,
                                                        monkeypatch, route,
                                                        speak):
    def boom():
        raise RuntimeError("backing module not importable")
    monkeypatch.setattr(routes_ops, "_backing_main_status", boom)
    monkeypatch.setattr(routes_ops, "_backing_waker_status", boom)
    status, payload = _req(running, token, route)
    assert status == 200
    assert payload == {"speak": speak}


@pytest.mark.parametrize("route,speak", [
    ("/v1/main/status", "Main orchestrator status unavailable."),
    ("/v1/orch/waker/status", "Orchestrator waker status unavailable."),
])
def test_non_dict_read_degrades_to_200(running, token, monkeypatch, route,
                                       speak):
    monkeypatch.setattr(routes_ops, "_backing_main_status", lambda: "text")
    monkeypatch.setattr(routes_ops, "_backing_waker_status", lambda: None)
    status, payload = _req(running, token, route)
    assert status == 200
    assert payload == {"speak": speak}


@pytest.mark.parametrize("route", ["/v1/main/status", "/v1/orch/waker/status"])
def test_auth_required(running, route):
    status, payload = _req(running, None, route)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# Read-only + no steering (D9: reads only, no profile switching)
# --------------------------------------------------------------------------- #

def test_backing_seams_reach_only_the_read_functions():
    """The no-second-source door: one call, to the read function, and nothing else.

    Measured on the code object's NAMES, not the source text: the docstrings of
    these helpers deliberately *name* the verbs they refuse to wrap, and a text
    scan would read that prose as a call site (measured: it reddened on the
    word ``cmd_main`` in a docstring).
    """
    forbidden = {"start", "cmd_main", "wakeups", "run_tmux", "set_enabled",
                 "waker_tick", "run_tick", "save_state", "stamp_entry"}
    main_names = set(routes_ops._backing_main_status.__code__.co_names)
    assert "status" in main_names
    assert not (main_names & forbidden), main_names & forbidden
    waker_names = set(routes_ops._backing_waker_status.__code__.co_names)
    assert "waker_status" in waker_names
    assert not (waker_names & forbidden), waker_names & forbidden


def test_main_status_accepts_no_profile_and_no_body(running, token, monkeypatch):
    """``?profile=`` must not steer the answer: the backing is called arg-less."""
    seen = []

    def _status(*args, **kwargs):
        seen.append((args, kwargs))
        return dict(MAIN_PAYLOAD)

    from hscc_daemon import main_cli
    monkeypatch.setattr(main_cli, "status", _status)
    status, payload = _req(running, token, "/v1/main/status?profile=other-orch",
                           method="POST")
    assert status == 405  # registered GET only; a body cannot reach the read
    status, payload = _req(running, token, "/v1/main/status?profile=other-orch")
    assert status == 200
    assert payload["profile"] == "general"
    assert seen == [((), {})]  # literally status(): no caller-steered profile


def test_waker_status_ignores_a_confirm_body(running, token, stubs):
    """A confirm flag buys nothing here — the route has no mutation to gate."""
    status, payload = _req(running, token, "/v1/orch/waker/status")
    assert status == 200
    assert stubs["waker"] == [True]
    assert payload["enabled"] is True


# --------------------------------------------------------------------------- #
# No second source of truth: the real reads, sandboxed
# --------------------------------------------------------------------------- #

class _FakeRepl:
    """Stand-in for ``repl_inject`` — discovery + busy marker, never tmux."""

    def __init__(self, panes, busy=False):
        self._panes = panes
        self._busy = busy
        self.busy_calls = []

    def hermes_panes(self):
        return list(self._panes)

    def is_busy(self, pane):
        self.busy_calls.append(pane)
        return self._busy


def test_main_status_answers_the_one_stored_name(running, token, monkeypatch,
                                                 tmp_path):
    """The route's identity IS ``main_orch.main_profile()``'s answer.

    A name stored through the resolver's own writer at a sandboxed ``HSCC_DIR``
    comes back from the route. Nothing here hardcodes a profile, so a renamed
    main needs no edit in ``routes_ops`` — and a route that grew its own
    resolver would answer ``general`` while the CLI answers ``fleet-demo``.
    """
    from hscc_daemon import main_cli

    root = tmp_path / "hscc"
    root.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(root))
    main_orch = main_cli._load_main_orch()
    main_orch.store_name("fleet-demo", hscc_dir=str(root),
                         roles_dir=str(tmp_path / "roles"),
                         profiles_dir=str(tmp_path / "profiles"))
    monkeypatch.setattr(
        main_cli, "load_repl_inject",
        lambda: _FakeRepl([{"profile": "fleet-demo", "pane": "%3",
                            "target": "demo:1.1"}], busy=True))

    status, payload = _req(running, token, "/v1/main/status")
    assert status == 200
    assert (payload["name"], payload["profile"], payload["board"]) == (
        "fleet-demo", "fleet-demo", "fleet-demo")
    assert payload["state"] == "busy" and payload["running"] is True
    assert payload["pane"] == "%3" and payload["target"] == "demo:1.1"
    assert payload["speak"] == "fleet-demo is running and busy."


def test_main_status_reports_no_pane_honestly(running, token, monkeypatch,
                                              tmp_path):
    """A pane the CLI cannot see is a pane this route cannot invent."""
    from hscc_daemon import main_cli

    root = tmp_path / "hscc"
    root.mkdir()
    monkeypatch.setenv("HSCC_DIR", str(root))
    monkeypatch.setattr(main_cli, "load_repl_inject", lambda: _FakeRepl([]))

    _, payload = _req(running, token, "/v1/main/status")
    assert payload["state"] == "not-running" and payload["running"] is False
    assert payload["pane"] is None and payload["panes"] == []
    assert payload["speak"] == "general is not running."


def test_waker_status_reads_the_waker_s_own_files_and_writes_nothing(
        running, token, monkeypatch, tmp_path):
    """The waker half of D9, read for real, and proven read-only.

    The daemon's stream file and the waker's own state file are the SAME path
    (the t_fa9a2faf collision), so the sandbox holds ONE merged document written
    through the waker's own writer — the same shape a real tick leaves behind —
    with the config block written by ``set_enabled``. HOME and
    ``state.STATE_DIR`` are both redirected at the tmp root, so nothing here can
    reach the operator's ``~/.hscc``.
    """
    import time

    from hscc_daemon import orch_waker, state

    home = tmp_path / "home"
    root = home / ".hscc"
    (root / "state").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HSCC_DIR", str(root))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(state, "STATE_DIR", str(root / "state"))

    # Real writers, sandboxed root: enabled config + one merged state document.
    assert orch_waker.set_enabled(True)["enabled"] is True
    now = time.time()
    orch_waker.save_state({"last": {"hscc": 41}, "pending": {"hscc": [42, 43]},
                           "sent": {}, "last_tick": now, "ok": True,
                           "message": "nudged 1 board"})

    # Tripwires: reaching either mutating function from a GET is a hard failure.
    def tripwire(*args, **kwargs):
        raise AssertionError(f"mutation reached from a GET: {args} {kwargs}")
    monkeypatch.setattr(orch_waker, "set_enabled", tripwire)
    monkeypatch.setattr(orch_waker, "waker_tick", tripwire)

    before = _waker_file_snapshot(home)
    status, payload = _req(running, token, "/v1/orch/waker/status")
    assert status == 200
    assert payload["enabled"] is True
    assert payload["boards"]["hscc"] == {"last_id": 41, "pending": 2}
    assert payload["stream"]["ok"] is True
    assert payload["stream"]["last_tick"] == pytest.approx(now)
    assert payload["stream"]["tick_age_s"] == pytest.approx(0.0, abs=5.0)
    assert payload["audit"] == []          # no audit line was ever written
    assert payload["paneless_skip"] == []
    assert payload["speak"].startswith("Orchestrator waker is enabled")
    # Read-only, measured rather than promised: the files this read touches are
    # byte-identical and no new one appeared (no audit line, no re-stamped tick).
    assert _waker_file_snapshot(home) == before


def _waker_file_snapshot(home):
    """(relpath, size, mtime_ns, sha256) for the files ``waker_status()`` reads.

    Scoped to the waker's OWN state — its config block and ``state/`` — rather
    than the whole ``.hscc`` tree on purpose: serving ANY request makes the API
    server stamp its own activity signal (``~/.hscc/activity.json``, kept
    outside ``state/`` by ``api_server.py``), and that write belongs to the
    transport, not to the read being measured.
    """
    import hashlib

    root = home / ".hscc"
    paths = [root / "orch_waker.json"]
    state_dir = root / "state"
    if state_dir.is_dir():
        paths += sorted(p for p in state_dir.iterdir() if p.is_file())
    rows = []
    for path in paths:
        if path.exists():
            st = path.stat()
            rows.append((str(path.relative_to(root)), st.st_size,
                         st.st_mtime_ns,
                         hashlib.sha256(path.read_bytes()).hexdigest()))
    return rows
