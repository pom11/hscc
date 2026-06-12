# WS8 — Upstream/Fork Strategy — Implementation Plan

**Spec:** `../specs/2026-06-12-hscc-hardening-and-orchestrator-design.md` §WS8, D1/D15.
**Status:** built (autonomous).

## Goal
Run official hermes + sparkrun; keep the local edits as captured, reapply-able
patches instead of long-lived forks (the "painful updates" pain).

## What was forked (enumerated)
- **hermes-agent** (NousResearch): the kanban review-flow feature — 6 files
  (`tools/kanban_tools.py`, `hermes_cli/kanban_db.py`,
  `hermes_cli/kanban_decompose.py`, `agent/prompt_builder.py`, + 2 tests),
  ~386 lines. (The branch was also nominally "8 commits ahead" but most of that
  delta was website churn from a stale base — only the kanban set is essential.)
- **sparkrun** (spark-arena): 2 commits — restart-policy default, openclaw compat.
- **recipes:** stay as the `~/.sparkrun-local/recipes/local-fixed/` overlay
  (sparkrun reads local recipes by path — no patching needed).

## Deliverables
- `patches/hermes/*.patch` + `patches/sparkrun/*.patch` — `git format-patch`
  artifacts of the curated commits.
- `patches/MANIFEST.md` — what each patch is, what's excluded + why, update flow.
- `hscc-bootstrap/apply_patches.py` — stdlib reapply/check tool:
  - `--check` (no `--target`): dry-run all sets against conventional locations
    (`~/.hermes/hermes-agent`, `~/sparkrun`), per-patch applies/fails report.
  - `--target <dir> --set <name> [--check]`: `git apply --check` (dry) or
    `git am` (apply, preserves author/message; aborts cleanly on conflict).
  - Accepts both a `.git` dir and a `.git` file (worktrees/submodules).
- `tests/test_apply_patches.py` — synthetic-repo tests: clean apply passes,
  upstream-moved fails + names the file, real `git am` commits, conflict aborts
  cleanly (no mid-am state), non-git target errors.

## Update flow (documented in MANIFEST)
```
apply_patches.py --check                              # do patches still apply?
apply_patches.py --target ~/.hermes/hermes-agent --set hermes
apply_patches.py --target ~/sparkrun --set sparkrun
```
On conflict the script names the patch + file so it can be regenerated from a
rebased branch onto the new upstream release.

## Notes / honest limitations
- A `--check` against the *currently-patched* hermes-agent reports "does not
  apply / already exists" — correct (the changes are already in). `--check` is
  for a *fresh* upstream checkout.
- `--check` against `origin/main` shows some patches no longer apply: that base
  has drifted from where the patches were authored. Real upstreaming/rebasing
  (D15, later) regenerates them against the chosen release tag. The script's job
  here is to *detect and report* that drift, which it does.
- Upstreaming the kanban commits to NousResearch (D15) is deferred until proven.
