# Address guard: the binary policy — t_3e6d3db7

**Card:** `[security] address guard is blind to binary blobs — a committed .pyc can carry a real address`
**Branch:** `wt/t_3e6d3db7` · **Repo:** pom11/hscc (PUBLIC)

Real addresses redacted per the documented placeholders: LAN `10.0.0.x`, tailnet
`100.64.0.1`. Nothing in this file carries a real address, and every fixture
address in the tests that come with it is assembled at runtime so this repo's
own guard cannot trip on its own test suite.

---

## 1. The gap

`scripts/address_guard.py` treated any NUL-bearing blob as out of scope:

```python
if b"\0" in data:
    return []          # "binary — skip", same intent as SKIP_SUFFIXES
```

Both local controls call that one function — `.githooks/pre-commit` via
`scan_staged()`, and the CI backstop (t_9a4b7687) plus the pytest gate via
`scan_tracked()` — so **every** control inherited the blindness. A committed
`.pyc` was invisible to all of them.

That is not hypothetical for this repo. Before the 2026-10-09 history rewrite,
`hscc-provision/__pycache__/hscc.cpython-313.pyc` carried a real address in
published history. A `.pyc` embeds the string constants of the source it was
compiled from, so the address is *inside* the blob in exactly the form a text
scan cannot see, and `.gitignore` is irrelevant once the blob is tracked: a
fresh clone or a CI checkout gets what was committed.

## 1b. The operator's probe, and where my reproduction differs

The orchestrator ran the pre-rewrite leak blob —
`hscc-provision/__pycache__/hscc.cpython-313.pyc` as committed in `0f165f7c`,
46762 bytes — through both detectors and reported:

* a plain `192\.168\.88\.\d{1,3}` over the bytes: **finds** a live-LAN address;
* `address_guard.FORBIDDEN`: **0 matches**, even decoding the blob as text.

I reproduced it independently against the same object (probe scripts in the task
scratch dir; they print counts and byte-context classes only, never values).
**Point 1 reproduces exactly.** Point 2 did not on my first attempt: I got **1
match** from `FORBIDDEN`, from a source-compiled import *and* a normal import (so
this is not t_38fd345f's `__pycache__` shadowing vector — I checked `__file__`,
`__cached__`, and pattern identity between the two loads). My first write-up of
this attributed the difference to the interpreter/marshal version and claimed I
"could not make the shipped pattern miss that blob". **Round-1 review showed both
of those statements were wrong, and I reproduced the correction myself on two
interpreters:**

| decode of the same 46762 bytes | chars | bytes not preserved | `FORBIDDEN` | naive |
|---|---|---|---|---|
| `utf-8`, `errors="ignore"` (what `scan_blob` does) | 42072 | 4332 | **1** | 1 |
| `latin-1` (faithful, one char per byte) | 46762 | 0 | **0** | 1 |

Same object, same pattern, same interpreter (measured on 3.11.16 and 3.13.x,
identical results). **The discriminator is the decode, not the interpreter.** The
mechanism, measured:

* the address's **true** neighbours in the blob are U+000E before and byte 0xDA
  after. 0xDA decodes as `Ú`, a word character, so over a faithful decode the
  trailing `\b` fails → **0 matches**;
* the lossy decode does not preserve 4332 of the 46762 bytes — `utf-8/replace`
  yields 4323 U+FFFD, so there are 4323 invalid byte sequences — and the character
  left after the address is no longer the true one but a leftover control byte
  (ord 5), which is non-word, so both `\b` hold → **1 match**. (The review
  recorded "4690 bytes dropped"; that figure is the *character*-count delta between
  the two decodes — 46762 − 42072 — not a byte count. Both measurements are the
  same measurement.)

So the operator's 0 is what a **faithful** decode yields, and my 1 was an
artifact of the guard's lossy decode. Three consequences, all of which make the
policy *more* defensible than my first write-up did:

