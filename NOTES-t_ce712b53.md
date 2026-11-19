# t_ce712b53 — `source`/`axis` codes: value parity across the two history surfaces

**Base:** `origin/main` = `d03b9acd` (PR #425 merged during this card's run, so the
four codes this card pins are on main — no stack needed, the card's
"PR off `origin/main`" gate is literal).
**Branch:** `wt/t_ce712b53`. **Touched:** one test file.

## The gap, re-verified on this base

`hscc-api/routes_cluster.py:679-682` and `hscc_daemon/metrics_history_cli.py:105-108`
each define the four `source` codes as their own module constants — copied
verbatim, because the daemon leg runs without the API package (same copy-not-import
rule as the node-name rule). #421's
`test_the_two_surfaces_expose_the_same_vocabulary` in
`hscc-api/tests/test_cluster_history_cli_parity.py` compares the two payloads
**key for key**, so it cannot see a value move.

Checked before writing anything:

* `grep '"source"' hscc_daemon/tests/*.py` → no hit for this surface: the CLI side
  had **zero** value pinning anywhere.
* The route's own value pins (`test_cluster_history_parity.py:556-575, 647, 795`)
  name only the route.
* Probe P1 below is the card's premise, measured rather than argued: under a
  reworded CLI `SOURCE_STORE_ABSENT`, `test_the_two_surfaces_expose_the_same_
  vocabulary` **passed** (14 passed / 2 failed, the vocabulary test among the 14).

## What was added (one section, four tests)

`hscc-api/tests/test_cluster_history_cli_parity.py`:

| test | the drift it pins |
|---|---|
| `test_the_two_surfaces_share_one_closed_set_of_codes[source]` | same constant names, same value per name, same closed set — plus the ruled literals pinned in the test, so a reword on BOTH surfaces fails too |
| `test_the_two_surfaces_share_one_closed_set_of_codes[axis]` | same, for `axis` (`window`/`tier`) — copied across the same seam, with the same hole |
| `test_a_code_never_means_two_different_things` | the two vocabularies cannot collide on either surface |
| `test_both_surfaces_answer_with_the_same_code_for_the_same_state` | the wire half: all four states asked of both surfaces, same ruled code — including tier 0 **with the ring resident**, a state no cross-surface test had ever compared (their transposed host/engine rows now have to match too) |

#421's key-level assertion is **untouched**, and probe P7 shows it is still
load-bearing: a *key* drift reddens it while the value pin stays green. Both
halves are needed, which is what the card asked for.

## Mutation probes — 7/7 caught, control green, tree restored

Driver: `probe_driver.py` (attached to the card; scratch `t_ce712b53/`), full
per-probe pytest output in `out/P*.txt` (attached as `probe-outputs.txt`). Copy at
an absolute path, provenance-gated by a `-p` plugin inside the copy that asserts
every collected module and both subject modules resolve
under the copy and writes a sentinel the driver deletes before each run;
pristine captured eagerly from `git show HEAD:<path>`; reset + byte-compare
before every probe; compound mutant composed on one running copy;
`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, `__pycache__` purged before
each run. Control P0 green (16 passed), every verdict below reproduced on a
second identical run.

| probe | mutant | verdict | reddened |
|---|---|---|---|
| P0 | none (control) | CONTROL-GREEN | — (16 passed) |
| P1 | CLI `SOURCE_STORE_ABSENT` → `store_missing` | CAUGHT | `closed_set[source]`, `same_code_for_same_state` |
| P2 | route `SOURCE_STORE_ABSENT` → `store_missing` | CAUGHT | same two |
| P3 | CLI `SOURCE_RING`/`SOURCE_STORE` values swapped (same value SET) | CAUGHT | same two — **only** the per-name binding can see this; a set-equality check alone passes |
| P4 | compound: BOTH surfaces reworded the same way | CAUGHT | same two — the ruled literals are the third anchor |
| P5 | route tier-0 branch writes an inline `"ring"` instead of the constant | CAUGHT | `same_code_for_same_state` only; `closed_set[source]` stayed green |
| P6 | CLI `AXIS_TIER` → `tier_axis` | CAUGHT | `closed_set[axis]`, `same_code_for_same_state` |
| P7 | CLI writes the store code under `src` instead of `source` | CAUGHT | #421's `expose_the_same_vocabulary` + the wire test; `closed_set[source]` stayed green |

Quoted consequences (from the run, not written after the fact):

* P1 — `SOURCE_STORE_ABSENT is 'sqlite_store_absent' on the route and
  'store_missing' in `hscc cluster history`. The payloads still match key for key,
  so nothing else notices: a client comparing 'sqlite_store_absent' takes the
  wrong branch for every source answer that came from the CLI`
* P3 — `SOURCE_RING is 'recorder_ring' on the route and 'sqlite_store' in
  `hscc cluster history` …` (the value set is unchanged; only the name→value
  binding moved)
* P4 — `the route's SOURCE_STORE_ABSENT is 'store_missing', not the ruled
  'sqlite_store_absent' (t_447cc7fc). A wire code is what clients branch on:
  rewording it breaks them all, so it is a decision recorded here, not a reword
  inside one module`
* P7 — `the CLI invents keys the route does not have: ['src']`

First driver pass reported P1-P4/P6 as MISSED: pytest ids a dict-parametrized
case as `[source-ruled0]`, so my expected id `[source]` matched nothing — the
skill's misattribution trap, caught by printing the actual `FAILED` list. Fixed
in the test (`pytest.param(..., id="source")`, which is also the readable id a
report should quote) and the probes re-run to 7/7.

## Gate

* `hscc-api/tests/test_cluster_history_cli_parity.py` → 16 passed (12 before).
* `pytest -q hscc-api hscc_daemon -p no:cacheprovider`, runner interpreter
  (`~/.hermes/hermes-agent/venv/bin/python`) and the runner's posture → see the
  card comment for the count measured at this sha.
* `bash scripts/run_tests.sh` roster → see the card comment.
* **No changelog fragment**: test-only, nothing user-visible changed — the
  payloads, codes and messages on both surfaces are exactly what #425 shipped.
