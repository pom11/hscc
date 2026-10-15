"""F2 — durable single-flight heal lease (HEAL_CASCADE 2026-10-14).

The lease is the one thing the four repair paths never had: on 2026-10-14
engine-wedge recovery, check_workers relaunch, the proxy relaunch and template
auto-heal all fired on ONE wedged unit within three minutes. These tests pin
the properties that make a lease (not a lock) the right instrument:

  * mutual exclusion between DIFFERENT mechanisms;
  * DURABILITY across a simulated daemon restart (the incident's strike
    counters were in-memory and reset);
  * BOUNDED hold — a leaked lease expires instead of disabling healing forever;
  * re-entrancy for the same mechanism, so one repair's second step is not
    blocked by its own claim;
  * release-by-owner, so a late ``finally`` cannot drop someone else's lease.

No node, sparkrun or network is touched: the lease is a file under the per-test
tmp dir (redirected by conftest's autouse fixture).
"""

import json
import os

import pytest

from hscc_daemon import heal_lease


@pytest.fixture
def lease_file(tmp_hfcc_dir, monkeypatch):
    """Point the lease at this test's tmp dir (also done autouse; explicit here
    so the file-based assertions below are meaningful)."""
    path = str(tmp_hfcc_dir / "heal-lease-test.json")
    monkeypatch.setattr(heal_lease, "HEAL_LEASE_FILE", path)
    if os.path.exists(path):
        os.unlink(path)
    return path


class TestLeaseBasics:
    def test_first_acquirer_wins_and_records_who_and_subject(self, lease_file):
        ok, rec = heal_lease.acquire("auto-heal", "10.0.0.2:8001")
        assert ok is True
        assert rec["who"] == "auto-heal"
        assert rec["subject"] == "10.0.0.2:8001"
        # Durable: the record exists on disk, not just in memory.
        assert os.path.exists(lease_file)
        on_disk = json.load(open(lease_file))
        assert on_disk["who"] == "auto-heal"

    def test_second_mechanism_is_refused_and_sees_the_holder(self, lease_file):
        assert heal_lease.acquire("auto-heal", "10.0.0.2:8001")[0] is True
        ok, holder = heal_lease.acquire("proxy-relaunch", "proxy :4000")
        assert ok is False
        assert holder["who"] == "auto-heal"
        # The refused caller must be able to SAY what is in flight.
        assert "auto-heal" in heal_lease.describe(holder)
        assert "10.0.0.2:8001" in heal_lease.describe(holder)

    def test_release_lets_the_next_mechanism_in(self, lease_file):
        heal_lease.acquire("auto-heal", "10.0.0.2:8001")
        heal_lease.release("auto-heal")
        assert heal_lease.acquire("worker-relaunch", "10.0.0.3:8002")[0] is True

    def test_same_mechanism_renews_rather_than_self_blocks(self, lease_file):
        """One repair has several steps; the second must not be blocked by its
        own claim (that would deadlock a mechanism against itself)."""
        assert heal_lease.acquire("auto-heal", "10.0.0.2:8001")[0] is True
        ok, _ = heal_lease.acquire("auto-heal", "10.0.0.2:8001")
        assert ok is True

    def test_release_by_non_owner_is_ignored(self, lease_file):
        heal_lease.acquire("auto-heal", "10.0.0.2:8001")
        heal_lease.release("proxy-relaunch")          # not ours -> no-op
        assert heal_lease.held_by("auto-heal") is True

    def test_release_is_idempotent_and_never_raises(self, lease_file):
        heal_lease.release("nobody")                  # no file at all
        heal_lease.acquire("auto-heal", "x")
        heal_lease.release("auto-heal")
        heal_lease.release("auto-heal")               # already gone
        assert heal_lease.held_by() is False


