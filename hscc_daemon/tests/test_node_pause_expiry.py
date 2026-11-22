"""Tests for the daemon's node-pause expiry tick (card t_110adc1a).

The seam under test is ``node_pause_exec.check_node_pause_expiry`` — the stream
``daemon_ops`` runs every 60 s — plus the two functions behind it
(``auto_resume_expired``: read the overlay once, decide, resume; ``expiry_tick``:
run the pass and stamp the stream on every path).

Hermetic by construction (``nolive-rule.txt``, and the same posture as
``test_node_cli.py`` / ``test_node_pause_exec.py``):

* every host is an RFC 5737 documentation placeholder (192.0.2.x);
* ``serving.load_serving`` and ``node_pause_exec.fetch_cluster_status`` are
  patched, and every lifecycle command goes to an injected fake ``run`` — so no
  test here reads the operator's serving.json, runs sparkrun's status query, or
  spawns a single subprocess;
* the overlay is a temp dir passed through ``directory=`` (the seam the card
  asked for), and the stream stamp goes to a temp ``state_dir``. The daemon leg's
  autouse ``_isolate_hscc`` fixture already seals ``node_pauses.HSCC_DIR`` and
  ``heal_breaker.HEAL_BREAKER_FILE``, and the tests that exercise the DEFAULT
  root (which is what the live daemon uses — ``check_node_pause_expiry()`` takes
  no arguments) guard-assert that seal below, because a tick test that forgot it
  would park a real node and the running daemon would start skipping it.

One ordering rule inherited from #568, kept here by construction rather than by
an AST guard: a pause is NEVER seeded before the heal strikes a case depends on
(``note_heal`` is a no-op on a node the overlay holds), and every slate is
asserted non-empty before the park. The guard that enforces this against
regression lives in ``test_node_cli.py::TestNoStrikeAccrualBehindAPause``, which
reads its own file; this file has no park-then-expect-accrual fixture to guard.
"""

import inspect
import io
import json
import os
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hscc_daemon.hscc as unified
import hscc_daemon.node_pause_exec as npe
from hscc_daemon import daemon_ops, heal_breaker, node_pauses, serving, util

# ── the fixture fleet (placeholders only) ──────────────────────────────────
W1 = "192.0.2.12"
W2 = "192.0.2.13"
W3 = "192.0.2.14"
W4 = "192.0.2.15"
W5 = "192.0.2.16"
W6 = "192.0.2.17"

WORKER_RECIPE = "/srv/recipes/worker.yaml"
SPAN_RECIPE = "/srv/recipes/worker-tp.yaml"

SERVING = {
    "port": 8000,
    "units": [
        {"id": "w1", "role": "worker", "nodes": [W1], "port": 8100,
         "recipe": WORKER_RECIPE, "model": "meta-llm/Work-8B", "tp": 1},
        {"id": "wspan", "role": "worker", "nodes": [W1, W2], "port": 8200,
         "recipe": SPAN_RECIPE, "model": "meta-llm/Work-8B", "tp": 2},
    ],
}

#: The single clock every case is judged against, so "expired" and "future" are
#: facts about the fixture, not about when pytest ran.
#:
#: It is a FIXED instant on purpose, and that is safe for exactly one reason:
#: ``TestFixtureClockIsTheOnlyClock`` below reddens this file if any case lets the
#: real wall clock answer one of its questions. The two halves of the history are
#: the argument for choosing the pin rather than the anchor:
#:
#:   * a fixed instant WITHOUT such a pin decays. Every ``at(3600)`` neighbour
#:     pause became a past-due record the moment the wall clock passed the literal
#:     hour, ``resume_host``'s span guard (``continue`` on an expired other pause,
#:     node_pause_exec.py) stepped aside exactly as designed, and the two
#:     span-refusal cases went red on an unchanged tree — CI leg hscc_daemon green
#:     on main at 12:45 UTC, red at 14:14 UTC on an unrelated PR, with
#:     ``{'action': 'resumed', 'ok': True}`` where the refusal was asserted.
#:   * anchoring the instant to IMPORT time instead (PR #617) does hide that
#:     decay, but it makes the fixture clock *equal* the wall clock, and the two
#:     cases that let ``resume_host`` take a clock of their own go quiet: an
#:     omitted clock then agrees with the wall clock by construction, so the case
#:     keeps passing with nothing pinned inside it. Card t_3c11f9d1 rejected that
#:     trade; the source guard in ``TestFixtureClockIsTheOnlyClock`` keeps it
#:     rejected, because no behavioural arm can see that particular move.
#:
#: So the anchor stays put, every case hands the policy a clock of its own (the
#: ``now=NOW`` argument, or the pinned fallback below), and the pin — not the
#: anchor, and not this comment — is what keeps that honest.
_T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
NOW = _T0.isoformat()


def at(seconds):
    """An ISO ``until`` this file can read out loud: ``at(-1)`` is one second
    past due, ``at(3600)`` is an hour from now."""
    return (_T0 + timedelta(seconds=seconds)).isoformat()


# ── the two clock seams a case can be wrong about ───────────────────────────
# ``node_pauses.is_expired(entry, now)`` asks ``_utc(now)``, and ``_utc`` is the
# only place the overlay reads the wall clock (node_pauses.py:116-132): every
# other value it handles was handed to it. So a case has exactly two ways to be
# honest, and they are different patches — which is why both are named here
# rather than in prose.
class _AnchorIsNotImportTime(type(datetime)):
    """Keeps ``isinstance(x, _WallClock)`` true for a REAL datetime.

    Without it, replacing the name ``datetime`` in ``node_pauses`` would make
    ``_utc``'s ``isinstance(value, datetime)`` arm reject real datetimes, and a
    pin that redirects one read would silently break two others.
    """

    def __instancecheck__(cls, instance):
        return isinstance(instance, datetime)


