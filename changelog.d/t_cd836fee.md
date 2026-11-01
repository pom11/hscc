kind: Fixed
task: t_cd836fee

- **`hscc kanban monitor` fleet caps no longer depend on call order (GAP-D-4).**
  `_read_caps()` did a bare `from hermes_cli.config import load_config` inside a
  `except Exception` that returned `(None, None)`. `hermes_cli` lives under the
  Hermes agent tree and is not on the daemon's `sys.path`, so until
  `autodown._load_kanban_db_or_default()` had run, the import raised
  `ModuleNotFoundError` and the broad handler turned it into "no caps
  configured". Measured under the brew keg venv: `_read_caps()` first returned
  `(None, None)`, after the loader `(12, 3)`. `collect()` happens to call the
  loader first, so today's output is right — the hazard was latent. Caps are now
  resolved through an explicit `_load_hermes_config()` that puts the agent tree
  on `sys.path` itself (same `HERMES_AGENT_PATH` override autodown honours), and
  a genuine read failure is recorded in `_CAPS_READ` + logged ONCE as WARN
  (`kanban monitor: fleet caps unreadable: …`), so blank caps from a broken
  config are distinguishable from blank caps because the operator set no cap.
  hermes' own fail-open `FailedConfigRead` fallback is flagged the same way.
  Caps values and bootstrap behaviour are unchanged.

kind: Verified
task: t_cd836fee

- New `hscc_daemon/tests/test_kanban_live_caps.py` (9 tests) is non-vacuous: a
  `sys.meta_path` gate lets `hermes_cli` resolve ONLY through `sys.path`, so the
  pre-fix `_read_caps` body fails 5 of them (mutation probe: `(None, None) !=
  (17, 5)`) while the fix is green. Planted caps 17/5 differ from this machine's
  real 12/3, so reading the operator's config cannot satisfy them.
