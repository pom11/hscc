# WS1 — HSCC Orchestrator Identity — Implementation Plan

**Spec:** §WS1, D5/D6/D9, absorbs M4 (#114). **Depends:** WS2 (done).

## Goal
One coherent identity named **HSCC**, balanced character, topology-free, with
`~/dev` working-dir discipline. Delivered via `install_soul.py` overlay so SOUL.md
and `personalities.ops` share a single source and can't drift.

## The M4 leak (root cause)
The managed `HSCC_SOUL_BLOCK` is already topology-free. The IPs leak in the
**preambles** that live OUTSIDE the managed block: live `SOUL.md` line 1
(`...node 192.168.88.244; workers .246/.247/.248; NAS .249...`) and the ops
persona header. Those are hand-edited and bootstrap doesn't own them.

## Changes to `install_soul.py`
1. **Name + balanced character.** `_DEFAULT_SOUL_HEADER` / `_OPS_PERSONA_HEADER`
   → "You are **HSCC**, the orchestrator of a DGX Spark GPU cluster…". Topology-
   free (no IPs). A little character (operator voice), still terse/action-first.
2. **Discovery in the guidance.** Add `discovery_status` to the read-tools list
   in `HSCC_SOUL_BLOCK`; reword "read live state" to name discovery as the
   source of topology.
3. **Working-dir discipline (D6).** New section in `HSCC_SOUL_BLOCK`: all dev
   work in `~/dev/<repo>`; one repo per project; never create duplicate clones;
   if a repo exists, work in it.
4. **Make the SOUL preamble managed too** — so a hardcoded-IP line can't sneak
   back. Wrap the identity preamble in its own sentinel (`HSCC:SOUL-HEADER`) that
   `install_soul` rewrites, while still preserving genuinely user-authored prose
   below. (Brevity/Autonomy/Auto-roles sections stay as user content between the
   header sentinel and the guidance block.)

## Live application
After editing, run `install_soul.py` against `~/.hermes` to regenerate SOUL.md +
ops persona. Backs up first (idempotent). The hardcoded line-1 IPs get replaced
by the topology-free HSCC header.

## Tests (extend `tests/test_install_soul.py`)
- rendered SOUL + ops contain no `\d+\.\d+\.\d+\.\d+`.
- identity says "HSCC"; working-dir discipline present; discovery_status named.
- SOUL guidance block == ops guidance block (shared source).
- idempotent re-run = unchanged.
- header-sentinel replace preserves user sections between header and guidance.

## Acceptance
Fresh `install_soul.py` → topology-free HSCC identity with `~/dev` discipline;
live SOUL.md line-1 IPs gone; re-run no-op.
