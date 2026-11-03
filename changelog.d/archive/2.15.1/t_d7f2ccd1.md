kind: Fixed
task: t_d7f2ccd1

- **The `doctor` budget-hook row checked the OPERATOR's plugin tree even when
  told which home to check — so a sandbox or `--home` bootstrap could read
  all-green over a broken deploy.** `_budget_plugin_dirs()` appended
  `~/.hermes/plugins` **unconditionally**, after the roots derived from the
  `hermes_home` it was handed, so the candidate list was always
  `[<named home>/plugins, ~/.hermes/plugins]`. Both downstream consumers then
  rescued the named tree with the host's: `_load_budget_watch` takes the first
  root whose `budget_watch.py` imports (a broken or missing module in the named
  tree fell through to the live one) and
  `hscc-cluster/budget_watch.py::doctor_check` calls ANY wired candidate green
  (an unwired named tree was rescued by a wired live one). The row answered
  `wired in ~/.hermes/plugins` about a tree nobody asked about — the same
  HERMES_HOME/plugin-dir resolution-leak class as the `importlib.reload()`
  HOOKS_DIR binding and t_444b1bb6's profile fallback, and the reason three
  `test_doctor_budget_hook` cases failed on any host whose own deploy is wired.
  The default root is now contributed **only when no home was named** (the human
  CLI path, which targets the operator's own deploy); a named home resolves to
  its own roots and nothing else. That tail was redundant in every legitimate
  case anyway — `home == ~/.hermes` dedupes onto it, and a profile-scoped home
  already contributes the root's dir through the `profiles` rule — so it bought
  nothing but the leak. Same fence as the waker / cron-watchers / supervision
  rows, and now honoured by the row that documented it.
---
kind: Verified
task: t_d7f2ccd1

- `hscc-bootstrap/tests/test_doctor_budget_hook.py`: **9 -> 16 cases**, green on
  a host whose live `~/.hermes/plugins` deploy IS wired. Three of the old cases
  (`test_missing_register_hook_lines_are_red_naming_the_fix`,
  `test_import_failure_is_non_fatal_degraded`,
  `test_broken_module_is_non_fatal_degraded`) failed at main @ `e1534088` here
  and passed on a host with an unwired deploy — host-state-dependent, which was
  itself the bug. They now seed a WIRED deploy at the live shape
  (`_live_deploy`) alongside the fixture they name, so the leak cannot rescue
  the verdict regardless of what the dispatching host has deployed; pinning
  `HOME` + clearing `PLUGINS` (`_isolate`) makes the whole file hermetic.
- Two-directional hermeticity pins: `test_named_home_wins_over_a_wired_live_deploy`
  (live wired + named home unwired -> RED, and the live dir must not appear in
  the detail) and `test_wired_named_home_is_green_while_the_live_deploy_is_unwired`
  (mirror image, so the fix cannot pass by always preferring the host);
  `test_no_named_home_still_falls_back_to_the_default_root` keeps the human CLI
  path pointed at `~/.hermes/plugins`.
- `test_plugin_dirs_honour_the_plugins_env_then_the_home` asserted the leak
  (`dirs[-1] == ~/.hermes/plugins` while a home was named) and contradicted its
  own docstring; it now pins exactly the two roots the named home owns.
  `test_plugin_dirs_add_the_default_root_only_without_a_named_home` covers the
  no-home and empty-home CLI shapes.
- Mutation probe: restoring `_budget_plugin_dirs` from HEAD turns **5** of these
  cases red again, and the restored file is byte-identical to the committed one
  afterwards — the new cases fail on the defect, not on the environment.
- Non-pytest probe of the real CLI (`_home_from_argv` -> `run_doctor`): this
  worker's profile home -> green naming the profile plugins dir (the root's
  plugins dir still contributed by the `profiles` rule); no-home -> green naming
  `~/.hermes/plugins`; `--home` at a throwaway tree -> honest `not importable`,
  no longer rescued by the live deploy.
- `scripts/run_tests.sh hscc-bootstrap` green; `scripts/address_guard.py` rc 0.
