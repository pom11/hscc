# NOTES — t_4e368d92

`main` was red on two gates this card was filed against. One was real and is
fixed here; the other had already been fixed on `main` by a commit that landed
after the card was written.

## Gate 1 — the sentinel profile: the test was the stale half

`hscc-api/tests/test_session_resolver.py::test_resolve_general_sentinel_is_honest`
asserted `profile == "general-orch"` for a no-project / `"general"` session. It
answered `"general"`.

**Which side is the contract was decided by reading the resolver, not by which
commit moved last:**

| fact | where |
|---|---|
| `None` / `"general"` → `main_orch.main_identity()` | `hscc-roles/orchestrators.py:133-134` |
| identity = stored main name for profile **and** session **and** board, default `general` | `hscc-roles/main_orch.py:112-121`, `:97-109` |
| `general-orch` reachable only when named outright | `hscc-roles/orchestrators.py:136` → `_legacy_general_identity()` `:61-69` |
| docstring: "explicitly requested, **never the default**" | `hscc-roles/orchestrators.py:124-126` |
| `GENERAL_PROFILE` / `GENERAL_BOARD` gone; no-hardcoding grep guard | `hscc-roles/tests/test_main_orch.py:190-207` |
| resolver-level behaviour already pinned by the roles suite | `hscc-roles/tests/test_main_orch.py:73-89` |

So t_c6eccd49 / #432 moved the profile deliberately and the test never followed.
The fix re-asserts the expectation and rewrites the docstring; the assertion
itself is **not** dropped — the card forbids that, and it is the thing that keeps
the sentinel honest about which profile a `None`-project session lands in.

### The hole underneath (this is the part worth reading)

`main_name()` reads `~/.hscc/main-orchestrator.json`, and **the test never pinned
`HSCC_DIR`**. So it was reading the operator's real main-orchestrator state —
against the file's own header promise that "no test touches the operator's real
registry, profile tree".

It only passed on a machine where bootstrap has never stored a name (verified:
`~/.hscc/main-orchestrator.json` does not exist here). The first operator to
answer the stage-3 interview with a custom name would have broken it
spuriously — and a naive "fix" that only flips the expected string would have
shipped exactly that landmine, green on the CI box and red on the operator's.

So the test now pins `HSCC_DIR` to an empty dir, and a second case pins the
**stored**-name path (`None`, `"general"` and the name itself all resolve to the
stored name). Without the second case, re-hardcoding `general` would pass — which
is the regression the assertion exists to catch.

### Mutation probes — 4/4 bite

Run twice, identical; source restored and `git status` clean after.

| # | mutation | result |
|---|---|---|
| A | sentinel answers `_legacy_general_identity()` instead of `main` | red |
| B | `main_identity()` ignores the stored name (`name = DEFAULT_NAME`) | red |
| C | legacy catch-all branch removed | red |
| D | the `HSCC_DIR` pin removed, with an external stored name present | red |

**Probe cost worth repeating:** the driver restored files with `git checkout --`,
which also reverted my *uncommitted* test fix — so the first commit said
`1 file changed, 27 insertions(+)` (the changelog fragment only) while all four
probes had reported CAUGHT against the reverted file. The rule is in the
`mutation-probing` skill already (from t_2fcf27ad); the corollary — **commit the
edit under test before the first in-place probe** — is now recorded there too.

## Gate 2 — already green on main; nothing to do

Card says `changelog_fragments.py check` → rc 2 on `t_c6eccd49.md`. Reproduced
exactly at the card's `b4fd8764`:

```
malformed fragment: t_c6eccd49.md: kind 'Added' body contains 'kind: Verified' —
missing `---` separator between entries
RC=2
```

But `main` moved from `b4fd8764` to `7c51a316` while the card sat, and `7c51a316`
(`bootstrap: main orchestrator board is the kanban default`, t_7b55db57) already
inserted the missing `---` at `changelog.d/t_c6eccd49.md:27`. At the tip:
`OK — [Unreleased] is pointer`.

No change in this branch touches gate 2 or any other card's fragment. The card
was filed against a tip that is no longer current — **only gate 1 was still real.**

## Gate numbers (branch `wt/t_4e368d92`, commit `524f3bee`)

Interpreter `~/.hermes/hermes-agent/venv/bin/python` (the runner's own `PY`).

- touched file: **12 passed** (10 before)
- `hscc-roles/tests/`: **310 passed** — includes the no-hardcoding grep guard,
  which tolerates the legacy name appearing in a test (the guard skips any path
  containing `tests`, `hscc-roles/tests/test_main_orch.py:186-187`)
- `hscc-api/tests/`: **1225 passed, 1 failed, 1 skipped** in 322 s
- `changelog_fragments.py check`: `OK — [Unreleased] is pointer, 46 fragment
  entries consistent`
- `bash scripts/run_tests.sh`: **9/12 legs green, 3 red** (exit 1). Full log:
  `~/.hermes/profiles/backend-engineer/cache/scratch/t4e368d92_roster.log`.

### The 3 red legs are main's own, not this branch's

Four failing tests, **each reproduced on an untouched worktree at `7c51a316`**:

| leg | test | also red at `7c51a316`? |
|---|---|---|
| hscc-bootstrap | `test_bootstrap_isolated.py::test_fresh_home_says_not_wired_instead_of_claiming_success` | yes (1 failed in 14.84 s) |
| hscc-api | `test_contract_swift_routes.py::test_audit_doc_citations_still_point_inside_their_handler` | yes (1 failed, 20 deselected) |
| scripts | `test_formula_dep_drift.py::test_formula_resources_match_requirements_sot` | yes |
| scripts | `test_site_landing.py::test_page_command_region_matches_the_generated_one` | yes |

`test_formula_resources_match_requirements_sot` reads
`/Users/desac/dev/homebrew-hscc/Formula/hscc.rb` — outside the repo, operator-side
tap drift (`h2` pinned in `requirements.txt`, no `resource` block). The bootstrap
one is the known "fresh home / degraded mode leaks real host state" class.
The api one is t_342b3ea3's.

The api leg on **this** branch fails on exactly one test, and it is that
citation test (`grep` of the leg section returns only that header) — so this
branch adds **zero** red and clears the sentinel red it was filed for.

**Consequence for the fleet:** `run_tests.sh` cannot be green for *any* branch
until the citation card merges, and the bootstrap + scripts reds need their own
cards — they are not this card's scope and are not caused by it.


## LEFT

Nothing for this card. Two facts for whoever sequences the next gate:

1. **t_342b3ea3** (audit-doc citation) — card completed, its PR must merge before
   any branch shows a green `run_tests.sh`.
2. **Gate-2-class staleness:** this card, and any card quoting `b4fd8764`, should
   re-run its repro at the *current* tip before work starts. One `git log --oneline -3`
   would have shown the separator fix had already landed. Worth a line in the
   gate-filing template.
