# A pack killed before any leg is reaped leaks its `/tmp/hscc-legs-*` dir (t_b94a2b39)

Card: `t_b94a2b39`. Parent `t_34c5a3ed` — whose glob flake is settled and is **not**
re-litigated here. Base: `origin/main@5d5ab25d`.

Decision: **both halves, ordered** — a bounded age-floor sweep at runner start is the
only thing that can reclaim the observed leak, and a fatal-signal trap family closes
the one catchable gap. Full reasoning in §4.

---

## 1. What the two pre-existing dirs actually are — and they are not the runner

Both dirs the parent card documented as pre-existing:

| dir | birth (2026-11-25) | contents |
|---|---|---|
| `/tmp/hscc-legs-2MIxna` | 07:25:42 | `1-hscc-commands.log`, `2-sparkrun-hermes.log` — **no `legs.tsv`, no `.rc`** |
| `/tmp/hscc-legs-AiHLjx` | 07:25:45 | identical shape |

Each per-leg log holds exactly one line: `7 passed in 0.2s`.

That string is not pytest output. It is the **stub interpreter's** success line,
`scripts/tests/test_run_tests_leg_parallelism.py:66`
(`echo "7 passed in ${secs}s"`), and `0.2` is that stub's *default* sleep
(`secs=0.2`, `:54`). No real leg emits it.

The shape rules the shipped runner out independently: `run_tests.sh:691`
truncates `legs.tsv` (`: > "$LEG_PIDFILE"`) **before** the cleanup trap is installed
(`:693`) and before any leg is launched, and appends a row at every launch (`:900`).
A directory containing per-leg logs therefore cannot exist without `legs.tsv`. These
have none. They are **test-harness residue** (a fabricated/fake-sibling pack dir),
not the residue of a gate run.

So the parent card's premise — "packs do sometimes leak their log dir" — is only
half right: the litter it documented is mostly *not* the runner. The leak itself is
real (§2), but it has a different signature.

## 2. The leak, reproduced deterministically

Method: the shipped `scripts/run_tests.sh` driven end-to-end through its own
`HSCC_TEST_PY` seam (the same seam the parallelism tests use), roster
`hscc-commands sparkrun-hermes memori_byodb`, `HSCC_TEST_PARALLEL=2`, three 30 s
stub legs, the runner killed 6 s after two legs were live, one signal per trial, in
a fresh session (`start_new_session=True`), cwd = worktree. Probe:
`scripts/tests/support/leg_tmp_signal_probe.py` (in this PR — it is the harness that
produced the table, kept so the numbers below are re-runnable).

| signal | runner rc | dir left behind | shape of the leaked dir |
|---|---|---|---|
| `TERM` | 143 | no | — |
| `INT` | 130 | no | — |
| `HUP` | 129 | no | — |
| `PIPE` | 141 | no | — |
| **`QUIT`** | 131 | **yes, 3/3 trials** | `legs.tsv` 60–64 B (two launch rows), `0-hscc-commands.log` 0 B, `1-sparkrun-hermes.log` 0 B |
| **`KILL`** | 137 | **yes, 1/1** | identical shape |

`TERM`/`INT` being clean is exactly why the parent card's candidate test passed in
isolation: `trap on_sigterm TERM` (`:496`) and `on_sigint` (`:838`) both `exit`, and
the `EXIT` trap at `:693` then removes the dir.

The `QUIT`/`KILL` shape is the parent card's observed signature: `legs.tsv` present,
per-leg logs present and **0 bytes**, no live holder. (Theirs had `legs.tsv` at 0
bytes too — the narrower window, a signal landing after the trap install (`:693`)
and the first `> "$logf"` redirect but before the launch row is appended (`:900`).)

### Why only these two

Isolated arm shells (only an `EXIT` trap installed, `sleep` in the foreground, one
signal each — `scripts/tests/support/exit_trap_signal_arm.sh` in this PR):

| signal | EXIT trap ran? |
|---|---|
| TERM, INT, HUP, QUIT, PIPE | **yes** |
| KILL | **no** |

So: **`SIGKILL` is the only death no handler in the runner can beat.** The `QUIT`
leak does *not* reproduce in the arm shell — it needs the shipped runner's state —
so it is pinned as shipped behaviour (§5), not explained as theory. Everything else
already reaches the cleanup.

## 3. The two candidates, scored against the measurement

**Candidate 1 — "make the TERM/INT handlers remove `$LEG_TMP` themselves, not
relying on `EXIT`": measured to be a no-op for this bug.** TERM and INT already
clean up (rc 143/130, zero leaks). The catchable gap is not TERM/INT; it is the
fatal signals the runner never installs a trap for (`QUIT` measured; `HUP`/`PIPE`
already clean by bash's own fatal-signal path). Adding a trap family for *those* is
worth doing, but it cannot be sold as the fix.

**Candidate 2 — a bounded sweep at runner start: the only thing that can reclaim the
observed leak**, because a `SIGKILL`ed shell runs no code at all. The card's own
warning is the design constraint: a naive `rm -rf /tmp/hscc-legs-*` deletes a live
sibling worker's pack, which is the shared-namespace assumption `t_34c5a3ed` removed.

The safety argument that makes the sweep sound without any cross-process
coordination: a pack's dir `mtime` is last touched by launch/reap activity
(`legs.tsv` append at `:900`, `.rc` writes at `:897`), and a pack's whole life is
bounded — the full roster at `p=2` is ~1093 s measured, the longest single leg
(`hscc-bootstrap`) ~700 s. A floor of **7200 s is 6.5× the longest pack that has
ever run on this host**, so a dir older than the floor cannot have a live writer.
Age alone therefore proves "no live holder" — no `lsof`, no pid-liveness, no
pid-reuse reasoning, and no assumption about who owns the dir beyond uid.

## 4. Decision

Ship both, in this order, and keep the sweep's safety argument in the code comment
where a future editor can break it:

1. **Bounded sweep, inside the parallel branch only, immediately before `mktemp`**
   (`run_tests.sh:688`). Removes `$LEG_ROOT/hscc-legs-*` owned by this uid, not a
   symlink, `mtime` older than `HSCC_LEG_SWEEP_MIN_AGE_SECONDS` (default 7200; `0`
   disables). Serial runs are untouched — the serial path makes no such dir, and
   `test_the_default_is_the_serial_stream_and_prints_nothing_new` pins that the
   serial stream gains no line. Reports only when it removed something, under the
   `parallel:` prefix already proven not to join the digest.
2. **Fatal-signal trap family.** One cleanup function, installed as the `EXIT` trap
   *and* as handlers for `HUP`/`QUIT`/`PIPE`, each re-raising the same `128+N` code
   the shell would have died with, so every rc a dispatcher or the queue reads
   stays byte-identical (129/131/141 preserved — pinned by test).

Not done, deliberately: no sweep on the serial path, no `lsof`, no pidfile, and no
change to any assertion in `t_34c5a3ed`'s test.

## 5. Proof

(red-before / green-after / mutation teeth)
