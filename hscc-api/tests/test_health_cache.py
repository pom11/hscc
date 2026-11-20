"""The cached verify snapshot behind GET /v1/health (t_7babf634).

The defect these tests pay for: ``handle_health`` ran ``verify.run_all()`` on
the request thread — all 17 checks of the fleet smoke test, real chat
round-trips to every serving GPU unit included — once per poll, with no cache
and no dedupe on a ``ThreadingHTTPServer``. Every assertion here is therefore
about *how often the verify seam is reached*, not about wall-clock timing: no
test sleeps to "prove" speed, and no test ever touches a real verify, GPU or
network. ``routes_cluster._backing_verify`` / ``routes_ops._backing_verify``
are the fakes, exactly like the rest of this suite stubs its ``_backing_*``
seams.

Covered:
  * a fresh snapshot answers a poll with the verify untouched;
  * nothing cached anywhere -> exactly ONE inline verify, shared by every
    concurrent caller (the point of the card: N clients x 64 s = one verify);
  * a stale snapshot is returned immediately (the caller does NOT wait for the
    refresh) while one — and only one — background refresh runs;
  * no verdict is ever invented: a verify that raises or returns a non-verdict
    stores nothing and cannot overwrite the last good snapshot;
  * with no verdict obtainable at all, sequential polls still cost ONE verify
    per ``TTL_SECONDS`` window — and the window never becomes an answer, never
    hides a snapshot, and never gates ``GET /v1/verify`` (t_a09ce4ed);
  * the payload stays a strict superset of {ok, checks, speak} for the iOS
    HealthResponse, and a stale snapshot says so in ``speak``;
  * GET /v1/verify keeps inline semantics and records into the same cache.
"""

import copy
import json
import os
import threading
import time

import pytest

import api_server
import health_cache
import routes_cluster
import routes_ops
from test_api import RunningServer

TTL = health_cache.TTL_SECONDS

