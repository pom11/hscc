# Address guard — the `\b` boundary was a delimiter assumption, not a safety property (t_1b7b3166)

Task: `t_1b7b3166` · parent: `t_3e6d3db7` · component: `scripts/address_guard.py` (`FORBIDDEN`)
Measured on: **py3.11.16** (`$HOME/.hermes/hermes-agent/venv/bin/python`) and
**py3.13.12** (`/Users/desac/miniconda3/envs/p313/bin/python`) — every number below
reproduced on both.
Probe scripts (scratch, never committed — they would themselves look like a second
implementation to the single-source pin):

- `probe_delim_matrix_t_1b7b3166.py` — the 26×26 both-sides delimiter matrix
- `probe_population_shape_t_1b7b3166.py` — does the miss shape occur in the tracked tree?
- `probe_pyc_framing_t_1b7b3166.py` — 7 marshal framings of a genuinely compiled `.pyc`
- `probe_marshal_digit_t_1b7b3166.py` — can marshal put a *digit* next to the address?
- `probe_container_blindness_t_1b7b3166.py` — where is the scanner still blind, by construction?

**Content discipline.** No real LAN or tailnet address appears in this note, in the
probes' output, or in any test. Address fixtures are assembled at runtime by the
tests (`_addr(*parts)`); this note writes the live-LAN shape as `<addr>` wherever a
value would otherwise be implied, and the only literal addresses here are the two
documented placeholders and the sanctioned `100.64.0.0/24` fixture block.

---

## 1. The finding, stated as a measurement

`FORBIDDEN` was `re.compile(r"\b(?:…)\b")`. `\b` is a word/non-word transition, so
the pattern required the address to be delimited **on both sides by non-word
characters** — it assumed an address is always written the way prose writes one.

The 26×26 cross product (26 delimiter classes on the left × 26 on the right = 676
cells; `eol` means the absence of a character, so row/column `eol` pins start- and
end-of-line) gives the miss set as a **boolean**: a cell misses iff a word
character sits immediately before *or* immediately after the address.

- **315 of 676 cells missed** under the shipped pattern.
- Collapsing the miss set to single-side classes, the blind classes are:
  `letter_ascii`, `digit`, `underscore`, **`uni_cjk` (`中`)**, **`uni_latin1` (`é`)**,
  **`uni_cyrillic` (`и`)**, **`uni_ordm` (`ª`)**.
- Every **non-word** class matches on both sides: space, tab, `,`, `=`, `&`, `:`,
  `/`, `"`, `'`, `]`, `-`, `.`, `;`, `@`, NUL, `\x0e`, `\x01`, `\n`, eol. Confirmed
  end-to-end on whole-line shapes too: minified JSON, CSV row, `key=<addr>&x=1`
  query string, URL — all matched before the fix and after.

The card's own correction (comment 1032) retracted `,`/`=`/`&`/`"`/`:` as risky —
that retraction is **confirmed**: those are non-word and were never blind. What the
retraction understated is that **`\w` is Unicode**. So the class is not merely
identifier-style adjacency (`NAS_<addr>`, `host<addr>`, `<addr>backup`) but also
**ordinary unspaced CJK prose**: `服务器<addr>` and `<addr>的配置` were invisible. That
is not exotic in this repo — the operator writes Chinese in docs, commit messages
and UI strings.

## 2. Is the miss class *real* here? (the question the card made load-bearing)

Asked as: how many forbidden-shaped addresses in the tracked tree sit flush against
a word character? Constrained to the guard's own branches, with the sanctioned
fixture block excluded so the count means "a real address is hiding":

```
tracked=1103  scanned_text=1101  nul_skipped=2
forbidden-shaped quad PRECEDED by word char: {}      <-- zero
forbidden-shaped quad FOLLOWED by word char:  {}     <-- zero
whole tracked tree: naive forbidden-shape=140  shipped find_in_text=0
```

So: **current exposure is zero — nothing is hiding today.** All 140 forbidden-shaped
occurrences in the tree are sanctioned `100.64.0.x`, and the guard correctly returns
0. (The unbounded-quad version of the same probe returns thousands, but that shape is
dominated by version numbers and semver strings; only the constrained count means
anything.)

