"""t_2f09d480 — install.py / api_launchd.py plist paths sealed for every case.

Same shape as ``test_event_driven_plist_seal_t_35d9f6ea.py``: ``install.py``
and ``api_launchd.py`` compute their ``$HOME``-rooted launchd (and systemd)
constants at IMPORT time, so the autouse ``_isolate_hscc`` fixture's
``os.path.expanduser`` rewrite — which only covers ``~/.hscc`` — cannot reach
them. Before this card only per-test patches kept them off the operator's real
``~/Library/LaunchAgents``. That is a write/remove risk, not litter:

    install._install_launchd()      writes   PLIST_FILE
    install._uninstall_launchd()    unlinks  PLIST_FILE
    api_launchd.install_api_plist() writes   API_PLIST_PATH
    api_launchd.uninstall_api_plist() os.remove()s API_PLIST_PATH

with no subprocess involved, so neither the launchctl fence nor a fork spy can
see it.

Every case here deliberately patches NO path of its own — that is the point.
``test_no_live_hscc_leak.py`` runs this file in a sandbox HOME whose launchd
directory already holds the production plist names, so dropping the seal entry
turns that arm red. Nothing in this file creates, reads or removes anything
under the real ``~/Library/LaunchAgents``; the real path is compared as a
string only.
"""

import os

from hscc_daemon import api_launchd, install


def _real_launchagents_dir():
    """The operator's real launchd dir, as a string, for negative assertions.

    ``_isolate_hscc`` delegates every path outside ``~/.hscc`` to the real
    ``expanduser``, so ``~`` still names the real home inside a test. Do NOT
    use ``expanduser("~/.hscc")`` for the negative reference: it is itself
    redirected and would make the assertion vacuous.
    """
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")


class TestSealedPaths:
    def test_install_plist_paths_are_not_the_operators(self, tmp_path):
        sealed = os.path.join(str(tmp_path), "Library", "LaunchAgents")
        assert install.PLIST_DIR == sealed, (
            "install.PLIST_DIR is not the per-test tmp launchd dir — the "
            "autouse seal stopped covering it, so a case that forgets to patch "
            "is writing/removing the operator's real daemon plist")
        assert install.PLIST_FILE == os.path.join(
            sealed, "com.hermes.hscc_daemon.plist")
        assert install.PLIST_DIR != _real_launchagents_dir()

    def test_install_systemd_paths_are_not_the_operators(self, tmp_path):
        sealed = os.path.join(str(tmp_path), ".config", "systemd", "user")
        assert str(install.SYSTEMD_USER_DIR) == sealed
        assert str(install.SYSTEMD_UNIT_FILE) == os.path.join(
            sealed, install.SYSTEMD_UNIT_NAME)
        assert str(install.LEGACY_SYSTEMD_UNIT_FILE) == os.path.join(
            sealed, install.LEGACY_SYSTEMD_UNIT_NAME)
        assert str(install.SYSTEMD_USER_DIR) != os.path.join(
            os.path.expanduser("~"), ".config", "systemd", "user")

    def test_api_plist_path_is_not_the_operators(self, tmp_path):
        sealed = os.path.join(str(tmp_path), "Library", "LaunchAgents",
                              "com.hscc.api.plist")
        assert api_launchd.API_PLIST_PATH == sealed, (
            "api_launchd.API_PLIST_PATH is not the per-test tmp launchd dir — "
            "the autouse seal stopped covering it, so a case that forgets to "
            "patch is writing/removing the operator's real API unit")
        assert api_launchd.API_PLIST_PATH != os.path.join(
            _real_launchagents_dir(), "com.hscc.api.plist")


class TestWriteRemoveLandsInsideTheSeal:
    """Drive the real write/remove paths, with only the exec side faked."""

    def test_install_launchd_writes_and_uninstalls_only_the_sealed_plist(
            self, fake_subprocess):
        fake_subprocess.set_result(returncode=0)
        plist = install.PLIST_FILE
        assert os.path.dirname(plist) == install.PLIST_DIR

        install._install_launchd()
        assert os.path.isfile(plist)

        install._uninstall_launchd()
        assert not os.path.exists(plist)

    def test_api_install_and_uninstall_touch_only_the_sealed_plist(self):
        path = api_launchd.API_PLIST_PATH
        res = api_launchd.install_api_plist(
            "/repo", python_path="/venv/bin/python3")
        assert res["path"] == path and res["changed"] is True
        assert os.path.isfile(path)

        assert api_launchd.uninstall_api_plist() == {
            "path": path, "removed": True}
        assert not os.path.exists(path)
