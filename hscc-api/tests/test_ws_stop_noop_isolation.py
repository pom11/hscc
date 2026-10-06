"""Guard: a relay straggler from an EARLIER turn must never look "in flight".

Regression test for the collection-order flake in
``test_ws_relay_not_noop.py::test_stop_kind_with_nothing_in_flight_is_noop``
(t_163fa09f). The flake was not caused by the stop path at all: a previous
test's relay worker thread registered a live ``project="hscc"`` job in the
process-global store AFTER the isolation fixture had cleared it, so
``_in_flight_job("hscc")`` found something to stop and the ``stop`` frame acked
``stopped: True``. It reproduced only in full-directory runs and (by scheduling
luck) on one interpreter, because it is a race between an unjoined thread's
``_new_job`` and the fixture's clear.

Ordering alone does not reproduce it — the captured leak needed the write to
land a few ms after the clear — so these tests DELIBERATELY DELAY the straggler's
``_new_job`` (that is what makes it deterministic) and then assert the outcome.

What each test pins:

  * ``test_retired_worker_cannot_register_a_job`` — the epoch guard: a worker
    born before the boundary is rejected by ``_new_job``.
  * ``test_boundary_drain_joins_a_slow_relay_worker`` — the drain quiesces a
    worker that is still blocked in its resolve phase, BEFORE the store is
    cleared (the ordering that makes clearing safe at all).
  * ``test_polluter_then_stop_noop_pair_is_isolated`` — the polluting PAIR in the
    polluting ORDER: a straggler whose write lands after the boundary must not
    be visible to the stop-no-op assertion.
"""

import threading
import time
import types

import pytest

import routes_orchestrator as ro
import routes_ws
from session_event import TYPE_MESSAGE, get_store, reset_stores


@pytest.fixture(autouse=True)
def clean_stores():
    reset_stores()
    yield
    reset_stores()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class _NoopProc:
    def poll(self):
        return None

    def wait(self, timeout=None):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass


def _install_backing(monkeypatch, invoke, resolve_gate=None):
    """Real job machinery, faked process seams — same shape as the relay tests.

    ``resolve_gate`` blocks the worker INSIDE ``_backing_resolve``, i.e. before
    ``_new_job``. That is the leak's timing: the worker is alive but has written
    nothing yet, so a clear that runs now does not see (or stop) it.
    """
    import sys
    monkeypatch.setitem(sys.modules, "routes_orchestrator", ro)

    def _resolve(project, path):
        if resolve_gate is not None:
            assert resolve_gate.wait(timeout=10), "resolve gate never released"
        return {"profile": "orch", "session": "hscc"}

    def _invoke(profile, session, text, timeout=None, image_data=None,
                image_mime=None, cancel_evt=None, on_spawn=None):
        if on_spawn is not None:
            on_spawn(_NoopProc())
        return invoke(profile, session, text)

    monkeypatch.setattr(ro, "_registry_path", lambda ctx: "/dev/null")
    monkeypatch.setattr(ro, "_backing_resolve", _resolve)
    monkeypatch.setattr(ro, "_backing_invoke", _invoke)
    monkeypatch.setattr(ro, "detect_active_cli_owner",
                        lambda project, registry_path=None: None)
    monkeypatch.setattr(routes_ws, "_registry",
                        types.SimpleNamespace(
                            ensure_session=lambda name, path=None: name))
    monkeypatch.setattr(routes_ws, "_registry_path", lambda ctx: "/dev/null")
    return ro


def _run_boundary(timeout=2.0):
    """Perform one test-boundary the way conftest._isolate_chat_jobs does.

    Drain (signal + join) FIRST, then clear the store, then retire the epoch and
    re-stamp the calling thread with it — the order that makes the clear
    meaningful. Repeating the harness here (rather than relying on collection
    order) is what makes the regression deterministic.
    """
    stragglers = routes_ws.drain_workers(timeout=timeout)
    with ro._jobs_lock:
        ro._jobs.clear()
    reset_stores()
    gen = ro.advance_job_generation()
    setattr(threading.current_thread(), "_hscc_job_generation", gen)
    return stragglers


