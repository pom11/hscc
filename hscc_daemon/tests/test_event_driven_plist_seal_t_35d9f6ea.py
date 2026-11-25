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
        some code path makedirs()d the launchd dir during the run."""
        assert not os.path.exists(ed.PLIST_DIR)

    def test_hscc_and_state_dirs_are_sealed(self, tmp_path):
        assert ed.HSCC_DIR == str(tmp_path / "hscc")
        assert ed.STATE_DIR == str(tmp_path / "hscc" / "state")
        real_hscc = os.path.join(_real_home(), ".hscc")
        assert ed.HSCC_DIR != real_hscc
        assert ed.STATE_DIR != os.path.join(real_hscc, "state")


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