CHECKS_OK = [
    {"name": "plugins", "ok": True, "detail": "ok"},
    {"name": "chat_roundtrip", "ok": True, "detail": "ok"},
]
CHECKS_BAD = [
    {"name": "plugins", "ok": True, "detail": "ok"},
    {"name": "proxy", "ok": False, "detail": "down"},
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class FakeVerify:
    """The verify seam. Counts calls; can block, raise, or return a verdict."""

    def __init__(self, result=None, raises=None, gate=None):
        self.result = result
        self.raises = raises
        self.gate = gate          # threading.Event the fake waits on
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        if self.gate is not None:
            assert self.gate.wait(timeout=10), "test released the verify gate too late"
        if self.raises is not None:
            raise self.raises
        return copy.deepcopy(self.result)


def _never_verify():
    """A seam that must never be reached; records that it was."""
    raise AssertionError("verify must not run on this path")


def _wait_idle(timeout=10.0):
    """Block until no background refresh is in flight (so none leaks to a sibling)."""
    deadline = time.time() + timeout
    with health_cache._COND:
        while health_cache._IN_FLIGHT:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError("a health refresh never settled")
            health_cache._COND.wait(remaining)


def _write_snapshot(generated_at, ok=True, checks=None, unverified=None):
    """Hand-write the snapshot file in the documented on-disk shape.

    Written by hand (not by ``record()``) on purpose: it also proves the reader
    accepts the shape the card specified, and lets a test backdate a verdict.
    """
    snap = {
        "generated_at": generated_at,
        "result": {
            "ok": ok,
            "checks": checks if checks is not None else copy.deepcopy(CHECKS_OK),
            "unverified": unverified if unverified is not None else [],
        },
    }
    path = health_cache.snapshot_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    return snap


@pytest.fixture
def running(tmp_path):
    srv = RunningServer(hscc_dir=str(tmp_path))
    yield srv
    srv.close()


@pytest.fixture
def token(running):
    return api_server.load_token(running.server.ctx.hscc_dir)


@pytest.fixture(autouse=True)
def _no_refresh_left_behind():
    """A refresh thread must not outlive the test that started it.

    Without this, a background verify finishing during a *sibling* test would
    populate the module-global cache that test is trying to reason about.
    """
    yield
    _wait_idle()


def _req(running, token, path):
    return running.request(token=token, path=path)


# --------------------------------------------------------------------------- #
# /v1/health serves the cache
# --------------------------------------------------------------------------- #

def test_health_never_calls_verify_when_fresh(running, token, monkeypatch):
    """THE timing claim, as a unit result: a fresh snapshot is answered with
    the verify seam reached ZERO times (no wall-clock promise implied)."""
    health_cache.record({"ok": True, "checks": CHECKS_OK, "unverified": []})
    fake = FakeVerify(result={"ok": False, "checks": CHECKS_BAD})
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    status, payload = _req(running, token, "/v1/health")

    assert status == 200
    assert fake.calls == 0
    assert payload["ok"] is True               # the cached verdict, not the fake's
    assert payload["checks"] == CHECKS_OK
    assert payload["speak"] == "All checks passed."
    assert payload["source"] == "cache"
    assert payload["stale"] is False
    assert payload["age_seconds"] < TTL
    assert abs(payload["generated_at"] - int(time.time())) <= 2


def test_health_payload_is_a_strict_superset_of_the_old_shape(
        running, token, monkeypatch):
    """iOS decodes ``HealthResponse`` {ok: Bool, checks: [...], speak: String}
    in a gate (ios-app/scripts/connection_probe_check); ok must stay a bool and
    the three keys must survive. The four new keys are additive."""
    health_cache.record({"ok": False, "checks": CHECKS_BAD, "unverified": ["proxy"]})
    monkeypatch.setattr(routes_cluster, "_backing_verify", _never_verify)

    status, payload = _req(running, token, "/v1/health")

    assert status == 200
    assert {"ok", "checks", "speak"} <= set(payload)
    assert set(payload) == {"ok", "checks", "speak", "age_seconds",
                            "generated_at", "stale", "source"}
    assert isinstance(payload["ok"], bool) and payload["ok"] is False
    assert isinstance(payload["generated_at"], int)
    assert isinstance(payload["age_seconds"], int)
    assert payload["stale"] is False
    assert payload["source"] in ("cache", "inline")


def test_first_ever_health_request_runs_one_inline_verify_and_caches_it(
        running, token, monkeypatch):
    """No snapshot anywhere (fresh process, no file) -> one inline verify,
    then every later poll is a cache hit on the same verdict."""
    fake = FakeVerify(result={"ok": False, "checks": CHECKS_BAD,
                              "unverified": ["proxy"]})
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    status, payload = _req(running, token, "/v1/health")

    assert status == 200
    assert fake.calls == 1
    assert payload["source"] == "inline"
    assert payload["ok"] is False
    assert payload["speak"].startswith("1 of 2 checks have problems")
    assert "proxy" in payload["speak"]
    assert payload["stale"] is False

    # Persisted in the documented shape, atomically (tmp + os.replace leaves no
    # litter behind, which is what makes the file safe to read concurrently).
    path = health_cache.snapshot_path()
    on_disk = json.loads(open(path, encoding="utf-8").read())
    assert set(on_disk) == {"generated_at", "result"}
    assert isinstance(on_disk["generated_at"], int)
    assert set(on_disk["result"]) == {"ok", "checks", "unverified"}
    assert on_disk["result"]["ok"] is False
    assert on_disk["result"]["unverified"] == ["proxy"]
    leftovers = [p for p in os.listdir(os.path.dirname(path))
                 if p.startswith(health_cache.SNAPSHOT_FILE + ".tmp")]
    assert leftovers == []

    _ , second = _req(running, token, "/v1/health")
    assert second["source"] == "cache"
    assert second["generated_at"] == payload["generated_at"]
    assert fake.calls == 1


def test_concurrent_first_callers_share_one_verify(monkeypatch):
    """The card's point: N clients must become ONE verify.

    Eight health polls arrive while the only verify is still running (the fake
    is held on a gate). Every one of them must be served from the single
    verify's result instead of starting its own 64 s of GPU chat.
    """
    gate = threading.Event()
    fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK}, gate=gate)
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    seen = []
    threads = [threading.Thread(
        target=lambda: seen.append(routes_cluster.handle_health(None, None, {}, None)))
        for _ in range(8)]
    for t in threads:
        t.start()

    # One verify is in flight; the other seven are parked on the single-flight
    # condition, not on their own verifies.
    deadline = time.time() + 5
    while fake.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert fake.calls == 1
    gate.set()
    for t in threads:
        t.join(timeout=10)

    assert fake.calls == 1, f"{fake.calls} verifies for 8 concurrent health polls"
    assert len(seen) == 8
    payloads = [p[1] for p in seen]
    assert {p["generated_at"] for p in payloads} == {payloads[0]["generated_at"]}
    assert all(p["ok"] is True and p["checks"] == CHECKS_OK for p in payloads)