1. **Every content-scanning form has a measured miss, and every one of them misses
   the accident that actually matters.** Run the shipping detector over the two
   blobs in every form an extract-and-scan could take:

   | detector form | historical blob `0f165f7c` | fresh-compile blob (the realistic accident) |
   |---|---|---|
   | naive `192\.168\.88\.\d{1,3}` over bytes | 1 | 1 |
   | `FORBIDDEN` over raw bytes | 1 | **0** |
   | `FORBIDDEN` over `utf-8/ignore` (the shipped decode) | 1 | **0** |
   | `FORBIDDEN` over `latin-1` (faithful) | **0** | **0** |
   | name-based refusal (this card) | refused | refused |

   A bytes-pattern scan is the obvious implementation and it misses the
   fresh-compile case — because `\w` in bytes mode is ASCII-only, and there the
   trailing byte *is* ASCII `N`. A decoded scan misses it under both decodes, and
   over-decodes the historical blob only by accident. So the historical blob's
   `FORBIDDEN`=1 under the shipped decode is not a detection: it is an artifact of
   which bytes a lossy codec chose to discard, and the same object reads 0 under a
   faithful one. The refusal is the only row with no measured miss.
2. My original table in this section was measured on decoded *strings*, so for the
   faithful-decode row it described a context that the decode itself had created.
   Re-measured on **raw bytes** — the only faithful representation — with the
   marshal-shaped trailing byte:

   | byte immediately before the address | naive (bytes) | `FORBIDDEN` (bytes) |
   |---|---|---|
   | `\x00` (opcode framing) | 1 | 1 |
   | `\x0e` (non-word) | 1 | 1 |
   | `\n` (line-anchored, i.e. ordinary text) | 1 | 1 |
   | ` `, `=`, `,`, `"` (the realistic text contexts) | 1 | 1 |
   | ASCII digit `5` (word char) | 1 | **0** |
   | ASCII letter `z` (word char) | 1 | **0** |

   The realistic **text** miss class is empty — every text separator is a
   non-word byte — which is why t_1b7b3166 records the miss class as
   binary-framing-only rather than a live text exposure.
3. The **fresh-compile** case — the realistic accident, and the one that decides
   the policy — misses under *every* decode, faithful or lossy. `py_compile` of
   `NAS = <real-shaped address>` gives a 200-byte blob in which the naive scan
   finds the address and `FORBIDDEN` finds **0** over the raw bytes, over
   `utf-8/ignore`, over `utf-8/replace` and over `latin-1`. The neighbour before
   is U+000E (leading `\b` holds) and after is `N` — marshal writes the next
   interned name with no delimiter — so it is the *trailing* boundary that fails:
   relaxing only the leading one still matches 0, relaxing only the trailing one
   restores 1. Meaning: **FORBIDDEN's `\b` assume the address is delimited the way
   text delimits it, and undelimited binary framing breaks that assumption.**
   Extract-and-scan would inherit exactly this blind spot.

So:

* `test_a_genuine_compiled_pyc_defeats_the_text_detector_so_refusal_is_required`
  (`scripts/tests`) compiles real source at runtime via `py_compile` and pins all
  three facts: the naive scan sees the address in the blob, `FORBIDDEN` does
  *not*, and the name-based refusal refuses it anyway. It carries a hair-trigger
  on the second assertion — if a future `FORBIDDEN` ever matches a compiled blob,
  that is precisely the moment extract-and-scan becomes arguable again, and the
  test says so rather than letting the reasoning rot silently.
* `test_a_genuinely_compiled_pyc_is_blocked_by_the_hook` (`hscc_daemon/tests`)
  does the same through a real `git add -f` + `git commit`.

The consequence for §3 is that the refusal is not merely *cheaper* than
extract-and-scan, it is *strictly stronger*: for the exact blob class the card
names, scanning the bytes is not a fallback, it is blind.

A corollary the follow-up card (t_1b7b3166) should own: this is a **detector-side**
finding, not only a binary-side one — the trailing `\b` is what fails. I first
wrote that any text file with a word character after an address (a minified JSON
blob, a CSV column, a compacted log line) would slip past the same way, then
measured it: those contexts all put a *non-word* separator after the address
(`"`, `,`, ` `, `=`), so the **realistic text miss class is empty** and the
exposure is binary framing only. That is a recordable outcome for t_1b7b3166 rather
than a live text hole. Changing the boundary would still be a change to the single
source of truth and to every pin built on it, which is bigger than this card's
scope; carding it rather than absorbing it.

