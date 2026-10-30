# t_0f2ee405 — what `hscc.py generate` drops from ecofire-pontaje-engineer/SOUL.md

Scope: read-only measurement + an in-tree spec proposal. The live profile was NOT
modified, `hscc.py generate` was NOT run, nothing under `~/.hermes/profiles/` was
written. Every mutation in this card's diff is inside `hscc-roles/` + `changelog.d/`.

## Method (reproducible)

`compose_soul(rolelib.load_spec("hscc-roles/roles/ecofire-pontaje-engineer.yaml"),
base-identity.md)` — the generator's own composition — diffed against the live
`~/.hermes/profiles/ecofire-pontaje-engineer/SOUL.md`. Two independent passes, both
run against `origin/main` @ 1e8349b9:

1. set-difference of non-blank lines (would a line survive anywhere in the new doc?),
2. STRICT positional pass — a live line counts as lost only if it falls inside a
   non-`equal` opcode of `difflib.SequenceMatcher(composed, live)`, so a line cannot
   "survive" by coincidentally matching text elsewhere in the document.

Pass 2 is the one that answers the card's real question: a heading-based check (the
doctor's) cannot see a hand-patched *paragraph under an emittable heading*, so the
only trustworthy measurement is a positional diff of the whole document.

Fleet-wide, not just this profile: the same positional pass was run over every
worker-signature profile (the same population PR #115's doctor check judges —
`WORKER_MARKER = "your own git worktree"`, orchestrator family excluded).

## Measurement result

    judged worker profiles: 25      (23 excluded: orchestrator family / no worker signature)
    profiles with non-reproducible live lines: 1  ->  ecofire-pontaje-engineer  (7 lines)

