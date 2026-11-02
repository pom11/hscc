kind: Added
task: reasoning-tiers

- **Reasoning effort is declared by the cluster template, per tier.** A v4
  template may carry `reasoning: {max, standard, light}` in the serving
  model's own vocabulary (the shipped `4node-flash-next-all` is now v4 with
  `xhigh / medium / low` — the only values its chat template accepts).
  `max` = orchestrators + `reasoning_tier: max` roles (reviewer, architect,
  security-engineer), `standard` = implementation workers, `light` =
  subagents (`delegate_task`) and bookkeeping auxiliary calls. `hscc template
  apply` records the policy (`~/.hscc/state/reasoning_policy.json`), sets the
  root `agent.reasoning_overrides` for `orchestrator-model` / `worker-model`
  and `auxiliary.<task>.reasoning_effort`, and runs the new `hscc-roles
  reasoning-sync`, which sets only `agent.reasoning_effort` +
  `delegation.reasoning_effort` in every profile. `hscc-roles generate` reads
  the same policy, so a regeneration keeps it. Role specs carry a tier
  (`reasoning_tier`), never a model-specific value; a tier the template does
  not declare is never written.
