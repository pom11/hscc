# Home-path enforcement: the decision (t_34de41c3)

Parent: `t_c927b73e` (which shipped the home branch advisory). Guard measured at
`origin/main` @ `21da34a8`, using the **shipping guard** over `git ls-files`
(`HSCC_ADDRESS_GUARD_HOME_ENFORCE=1`), not `git grep`.

## Measured baseline

| | |
|---|---|
| home offenders | **198** |
| files | **87** |
| non-home (address/artefact) offenders | 0 |
| tracked files scanned | 2545 |
| `len(FORBIDDEN.pattern)` | **264** vs `MAX_PATTERN_CHARS = 4096` |

The card body said 197/86; the extra finding is `changelog.d/t_49827b5d.md:24`,
the one genuine operator path. **PR #812 that scrubs it is still OPEN**, so this
branch carries it too — it is scrubbed here as well (same one-line placeholder),
and if #812 lands first this diff is empty for that file.

Per-user (measured): `devuser` 53, `operator` 29, `u` 25, `spark` 23, `example`
19, `op` 15, `x` 14, `user` 5, `...` 4, `hooks` 3, `dev` 3, `<operator>` 1,
`you` 1, `.local` 1, `.hermes` 1, `someone` 1. Per-area: `ios-app` 61,
`hscc-bootstrap` 37, `hscc_daemon` 27, `scripts` 18, `_archive` 12, `install` 11,
then a long tail of 1–8.

## Decision 1 — do NOT widen `SANCTIONED_HOME_USERS` (option 2 rejected)

Every candidate name fails the card's own bar ("only if a name is provably not a
real account"):

* **`spark` is provably a real fleet account.** It is the ssh-user *default* in
  shipping code — `install/hscc-skills/hscc-model-onboard/scripts/onboard.py:441`
  reads `cfg.get("ssh_user", "spark")` — and the HF cache path under that
  account's home is the live path a production tool `cat`s on a real node
  (`onboard.py:503`, `:518`, `:532`). Sanctioning it would exempt the one name in
  this set that names a real machine account, and would exempt it in the
  operational docs where a real leak would most plausibly hide.
* `operator`, `example`, `x`, `u`, `op`, `dev`, `you`, `someone`, `user`,
  `devuser` are generic *shapes*, and the shape is the point: a real account
  could carry any of them. `t_3480956c` refused them because "banning every
  concrete user is strictly stronger"; nothing measured here overturns that.

Consequence: **all 198 are scrubbed, not exempted** (option 1).

## Decision 2 — the left-boundary lookbehind (option 3) is REJECTED

The card proposes a left-boundary lookbehind to kill relative-path false
positives, with the caveat that it must be "proven not to under-report". It
cannot be. The natural form `(?<![A-Za-z0-9._-])` was measured over the whole
tracked tree; it drops 10 findings, and **one of them is not a false positive**:

```
hscc-api/tests/test_repl_inject.py:67  the path under user 'x' at .hermes/bin/hermes
    preceding text: 'tty: "/bin/zsh\\n'
```

That is a home path sitting immediately after a `\n` escape inside a Python
string literal. In the *rendered* string it is at line start and is a genuine
home-path occurrence; in the *file bytes* the preceding char is `n`, a word
char, so the lookbehind refuses to match. A real leak committed as
`".../bin/zsh\n/Users/<operator>/.ssh/id_rsa\n"` would pass every gate — the
guard, the hook and the CI backstop alike. That is the same failure family as
the `.pyc` boundary trap this module already documents, and it is exactly why
`t_3480956c` refused `\b` on this branch. A lookbehind over the path-name class
is `\b` wearing a different hat.

**Rejected.** The 3 findings at the scratch tree's fake-home `hooks` directory
are worth less than one blind spot.

## Decision 3 — keep the one boundary tightening that cannot blind the guard