The 7 lines, with the verdict for each (live SOUL line numbers):

    121  ## Role: ecofire-bc-engineer
    123  You are the ecofire-bc-engineer specialist. ... (bc-engineer's generic identity)
    127  You run as the **ecofire-bc-engineer** role on a worker GPU node ...
    130  ## Project override: ecofire-pontaje
    131  When your card is on the `ecofire-pontaje` board: branch from and PR into `feat/pontaje` (never
    132  main), object IDs 70600-70699 only, files under `Pontaje/`, publish only to POM_PONTAJE (never
    133  POM_<…A>, POM_<…B> or <PROD>). Spec + plan in docs/superpowers/{specs,plans}/2026-10-26-pontaje-*.

(Line 133's three sandbox names are redacted here as `POM_<…A>`, `POM_<…B>`,
`<PROD>`. They are the operator's shared/production BC sandboxes; a public repo
— and this audit doc is as public as any — must not name them. See below.)

Split by whether the loss is real:

- LINES 121/123/127 — NOT a loss. They are the copied-from-bc-engineer identity,
  which is exactly the defect PR #115's check reports as the header mismatch. `generate`
  replaces them with the pontaje spec's own identity and re-emits the ops block with the
  correct name. Deleting them IS the repair.
- LINE 130 — the heading itself. `compose_soul()` emits only base-identity headings +
  `## Role: <spec name>` + ops, so this heading cannot come back. Content survives if
  folded into the spec `identity`; the heading text does not, and should not (the
  information belongs under the role section, not a parallel one).
- LINES 131/132 — SAFE, already re-emitted by the spec `identity`
  (`hscc-roles/roles/ecofire-pontaje-engineer.yaml:11-14`): branch `feat/pontaje`,
  object IDs 70600-70699, `Pontaje/` prefix, publish only to the project's own
  `POM_PONTAJE`. Confirmed by the diff: these tokens appear in the composed text.
- LINE 133 — REAL LOSS, and it contains two different things:
  - the named shared/prod sandboxes (rendered here as `POM_<…A>`, `POM_<…B>`,
    `<PROD>` — deliberately not spelled out, in this file as much as anywhere: this
    doc is as much in-tree as a spec is, and naming them here would reproduce the
    very leak class the finding is about). Deliberately NOT preserved in-tree. That
    is t_8d22ec81's standing ruling, recorded in `changelog.d/t_8d22ec81.md`: "The
    named shared/prod sandboxes are deliberately not reproduced in-tree (no tracked
    file names them; `scripts/address_guard.py` documents that leak class) — the
    constraint is preserved, the inventory is not."
    The constraint IS carried by the spec's "never a shared or production sandbox".
    Nothing tracked in the repo names them today (`git grep -ohE 'POM_[A-Za-z0-9_]+'`
    -> `POM_PONTAJE` only, fleet-wide), so `generate` dropping them is intended, not
    an accident.
  - the spec/plan doc pointer — **the only genuine, unmanaged loss**. Absent from the
    spec, absent from the composed text, so `generate` deletes it and nothing in the
    repo records where the module's design docs live.

Cross-check against PR #115's doctor (read-only, from the parent worktree): it flags
exactly this one profile, with both defects (header mismatch + non-emittable heading),
25 worker rows — i.e. the heading sweep saw the block, and the positional sweep above
found nothing ELSE hidden under an emittable heading in any of the 25 profiles. The
card's worry ("the live SOUL may carry more hand-patched content than the doctor's
heading sweep can see") is measured negative, but only for this fleet snapshot.

## Are the doc targets real?

`docs/superpowers/{specs,plans}/2026-10-26-pontaje-*` resolves in the repo the role
owns (`~/dev/EcoFire_customizations_bc`) to exactly two tracked files:

    docs/superpowers/specs/2026-10-26-pontaje-module-design.md
    docs/superpowers/plans/2026-10-26-pontaje-module.md

`git check-ignore` on both: not ignored (rc=1) — they are tracked there, so a
repo-relative pointer is stable and does not depend on an untracked local file.

## The smallest spec `identity` edit that makes regeneration lossless

Append ONE sentence to the existing project-scoping sentence in
`hscc-roles/roles/ecofire-pontaje-engineer.yaml` `identity` — no new heading, no new
field, no generator change, no new spec key:

    The module's spec and plan are in the repo at
    `docs/superpowers/{specs,plans}/2026-10-26-pontaje-*`; read them before designing
    or planning new Pontaje work.

Why this is the minimum:

- `compose_soul()` emits `spec['identity']` verbatim (generator.py:518), so anything in
  `identity` is regenerated forever. That is the mechanism t_8d22ec81 already used for
  the branch/ID/prefix/publish constraints; this uses the same mechanism for the one
  remaining line.
- No named shared/prod sandbox is introduced. The phrasing keeps t_8d22ec81's
  "constraint, not inventory" rule, and the new test enforces that rule structurally
  rather than by re-listing forbidden names (see below).
- A repo-relative pointer only, no absolute operator path added beyond `~/dev/...`
  which the spec already carries.

Applied as a proposal in this PR (mergeable by the operator); NOT applied to the live
profile, and `generate` is still the operator's action.

## What the operator still has to do

1. Merge this PR.
2. `python3 hscc-roles/hscc.py generate` (re-stamps all generated profiles, including
   live profile state — worker-forbidden, so left to the operator).
3. Re-run `python3 hscc-bootstrap/doctor_role_soul.py --json`: the pontaje row should
   then have 0 problems (header == dir name, no non-emittable heading).
4. Expect `## Project override: ecofire-pontaje` to disappear as a heading — its
   content now lives under `## Role: ecofire-pontaje-engineer`, and the three named
   shared/prod sandboxes intentionally do not come back.

## Caveats / limits of this measurement

- The fleet numbers are a **snapshot** of this host's `~/.hermes/profiles` at run
  time (25 judged, 23 excluded). They say nothing about a future profile or a
  different machine; the permanent guard is the test, not this measurement.
- The 23 excluded rows are the orchestrator family and dirs without the worker
  signature. Those SOULs are minted by `orchestrators.py`/`orch_sync.py`, not by
  `compose_soul()` over a spec, so "generate is lossless for them" is not a claim
  this card can make and this sweep does not judge them.
- The pass answers "what is lost", not "is the resulting text desirable". After
  `generate`, the constraint text moves out of a `## Project override:` heading and
  under `## Role: ecofire-pontaje-engineer` — same information, different heading
  (see operator step 4).
- Nothing here verifies that the operator has run `generate` yet, or that the live
  SOUL has changed. At the time of writing the live SOUL is still the drifted one,
  byte-identical to the measurement input: this card changed only tracked files.

## Tests added (hermetic, 3)

`hscc-roles/tests/test_generator.py` (leg went 50 -> 53 passed):

1. `test_compose_soul_pontaje_carries_every_project_override_token` — pins every
   constraint token the live block carried (branch, object-ID range, `Pontaje/`
   prefix, `POM_PONTAJE`, the doc-pointer tokens) against `compose_soul()` of the
   REAL tracked spec, so the exact regression class this card measured (spec text
   quietly narrowed, `generate` then drops the constraint) fails loudly and names
   what would be dropped. Token-level, not line-level: `compose_soul` reflows the
   identity, so a line pin would fail on a rewrap that loses nothing.
2. `test_compose_soul_pontaje_keeps_publish_constraint_without_naming_others` — the
   guardrail must survive while the named inventory must not.
3. `test_role_specs_name_no_shared_sandbox_inventory` — enforces "constraint, not
   inventory" STRUCTURALLY: across all tracked role specs, `POM_PONTAJE` is the only
   `POM_`-prefixed token allowed. A blocklist would itself reproduce the leak class
   it polices, so the rule is inverted and this file never names the sandboxes.

No test reads `~/.hermes/profiles` (PR #115's ruling: live state belongs to the
doctor, not pytest — a live-reading test is red on the operator machine and vacates
in CI).
