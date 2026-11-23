# Main-only cluster surface: per-profile plugin/toolset resolution and the hooks that must survive

**Card:** t_1700dfc8 (read-only re-cut of t_0f214ba0)
**Measured:** 2026-11-23 at `origin/main` = `e88c1bb7` (worktree `wt/t_1700dfc8`)
**Posture:** read-only. No code change, no fleet call, no write to the live `~/.hermes`.
Every probe ran in a sandboxed `HERMES_HOME` under the scratch dir with fake hosts.
**Sibling doc:** `docs/audits/2026-11-23-agent-surface.md` (the 2.17.4 gate doc). Its
LEFT-2 — "same matrix for `hscc-cluster`'s *hooks* … they are a 6th surface" — is
answered here in §2.

## 0. Headline

_WIP — findings appended as measured._

## 1. Where each fact comes from (source paths read)

_WIP_

## 2. Hook matrix

_WIP_

## 3. Who calls `orch_message` and the `hscc-commands` slash verbs today

_WIP_

## 4. What breaks when a worker loses the toolset

_WIP_

## 5. Resolution order: root config vs profile overlay vs multiplex_profiles

_WIP_

## 6. The config shape

_WIP_

## 7. Gaps that need a hermes-side change

_WIP_