# --------------------------------------------------------------------------- #
# Stale-while-revalidate
# --------------------------------------------------------------------------- #

def test_stale_snapshot_is_served_immediately_and_revalidated_once(
        running, token, monkeypatch):
    """A stale verdict is returned WITHOUT waiting for the refresh: the fake
    verify stays blocked on a gate for the whole first response, so "it answered
    with the old verdict" is proof the caller did not wait on a verify."""
    _write_snapshot(generated_at=int(time.time()) - (TTL + 30),
                    ok=False, checks=CHECKS_BAD)
    health_cache.reset_state()          # force the cold-start disk read
    gate = threading.Event()
    fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK}, gate=gate)
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    status, payload = _req(running, token, "/v1/health")

    assert status == 200
    assert payload["ok"] is False                    # the STALE verdict, as-is
    assert payload["checks"] == CHECKS_BAD
    assert payload["source"] == "cache"
    assert payload["stale"] is True
    assert payload["age_seconds"] >= TTL
    # honest about age: never implies a check that just ran
    assert "Health from" in payload["speak"] and payload["speak"].rstrip().endswith("ago.")
    assert fake.calls == 1                           # one background refresh kicked

    # Polls while it revalidates keep getting the stale verdict, and none of
    # them starts a second verify.
    for _ in range(3):
        status, again = _req(running, token, "/v1/health")
        assert again["ok"] is False and again["stale"] is True
    assert fake.calls == 1

    gate.set()
    _wait_idle()

    status, refreshed = _req(running, token, "/v1/health")
    assert refreshed["ok"] is True
    assert refreshed["stale"] is False
    assert refreshed["speak"] == "All checks passed."
    assert refreshed["source"] == "cache"
    assert fake.calls == 1


def test_ttl_boundary_is_strictly_greater_than(monkeypatch):
    """age == TTL is fresh; one second past it is stale."""
    now = 1_700_000_000
    monkeypatch.setattr(health_cache.time, "time", lambda: now)
    fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK})

    health_cache.record({"ok": False, "checks": CHECKS_BAD},
                        generated_at=now - TTL)
    view = health_cache.get_snapshot(verify=fake)
    assert view["age_seconds"] == TTL
    assert view["stale"] is False
    assert view["source"] == "cache"

    health_cache.record({"ok": False, "checks": CHECKS_BAD},
                        generated_at=now - TTL - 1)
    view = health_cache.get_snapshot(verify=fake)
    assert view["age_seconds"] == TTL + 1
    assert view["stale"] is True
    _wait_idle()
    assert fake.calls == 1                           # the revalidate it kicked


# --------------------------------------------------------------------------- #
# Never invent a verdict
# --------------------------------------------------------------------------- #

def test_no_verdict_is_never_invented(running, token, monkeypatch):
    """No snapshot + a verify that raises -> the honest old failure shape
    ({speak: "health check unavailable"}, no fabricated ok) and no file."""
    monkeypatch.setattr(routes_cluster, "_backing_verify",
                        FakeVerify(raises=RuntimeError("sparkrun exploded")))

    status, payload = _req(running, token, "/v1/health")

    assert status == 200
    assert payload == {"speak": "health check unavailable"}
    assert not os.path.exists(health_cache.snapshot_path())


