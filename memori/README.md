# memori

Cloud-backed long-term memory provider for Hermes (agent memory). Hermes
selects it via its `memory.provider` config; the plugin registers itself
through the Hermes plugin API — `register(ctx)` calls
`ctx.register_memory_provider(MemoriMemoryProvider())`
(`memori/__init__.py:450`).

## What it is

- Wraps **Memori Cloud** (pip dep `memori>=3.3.3`, see `plugin.yaml`):
  structured memory capture + recall against the hosted service.
- Captures completed turns on the `on_session_end` hook (`plugin.yaml`).
- Exposes the `memori_*` agent tools (`tools.py`): `memori_recall`,
  `memori_recall_summary`, `memori_compaction`, `memori_quota`,
  `memori_signup`, `memori_feedback`.

## Configuration

Config resolves from environment first, then a `memori.json` under the Hermes
home (`_paths.config_path`; env names in `__init__.py::_load_config`):

| Env | `memori.json` key | Required |
|---|---|---|
| `MEMORI_API_KEY` | `apiKey` | yes |
| `MEMORI_ENTITY_ID` | `entityId` | yes |
| `MEMORI_PROJECT_ID` | `projectId` | no |
| `MEMORI_PROCESS_ID` | `processId` | no |
| `MEMORI_API_URL_BASE` | `baseUrl` | no |

Without api key + entity id the provider reports unavailable and never
initializes.

## Sibling

`memori_byodb/` is the bring-your-own-DB variant HSCC actually runs: local
SQLite store, no API key, offline local-LLM augmentation.