class _WallClock(datetime, metaclass=_AnchorIsNotImportTime):
    """The wall clock, answering a chosen instant.

    A SUBCLASS so ``fromisoformat``, ``astimezone`` and comparisons keep working
    — the point is to move ONE read (``datetime.now``), never to replace the type.
    """

    _at = None

    @classmethod
    def now(cls, tz=None):
        assert cls._at is not None, "the pin must install an instant"
        return cls._at if tz is None else cls._at.astimezone(tz)


#: The instant the pin pretends the wall clock reads: eight decades past the
#: FIXTURE, not an absolute year. Absolute would be weaker and the reason matters.
#: A guard hard-coded to 2099 stops biting the moment anyone moves ``_T0`` past
#: it — and "just bump ``_T0`` forward" is the exact fix card t_3c11f9d1 calls "the
#: same bomb with a longer fuse". Derived instead, it is always a decade's worth
#: of past due for every ``at(N)`` this file can write, wherever the anchor sits,
#: so it catches that fix too instead of relying on a reviewer to.
#:
#: 2099 is kept as an asserted FLOOR below, so "the wall clock is driven past
#: 2099" stays a checked fact rather than a comment.
WALL_FAR_FUTURE = _T0 + timedelta(days=365 * 80)
assert WALL_FAR_FUTURE >= datetime(2099, 1, 1, tzinfo=timezone.utc), (
    "the pin no longer drives the wall clock past 2099 — the fixture anchor has "
    "moved and this file's far-future instant moved with it, which is the point, "
    "but the floor below is what the guard's name promises")


def drive_the_wall_clock(monkeypatch, instant=WALL_FAR_FUTURE):
    """Move ``node_pauses``' wall clock to ``instant``.

    This is the clock the fixture cannot name — the one that answers
    ``_utc(None)`` when a caller brought no clock of its own. Applied by the pin
    class below, so the cases there run as if pytest were executed long after
    every deadline this file can seed.

    Must be applied BEFORE :func:`pin_the_fallback_clock`, which is what hides
    this read from a case that has been given a clock; the guard-assert below is
    the reason, and it fails loudly if the order is ever reversed.
    """
    stub = type("_PinnedWallClock", (_WallClock,), {"_at": instant})
    monkeypatch.setattr(node_pauses, "datetime", stub)
    assert node_pauses._utc(None) == instant, (
        "the wall clock was NOT redirected — a pin using this would be judging "
        "the real clock, and its green would mean nothing")
    return stub


def pin_the_fallback_clock(monkeypatch):
    """Make the overlay's no-clock fallback answer THE FIXTURE clock.

    ``resume_host`` documents that "a caller that has none (the CLI) lets this
    function take one read of its own" — that read is ``_utc(None)``. A verb-level
    case cannot pass ``now=`` (``hscc node resume`` takes no clock, and adding a
    test-only one to the verb would be product code bending to the test), so this
    is the seam: it redirects ``None`` to ``_T0`` and delegates every other arm
    — parse, naive-is-UTC, normalise — to the real ``_utc`` untouched.

    It is not a way to make a case pass. It is the same statement ``now=NOW``
    makes, for the one caller that has no way to make it.
    """
    real = node_pauses._utc

    def fixture_clock(value):
        return _T0 if value is None else real(value)

    monkeypatch.setattr(node_pauses, "_utc", fixture_clock)
    return fixture_clock


def _group(cid, recipe, hosts, port, tp=None):
    meta = {"recipe": recipe, "hosts": list(hosts), "port": port,
            "model": "meta-llm/Work-8B"}
    if tp:
        meta["tensor_parallel"] = tp
    return {cid: {"meta": meta,
                  "containers": [{"host": h, "role": "worker",
                                  "status": "running", "image": "img"}
                                 for h in hosts]}}


def fleet_payload():
    groups = {}
    groups.update(_group("cid_w1", WORKER_RECIPE, [W1], 8100))
    groups.update(_group("cid_span", SPAN_RECIPE, [W1, W2], 8200, tp=2))
    return {"groups": groups, "solo_entries": [], "errors": {},
            "idle_hosts": [], "preparing_hosts": [], "pending_ops": [],
            "total_containers": 0, "host_count": 0}


class Recorder:
    """Fake lifecycle runner: records argv, answers ok unless told otherwise."""

    def __init__(self, results=()):
        self.calls = []
        self._results = list(results)

    def __call__(self, cmd, timeout=None, **kw):
        self.calls.append([str(c) for c in cmd])
        if self._results:
            return self._results.pop(0)
        return {"ok": True, "output": "", "stderr": ""}


class Lines:
    """Fake ``echo``: what the daemon would log."""
    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


class Stamps:
    """Fake ``state.write_state``: every stamp the tick made, in order."""

    def __init__(self):
        self.stamped = []

    def __call__(self, stream_name, data, directory=None):
        self.stamped.append((stream_name, dict(data)))


@pytest.fixture
def fleet(monkeypatch):
    """Patch the seams a resume can reach: config, status, transport."""
    rec = Recorder()
    monkeypatch.setattr(serving, "load_serving", lambda path=None: SERVING)
    monkeypatch.setattr(npe, "fetch_cluster_status",
                        lambda: (fleet_payload(), None))
    monkeypatch.setattr(util, "run_cmd", rec)
    return rec


