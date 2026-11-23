"""t_569fb349 — ``install_proxy`` must copy sparkrun's gateway log aside BEFORE
the ``sparkrun proxy start`` that truncates it.

WHY (audit ``2026-11-22-proxy-connection-error.md`` §3.4, via t_9713c765):
sparkrun opens the gateway log in mode ``"w"`` on every launch
(``sparkrun/proxy/_supervisor.py::_launch_background``), and its gateway state
dir is the cache dir — GLOBAL, not per-port — so this apply path truncates the
same ``litellm.log`` the daemon's repair path does. It truncates whenever
sparkrun records no live gateway: after a crash, a reboot or a ``proxy stop``,
which is exactly when that file is the only record of why the proxy went down.
The refusal a healthy proxy answers instead (``ProxyAlreadyRunning``) is what
keeps the common case harmless, and it is why the seam sits BEFORE the start
rather than behind a liveness check we would have to duplicate from sparkrun.

Fakes only: ``subprocess.run`` is stubbed so nothing execs sparkrun (this
suite's sparkrun fence must stay at 0 live-CLI resolutions), and the archive
side is pinned by conftest's ``_pin_proxy_log_archive_to_tmp`` unless a test
points ``SRC_OVERRIDE`` at its own tmp file.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import cluster_template as ct                     # noqa: E402
import fleet                                      # noqa: E402
import template_intent as ti                      # noqa: E402


# ── rig ────────────────────────────────────────────────────────────────────

def _family(name="coding", port=4000):
    """A resolved family in the shape the deployed template produces."""
    return ti.ResolvedFamily(name=name, proxy_port=port, units=[
        ti.ResolvedUnit("worker", name, "m.yaml", "M", ["192.0.2.2"], 8000, 1, 1)])


def _result(**over):
    """An archive_before_restart() shaped result (the module never raises)."""
    out = {"ok": True, "action": "absent", "archived": None, "error": None,
           "pruned": []}
    out.update(over)
    return out


class _Archiver:
    """Stand-in for ``hscc_daemon.proxy_log_archive``, reached through the one
    seam the wrapper uses (``fleet._daemon_mod``)."""

    def __init__(self, *results, exc=None):
        self.calls = []
        self._results = list(results)
        self._exc = exc

    def archive_before_restart(self, **kw):
        self.calls.append(kw)
        if self._exc is not None:
            raise self._exc
        return self._results.pop(0) if self._results else _result()


@pytest.fixture
def fake_archiver(monkeypatch):
    """Install a recording archiver and hand it back."""
    def _install(*results, exc=None):
        arch = _Archiver(*results, exc=exc)
        monkeypatch.setattr(fleet, "_daemon_mod", lambda name: arch)
        return arch
    return _install


@pytest.fixture
def fake_start(monkeypatch, tmp_path):
    """Stub ``subprocess.run`` (sparkrun is NEVER exec'd) and record argv."""
    monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
    calls = []

    def _run(argv, **kw):
        calls.append(list(argv))
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _is_proxy_start(argv):
    return argv[:3] == ["sparkrun", "proxy", "start"]


# ── ordering: the copy happens BEFORE the argv that truncates ──────────────

def test_archive_call_precedes_the_proxy_start_argv(monkeypatch, tmp_path):
    """The seam's whole value is ordering: a copy taken after the start is a
    copy of an empty file. Asserted on an event tape, not on timestamps."""
    events = []
    monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")
    monkeypatch.setattr(
        ct, "_archive_proxy_log_before_start",
        lambda reason: events.append(("archive", reason)) or _result())

    def _run(argv, **kw):
        events.append(("start", list(argv)))
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", _run)

    res = ct.install_proxy(_family())

    assert [kind for kind, _ in events] == ["archive", "start"], events
    assert events[0][1] == "proxy start (coding)"
    assert _is_proxy_start(events[1][1])
    assert res["loaded"] is True


def test_wrapper_calls_the_archiver_before_running_the_argv(fake_archiver,
                                                            fake_start, capsys):
    """Same claim through the real wrapper (no stub of it): the archiver is
    entered while the argv has not been built-and-run yet, and the start still
    proceeds with the argv the deployed template needs."""
    arch = fake_archiver(_result())

    res = ct.install_proxy(_family())

    assert len(arch.calls) == 1
    assert len(fake_start) == 1 and _is_proxy_start(fake_start[0])
    assert fake_start[0][fake_start[0].index("--port") + 1] == "4000"
    assert fake_start[0][fake_start[0].index("--cluster") + 1] == "hscc"
    assert res["loaded"] is True
    # Nothing to preserve is the ordinary healthy-proxy case: stay silent.
    assert capsys.readouterr().err == ""


# ── the point of it: a truncating start leaves the outage copy behind ──────

def test_a_truncating_start_leaves_the_outage_copy_behind(tmp_path, monkeypatch,
                                                          fake_start, capsys):
    """Simulated-truncation proof (same rig shape as t_9713c765's).

    The stub answers the start the way sparkrun does — ``open(log, "w")`` — so
    the live file really is emptied. The outage line must still be readable in
    ``~/.hscc/proxy-logs`` (pinned here to tmp), which is the entire purpose.
    """
    from hscc_daemon import proxy_log_archive as pla

    src = tmp_path / "litellm.log"
    src.write_text("litellm: 57 x Connection error for alias orchestrator-model\n")
    dest = tmp_path / "proxy-logs"
    monkeypatch.setattr(pla, "SRC_OVERRIDE", str(src))
    monkeypatch.setattr(pla, "PROXY_LOG_DIR", str(dest))

    def _run(argv, **kw):
        open(str(src), "w").close()      # sparkrun's mode "w" truncation
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", _run)

    res = ct.install_proxy(_family())

    assert res["loaded"] is True
    assert src.read_text() == "", "the simulated truncation did not happen"
    archived = sorted(dest.glob("litellm.log.*.pre-restart.log"))
    assert len(archived) == 1, archived
    assert "Connection error" in archived[0].read_text()
    # And the operator sees what was preserved, on stderr, once.
    err = capsys.readouterr().err
    assert "proxy log archived before proxy start (coding)" in err


# ── a lost log must never cost the apply its proxy ─────────────────────────

def test_failing_archive_warns_and_still_starts(fake_archiver, fake_start, capsys):
    """The archiver returns ok=False (its contract: never raises). The WARN
    names the reason and the start proceeds — loaded stays True."""
    fake_archiver(_result(ok=False, error="OSError: 28 No space left on device"))

    res = ct.install_proxy(_family())

    assert res["loaded"] is True
    assert len(fake_start) == 1 and _is_proxy_start(fake_start[0])
    err = capsys.readouterr().err
    assert "WARN" in err
    assert "No space left on device" in err
    assert "proxy start (coding)" in err


def test_exploding_archiver_warns_and_still_starts(fake_archiver, fake_start,
                                                   capsys):
    """Defence in depth: the wrapper's own except-arm, for anything the
    archiver module raises despite its contract."""
    fake_archiver(exc=RuntimeError("kaboom"))

    res = ct.install_proxy(_family())

    assert res["loaded"] is True
    assert len(fake_start) == 1
    err = capsys.readouterr().err
    assert "WARN" in err and "RuntimeError: kaboom" in err


def test_missing_peer_package_warns_and_still_starts(fake_start, capsys,
                                                     monkeypatch):
    """A standalone hscc-cluster invocation (no importable hscc_daemon) must
    degrade to today's behaviour with one WARN, not break ``apply``."""
    def _boom(name):
        raise ImportError(f"No module named 'hscc_daemon.{name}'")

    monkeypatch.setattr(fleet, "_daemon_mod", _boom)

    res = ct.install_proxy(_family())

    assert res["loaded"] is True
    assert res["via"] == "sparkrun-proxy"
    assert len(fake_start) == 1 and _is_proxy_start(fake_start[0])
    err = capsys.readouterr().err
    assert "WARN" in err and "ImportError" in err


def test_a_failed_start_is_still_reported_as_failed(fake_archiver, monkeypatch,
                                                     tmp_path):
    """The seam must not launder a genuinely failed start into success."""
    fake_archiver(_result())
    monkeypatch.setattr(ct, "PROXY_DIR", tmp_path / "proxies")

    def _run(argv, **kw):
        return MagicMock(returncode=1, stderr="Proxy is already running",
                         stdout="")

    monkeypatch.setattr(subprocess, "run", _run)

    res = ct.install_proxy(_family())

    assert res["loaded"] is False
    assert "already running" in res["error"]


# ── one copy per apply, because sparkrun keeps ONE log for the whole dir ───

def test_multi_family_apply_copies_once_but_starts_every_family(fake_archiver,
                                                                fake_start):
    arch = fake_archiver(_result(), _result())

    ct.install_proxy(_family("coding", 4000))
    ct.install_proxy(_family("vision", 4001))

    assert len(arch.calls) == 1, "one shared gateway log needs one copy per apply"
    assert len(fake_start) == 2, "the guard must not skip a family's start"
    assert {s[s.index("--port") + 1] for s in fake_start} == {"4000", "4001"}

    # The documented test seam puts the guard back for a new apply.
    ct._reset_proxy_log_archive_once()
    ct.install_proxy(_family("coding", 4000))
    assert len(arch.calls) == 2


# ── hermeticity: the suite never reads or writes the operator's state ─────

def test_suite_floor_pins_both_sides_of_the_archiver(tmp_path):
    """conftest's ``_pin_proxy_log_archive_to_tmp`` is the reason ~15 existing
    apply tests may call ``install_proxy`` with only subprocess stubbed. If it
    ever stops applying, this fails instead of the operator's live log being
    read and ``~/.hscc/proxy-logs`` being created during the suite."""
    from hscc_daemon import proxy_log_archive as pla

    assert pla.SRC_OVERRIDE, "read side must be overridden in this suite"
    assert not Path(pla.SRC_OVERRIDE).exists(), pla.SRC_OVERRIDE
    assert str(tmp_path) in str(pla.PROXY_LOG_DIR), pla.PROXY_LOG_DIR


def test_default_posture_creates_nothing_and_prints_nothing(fake_start, capsys,
                                                            tmp_path, monkeypatch):
    """Unstubbed archiver, pinned floor: a real archive_before_restart() runs,
    finds no file, files no archive, and the apply stays quiet.

    The pass-through spy is what makes this more than an absence check: it
    pins that the seam really fired, so the "nothing was created" half means
    "nothing was created BY A COPY THAT RAN", not "the copy never happened".
    """
    from hscc_daemon import proxy_log_archive as pla

    seen = []
    real = pla.archive_before_restart

    def _spy(*a, **kw):
        seen.append(kw)
        return real(*a, **kw)

    monkeypatch.setattr(pla, "archive_before_restart", _spy)

    res = ct.install_proxy(_family())

    assert res["loaded"] is True
    assert len(seen) == 1, "the seam must still fire in the default posture"
    assert not Path(pla.PROXY_LOG_DIR).exists()
    assert capsys.readouterr().err == ""
