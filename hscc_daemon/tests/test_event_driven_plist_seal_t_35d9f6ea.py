"""t_35d9f6ea — the launchd plist path must be sealed for every case, not per-file.

``hscc_daemon/event_driven.py`` computes its three $HOME-rooted constants at
IMPORT time::

    HSCC_DIR  = os.path.expanduser("~/.hscc")
    STATE_DIR = os.path.join(HSCC_DIR, "state")
    PLIST_DIR = os.path.expanduser("~/Library/LaunchAgents")

so the autouse ``_isolate_hscc`` fixture's ``os.path.expanduser`` rewrite —
which only redirects paths under ``~/.hscc`` — cannot reach any of them. Before
this card none of the three was in the fixture's explicit constant list either,
which meant the operator's REAL ``~/Library/LaunchAgents`` was in scope for
every event-driven case that did not patch it by hand. That is not a litter
risk but a DELETION risk:

    LaunchdJobGenerator.uninstall_job
        os.remove(os.path.join(PLIST_DIR, f"{label}.plist"))

runs once per stream in ``PERIODIC_STREAMS`` with no subprocess involved, so it
is invisible to the ``launchctl`` exec fence (t_5fd62226) and to every fork
spy, and it silently de-Registers the operator's periodic cluster checks.

Every case here deliberately patches NO path of its own — that is the point. A
case that patches its own path passes with or without the seal, so it cannot be
the thing that notices the seal vanished. These read the production attributes
and exercise the real removal/write path, and
``test_no_live_hscc_leak.py`` plants the plists in a sandbox HOME to prove the
operator's own directory is never the target.

Nothing in this file creates, reads or removes anything under the real
~/Library/LaunchAgents; the real path is compared as a STRING only.

One case (``TestPostureTmpPathUnderHome``) spawns a CHILD pytest with
``TMPDIR`` inside a sandboxed ``$HOME``, because the seal's own negative arms
must not assume the runner's TMPDIR is outside the home — see that class.
"""

import os

from hscc_daemon import event_driven as ed


def _real_home():
    """The operator's real $HOME.

    ``_isolate_hscc`` replaces ``os.path.expanduser`` but delegates every path
    outside ``~/.hscc`` to the real implementation, so ``expanduser("~")``
    still returns the real home inside a test — which is what lets these cases
    assert "and it is NOT under there". Note the corollary: ``expanduser
    ("~/.hscc")`` is itself redirected, so it must NOT be used as the negative
    reference (it names the seal's own target and would make the assertion
    vacuously true).
    """
    return os.path.expanduser("~")


def _real_launchagents_dir():
    """The operator's real launchd dir, as a string, for negative assertions."""
    return os.path.join(_real_home(), "Library", "LaunchAgents")


class TestSealRedirectsEventDrivenPaths:
    """The seal, read back through the production module, not through a patch."""

    def test_plist_dir_is_not_the_operators_launchagents(self, tmp_path):
        assert ed.PLIST_DIR == str(tmp_path / "Library" / "LaunchAgents"), (
            "event_driven.PLIST_DIR is not the per-test tmp launchd dir — the "
            "autouse seal stopped covering this module, so any case that "
            "forgets to patch is now removing the operator's real "
            "com.nousresearch.hscc-periodic.*.plist files")
        # Exact-path comparison, not a substring: the seal target is whatever
        # TMPDIR the runner picked, and asserting against $HOME textually would
        # flake on a runner whose TMPDIR happens to sit under $HOME.
        assert ed.PLIST_DIR != _real_launchagents_dir()

    def test_sealed_plist_dir_has_never_been_written(self, tmp_path):
        """The seal target is a fresh path: importing the module and running
        the suite created nothing in it. A pre-existing dir here would mean
        some code path makedirs()d the launchd dir during the run.

        Scope, stated so nobody over-counts it: this is a LITTER arm, not a
        second proof of the redirect. An unsealed-but-untouched path passes it
        too (measured: it stayed green under the mutation that dropped the
        PLIST_DIR seal entry). The equality arms above, and the sandboxed
        before/after diff in test_no_live_hscc_leak.py, are what carry the
        redirect claim."""
        assert not os.path.exists(ed.PLIST_DIR)

    def test_hscc_and_state_dirs_are_sealed(self, tmp_path):
        assert ed.HSCC_DIR == str(tmp_path / "hscc")
        assert ed.STATE_DIR == str(tmp_path / "hscc" / "state")
        real_hscc = os.path.join(_real_home(), ".hscc")
        assert ed.HSCC_DIR != real_hscc
        assert ed.STATE_DIR != os.path.join(real_hscc, "state")