@pytest.mark.parametrize("junk", [
    None, "not a result", {}, {"ok": True}, {"ok": "true", "checks": []},
    {"ok": None, "checks": CHECKS_OK},          # unverified != a verdict
    {"ok": True, "checks": "nope"},
])
def test_record_ignores_a_result_that_is_not_a_verdict(junk):
    health_cache.record({"ok": True, "checks": CHECKS_OK, "unverified": []})
    assert health_cache.record(junk) is None
    view = health_cache.get_snapshot(verify=_never_verify)
    assert view["result"]["ok"] is True          # the good snapshot survived


def test_a_failing_refresh_never_overwrites_a_good_snapshot(running, token, monkeypatch):
    """Stale + good snapshot + verify raises -> the client is still served the
    last good verdict, and neither copy is replaced by nothing."""
    good = health_cache.record({"ok": True, "checks": CHECKS_OK},
                               generated_at=int(time.time()) - (TTL + 5))
    health_cache.reset_state()
    monkeypatch.setattr(routes_cluster, "_backing_verify",
                        FakeVerify(raises=RuntimeError("gpu offline")))

    status, payload = _req(running, token, "/v1/health")
    assert payload["ok"] is True and payload["stale"] is True
    _wait_idle()

    status, again = _req(running, token, "/v1/health")
    assert again["ok"] is True and again["stale"] is True
    on_disk = json.loads(open(health_cache.snapshot_path(), encoding="utf-8").read())
    assert on_disk["generated_at"] == good["generated_at"]
    assert on_disk["result"]["ok"] is True


def test_an_unparseable_snapshot_file_is_not_a_verdict(running, token, monkeypatch):
    """A corrupt file counts as "no snapshot anywhere": one inline verify, not
    a fabricated verdict read off the disk."""
    path = health_cache.snapshot_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"generated_at": "yesterday", ')      # truncated JSON
    health_cache.reset_state()
    fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK})
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    status, payload = _req(running, token, "/v1/health")

    assert payload["ok"] is True and payload["source"] == "inline"
    assert fake.calls == 1


# --------------------------------------------------------------------------- #
# The no-verdict retry window (t_a09ce4ed)
#
# Everything above proves a verdict is served once one exists. This section is
# the case where one never will: sparkrun gone, the GPU fleet powered down, the
# deployed verify.py broken. Nothing gets cached, so single-flight protects
# concurrent polls and nothing protected sequential ones — until the window.
# --------------------------------------------------------------------------- #

UNAVAILABLE = {"speak": "health check unavailable"}


def _window_open() -> bool:
    """Whether the retry window is armed (a failed attempt is being honoured)."""
    with health_cache._COND:
        return health_cache._RETRY_AFTER is not None and (
            health_cache._clock() < health_cache._RETRY_AFTER)


def _close_backoff_window():
    """Declare the retry window over, without waiting TTL seconds for it.

    The window is one monotonic deadline, so "the window has passed" is a fact
    about that number; moving it into the past is exactly what the passage of
    TTL_SECONDS does to it, minus a two-minute test.
    """
    with health_cache._COND:
        assert health_cache._RETRY_AFTER is not None, "no window to close"
        health_cache._RETRY_AFTER = health_cache._clock() - 0.001


def test_repeated_polls_with_no_verdict_obtainable_run_one_verify(
        running, token, monkeypatch):
    """THE case this card exists for. Four SEQUENTIAL health polls (the iOS
    ConnectionMonitor's cadence; single-flight is no help to sequential ones)
    on a fleet whose verify cannot produce a verdict, must cost ONE verify —
    and every poll must be answered with the failure shape the route has always
    used for "no verdict", with NO ``ok`` key. A backoff that answered
    ``ok: False`` would be inventing the verdict the card forbids."""
    fake = FakeVerify(raises=RuntimeError("sparkrun gone"))
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    for poll in range(4):
        status, payload = _req(running, token, "/v1/health")
        assert status == 200, poll
        assert payload == UNAVAILABLE, poll
        assert "ok" not in payload and "checks" not in payload, poll

    assert fake.calls == 1, (
        f"{fake.calls} verifies for 4 sequential no-verdict polls")
    # And the failure left no durable trace: a failed attempt is not a verdict,
    # so it must never reach the snapshot file (which would be served as one).
    assert not os.path.exists(health_cache.snapshot_path())


