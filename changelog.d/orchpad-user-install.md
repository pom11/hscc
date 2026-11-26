kind: Fixed

- **The isolated-install test wrote orchpad into the LIVE Hermes venv.**
  `test_bootstrap_isolated.py` (and the main-orchestrator e2e) symlink the
  sandbox's `hermes-agent` to the real tree, so bootstrap's orchpad stage
  pip-installed into the shared venv with console-script shebangs pointing at
  pytest's tmp dir. Once that dir was gone, the live `bin/orchpad` and
  `orchpad-hermes` were dead and the 2.17.8 bootstrap's orchpad stage failed on
  every orchestrator profile. New `--skip-orchpad-package` (installer
  `--no-package`): check the package, never install it; profiles are reported
  `skipped` when none is there. Both real-bootstrap tests pass it, and the
  isolated test now asserts the live venv's `bin/orchpad*` and dist-info
  `RECORD` are byte-identical before and after.
- **A user's own orchpad install is never broken.** orchpad is standalone, so it
  may be installed before hscc. The stage no longer reinstalls the pin over any
  version that is not exactly the pin: a version at or above the pin is left as
  it is (no downgrade), a local/editable checkout (`direct_url.json`
  `dir_info`) is `user-managed` and left alone whatever its version, and only a
  missing or older regular install is installed/upgraded. Standalone installs
  outside the Hermes venv are never touched; `plugins.enabled` keeps every
  existing entry.
---
kind: Verified

- `hscc-bootstrap/tests`: 1567 passed, including the real isolated and
  main-orchestrator bootstraps; an md5 of the live venv's `bin/orchpad`,
  `bin/orchpad-hermes` and the orchpad `RECORD` was identical before and after
  the run. 8 new cases in `test_install_orchpad_t_2d87d94d.py` (newer, editable,
  older, enabled-entries, `--no-package` both ways, flag plumbing, version
  compare).