def _stop_ack(monkeypatch, project="hscc"):
    """Send a ``{"kind":"stop"}`` frame and return the parsed stop ack."""
    import json
    import ws_frame
    acks = []
    monkeypatch.setattr(routes_ws, "_send_text",
                        lambda s, t: acks.append(t), raising=True)
    sock = types.SimpleNamespace(sendall=lambda b: None)
    routes_ws._process_inbound(sock, project, ws_frame.OP_TEXT,
                               b'{"kind":"stop"}')
    for raw in acks:
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if obj.get("type") == "ack" and obj.get("payload", {}).get("kind") == "stop":
            return obj
    return None


# --------------------------------------------------------------------------- #
# 1. The epoch guard: a retired worker cannot register a job
# --------------------------------------------------------------------------- #

def test_retired_worker_cannot_register_a_job():
    """A worker born in a retired generation is rejected at ``_new_job``.

    This is the backstop for a drain whose join timed out: even if the thread is
    still running, its write is refused, so it cannot leave a phantom live job.
    """
    live_gen = ro._job_generation
    worker_gen = live_gen  # what a worker inherits while this generation is live

    def _body():
        # Born in the live generation, then the epoch is retired underneath it.
        ro.advance_job_generation()
        with pytest.raises(ro._StaleJobGeneration):
            ro._new_job("hscc", "orch", "hscc", "straggler prompt")

    t = ro.spawn_tracked_worker(_body, "ws-relay-guardtest")
    t.join(timeout=5)
    assert not t.is_alive(), "guard worker did not finish"
    assert worker_gen == live_gen

    # The rejected write left NOTHING in the store — that is the whole point.
    with ro._jobs_lock:
        stale = [j for j in ro._jobs.values() if j.project == "hscc"
                 and j.prompt == "straggler prompt"]
    assert not stale, "a retired worker registered a job: %r" % (stale,)

    # An UNTRACKED thread is never affected, whatever the generation counter is.
    # This is the PRODUCTION shape: POST /v1/orchestrator/chat registers its job
    # from an HTTP handler thread, which is not a tracked turn worker, so the
    # guard can never reject a real operator request.
    submitted = {}

    def _submit():
        submitted["job"] = ro._new_job("hscc", "orch", "hscc",
                                       "untracked submission")

    handler = threading.Thread(target=_submit)   # plain thread: no generation
    handler.start()
    handler.join(timeout=5)
    job = submitted.get("job")
    assert job is not None, "untracked thread's job was rejected by the guard"
    with ro._jobs_lock:
        assert ro._jobs.get(job.job_id) is job
        ro._jobs.pop(job.job_id, None)


# --------------------------------------------------------------------------- #
# 2. The drain quiesces a worker BEFORE the clear
# --------------------------------------------------------------------------- #

def test_boundary_drain_joins_a_slow_relay_worker(monkeypatch):
    """Drain must make a worker blocked in its resolve phase EXIT, and it must
    happen before the store is cleared.

    The captured leak was exactly this shape: worker alive, nothing written yet,
    clear ran anyway, write landed afterwards. A clear cannot compete with that,
    so the boundary has to join the writer first.
    """
    release = threading.Event()

    def invoke(profile, session, text):
        return ("should never run", profile, session)

    _install_backing(monkeypatch, invoke, resolve_gate=release)

    assert routes_ws._default_relay("hscc", "slow resolve") is True
    # The worker is alive and has registered NOTHING yet (blocked in resolve).
    deadline = time.time() + 2
    while time.time() < deadline and not ro.live_workers():
        time.sleep(0.005)
    assert ro.live_workers(), "relay worker was never tracked"
    with ro._jobs_lock:
        assert not ro._jobs, "nothing should be registered while still resolving"

    # Release it and IMMEDIATELY run the boundary: the write would land during or
    # after the clear if the boundary did not join.
    release.set()
    stragglers = _run_boundary()
    assert stragglers == [], "drain left stragglers: %r" % (stragglers,)
    assert not ro.live_workers(), "worker outlived the boundary drain"

    # Whatever it was going to write is gone: the store is empty and clean.
    with ro._jobs_lock:
        assert not ro._jobs, "leaked job survived the drain+clear boundary: %r" % (
            list(ro._jobs.values()),)


# --------------------------------------------------------------------------- #
# 3. The polluting PAIR, in the polluting ORDER
# --------------------------------------------------------------------------- #

