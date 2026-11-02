kind: Added
task: orch-work-rules

- **Orchestrator profiles carry the work-management rules and one standard
  skill pack.** The per-project orchestrator SOUL block now includes "How you
  manage work": sub-atomic cards, chain with parents (never create dependents
  `blocked`), no triage parking, required card evidence, the orchestrator
  re-runs checks before merging, card vs `delegate_task` subagent vs self,
  creating a missing specialist via `hscc-roles create` + `generate`, slots
  belong to the dispatcher, no polling loops. New `hscc-roles orch-sync
  [--dry-run] [--only A,B] [--skills-source DIR] [--json]` brings existing
  `<project>-orch` profiles in line without regenerating them: it re-renders
  the SOUL base + Operational block (the project's `## Role:` section is kept
  byte-for-byte, the old file is kept as `SOUL.md.bak-<stamp>`) and adds the
  missing skills of `ORCH_SKILL_PACK` from the shared skills tree, matched by
  name so it never creates a same-name collision. Config, memory, sessions and
  project skills are never touched.
