# t_d1afc6e6 — the orch_waker identity literals: the ruling, and how it was measured

The `hscc-roles` leg has been red since `d3a7987d` (#587) and was **still red at
`origin/main 04586596`** — #589 merged over it because it touched neither
`orch_waker.py` nor the guard. As filed on `6e59aaf9`:

```
hscc-roles/tests/test_main_orch.py::test_no_hardcoded_old_profile_or_board_literals_outside_resolver
AssertionError: hardcoded main-orchestrator identity outside the resolver:
    hscc_daemon/orch_waker.py: GENERAL_PROFILE
    hscc_daemon/orch_waker.py: general-orch
1 failed, 355 passed in 8.27s
```

Reproduced here on a detached worktree of `6e59aaf9` before touching anything:
`1 failed, 22 passed` for that file alone.

## The question the card posed

Either the literals are legitimate — `orch_waker` is resolver-adjacent and
belongs in `RESOLVER_FILES` — or they are a leak that must route through the
resolver. Both readings were live because `hermes profile list` shows `general`
**and** `general-orch` (and `orchestrator`) on this host, so nothing in the
file itself says which name is sanctioned.

## Ruling: not legitimate, and no allowlist

`orch_waker.py` is a **consumer** of the main-orchestrator identity. It never
launches a pane with it; it only asks "is this the retired identity, so no pane
is expected here?" (PR #587's skip). The guard's whole purpose is that a
consumer cannot hardcode the identity it is supposed to resolve, so listing a
consumer in `RESOLVER_FILES` would grant exactly the licence the guard exists
to refuse — that file could then spell `"general-orch"` as a *value* and the
leg would stay green. Rejected.

`RESOLVER_FILES` stays `{"hscc-roles/main_orch.py", "hscc-roles/orchestrators.py"}`.

## What the two offenders actually were

`git log -L` pins both to `d3a7987d` (#587, the paneless-skip card) — the
resolver card `t_c6eccd49` wrote the guard, and a later daemon-leg card walked
the literals in. Neither is a policy leak:

1. **`mod.LEGACY_GENERAL_PROFILE`** (one line, `legacy_cluster_identity`). The
   resolver already owns this name behind an accessor,
   `main_orch.legacy_general_profile()`, and that accessor's docstring states
   the rule verbatim: *"A function, not a bare constant reference, so modules
   that migrate the legacy profile never spell the old name into new code."*
   `hscc-roles/migrate_main.py:144` obeys it; `orch_waker` was the one caller
   that reached past the accessor for the constant — and the constant's *name*
   carries the forbidden `GENERAL_PROFILE` substring. Fixed by calling the
   accessor. Same value by construction; same fail-open posture, because the
   surrounding `except Exception` still answers False if a deployed resolver
   predates the accessor (checked the live
   `~/.hermes/plugins/hscc-roles/main_orch.py:124` — it has it, so the live
   daemon is unaffected either way).
2. **`general-orch`** — prose only, in two docstrings quoting
   `hscc-roles/main_orch.py`'s migration note. The guard excuses docs and
   `tests/`; it does not excuse prose in a production `.py`, and it should not
   — a production file that names the retired wire identity invites the next
   reader to hardcode it. The sentences now name the accessor instead of the
   wire name, so the reasoning ("retired, never deleted", "kept on disk, never
   panned") is intact without the string.

## The policy question, answered from state rather than opinion

Nothing here re-decides which profile is the main orchestrator. That is settled
by the resolver plus the operator's stored record:
`~/.hscc/main-orchestrator.json` holds `{"name": "general"}`, so
`main_profile()` / `main_board()` are `general`, and `general-orch` +
`orchestrator` are the retired-but-present identities the resolver still names.
That is precisely what `legacy_cluster_identity` is for: `resolve_orch` approves
a profile on directory existence alone, so the waker needs to know which of the
profiles that still exist on disk hscc itself retired. `hscc-api/tests/
test_session_resolver.py::test_resolve_explicit_legacy_general_orch_still_resolves_catch_all`
keeps the other half pinned — an *explicitly* named legacy profile still
resolves to the legacy catch-all, deliberately spelled as a literal in a test.

So: `general` sanctioned; `general-orch`/`orchestrator` recognised as retired.
Behaviour of this diff: none.

## Measurements (both legs, with shas and the runner's posture)

Measured on a detached throwaway worktree of each base before editing, and
re-measured after rebasing onto the new main:

| sha | leg | result |
|---|---|---|
| `6e59aaf9` (pristine, as the card filed it) | `hscc-roles/tests` | 1 failed, 355 passed |
| `04586596` (pristine; #589 merged over this red without touching `orch_waker.py`) | `hscc-roles/tests/test_main_orch.py` | 1 failed, 22 passed |
| `88c91b5d` rebased on `04586596` | `hscc-roles/tests` | **356 passed** in 5.4 s |
| `88c91b5d` rebased on `04586596` | `hscc_daemon/tests` | **4676 passed, 1 skipped** in 179 s |

(the 1 skip is the standing §3.1 wake-claim upstream-gap gate in
`test_orch_waker_wake_claim.py:1495`, unrelated to this diff)

Interpreter: `~/.hermes/hermes-agent/venv/bin/python`, with the five
`SUITE_ENV_UNSET` unsets from `scripts/run_tests.sh`.

## Mutation probes (on a scratch copy — the repo tree was never mutated)

| probe | mutation | expected | result |
|---|---|---|---|
| A | re-spell the literal in `orch_waker` (`{"general-orch", …}`) | guard reddens → the file is **not** exempt | `1 failed, 22 passed` ✔ |
| B | none (control) | both files green | `23 passed`, `31 passed` ✔ |
| C | `legacy_general_profile()` renamed away in the resolver (an old deployed `main_orch`) | the paneless test reddens → it really reads the resolver | `1 failed, 30 passed` ✔ |

## Follow-ups noticed, deliberately not done here

- **Cross-leg drift.** The guard lives in the `hscc-roles` leg but scans
  `hscc_daemon/**`; #587 therefore merged with the roles leg red while its own
  daemon leg was green. The roster in `scripts/run_tests.sh` does cover it, so
  this is a merge-gate question ("did the merge run the roster, or one leg?"),
  not a guard gap. Worth its own card before the next daemon-leg card repeats
  it.
- **`hscc-roles/orchestrators.py:65,135`** still read
  `main_orch.LEGACY_GENERAL_PROFILE` (the constant) rather than the accessor.
  Allowed today because that file is in `RESOLVER_FILES`, and the hscc-api
  resolver test argues *for* spelling the wire name in the resolver's
  neighbourhood (a rename-detecting pin). Left alone on purpose; if the
  resolver is ever tidied, this is the place to decide it once.
