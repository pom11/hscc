kind: Fixed
task: t_3aadd2c0

- **`install_dep_watcher.sh` wrote the poller to a dir the cron job could
  never read.** Hermes resolves a bare `--script NAME` only against the bound
  home's `scripts/` dir (`$HERMES_HOME/scripts`; profiles never share files),
  but the installer hard-coded the shared `~/.hermes/scripts/` while
  `hermes cron create` bound the invoking profile's home. Installing from a
  profile shell therefore produced a job whose script could not be found —
  `hscc-dep-watcher` failed 17 consecutive daily runs under the
  backend-engineer profile. The zombie job was removed with
  `hermes cron remove`; the shared-home job (the installer's real target all
  along) had been running green daily the whole time, so no verification card
  was ever actually owed one: no `needs-cluster-check` PR was open at any run
  boundary while that job was live. The installer now derives
  `HERMES_SCRIPTS` from the same `$HERMES_HOME` the cron CLI binds, and
  `scripts/tests/test_install_dep_watcher.py` pins the invariant
  (install/uninstall resolve to one home; no hard-coded shared path;
  byte-identical copy).
