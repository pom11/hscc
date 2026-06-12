# docs

HSCC follows a doc-driven flow: **spec → plan → build**, with the plan's
checklist as the acceptance contract the reviewer checks against.

Internal design specs and per-workstream implementation plans are kept **local
and untracked** (under `docs/superpowers/`, gitignored) — they're working
documents, not part of the published repo. The shipped record of what changed
lives in the top-level [`CHANGELOG.md`](../CHANGELOG.md) and the GitHub releases.
