# docs

HSCC follows a doc-driven flow: **spec → plan → build**, with the plan's
checklist as the acceptance contract the reviewer checks against.

Internal design specs and per-workstream implementation plans are kept **local
and untracked** (under `docs/superpowers/`, gitignored) — they're working
documents, not part of the published repo. The shipped record of what changed
lives in the top-level [`CHANGELOG.md`](../CHANGELOG.md) and the GitHub releases.

**Authoring an entry:** never edit `CHANGELOG.md`. Add one file per kanban
card at the repo root's `changelog.d/<your-task-id>.md` (see its
`changelog.d/README.md`, at the repo root — `git rev-parse --show-toplevel`;
this deployed copy of `docs/` does not carry that directory), and
`[Unreleased]` is generated from those fragments at release. The rule exists
because the shared `[Unreleased]` section had become a serial merge-conflict
point for concurrent cards.