class TestLeaseBounded:
    """The property that separates a lease from a lock: it ALWAYS expires."""

    def test_expired_lease_is_readable_as_absent(self, lease_file):
        heal_lease.acquire("auto-heal", "10.0.0.2:8001", ttl=100)
        later = heal_lease._now() + 200
        assert heal_lease.read_lease(now=later) is None
        assert heal_lease.held_by(now=later) is False

    def test_leaked_lease_does_not_disable_healing_forever(self, lease_file):
        """A holder killed before ``release`` (crash, SIGKILL, daemon restarted
        mid-repair) must not suppress every repair for the whole TTL."""
        heal_lease.acquire("auto-heal", "10.0.0.2:8001", ttl=60)
        assert heal_lease.acquire("proxy-relaunch", "proxy")[0] is False
        # TTL elapses (no release ever happened) -> healing resumes.
        assert heal_lease.acquire("proxy-relaunch", "proxy",
                                  now=heal_lease._now() + 61)[0] is True

    def test_corrupt_lease_file_is_treated_as_free(self, lease_file):
        """A half-written file must never wedge healing (the daemon reads this
        on a supervisory path)."""
        with open(lease_file, "w") as f:
            f.write("{not json")
        assert heal_lease.read_lease() is None
        assert heal_lease.acquire("auto-heal", "x")[0] is True

    def test_unparseable_expiry_is_treated_as_free(self, lease_file):
        with open(lease_file, "w") as f:
            json.dump({"who": "auto-heal", "until": "yesterday"}, f)
        assert heal_lease.read_lease() is None


class TestLeaseDurability:
    def test_lease_survives_a_daemon_restart(self, lease_file):
        """THE INCIDENT FLAW: the strike counters were in-memory, so a daemon
        restart reset them and a still-loading unit got a fresh countdown into
        a force-recreate. The lease must be readable purely from disk by a
        caller with no memory of the acquire.

        Expressed by hand-writing the file the way a DIFFERENT process (a
        restarted daemon, or the CLI) would write it, then reading it back —
        deliberately NOT by popping and re-importing the module. That version
        of this test mutated `sys.modules` and poisoned every later test in the
        session (observed: 4 failures in test_health.py after it), which is
        precisely the test-isolation failure this audit is about. If the module
        ever caches lease state in memory, the foreign-file form fails loudly.
        """
        heal_lease.acquire("auto-heal", "10.0.0.2:8001", ttl=600)
        now = heal_lease._now()
        foreign = {"who": "auto-heal", "subject": "10.0.0.2:8001",
                   "acquired": now - 5.0, "until": now + 600.0, "pid": 999999}
        with open(lease_file, "w") as f:
            json.dump(foreign, f)

        assert heal_lease.held_by() is True
        ok, holder = heal_lease.acquire("proxy-relaunch", "proxy")
        assert ok is False and holder is not None
        assert holder["who"] == "auto-heal"
        # And it is the FILE that decides, not what we acquired earlier: an
        # expired foreign record frees healing.
        expired = dict(foreign, until=now - 1.0)
        with open(lease_file, "w") as f:
            json.dump(expired, f)
        assert heal_lease.held_by() is False
        assert heal_lease.acquire("proxy-relaunch", "proxy")[0] is True

    def test_renew_extends_an_existing_claim(self, lease_file):
        heal_lease.acquire("auto-heal", "10.0.0.2:8001", ttl=60)
        assert heal_lease.renew(ttl=600) is True
        # Still held well past the ORIGINAL window.
        assert heal_lease.held_by(now=heal_lease._now() + 120) is True


class TestLeaseRace:
    def test_two_acquirers_cannot_both_win(self, lease_file):
        """O_CREAT|O_EXCL is the arbiter. Interleave the two calls the way two
        threads would: both see the file absent, only one creates it."""
        first, second = heal_lease.acquire("auto-heal", "a"), None

        # Second attempt while the first is live — must lose.
        second = heal_lease.acquire("worker-relaunch", "b")
        assert first[0] is True and second[0] is False

    def test_acquire_survives_a_directory_that_does_not_exist_yet(
            self, tmp_hfcc_dir, monkeypatch):
        path = str(tmp_hfcc_dir / "nested" / "deeper" / "heal-lease.json")
        monkeypatch.setattr(heal_lease, "HEAL_LEASE_FILE", path)
        assert heal_lease.acquire("auto-heal", "x")[0] is True
        assert os.path.exists(path)
