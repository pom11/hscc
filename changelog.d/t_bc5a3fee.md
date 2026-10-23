kind: Added
task: t_bc5a3fee

- **`hscc bootstrap` verb + `--dry-run` + brew-libexec-aware CLI-shim skip.**
  `hscc bootstrap [flags]` locates `bootstrap.sh` relative to the resolved
  source root — repo checkout, deployed `$HERMES_HOME/plugins` copy, or a brew
  libexec tree (`<prefix>/libexec/hscc-<ver>/`) — and execs it with every
  remaining argv token passed through (`--yes`, `--dry-run`, `--skip-*`);
  `HSCC_BOOTSTRAP_SH` overrides the lookup outright. `bootstrap.sh --dry-run`
  prints every planned write (plugin payload, CLI shim, watchdog scripts, git
  hooks, skills, role profiles, per-project orchestrators, `~/.hscc` state +
  serving.json, patches, config wiring, SOUL, review feature, trigger rules,
  daemon) and performs none. When the installer runs from a brew libexec root
  (marker file `.hscc-brew-libexec` the tap writes, or the `*/Cellar/*` /
  `*/libexec/hscc-*` path shapes), the CLI-shim stage is SKIPPED with the
  reason printed — brew already owns `hscc` on PATH, and a shim into the
  Hermes venv would shadow it with a second, brew-invisible entry point.
  Non-brew trees keep the previous behaviour byte-for-byte.
---
kind: Verified
order: 1

- `hscc_daemon/tests/test_bootstrap_verb.py`: 20 new cases — locator across
  the three roots (repo sibling, deployed plugins, brew libexec incl. the
  `libexec/venv` site-packages walk-up + newest-version glob), env override,
  argv pass-through, exit-code propagation, exec-bit fallback, and the
  raw-argv `-h`/`--help` intercept that must never exec the installer.
- `hscc-bootstrap/tests/test_bootstrap_dry_run.py`: 8 new cases on fabricated
  trees only — a fake brew prefix under tmp_path with the real venv symlinked
  as a directory and a stub `sparkrun`; snapshot-diff asserts zero writes
  under `HERMES_HOME`/`HSCC_DIR` across dry-run, skip-flag, preflight-failure
  and the e2e `hscc bootstrap --dry-run` whose *running* CLI lives inside
  `libexec/venv/lib/python3.11/site-packages`; plus the real-mode pair:
  brew tree skips the shim and completes, non-brew tree still attempts it.
- `hscc_daemon/tests/test_bootstrap_verb.py` + `hscc-bootstrap/tests/` + the
  scripts help-surface/readme-lint gates green on the final tip via
  `scripts/run_tests.sh` (canonical Hermes venv, frozen tree);
  `python3 scripts/address_guard.py --tracked` rc=0.
