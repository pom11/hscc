# Agent Data Model

Source: `~/.hscc/agents.json`

## Structure

```json
{
  "agents": [...],
  "tasks": [],
  "mcpServers": [],
  "channels": []
}
```

## Agent Object Fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique agent identifier (dev-001 to dev-020, merge-001) |
| `name` | string | Display name (Builder, Merger) |
| `role` | string | Agent role (developer, reviewer) |
| `model` | string | Full model path including endpoint (e.g. `vllm-192-168-1-202/unsloth/Qwen3.5-122B-A10B-GGUF:Q4_K_M`) |
| `endpoint` | string | vLLM endpoint hostname |
| `systemPrompt` | string | System prompt (usually empty in source) |
| `tools` | string[] | Available tools (filesystem, shell) |
| `mcpServers` | string[] | MCP server connections (usually empty) |
| `skills` | string[] | Available skills (usually empty) |
| `maxTokens` | number | Token limit (usually 4096) |
| `temperature` | number | Model temperature (usually 0.2) |
| `status` | string | Agent status: `idle`, `failed`, `working`, `error` |
| `enabled` | boolean | Whether the agent is active in the fleet |

## Agent Inventory

| ID | Name | Role | Model | Endpoint |
|---|---|---|---|---|
| dev-001 | Builder | developer | Qwen3.6-35B-A3B-FP8 | vllm-192-0-2-246 |
| dev-002 to dev-020 | Builder | developer | Qwen3.5-122B-A10B-GGUF:Q4_K_M | vllm-192-168-1-202 |
| merge-001 | Merger | reviewer | Qwen3.5-122B-A10B-GGUF:Q4_K_M | vllm-192-168-1-202 |

## Notes

- dev-001 uses a different model (Qwen3.6-35B-A3B-FP8) on a different endpoint — it's the first/dev agent
- All other dev agents (dev-002 through dev-020) share the same model and endpoint
- merge-001 (reviewer) shares the model with the other dev agents but has role=reviewer
- Status `failed` on dev-002 in our sessions indicates a runtime failure, not a config issue
- Agents are sourced directly from `~/.hscc/agents.json` — never duplicate the data
- Task assignments link via `assignedAgent` field in `~/.hscc/projects.json` tasks
