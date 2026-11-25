"""Every way a pack can die, its `/tmp/hscc-legs-*` dir must not survive it.

`t_34c5a3ed` proved the *test* was wrong to judge a global `/tmp` glob, and in
doing so surfaced the real fact this file guards: a pack whose runner dies in
certain ways leaves its leg-log dir behind. The measurement is in
`docs/audits/leg-tmp-leak_t_b94a2b39.md`; the two rows that matter are:

    signal   runner rc   dir left behind   legs orphaned
    TERM     143         no                0
    INT      130         no                0
    HUP      129         no                2
    PIPE     141         sometimes         2
    QUIT     131         YES (3/3)         2
    KILL     137         YES               2

`SIGKILL` is the one death no handler in a shell can beat, so *no* trap can be
the whole answer; the fix that closes it is the bounded sweep at runner start.
The trap family closes the catchable half (QUIT) and stops the untrapped fatal
signals orphaning N pytests on a host that runs the fleet control plane.

Two rules this file holds, both learned the expensive way:

  * **Nothing here globs `/tmp/hscc-legs-*` to decide anything.** Every assertion
    names a path this file created or a path the runner PRINTED in its banner.
    A sibling worker's live pack lives in the same namespace, and t_34c5a3ed
    removed the assumption that a run may judge it.
  * **The sweep is never provoked by lowering the floor.** A fabricated dir is
    backdated past the DEFAULT floor with `os.utime` instead. Lowering the floor
    to 1 s would make this suite delete a sibling's two-second-old pack — the
    exact shared-namespace damage the sweep exists to avoid.
"""

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
RUNNER = SCRIPTS / "run_tests.sh"

#: The ONE literal the runner mktemps under (run_tests.sh:688) — never `$TMPDIR`,
#: by the same argument the sealed-HOME block makes. Tests read it only to build
#: a path they own, never to enumerate other people's.
LEG_ROOT = Path("/tmp")
LEG_PREFIX = "hscc-legs-"

# Real leg names: the runner rejects an unknown leg with exit 64 by design.
LEG_A, LEG_B, LEG_C = "hscc-commands", "sparkrun-hermes", "memori_byodb"
THREE = f"{LEG_A} {LEG_B} {LEG_C}"

#: The knob the sweep reads. Tests use it ONLY to prove the sweep is off when it
#: says off — never to widen it (see the module docstring).
SWEEP_ENV = "HSCC_LEG_SWEEP_MIN_AGE_SECONDS"

#: The merge queue caps one run of this script; a pack can therefore never be
#: alive at an age the default sweep floor exceeds. Pinned below so a future
#: editor cannot quietly make the sweep younger than the longest run it can kill.
QUEUE_CAP_SECONDS = 2400

STUB = """#!/usr/bin/env bash
set -u
tests_arg=""
for a in "$@"; do case "$a" in */tests) tests_arg="$a" ;; esac; done
leg="(none)"
[ -n "$tests_arg" ] && leg="$(basename "$(dirname "$tests_arg")")"
secs=0.2; rc=0
for entry in $STUB_PLAN; do
  name="${entry%%:*}"; rest="${entry#*:}"
  if [ "$name" = "$leg" ]; then secs="${rest%%:*}"; rc="${rest#*:}"; break; fi
done
echo "start $leg $$" >> "$STUB_TRACE"
sleep "$secs"
echo "end $leg $$" >> "$STUB_TRACE"
if [ "$rc" -ne 0 ]; then
  echo "FAILED $leg/tests/test_stub.py::test_breaks - assert 1 == 2"
else
  echo "7 passed in ${secs}s"
fi
exit "$rc"
"""


def _plan(spec):
    return " ".join(f"{leg}:{secs}:{rc}" for leg, secs, rc in spec)


def _alive(pid):
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return st not in ("", "Z")