def test_a_verify_that_returns_no_verdict_arms_the_window_too(
        running, token, monkeypatch):
    """The window is about "no verdict", not about "the seam raised". A verify
    that comes back with no bool ``ok`` is the same broken fleet and must not
    be re-run on the next poll either."""
    fake = FakeVerify(result={"ok": None, "checks": CHECKS_OK})
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    for poll in range(3):
        status, payload = _req(running, token, "/v1/health")
        assert payload == UNAVAILABLE, poll

    assert fake.calls == 1
    assert not os.path.exists(health_cache.snapshot_path())


def test_the_window_defers_a_probe_it_does_not_stop_it(running, token,
                                                       monkeypatch):
    """The bound on the silence, pinned from both sides.

    A broken fleet must still be probed — the window throttles how often a
    probe may run, it never removes the probe. So while the window is open no
    poll may spend a verify, and the first poll after it closes must spend
    exactly one, then park again.
    """
    fake = FakeVerify(raises=RuntimeError("gpu fleet down"))
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE
    assert fake.calls == 1 and _window_open()
    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE
    assert fake.calls == 1                           # window open: no probe

    _close_backoff_window()
    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE
    assert fake.calls == 2                           # window shut: probed again

    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE
    assert fake.calls == 2                           # and the window is armed again


def test_a_fleet_that_recovers_is_served_within_one_window(running, token,
                                                           monkeypatch):
    """What the window costs, and the bound on the cost: once the seam works
    again, the next probe after the window returns the real verdict, the window
    is cleared by storing it, and every later poll is a cache hit."""
    fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK},
                      raises=RuntimeError("sparkrun gone"))
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE

    fake.raises = None                                # the operator fixed it
    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE   # still parked
    assert fake.calls == 1

    _close_backoff_window()
    status, payload = _req(running, token, "/v1/health")
    assert payload["ok"] is True and payload["source"] == "inline"
    assert fake.calls == 2
    assert not _window_open()                         # storing a verdict ends it

    status, again = _req(running, token, "/v1/health")
    assert again["ok"] is True and again["source"] == "cache"
    assert fake.calls == 2                            # no verify since the recovery


def test_concurrent_pollers_do_not_each_run_a_failed_verify(monkeypatch):
    """The concurrent case was NOT safe either once the verify FAILS.

    ``test_concurrent_first_callers_share_one_verify`` pins the succeeding
    verify. When it fails, every waiter used to wake to an empty cache and the
    first one became the next runner — 8 polls, 8 back-to-back verifies. With
    the shared failure arming the window, all 8 are answered "no verdict" and
    the seam is reached once.
    """
    gate = threading.Event()
    fake = FakeVerify(raises=RuntimeError("gpu offline"), gate=gate)
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    seen = []
    threads = [threading.Thread(
        target=lambda: seen.append(routes_cluster.handle_health(None, None, {}, None)))
        for _ in range(8)]
    for t in threads:
        t.start()

    deadline = time.time() + 5
    while fake.calls < 1 and time.time() < deadline:
        time.sleep(0.01)
    gate.set()
    for t in threads:
        t.join(timeout=10)

    assert fake.calls == 1, f"{fake.calls} verifies for 8 concurrent failing polls"
    assert len(seen) == 8
    assert all(response == (200, UNAVAILABLE) for response in seen)