def test_polluter_then_stop_noop_pair_is_isolated(monkeypatch):
    """Polluter -> boundary -> stop-no-op assertion, with the straggler's write
    forced to land AFTER the boundary.

    This is the flake's exact shape made deterministic: the polluter's relay
    worker is still resolving when the boundary runs, and its ``_new_job`` can
    only happen afterwards (the gate is held until the timer fires). Without
    isolation the stop frame would then ack ``stopped: True`` — the failure the
    reviewers saw on full-suite runs.
    """
    release = threading.Event()

    def invoke(profile, session, text):
        return ("late reply", profile, session)

    _install_backing(monkeypatch, invoke, resolve_gate=release)

    # ---- polluter: spawns a relay whose job registration is deliberately late
    assert routes_ws._default_relay("hscc", "polluting turn") is True
    deadline = time.time() + 2
    while time.time() < deadline and not ro.live_workers():
        time.sleep(0.005)
    assert ro.live_workers(), "polluter did not leave a live relay worker"

    # ---- boundary (what conftest does between tests), gate STILL held
    _run_boundary()

    # ---- target: the stop-no-op assertion runs while the straggler is blocked
    ack = _stop_ack(monkeypatch, "hscc")
    assert ack is not None, "no stop ack was sent"
    assert ack["payload"]["stopped"] is False, (
        "a straggler from a previous turn was visible as an in-flight job: %r"
        % (ack,))

    # ---- now let the straggler through: its write must be REFUSED, and the
    # stop-no-op assertion must still hold.
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline and ro.live_workers():
        time.sleep(0.01)
    assert not ro.live_workers(), "straggler never exited"

    with ro._jobs_lock:
        live = [j for j in ro._jobs.values()
                if j.project == "hscc" and j.finished_at is None]
    assert not live, "straggler registered a live job after the boundary: %r" % (live,)

    ack2 = _stop_ack(monkeypatch, "hscc")
    assert ack2 is not None and ack2["payload"]["stopped"] is False, (
        "stop no-op broke after the straggler ran: %r" % (ack2,))

    # The straggler must not have written into the transcript either: a
    # "relay_failed"/reply event appended to a store the NEXT test owns is the
    # same class of leak with a different victim.
    events = get_store("hscc").history()["events"]
    assert not any(e["type"] == TYPE_MESSAGE
                   and (e["payload"].get("delta") or "") == "late reply"
                   for e in events), (
        "straggler wrote into the transcript: %r" % (events,))


def test_tracked_worker_registry_does_not_leak_threads():
    """Finished workers deregister, so the registry cannot grow without bound.

    The drain is only cheap if ``_workers`` holds live threads; a registry that
    accumulated every turn's thread would turn each boundary into a full sweep
    and eventually make the drain the thing that slows the suite.
    """
    done = threading.Event()
    t = ro.spawn_tracked_worker(lambda: done.set(), "ws-relay-registrytest")
    t.join(timeout=5)
    assert done.is_set()
    names = [w.name for w in ro.live_workers()]
    assert "ws-relay-registrytest" not in names, (
        "finished worker still registered: %r" % (names,))


def test_server_path_never_advances_the_generation():
    """PIN: the epoch guard and the drain are TEST-HARNESS-ONLY mechanisms.

    The whole design rests on production inertness: the server never retires an
    epoch, so ``_new_job`` never rejects an operator's job, and no request path
    joins turn workers (that would block the socket loop on a live turn). A
    future refactor that wires ``advance_job_generation`` or ``drain_workers``
    into a server code path would silently arm the guard in production — real
    chats could be refused or turns cut short. This test makes that refactor
    fail loudly instead of shipping.

    Checked statically over the shipped modules: the only occurrences allowed
    are the definitions themselves, the routes_ws pass-through wrapper, and the
    log/message strings.
    """
    import pathlib
    import re

    pkg = pathlib.Path(ro.__file__).resolve().parent
    offenders = []
    call_re = re.compile(r"\b(advance_job_generation|drain_workers)\s*\(")
    for path in sorted(pkg.glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if not call_re.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith(("def ", "cpdef ")):
                continue                       # the definition itself
            if stripped.startswith("return _ro.drain_workers"):
                continue                       # routes_ws pass-through wrapper
            offenders.append("%s:%d %s" % (path.name, lineno, stripped))
    assert not offenders, (
        "server code must never advance the job epoch or drain turn workers "
        "(isolation-harness-only mechanisms, t_163fa09f): %r" % (offenders,))
