"""`hscc api stop` when launchctl could not be asked AT ALL (t_cf8416dd).

`_handle_stop` has two "nothing to stop" rows, and they make DIFFERENT claims
about the supervisor:

    if not pid:
        if sup.get("available"):
            console.print("HSCC API is not running "
                          "(launchd: unit not loaded, nothing to stop)")
        else:
            console.print("HSCC API is not running")

The second row — the plain one — is what a host with no `launchctl` at all (Linux,
a container, a stripped image) may print: `launchd_status` read rc `None` and
returned `available: False`, which means "the supervisor could not be asked", not
"the unit is not loaded". The first row may ONLY print when launchd actually
answered, because it states a launchd verdict.

Nothing in the suite used to tell the two apart, so collapsing the
`if sup.get("available")` guard — or reordering the branches — was invisible, and
the result is a false verdict of exactly the class t_8bf34130 exists to kill: a
host that never asked launchd reporting "launchd: unit not loaded, nothing to
stop". A `refused` answer is fenced (that is t_8bf34130's file); an UNASKABLE one
is a different answer and needs its own pin.

The three cases, and why each is here:

  1. `test_stop_unaskable_launchd_prints_the_plain_row` — THE pin. The verb asks
     launchd, launchctl cannot be execed, no pid file → the plain row, and NO
     launchd verdict anywhere in the output.
  2. `test_stop_launchd_answered_no_unit_says_which` — the control half. Without
     it, case 1 could be satisfied by a verb that never mentions launchd at all
     (or by a probe that always answers `available: False`), which is the same
     way "never say not running" is satisfiable by a probe that always refuses.
     launchd DID answer here, so the verdict row is the true one and must print.
  3. `test_stop_non_darwin_host_never_forks_launchctl` — the other road to
     `available: False`: `_launchd_matters()` refuses to ask on a non-macOS host,
     so the verb must still print the plain row AND must not fork a `launchctl`
     it knows cannot exist.

Every case DECLARES its supervisor posture by injecting `launchd_status(_run_probe
=...)` — never a leg-wide default (the refusal fence in `launchd_status` is the
feature; stubbing it away in conftest would let every case that forgot the seam
pass for the wrong reason, per `hscc-daemon-ambient-seam-stub`). Each pins the
probe's `launchctl print` call count too, so a case cannot pass silently because
the verb stopped consulting the supervisor.

Safety of the canned answers: every one is a READ-SIDE answer. rc `None` parses to
`loaded: False`, so `_stop_launchd` — which execs a real `launchctl bootout`
through the still-unfenced `_launchctl_run` (t_e1c7c925) — is never reached, and
`_launchctl_run` is stubbed here anyway so a mutation that wanders onto the
bootout path is recorded rather than executed. No `launchctl` process is ever
forked, no server started, no port bound, and `API_PID_FILE`/`API_LOG_FILE` are
redirected to `tmp_path` so the operator's real `~/.hscc` is untouched.
"""
import io
import os
from contextlib import redirect_stderr, redirect_stdout

import hscc_daemon.api_cli as api_cli_mod


#: What `_launchctl_run` returns when the binary cannot be execed at all: rc
#: `None` plus the fault text (`FileNotFoundError` -> "command not found:
#: <argv0>"). `launchd_status` must read this as "the supervisor could not be
#: asked" — `available: False`, and NOT a statement about the unit. Kept as data
#: so the posture is readable rather than buried in a lambda.
LAUNCHCTL_UNASKABLE = (None, "command not found: launchctl")

#: Measured on macOS 26: `launchctl print gui/<uid>/com.hscc.api` for a job that
#: is NOT loaded answers rc 113 with `Bad request.` plus a could-not-find-service
#: line (`_launchctl_run` folds stderr into the returned text). ANY rc!=0 is an
#: ANSWER — available, not loaded — so this is the `available: True` posture the
#: control case needs. Cross-checked against the probe-level pin in
#: test_api_cli_launchd.py::test_probe_not_loaded_is_an_answer_not_a_fault (rc
#: 113, same wording); rc 3 is `bootout`'s already-unloaded answer, not print's.
LAUNCHCTL_NO_JOB = (
    113,
    'Bad request.\nCould not find service "com.hscc.api" '
    "in domain for user gui: 501",
)

