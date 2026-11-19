kind: Fixed
task: t_e9dceb2d

- **`hscc --help` now names every dispatched top-level command.**
  `merge-queue` was live — dispatched at `main()`, present in
  `COMMAND_HELP`, `hscc help merge-queue` exited 0 — yet absent from the
  primary help surface. `heal-clear`, `heal-status` and `start-daemon` were in
  the same blind spot. All four are listed on `hscc --help`, and
  `hscc_daemon/tests/test_unknown_command_hint.py` now pins every
  `TOP_LEVEL_COMMANDS` entry against the rendered surface.
- **The cron watchers stamp through the checkout's `cron_watchers.py`, never a
  deployed `~/.hermes/plugins` copy.** `_stamp_module` imported
  `hscc_daemon.cron_watchers` first, so on a live install an older deployed
  package shadowed the tree under test and the end-to-end stamp test loaded
  `~/.hermes/plugins/hscc_daemon/cron_watchers.py` instead of the checkout
  file. Both `scripts/dep_pr_watcher.py` and
  `scripts/escalate_watcher_run.py` now load the sibling file by path first;
  the deployed path is only the fallback when the script itself runs from
  `~/.hermes/scripts`.

---
kind: Verified
task: t_e9dceb2d

- `hscc --help` in a fresh temporary HOME lists `merge-queue`, `heal-clear`,
  `heal-status` and `start-daemon`; `hscc help merge-queue` and
  `hscc merge-queue --help` both exit 0.
- `scripts/tests/test_escalate_watcher_run.py` (the end-to-end doctor-verdict
  test included) and `scripts/tests/test_dep_pr_watcher.py` are green on this
  live-install host; the stamp module resolves
  `<checkout>/hscc_daemon/cron_watchers.py`, not `~/.hermes/plugins`.
- `hscc_daemon/tests/test_unknown_command_hint.py`,
  `test_toplevel_cli_help_f5.py`, `test_unified_cli.py` and
  `scripts/tests/test_site_landing.py` are green; `site/index.html` was
  regenerated from the repo's own help and `site/COPY.md` gained the four
  missing command bullets.
- `python3 scripts/address_guard.py --tracked` is clean.