`(?=[A-Za-z0-9])` before the user component: a user-name component must start
with an alphanumeric. This is a *lookahead*, and a lookahead can only shorten
the reported match — it cannot make the guard skip a line it would otherwise
see, because the offence is still the `/Users/<user>` prefix that starts the
match. Measured: drops exactly 5 findings, all provably not accounts — the
three sites that spell a prose ellipsis as the user component
(`hscc-project/docs/COMMANDS.md:947`,
`hscc-project/flightdeck/core/self_update.py:109`,
`hscc-project/tests/test_sync.py:796`) and the two malformed
`<tmp>/home/<dot-prefixed>` fixture strings at
`hscc_daemon/tests/test_update_check_shadow.py:84,85`. No real POSIX account
starts with `.`. Findings gained: 0.
Pattern grows 264 → 298 chars, against a cap of 4096.

## Decision 4 — the remaining false positives are scrubbed with the placeholder convention

4 findings are false positives in *shape* but are not killable by a safe
lookahead. They are rewritten rather than exempted, using the repo's documented
angle-bracket convention (`<` and `>` are not path-name chars, so the
placeholder cannot match — same mechanism that makes `/Users/<operator>` safe):

* `…/t_9462260b/<home>/hooks` ×3 (`docs/audits/backup-retain-atomicity_t_9462260b.md:13,190`,
  `docs/audits/GOAL_LEDGER_2026-09-26.md:3113`) — the scratch tree's fake-home
  directory. The measured facts (entry counts, the false alarm) are untouched;
  only the literal segment becomes a placeholder.
* `/tmp/Users/<x>` in `scripts/tests/test_hermetic_spawn_guard.py:192` — a
  *negative* fixture ("this must not be treated as home-prefixed"); it keeps its
  meaning with the fixture user.

## Decision 5 — scrub targets, by audience

Not one replacement string: the right placeholder depends on whether a human or
a test consumes the path.

| context | replacement | why |
|---|---|---|
| test / fixture data | `/Users/hscc-fixture`, `/home/hscc-fixture` | the sanctioned fixture user |
| operational doc/template where the path is *used* | `/home/<ssh_user>/…` | keeps the instruction true; the reader substitutes their own account |
| live code that builds the path | derive it: `f"/home/{ssh_user}/.cache/huggingface"` | removes the literal AND fixes the hardcoded-user bug it was hiding |
| prose about the operator's machine | `/Users/<operator>` | the documented placeholder |

`_archive/` is scrubbed with the `<ssh_user>` form too: it is a frozen snapshot,
but it is also public and it names a real account.

## Known limitation this card does NOT close

`_HOME_TAIL_STOP` includes `)` — deliberate, so a Markdown link whose target is
an absolute home path still matches; the paren context is pinned by the
`link ](…)` case in `test_find_in_text_home_verdicts`
(`scripts/tests/test_address_guard.py`). The cost is that a *closing paren is
also a stop char*, so a home path followed by `)` truncates at the paren rather
than reporting the whole tail. Unchanged by this card.

## Status

* [x] baseline measured with the shipping guard (198 / 87)
* [x] decision recorded (this file)
* [x] pattern tightening `(?=[A-Za-z0-9])` + tests (`test_a_newline_escaped_home_path_still_trips_the_branch`
      is the test that goes red if anyone re-adds a left boundary)
* [x] scrub sweep — all 198 cleared, `scan_tracked` returns 0 — and
      `HOME_PATHS_ENFORCE = True`
* [x] redactor-side home coverage (the gap `t_c927b73e` left): unit mask,
      byte-identical anti-vacuity, and the end-to-end job leg in **both** postures
* [x] cross-file `MAX_PATTERN_CHARS` pin (279 of 4096 today, pinned under half)
* [x] the flip proved to bite: planted path → `--tracked` rc=1, `--staged` rc=1,
      and a guard that cannot run still rc=2

### Measured after the flip

`len(FORBIDDEN.pattern)` 264 → **279** (cap 4096). Tracked offenders: **0**.
`hscc_daemon/tests/test_no_real_addresses_committed.py scripts/tests`:
**744 passed, 4 skipped, 1 failed** — the failure is
`test_run_tests_leg_parallelism.py::test_the_per_leg_logs_do_not_pile_up_in_tmp`,
which snapshots a *global* `/tmp/hscc-legs-*` glob and therefore breaks whenever
another suite runs on the shared host. It passes in isolation (2/2 here) and no
file this card touches is on its path. Worth a flake card; not this diff.
