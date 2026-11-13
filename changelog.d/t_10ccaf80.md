kind: Fixed
task: t_10ccaf80

- **`hscc bootstrap` deleted every profile-config key it did not own.**
  `generate_profile()` built `config.yaml` as a dict from scratch and wrote
  that dict, so any key outside its own set vanished — the per-block
  "operator values survive" merges (`_compression_block`, `_memory_block`,
  `_merge_toolsets`, `_onboarding_block`) only ever protected the blocks the
  generator already knew about. Measured against the operator snapshot
  `pre-2.15.10-bootstrap-20261113-1328` (49 profiles): `_config_version`
  disappeared from 45 of them and the whole `browser:` section (not a nested
  leaf — the top-level key) from 25. Losing `_config_version` is not cosmetic:
  Hermes then runs its silent config migration on that profile's next start,
  so 45 profiles took an unreviewed migration none of them asked for.
  `generate_profile()` now merges its generated blocks over the file's
  COMPLETE existing mapping (existing wins key order and every key the
  generator does not emit; the generator wins only the values it computes) and
  writes through Hermes' own `atomic_config_write()`, which keeps comments,
  key order and quoting and refuses a write that would drop a mapping path —
  so this class of loss fails loudly instead of deleting operator state.
---
kind: Verified
order: 1

- `hscc-roles/tests/test_generator.py`: 219 -> **235** cases at `19f92670`,
  green on the canonical interpreter with the board pins stripped. The new
  cases drive the real `generate_profile` and assert on FOREIGN keys rather
  than the two that got noticed, plus the fail-closed refusal and the payload
  handed to the writer.
- Acceptance ran against a COPY of the operator's own snapshot, never the live
  tree: bootstrap's two profile steps (`hscc.py generate` + `orch-all`)
  replayed into a throwaway home seeded from
  `pre-2.15.10-bootstrap-20261113-1328`, carrying that run's `serving.json`,
  reasoning policy and project registry so no difference can be an input
  artifact. 49 profiles compared, **0** `config.yaml` differences. The same
  harness at `7e546339` reports **45** differences, losing exactly
  `_config_version` and `browser` — the incident itself reproduced, so the
  zero is measured against a harness with teeth. Of the 49, 45 are inside
  bootstrap's generation set; `ecofire-flows-executor`, `flightdeck-orch`,
  `pontaje-orch` and `probe` are outside it and are left alone by both runs.