class Pack:
    """The shipped runner, driven through its own `HSCC_TEST_PY` seam.

    Owns exactly two things a test may clean up: the dirs it caused and the
    process session it started. Nothing it does reaches outside those.
    """

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.stub = tmp_path / "stub_py.sh"
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)
        self.trace = tmp_path / "trace.txt"
        self.trace.write_text("")
        self.log = tmp_path / "run.log"
        self.proc = None
        self.own_dirs = []          # dirs THIS run made (from the banner)
        self.faked = []             # dirs THIS test fabricated

    # ── launching ───────────────────────────────────────────────────────────
    def launch(self, plan, *, parallel=2, legs=THREE, env_extra=None):
        env = dict(os.environ)
        env.update({
            "HSCC_TEST_PY": str(self.stub),
            "STUB_TRACE": str(self.trace),
            "STUB_ARGV": str(self.tmp / "argv.tsv"),
            "STUB_PLAN": plan,
            "HSCC_TEST_LEGS": legs,
            "HSCC_LEG_HEARTBEAT_SECONDS": "0",
        })
        if parallel is None:
            env.pop("HSCC_TEST_PARALLEL", None)
        else:
            env["HSCC_TEST_PARALLEL"] = str(parallel)
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
        env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        if env_extra:
            env.update({k: str(v) for k, v in env_extra.items()})
        fh = open(self.log, "wb")
        self._fh = fh
        self.proc = subprocess.Popen(["bash", str(RUNNER)], stdout=fh,
                                     stderr=subprocess.STDOUT, env=env,
                                     cwd=str(REPO), start_new_session=True)
        return self

    def wait_for_live_legs(self, n=2, timeout=60):
        """Block until the banner names the dir and n legs reported in."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = self.dir()
            if m and len(self.starts()) >= n:
                return m
            time.sleep(0.1)
        raise AssertionError(f"never saw {n} live legs in a pack:\n{self.text()}")

    # ── reading ─────────────────────────────────────────────────────────────
    def text(self):
        try:
            return self.log.read_text()
        except OSError:
            return ""

    def dir(self):
        """The dir THIS run printed in its banner, as a Path or None.

        The runner announces the dir (`leg logs in …`); that announcement is the
        only legitimate way for a test to learn a leg-log path, and t_34c5a3ed
        pins it. It is registered as ours the moment it is seen.
        """
        m = re.search(r"leg logs in (/\S+)", self.text())
        if not m:
            return None
        p = Path(m.group(1))
        if p not in self.own_dirs:
            self.own_dirs.append(p)
        return p

    def starts(self):
        return [l.split()[-1] for l in self.trace.read_text().splitlines()
                if l.startswith("start")]

    def orphaned_legs(self):
        return [int(p) for p in self.starts() if _alive(int(p))]

    # ── killing ─────────────────────────────────────────────────────────────
    def kill(self, signame, timeout=45):
        self.proc.send_signal(getattr(signal, "SIG" + signame))
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=30)

    def rc_as_a_shell_sees_it(self):
        """The number a dispatcher reads: `128+N` for a signal death.

        `Popen.returncode` reports a signal death as `-N`; every real consumer of
        this script is a shell reading `$?`, which reports `128+N`.
        """
        return self.proc.returncode if self.proc.returncode >= 0 else 128 - self.proc.returncode

    # ── cleanup, strictly inside what this test caused ──────────────────────
    def fabricate(self, *, age_s, holder=False, files=("legs.tsv", "0-hscc-commands.log")):
        """A plausible leg-log dir, backdated by `age_s`, owned by this test."""
        d = LEG_ROOT / f"{LEG_PREFIX}{os.urandom(3).hex()}"
        d.mkdir(parents=True)
        for name in files:
            (d / name).write_text("")
        stamp = time.time() - age_s
        os.utime(d, (stamp, stamp))
        self.faked.append(d)
        if holder:
            # A live process holding a file open inside it: what a sibling
            # worker's pack looks like to anything that asks about liveness.
            self._holder = subprocess.Popen(
                ["bash", "-c", f"exec 9>> '{d}/0-hscc-commands.log'; sleep 60"],
                start_new_session=True)
        return d

    def cleanup(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in self.orphaned_legs():
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        holder = getattr(self, "_holder", None)
        if holder is not None and holder.poll() is None:
            try:
                os.killpg(holder.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for d in self.own_dirs + self.faked:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
        try:
            self._fh.close()
        except Exception:
            pass


@pytest.fixture
def pack(tmp_path):
    p = Pack(tmp_path)
    yield p
    p.cleanup()


# ── the leak, pinned as a fact about SIGKILL ────────────────────────────────


def test_sigkill_leaves_the_dir_because_no_handler_can_run(pack):
    """The red-before of this card, kept as a shipped-behaviour pin.

    A SIGKILLed shell runs no code at all, so the dir survives: that is the whole
    reason the fix cannot be a trap. Asserting the leak here is deliberate — if a
    future change ever makes this green by removing the sweep, the sweep tests
    below go red and say why.
    """
    pack.launch(_plan([(LEG_A, 30, 0), (LEG_B, 30, 0), (LEG_C, 30, 0)]))
    d = pack.wait_for_live_legs(2)
    rc = pack.kill("KILL")
    assert rc == -9, f"rc={rc}\n{pack.text()}"
    assert d.exists(), "SIGKILL stopped being uncatchable — re-read this card"


def test_a_sigkill_leaked_dir_is_gone_after_the_next_pack(pack):
    """Candidate 2, end to end: the next pack reclaims the previous one's litter."""
    pack.launch(_plan([(LEG_A, 30, 0), (LEG_B, 30, 0), (LEG_C, 30, 0)]))
    d = pack.wait_for_live_legs(2)
    pack.kill("KILL")
    assert d.exists(), "precondition: the kill leaked"

    # A dir this old cannot belong to a live pack (see the floor pin), so the
    # default sweep is allowed to take it.
    stamp = time.time() - QUEUE_CAP_SECONDS * 4
    os.utime(d, (stamp, stamp))
    second = Pack(pack.tmp)
    try:
        second.launch(_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0), (LEG_C, 0.2, 0)]))
        second.wait_for_live_legs(2)
        assert second.proc.wait(timeout=120) == 0
        assert not d.exists(), "a stale leg-log dir survived the sweep"
        assert re.search(r"^parallel: swept \d+ stale leg-log", second.text(), re.M), \
            f"the sweep ran silently:\n{second.text()}"
    finally:
        second.cleanup()


