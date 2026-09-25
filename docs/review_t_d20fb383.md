# README Review — t_d20fb383 (memori + memori_byodb + sparkrun-hermes)

PART 2 README REVIEW (2/5). Scope: the three per-dir READMEs created in fbb9b05
(2026-06-13), which predate the telegram removal, main-only cutover, and Rich
CLI epic. Verify every README statement is TRUE against the current tree, fix
or delete anything stale, verify commands by running.

## Method
- Read each README and cross-check every factual claim against the package code
  (plugin.yaml, __init__.py, execlib.py, skills/) and the deployment evidence
  (hscc-project/flightdeck/commands/doctor.py reads the gateway launchd plist
  env + memori_byodb config).
- These are prose READMEs — they document NO runnable commands, so there is
  nothing to execute; verification is claim-by-claim against the tree.

## Findings

### memori/README.md (5 lines) — ACCURATE, no change
- "Memory-provider plugin for Hermes (agent long-term memory)." TRUE —
  memori/__init__.py implements MemoryProvider and register() calls
  ctx.register_memory_provider(MemoriMemoryProvider()).
- "Used by Hermes' `memory.provider` config." TRUE — memori/install.py documents
  `hermes config set memory.provider memori` (install.py:162).
- "See `memori_byodb/` for the bring-your-own-DB variant HSCC runs (NAS-backed
  store + offline local-LLM augmentation)." TRUE — memori_byodb is the BYOD
  sibling; local_augmentation.py implements the offline local-LLM augmentation.
- No telegram mention, no CLI commands, unaffected by Rich CLI / main-only
  cutover.

### memori_byodb/README.md (5 lines) — ACCURATE, no change
- "Bring-your-own-DB memory provider (the variant HSCC uses)" TRUE — implements
  MemoriBYODBMProvider, registered via ctx.register_memory_provider (__init__.py:474).
- "NAS-backed memory store with offline local-LLM augmentation" TRUE — db_path
  resolved from HSCC_MEMORI_DB_PATH (NAS in deployment) or ~/.hermes default;
  local_augmentation.py is the offline LLM layer.
- "wired via `memory.provider: memori_byodb` + env in the gateway plist" TRUE —
  doctor.py `_read_gateway_env` parses the gateway launchd plist
  EnvironmentVariables for HSCC_MEMORI_AUGMENT_URL/_MODEL (doctor.py:458-489).
- "Sibling of `memori/`." TRUE.
- No telegram mention, no CLI commands, unaffected by Rich CLI / main-only cutover.

### sparkrun-hermes/README.md (10 lines) — ONE FALSE CLAIM, fixed
- "official Hermes plugin for sparkrun" TRUE (plugin.yaml author scitrera.ai,
  "Mirrors the official OpenClaw plugin").
- "single guarded `sparkrun_exec` tool — a raw `sparkrun …` CLI passthrough" TRUE
  (register registers only sparkrun_exec; execlib runs the CLI).
- "(orchestrator-only)" — NOT enforced by code. The guard is only that the
  command MUST start with `sparkrun`. Removed the qualifier as unverifiable.
- operation list (browse/search recipes, benchmark, tune, proxy, cluster
  definitions, export) TRUE — matches the run/registry skills commands.
- **"State-changing commands confirm first; pure reads run directly." FALSE** —
  execlib.py has NO confirmation stage. sparkrun_exec runs any command starting
  with `sparkrun` directly, capturing stdout/stderr. There is no read vs
  state-change distinction in the code. FIXED to describe the real guard.
- "Pairs with the run/setup/registry skills for sparkrun usage." TRUE — the
  skills/ dir has run/, setup/, registry/ (plus data-engineer/).
- "Registered via `register(ctx)`." TRUE.

## Changes made
- sparkrun-hermes/README.md: replaced the false confirmation claim with the
  accurate guard description (must start with sparkrun; runs directly;
  captures stdout/stderr). Dropped the unenforced "(orchestrator-only)" qualifier.
- docs/review_t_d20fb383.md: this report.
- memori/ and memori_byodb/ READMEs: verified accurate, no changes.

## Verification
- sparkrun-hermes tests pass: 12 passed.
- memori/memori_byodb have no README-documented commands to run.
- Cross-checked all claim evidence paths above.

## Commit
- 8c98207 docs(t_d20fb383): fix false 'confirm first' claim in sparkrun-hermes README
