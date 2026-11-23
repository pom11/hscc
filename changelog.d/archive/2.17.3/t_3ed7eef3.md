kind: Added
task: t_3ed7eef3

- **`install/hscc-skills/devops/kanban-orchestrator/` now ships the three
  `references/` docs its own `SKILL.md` cites.** `SKILL.md` points readers at
  `references/placeholder-ids-and-parking.md`,
  `references/wrong-board-cards.md` and
  `references/landing-a-superseded-pr.md`, but the template directory held only
  `SKILL.md`, so a profile recreated from the template got three dangling
  pointers. The docs are copied verbatim from the live `hscc-orch` profile
  copy. No manifest change was needed: `hscc-skills/hscc.py` installs a skill
  by walking its whole tree, so a nested `references/` directory ships with it
  the way other skills in the same tree already do.
---
kind: Verified
order: 1

- `diff` is empty and the sha256 matches for all three files against their
  live-profile counterparts, and each copied name is cited by the `SKILL.md`
  that ships beside it — all three citations resolve, none left dangling.
  `references/exit78-provider-rejection.md` is cited by no `SKILL.md` text, so
  it was deliberately not copied.