def test_the_sweep_is_off_when_its_floor_says_off(pack):
    """`0` must disable it — the escape hatch a shared host may need."""
    d = pack.fabricate(age_s=QUEUE_CAP_SECONDS * 4)
    pack.launch(_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0), (LEG_C, 0.2, 0)]),
                env_extra={SWEEP_ENV: 0})
    assert pack.proc.wait(timeout=120) == 0
    assert d.exists(), "the sweep ran with the floor at 0"
    assert "swept" not in pack.text()


# ── the safety property the sweep must never lose ───────────────────────────


def test_a_pack_still_running_is_never_swept(pack):
    """The reason t_34c5a3ed refuses to sweep: a sibling's pack is in this namespace.

    A dir with a live holder and a *fresh* mtime is what a sibling worker's pack
    looks like — including one whose pytest is mid-run and has touched nothing in
    its own dir for minutes. Age is what distinguishes it from litter, so age is
    what the sweep reads, and this is the assertion that keeps it that way.
    """
    d = pack.fabricate(age_s=0, holder=True)
    pack.launch(_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0), (LEG_C, 0.2, 0)]))
    assert pack.proc.wait(timeout=120) == 0
    assert d.exists(), "the sweep deleted a leg-log dir that is still in use"
    assert (d / "0-hscc-commands.log").exists(), "the sweep emptied a live dir"


def test_the_sweep_never_follows_a_symlink(pack):
    """`/tmp` is world-writable and sticky: a planted name must not become a lever."""
    target = pack.tmp / "someone-elses-tree"
    target.mkdir()
    (target / "keep.txt").write_text("x")
    link = LEG_ROOT / f"{LEG_PREFIX}{os.urandom(3).hex()}"
    link.symlink_to(target, target_is_directory=True)
    pack.faked.append(link)
    stamp = time.time() - QUEUE_CAP_SECONDS * 4
    os.utime(link, (stamp, stamp), follow_symlinks=False)

    pack.launch(_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0), (LEG_C, 0.2, 0)]))
    assert pack.proc.wait(timeout=120) == 0
    assert target.exists() and (target / "keep.txt").exists(), \
        "the sweep followed a symlink out of the leg-log namespace"


