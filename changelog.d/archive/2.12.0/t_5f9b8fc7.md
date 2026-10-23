kind: Fixed
task: t_5f9b8fc7

- **`init` dry-run line still said `flightdeck home:`** — the F1 branding
  reword (t_5005a067) covered every rendered *help* string (`--help`,
  `hscc help`) but missed the runtime dry-run line in
  `hscc-project/flightdeck/commands/init.py` (`cmd_init`, dry-run branch),
  the single line the `README_REVIEW_2026-10-15.md` R3 row cites as "post-F1
  reality". It now renders `hscc-project home: <path> (dry run — use --apply
  to write)`; the on-disk `~/.flightdeck` directory is unchanged (branding,
  not behaviour). `test_init.py::TestNoAnsiInit::test_dry_run` pins both
  directions so the reword cannot silently regress. Lands the R3 precondition
  (the hscc-project README rewrite can now truthfully quote the init output).