Zero exposure today is **not** the same as a false report, for two reasons that are
both measured rather than argued:

1. There **is** a reproduced non-text instance — a genuinely compiled `.pyc`, §4 —
   where the same trailing `\b` argument was the *entire* basis of the parent card's
   binary policy. So the miss class had one real member, not zero.
2. The blind classes are content-shaped, not file-shaped: one unspaced Chinese
   sentence naming a host, or one `NAS_<addr>` log token, puts the repo in the miss
   set immediately, and no gate would ever mention it.

Verdict recorded: **real miss class, narrow but non-empty → fix the boundary.**
Not closed as not-a-bug, and the zero-exposure measurement is recorded here so the
next reader does not have to re-derive whether anything was ever hiding.

## 3. The fix, and why this boundary rather than another

One place changed — `FORBIDDEN` in `scripts/address_guard.py`:

```
-    r"\b(?:…)\b"
+    r"(?<![0-9])(?:…)(?![0-9])"
```

Rationale: what the guard must reject is an address that is a **fragment of a longer
digit run**. `1<addr>` and `<addr>1` are not dotted quads, and
flagging them would make the tool unusable (this repo carries version strings on
every page). A letter or `_` on either side is not that — it is a *label*, and the
address inside `NAS_<addr>` is still a real address.

- Miss set: **315 → 51 cells**, and the residue is *exactly* the digit row and
  column — deliberate, and the reason `<addr>1` stays unflagged.
- **Strictly widening — provably.** Every `\b`-delimited occurrence is also
  digit-delimited (a digit is a word char, so non-word ⇒ non-digit), so the new
  pattern matches a superset of the old one. Pinned as a property test rather than
  left to prose: the test rebuilds the old `\b` form **from the shipped pattern at
  runtime** and asserts old ⊆ new over the whole delimiter corpus. No second copy of
  the alternation is written down, so the single-implementation pin stays green.
- **`[0-9]`, not `\d`.** In text mode `\d` is Unicode-aware; these octets are ASCII.
  The explicit class makes the pattern behave identically as `str` and as `bytes`,
  which matters because `.github/scripts/redact_guard_report.py` ships
  `FORBIDDEN.pattern` over a child-process protocol and re-compiles it on the far
  side. Measured identical verdicts in `str` and `bytes` mode for every blob case.
- Sanctioned material is untouched, **including when adjacent to a word char**:
  `10.0.0.x`, `10.0.0.x/24`, `100.64.0.1`, `100.64.0.3`, `100.64.0.254`,
  `NAS_100.64.0.1`, `node100.64.0.1x`, `5100.64.0.19`, `host10.0.0.x`,
  `192.168.89.244` (other subnet), `172.16.4.5`, `8.8.8.8` — all still 0.
- **Whole tracked tree re-scanned under the new pattern through `scan_blob`:
  offenders = 0.** No tracked file flips verdict, so no new false positive lands on
  any developer's commit path.

## 4. What the fix *falsified* — and what it did not

The parent card's binary policy (`t_3e6d3db7`, approved at `104eac34`) rests on a
sentence: *the text scan is BLIND to a compiled blob, therefore a compiled-artefact
suffix is never waivable.* That sentence was true under `\b`, and the fix makes it
**false**. Measured, on a genuinely `py_compile`-d blob, 7 marshal framings
(`probe_pyc_framing`), both interpreters:

```
construction             naive  F_text  F_bytes  F_latin1
single_const               1       1        1         1
const_reused               1       1        1         1
const_then_many_names      1       1        1         1
const_in_fstring_like      1       1        1         1
const_then_digits          1       1        1         1
const_in_tuple             1       1        1         1
many_consts                1       1        1         1
```

