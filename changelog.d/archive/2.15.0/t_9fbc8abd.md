kind: Fixed
task: t_9fbc8abd

- **`bootstrap` no longer silently overwrites operator-modified plugin files
  (GAP-E-1, audit E).** `install_payload.py` moved the deployed payload tree
  to `plugins-backups/` and copytree'd over it with `_EXCLUDE` (build
  artifacts) as the only filter — a file patched directly in
  `~/.hermes/plugins/` (the exact scenario audit E flagged for `hscc.py` /
  `kanban_cli.py` / `kanban_blocked.py`) was destroyed with zero signal, and
  `install_payload_profiles` repeated the unprotected overwrite into every
  `profiles/<p>/plugins` (GAP-E-4's second half). Before any copy now, each
  deployed file is content-hashed against the incoming payload: drifted files
  (changed, or deployed-only) are collected into
  `plugins-backups/<entry>.drifted.bak-<ts>/`, flagged per-path in the install
  summary (`drifted` / `drift_detected`), and surfaced by `bootstrap.sh` as a
  loud `⚠ OPERATOR DRIFT` list — the install proceeds (payload wins the live
  path, files are NEVER auto-merged) and exits 3 to signal it. `--force` opts
  in to the old silent-overwrite behavior across every destination. A clean
  (identical) deployed tree never flags, so the normal reinstall path stays
  silent; `backup=False` with drift reports the destruction honestly
  (`preserved: False`). The drifted collection is its own `backup_util`
  family (`<entry>.drifted`), so both piles keep independent keep-N windows
  (t_9462260b discipline). The guard generalises the preserve precedent that
  previously covered only `autodown.json`. Tests:
  `hscc-bootstrap/tests/test_install_payload.py` (12 new cases covering
  preserve+flag, `--force`, clean-tree silence, profile fan-out drift, shape
  changes, unreadable-file fail-safe, retain windows, and the rc-3 CLI
  contract).
