kind: Changed
task: t_bf5b1d50

- **Audit §5.1 now records the live plugin tier and the 2026-11-25 denominator.** The
  main-only-surface audit still said `0 / 51` profile configs carried a `plugins` key and
  that the 51 clean configs were one `_migrate_to_21` away from the whole cluster surface.
  Re-measured 2026-11-25: **52 / 52** configs carry an explicit key — 51 hooks-only
  `[hscc-cluster]`, `<main>` (`general`) all three — written by bootstrap's post-2.17.4
  tier sweep (`t_e98c6ad4`) on 2026-11-23 17:52, not by the migration. §5.1 names the
  writer and why the shape is compliant (register-time gate, not config alone), §5.2
  re-runs the production gate against it, and the directory count is corrected again to
  **54** because a real profile was created 2026-11-25 (`.deleted` remains excluded).