@pytest.fixture
def overlay(tmp_path):
    """A temp overlay root — the seam the card asked for.

    Guard-asserted, because a tick test that forgot ``directory=`` would park a
    real node and the running daemon would then start skipping it. The live root
    is read out of ``HOME``, not ``expanduser("~/.hscc")``: the conftest seal
    redirects ``expanduser`` too, so the obvious probe would compare the sealed
    root against itself and pass anywhere.
    """
    real = os.path.join(os.environ["HOME"], ".hscc")
    assert os.path.realpath(node_pauses.HSCC_DIR) != os.path.realpath(real), (
        "the overlay's default root is NOT sealed — a default-root write here "
        "would pause a real node")
    assert os.path.realpath(heal_breaker.HEAL_BREAKER_FILE) != os.path.realpath(
        os.path.join(real, "heal-breaker.json")), (
        "the heal ledger is NOT sealed — strikes would land on the live node")
    d = tmp_path / "overlay"
    d.mkdir()
    return str(d)


@pytest.fixture
def live_root(overlay):
    """``None`` — the DEFAULT overlay root, i.e. the one the CLI and daemon use.

    A verb-level case has to seed where the verb reads: ``hscc node …`` takes no
    ``--hscc-dir``, so it goes to ``node_pauses.HSCC_DIR``. The seal is asserted by
    the ``overlay`` fixture this reuses, so seeding here lands in the conftest's tmp
    root and never in the operator's real ``~/.hscc``.
    """
    return None


def seed(directory, host=W1, until=at(-1), units=("w1", "wspan"),
         span_nodes=(W1, W2)):
    """Write a pause through the overlay's OWN write path (the CLI's path).

    ``directory`` is the first positional on purpose: a seeded pause that lands
    in the default root instead of the test's overlay is the one fixture mistake
    that would still leave most assertions green (nothing is due) while quietly
    parking a host somewhere else.
    """
    return node_pauses.pause(
        host, reason="maintenance", until=until, units=list(units),
        span_nodes=list(span_nodes), stopped=True, directory=directory,
        now=NOW)


def earn(host=W1, n=1):
    """Accrue n heal strikes BEFORE any pause, and prove they landed.

    ``note_heal`` is a no-op on a node the overlay holds (#568), so a slate
    seeded after the park would be empty — and a test that then asserts the
    resume cleared it passes vacuously. The assertion below is what keeps that
    from being possible here.
    """
    for _ in range(n):
        heal_breaker.note_heal(host)
    assert heal_breaker.strike_count(host) == n, (
        f"the slate for {host} did not land — every clearing assertion below "
        "would be vacuous")
    return n


def cli(*argv):
    """Drive ``hscc node …`` in-process and capture what it prints.

    The verb dispatcher is called directly (``main()`` would need ``sys.argv``
    surgery for no extra coverage), and stdout is captured because the resume
    policy prints its own refusal through ``echo=print`` on this path.
    """
    out = io.StringIO()
    rc = None
    try:
        with redirect_stdout(out):
            rc = unified._handle_node(list(argv))
    except SystemExit as e:  # handlers must RETURN, never exit
        rc = e.code if isinstance(e.code, int) else 0
    return rc, out.getvalue()


