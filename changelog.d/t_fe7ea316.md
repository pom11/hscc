kind: Fixed
task: t_fe7ea316

- **Two merge-queue review lessons were readable only by the orchestrator
  profile.** They lived in the live `hscc-orch` copy of the
  `kanban-orchestrator` skill, so no worker or reviewer profile ever saw
  them, and both have already produced a wrong verdict on this board: a lane
  accused of pushing on its own PR because of a stale `gh pr view` read, and a
  new doctor row judged by the wrong test. The template now vendors
  `references/reviewing-queue-gated-prs.md` from
  `~/.hermes/profiles/hscc-orch/skills/devops/kanban-orchestrator/references/`,
  following the `pitfalls-card-writes.md` precedent (#796). Unlike that one,
  the template cited this reference nowhere, so `SKILL.md` gains the pointer
  line naming both discriminators; `version:` stays `3.1.0`, per the
  precedent. Two absolute personal paths present in the live copy were
  scrubbed — one to `$HOME`, one to the guard's documented placeholder — and
  the section that was history-about-the-previous-draft rather than a rule was
  retitled to state the rule.