Trailing neighbour in every framing is a **letter** — marshal writes the next
interned name with no delimiter — so relaxing the trailing boundary reaches it. All
seven framings are now seen. I also tried to *construct* a blind case, because a
digit neighbour is exactly what the new boundary refuses and marshal's
re-referenced-object type code is the ASCII byte `0`: 7 hand-built marshal shapes
(`probe_marshal_digit`), and **no blind construction was found** — the repeated
constant never lands flush against a digit here. I record that as *not found*, not
as *impossible*: it is a property of CPython's marshal writer, which is an
implementation detail of whatever interpreter built the blob.

So the **rule stands, the justification does not**, and that distinction is the
point. The enforcement story after this card:

- **A compiled-artefact suffix stays never-waivable** — not because the scanner
  cannot read such a blob (it can, today), but because whether it can read one now
  depends on the *emitting* interpreter's framing choices, and a gate whose verdict
  depends on bytes a foreign build produced is not a control. The refusal is
  content-independent and therefore stable.
- The blindness that is **irreducible and measured** is containers. A `.whl`/`.zip`
  whose member is DEFLATE-compressed does not contain the address bytes at all
  (`probe_container_blindness`):

  ```
  dist/pkg-1.0-py3-none-any.whl   (deflated) naive=0 F_text=0 F_bytes=0  scan_blob=1(refused)
        address bytes present in container? False
  dist/stored-1.0-py3-none-any.whl (stored)  naive=1 F_text=1 F_bytes=1  scan_blob=1(refused)
  ```

  No boundary change of any kind can scan bytes that are not there. The stored
  (uncompressed) member is readable, which is exactly the point: readability is a
  property of the *container format*, not of the pattern, so the pattern is the
  wrong layer to fix this at.
- The other remaining blindness is the **NUL gate, not the regex** — and this is the
  honest successor to the old claim. An unlisted extension carrying a NUL byte is
  skipped *before any decode*, so text that the new pattern would match goes unread:

  ```
  regex applied to the decoded bytes:            1
  scan_blob("vendor/blob.bin") (NUL-bearing):    0     <-- the gate, not the pattern
  scan_blob("docs/notes.txt") (SAME NUL bytes):  0     <-- extension does not help
  scan_blob("docs/notes.txt") (NULs stripped):   1     <-- so the gate owns the silence
  ```

  Before the fix this case was *doubly* blind (gate **and** pattern), which is why
  the old prose could blame the pattern. It can no longer. Note also what the gate
  keys on: the **content** (a NUL byte), not the extension — so the honest limit is
  "NUL-bearing and not on the hatch", which is narrower than the `SKIP_SUFFIXES`
  wording suggests. Pinned by `test_the_nul_gate_not_the_pattern_is_now_the_blind_layer`.

The two hair-trigger tests written by `t_3e6d3db7` (`test_a_genuine_compiled_pyc_
defeats_the_text_detector_so_refusal_is_required` and
`test_a_genuinely_compiled_pyc_on_the_hatch_is_still_refused`) asserted the now-false
half. They went red on schedule — that is the mechanism working — and are re-pinned
in the same commit to the surviving discriminator: **the blob is now visible to the
pattern, and the refusal is retained for container/format reasons**, so the hatch
argument is restated on measurement rather than inherited.

## 5. Limits of this fix, stated plainly

1. **Digit-adjacency is still blind, by choice.** `<addr>1`, `5<addr>` are not
   flagged. This was true before the fix (`\b` excludes digits too), so it is not a
   regression, but it is a real residue — 51 of 676 cells. It buys the ability to
   carry version numbers.
2. **A rename-to-`.txt` compiled blob is still refused-or-scanned by name, and a
   renamed artefact that keeps a forbidden suffix stays refused** — unchanged from
   the parent card. The parent's limit #3 (artefact renamed to `.txt`) is untouched.
3. **The NUL gate still silently skips unknown-extension binary** (§4). Widening it
   is a policy change with a noise cost on every commit, deliberately out of scope
   here.
4. **A deflated container member is unreachable** (§4). Closing it needs an
   extract-and-scan layer, which the parent card declined for cost/stopping-rule
   reasons and this card does not reopen.
5. Detection remains **content-shaped, not exhaustive**: the pattern still covers the
   three documented classes (live LAN, CGNAT above the fixture block, `100.64.x`
   outside it). Nothing here widens *which* addresses are considered secret.

