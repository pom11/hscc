"""Unit tests for the autodown endpoints (status / enable / disable / wake / cancel).

Hermetic: every backing call is stubbed via monkeypatch on the ``_backing_*``
module functions, so NO test ever writes the operator's live
``~/.hscc/autodown.json``, reads a real kanban DB, or invokes a real
``autoup()``/teardown. Handlers are driven over real loopback HTTP (port 0)
so auth and the router are exercised end-to-end.

Coverage required by the card, per endpoint:
  * reads -> 200 + expected shape + non-empty ``speak``;
  * mutations -> 409 ``confirm_required`` without ``confirm`` AND the backing
    call was NOT made; ``confirm: true`` -> backing called (for wake: returns
    promptly without blocking — the backing autoup runs on a background thread);
  * auth enforced (401 without a token);
  * error envelope shape ({error: {code, message, speak}}).
"""

import http.client
import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import api_server
import routes_autodown
from hscc_daemon.tests import door_sandbox


# --------------------------------------------------------------------------- #
# Hermetic backing fakes
# --------------------------------------------------------------------------- #

@ pytest.fixture
def fakes(monkeypatch):
    state = {
        "saves": [],
        "records": [],
        "activity_source": "api",
        "enabled": False,
        "state": "up",
        "idle_minutes": 10,
        "autoup_calls": 0,
        "clears": [],
    }
    cfg = {
        "enabled": state["enabled"],
        "idle_minutes": state["idle_minutes"],
        "state": state["state"],
        "last_activity_iso": None,
        "down_since": None,
        "wake_source": None,
        "reason": "",
        "cancel_requested": False,
        "force_armed": False,
        "force_armed_overrides": [],
    }
    block = {"blocked": False, "intentional": None, "reason": ""}

    b = {
        "load_config": lambda: dict(cfg),
        "save_config": lambda c: state["saves"].append(c),
        "record_activity": lambda source: (
            state["records"].append(source)
            or cfg.update(last_activity_iso="2026-08-26T00:00:00+00:00")
            or cfg
        ),
        "load_watchdog_block": lambda: dict(block),
        "clear_intentional_block": lambda reason=None: (
            state["clears"].append(reason)),
        "status_snapshot": lambda: {
            "enabled": bool(cfg.get("enabled")),
            "state": cfg.get("state"),
            "state_recorded": cfg.get("state"),
            "state_source": "autodown.json",
            "fleet_up": None,
            "idle_minutes": cfg.get("idle_minutes"),
            "last_activity_iso": cfg.get("last_activity_iso"),
            "down_since": cfg.get("down_since"),
            "wake_source": cfg.get("wake_source"),
            "reason": cfg.get("reason"),
            "watchdog_blocked": bool(block.get("blocked")),
            "watchdog_intentional": block.get("intentional"),
            "kanban_ok": True, "kanban_reason": "", "blocked_by": None,
            "force_armed": bool(cfg.get("force_armed")),
            "force_armed_overrides": cfg.get("force_armed_overrides") or [],
            "active_cron_cpu_only": [], "active_cron_model": [],
            "prci_active": False, "prci_ok": True, "prci_reason": "",
        },
        "list_active_cron_jobs": lambda: [],
        "autoup": lambda: state["autoup_calls"] and None or
            state.__setitem__("autoup_calls", state["autoup_calls"] + 1) or
            {"result": "up"},
    }
    _install(monkeypatch, b)
    return state


def _install(monkeypatch, backing: dict):
    for name, fn in backing.items():
        monkeypatch.setattr(routes_autodown, f"_backing_{name}", fn)


@pytest.fixture
def running(tmp_path, fakes):
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


def _req(running, token, path, body=None, method="GET"):
    conn = http.client.HTTPConnection(running.host, running.port, timeout=8)
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
        raw = json.dumps(body).encode("utf-8")
    else:
        raw = None
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    payload: dict
    try:
        payload = json.loads(data) if data else {}
    except ValueError:
        payload = {"raw": data}
    return resp.status, payload


# --------------------------------------------------------------------------- #
# GET /v1/autodown/status (read-only)
# --------------------------------------------------------------------------- #

def test_status_200_shape(running, token):
    status, payload = _req(running, token, "/v1/autodown/status")
    assert status == 200
    assert payload["enabled"] is False
    assert payload["state"] == "up"
    assert payload["idle_minutes"] == 10
    assert payload["watchdog_blocked"] is False
    assert "blocked_by" in payload
    assert payload["speak"] and isinstance(payload["speak"], str)


def test_status_auth_401(running):
    status, payload = _req(running, None, "/v1/autodown/status")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


# --------------------------------------------------------------------------- #
# POST /v1/autodown/enable (confirm-gated)
# --------------------------------------------------------------------------- #

def test_enable_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/enable",
                           body={"idle_minutes": 10}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["records"] == []
    assert fakes["saves"] == []


