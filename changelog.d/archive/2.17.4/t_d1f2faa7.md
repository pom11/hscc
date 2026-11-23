kind: Fixed
task: t_d1f2faa7

- **A `flightdeck project new` orchestrator profile was provisioned unseeded, which
  is the state `_migrate_to_21` exists to be defused against.** Step 6a of
  `hscc-project/flightdeck/core/project_lifecycle.py` ran `hermes profile create`
  and touched nothing else, so `<name>-orch` — a home that must hold hooks only — was
  left without an explicit `plugins.enabled`. Measured against a sandboxed
  `HOME`/`HERMES_HOME`: `hermes profile create` writes `.env`, `SOUL.md` and
  `skills/` and **no `config.yaml` at all** (it calls
  `_migrate_profile_config_if_outdated`, which returns when there is nothing to
  migrate), and Hermes then creates that config on the profile's first run stamped
  with the current `_config_version` — so `_migrate_to_21` never runs for that home
  again and its `plugins` key is never filled by anything. Where the stored version
  *is* old (a cloned or legacy home), that migration fills `plugins.enabled` with
  every manifest-bearing dir under the home's own `plugins/`: **10 names** in a live
  `<name>-orch` home, i.e. the whole cluster surface landing in a hooks-only home.
- Step 6a now seeds the tier it creates, through `enable_plugins.py
  --plugins-tier-only --home <dir>` as a subprocess on flightdeck's own injectable
  `_run` seam (flightdeck runs in the isolated brew-keg venv and has no import path to
  `hscc-bootstrap`, t_9a9aeab1). The narrow flag is used rather than the plain
  `--home` wire on purpose: the full wire would provision a per-project orchestrator
  with the main install's toolsets, kanban routing, delegation and hooks. Best-effort
  and never fatal — the step's `created` | `skipped` contract and its
  `LifecycleError` still mean only "the profile could not be created", and the seeding
  outcome rides on the step's detail line instead of failing it.
- `enable_plugins.py` gained the entry point that new-home seeding needs:
  `ensure_plugins_tier_for_new_home()`. `ensure_plugins_tier_for_home()` correctly
  declines a home with no `config.yaml` (Hermes owns that file) — but declining is what
  left the create-time path unseeded, so the new function writes a tier-only config
  when none exists. That is a deliberate exception to `enable()`'s
  never-invent-a-config policy, recorded at the function, and it is self-defusing:
  `_migrate_to_21` returns as soon as it sees `plugins.enabled`.
- Both halves resolve the tier through `hscc-roles/main_orch.py` and nowhere else
  (ruling D-A): neither `enable_plugins.py`'s new function nor flightdeck's step spells
  a profile name or a plugin name, and a test pins that flightdeck never reaches the
  resolver directly.
---
kind: Verified
task: t_d1f2faa7
order: 1

- `hscc-bootstrap/tests/test_enable_plugins_new_home_tier.py`: **13** new cases —
  the config-less home, the two tiers from one resolver, the tier-only write touching
  no other key, pruning a home `_migrate_to_21` had widened, idempotence, the missing
  home, the escape hatch, and the `--plugins-tier-only` CLI (including that it refuses
  to run without `--home` and never enters the full wire).
- `hscc-project/tests/test_project_lifecycle_plugin_tier.py`: **14** new cases — the
  seed command's shape and ordering behind `hermes profile create`, seeding on the
  `skipped` path too, the four non-fatal degradations, the profile-scoped
  `HERMES_HOME` resolution, and two end-to-end runs of the real writer against a tmp
  home (the `hermes` CLI is stubbed by the `_run` seam in every case; nothing live and
  no cluster host is touched).
