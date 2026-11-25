# An untrapped `SIGPIPE` kills a leg pack and orphans the legs in flight (t_5db4cbba)

Card: `t_5db4cbba`. Split off `t_b94a2b39` (`docs/audits/leg-tmp-leak_t_b94a2b39.md`
§6 LEFT item 1). Base: `origin/devops/t_b94a2b39@ff0eb34f` — **stacked on PR #825**,
because the `_on_fatal_signal` this card extends does not exist on `main` yet.

**Decision: route `SIGPIPE` through `_on_fatal_signal PIPE 141`.** The fear that
kept `t_b94a2b39` out is measured away below: the benign early-closing consumer it
worried about does not exist, because `| head` **already kills the pack today** with
rc 141. The trap cannot make that worse — it only adds the reap and the cleanup the
pack was already owed, and it costs 2.0 orphaned pytests per external `kill -PIPE`.

---

## 1. What the card was handed, and the one sentence it had to re-measure

`t_b94a2b39` routed `HUP`/`QUIT` through the fatal-signal handler and left `PIPE`
out, with the reason written into the handler's own comment:

> SIGPIPE is deliberately NOT routed here: it measured never to leak the dir, and
> trapping it in a 967-line script that pipes through `head`/`tr` changes error
> paths this card has no evidence for.

Two claims in that sentence, and the card's job was to test both:

