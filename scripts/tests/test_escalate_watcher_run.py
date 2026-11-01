"""GAP-B-4 (t_87663869): the escalate runner stamps liveness on EVERY pass.

``hscc-escalate-watcher`` runs as a Hermes cron ``--no-agent`` job; before this
card an action-less tick printed nothing and left no file, so a cron job that
stopped firing looked exactly like a healthy quiet watcher. These tests pin:

  * idle pass -> stamp with idle=True, stdout still empty (hermes cron run
    history treats stdout as the delivery record);
  * actions pass -> stamp with the action count, alert line still printed;
  * a crash -> stamp_failure (ok=False) and the original exception re-raised;
  * stamp plumbing (missing module / refused write / raising stamp) never
    breaks the pass and lands on stderr, never stdout;
  * the runner never writes the operator's real state: the dedup file
    (STATE_PATH) is redirected per test, HOME + STATE_DIR by conftest.py;
  * an end-to-end pass is visible to the REAL cron_watchers verdict — runner
    and doctor meet on one stream name and one canonical file.

The verdict maths (fresh/stale/not-configured) is pinned in
hscc_daemon/tests/test_cron_watchers.py; here it is only the runner's wiring.
"""

import io
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import escalate_watcher_run as r  # noqa: E402


class _FakeCW:
    def __init__(self, ok=True, explode=False):
        self.calls = []
        self.fail_calls = []
        self.ok = ok
        self.explode = explode

    def stamp(self, stream, **fields):
        if self.explode:
            raise RuntimeError("stamp blew up")
        self.calls.append((stream, fields))
        return self.ok

    def stamp_failure(self, stream, error):
        self.fail_calls.append((stream, str(error)))
        return True


@pytest.fixture(autouse=True)
def _dedup_state_in_sandbox(monkeypatch, tmp_path):
    """The runner rewrites its cross-run dedup file every pass. conftest.py
    redirects HOME + the canonical stamp dir; this closes the last live path."""
    monkeypatch.setattr(r, "STATE_PATH", str(tmp_path / "escalated.json"))


@pytest.fixture
def scan(monkeypatch):
    """Stub the real hscc_daemon.escalate_watcher.scan_and_escalate — the
    smallest seam that keeps main() from opening the live kanban DB, without
    faking the whole package (which would also break the stamp import)."""
    import hscc_daemon.escalate_watcher as ew
    box = {"actions": [], "raises": None}

    def fake(*, fail_limit=3, strong_profile=None, _notified=None,
             _notify=None, **kw):
        if box["raises"]:
            raise box["raises"]
        return list(box["actions"])

    monkeypatch.setattr(ew, "scan_and_escalate", fake)
    return box


def _run_main(monkeypatch, cw=None, *, run=None):
    fake = cw if cw is not None else _FakeCW()
    monkeypatch.setattr(r, "_stamp_module", lambda: fake)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = (run or r.main)()
    return rc, out.getvalue(), err.getvalue(), fake


# ── stamps ───────────────────────────────────────────────────────────────────

def test_idle_pass_stamps_and_stays_silent_on_stdout(monkeypatch, scan):
    """The GAP-B-4 core for this watcher: an action-less tick STILL stamps
    (the only thing separating healthy-quiet from dead) and still prints
    nothing (stdout is the cron delivery record)."""
    rc, out, err, fake = _run_main(monkeypatch)
    assert rc == 0 and out == ""
    assert fake.calls == [("escalate_watcher",
                           {"actions": 0, "idle": True, "fail_limit": 3})]


def test_actions_pass_stamps_with_count(monkeypatch, scan):
    scan["actions"] = [{"action": "human", "task": "t_1", "category": "c"}]
    rc, out, err, fake = _run_main(monkeypatch)
    assert rc == 0
    assert fake.calls == [("escalate_watcher",
                           {"actions": 1, "idle": False, "fail_limit": 3})]
    assert "t_1" in out  # the alert line still prints (behaviour unchanged)


