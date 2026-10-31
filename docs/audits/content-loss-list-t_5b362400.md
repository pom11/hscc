# t_5b362400 — the doctor's role-SOUL check now reports the CONTENT loss list

Scope: read-only. Nothing under `~/.hermes/profiles/` was written, `hscc.py
generate` was NOT run, and no live profile was modified. Every mutation in this
card's diff is inside `hscc-roles/`, `hscc-bootstrap/tests/`, `docs/audits/` and
`changelog.d/`.

## What was missing, and why a heading sweep cannot supply it

`hscc-roles/role_soul_check.py` (PR #115) answers "does this live SOUL carry a
`## ` heading `compose_soul()` cannot emit?". That is the right headline for a
hand-appended *block*, but it is a HEADING sweep: a hand-patched **paragraph
under an emittable heading** leaves every row clean while the next
`hscc.py generate` deletes the sentence. t_0f2ee405 had to answer that with an
ad-hoc scratch script, so nothing in the repo detected the *next* such loss.

This card makes that script permanent. The check now composes what `generate`
WOULD write for each judged profile (`generator.compose_soul(spec, base)` — the
same call `cmd_generate` makes) and positionally diffs the live SOUL against it,
emitting the per-profile loss list in the same non-fatal WARNING row.

## Method (reproducible, and now owned by the repo rather than a scratch file)

1. `compose_soul(spec, base-identity.md)` for the profile's own tracked spec.
2. `difflib.SequenceMatcher(composed_lines, live_lines, autojunk=False)`, and a
   live line counts as surviving only if it falls inside an `equal` opcode
   (POSITIONAL, never a set difference — see traps below).
3. Blank lines and `## ` heading lines are not judged here; the heading sweep
   owns headings, so one defect is never counted twice.
4. Every remaining line is classified `identity` / `inventory` / `covered` /
   `lost`; only `lost` becomes a problem line.

## The two false-signal traps the card warned about, and what each costs

**Trap (a) — a duplicated line must not "survive" by coincidence.** A set
difference scores a hand-patched bullet as surviving whenever identical text
appears anywhere in the document. The positional opcode test is the fix, pinned
by `test_a_duplicated_line_does_not_survive_by_coincidence`.

The same test also required `autojunk=False`, and the reason turned out to be
the opposite of the one first written here — worth recording because a mutant
caught it (see below). `difflib`'s popular-element heuristic is **not** a
fail-open in this pass: measured over ~6000 boilerplate-heavy documents it never
marked a genuinely novel line as equal (0 hits). What it does is destroy the
report's usefulness when a repeated line is duplicated — on the pin's fixture it
flags **713 of 799** live lines instead of the 1 that is actually the patch, and
the line number the operator needs disappears with them. So the flag buys a
triageable list and a stable verdict, not catch; `test_autojunk_off_is_what_keeps_the_report_a_list_not_a_wall`
pins both halves, including the assertion that the fixture still distinguishes
the two settings.

**Trap (b) — not every differing line is a loss, or the report is untriageable.**
Each class needs its own rule, and the rules are deliberately different:

| class | rule | why it is not a loss |
|---|---|---|
| `identity` | every missing word is the copy-pasted `## Role:` name, AND a header mismatch was already reported | replacing it IS the repair (that is what `generate` is for) |
| `inventory` | every missing word is identifier-shaped (carries an underscore) | the named shared/prod sandboxes t_8d22ec81 ruled must NOT come back |
| `covered` | nothing missing: each of its words also appears in the composed text | a rewrap / reorder / duplicate loses no *novel* content |
| `lost` | anything else | content that appears nowhere in what `generate` writes |

Two judgement calls worth stating plainly, because both are places where this
check could lie:

* `identity` requires **no other missing word**. Without that condition a
  hand-patched paragraph that merely *mentions* another role name gets swallowed
  by the exemption. `test_identity_bucket_does_not_swallow_novel_content` pins it.
* `inventory` requires **all** its missing words to be identifier-shaped, not
  any. The real pontaje line carried the sandbox inventory *and* the doc pointer;
  classifying it as inventory because one token matched would have hidden the one
  genuine loss in the fleet. This is the same fail-open shape that let
  t_0f2ee405's mutant M3 survive its first test (words present, constraint gone).

Because the classification is a judgement, the lines it EXCLUDES are printed with
their line numbers (`[!!]` in the rendered detail, full lists in `--json`) rather
than summarised away. A per-row budget bounds what one profile can print, so a
genuinely re-written block cannot bury every other row.

**Known limit, stated rather than hidden:** a hand-patched sentence built
*entirely* from words the composed text already uses elsewhere lands in
`covered`, so the loss list can under-report that one shape. It is the price of
not drowning the operator in rewraps; printing every covered line is what keeps
it reviewable instead of invisible.

## Leak class: the loss list quotes the line the operator must NOT see verbatim

The line the check has to render is exactly the line carrying the operator's
named shared/prod sandboxes. `POM_<…A>`, `POM_<…B>` and `<PROD>` are how this doc
refers to them (the convention t_0f2ee405's audit doc established after its first
draft reproduced them in-tree and was caught only by grepping the diff).

The check therefore masks any identifier-shaped token to `<sandbox>` in every
surface — row, rendered detail and `--json` — keeping the line number, the
position within the line and the rest of the sentence, which is everything needed
to find and fold the line. The rule is a SHAPE, never a name list: a blocklist
would itself contain the names it polices. `test_sandbox_names_are_masked_in_every_surface`
and the `LEAK CHECK` line of the sweep below are the pins.

## Measurement (read-only, this host, HEAD of this branch)

Same population as PR #115's sweep — SOULs carrying the worker signature,
orchestrator family excluded:

    judged worker profiles: 25      (23 excluded: orchestrator family / no worker signature)
    profiles flagged:       1       ecofire-pontaje-engineer
    genuine loss lines:     1

Per-bucket verdict for the flagged profile (live SOUL line numbers):

    lost      1   L133  the sandbox inventory + the docs/superpowers/{specs,plans}/2026-10-26-pontaje-* pointer
    covered   2   L131, L132  the feat/pontaje / 70600-70699 / Pontaje/ / POM_<own> constraint, re-wrapped
    identity  2   L123, L127  the copied `## Role: ecofire-…bc-engineer` identity + its ops line
    inventory 0

which is the same verdict t_0f2ee405 reached by hand (its "7 lines": 3 copied
identity incl. the header line, 1 non-emittable heading — owned by the heading
sweep, not this pass — 2 already re-emitted by the spec, 1 genuine loss), reached
now by the repo's own check instead of a scratch script. The check's own summary
for the fleet:

    24/25 worker-signature SOUL(s) match their dir name and spec (22 orchestrator-family
    SOUL(s) not judged — generated from a shared spec); 1 live line(s) across 1 profile(s)
    cannot be reproduced by generate; 1 need attention

Noise budget: 6 `[!!]` lines total across the whole 25-profile sweep, so the
operator can triage the classification without wading through it.

`LEAK CHECK unmasked inventory tokens: none` — asserted over the serialised
`--json` payload of the live sweep, not over a fixture.

## Limits of this measurement

- Snapshot of this host's `~/.hermes/profiles` at run time. It says nothing about
  a future profile or another machine; the permanent guard is the check + tests,
  not these numbers.
- The 23 excluded SOULs are not judged because they are not `compose_soul()`
  output: 22 carry a `## Role:` heading without the worker signature (the
  orchestrator family, minted by `orchestrators.py`/`orch_sync.py`) and 1
  (`probe`) carries neither signature, so it is not an HSCC-generated identity
  either. This pass makes no losslessness claim about any of them. Counted
  independently of the check by classifying every `SOUL.md` under the profiles
  dir: 25 worker + 22 orchestrator + 1 neither + 2 dirs with no SOUL at all.
- The pass answers "what is lost", not "is the resulting text desirable". After
  `generate`, the constraint text lives under `## Role: <name>` rather than a
  `## Project override:` heading, and the named sandboxes intentionally do not
  come back (t_8d22ec81).
- Nothing here verifies the operator has run `generate`. The repair stays an
  operator action; `hscc.py generate` rewrites live profile state for every
  generated profile at once.
