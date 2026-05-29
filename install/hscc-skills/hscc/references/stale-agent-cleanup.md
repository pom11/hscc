# Stale Agent Cleanup

Agents that have been idle since `2026-05-22` or earlier should be cleaned up regularly to prevent zombie agent entries.

## Problem

When agents are created but never assigned tasks, or tasks are abandoned, stale `idle` entries accumulate in:
- `~/.hermes/plugins/plugin-state/hermes-agents.json`
- `~/.hermes/plugins/plugin-state/hermes-lifecycle.json`

## Detection

```bash
# Find stale agents (idle since 2026-05-22 or earlier)
python3 -c "
import json
l = json.load(open('~/.hermes/plugins/plugin-state/hermes-lifecycle.json'))
stale = [a for a, i in l['agents'].items()
         if i.get('state') == 'idle' and i.get('since', '').startswith('2026-05-22')]
print(f'Stale: {len(stale)}')
print(stale)
"
```

## Cleanup

Keep only agents with recent activity. Remove all `idle` entries older than 7 days:

```python
import json
from pathlib import Path

KEEP = {'dev-001', 'dev-002', 'dev-003'}  # agents with recent activity

for fname in ['hermes-lifecycle.json', 'hermes-agents.json']:
    fpath = Path.home() / f'.hermes/plugins/plugin-state/{fname}'
    data = json.loads(fpath.read_text())
    
    if fname == 'hermes-lifecycle.json':
        data['agents'] = {k: v for k, v in data['agents'].items() if k in KEEP}
    else:
        if isinstance(data, dict) and 'agents' in data:
            data['agents'] = {k: v for k, v in data['agents'].items() if k in KEEP}
        else:
            data = {k: v for k, v in data.items() if k in KEEP}
    
    fpath.write_text(json.dumps(data, indent=2))
    print(f'{fname}: cleaned to {len(data.get("agents", data))} entries')
```

## Prevention

After cleaning, the agent lifecycle should show only active agents. New agents can be provisioned via the orchestrator plugin when needed.