def test_the_window_does_not_gate_v1_verify(running, token, monkeypatch):
    """``GET /v1/verify`` is the explicit trigger: window open or not, it runs
    every time. Pinned end-to-end — a failing health poll arms the window, the
    operator's manual verify still runs, and its verdict then answers
    /v1/health from cache without health verifying anything itself."""
    health_fake = FakeVerify(raises=RuntimeError("sparkrun gone"))
    monkeypatch.setattr(routes_cluster, "_backing_verify", health_fake)

    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE
    assert health_fake.calls == 1 and _window_open()

    ops_fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK})
    monkeypatch.setattr(routes_ops, "_backing_verify", ops_fake)

    status, payload = _req(running, token, "/v1/verify")
    assert status == 200
    assert ops_fake.calls == 1                        # never gated by the window
    assert payload["ok"] is True

    status, health = _req(running, token, "/v1/health")
    assert health["ok"] is True and health["source"] == "cache"
    assert health_fake.calls == 1                     # health ran none of its own


def test_an_open_window_never_hides_a_snapshot(running, token, monkeypatch):
    """Scope pin: the window is consulted ONLY when there is nothing to serve.

    Arm it with a failed probe, then put a STALE verdict where the route can
    find it (the failed probe stored nothing, so the in-process copy is still
    empty and the next poll reads the disk). The route must serve the stale
    verdict and kick its background refresh as if no window existed — deferring
    a cold-path probe must never mute a real answer or its revalidation.
    """
    fake = FakeVerify(raises=RuntimeError("gpu offline"))
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake)

    assert _req(running, token, "/v1/health")[1] == UNAVAILABLE
    assert _window_open()

    _write_snapshot(generated_at=int(time.time()) - (TTL + 30),
                    ok=True, checks=CHECKS_OK)
    fake.raises = None

    status, payload = _req(running, token, "/v1/health")
    assert payload["ok"] is True and payload["stale"] is True
    assert payload["source"] == "cache"
    assert "Health from" in payload["speak"]
    assert fake.calls == 2                            # the revalidation still ran
    _wait_idle()


def test_reset_state_clears_the_window():
    """The isolation contract the autouse fixture in conftest.py relies on: a
    test that armed the window by failing must not leave its neighbour unable
    to probe at all."""
    fake = FakeVerify(raises=RuntimeError("boom"))

    assert health_cache.get_snapshot(verify=fake) is None
    assert _window_open()

    health_cache.reset_state()
    with health_cache._COND:
        assert health_cache._RETRY_AFTER is None

    assert health_cache.get_snapshot(verify=fake) is None
    assert fake.calls == 2                            # probed, not parked


def test_storing_a_verdict_ends_the_window():
    """The window covers ONLY "nothing to serve", so a verdict ends it.

    Pinned as state, not as behaviour: while a verdict is in the in-process copy
    the route answers from path 1 and never reaches the check, so the observable
    calls are identical either way — what must not drift is the stamp. A window
    left armed next to a verdict could outlive the copy it was reasoned about
    and defer the next probe for no reason.
    """
    fake = FakeVerify(raises=RuntimeError("gpu down"))

    assert health_cache.get_snapshot(verify=fake) is None
    assert _window_open()

    health_cache.record({"ok": True, "checks": CHECKS_OK, "unverified": []})
    assert not _window_open()
    view = health_cache.get_snapshot(verify=_never_verify)
    assert view is not None and view["result"]["ok"] is True


# --------------------------------------------------------------------------- #
# The in-process copy (the hot path must not read the disk)
# --------------------------------------------------------------------------- #

def test_hot_path_never_reads_the_disk(monkeypatch):
    """With a verdict in memory, the snapshot file is irrelevant: delete it and
    blank the loader — the answer must not change and the disk must not be read."""
    health_cache.record({"ok": True, "checks": CHECKS_OK})
    os.unlink(health_cache.snapshot_path())
    reads = []
    monkeypatch.setattr(health_cache, "_read_disk",
                        lambda: (reads.append(1), None)[1])
    fake = FakeVerify(result={"ok": False, "checks": CHECKS_BAD})

    view = health_cache.get_snapshot(verify=fake)

    assert view["source"] == "cache"
    assert view["result"]["ok"] is True
    assert reads == []
    assert fake.calls == 0


