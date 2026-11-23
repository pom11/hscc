kind: Added
task: t_d0a3ab8a

* **The main orchestrator's SOUL gets a `## Controlling the cluster` section —
  the audit's verdict table, in the one place the main actually reads.**
  `hscc-roles/orchestrators.py` carries it next to `_MAIN_ASSISTANT_WORK`, on the
  main-orchestrator identity, so `compose_soul` and bootstrap's own
  `ensure_orchestrator` both emit it and a regeneration cannot drop it (the same
  carriage that keeps `## Work the way the operator's main assistant works`).
  It maps WHICH TOOL FOR WHICH JOB across all 28 registered `hscc-cluster` tools
  (measured from the registry, not from prose), each beside the CLI verb it
  mirrors, and states the confirm-gate contract in the order the gate exists to
  enforce: plan first, read it, then `confirm`. It names the guards that are
  shipped, not a paraphrase — `node_pause`'s eligibility rule (a node parks only
  when every model on it runs on that node alone, a span is refused with NO
  override, `force` covers the orchestrator guard only) and `reap_orphans`'
  refusal when `serving.json` is absent, unparseable, unit-less or silent about
  the node. Where the operator-signed-off audit decided AGAINST a tool the
  section says the absence is a decision, so nobody routes around the guard to
  fill a gap they were never told about: no monitor tool (D2), no fleet-power
  tool (D3), the board brake stays CLI (D6), the merge queue's write half stays
  closed (D8), `doctor` stays CLI because it reads this host's trees (D11), and
  the autodown write half is operator scope (audit section 2.4). It closes with
  what is OPERATOR-ONLY — a LIVE publish, and arming or changing autodown — and
  states the shipped release rule beside them rather than inventing a new gate.
  The nudge/channel rules are NOT restated: `orch_message` is mapped to its job
  and the guard list stays owned by the single `## Talking to orchestrators`
  section (#644), because two copies of a guard list is how one goes stale.
* **The section opens by stating the main-only cluster rule (operator,
  2026-11-23 07:24).** The main orchestrator profile — named by the bootstrap
  config, never spelled in the text — is the ONLY profile with cluster control:
  the `hscc-cluster` tools, the `hscc` slash commands, the `sparkrun` passthrough
  and the hscc skills live there alone, and a worker or project orchestrator asks
  the main for a cluster action (a card on its board, or `orch_message`) instead
  of taking it. No sentence in the section phrases cluster access as available to
  another profile. That is the rule written down; scoping the plugins in
  bootstrap and reversing the hscc-skill fan-out are the operator's own follow-up
  cards from the same brief, and the worker/project-orch template sweep is
  t_c3a29c37 — none of them is done here.
---
kind: Verified
task: t_d0a3ab8a

* `hscc-roles/tests/test_main_orch_cluster_control.py` (new, 79 items) renders
  the section through BOTH write paths (`compose_soul`, and bootstrap's
  `ensure_orchestrator` -> `SOUL.md` in a tmp home), asserts it lands once inside
  the role section a regeneration owns, that it follows a RENAMED main through
  `main_orch`, and that a project orchestrator and a worker never get it.
  `hscc-roles` leg, sealed env, one pytest process: **451 passed, 0 failed**
  (372 pre-existing + 79 new) at this head.
* No invented verbs — the audit's own F3/F5 class, pinned against this card.
  Every backticked tool name must be in the set `hscc-cluster/__init__.py`
  registers (its parser asserts >= 28 tools so the read cannot go vacuous) and
  every backticked `hscc ...` verb must be dispatched: top level against
  `TOP_LEVEL_COMMANDS`, group verbs against the subcommand table in the module
  that routes that group. Coverage is pinned BOTH ways — a tool the section
  skips must be in a two-name reasoned exclusion set, and >= 15 verbs must be
  inspected, so a thin section fails the gate instead of passing it.
* The toolset + skill guarantee is asserted on the GENERATED PROFILE, not on the
  prose: `config.yaml` for the main profile (default name and a renamed one)
  carries `hscc-cluster` + `sparkrun`, a generated worker profile still lacks
  both, and every skill the section points at (`hscc`, `hscc-cluster`,
  `hscc-model-onboard`) is in `hscc-skills`' `BUNDLED_SKILLS` — a reference
  bootstrap cannot install is caught at the sentence that promises it, not at
  spawn time.
* The operator's main-only rule is pinned in both directions: nine named clauses
  (the ONLY-profile sentence, the four surfaces, the ask-instead-of-take path),
  no spelled main name (so a renamed main stays correct), and a phrasing guard
  that refuses any line handing the surface to a worker or project orchestrator —
  with a positive control proving the phrasing predicate can fire.
* Genericity guards re-run against this section (public repo): no path-shaped
  token, no home-relative path, no account or home name checked against the
  RUNTIME user, no address literal, no assistant product name, no fixture project
  name baked in — plus a positive control that proves each predicate is
  satisfiable. `hscc notify` is the named escalation path; no raw transport is
  offered (`docker`, `ssh`, `launchctl`, `sudo` are all asserted absent).
* Eight mutation probes on a copy of the section, each reverted and re-verified
  (file byte-identical after the run): invent a tool name, rename a read tool to
  a mutating verb, invent a CLI verb, drop the D6 board-brake line, restate the
  nudge guard list, drop the toolset claim, drop the ONLY-profile sentence, and
  phrase access as available to workers. 8/8 bite.