# ── the pin: the fixture clock is the ONLY clock these cases can be judged by ─
class TestFixtureClockIsTheOnlyClock:
    """Supersedes #617's ``TestFixtureClock``, and keeps its promise.

    #617's own fragment states the goal exactly: *"prose did not stop the file
    from decaying once and will not stop it again — nothing in the leg reddened
    if someone set* ``_T0`` *back to a literal instant."* This class keeps the
    goal and changes the target, because the anchor is not the defect. The
    defect is a case letting ``_utc(None)`` — the wall clock — answer a question
    about the fixture. Pin THAT and a literal anchor is harmless; pin the anchor
    and the wall clock stays load-bearing in exactly the two cases that broke.

    The proof is the direction of the bite. #617's probe reddened the file when
    someone made the anchor fixed. These cases redden when someone makes a case
    *ask the wall clock* — by removing a ``now=`` argument or a fallback pin, the
    two edits that are actually load-bearing here — and they bite wherever the
    anchor sits, because :data:`WALL_FAR_FUTURE` is derived from it rather than
    written next to it.

    The fixed anchor is still the half that makes the rest cheap. With a fixed
    instant, the shipped cases are self-policing: drop one of their injections and
    it is red on the next run, not on some future date. With an import-time anchor
    they are self-*silencing* — an omitted clock agrees with the wall clock by
    construction, so the case keeps passing while nothing in it is pinned, and only
    the two scenarios duplicated below stay protected. Hence both halves, and the
    source guard at the end of this class for the one move no behavioural arm sees.
    """

    def test_the_fixture_clock_answers_what_the_wall_clock_used_to(self):
        """The honest descendants of #617's two real-clock cases.

        Same two questions — is ``at(-1)`` past due, is ``at(3600)`` still
        held — now asked of the clock the cases are actually judged by. Against
        the real clock they could only ever agree with the anchor by
        construction; against ``NOW`` they are facts about the fixture, which is
        what the anchor's comment always claimed they were.
        """
        assert node_pauses.is_expired({"until": at(-1)}, NOW) is True, (
            "at(-1) is not past due against the fixture clock — every 'expired' "
            "case here resumes nothing and passes vacuously")
        assert node_pauses.is_expired({"until": at(3600)}, NOW) is False, (
            "at(3600) is not held against the fixture clock — the span guard "
            "reads a still-parked neighbour as released")

    def test_the_verb_still_refuses_when_the_wall_clock_reads_past_2099(
            self, fleet, live_root, monkeypatch):
        """The CLI's arm: the same refusal, with pytest running past 2099.

        The CLI cannot bring a clock, so :func:`pin_the_fallback_clock` is what
        stands between this verb and the wall clock. Drop that one call and this
        case goes red — the fallback answers the far-future clock instead,
        ``at(3600)`` is ancient history, W2 stops counting as held, and the verb
        resumes half a span exactly as it did on 2026-11-22 at 13:00Z.
        """
        drive_the_wall_clock(monkeypatch)
        pin_the_fallback_clock(monkeypatch)
        seed(live_root, W1, until=at(-1), span_nodes=(W1, W2))
        seed(live_root, W2, until=at(3600), units=["wspan"],
             span_nodes=(W1, W2))
        rc, out = cli("resume", W1, "--yes")
        assert rc == 1 and "REFUSED" in out and W2 in out, (
            f"the wall clock decided this one, not the fixture: rc={rc} {out}")
        assert fleet.calls == [], "a refused span must not run anything"

    def test_the_policy_still_refuses_when_the_wall_clock_reads_past_2099(
            self, fleet, overlay, monkeypatch):
        """The direct-policy arm: same refusal, wall clock past 2099.

        Here the clock arrives as ``now=NOW`` — the argument a third caller
        (a plugin tool, a future verb) passes. Delete that argument and this case
        goes red for the same reason, by way of ``_utc(None)`` instead of the
        CLI. No fallback pin is installed in this arm on purpose: it would mask
        the very mutation this case exists to catch.
        """
        drive_the_wall_clock(monkeypatch)
        seed(overlay, W1, until=at(-1), span_nodes=(W1, W2))
        seed(overlay, W2, until=at(3600), units=["wspan"],
             span_nodes=(W1, W2))
        entry = node_pauses.paused(W1, directory=overlay)
        res = npe.resume_host(W1, entry, apply=True, directory=overlay,
                              echo=Lines(), run=fleet, now=NOW)
        assert res["action"] == npe.RESUME_KEPT and res["ok"] is False, (
            f"the wall clock answered the span question: {res}")
        assert fleet.calls == []

    def test_the_anchor_is_a_literal_the_file_owns_not_a_clock_reading(self):
        """#617's protection, running the other way — stated structurally.

        Anchoring ``_T0`` to import hides the decay by putting the fixture ON the
        wall clock, which is the trade this file refuses, and it is a quiet
        trade: the span cases keep passing while nothing in them is pinned. The
        two cases above still bite under that anchor (their far-future instant is
        derived from it, not written next to it), so this is the only arm that
        sees the move at all — and it is read off the source, the one place the
        choice is actually made.
        """
        source = Path(__file__).read_text(encoding="utf-8").splitlines()
        anchors = [ln for ln in source if ln.startswith("_T0 = ")]
        assert len(anchors) == 1, (
            f"expected exactly one anchor line to guard, found {len(anchors)}")
        assert ".now(" not in anchors[0], (
            "the fixture clock is being READ from the wall clock again "
            "(PR #617's shape) — that makes every `now=` injection in this file "
            "unpinnable: the two 2099 cases above stop biting the moment it is "
            "done, while staying green.")
        assert "datetime(" in anchors[0], (
            "the anchor is no longer a literal instant this file owns")


