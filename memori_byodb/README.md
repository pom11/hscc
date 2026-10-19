# memori_byodb

Bring-your-own-DB memory provider — the variant HSCC runs. Hermes selects it
via its `memory.provider` config; the plugin registers through the Hermes
plugin API — `register(ctx)` calls
`ctx.register_memory_provider(MemoriBYODBMProvider())`
(`memori_byodb/__init__.py:486`).

## What it is

- **Local SQLite store, no API key, no cloud** (module docstring
  `__init__.py:1-3`; `plugin.yaml` pip dep `sqlalchemy>=2.0.0`). The store
  defaults to `~/.hermes/memori_byodb.db` and is **shared across Hermes
  profiles**: one global `~/.hermes/memori_byodb.json` + one DB, with a
  fallback to that shared config for profile-scoped homes
  (`__init__.py::_load_config`).
- **Offline local-LLM augmentation** (`local_augmentation.py`): memory
  summarisation calls a local OpenAI-compatible endpoint instead of a cloud
  API — env `HSCC_MEMORI_AUGMENT_URL` (default: the local worker proxy
  `:4000`, not a single node's vLLM port) and `HSCC_MEMORI_AUGMENT_MODEL`
  (default: `worker-model`, the role alias every serving unit advertises).
- Captures completed turns on the `on_session_end` hook (`plugin.yaml`).
- Exposes the `memori_byodb_*` agent tools (`__init__.py::TOOL_SCHEMAS`):
  `memori_byodb_recall`, `memori_byodb_recall_summary`,
  `memori_byodb_feedback`, `memori_byodb_compaction`.

## Configuration

`~/.hermes/memori_byodb.json` (written by `save_config`; camelCase on disk —
`_config.py::MemoriConfig.from_dict` accepts both cases):

| Key | Required | Meaning |
|---|---|---|
| `entityId` | yes | stable end-user / workspace identifier |
| `projectId` | no | recall/summary scope (else derived from the agent context) |
| `processId` | no | process/agent identifier |
| `dbPath` | no | SQLite file path (default `~/.hermes/memori_byodb.db`) |

Hermes env (e.g. the gateway launchd plist) selects the provider:
`memory.provider: memori_byodb` plus the `HSCC_MEMORI_AUGMENT_*` env vars.

## Sibling

`memori/` is the Memori **Cloud** variant (API key + hosted service); this
package is the fully-local alternative.
