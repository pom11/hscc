# docs

Design specs and implementation plans for HSCC, following a doc-driven flow:
**spec → plan → build**, with the plan's checklist as the acceptance contract the
reviewer checks against.

```
docs/superpowers/
  specs/   # what + why (design)   e.g. 2026-06-12-hscc-hardening-and-orchestrator-design.md
  plans/   # how (per-workstream)  e.g. 2026-06-12-ws2-discovery.md, …-ws4-workflows.md
```

The master design (`specs/2026-06-12-hscc-hardening-and-orchestrator-design.md`)
captures the 8-workstream hardening effort + locked decisions (D1–D17); each
workstream has a matching plan under `plans/`.