## 2. Measured, not assumed

Against `origin/main` at `3c20990c` (2.5.8), from a clean worktree:

| measurement | value |
|---|---|
| tracked files (`git ls-files`) | 1096 |
| total tracked bytes | 13.2 MiB |
| NUL-bearing (binary) tracked files | **2** — `assets/hscc.png`, `hscc-project/docs/assets/banner.png` |
| tracked build artefacts (`*.pyc`/`*.pyo`/`__pycache__/`/`*.so`/…) | **0** |
| `scan_tracked()` wall time | **0.121 s** |
| offenders in the tracked tree | 0 |

Two things follow. Current exposure is zero — this card keeps it that way, it
does not clean anything up. And the whole tracked tree is two binary files, so
the cost of any policy I pick here is essentially free and stays free.

## 3. The policy (decided)

**Refuse to track a build artefact of a known-risky type. Do not extract strings
from arbitrary binaries.**

Three classes, three verdicts:

| class | how recognised | verdict |
|---|---|---|
| **build artefact** | path name: `*.pyc` `*.pyo` `*.pyd` `*.o` `*.a` `*.so` `*.dylib` `*.dll` `*.class` `*.jar` `*.egg` `*.whl` `*.pyz`, or any path containing a `__pycache__/` segment | **refused outright**, whatever its content, before its bytes are read |
| **declared asset** | `SKIP_SUFFIXES` (`.png` `.jpg` `.jpeg` `.pdf` `.ico` `.zip` `.gz` `.xcuserstate`) | skipped, as before — these are content the guard has no business decoding |
| **everything else** | text (no NUL byte) | scanned with `FORBIDDEN`, as before |

Why refuse rather than extract-and-scan:

* **A `.pyc` should not be in the tree at any address.** It is build output. A
  rule that scans it *and thereby permits* it answers a different question than
  the one the repo needs answered. "Never tracked" is the correct verdict for a
  build artefact; whether its strings happen to contain an address is a bonus.
* **Extract-and-scan has no stopping rule.** A `.pyc` is a container, but so is
  a `.zip`, a `.gz`, a `.jar`, a PDF with an embedded font. Each layer added is
  more code on the commit path, run on every commit forever, and every layer is
  a place to get a decompression bomb or an infinite-archive loop wrong. The
  card's own constraint says it plainly: a scan that is slow gets bypassed.
* **String extraction is not more correct, it is more expensive.** To decide
  "does this blob contain a real address" you need a byte-level scan of a blob
  whose encoding you do not control — and if it answers yes, the fix is still
  "do not track this file", which the path rule already gave you without reading
  a byte.
* **It cannot be slow, so it cannot be bypassed.** Path matching alone; zero new
  `git` calls, zero new reads. Measured after the change: unchanged at 0.12 s
  over the tracked tree (§5).

The refusal is content-independent on purpose. A `.pyc` with no address in it is
still refused: the guard cannot tell the two apart without the machinery we just
declined to build, and a rule that is only enforced when it can prove a leak is
a rule that leaks.

### Allowlisted exceptions (two tiers — round-1 review made this real)

`ALLOWED_BINARY_PATHS` in the guard is the escape hatch for a binary that
genuinely belongs in this repo. It is deliberately **weaker than it first looks**,
and the reason is §1b: the text scan is blind to a compiled blob, so a hatch that
could waive the refusal of a compiled artefact would waive detection with it. My
first version of this section claimed the hatch "cannot hide an address" because
an allowlisted path is still text-scanned. The reviewer's probe disproved it — a
genuinely compiled `.pyc` on the hatch returned `[]` from `scan_blob`, silent,
because the scan that was supposed to be the backstop cannot read that blob. The
guarantee was made true by changing the code, not the wording:

* **a path whose suffix is in `BUILD_ARTEFACT_SUFFIXES` is never waivable.** The
  suffix tier of `is_build_artefact()` runs first and ignores the hatch. So a
  `.pyc`/`.so`/`.class`/`.whl` on the hatch is still refused, and no review of a
  diff to that list can open a silent hole in the compiled class.
