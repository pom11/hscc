# Closing the round-4 publish channels: child authorship + `__pycache__` (t_38fd345f)

Status: implementation complete at the tip named in the completion comment; the
authoritative two-interpreter gate is stamped there (see Results).

Follow-up to `ci-backstop-address-guard-t_9a4b7687.md`. Reviewer round 4
(run 966) reproduced two publish channels *inside* the round-3 child boundary,
on py3.11.16 + py3.13.7, through the shipping step script and the redactor CLI
at gated tip `8d73cf1f` (now an ancestor of `main`, released as 2.5.8):

* **D0** — the child protocol validated the FORM of a verdict, never its
  AUTHORSHIP: a guard module body could write a well-formed `OK` line itself
  and `os._exit(0)`, so the parent saw exactly one honest-looking verdict and
  honoured it. Advisory job GREEN, value in the public log.
* **D1** — the child's `spec_from_file_location` import honoured a committed
  `scripts/__pycache__/*.pyc`; an unchecked-hash pyc (PEP 552, official
  `compileall --invalidation-mode unchecked-hash` artifact) is trusted WITHOUT
  reading the source, so the child redacted with the pyc's pattern while the
  CLI's detection stayed honest. Advisory GREEN, value printed.

## Baseline at the pre-fix tip (measured)

Reproduced against the shipped tip with the reviewer's own batteries
(`probe_r4_forged_exit.py`, `probe_r4_pyc_step.py`), matching the reviewer's
counts exactly:

| battery | py3.11.16 (venv) | py3.13 |
| --- | --- | --- |
| `probe_r4_forged_exit.py` | **6 leaks** | **6 leaks** |
| `probe_r4_pyc_step.py`    | **1 leak** (`alone` leg) | **3 leaks**, incl. step legs |

PATH `python3` is 3.13.7, so the D1 leak class was live on the interpreter CI
actually uses. Baseline control: the pre-fix code honours the forged verdict
(rc=0, value printed) — the tests below fail against it.

## The fix (design)

Three rails in `.github/scripts/redact_guard_report.py`, one per assumption the
reviewer broke:

1. **Source authority (closes D0 from the parent side).** The parent reads the
   guard's bytes and derives `FORBIDDEN` itself with `ast` — zero execution.
   ONE shape is accepted: module-level `FORBIDDEN = re.compile(<str literal>)`
   or `…(<str literal>, <int literal>)` (also annotated). The gates are
   NODE-SHAPE gates (`ast.Constant`), not an evaluator: what
   `ast.literal_eval` agrees to compute is version-dependent (measured here: it
   refuses a str-concat `BinOp` but FOLDS a numeric one, so a flags expression
   like `2-0` passes an evaluator check) and on an f-string it RAISES — an
   evaluator-based check turns that raise into a traceback in the log instead
   of a clean rc=3. Every deviation (`FORBIDDEN = _m.FORBIDDEN`, f-string,
   concat, name, call, attribute, keyword flags, 3 args, out-of-range flags,
   unparsable source) fails closed. Adjacent literals fold into ONE
   `ast.Constant` at parse time, which is how the shipped guard's multi-line
   pattern passes.
2. **Agreement (closes D0 from the verdict side).** The child's verdict must
   equal the source-derived pattern by COMPILED equality — `.pattern` and
   `.flags` of two `re` objects, never the `(text, flags)` tuple the AST saw
   (a bare `re.compile(text)` gains `re.UNICODE` (32) at compile time: the
   tuple comparison fails on the repo's own shipped guard, measured on both
   interpreters). The pattern used for masking is the SOURCE-DERIVED one, so
   even a verdict that happens to name the real pattern cannot steer anything;
   a forged one dies at agreement (`PatternDisagreement` → rc=3, distinct
   operator sentence, payload never quoted). The exactly-one-`OK` count gate
   stays: with the last-emit assumption gone, two verdict lines are not a
   verdict even when both name the real pattern.
3. **The child executes the source the parent read (closes D1 + the
   file-swap race).** `importlib`/`SourceFileLoader` are gone from
   `CHILD_SOURCE` entirely. The parent passes the sha256 of the bytes it
   parsed as argv; the child RE-READS the file, raises BEFORE any `OK` can be
   emitted unless its bytes hash to the pinned digest, and `compile()`s
   exactly those bytes — `compile()` of a byte string neither consults nor
   writes a cache, so the cache directory is not merely invalidated, it is not
   on the path. Deliberately NOT stdin transport: the round-3 boundary pins
   `stdin=DEVNULL` and the descriptor posture, and re-opening stdin would
   reintroduce a write-block hang vector.