# --------------------------------------------------------------------------- #
# request_refresh
# --------------------------------------------------------------------------- #

def test_request_refresh_is_single_flight():
    gate = threading.Event()
    fake = FakeVerify(result={"ok": True, "checks": CHECKS_OK}, gate=gate)

    assert health_cache.request_refresh(verify=fake) is True
    assert health_cache.request_refresh(verify=fake) is False
    assert health_cache.request_refresh(verify=fake) is False

    gate.set()
    _wait_idle()
    assert fake.calls == 1
    assert health_cache.get_snapshot(verify=_never_verify)["result"]["ok"] is True


# --------------------------------------------------------------------------- #
# GET /v1/verify: still inline, and it feeds the same cache
# --------------------------------------------------------------------------- #

def test_verify_route_still_runs_inline_even_with_a_fresh_cache(
        running, token, monkeypatch):
    """/v1/verify is the explicit trigger: it must NOT serve the cache, and its
    own response shape is unchanged (no age/source keys added)."""
    health_cache.record({"ok": True, "checks": CHECKS_OK})
    fake = FakeVerify(result={"ok": False, "checks": CHECKS_BAD})
    monkeypatch.setattr(routes_ops, "_backing_verify", fake)

    status, payload = _req(running, token, "/v1/verify")

    assert status == 200
    assert fake.calls == 1
    assert payload["ok"] is False                  # its own run, not the cached ok
    assert set(payload) == {"ok", "checks", "speak"}


def test_verify_route_records_into_the_same_cache(running, token, monkeypatch):
    """A manual verify refreshes the health screen — the two routes cannot
    drift. Proven from disk: the in-process copy is dropped first, so the only
    way /v1/health can know this verdict is that /v1/verify recorded it."""
    fake_ops = FakeVerify(result={"ok": False, "checks": CHECKS_BAD,
                                  "unverified": ["proxy"]})
    monkeypatch.setattr(routes_ops, "_backing_verify", fake_ops)

    status, verify_payload = _req(running, token, "/v1/verify")
    assert verify_payload["ok"] is False
    assert fake_ops.calls == 1

    health_cache.reset_state()
    fake_health = FakeVerify(result={"ok": True, "checks": CHECKS_OK})
    monkeypatch.setattr(routes_cluster, "_backing_verify", fake_health)

    status, health_payload = _req(running, token, "/v1/health")

    assert health_payload["ok"] is False           # the verdict /v1/verify produced
    assert "proxy" in health_payload["speak"]
    assert health_payload["source"] == "cache"
    assert fake_health.calls == 0                  # health ran no verify of its own


# --------------------------------------------------------------------------- #
# speak honesty (pure)
# --------------------------------------------------------------------------- #

def test_speak_health_wording_is_unchanged_for_a_fresh_verdict():
    assert routes_cluster._speak_health({"ok": True, "checks": CHECKS_OK}) == \
        "All checks passed."
    assert routes_cluster._speak_health({"ok": False, "checks": CHECKS_BAD}) == \
        "1 of 2 checks have problems. (proxy)"


def test_speak_health_labels_a_stale_verdict_by_age():
    s = routes_cluster._speak_health({"ok": True, "checks": CHECKS_OK},
                                     age_seconds=250, stale=True)
    assert s == "All checks passed. Health from 4m ago."
    s = routes_cluster._speak_health({"ok": False, "checks": CHECKS_BAD},
                                     age_seconds=90, stale=True)
    assert s == "1 of 2 checks have problems. (proxy) Health from 1m ago."


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (59, "59s"), (60, "1m"), (125, "2m"),
    (3600, "1h00m"), (11520, "3h12m"), (187200, "2d04h"), (-5, "0s"),
])
def test_humanize_age(seconds, expected):
    assert routes_cluster._humanize_age(seconds) == expected


def test_humanize_age_survives_a_missing_value():
    assert routes_cluster._humanize_age(None) == ""