# ── ACCEPT 1: an expired pause resumes, clears, and unsuspends the node ─────
class TestExpiredResumesAndClears:
    def test_past_due_pause_restarts_clears_and_resets_strikes(self, fleet,
                                                               overlay):
        earn(W1, 7)
        assert heal_breaker.suspended(W1) > 0.0, \
            "the fixture must trip the breaker for 'unsuspended' to mean anything"
        seed(overlay, until=at(-1))
        stamps = Stamps()
        lines = Lines()

        entry = npe.expiry_tick(now=NOW, directory=overlay, state_dir=None,
                                write=stamps, echo=lines, run=fleet)

        assert entry["ok"] is True, entry
        assert entry["resumed"] == [W1], entry
        assert node_pauses.paused(W1, directory=overlay) is None, \
            "the pause record must be gone once the units are back up"
        assert heal_breaker.strike_count(W1) == 0, \
            "heal strikes must be cleared with the pause"
        assert heal_breaker.suspended(W1) == 0.0
        # Two recorded units, two sparkrun --ensure calls, and nothing else.
        assert len(fleet.calls) == 2, fleet.calls
        assert all("run" in c and "--ensure" in c for c in fleet.calls), \
            "resume must re-issue the declared workload through --ensure"
        assert "auto-resumed 1 expired pause" in entry["message"]
        assert [name for name, _e in stamps.stamped] == [npe.STREAM_NAME]
        assert stamps.stamped[0][1]["resumed"] == [W1]

    def test_the_alert_stamp_is_reset_so_a_later_trip_can_alert_again(
            self, fleet, overlay):
        """``heal_breaker.clear`` alone is not enough.

        The alert stamp is what makes ``should_alert`` quiet after a latch, so a
        resume that clears the ledger without resetting the stamp silences the
        node forever — the next genuine outage would heal and heal and never
        notify. This is the only test that can see the reset: every other
        clearing assertion in the file is about the ledger, which ``clear``
        empties by itself.
        """
        earn(W1, 7)
        assert heal_breaker.should_alert(W1) is True
        assert heal_breaker.should_alert(W1) is False, \
            "the cooldown must be what silenced the second alert"
        seed(overlay, until=at(-1))
        npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                        echo=Lines(), run=fleet)
        assert heal_breaker.should_alert(W1) is True, (
            "reset_alert did not run — this node would never alert again")

    def test_the_pause_record_is_cleared_only_after_a_successful_restart(
            self, fleet, overlay):
        """Order is the contract: units first, record second."""
        seed(overlay, until=at(-1))
        broken = Recorder([{"ok": False, "output": "", "stderr": "ensure failed"}])
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=broken)
        assert entry["ok"] is False, entry
        assert entry["kept"] == [W1], entry
        assert node_pauses.paused(W1, directory=overlay) is not None

    def test_the_next_tick_retries_a_pause_the_last_tick_could_not_clear(
            self, fleet, overlay):
        """The retry needs no memory: it is the file, re-read.

        This is also the restart case. Everything the first tick knew lived in
        a local variable and is gone; what survived is the overlay entry, and
        the second tick reaches the same decision the first one would have made
        had the fleet answered.
        """
        earn(W1, 1)
        seed(overlay, until=at(-1))
        bad = Recorder([{"ok": False, "output": "", "stderr": "ensure failed"}])
        first = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=bad)
        assert first["ok"] is False and first["resumed"] == []
        assert node_pauses.paused(W1, directory=overlay) is not None
        assert heal_breaker.strike_count(W1) == 1, \
            "a resume that did not happen must not clear the slate it did not earn"

        good = Recorder()
        second = npe.expiry_tick(now=NOW, directory=overlay,
                                 write=Stamps(), echo=Lines(), run=good)
        assert second["resumed"] == [W1], second
        assert node_pauses.paused(W1, directory=overlay) is None
        assert heal_breaker.strike_count(W1) == 0

    def test_a_pause_whose_command_cannot_be_rebuilt_is_kept_and_not_run(
            self, fleet, overlay):
        seed(overlay, until=at(-1), units=["w1", "vanished"])
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=fleet)
        assert entry["ok"] is False
        assert node_pauses.paused(W1, directory=overlay) is not None
        assert fleet.calls == [], "never guess a launch command"


# ── ACCEPT 2: a pause that is not due yet is left exactly as it was ─────────
class TestNotYetDueIsUntouched:
    def test_a_future_until_is_never_resumed(self, fleet, overlay):
        earn(W1, 1)
        seed(overlay, until=at(3600))
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=fleet)
        assert entry["ok"] is True and entry["resumed"] == []
        assert entry["message"] == "no expired pauses"
        assert fleet.calls == [], "not due is not a restart"
        assert node_pauses.paused(W1, directory=overlay) is not None
        assert heal_breaker.strike_count(W1) == 1

    def test_an_open_ended_pause_is_the_operators_to_end(self, fleet, overlay):
        seed(overlay, until=None)
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=fleet)
        assert entry["resumed"] == [] and fleet.calls == []
        assert node_pauses.paused(W1, directory=overlay) is not None, \
            "no deadline means no auto-resume, ever"

    def test_an_unreadable_until_fails_toward_still_parked(self, fleet,
                                                           overlay):
        """A deadline the overlay cannot parse is open-ended, not expired.

        ``node_pauses._normalize_entry`` maps garbage to ``None`` precisely so a
        typo cannot wake a node behind the operator's back; the tick inherits
        that, and this is the case that would otherwise auto-resume a machine
        somebody was still working on.
        """
        seed(overlay, until=at(-1))
        path = node_pauses.overlay_path(overlay)
        data = json.loads(Path(path).read_text())
        data["pauses"][W1]["until"] = "two days or whenever"
        Path(path).write_text(json.dumps(data))

        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=fleet)
        assert entry["resumed"] == [] and fleet.calls == []
        assert node_pauses.paused(W1, directory=overlay) is not None

    def test_the_decision_comes_from_the_file_not_from_a_cache(self, fleet,
                                                               overlay):
        """Change the file and the next tick changes its mind.

        The card's "durable until, never in-memory timers" claim, measured: the
        pass holds no state between calls, so the only thing that can make it
        resume is what is on disk right now.
        """
        seed(overlay, until=at(3600))
        assert npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                               echo=Lines(), run=fleet)["resumed"] == []
        # The operator extends... then shortens, the deadline, on disk.
        seed(overlay, until=at(-1))
        assert npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                               echo=Lines(), run=fleet)["resumed"] == [W1]
        assert len(fleet.calls) == 2, fleet.calls


