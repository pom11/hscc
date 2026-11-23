kind: Docs
task: t_4c12ff7f

- **`docs/audits/2026-11-23-agent-surface.md`** — the agent-surface coverage
  matrix the 2.17.4 workstream is gated on: every `hscc` CLI verb (33 top-level,
  48 group leaves) × the 26 `hscc-cluster` agent tools × the 12 `hscc-commands`
  slash commands × the 96 API routes × the bundled skill that documents it, each
  cell marked MISSING / STALE / INTENTIONALLY CLI-ONLY with a per-verb reason
  rather than a blanket "destructive, therefore CLI-only". It also records where
  each tool reads state (`serving.json`, `applied_template.json`,
  `node_pauses.json`, `cluster.json`, the daemon's `state/*.json`) and who else
  writes that file, so a tool working from a stale copy has a named owner to
  check against. Findings F1–F13, open operator decisions D1–D11, and a LEFT
  list; the doc ships no behaviour change.
---
kind: Verified
task: t_4c12ff7f

- Counts are table counts at `origin/main` `82364196` (branch carries current
  main), not estimates: 33 `TOP_LEVEL_COMMANDS` (`hscc_daemon/hscc.py:698`), 26
  tools (`hscc-cluster/__init__.py:31-131`), 12 `register_command` sites
  (`hscc-commands/__init__.py:852-913`), 96 unique `(method, path)` route pairs
  with zero duplicate registrations + 1 WS (`ROUTES.append` sites across 22
  `routes_*.py` plus the 12-entry `routes_cluster.load()` table), 13
  `BUNDLED_SKILLS` names (`hscc-skills/hscc.py:59`).
- Skill-coverage column measured by `grep -rl` over `install/hscc-skills/` for
  49 verb strings: 7 hit, 42 hit nothing. Install-vs-skip measured by md5
  pairing each installed `~/.hermes/skills/<name>/SKILL.md` against its template
  (all 11 equal) and by `diff` against a profile-scoped copy (differs) — so the
  installer updates where it writes, and the gap is the template plus the
  default-home-only write target.
- Read-only throughout: no fleet mutation, no confirm-gated call, no daemon
  state write. `scripts/changelog_fragments.py check` and `scripts/readme_lint.py`
  both clean on the branch.
