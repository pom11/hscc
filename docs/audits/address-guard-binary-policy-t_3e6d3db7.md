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
**Point 1 reproduces exactly.** Point 2 does not, on my machine: I get **1 match**
from `FORBIDDEN` over the decoded blob, from a source-compiled import *and* a
normal import (so this is not t_38fd345f's `__pycache__` shadowing vector — I
checked `__file__`, `__cached__`, and pattern identity between the two loads).
In that object the address sits between `\x0e` and `\x05` — both non-word bytes,
so both `\b` boundaries hold and the match stands. I could not make the shipped
pattern miss *that* blob. The probe did not state its interpreter; I did not
chase the difference further because **the card does not depend on it** — see
the next paragraph, and note that if the operator's 0-match result is right on
their machine, it only strengthens what follows, it cannot weaken it.

What *does* reproduce — and is the finding that decides the policy — is the
stronger version: **a freshly compiled `.pyc` of leaking source defeats the
shipping detector entirely.** Compile `NAS = <real-shaped address>` with
`py_compile` and the naive regex finds the address in the decoded bytes while
`FORBIDDEN` finds **0**. The mechanism is the *trailing* boundary, measured
rather than assumed: in that blob the character before the address is U+000E
(non-word, leading `\b` holds) and the character after is `N` — the first letter
of the next interned name, which marshal writes with no delimiter — so the
trailing `\b` fails. Relaxing only the leading boundary still matches 0; relaxing
only the trailing one restores 1. Meaning: **FORBIDDEN's `\b` assume the address
is delimited the way text delimits it, and undelimited binary framing breaks
that assumption.** Extract-and-scan would inherit exactly this blind spot. A
synthetic context table for the same boundary logic (address spliced after
various single bytes, decoded as text):

| byte immediately before the address | naive regex | shipping FORBIDDEN |
|---|---|---|
| `\x00` (opcode framing) | 1 | 1 |
| `\x0e` (non-word) | 1 | 1 |
| ASCII digit `5` (word char) | 1 | **0** |
| ASCII letter `z` (word char) | 1 | **0** |
| `\n` (line-anchored, i.e. ordinary text) | 1 | 1 |

This is the card's requirement-3 question answered with a number instead of an
argument: extract-and-scan **inherits the text detector's blind spot**, and the
card's own suggested mutant — "a NUL-bearing blob" — is matched by the *current*
pattern and therefore proves nothing. So:

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

A corollary the follow-up card should own: this is a **detector-side** finding,
not only a binary-side one. Any text file that ends up with a word character
immediately after an address — a minified JSON blob, a CSV column, a compacted
log line — slips past the trailing `\b` the same way. Changing the boundary is a
change to the single source of truth and to every pin built on it, which is
bigger than this card's scope; carding it rather than absorbing it.

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

### Allowlisted exceptions

`ALLOWED_BINARY_PATHS` in the guard is the escape hatch for a binary that
genuinely belongs in this repo. It exempts a path from the artefact refusal
only; the text scan still applies to it, so the hatch cannot hide an address.
It is empty today. It is deliberately a literal in the one file the guard
tests pin, so widening it is a visible diff in the security control rather than
a config file edit.

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
* `test_artefact_allowlist_exempts_the_refusal_not_the_scan` — an allowlisted
  artefact path commits, and is still scanned for an address even when NUL-bearing.
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