# ── ACCEPT 4: no pauses ⇒ the tick costs nothing ────────────────────────────
class TestIdleTickIsFree:
    @pytest.fixture
    def no_fleet_at_all(self, monkeypatch):
        """Any fleet read, launch command or serving read is now a failure."""
        def boom(*a, **kw):
            raise AssertionError("the idle tick must touch nothing on the fleet")
        monkeypatch.setattr(npe, "plan", boom)
        monkeypatch.setattr(npe, "resume_units", boom)
        monkeypatch.setattr(npe, "fetch_cluster_status", boom)
        monkeypatch.setattr(util, "run_cmd", boom)
        monkeypatch.setattr(serving, "load_serving", boom)
        return boom

    def test_an_empty_overlay_costs_no_fleet_call_and_no_read(self, monkeypatch,
                                                              no_fleet_at_all):
        reads = []
        real = node_pauses.list_pauses

        def counting(directory=None):
            reads.append(directory)
            return real(directory)
        monkeypatch.setattr(node_pauses, "list_pauses", counting)

        # The production entry point, with NO arguments — exactly what the
        # daemon's stream thread calls, so this is the wiring and the default
        # root in one shot.
        assert npe.check_node_pause_expiry() is True
        assert reads == [None], (
            f"the idle pass read the overlay {len(reads)} time(s); one read is "
            "the contract, zero fleet calls is the point")

    def test_the_idle_tick_still_stamps_its_stream(self, tmp_path, fleet):
        """A stream that is deliberately idle must not read as a dead one."""
        state_dir = tmp_path / "state"
        npe.expiry_tick(now=NOW, directory=str(tmp_path / "empty-overlay"),
                        state_dir=str(state_dir), echo=Lines(), run=fleet)
        stamped = json.loads(
            (state_dir / f"{npe.STREAM_NAME}.json").read_text())
        assert stamped["ok"] is True
        assert stamped["message"] == "no expired pauses"
        assert stamped["resumed"] == []

    def test_the_stream_name_is_the_one_the_loop_and_verify_read(self):
        """One name for the cadence key, the state file and the verify row."""
        assert npe.STREAM_NAME == "node_pause_expiry"
        assert daemon_ops.PERIODIC_INTERVALS[npe.STREAM_NAME] == 60
        from hscc_daemon import verify
        assert verify._live_stream_intervals()[npe.STREAM_NAME] == 60


# ── the span guard: a multi-node unit resumes as one unit or not at all ─────
class TestSpanGuard:
    def test_a_due_pause_spanning_a_still_parked_node_is_skipped(self, fleet,
                                                                 overlay):
        """The half the CLI cannot express, so the tick must.

        ``wspan`` runs on W1 and W2. W1's pause is past due; W2's own pause is
        not. The recorded resume re-issues the WHOLE declared workload, so
        restarting W1 would start units on a node the operator has not given
        back — a half-restart of a multi-node unit.
        """
        seed(overlay, W1, until=at(-1), span_nodes=(W1, W2))
        seed(overlay, W2, until=at(3600), units=["wspan"],
             span_nodes=(W1, W2))
        lines = Lines()
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=lines, run=fleet)
        assert entry["resumed"] == []
        assert entry["skipped"] == [W1], entry
        assert entry["ok"] is False, "a schedule that is not being honoured is red"
        assert fleet.calls == [], "restarting the span would breach W2's pause"
        assert node_pauses.paused(W1, directory=overlay) is not None, \
            "skipping is not resolving — the pause stays and is retried"
        assert node_pauses.paused(W2, directory=overlay) is not None
        assert "another pause" in lines.text

    def test_the_verb_refuses_to_resume_half_a_span(self, fleet, live_root,
                                                   monkeypatch):
        """The same rule, from the operator's side of the seam.

        ``hscc node resume W1 --yes`` re-issues the whole declared workload, so
        before this rule it restarted units onto W2 while W2's own pause was still
        in force — the half-restart a tensor-parallel unit must never see. The
        refusal lives in ``resume_host``, shared with the tick. `--yes` is what
        gets the verb past its own confirmation gate, so this proves the span
        guard is NOT the question — an answer cannot unlock it either.

        The verb brings no clock of its own, so the fixture supplies one through
        :func:`pin_the_fallback_clock` — otherwise the guard's own
        ``is_expired(other, None)`` reads the wall clock, and this case is a fact
        about when pytest ran. It passed for exactly that reason on 2026-11-22:
        the fixture clock had gone past ``at(3600)``, W2 stopped counting as
        held, and the refusal this case demands could no longer fire.
        ``TestFixtureClockIsTheOnlyClock`` is what stops that from being possible
        again.
        """
        pin_the_fallback_clock(monkeypatch)
        seed(live_root, W1, until=at(-1), span_nodes=(W1, W2))
        seed(live_root, W2, until=at(3600), units=["wspan"],
             span_nodes=(W1, W2))
        rc, out = cli("resume", W1, "--yes")
        assert rc == 1, out
        assert "REFUSED" in out and W2 in out, out
        assert fleet.calls == [], "a refused span must not run anything"
        assert node_pauses.paused(W1, directory=live_root) is not None
        assert node_pauses.paused(W2, directory=live_root) is not None

    def test_the_rule_is_in_the_policy_not_only_in_the_tick(self, fleet,
                                                           overlay):
        """Bypass the tick's pre-skip and the policy still refuses.

        ``auto_resume_expired`` skips these hosts before it ever calls
        ``resume_host``, so without this case the guard underneath the skip would be
        dead code that a green suite happily keeps green. Calling the policy
        directly is how any third caller — a plugin tool, a future verb — meets it.

        ``now=NOW`` is the clock this case is judged by, and it is not decoration:
        the span guard asks ``is_expired(other, now)``, and an omitted ``now``
        falls through to the wall clock (``node_pauses._utc(None)``), which made
        W2's ``at(3600)`` look released on any day past the fixture. The pin class
        near the top of this file reddens if this argument is dropped.
        """
        seed(overlay, W1, until=at(-1), span_nodes=(W1, W2))
        seed(overlay, W2, until=at(3600), units=["wspan"], span_nodes=(W1, W2))
        entry = node_pauses.paused(W1, directory=overlay)
        # `now` is named, not defaulted, for the same reason #616 anchors _T0: a
        # caller that reads the wall clock makes a fixture judgement about WHEN the
        # suite ran. resume_host takes now= for exactly this (node_pause_exec.py).
        res = npe.resume_host(W1, entry, now=NOW, apply=True, directory=overlay,
                              echo=Lines(), run=fleet)
        assert res["action"] == npe.RESUME_KEPT and res["ok"] is False, res
        assert fleet.calls == []
        assert node_pauses.paused(W1, directory=overlay) is not None

    def test_two_due_pauses_sharing_a_span_both_resume(self, fleet, overlay):
        """Two pauses that are both past due are not each other's blockers."""
        seed(overlay, W1, until=at(-60), span_nodes=(W1, W2))
        seed(overlay, W2, until=at(-30), units=["wspan"],
             span_nodes=(W1, W2))
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                                echo=Lines(), run=fleet)
        assert entry["resumed"] == [W1, W2], entry
        assert entry["skipped"] == []
        assert node_pauses.paused(W1, directory=overlay) is None
        assert node_pauses.paused(W2, directory=overlay) is None

    def test_the_pass_reads_the_overlay_once_even_with_two_due_hosts(
            self, fleet, overlay, monkeypatch):
        reads = []
        real = node_pauses.list_pauses

        def counting(directory=None):
            reads.append(directory)
            return real(directory)
        monkeypatch.setattr(node_pauses, "list_pauses", counting)
        seed(overlay, W1, until=at(-60))
        seed(overlay, W3, until=at(-30), units=[], span_nodes=[])
        npe.expiry_tick(now=NOW, directory=overlay, write=Stamps(),
                        echo=Lines(), run=fleet)
        assert reads == [overlay], (
            f"{len(reads)} overlay reads: expiry and the span decision must "
            "come from ONE snapshot — load() re-normalises into fresh objects, "
            "so a second read is a different world")


