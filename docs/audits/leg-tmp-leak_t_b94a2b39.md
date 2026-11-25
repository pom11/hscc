# A pack killed before any leg is reaped leaks its `/tmp/hscc-legs-*` dir (t_b94a2b39)

Card: `t_b94a2b39` (parent `t_34c5a3ed`, whose glob flake is settled and is NOT
re-litigated here). Base: `origin/main@5d5ab25d`.

Status: FINDINGS + DECISION (below).

---

## 1. What the litter actually is (observed, on this host, 2026-11-25)

Two directories pre-dated this card:

| dir | birth | contents |
|---|---|---|
| `/tmp/hscc-legs-2MIxna` | 07:25:42 | `1-hscc-commands.log`, `2-sparkrun-hermes.log`, **no `legs.tsv`, no `.rc`** |
| `/tmp/hscc-legs-AiHLjx` | 07:25:45 | identical shape |

Both per-leg logs contain exactly one line: `7 passed in 0.2s`.

That string is decisive. It is not pytest's output — it is the **stub
interpreter's** success line in
`scripts/tests/test_run_tests_leg_parallelism.py:66` (`echo "7 passed in ${secs}s"`),
and `0.2` is that stub's *default* sleep (`secs=0.2`, `:54`). No real leg can
produce it. So the pre-existing pair is **test-harness residue**, not the residue
of a real gate run.

The third dir the parent card recorded (13:27:51) has the opposite shape:
`legs.tsv` present and **empty**, per-leg logs present and **empty**, no `lsof`
holders — which *is* a signature the shipped runner can produce (see §3).

## 2. Why the pre-existing pair is not the shipped runner

`scripts/run_tests.sh` creates `legs.tsv` at `:691` (`: > "$LEG_PIDFILE"`) — before
the trap at `:693`, before any leg launches — and appends a row per launch at
`:900`. A directory that holds per-leg logs therefore cannot exist without
`legs.tsv`. The pair has no `legs.tsv`.

## 3. Where the cleanup can be skipped — the paths

(exact evidence, measured)

## 4. Decision

## 5. Proof