## 6. Evidence index

| Claim | Where it is pinned |
| --- | --- |
| Each delimiter class matches / misses as measured | `scripts/tests/test_address_guard.py::test_find_in_text_verdicts` — the whole 676-cell cross product is parametrised from `_DELIM_CLASSES`, so no cell can regress silently |
| Non-word delimiters were never blind (the retraction) | same, `dquote`/`equals`/`comma`/`amp`/`colon` cases |
| CJK / accented adjacency is now caught (the new class) | same, `uni_cjk` / `uni_latin1` cases + the explicit CJK prose cases |
| The fix cannot reduce detection (old ⊆ new) | `test_the_new_boundary_is_a_strict_superset_of_the_old_word_boundary` |
| Truncation guard still rejects longer digit runs | same suite, `<addr>1` / `1<addr>` / `.2444` cases |
| Placeholders + fixture block accepted, adjacency included | `test_the_sanctioned_forms_stay_accepted_in_every_context` |
| str and bytes forms of the pattern cannot disagree (`[0-9]`, not `\d`) | `test_pattern_text_is_bytes_transportable_and_verdict_identical` |
| Compiled `.pyc` is now visible to the pattern | `test_a_genuine_compiled_pyc_is_now_visible_to_the_pattern` |
| Refusal still required, for the measured reasons | `test_a_deflated_container_member_is_unreachable_at_any_boundary` (stored control) |
| NUL gate — not the regex — is the blind layer | `test_the_nul_gate_not_the_pattern_is_now_the_blind_layer` |
| Two-tier hatch still holds | `test_a_compiled_artefact_suffix_is_never_waivable_by_the_hatch`, `test_a_genuinely_compiled_pyc_on_the_hatch_is_still_refused` |
| Hook and tracked gates still agree | `test_hook_and_tracked_agree` (unchanged, re-run) |
| One implementation of the pattern | `test_forbidden_pattern_is_defined_in_exactly_one_tracked_file` (unchanged, green) |
| Redactor reads the shipped pattern, never a copy | `.github/scripts/tests/test_redact_guard_report.py` (93 cases, green) |

## 7. Mutation battery (are the new tests load-bearing?)

Six mutants applied to scratch copies of the tree (never the workspace), each checked
against only the eight new/rewritten functions. Every mutant was caught:

| Mutant | Caught by |
| --- | --- |
| `\b` boundary restored (full revert) | 4 functions incl. the superset proof and the matrix |
| trailing side reverted to `\b` (the original defect) | 5 functions incl. both re-pinned hair triggers |
| boundary made Unicode-aware (`(?<!\d)`) | the bytes-transport + superset tests |
| boundary deleted entirely (no anti-truncation) | the matrix + superset proof (false positives on digit runs) |
| compiled suffix made waivable by the hatch | `test_a_genuinely_compiled_pyc_on_the_hatch_is_still_refused` |
| NUL gate disabled | the NUL-gate + compiled-blob tests |

The second row is the important one: the exact defect this card was written for,
reintroduced on one side only, fails five functions — the hair trigger t_3e6d3db7
built still fires, it simply points at the new truth.

## 8. Round-1 review: this card leaked the thing it guards, through its own residue

Round 1 of review returned REQUEST CHANGES on one blocking finding, and the
finding was correct. `docs/audits/address-guard-boundary-t_1b7b3166.md` §3 — the
section that *justifies* the anti-truncation boundary — spelled the live LAN value
inside two digit-run examples, three occurrences. Independent re-measurement
(`probe_containment_t_1b7b3166.py`, both interpreters) before any fix:

```
tracked=1105  containment_hits=3  files=1        (3.11.16 and 3.13.12 identical)
  line  95: digit .. backtick
  line  95: backtick .. digit
  line 101: backtick .. digit
shipped scan_tracked offenders = 0               <-- every gate green
```