class TestPostureTmpPathUnderHome:
    """The seal must hold on the posture the leg usually does NOT run.

    pytest builds ``tmp_path`` under TMPDIR, and TMPDIR may legitimately sit
    INSIDE ``$HOME``: Hermes exports TMPDIR/TMP/TEMP at its profile scratch dir
    (``hermes_constants.apply_scratch_tmp_env``, called from hermes_bootstrap),
    which lives under the home, and a clean-env spawn (cron, CI, ssh without
    TMPDIR) has no temp var at all. In that posture the seal's target contains
    the ``$HOME`` string as a substring, so a negative arm written as
    ``_real_home not in PLIST_DIR`` errors — and because ``_isolate_hscc`` is
    autouse, it errors on EVERY case in the package, not just this file
    (measured at head 1f15862b: 5 errors in 0.07s, whole-package blast radius).

    A child run is the only way to cover this from inside the suite: the parent
    runner's TMPDIR is whatever the operator/CI happens to have set, so the
    posture cannot be chosen by a case. It costs one interpreter start and makes
    a reintroduced substring arm go red on the leg instead of surfacing weeks
    later on a clean-env spawn.

    ``HOME`` is a fresh empty sandbox, so the child can reach neither the
    operator's ``~/.hscc`` nor the operator's ``~/Library/LaunchAgents`` — this
    case is safety-positive, not just a posture probe.

    The child selects only the other classes (``-k``) — otherwise it would
    re-enter this case and recurse.
    """

    def test_seal_pin_survives_tmp_path_under_home(self, tmp_path):
        import re
        import subprocess
        import sys

        home = os.path.join(str(tmp_path), "homeB")
        tmpdir = os.path.join(home, "tmp")   # <-- TMPDIR INSIDE HOME
        os.makedirs(tmpdir, exist_ok=True)

        tests_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(tests_dir))
        env = dict(os.environ)
        env["HOME"] = home
        for key in ("TMPDIR", "TMP", "TEMP"):
            env[key] = tmpdir
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", __file__,
             "-k", "not TestPostureTmpPathUnderHome",
             "-p", "no:cacheprovider"],
            env=env, cwd=tests_dir, capture_output=True, text=True,
            timeout=300,
        )
        out = proc.stdout + proc.stderr
        assert proc.returncode == 0, (
            "the seal pin does NOT survive a runner whose TMPDIR sits under "
            f"$HOME (rc={proc.returncode}; a child error here means an "
            "autouse-fixture assert depends on the TMPDIR-vs-HOME text):"
            f"\n{out}")
        # The child must really have run the seal cases — rc 0 with nothing
        # collected (pytest rc 5) would be a silent pass.
        counts = {word: int(n)
                  for n, word in re.findall(r"(\d+) (passed|failed|error)",
                                            out)}
        assert counts.get("passed", 0) >= 5, (
            f"child did not run the seal cases: {out}")
        assert not counts.get("failed") and not counts.get("error"), out

        # Posture actually achieved: the child's tmp root was under its HOME,
        # so the substring arm would have fired there. Asserted from the
        # sandbox we built, never by reading the operator's home.
        assert os.path.commonpath([home, tmpdir]) == home


class TestTheRemovalPathLandsInsideTheSeal:
    """The seal must carry the code that actually deletes, not just the string.

    These drive the real ``os.remove`` / write path (with ``subprocess.run``
    faked, because this card is about the FILESYSTEM seal and the exec side is
    fenced separately, in production, per t_5fd62226). They prove the case is
    not vacuous: the files really are there before and really gone after.
    """

    def _gen(self):
        hscc_py = os.path.join(os.path.dirname(os.path.abspath(ed.__file__)),
                               "hscc.py")
        return ed.LaunchdJobGenerator(hscc_script=hscc_py)

    def _plant_all_periodic_plists(self):
        os.makedirs(ed.PLIST_DIR, exist_ok=True)
        planted = []
        for stream in ed.PERIODIC_STREAMS:
            path = os.path.join(
                ed.PLIST_DIR,
                f"{ed.PERIODIC_LABEL_PREFIX}.{stream}.plist")
            with open(path, "w") as f:
                f.write("<plist>planted</plist>\n")
            planted.append(path)
        return planted

    def test_uninstall_all_periodic_removes_exactly_the_sealed_plants(
            self, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        planted = self._plant_all_periodic_plists()
        assert len(planted) == len(ed.PERIODIC_STREAMS)
        assert all(os.path.exists(p) for p in planted)

        results = self._gen().uninstall_all_periodic()

        assert set(results) == set(ed.PERIODIC_STREAMS)
        assert all(ok for ok, _msg in results.values()), results
        for path in planted:
            assert not os.path.exists(path), (
                f"uninstall_job did not remove its plant: {path}")
        # Nothing landed outside the seal: the whole launchd dir under test is
        # empty again, and it is inside this test's tmp dir.
        assert os.listdir(ed.PLIST_DIR) == []
        assert ed.PLIST_DIR != _real_launchagents_dir()

    def test_install_job_writes_inside_the_seal_only(self, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        ok, msg = self._gen().install_job("dgx", 5)
        assert ok, msg
        written = os.path.join(
            ed.PLIST_DIR, f"{ed.PERIODIC_LABEL_PREFIX}.dgx.plist")
        assert written == msg.split("Installed: ", 1)[1], msg
        assert os.path.exists(written)
        assert os.path.dirname(written) == ed.PLIST_DIR
        assert ed.PLIST_DIR != _real_launchagents_dir()
        # The plist content carries the plist dir's own label — the point is
        # only that the write target was the sealed path.
        with open(written) as f:
            assert ("<string>%s.dgx</string>"
                    % ed.PERIODIC_LABEL_PREFIX) in f.read()
