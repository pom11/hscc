kind: Fixed
task: t_cfdcefa9

- **CLI help-surface gate was interpreter-dependent (GATE-313).** The probe
  interpreter renders the help it audits (`PY = sys.executable`), and class
  (A) deduped findings by *raw rendered* text across all three widths — so
  CPython 3.13's argparse (help column +2 chars) wrapped one `doctor`/`update`
  description at width 80 where 3.11 did not, and the gate counted 15 findings
  on 3.13 vs 14 on 3.11 for the *same* 14 logical help lines
  (`EXTRA x15 (baseline 14) [A] hscc project --help contains 'flightdeck'`,
  green on the canonical leg, red on the p313 leg). (A) now has
  logical-line identity: derived from the widest render only and
  whitespace-canonicalised (`canonical_line`), exactly as the engine's own
  docstring always mandated for (A)/(C) — (C) already honoured it, (A) did
  not. (B)/(C)/(D)/DEAD are untouched: (B) *measures* wrapping, so
  width-dependent rendering is its subject. A narrow render can no longer add
  a violation, only a fragment; a narrow line that is not contained in any
  wide line means help CONTENT is width-aware and is reported as a new
  `A-WIDTH` finding — the superset assumption is guarded, not trusted.
  Re-baselined the 2 remaining A rules from the wide canonical form
  (`hscc --help` 2 → **1**, the collapsed pair was one logical line; `hscc
  help project` stays **2** — two genuinely distinct lines). Also: `re.split`
  `maxsplit` in the example cleaner is now keyword, killing the 3.13
  `DeprecationWarning` that becomes an error in a future CPython.
---
kind: Verified
order: 1

- `scripts/tests/test_cli_help_surface.py`: 8 → **13** cases (the 127-case
  `scripts` leg green on BOTH interpreters — hermes venv 3.11.16 and conda
  p313 3.13.12, where the gate was red before this fix). New pins:
  `test_a_counts_match_committed_table` (committed per-surface (A) table
  `scripts/tests/cli_help_a_counts.json` — fails the moment an (A) count
  becomes rendering-dependent, no subprocess),
  `test_a_table_identical_under_sibling_interpreters` (runs the audit's new
  `--a-table` pass under every sibling CPython on PATH and asserts table
  equality inside a single leg), plus four pure-function pins of the
  fragment/canonicalisation rules and the `A-WIDTH` guard. Mutation-proven
  both directions: a genuinely NEW flightdeck help line injected on a clean
  surface (`hscc cluster`) went NEW-red exactly once despite wrapping at
  width 80 (wide-only neither swallows nor double-counts), and clearing the
  leak without pruning its rule went STALE-red as designed.