def test_refused_stamp_write_warns_on_stderr(monkeypatch, scan):
    rc, out, err, fake = _run_main(monkeypatch, cw=_FakeCW(ok=False))
    assert rc == 0 and out == ""
    assert "stamp write failed" in err


def test_exploding_stamp_never_kills_the_pass(monkeypatch, scan):
    """The stamp is the detector, the pass is the job: a raising stamp costs
    the signal, never the escalation."""
    rc, out, err, fake = _run_main(monkeypatch, cw=_FakeCW(explode=True))
    assert rc == 0 and out == ""
    assert "stamp raised" in err


def test_missing_stamp_module_warned_not_fatal(monkeypatch, scan):
    """No stamp module (a deploy without hscc_daemon) must not break the
    watcher — but the lost detector has to be loud in the run record."""
    monkeypatch.setattr(r, "_stamp_module", lambda: None)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert r.main() == 0
    assert out.getvalue() == ""
    assert "not importable" in err.getvalue()


def test_crash_is_stamped_as_a_failed_pass(monkeypatch, scan):
    """run_with_failure_stamp (the __main__ contract) records 'cron fired and
    the script raised' (ok=False) and re-raises the exception untouched."""
    scan["raises"] = RuntimeError("db exploded")
    fake = _FakeCW()
    monkeypatch.setattr(r, "_stamp_module", lambda: fake)
    with pytest.raises(RuntimeError, match="db exploded"):
        r.run_with_failure_stamp()
    assert fake.fail_calls == [("escalate_watcher", "db exploded")]
    assert fake.calls == []  # the crash path stamps failure only, not ok=True


def test_clean_exit_never_stamps_a_failure(monkeypatch, scan):
    rc, out, err, fake = _run_main(monkeypatch, run=r.run_with_failure_stamp)
    assert rc == 0
    assert fake.fail_calls == []


def test_dedup_state_stays_in_sandbox(monkeypatch, scan, tmp_path):
    rc, *_ = _run_main(monkeypatch)
    assert rc == 0
    assert (tmp_path / "escalated.json").is_file()


def test_stamp_module_resolves_the_sibling_file(monkeypatch):
    """_stamp_module must find hscc_daemon/cron_watchers.py from a bare repo
    checkout: force the package import to fail and take the by-path fallback.
    (The installed runtime covers the other homes.)"""
    monkeypatch.setitem(sys.modules, "hscc_daemon", None)
    cw = r._stamp_module()
    assert cw is not None and cw.CHECK_NAME.startswith("cron watchers")


def test_pass_is_visible_to_the_real_doctor_verdict(monkeypatch, scan):
    """End-to-end wiring with NO fake on the stamp path: main() stamps through
    the REAL cron_watchers (loaded by the runner's own production loader, so
    the checkout file is the one under test even where a deployed plugin copy
    shadows the package import), and the real verdict() calls the pass
    healthy. A stream-name drift between runner and doctor would land this on
    'not-configured' — silently reopening GAP-B-4 — so this is the seam that
    pins both sides together. The stamp file must land under the sandbox HOME
    conftest set, never the operator's real ~/.hscc/state."""
    cw = r._stamp_module()
    assert cw is not None, "production loader must reach the checkout module"
    assert cw.__file__ == os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(r.__file__))),
        "hscc_daemon", "cron_watchers.py")
    monkeypatch.setattr(r, "_stamp_module", lambda: cw)
    assert cw.read_stamp("escalate_watcher") is None  # clean sandbox
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        assert r.main() == 0
    stamp = cw.read_stamp("escalate_watcher")
    assert stamp is not None and stamp["idle"] is True
    v = cw.verdict("escalate_watcher")   # live stamp vs live clock
    assert v["status"] == "ok", v
    landed = os.path.join(os.environ["HOME"], ".hscc", "state",
                          "escalate_watcher.json")
    assert os.path.isfile(landed), landed