# ── the tick's own contract ─────────────────────────────────────────────────
class TestTickContract:
    def test_every_path_stamps_the_stream_including_the_raising_one(self,
                                                                   overlay):
        stamps = Stamps()
        npe.expiry_tick(now=NOW, directory="/nonexistent-overlay-dir",
                        write=stamps, echo=Lines())
        assert stamps.stamped[-1][0] == npe.STREAM_NAME
        assert stamps.stamped[-1][1]["ok"] is True

        seed(overlay, until=at(-1))

        def boom(host, entry, **kw):
            raise RuntimeError("serving exploded")
        stamps2 = Stamps()
        entry = npe.expiry_tick(now=NOW, directory=overlay, write=stamps2,
                                echo=Lines(), resume=boom)
        assert entry["ok"] is False
        assert "serving exploded" in entry["message"]
        assert stamps2.stamped[-1][1]["ok"] is False, \
            "a failing tick must stamp, or it looks like a stalled stream"

    def test_the_check_wrapper_reports_the_entry_verdict(self, monkeypatch):
        monkeypatch.setattr(npe, "expiry_tick",
                            lambda **kw: {"ok": False, "message": "x"})
        assert npe.check_node_pause_expiry() is False
        monkeypatch.setattr(npe, "expiry_tick",
                            lambda **kw: {"ok": True, "message": "x"})
        assert npe.check_node_pause_expiry() is True

    def test_a_raising_wrapper_never_escapes_the_stream_thread(self,
                                                               monkeypatch):
        def boom(**kw):
            raise RuntimeError("tick died")
        monkeypatch.setattr(npe, "expiry_tick", boom)
        assert npe.check_node_pause_expiry() is False

    def test_a_dry_run_pass_restarts_and_clears_nothing(self, fleet, overlay):
        """The #478 default is reachable from the tick too, not just the verb."""
        earn(W1, 1)
        seed(overlay, until=at(-1))
        report = npe.auto_resume_expired(now=NOW, directory=overlay,
                                         apply=False, echo=Lines(), run=fleet)
        assert report["ok"] is True and report["resumed"] == []
        assert report["attempted"] == 1
        assert fleet.calls == [], "a dry run must not restart anything"
        assert node_pauses.paused(W1, directory=overlay) is not None
        assert heal_breaker.strike_count(W1) == 1

    def test_a_backlog_drains_by_the_per_tick_limit(self, fleet, overlay):
        """Each resume is a 120 s-budget control call in this stream's own
        thread, so a big backlog must not gate the stream for an hour. The rest
        is deferred visibly, longest-waiting first, and drains next tick.

        The pauses carry no units on purpose: what is under test is which hosts
        a pass takes, not what a restart runs (``TestExpiredResumesAndClears``
        covers the restart path).
        """
        hosts = [W1, W2, W3, W4, W5, W6]
        for i, host in enumerate(hosts):
            seed(overlay, host, until=at(-100 + i), units=[],
                 span_nodes=(host,))
        report = npe.auto_resume_expired(now=NOW, directory=overlay,
                                         echo=Lines(), run=fleet, limit=2)
        assert report["resumed"] == [W1, W2], report
        assert report["deferred"] == [W3, W4, W5, W6], report
        assert report["ok"] is True, "deferring is healthy, not a failure"
        assert "deferred" in report["message"]
        assert fleet.calls == []
        # ...and the next tick takes the next two off the same file.
        again = npe.auto_resume_expired(now=NOW, directory=overlay,
                                        echo=Lines(), run=fleet, limit=2)
        assert again["resumed"] == [W3, W4], again

    def test_the_stamp_carries_the_clock_the_pass_judged_against(
            self, fleet, overlay):
        stamps = Stamps()
        npe.expiry_tick(now=NOW, directory=overlay, write=stamps,
                        echo=Lines(), run=fleet)
        assert stamps.stamped[-1][1]["checked_at"] == NOW

    def test_the_stamp_is_plain_json_because_verify_reads_the_file(
            self, fleet, overlay):
        """A stamp that cannot be serialised is a stream with no state file.

        This is why the tick flattens ``kept``/``skipped`` to host names instead
        of passing the pass's ``(host, why)`` pairs through: the pass reports to
        a human, the stamp reports to ``hscc verify``.
        """
        seed(overlay, until=at(-1))
        seed(overlay, W3, until=at(3600), units=[], span_nodes=[])
        stamps = Stamps()
        npe.expiry_tick(now=NOW, directory=overlay, write=stamps,
                        echo=Lines(), run=fleet)
        blob = json.dumps(stamps.stamped[-1][1])
        assert json.loads(blob)["resumed"] == [W1]