#: The phrase only the launchd-aware row may contain. Its absence from the plain
#: row is the assertion that separates the two branches; its presence in the
#: control case is what stops the pin from being satisfied by amnesia.
LAUNCHD_VERDICT_PHRASE = "unit not loaded"

#: The verbs may ask launchd exactly once, and only ever the read-only `print`.
#: An empty list means the seam was never consulted (the ambient bug back); a
#: `bootout` entry means the case silently moved onto the supervised path.
PRINT_ONLY = ["print"]


class _Probe:
    """A canned `launchctl` runner. Records every argv it was asked to run."""

    def __init__(self, rc, out):
        self.rc = rc
        self.out = out
        self.calls = []

    def __call__(self, argv, timeout=10):
        self.calls.append([str(a) for a in argv])
        return self.rc, self.out


def _declare_supervisor(monkeypatch, tmp_path, probe):
    """Route the verb's supervisor question through `probe`, via the REAL parser.

    Pins every input the supervisor question can turn on, so the case depicts a
    posture instead of inheriting the host's:

      * `sys.platform` — `_launchd_matters()`'s first test. Pinned by the caller
        so the case answers the same way on the macOS workstation and on the
        Linux runner;
      * `HSCC_LAUNCHD_PROBE` — the force-the-probe-on hook. Unset here, because
        an operator env carrying it would flip a "must not even ask" case into an
        asking one;
      * `api_launchd.API_PLIST_PATH` — a unit file on disk is what makes asking
        launchd worth a fork; pinning it keeps the case out of the host's real
        `~/Library/LaunchAgents`.

    The probe goes in as `_run_probe`, NOT as a canned dict: the thing under test
    is what `launchd_status` makes of the runner's answer (`rc None` ->
    `available: False`) as much as what `_handle_stop` makes of that. The original
    `launchd_status` is captured BEFORE the patch — `lambda **kw:
    api_cli_mod.launchd_status(_run_probe=probe)` self-recurses, because by the
    time the lambda runs the attribute IS the lambda.
    """
    from hscc_daemon import api_launchd

    monkeypatch.delenv("HSCC_LAUNCHD_PROBE", raising=False)
    plist = tmp_path / "com.hscc.api.plist"
    plist.write_text("declared by the case; never read by the verbs\n")
    monkeypatch.setattr(api_launchd, "API_PLIST_PATH", str(plist))
    real = api_cli_mod.launchd_status
    monkeypatch.setattr(api_cli_mod, "launchd_status",
                        lambda **kw: real(_run_probe=probe))
    return probe


