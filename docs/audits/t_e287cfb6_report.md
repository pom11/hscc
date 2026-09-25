# Audit — t_e287cfb6: generator.py emits + preserves per-profile `memory:` block

Status: DONE (merged to main + pushed + deployed)
Date: 2026-09-25
Branch: wt/t_e287cfb6 -> main
Merge SHA: 29c89c7 (final main tip; 92950e5 merged the feat + first report, 29c89c7 the report SHA update); branch commits 0920af6 (feat) + 1c0a93a + 7fe5afc (docs)
Deployed: yes — `python3 hscc-bootstrap/install_payload.py` (hscc-roles installed, missing=[])

## Problem

`generate_profile` (hscc-roles/generator.py) wrote only `toolsets`, `skills`,
`model`, `compression` into each profile's config.yaml — it never emitted or
read a `memory:` key. A regeneration (`hscc-roles generate`, or `orch`/`orch-all`
via `orchestrators.ensure_orchestrator`) rewrote config.yaml WITHOUT the
`memory:` block, silently dropping the hand-set values that ~14 orchestrator
profiles carry:

    memory:
      memory_enabled: true
      memory_char_limit: 4000
      user_char_limit: 2000
      provider: memori_byodb

Every regenerated profile fell back to Hermes' built-in memory at its smaller
default — the "bootstrap run silently reverts a hand-set value" the operator
reported. (ROADMAP `profile-provisioning` item 1, docs/ROADMAP.md lines 13-31.)

## Change (hscc-roles/generator.py only)

- `PROFILE_MEMORY_CHAR_LIMIT = int(os.environ.get("HSCC_MEMORY_CHAR_LIMIT",
  "4000"))` — single source of truth for the char-limit default (mirrors
  SESSION_COMPACTION_THRESHOLD_TOKENS; importable by consumers).
- `PROFILE_MEMORY_PROVIDER = os.environ.get("HSCC_MEMORY_PROVIDER",
  "memori_byodb")` — the provider default, only applied when absent/empty.
- `_read_existing_memory(pdir)` — mirrors `_read_existing_config`: reads the
  profile's on-disk config.yaml and returns its `memory:` dict or None.
- `_memory_block(existing)` — mirrors `_compression_block`: MERGES the desired
  block over any existing one (operator-choices-survive, never clobber):
  - `memory_enabled: true` only when absent/empty (None or ""); an explicit
    operator `False` (= disabled) is preserved.
  - `memory_char_limit` RAISED toward 4000, never lowered; a valid int >= 4000
    survives; a non-int is replaced by the default.
  - `provider` kept when non-empty string; only set to `memori_byodb` when
    absent/empty.
  - every other key (e.g. `user_char_limit`) preserved verbatim.
- `generate_profile` now calls `_read_existing_memory(pdir)` and sets
  `config["memory"] = _memory_block(existing_memory)` unconditionally, so EVERY
  generated profile (orchestrator, per-project orch, worker, general) carries a
  memory block that never reverts a hand-set value.

## Tests (hscc-roles/tests/test_generator.py, all hermetic)

6 new tests, all against tmp_path + monkeypatched HERMES_HOME / PROFILES_DIR
(never live ~/.hermes profiles):

- `test_fresh_profile_gets_default_memory_block` — fresh profile gets
  provider=memori_byodb + memory_char_limit=4000 + memory_enabled=true.
- `test_every_profile_kind_gets_memory_block` — emitted for orchestrator,
  project-orch, architect, worker.
- `test_regen_memory_is_idempotent_noop` — second run unchanged (changed=False).
- `test_hand_set_memory_char_limit_survives_regen` — the core operator property:
  6000 survives; user_char_limit preserved verbatim.
- `test_hand_set_provider_survives_regen` — operator provider string survives.
- `test_memory_block_preserves_lower_operator_limit_never_lowers` — higher value
  never lowered, lower value raised to floor, disabled memory stays disabled.

## Verification (exact commands)

hscc-roles/tests/test_generator.py (47 passed) under BOTH interpreters:

    ~/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_generator.py -q
    -> 47 passed in 0.51s

    /Users/desac/miniconda3/envs/p313/bin/python -m pytest tests/test_generator.py -q
    -> 47 passed in 0.28s

Full repo suite via scripts/run_tests.sh (8 plugin dirs, per-dir pytest
processes): ALL GREEN. Exact output:

    ━━━ hscc-bootstrap ━━━     272 passed in 406.47s
    ━━━ hscc-commands ━━━       69 passed in 0.04s
    ━━━ hscc-roles ━━━         120 passed in 1.14s
    ━━━ hscc-cluster ━━━       422 passed in 23.91s
    ━━━ hscc-project ━━━      1351 passed, 1 warning in 4.71s
    ━━━ hscc_daemon ━━━       1160 passed in 53.26s
    ━━━ sparkrun-hermes ━━━     12 passed in 0.01s
    ━━━ hscc-api ━━━           786 passed, 1 skipped in 233.72s
    ━━━ Summary ━━━  ALL GREEN

(Note: hscc-roles total is 120 passed; 47 of those are test_generator.py.)

## Hermetic demonstration: second run does not revert a hand-set value

Exact command (temp HERMES_HOME, never live profiles):

    TMPHOME=$(mktemp -d)
    PYTHONPATH=hscc-roles HERMES_HOME=$TMPHOME \
      ~/.hermes/hermes-agent/venv/bin/python docs/audits/demo_memory_regen.py $TMPHOME

Output:

    run1 changed      : True
    run1 memory       : {'memory_enabled': True, 'memory_char_limit': 4000, 'provider': 'memori_byodb'}
    after hand-edit  : {'memory_enabled': True, 'memory_char_limit': 6000, 'provider': 'operator-picked-provider', 'user_char_limit': 2000}
    run2 changed      : False
    run2 memory       : {'memory_enabled': True, 'memory_char_limit': 6000, 'provider': 'operator-picked-provider', 'user_char_limit': 2000}
    run2 is no-op     : True
    DEMO PASS: second generate_profile run did NOT revert hand-set memory values.

The self-contained script (docs/audits/demo_memory_regen.py) shows: run 1
builds the fresh default block (memori_byodb + 4000); the operator hand-sets
6000, an operator provider, and user_char_limit; run 2 is a byte-identical
no-op (changed=False) and every hand-set value survives.

## Deploy

Merged to main + pushed; `python3 hscc-bootstrap/install_payload.py` run to
deploy hscc-roles into ~/.hermes/plugins. The generator itself was NOT invoked
against live profiles — this card is code + hermetic tests only.

## Notes

- No LAN/tailnet addresses introduced. No secrets. No AI attribution in commits.
- Scope honored: only hscc-roles/generator.py + its test were modified.