# ── wiring: the stream really runs in the loop (daemon_ops) ─────────────────
class TestStreamIsWiredIntoTheLoop:
    """The tick body is only a feature once ``run_daemon_loop`` starts a thread
    for it. Same harness ``test_daemon_ops`` uses for the other streams: boot
    the loop with the tick bodies stubbed and watch which streams get supervised.
    """

    def _run_loop_capturing(self, monkeypatch):
        from hscc_daemon import daemon_singleton as ds
        seen, logs = [], []

        def fake_supervised(stop_event, check_fn, interval, stream_name):
            seen.append((stream_name, interval, check_fn))
            stop_event.set()

        for path in ("hscc_daemon.autodown.resume_from_restart_defensive",
                     "hscc_daemon.serving.resolve_cluster_config",
                     "hscc_daemon.sparkrun_bin.ensure_on_path",
                     "hscc_daemon.recover.recover_engine_wedge",
                     "hscc_daemon.dispatcher_wedge.recover_dispatcher_wedge",
                     "hscc_daemon.lifecycle.pipeline_watchdog",
                     "hscc_daemon.trigger.trigger_engine"):
            monkeypatch.setattr(path, lambda *a, **k: None)
        monkeypatch.setattr(ds, "hold_daemon_loop_lock_fail_open",
                            lambda **k: (True, ""))
        monkeypatch.setattr(daemon_ops, "log",
                            lambda *a, **k: logs.append(a[0]))
        monkeypatch.setattr(daemon_ops, "run_autodown_loop",
                            lambda ev, **k: None)
        monkeypatch.setattr(daemon_ops, "_run_supervised_periodic",
                            fake_supervised)
        monkeypatch.setattr(daemon_ops, "run_heartbeat_loop",
                            lambda ev, **k: ev.wait(2))
        monkeypatch.setattr(daemon_ops, "write_stopped", lambda: None)
        monkeypatch.setattr(daemon_ops, "prune_dead_files",
                            lambda *a, **k: {"removed_dead": 0,
                                             "pruned_bak": 0})
        daemon_ops.run_daemon_loop()
        return seen

    def test_the_loop_supervises_the_expiry_stream_with_its_check_fn(
            self, monkeypatch):
        seen = self._run_loop_capturing(monkeypatch)
        row = [r for r in seen if r[0] == npe.STREAM_NAME]
        assert len(row) == 1, seen
        assert row[0][1] == daemon_ops.PERIODIC_INTERVALS[npe.STREAM_NAME]
        assert row[0][2] is npe.check_node_pause_expiry, (
            "the CHECKS row must point at the feature's check fn, not a copy")


# ── durable, not in-memory: the shape the card asked for ────────────────────
class TestNoInMemoryTimers:
    """The schedule is a file plus a clock read per tick — nothing else.

    A pause held in a module dict or a ``threading.Timer`` dies with the daemon,
    which is exactly when it matters (crash or upgrade cycles the daemon and the
    watchdog then "heals" the paused unit back onto whatever the node is doing).
    This pin makes the wrong shape unrepresentable rather than merely unfashion-
    able: an in-memory timer needs one of these names, and none of them may
    appear in the module's executable code.
    """

    def test_the_executor_cannot_hold_a_timer(self):
        import io
        import tokenize
        src = inspect.getsource(npe)
        code = " ".join(tok.string for tok in
                        tokenize.generate_tokens(io.StringIO(src).readline)
                        if tok.type not in (tokenize.COMMENT, tokenize.STRING))
        for forbidden in ("threading", "sched", "Timer", "sleep"):
            assert forbidden not in code, (
                f"node_pause_exec references {forbidden!r} — an expiry held in "
                "memory is lost on a daemon restart, which is the bug this "
                "feature exists to avoid")

    def test_the_decision_is_a_pure_function_of_the_file_and_the_clock(
            self, fleet, overlay):
        """Same bytes + same clock ⇒ same decision, however many passes run."""
        seed(overlay, until=at(60))
        first = npe.auto_resume_expired(now=NOW, directory=overlay,
                                        echo=Lines(), run=fleet)
        second = npe.auto_resume_expired(now=NOW, directory=overlay,
                                         echo=Lines(), run=fleet)
        assert first["resumed"] == second["resumed"] == []
        assert fleet.calls == []
        # ...and one hour later, with the same bytes, it is due.
        later = npe.auto_resume_expired(now=at(120), directory=overlay,
                                        echo=Lines(), run=fleet)
        assert later["resumed"] == [W1], later