* **the segment tier (`__pycache__/`) is waivable**, and a waived path is decoded
  and text-scanned even when it carries NUL bytes — i.e. it stays open to the one
  detector that works on non-compiled bytes.

The same review logic exposed a second hole in the same guarantee, which is closed
by the same change: `SKIP_SUFFIXES` is an *independent* way for a path to escape
the scan, so an allowlisted `.pdf` used to short-circuit before any decode —
"tracked and never read", precisely what a hatch entry must never mean. Now an
allowlisted path bypasses both silence rules, in `scan_blob` and in
`scan_paths`'s pre-read shortcut (both call one `is_skipped_asset()` predicate, so
the two gates cannot spell the rule differently). Pinned by
`test_hatch_does_not_bypass_the_skip_suffix_shortcut_silently` and
`test_scan_paths_and_scan_blob_agree_on_a_hatched_path`.

Honest cost of the suffix tier: a prebuilt `.so` can never be tracked here, even
deliberately. Today's tracked binary population is two `.png` files, so that costs
nothing today. If a real need ever appears, the answer is a non-artefact extension
or a vendored source build, not a wider hatch.

The hatch is empty today, and it stays a literal in the one file the guard's tests
pin, so widening it is a visible diff in a security control rather than a config
edit.

## 4. Hook and CI agree because they cannot not agree

The refusal lives in `scan_blob()` — the same function `scan_staged()` (the
hook) and `scan_tracked()` (the pytest gate and the CI job) both go through —
and in `scan_paths()`, which had a pre-read `SKIP_SUFFIXES` shortcut that had
to be restructured so a deny-listed path is refused **before** it is skipped
and even when its file is missing on disk (a path in HEAD's index is a tracked
artefact whether or not the working tree has it).

There is one code path and one list. The pinned test
`test_hook_and_tracked_agree_on_a_tracked_build_artefact` runs the real
`git commit` through the real hook and the real `--tracked` scan over the same
tree and asserts the same path is named by both.

`report()` gained a separate section for artefact offenders — different problem,
different fix (`git rm --cached`, not "scrub to a placeholder"), and its own
warning that `--no-verify` is how the last two leaks happened.

## 5. Bounds

* No new subprocess. `staged_blobs()` already batch-reads every staged path in
  one `cat-file --batch`; the artefact check reads no bytes at all.
* No new I/O in the tracked scan beyond what `scan_tracked()` already did, and
  deny-listed paths short-circuit *before* the file read.
* Measured over the real repo before and after: `--tracked` 0.12 s → 0.12 s;
  `--staged` over 13 files, in `test_guard_is_dependency_free_and_bounded`,
  well inside the 5 s budget that test enforces.

## 6. Tests added

`scripts/tests/test_address_guard.py` (detector contract):

* `test_scan_blob_refuses_a_pyc_carrying_a_real_address` — a real `.pyc`-shaped
  blob (NUL-bearing, so the old code returned `[]`) holding a real LAN address
  is named by the scanner.
* `test_scan_blob_refuses_build_artefacts_even_when_clean` — the verdict is about
  the file, not the content: a clean `.pyc`, a `.so` and a `__pycache__/` path are
  all refused.
* `test_scan_blob_refuses_an_artefact_regardless_of_case_and_separators` —
  `PYC` and a backslash form do not dodge the list, and `native/source.c` /
  `data/alpine.software.json` do not get caught by it.
* `test_legitimate_binary_asset_is_still_accepted` — the tracked `.png` population
  (and a NUL-bearing unknown extension) is **not** broken by the new rule.
