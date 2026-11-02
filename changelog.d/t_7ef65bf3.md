kind: Fixed
task: t_7ef65bf3

- **A profile regeneration can no longer delete the top-level `onboarding`
  block.** `hscc bootstrap --yes` at 12:24:45 regenerated every profile
  `config.yaml` through `hscc-roles/generator.py:generate_profile`, and the
  regeneration silently dropped `onboarding.seen.tool_progress_prompt` from 47
  profiles: every worker's first tool call re-fired the one-time progress
  prompt, hung at "preparing terminal", and dispatcher spawns stalled until the
  operator restored 47 configs by hand and restarted the gateway (fleet healthy
  12:41). Root cause: `_read_existing_config` read back ONLY `compression` and
  `auxiliary` from the on-disk file, and `generate_profile` builds the config
  dict from the role spec and writes it wholesale — so any top-level key the
  generator does not emit itself (onboarding, and anything else Hermes owns)
  could not survive a regeneration. Fix: `_read_existing_config` now returns
  `(compression, auxiliary, onboarding)` and the config write site merges the
  on-disk `onboarding` block through a new `_onboarding_block`, the same
  operator-choices-survive polarity as `_compression_block` / `_memory_block`
  — existing wins at EVERY depth, `_ONBOARDING_DEFAULTS` (empty today) fills
  only keys that are missing, nothing existing is ever deleted or overwritten,
  and the caller's dict is never mutated. A profile that has no onboarding
  block still gets none, so fresh-profile output is byte-identical to before
  (verified by the regen-is-idempotent tests). `compression` / `auxiliary`
  merge behaviour is untouched — `auxiliary.goal_judge` being ADDED is a
  separate, correct new-feature change. New in `hscc-roles/tests/
  test_generator.py`: the incident regression (seed
  `onboarding: {seen: {tool_progress_prompt: true}, extra_custom: 7}`,
  regenerate, both subkeys survive verbatim), an additive-only companion (the
  rest of the generated config is unchanged versus a profile with no
  onboarding block), the fresh-profile case, plus unit coverage for the deep
  merge, falsy-value handling (`False`/`0`/`None` are preserved, not treated
  as absent), non-mutation and the `None`-means-write-nothing contract.
  Mutation probe: deleting the two merge lines makes ONLY the incident test
  fail (55 passed, 1 failed) — the suite is not merely counting tests.