4. **Defense in depth: cache refusal.** While an unchecked-hash
   (`flags & 0b11 == 0b01`) pyc sits on the guard's cache name the CLI refuses
   to redact at all (`PatternCacheShadowed` → rc=3 + `CACHE SHADOW` operator
   sentence, invoked in `load_pattern()` BEFORE the source is trusted —
   call-order pinned). Timestamp (`0b00`) and checked-hash (`0b11`) artifacts
   — what local pytest runs legitimately write into `scripts/__pycache__`
   (measured) — must NOT fail the job; refusing them would turn
   defense-in-depth into a CI-wide outage.

## Results at the new tip

* Reviewer round-4 batteries: `probe_r4_forged_exit.py` **TOTAL LEAKS: 0**
  and `probe_r4_pyc_step.py` **TOTAL LEAKS: 0**, both py3.11.16 and py3.13,
  all legs `rc=3` fail-closed on the controls, step-ENFORCED and alone.
  `probe_r4_forged_ok.py` and `probe_r4_site.py`: 0 leaks.
  `probe_r4_child_raw.py`: `LEAK? False`.
* Round-2 battery (`probe_r2.py`): 9/9 `ok rc=3` on both interpreters (the
  round-1 channels are inside it: `r1_str_exc`, `r1_warning`).
* `probe_r4_pyc.py` crashes with `AttributeError: module 'importlib.machinery'
  has no attribute 'BYTECODE_MAGIC'` — a **probe-side defect**, reproduced
  identically against the untouched `main` checkout (it never ran against any
  implementation). The channel it targets is covered by `probe_r4_pyc_step.py`
  and `probe_r4_site.py`, which both pass.
* Card suite: **122 passed** on py3.11 and py3.13
  (`.github/scripts/tests/test_redact_guard_report.py`). The 32 round-4 cases (13 new test functions, 122 collected total):
  16-case source-deviation matrix (incl. the numeric-`BinOp` flags case that
  kills the evaluator mutant), acceptance shapes incl. annotated + last-binding
  + compiled-vs-tuple pin, forged-OK end-to-end (honoured-path proof that the
  source pattern is what masks), two-OK count gate isolated from agreement,
  pyc refusal matrix + benign-pyc-must-not-fire + call-order pin, digest pin
  exercised through `_child_pipe` (mismatch ⇒ `ERR RuntimeError`, never `OK`),
  structural pins on `CHILD_SOURCE` (no loader; digest before first `emit("OK"`)
  judged on comment-stripped code.
* Mutation battery (each mutant weakens ONE rail; the named test must fail):
  `drop_agreement`, `drop_digest`, `drop_cache_check`, `eval_instead_of_shape`
  (both gates reverted to `ast.literal_eval`), `first_ok_wins` — **all five
  CAUGHT**, suite green after restore. Two battery bugs found and fixed during
  this: a unit test of `_refuse_if_cache_shadows()` cannot catch a removed CALL
  SITE (the e2e call-order pin is the catcher), and a mutant that reverts only
  the text gate does not test the flags gate.
* `scripts/tests`: 97 passed. `python3 scripts/address_guard.py --tracked`:
  rc=0 on the tree. `changelog_fragments.py check`: clean.
* Authoritative gate: `scripts/run_tests.sh` from a clean DETACHED worktree at
  the frozen stamped tip, `HSCC_TEST_PY` = p311 venv and p313 — stamps and log
  paths are in the completion comment (`RUN_TESTS_RC=0` required on both).

## The residual, named so round 5 cannot re-open it

* **Guard integrity, not this boundary** (unchanged from the parent audit, in
  the reviewer's own framing): a guard whose `FORBIDDEN` has been *weakened* —
  narrowed, or replaced by one matching nothing — still redacts "correctly" and
  the job passes. Detection correctness is the guard's own battery and
  `scripts/tests/test_address_guard.py`; this boundary guarantees only that a
  real address never reaches the log. A source-authority design makes this
  class *harder* to attack than the round-3 one (the pattern now has to be
  stated in the tracked source, and a committed pyc is inert), but does not
  eliminate it: the tracked source is still the trust root.
* **The digest pin covers a swap between the parent's read and the child's
  read**, not a TOCTOU inside the child's own `open()` — the child hashes what
  it opens and compiles what it hashed, in the same `try` block, so there is no
  window between them; the window that *is* closed (parent read → child read)
  is the one the protocol needed.
* **`probe_r4_pyc.py` is broken upstream** (reviewer scratch, 3.11+ API drift);
  its channel is covered by the two passing pyc probes. If round 5 re-runs it,
  read the `AttributeError` as probe defect, not regression.
* **Local `scripts/__pycache__` timestamp pycs remain on dev machines by
  design** (pytest imports the guard bare). The refusal targets only the
  dangerous class; widening it would break every honest `--tracked` run.