def test_the_sweep_floor_is_older_than_any_run_the_queue_can_produce():
    """The whole safety argument, as an assertion.

    `mtime` alone decides, and it is only sound while the floor is older than the
    longest pack that can exist. That bound is not a guess: the merge queue caps
    one invocation of this script, so a dir older than the cap is by definition
    not being written. If the floor drops under the cap, delete this test only by
    adding a real liveness check.
    """
    src = RUNNER.read_text()
    m = re.search(rf"{SWEEP_ENV}:-([\d]+)", src)
    assert m, f"{SWEEP_ENV} default not found in {RUNNER}"
    floor = int(m.group(1))
    assert floor >= QUEUE_CAP_SECONDS, (
        f"sweep floor {floor}s is not older than one capped queue run "
        f"({QUEUE_CAP_SECONDS}s): a live pack could be swept")
    assert floor >= QUEUE_CAP_SECONDS * 2, (
        "the floor needs real margin over the queue cap, not one timeout's worth")


def test_the_sweep_is_not_on_the_serial_path(pack):
    """Serial runs make no such dir and gain no line (the t_6c2123f9 contract).

    Recorded as a deliberate scope line, not an oversight: litter is created by
    packed runs, so a serial host never reclaims it. Widening the sweep to the
    serial path would put a new line in the stream the queue parses, which is a
    different decision with its own evidence.
    """
    d = pack.fabricate(age_s=QUEUE_CAP_SECONDS * 4)
    pack.launch(_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0)]), parallel=None)
    assert pack.proc.wait(timeout=120) == 0
    assert "parallel:" not in pack.text(), "the serial stream gained a line"
    assert d.exists(), "the serial path swept a dir it never created"


# ── the catchable half: signals the runner does not trap today ──────────────


@pytest.mark.parametrize("signame,expect_rc", [("QUIT", 131), ("HUP", 129)])
def test_a_fatal_signal_the_runner_does_not_trap_still_cleans_up(pack, signame, expect_rc):
    """QUIT leaked its dir in 3/3 measured trials; QUIT/HUP/PIPE orphaned both legs.

    Each handler must keep the `128+N` code a shell would have reported — every
    consumer of this script reads `$?`, and rc=143 being explainable is a
    standing contract (t_6bb29d46). SIGPIPE is NOT in this table on purpose: it
    measured 0/4 dir leaks, and trapping it is a separate decision (see the
    handler's comment and the card's LEFT list).
    """
    pack.launch(_plan([(LEG_A, 30, 0), (LEG_B, 30, 0), (LEG_C, 30, 0)]))
    d = pack.wait_for_live_legs(2)
    pack.kill(signame)
    assert pack.rc_as_a_shell_sees_it() == expect_rc, \
        f"SIG{signame} changed the code a dispatcher reads: {pack.rc_as_a_shell_sees_it()}"
    assert not d.exists(), f"SIG{signame} left the leg-log dir behind"
    assert pack.orphaned_legs() == [], (
        f"SIG{signame} orphaned the legs that were in flight")


def test_the_term_path_still_reports_what_it_reported_before(pack):
    """Regression guard for the handler refactor: rc 143, named legs, no residue."""
    pack.launch(_plan([(LEG_A, 30, 0), (LEG_B, 30, 0), (LEG_C, 30, 0)]))
    d = pack.wait_for_live_legs(2)
    assert pack.kill("TERM") == 143
    out = pack.text()
    assert "RECEIVED SIGTERM" in out, out
    m = re.search(r"during suite: (.*)$", out, re.M)
    assert m and set(m.group(1).split()) == {LEG_A, LEG_B}, out
    assert not d.exists()
    assert pack.orphaned_legs() == []


def test_the_sweep_line_cannot_join_the_handback_digest(pack):
    """A new line in the pack's stream must not read as a leg failure.

    `merge_queue` builds a red gate's handback from anchored patterns; the sweep
    line is prefixed exactly like the `parallel: done` line for this reason.
    """
    from hscc_daemon import merge_queue as mq
    d = pack.fabricate(age_s=QUEUE_CAP_SECONDS * 4)
    pack.launch(_plan([(LEG_A, 0.2, 0), (LEG_B, 0.2, 0), (LEG_C, 0.2, 0)]))
    assert pack.proc.wait(timeout=120) == 0
    assert d.exists() is False
    lines = pack.text().splitlines()
    assert any(l.startswith("parallel: swept ") for l in lines), pack.text()
    assert not [l for l in mq._failure_lines(pack.text()) if "swept" in l], pack.text()