* Four cases for the two-tier hatch (round-1 review; see §3):
  `test_a_compiled_artefact_suffix_is_never_waivable_by_the_hatch` — every
  compiled shape stays refused *from the hatch alone*;
  `test_a_genuinely_compiled_pyc_on_the_hatch_is_still_refused` — the reviewer's
  exact probe inverted into a requirement, and it re-pins from the inside that the
  text scan is still blind to that blob;
  `test_hatch_waives_only_the_segment_refusal_and_still_scans` — a `__pycache__`
  path on the hatch commits and is still scanned with its NULs;
  `test_hatch_does_not_bypass_the_skip_suffix_shortcut_silently` and
  `test_scan_paths_and_scan_blob_agree_on_a_hatched_path` — the hatch cannot make a
  path tracked-and-never-read, and the two gates spell that rule identically.
* `test_scan_paths_refuses_a_tracked_artefact_even_if_unreadable` — the tracked
  gate's pre-read path.
* `test_cli_blocks_a_staged_pyc_carrying_a_real_address` and
  `test_cli_tracked_flags_a_committed_pyc_even_after_a_scrubbed_worktree` — the
  two shipped invocations, exit 1, actionable text, and the tracked verdict
  survives a scrubbed working tree.
* `test_report_names_the_artefact_and_the_removal_command` — the failure text is
  actionable.
* `test_hook_and_tracked_agree_on_a_tracked_build_artefact` (in
  `hscc_daemon/tests/test_precommit_address_hook.py`, which is where the real
  `git commit` runs) — the real hook and the real `--tracked` CLI over the same
  tree produce **identical** verdict strings.
* `test_scan_blob_skips_binaries_and_skip_suffixes` updated: the "binary is
  silent" assertion is now pinned on an *unknown* extension, not on a `.pyc`,
  so the test cannot quietly re-legitimise the old behaviour.
* `test_a_genuine_compiled_pyc_defeats_the_text_detector_so_refusal_is_required`
  — `py_compile` at runtime, the strong mutant §1b shows is necessary. Pins the
  naive-scan hit, the `FORBIDDEN` miss, and the refusal, in one case.
* `test_a_genuinely_compiled_pyc_is_blocked_by_the_hook` (in
  `hscc_daemon/tests`) — the same blob through a real `git add -f` + `git commit`.

`.github/scripts/tests/test_redact_guard_report.py` (CI job level, via the same
`_run_job_leg` helper that runs the real step script):

* `test_committed_build_artefact_fails_the_job_leg_and_stays_redacted` — the
  backstop catches what `--no-verify` let into HEAD, and the public log still
  never sees the address.
* `test_a_tracked_binary_asset_keeps_the_job_leg_green`.

## 7. Known limits (stated, not hidden)

1. **An unknown binary is still not content-scanned.** A NUL-bearing file whose
   name is in neither list (a `.bin`, a `.dat`, a vendored blob) is skipped, as
   before. The population here is zero and the deny list covers every
   compiled-artefact shape that has shown up in this repo's history, but the
   general statement "no binary carries an address" is not one this guard can
   make. It makes the narrower, enforceable one: no build artefact is tracked.
2. **Archives are in the skip list, not the deny list.** `.zip`/`.gz` can carry a
   real address inside and the guard will not see it. They are in `SKIP_SUFFIXES`
   because a test pins that list as the *gate's original noise list* and shrinking
   it unasked is its own risk; moving them to the deny list is a one-line change
   plus a decision about whether any committed archive is load-bearing. Carded as
   a follow-up rather than decided here.
3. **The deny list is a name list.** A compiled artefact renamed to `.txt` is
   scanned as text (NUL-bearing → skipped) and slips through. Detecting that
   means sniffing content types, i.e. the extract-and-scan machinery §3 declines.
   A deliberate evader already has `--no-verify`; the list's job is accidents.
4. **History is not rewritten here.** The `.pyc` from §1 is in commits before the
   2026-10-09 rewrite; publishing that decision is the operator's, as before.

## 8. Files changed

* `scripts/address_guard.py` — the policy, `is_build_artefact()`, `report()`.
* `scripts/tests/test_address_guard.py` — detector contract above.
* `hscc_daemon/tests/test_precommit_address_hook.py` — hook/`--tracked` agreement.
* `changelog.d/t_3e6d3db7.md` — the fragment.
* this file.

No change to `.githooks/pre-commit` or `.github/workflows/address-guard.yml`:
both call the scanner, which is the point of §4.
