kind: Added
task: t_bc5a3fee

- **`hscc bootstrap` verb + `--dry-run` + brew-libexec-aware CLI-shim skip.**
  `hscc bootstrap [flags]` locates `bootstrap.sh` relative to the resolved
  source root — repo checkout, deployed `$HERMES_HOME/plugins` copy, or a brew
  libexec tree (`<prefix>/Cellar/hscc/<ver>/libexec/hscc/`, version-stable as
  the shipped tap installs it; a versioned `libexec/hscc-<ver>/` is honoured
  too) — and execs it with every remaining argv token passed through
  (`--yes`, `--dry-run`, `--skip-*`); `HSCC_BOOTSTRAP_SH` overrides the lookup
  outright. `bootstrap.sh --dry-run` prints every planned write (plugin
  payload, CLI shim, watchdog scripts, git hooks, skills, role profiles,
  per-project orchestrators, `~/.hscc` state + serving.json, patches, config
  wiring, SOUL, review feature, trigger rules, daemon) and performs none.
  When the installer runs from a brew libexec root (marker file
  `.hscc-brew-libexec` if a tap writes one, or the `*/Cellar/*` /
  `*/libexec/hscc-*` path shapes — the shipped formula drops no marker, so
  `*/Cellar/*` is what fires on a real keg), the CLI-shim stage is SKIPPED
  with the reason printed — brew already owns `hscc` on PATH, and a shim
  into the Hermes venv would shadow it with a second, brew-invisible entry
  point.
  Non-brew trees keep the previous behaviour byte-for-byte. `hscc_cli`'s own
  `hscc_daemon` lookup gained a brew-libexec leg (ancestor `libexec/hscc` and
  `libexec/hscc-*` dirs, newest first, checked before `~/.hermes/plugins`) so
  a brew-installed `hscc` is self-contained: its venv lives inside
  `libexec/`, where the old sibling/repo candidates never saw the payload.
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
- `hscc_daemon/tests/test_hscc_cli_libexec_resolution.py`: 4 new cases — the
  SHIPPED `hscc_cli/__init__.py` source loaded from fabricated brew layouts
  (libexec/venv site-packages → newest `libexec/hscc-*` payload wins; dirs
  without `hscc_daemon` skipped; deployed sibling-plugins layout unchanged),
  with `hscc_daemon*` purged from `sys.modules` around each probe so "which
  tree won" is measured, not assumed.
- Shipped-tap layout pins (t_0f5616fd, +10 cases): the merged
  `pom11/homebrew-hscc` formula installs at the version-STABLE
  `Cellar/hscc/<ver>/libexec/hscc` and drops no marker, so every tree above
  fabricated a shape `brew install` never produces. New fabricated-keg cases:
  `hscc_cli` resolves the payload through the formula's relative
  `site-packages/plugins -> ../../../../hscc` symlink (a `~/.hermes/plugins`
  decoy must NOT win); `_resolve_bootstrap_script()` finds the installer from
  both real roots — through that symlink, and from a venv site-packages copy
  — with no `hscc-*` dir and no marker, ahead of the `$HERMES_HOME/plugins`
  copy; the shim skip fires from the `*/Cellar/*` shape in dry-run (prints
  the reason) and in real mode (completes, rc 0); plus an e2e
  `hscc bootstrap --dry-run` whose dispatcher is imported through the
  symlink. The stable `libexec/hscc` name is a real resolver fix: removing
  that candidate fails exactly the two new locator cases (mutation-checked).
- `hscc_daemon/tests/test_bootstrap_verb.py` + `hscc-bootstrap/tests/` + the
  scripts help-surface/readme-lint gates green on the final tip via
  `scripts/run_tests.sh` (canonical Hermes venv, frozen tree);
  `python3 scripts/address_guard.py --tracked` rc=0.