* *"measured never to leak the dir"* — true of the posture it measured (one signal
  delivered to the runner's pid from outside), and **not true of the posture that
  produces a SIGPIPE in this script in the first place** (§3: 1/3 trials leaked).
* *"changes error paths this card has no evidence for"* — the enumeration (§2) is
  three pipes, and none of them is a path a PIPE trap touches.

## 2. The enumeration: how many pipe sites are there, really?

`run_tests.sh` is ~1060 lines. Every top-level `|` was split with quote state
tracked (`docs/audits/support/t_5db4cbba/pipe_sites.py`), and the result is small
enough to state as a fact rather than a survey — the three `case`-pattern `|`s are
not pipes, so the script has exactly **three** real pipes:

| line | the pipe | writer | can the consumer close early? |
|---|---|---|---|
| `scripts/run_tests.sh:518` | `$(ps … \| tr -s ' ')` in `_on_fatal_signal`'s banner | **`ps`** (external) | no — `tr` reads to EOF, and the `$( )` reads to EOF |
| `scripts/run_tests.sh:532` | same shape, in `on_sigterm` | **`ps`** | no |
| `scripts/run_tests.sh:979` | `$(ps … \| tr -d ' ')` in the leg-liveness probe | **`ps`** | no |

There is **no `| head`, no `| wc -l`, no `| grep`, no `| sed`** anywhere in the
script, and no pipe whose writer is a shell builtin. That matters because a
self-inflicted `SIGPIPE` needs both halves at once: the writer must be the SHELL
(a builtin performs its own write in the shell's process, so the closed reader
signals the shell rather than a child), and the consumer must be free to stop
reading. No site here has the first half, let alone both.

So the premise "a script that pipes through `head`/`tr`" describes the **caller's**
pipeline (`run_tests.sh | head -40`), not the script's interior. That reframing is
what unblocks the decision: the question is not "what does a PIPE trap do to
in-script pipes" (nothing — measured, §3 row 1–2) but "what does it do when the
runner's own stdout dies", which is the one self-PIPE shape that exists at all.

## 3. Measured, not reasoned

`bash` on this host is **3.2.57** (`/bin/bash`, the shebang's `env bash`), so every
number below is the interpreter the runner actually runs under.

**Trap semantics at each pipe shape** (`docs/audits/support/t_5db4cbba/pipe_trap_probe.py`;
`handler` = did a `trap … PIPE` fire):

| shape | untrapped rc | trapped rc | handler fired? |
|---|---|---|---|
| `$(ps … \| tr -s ' ')` — the only shape in the script | 0 | 0 | no |
| `for …; do echo; done \| head -c 4` (builtin writer, early closer) | 0 | 0 | no |
| `echo … \| tr -d ' '` | 0 | 0 | no |
| **the shell's own stdout is a pipe and the reader closes** | **−13 (=141)** | **−13 (=141)** | **yes** |

The first three rows are the whole answer to the card's fear: trapping `PIPE`
changes nothing at any pipe that exists, and a `| head` inside a command
substitution does not reach the parent shell either (the pipeline's writer is a
subshell; the parent survives with rc 0 in **both** postures). Only the last row
changes, and there the shell was already dying at 141 — the trap adds cleanup, it
does not add a death.

**Does the handler survive running *because* stdout is closed?** Its first act is
four banner `echo`s into the very pipe that closed. Arm:
`docs/audits/support/t_5db4cbba/closed_stdout_arm.py`, markers written on a
dedicated `exec 9>>` fd so no redirection can move them:

| posture | rc | shell sees | markers on fd 9 | `$LEG_TMP` removed |
|---|---|---|---|---|
| untrapped (today) | −13 | 141 | *(none — nothing ran)* | **no** |
| trapped (proposed) | −13 | 141 | `REAPED`, `CLEANED` | **yes** |

with `bash: line 19: echo: write error: Broken pipe` on stderr. Bash 3.2 reports
the write error and **keeps going**, so the reap and the cleanup still run. That is
the difference between an orphaning death and a clean one.

**The orphan counts — the red-before this card asked for.** Harness:
`docs/audits/support/t_5db4cbba/sigpipe_probe.py` — the shipped runner driven
through its own `HSCC_TEST_PY` seam, 3 legs, `HSCC_TEST_PARALLEL=2`, one posture per
trial, `SIGPROBE_REPEATS=3`, survivors counted with `ps` *before* the session is
taken down. Base tree = `ff0eb34f` (HUP/QUIT trapped, **PIPE not**).

| posture | what it models | dir leaked | **mean orphans** | runner rc |
|---|---|---|---|---|
| `external` | `kill -PIPE <runner pid>` (the `t_b94a2b39` posture) | 0/3 | **2.0** | −13 (141) |
| `head_live` | reader holds the pipe, then closes while legs are in flight | 0/3 | **1.0** | −13 (141) |
| `head` | `\| head -c 200` — reader closes at the banner | **1/3** | 0.0 | −13 (141) |
| `control_term` | same as `head_live`, killed with TERM | 0/3 | 0.0 | 143 |

Two of those numbers are corrections to the parent audit's record:

* **`PIPE` does leak the dir** — 1/3 in the `head` posture, shape
  `legs.tsv` 94 B, `0-hscc-commands.log` 19 B + `0.rc` 13 B, `1-sparkrun-hermes.log`
  19 B + `1.rc` 13 B, `2-memori_byodb.log` 0 B. "measured never to leak the dir" was
  an artefact of measuring only the external posture.
* **`head_live` needed a staggered plan to mean anything.** With a uniform 30 s
  plan it scores a *vacuous* 0 orphans, because the runner's next write to stdout
  falls exactly when those legs have just finished. The first run of this probe
  produced exactly that false negative; `SIGPROBE_PLAN` with a short first leg is
  what makes the runner write to a closed pipe *while the other two are still in
  flight*. A harness that cannot fail is not evidence.

**Green-after** (same harness, same base, one added trap line):

| posture | dir leaked | mean orphans | runner rc | banner named SIGPIPE |
|---|---|---|---|---|
| `external` | 0/3 | **0.0** (was 2.0) | **−13 (141)** | 3/3 |
| `head_live` | 0/3 | **0.0** (was 1.0) | **−13 (141)** | — |

`rc` is byte-identical because the handler re-raises with the default disposition:
`128+N` survives the trap, which is the constraint the card set (a dispatcher and
the merge queue parse 129/131/141; nothing here relaxes an rc assertion).

## 4. Decision, and the scope line

Route `PIPE` through `_on_fatal_signal PIPE 141`, alongside `HUP 129` / `QUIT 131`.
Rejected alternative: "leave `PIPE` untrapped and make the leg wrappers reap
themselves" — it cannot be done without a per-leg watchdog that kills a *process
group by remembered pid*, and pid reuse on a host where several cards run packs
concurrently is precisely the name-based-sweep hazard `run_tests.sh` already refuses
in its own comment. The handler-based reap is bounded, by pid, to legs this pack
started.

Not done, deliberately: `SIGKILL` still leaves the dir (no handler runs — pinned as
a shipped fact by `test_sigkill_leaves_the_dir_because_no_handler_can_run`, and
reclaimed by `t_b94a2b39`'s sweep), and the `trap - INT` window after the pack
(LEFT item 2 there) is untouched.

## 5. Proof

* **Red-before is the measured table**, both postures: `external` 2.0 mean orphans
  and `head_live` 1.0, on the base tree, with rc 141 in every trial — and the
  `closed_stdout_arm` pair showing that today the closed-reader death runs *no*
  cleanup at all while the trapped one reaches `REAPED` and `CLEANED`.
* **Green-after:** `scripts/tests/test_run_tests_leg_tmp_lifecycle.py` —
  **14 passed** (11 before this card; +1 parametrised `PIPE` case in
  `test_a_fatal_signal_the_runner_does_not_trap_still_cleans_up`, asserting
  rc 141 **and** `pack.orphaned_legs() == []`, +2 structural pins).
* **Teeth** (mutation battery, each mutant restoring the tree and asserting it
  restored; M0 is the no-op control and quotes its own pass count):

  | mutant | expected | measured |
  |---|---|---|
  | M0 no-op control | green | `1 passed` |
  | M1 delete the `PIPE` trap | red | `1 failed` |
  | M2 `PIPE` falls back to 130 | red | `1 failed` |
  | M3 `PIPE` trap installed for the wrong signal (`USR1`) | red | `1 failed` |
  | M4 insert `echo x \| head -1` into the runner | red | `1 failed` |

  M4 is the anti-vacuity check on the safety pin: the day someone adds a builtin
  writer in front of an early-closing consumer, the pin names the line and points
  at this audit instead of silently permitting it.
