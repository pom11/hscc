kind: Added
task: t_7edf58ce

- **`orch-sync` and `reasoning-sync` now take `--hermes-home DIR`, like the
  three commands `t_d39c720e` covered.** Both resolved `rolelib.PROFILES_DIR`
  and wrote into it — `orch_sync.sync_all` rewrites `SOUL.md` (plus a
  `SOUL.md.bak-<stamp>`) and copies skill dirs into every live `<project>-orch`
  profile, `reasoning_sync.sync_all` rewrites the `agent`/`delegation`
  `reasoning_effort` keys of every profile `config.yaml` — with no intent check
  at all. They now share the single parse+apply helper `_take_home_flag`: apply
  the home, then `rolelib.assert_profile_root(<DIR>/profiles, purpose="<cmd>
  --hermes-home")` before the first write, refusal = exit **2** with stdout left
  empty (stdout on both is machine-captured JSON, so a refused run must not
  print a partial report). No flag still means no apply and no assert: what a
  real `hscc template apply` targets is unchanged.
---
kind: Fixed

- **A "sandboxed" `orch-sync` could no longer copy the operator's real skill
  tree into a throwaway profile.** `--skills-source` defaulted to
  `<hermes-home>/skills`, but the default was computed *before* the options were
  parsed — so it always resolved to the ambient `~/.hermes/skills` even when a
  different home was requested, and a run that believed it was sandboxed read
  the operator's skill tree. The read root now follows the write root:
  `--hermes-home DIR` makes the default `DIR/skills`, and a missing pack there
  is reported under `skills_missing_source` rather than silently borrowed. An
  explicit `--skills-source` still wins, and the one root the flag deliberately
  does **not** move — the reasoning policy, read from `$HSCC_REASONING_POLICY`
  (`~/.hscc/state/reasoning_policy.json`, cluster-global config outside the
  hermes home) — is now stated in the help, the command docstrings and the
  README instead of being something a caller had to know.
---
kind: Verified

- `hscc-roles/tests/test_sync_profile_root.py`: 15 cases, same shape as
  `test_profile_root_assert.py` — fake live root under `tmp_path`, `HOME`
  relocated for the child only, nothing ever written against
  `~/.hermes/profiles`. The `hscc-roles` leg **250 -> 265** at this commit
  (0 skips), baseline 250 re-measured at `035e2c52` in the same invocation; the
  pre-existing 11 profile-root cases stay green, so the two commands did not
  drift from the three `t_d39c720e` covered.
- The fingerprint walks **every** file under the live root rather than the
  parent test's three watched names: `orch-sync` creates files under
  `<profile>/skills/` and a `SOUL.md.bak-<stamp>`, which a name-limited check
  would have called "unchanged".
- Non-vacuity pinned on both sides, per command: the same seed *without* the
  flag really does change that fingerprint (so "unchanged" after a refusal means
  something), and a sandboxed run really does produce its claimed output in the
  requested home. The read-root cases put a different marker in each skill tree,
  so "it read the operator's tree" is a detected event, not an assumption.
- Mutation-probed on a copy (control green, provenance asserted from inside the
  run by a plugin that writes a sentinel): **6/6 CAUGHT**, each by the case that
  owns the decision — orch-sync continuing after a refusal ->
  `test_refused_intent_writes_nothing[orch-sync]`; reasoning-sync continuing
  after a refusal -> the same case for `reasoning-sync`; the read root left
  pinned on the operator's `~/.hermes/skills` ->
  `test_skills_source_default_follows_hermes_home`; an explicit
  `--skills-source` dropped -> `test_explicit_skills_source_beats_the_home_flag`;
  the flag parsed but never applied -> `test_refused_intent_writes_nothing` and
  `test_sync_writes_only_the_requested_home` for `reasoning-sync`; a `--dry-run`
  that writes -> `test_dry_run_with_flag_writes_neither_home`.
