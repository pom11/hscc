# Plugin Build Checklist

Before creating a new hscc-* plugin, verify:
- Python script at ~/.hermes/plugins/<name>/hscc.py
- Executable permissions: chmod +x
- SKILL.md at ~/.hermes/skills/<name>/
- All commands return structured JSON
- No streaming commands (use timeout wrapper)
- Node IPs read from cluster.json, not hardcoded
