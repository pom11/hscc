kind: Fixed
task: t_ba14f77f

- **A profile-scoped home no longer lets the ROOT plugin tree answer for it, so
  the `doctor` budget-hook row cannot read green over the hook the running worker
  actually lost.** `_budget_plugin_dirs()` contributed BOTH `<root>/profiles/<name>/plugins`
  AND `<root>/plugins` for a profile-scoped home, and
  `hscc-cluster/budget_watch.py::doctor_check` returns ok=True when ANY candidate
  dir is wired. Measured at the base commit: with the profile copy's `register()`
  stripped of the two budget hooks and the root copy intact, the row returned
  `ok=True` quoting `<root>/.hermes/plugins` — the deploy a profile worker never
  loads. Hermes' user-plugin scan is `get_hermes_home() / "plugins"`
  (`hermes_cli/plugins_discovery.py:184`) and under a profile-scoped `HERMES_HOME`
  that IS the profile dir, with **no fallback** to the root's; verified on this
  host against the real CLI (`HERMES_PLUGINS_DEBUG=1` parses exactly one
  `hscc-cluster` manifest, from the profile dir). The root copy
  `install_payload.install_payload_profiles` writes therefore cannot be
  authoritative for a profile, and letting it vote is what hid the dead hook —
  the same failure class the row exists to catch (a lost `[budget]` note nobody
  notices). Decision (strict, per the card): **the profile's own `plugins` dir is
  the ONLY authoritative tree for a profile-scoped home**; only a root-scoped home
  may be answered by `<root>/plugins`, which it still reaches via the ordinary
  `home/plugins` branch. The installer's write-both symmetry is untouched — only
  who may answer for a given home changed. The unwired verdict now also names the
  dirs it inspected (the green verdict already named its winner; a red row that
  names nothing cannot be acted on, and a profile-vs-root mixup is invisible
  otherwise).
---
kind: Verified
task: t_ba14f77f

- Canary `test_wired_root_deploy_does_not_rescue_an_unwired_profile_deploy`
  seeds the profile copy unwired + the root copy wired and asserts red naming the
  profile dir: **measured RED at the base commit** (`ok=True`, detail quoting the
  root's plugins dir) and green after the fix. Resolution tests, per the chosen
  semantics:
  `test_profile_scoped_home_contributes_only_the_profile_plugins_dir`
  (`_budget_plugin_dirs(<root>/profiles/<name>) == [<profile>/plugins]` exactly —
  replaces `test_profile_scoped_home_also_contributes_the_root_plugins`, which
  pinned the rescue) and
  `test_root_scoped_home_still_checks_the_root_plugins_dir` (the root-scoped home
  is not blinded). Mutation probes, each reverted after: restore the old
  `profiles` rule -> exactly those 2 tests fail (16 passed); drop the dir list
  from the unwired detail -> only the canary fails (17 passed).
  `test_doctor_budget_hook.py` 16 -> 18 passed; `hscc-cluster/tests/test_budget_watch.py`
  27 passed; `scripts/run_tests.sh` hscc-bootstrap and hscc-cluster legs green;
  `scripts/changelog_fragments.py` check and `scripts/address_guard.py` rc 0.