Every control failed to see it **for the reason this card documents**: all three
occurrences abut a digit, and `(?<![0-9])…(?![0-9])` refuses digit-adjacency by
design. The guard was behaving exactly as specified. What was wrong was the
*evidence*: the pre-commit self-scan of the diff was guard-based, so it shared the
blind spot it was used to clear, and I cited "768 added lines, 0 offenders" as
proof of something no guard-based scan can prove. A control cannot audit the class
it is defined to skip.

### The containment control

The companion control now lives in
`hscc_daemon/tests/test_no_real_addresses_committed.py`:
`test_no_real_value_survives_as_a_substring_in_any_tracked_file`, which checks
whether either real value appears **as a substring** of any tracked file. No
boundary to miss, no adjacency to get wrong. Three properties it was written to
have, each pinned:

- **Bytes-level**, so it covers the population `scan_blob` skips by content or
  extension (a `.png`, an unlisted NUL-bearing blob) —
  `test_the_containment_tripwire_covers_blobs_the_guard_skips` asserts the guard
  returns `[]` on exactly those files *while the tripwire names them*.
- **Never prints the value.** An offender string is `path:line: which-value
  (+digit-abutting)`. A failing assertion goes to the GitHub job log, the log is
  public, and the redactor that scrubs logs keys on the same boundary that hid the
  leak — a tripwire that quoted what it found would be the 2026-08-30 incident
  relocated into the test suite.
- **Differentially pinned** — `test_the_containment_tripwire_sees_what_the_guard_regex_cannot`
  asserts the guard's own regex returns `[]` on the fixture and the
  tripwire returns 2, so the tripwire cannot quietly be "simplified" into a regex
  reusing `FORBIDDEN` and keep passing.

Mutation proof (the reviewer's own standard — the control must have failed the
commit it exists to catch): run over `git show e25b7677:…`, the tripwire reports
**3 offenders, all three flagged digit-abutting**, on both interpreters; over the
scrubbed tree, **0**.

### The stronger finding: absorption

Writing that differential surfaced something neither the card nor the review
named. Digit adjacency is not only a *miss* class — it can be a **misread** class.
The tailnet branch's last octet is `\d{1,3}`, so a real value followed by one
digit is *not* refused: the pattern absorbs the abutting digit and reports a
longer quad. A match comes back on a string that does not contain the value, and
the reported text is not the value. (The example is deliberately not spelled out
here or in the test — `value + "1"` is itself a real-shaped token, and the widened
boundary would flag it in this very file, as it flagged §3.)

So the regex is unreliable in **both** directions once a digit abuts: it can stay
silent on a real value (the LAN branch, whose prefix is a fixed literal and so has
no quantifier to re-anchor into) and it can report a hit that is not the value
(the CGNAT branches, whose octets are quantified). Absorption stops at the octet
width — value + 4 digits is refused again — which is pinned by
`test_a_digit_neighbour_can_shift_what_the_regex_thinks_it_matched`.

Practical consequence, stated for whoever reads a gate verdict next: **"the guard
flagged it" is not evidence that a file is value-clean, and "the guard was quiet"
is not evidence that it is.** For the yes/no question "is this value in this
tree", the control of record is containment, because it reads the value rather
than a shape. The regex stays what it is for — a *blocking* rule with an
acceptable false-positive budget on version numbers — and is now complemented, not
trusted, on the residue.

### History reachability, recorded so it is not mistaken for fixed

The scrub is forward-only, as the gated-ref discipline requires (merge, never
rebase a pushed ref). That means the value is scrubbed from the **tip** and not
from the **branch**: `e25b7677` still carries all three occurrences, and that
commit is on the pushed refs `wt/t_1b7b3166` and `wt/t_1b7b3166-gated` on the
public origin, so `git show e25b7677:<path>` serves the value today even though
`origin/main` is 0-hit. Merging this branch to main on a forward-only scrub would
move the value into **main's** history — the state that cost a force-push rewrite
on 2026-08-30. Merge-vs-rewrite is therefore an operator decision, not a worker
one, and it is flagged in the handoff rather than resolved here. Deleting the two
spent refs after the scrub lands restores remote-unreachability for the branch;
GitHub may retain detached objects, and rewriting published history remains
operator-side under the standing rule.
