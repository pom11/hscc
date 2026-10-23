kind: Fixed
task: t_70d0cffa

- **`proxy_router_settings` would false-warn the moment the top-level
  `router_settings` duplicate is cleaned out of proxy.yaml.** The drift
  comparator read only `proxy_doc["router_settings"]`, but sparkrun 0.4.0
  reads `gateways.litellm.router_settings`
  (`plugins/litellm/engine.py::_configured_router_settings`). It passed on
  the live host only because `~/.config/sparkrun/proxy.yaml` carries BOTH
  keys with identical content: with the nested key alone (the shape a
  rollback cleanup leaves), the check expected NO `router_settings`
  emission while 0.4.0 emits the nested one -> a WARN against a healthy
  proxy. New `router_settings_source()` mirrors sparkrun's own read order
  (nested first, top level only when nested is absent or null, so the
  0.3.10 rollback line stays covered) and both warn texts now name WHICH
  key the intent resolved from. 11 hermetic cases added
  (nested-only / nested-wins-over-differing-top-level / top-level-only /
  non-mapping nested / nested-null fallback / scalar gateways + a direct
  read-order pin), mutation-probed against the old read (7 kills) and
  against a precedence swap (4 kills).
- **Fork-branch assertions were vacuous.** `test_check_runtime_deps.py` and
  `test_dep_pr_watcher.py` asserted the bare substring
  `feat/proxy-least-in-flight`, which the superseded 0.3.10 rollback branch
  name also satisfies — a rule that regressed to the old branch would still
  pass. Both now assert the exact `-040` name and that every mention of the
  branch carries the suffix.
- **Bump runbook re-pinned to the live 0.4.0 topology** (verified
  read-only before adoption): `~/.local/bin/sparkrun` ->
  `.venv-sparkrun-040`, editable checkout `~/dev/sparkrun-live` on
  `feat/proxy-least-in-flight-040` @ `b5c60edd` (one macOS-spawn test fix
  on the `v0.4.0` tag), engine `src/sparkrun/plugins/litellm/engine.py`;
  plus the 0.4.0 notes on the nested read order and why `simple-shuffle`
  is the healthy emitted value. `verify.py` prose re-pinned to the `-040`
  branch. Residual adoption from the superseded PR #53 (`1f0ecba7`); its
  `docs/COMPAT-sparkrun-0.4.0.md` was NOT adopted —
  `docs/audits/SPARKRUN_040_COMPAT_2026-10-19.md` stays the single compat
  record (fact-by-fact comparison: the audits doc is a strict superset).
