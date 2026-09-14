# WS3 Verification: Native 0.17 Profile API

WS3 commit `32cdbc2` refactored `hscc-roles/generator.py` to use the Hermes 0.17 native profile API instead of hand-writing the profile directory.

## What changed

`generator.py` now uses three functions from `hermes_cli.profiles`:

| Function | Purpose |
|----------|---------|
| `create_profile(name, no_alias=True, no_skills=True)` | Scaffolds the profile directory (idempotent — raises `FileExistsError` if present, caught by generator) |
| `write_profile_meta(path, description, description_auto=False)` | Writes `profile.yaml` with the routing_description (WS2 feature) |
| `get_profile_dir(name)` | Resolves the path when `create_profile` already exists |

HSCC-specific config.yaml (model block, compaction, toolsets) is still written manually because the native API has no concept of cluster topology.

## Idempotency evidence

Generated a scratch profile twice — `changed=False` on second run, and a byte-level diff of the profile directory between runs shows **zero differences**.

```
=== FIRST GENERATION ===
changed: True

=== FILES CREATED ===
  SOUL.md  config.yaml  profile.yaml

=== SECOND GENERATION (idempotency) ===
changed: False

=== DIFF (second vs third generation) ===
IDENTICAL — no diff
changed3: False
```

## Worker model evidence

### Fast-tier (default) — worker proxy `:4000`

```yaml
model:
  default: Qwen/Qwen3.6-27B-FP8
  provider: custom
  base_url: http://localhost:4000/v1
  api_key: sk-sparkrun
```

### Strong-tier — orchestrator GPU `:8000`

```yaml
model:
  default: nvidia/Qwen3.6-35B-A3B-NVFP4
  provider: custom
  base_url: http://10.0.0.244:8000/v1
  api_key: sk-sparkrun
```

Worker model block is preserved: every generated role (except orchestrator) gets the `:4000` proxy endpoint, never the direct `:8000` orchestrator port.

## Tests

All 6 component suites pass:

| Suite | Passed |
|-------|--------|
| hscc-bootstrap | 185 |
| hscc-commands | 33 |
| hscc-roles | 48 |
| hscc-cluster | 240 |
| hscc_daemon | 497 |
| sparkrun-hermes | 8 |
| **Total** | **971** |