def test_enable_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/enable",
                           body={"idle_minutes": 20, "confirm": True},
                           method="POST")
    assert status == 200
    assert payload["enabled"] is True
    assert payload["idle_minutes"] == 20
    assert payload["message"]
    assert fakes["records"] == ["api"]
    # save_config was called with enabled True and idle 20
    assert fakes["saves"] and fakes["saves"][-1]["enabled"] is True
    assert fakes["saves"][-1]["idle_minutes"] == 20


def test_enable_default_idle_minutes(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/enable",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["idle_minutes"] == 10
    assert fakes["saves"][-1]["idle_minutes"] == 10


def test_enable_bad_idle_minutes_400(running, token, fakes):
    for bad in ("abc", -5):
        status, payload = _req(running, token, "/v1/autodown/enable",
                               body={"idle_minutes": bad, "confirm": True},
                               method="POST")
        assert status == 400, f"idle_minutes={bad}"
        assert payload["error"]["code"] == "bad_request"
    assert fakes["saves"] == []


def test_enable_cron_conflict_409(running, token, fakes, monkeypatch):
    """A model-requiring active cron job blocks arming without force."""
    _install(monkeypatch, {"list_active_cron_jobs": lambda: [
        {"name": "hscc-dep-watcher", "no_agent": False, "model": "x"},
    ]})
    status, payload = _req(running, token, "/v1/autodown/enable",
                           body={"confirm": True}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "cron_conflict"
    assert fakes["saves"] == []  # backing NOT called


def test_enable_force_overrides_cron(running, token, fakes, monkeypatch):
    _install(monkeypatch, {"list_active_cron_jobs": lambda: [
        {"name": "hscc-dep-watcher", "no_agent": False, "model": "x"},
    ]})
    status, payload = _req(running, token, "/v1/autodown/enable",
                           body={"confirm": True, "force": True}, method="POST")
    assert status == 200
    assert payload["enabled"] is True
    assert payload["force_armed"] is True
    assert payload["force_armed_overrides"] == ["hscc-dep-watcher"]


def test_enable_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/autodown/enable",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["saves"] == []


# --------------------------------------------------------------------------- #
# POST /v1/autodown/disable (confirm-gated)
# --------------------------------------------------------------------------- #

def test_disable_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/disable",
                           body={}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["saves"] == []
    assert fakes["clears"] == []


def test_disable_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/disable",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["enabled"] is False
    assert payload["message"]
    assert fakes["clears"]  # intentional block cleared
    assert fakes["saves"][-1]["enabled"] is False


def test_disable_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/autodown/disable",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["clears"] == []


# --------------------------------------------------------------------------- #
# POST /v1/autodown/wake (confirm-gated, returns promptly)
# --------------------------------------------------------------------------- #

def test_wake_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/wake",
                           body={}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["autoup_calls"] == 0
    assert fakes["records"] == []


def test_wake_confirm_true_returns_promptly(running, token, fakes):
    """Wake must NOT block ~9 min holding the connection — it returns promptly
    with a 'waking' status; the backing autoup runs on a background thread."""
    start = time.time()
    status, payload = _req(running, token, "/v1/autodown/wake",
                           body={"confirm": True}, method="POST")
    elapsed = time.time() - start
    assert status == 200
    assert payload["state"] == "waking"
    assert payload["result"] == "waking"
    assert payload["speak"]
    assert "wake" in payload["message"].lower()
    # Must return in seconds, not minutes.
    assert elapsed < 5, f"wake blocked too long: {elapsed:.1f}s"
    # The backing autoup is dispatched on a background thread — give it a beat
    # to run (the fake is instant) then confirm it was invoked.
    time.sleep(0.2)
    assert fakes["autoup_calls"] == 1
    assert fakes["records"] == ["api"]


def test_wake_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/autodown/wake",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["autoup_calls"] == 0


# --------------------------------------------------------------------------- #
# POST /v1/autodown/cancel (confirm-gated)
# --------------------------------------------------------------------------- #

def test_cancel_missing_confirm_409_no_backing(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/cancel",
                           body={}, method="POST")
    assert status == 409
    assert payload["error"]["code"] == "confirm_required"
    assert fakes["saves"] == []


def test_cancel_confirm_true_backs(running, token, fakes):
    status, payload = _req(running, token, "/v1/autodown/cancel",
                           body={"confirm": True}, method="POST")
    assert status == 200
    assert payload["cancel_requested"] is True
    assert fakes["saves"][-1]["cancel_requested"] is True


def test_cancel_auth_401(running, fakes):
    status, payload = _req(running, None, "/v1/autodown/cancel",
                           body={"confirm": True}, method="POST")
    assert status == 401
    assert fakes["saves"] == []


# --------------------------------------------------------------------------- #
# Mutating paths are POST-only: GET -> 405, never the handler
# --------------------------------------------------------------------------- #

def test_autodown_mutations_not_reachable_via_get(running, token, fakes):
    for path in ("/v1/autodown/enable", "/v1/autodown/disable",
                 "/v1/autodown/wake", "/v1/autodown/cancel"):
        status, payload = _req(running, token, path, method="GET")
        assert status == 405, f"{path}: got {status}"
        assert payload["error"]["code"] == "method_not_allowed"
    assert fakes["saves"] == []
    assert fakes["autoup_calls"] == 0


# --------------------------------------------------------------------------- #
# The route is a documented READ: it creates no $HSCC_HOME/daemon.log (t_85875291)
# --------------------------------------------------------------------------- #

def _child_verdict(proc, mode: str) -> dict:
    """The one JSON document the read harness prints — or a failure that quotes it.

    Unparseable output is the harness's failure, not the route's: quoting stdout
    and stderr is the only way a red run says which of the two broke.
    """
    lines = proc.stdout.strip().splitlines()
    try:
        data = json.loads(lines[-1]) if lines else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise AssertionError(
            f"child ({mode}) printed no verdict JSON (rc={proc.returncode})\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}")
    return data


class TestTheRouteWritesNoDaemonLog:
    """``GET /v1/autodown/status`` writes no daemon log — measured, not asserted from docs.

    The route shares the CLI door's payload builder, and that builder reports an
    unreachable Hermes library through a WARN that ``daemon_ops.log`` appends to
    ``$HSCC_HOME/daemon.log`` — the DAEMON'S rotating log, written by the API
    process. Verdict + evidence:
    ``docs/audits/verdict-api-autodown-status-read_t_85875291.md``; the fix is the
    one-borrowed-call mute in ``routes_autodown._backing_status_snapshot``
    (``autodown.silent_read()``). It is guarded HERE, in the surface's own suite,
    because the CLI door's guards in ``hscc_daemon`` cannot see this route — it
    reaches the builder directly (the #704 lesson).

    Deliberately NOT hermetic like the rest of this file: monkeypatching
    ``_backing_status_snapshot`` would stub away the very call that writes, so the
    guard runs a real ``api_server`` + a real GET in a child process inside
    ``door_sandbox``'s sealed HOME (that module builds the env and holds no
    assert, by design). Three things stop it passing vacuously:

      * the response must be ``200`` AND name the **sandbox** path in
        ``kanban_reason`` — the leaf really ran, and resolved ``~`` to the sandbox;
      * ``activity.json`` must appear — the request really was authenticated and
        routed, so the handler really ran (a 401 would skip the handler and
        "pass"). That stamp is the route's DESIGNED write
        (``api_server._do_stamp_http_activity``): what this guard forbids is
        ``daemon.log``, not every byte the surface is meant to produce;
      * the control below runs the same builder **un-muted** in the same sealed
        environment and must SEE the WARN — so silence means "muted", not
        "nothing there to write".
    """

    _CHILD = Path(__file__).resolve().parent / "_autodown_status_child.py"

    def _run(self, tmp_path, monkeypatch, mode):
        """One child run in a fresh sealed HOME; returns (seal, its verdict dict)."""
        seal = door_sandbox.seal(tmp_path, monkeypatch)
        door_sandbox.the_seal_actually_took()  # only meaningful AFTER the seal
        proc = subprocess.run(
            [sys.executable, str(self._CHILD), mode, str(seal.hscc)],
            env=seal.env, cwd=str(seal.cwd), capture_output=True, text=True,
            timeout=240)
        data = _child_verdict(proc, mode)
        assert "error" not in data, (
            f"child ({mode}) errored: {data['error']}\n{data.get('traceback', '')}")
        door_sandbox.the_seal_actually_took()
        return seal, data

    def test_the_route_writes_no_daemon_log(self, tmp_path, monkeypatch):
        seal, data = self._run(tmp_path, monkeypatch, "route")
        assert data["status"] == 200, f"the route did not serve: {data['payload']}"
        payload = data["payload"]
        assert payload["kanban_ok"] is False, (
            "the leaf must have been reached — the sandbox has no Hermes library: "
            f"{payload}")
        assert str(seal.home) in payload["kanban_reason"], (
            "the payload does not name the SANDBOX path, so ~ resolved somewhere "
            f"else and the silence would prove nothing: {payload['kanban_reason']}")
        assert not data["daemon_log_exists"], (
            "GET /v1/autodown/status created the daemon's log — a read route "
            f"appended to another process's rotating log:\n{data['daemon_log']}")
        assert "daemon.log" not in data["new_paths"], data["new_paths"]
        assert "daemon.log" not in data["changed_paths"], data["changed_paths"]
        assert "activity.json" in data["new_paths"], (
            "the request was never authenticated+routed, so the handler never ran "
            f"and had nothing to write: {data['new_paths']}")

    def test_the_leaf_still_writes_when_the_read_is_not_muted(
            self, tmp_path, monkeypatch):
        """The control: same sealed sandbox, same builder, no mute -> the WARN lands."""
        seal, data = self._run(tmp_path, monkeypatch, "unmuted")
        assert str(seal.home) in data["kanban_reason"], data
        assert "daemon.log" in data["new_paths"], (
            "CONTROL IS VACUOUS: the un-muted builder wrote nothing in this "
            "sandbox either, so the route's silence above proves nothing about "
            f"the mute. new_paths={data['new_paths']}")
        assert "interlock unevaluable" in data["daemon_log"], data["daemon_log"]