def _drive_stop(monkeypatch, tmp_path):
    """Run `hscc api stop`. Returns (stdout, stderr, rc, signals, execs).

    `get_pid` is stubbed so "nothing is running" is a premise of the case rather
    than a property of the machine running the suite, and `API_PID_FILE` is
    redirected to `tmp_path` because the row this path prints ends in
    `write_stopped`, which REMOVES a file — the redirect is what keeps that off
    the operator's real `~/.hscc/api.pid`.

    `os.kill` and `_launchctl_run` are recorded, not removed: on this path neither
    should be reached, so a non-empty recording is the mutation's confession
    rather than a signal sent or a `launchctl` execed on the operator's box.
    """
    pid_file = tmp_path / "api.pid"          # deliberately never written: no pid
    monkeypatch.setattr(api_cli_mod, "API_PID_FILE", str(pid_file))
    monkeypatch.setattr(api_cli_mod, "API_LOG_FILE", str(tmp_path / "api.log"))
    monkeypatch.setattr(api_cli_mod.daemon_ops, "log", lambda *a, **k: None)
    monkeypatch.setattr(api_cli_mod.daemon_ops, "get_pid",
                        lambda pid_file=None: None)
    signals = []
    monkeypatch.setattr(api_cli_mod.os, "kill",
                        lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(api_cli_mod, "_wait_for_pid_gone", lambda pid, **kw: True)
    execs = []

    def _fenced_launchctl_run(argv, timeout=10):
        execs.append([str(a) for a in argv])
        return None, "fenced: this case must not exec a real launchctl"

    monkeypatch.setattr(api_cli_mod, "_launchctl_run", _fenced_launchctl_run)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = api_cli_mod._handle_stop([])
    return out.getvalue(), err.getvalue(), rc, signals, execs


# --------------------------------------------------------------------------- #
# 1. THE PIN: launchctl unaskable -> the plain row, and no launchd verdict
# --------------------------------------------------------------------------- #

def test_stop_unaskable_launchd_prints_the_plain_row(monkeypatch, tmp_path):
    """`available: False` is "could not be asked", and must not read as a verdict.

    Posture: macOS with a unit file on disk (so the verb DOES ask), no pid file,
    and `launchctl` cannot be execed. What the verb is allowed to say is the plain
    row; what it must not say is anything about a launchd unit — it has no launchd
    answer to base that on.
    """
    monkeypatch.setattr(api_cli_mod.sys, "platform", "darwin")
    probe = _declare_supervisor(monkeypatch, tmp_path, _Probe(*LAUNCHCTL_UNASKABLE))

    out, err, rc, signals, execs = _drive_stop(monkeypatch, tmp_path)

    # Nothing to stop is a normal outcome, not an error.
    assert rc == 0
    # (a) the plain row is what printed.
    assert "HSCC API is not running" in out
    # (b) and it is the PLAIN row: no launchd verdict anywhere. This is the
    # assertion that separates the two branches — the launchd-aware row CONTAINS
    # "HSCC API is not running" as a prefix, so the positive assert above is
    # satisfied by the WRONG branch (the same weak-phrase trap that made
    # `assert "not running" in out` useless in test_api_cli_no_ansi.py).
    assert LAUNCHD_VERDICT_PHRASE not in out.lower()
    assert "launchd" not in out.lower()
    # The fence must not have answered instead of the injected seam: a `refused`
    # row takes the fail-closed path (rc 1 + stderr) and would make this case
    # assert nothing about the branch it names.
    assert "refused" not in out.lower() and "refused" not in err.lower()
    assert not err.strip()
    # The seam really was consulted, once, read-only — otherwise this case would
    # pass just as happily on a verb that never asks the supervisor.
    assert [c[1] for c in probe.calls] == PRINT_ONLY
    assert execs == []                       # no `launchctl` was execed
    assert signals == []                     # and nothing was signalled


# --------------------------------------------------------------------------- #
# 2. CONTROL: when launchd DID answer, the verdict row is the true one
# --------------------------------------------------------------------------- #

def test_stop_launchd_answered_no_unit_says_which(monkeypatch, tmp_path):
    """The pin may not be satisfied by amnesia: a real "no such job" answer earns
    the row that says so. Without this case, "never mention launchd" could be met
    by a verb that never asks — or by a probe forced to answer `available: False`
    for every host — and case 1 would stay green while the useful row died.
    """
    monkeypatch.setattr(api_cli_mod.sys, "platform", "darwin")
    probe = _declare_supervisor(monkeypatch, tmp_path, _Probe(*LAUNCHCTL_NO_JOB))

    out, _err, rc, signals, execs = _drive_stop(monkeypatch, tmp_path)

    assert rc == 0
    assert "HSCC API is not running" in out
    assert LAUNCHD_VERDICT_PHRASE in out.lower()   # launchd answered; say which
    assert [c[1] for c in probe.calls] == PRINT_ONLY
    assert execs == []
    assert signals == []


# --------------------------------------------------------------------------- #
# 3. The other road to `available: False`: the host where asking is pointless
# --------------------------------------------------------------------------- #

def test_stop_non_darwin_host_never_forks_launchctl(monkeypatch, tmp_path):
    """Linux/container: `_launchd_matters()` refuses to ask, so the verb must
    still print the plain row — and must not fork a `launchctl` that cannot exist
    there. Pinned BOTH ways: the row, and an empty probe recording. The empty
    recording is what reddens if the gate is ever widened to ask unconditionally,
    which would make every Linux `hscc api stop` pay a doomed fork.
    """
    monkeypatch.setattr(api_cli_mod.sys, "platform", "linux")
    probe = _declare_supervisor(monkeypatch, tmp_path, _Probe(*LAUNCHCTL_UNASKABLE))

    out, err, rc, signals, execs = _drive_stop(monkeypatch, tmp_path)

    assert rc == 0
    assert "HSCC API is not running" in out
    assert LAUNCHD_VERDICT_PHRASE not in out.lower()
    assert "launchd" not in out.lower()
    assert "refused" not in out.lower() and "refused" not in err.lower()
    assert probe.calls == []                 # never even asked
    assert execs == []
    assert signals == []
