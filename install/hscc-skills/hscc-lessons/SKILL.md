---
name: hscc-lessons
description: "Use when doing engineering work on the HSCC codebase / cluster — the fleet's hard-won conventions and recurring-bug patterns to avoid."
---

# HSCC Fleet Lessons

Hard-won lessons from the HSCC codebase and DGX Spark cluster. Load this skill before starting engineering work to avoid recurring mistakes.

## Task scoping

- **Scope sub-atomically: one file per card.** Big multi-part tasks make a worker wedge — they lose context mid-way and produce incomplete diffs. Split into focused, single-file cards before dispatch.

## Verification

- **Verify reality, not narrative.** Confirm the actual code and HEAD state with grep, tests, or file reads before claiming done. Do not trust "it passes" or "it's committed" without running the check yourself.
- **Run the component test suite AND verify live before merging.** Tests alone are not enough — hit the running service or inspect the deployed state to confirm the change landed.

## Ports and endpoints

- **Ports and endpoints come from serving.json / the sparkrun unit, never hardcoded.** Never assume `:8000` or `:4000`. Probe the unit's real port from `serving.json` or `sparkrun cluster list --json` before constructing URLs.

## Error handling

- **Every check and IO is best-effort.** Never crash on missing or corrupt files — return skipped/ok, log the issue, and continue. A single bad sensor should not halt the pipeline.

## Configuration

- **Preserve explicit operator config: fill only when absent.** Never clobber an operator's explicit value, even if it is `false` or `""`. Write defaults only into empty keys.

## Cluster operations

- **Stop only the target recipe on a node, never `sparkrun stop --all`.** A blanket stop kills co-located siblings sharing the same GPU — including the orchestrator or other agents.

## Process management

- **Silent success is a bug.** Check subprocess return values. If a command fails, surface a warn/error instead of reporting "ok" and hiding the failure.

## Scripting

- **bootstrap.sh JSON parsing: `python -c CODE _ JSON` puts JSON at `sys.argv[2]`, not `argv[1]`.** The underscore placeholder occupies `argv[1]`. Off-by-one arg indexing silently reads the wrong input.

## Quick checklist before dispatching or completing

| # | Lesson | Checked |
|---|--------|---------|
| 1 | Task scoped to single file? | |
| 2 | Verified actual code, not assumed? | |
| 3 | Ports resolved from serving.json? | |
| 4 | IO is best-effort (no crash on missing data)? | |
| 5 | Operator config preserved? | |
| 6 | Targeted stop, not `--all`? | |
| 7 | Subprocess return values checked? | |
| 8 | bootstrap.sh arg indexing correct? | |
| 9 | Tests run AND live state verified? | |
