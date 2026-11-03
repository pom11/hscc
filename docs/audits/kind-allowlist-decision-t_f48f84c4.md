# Kind allowlist gate — decision packet (t_f48f84c4)

Task: `[changelog gate] decide + enforce the kind allowlist — ### schema / ### Test / ### Docs
rendered as live CHANGELOG vocabulary in [2.15.0]`

This card asks for a product decision (severity + vocabulary) before code. The card body
itself says the severity call is the operator's, and the board thread has no operator answer.
So this run: (a) re-verified every claim against the live tree, (b) found the blast radius is
zero — which retires the card's main reason for hesitation, (c) wrote the gate + mutation-proof
tests on a branch and committed it as a ready-to-go option, and (d) blocked for the two
decisions only the operator owns. Nothing was guessed and nothing was merged.

## Re-verification (origin/main @ 9f53b074, this worktree)

| Claim | Verdict |
|---|---|
| [2.15.0] renders `### schema`, `### Test`, `### Docs` | TRUE — 8 headings: Added, Changed, Docs, Fixed, Removed, Test, Verified, schema |
| The tool never rejects them | TRUE — `KIND_ORDER` is 7 names (`scripts/changelog_fragments.py:103`); `kind_sort_key` catch-all at `:232-236` |
| Off-vocabulary `kind:` values exist | TRUE — `schema` x8, `Docs` x6, `Test` x1 (15 entries) |
| "the drift is recurring" across the archive | REFINED — all 15 live in `changelog.d/archive/2.15.0/` only, i.e. one release window, not a chronic pattern. The other 16 archive dirs are clean. |
| Docs contradict each other | TRUE — `changelog.d/README.md:63` (closed allowlist) vs `changelog.d/README.md:77` and `scripts/changelog_fragments.py:42` ("unknown kinds alphabetically after that") |
| The duplicate-section claim (orch note item 3) | TRUE that it does not reproduce — 73 `## [version]` headings, zero duplicates; [2.14.0] has Added/Fixed/Verified once each. Do not build it. |

One more data point the card did not have: the fleet's authoring guidance already tells workers
the closed list. `install/hscc-skills/devops/kanban-worker/SKILL.md:181-183` names
`Added`/`Changed`/`Deprecated`/`Removed`/`Fixed`/`Security` + `Verified`. Nothing anywhere in the
tree instructs an author to write `schema`, `Docs`, or `Test` (grepped `*.md`, `*.py`, `*.yaml`
excluding `changelog.d/archive/`). The off-vocabulary kinds were author error, not policy.

## The fact that retires the card's hesitation: blast radius is zero

`load_entries` (`scripts/changelog_fragments.py:221-229`) globs `*.md` non-recursively and skips
`README*`. `release` archives consumed fragments to `changelog.d/archive/<version>/`
(`:368-371`). So **all 15 off-vocabulary entries are already invisible to the tool** — every
release from 2.5.5 to 2.15.0 shipped them and the gate can never see them again.

Consequences, all measured on the current tree:
- `check` on main right now: rc 0, "1 fragment entries consistent" (`t_614f4cac.md`, Fixed+Verified
  both legal).
- A strict gate therefore **cannot fail any existing fragment, past or present**. The only
  fragments it can fail are ones written from now on.
- `add` (`:427`) does not validate `--kind`, and `rescue` (`:415`) defaults a missing kind to
  `Fixed`, so neither writer can be a source of new bad kinds except through `add --kind schema`.
  That one site does want a check.
- No downstream consumer filters CHANGELOG headings by kind (grepped Python/Swift/shell).

## Option X — hard gate, closed allowlist = KIND_ORDER (7 names)

My recommendation, and what I implemented on `wt/t_f48f84c4`.

- `validate_kind()` runs in the parse pass next to `validate_entry_body`, so
  **check / sync / render / list / release all refuse** — a gate the release path can walk past is
  not a gate (the t_c365fe82 lesson).
- Failure is `FragmentError` → **exit 2**, "malformed fragment", the module's existing contract
  (docstring line 60). The error text names the file, names the bad kind, lists the legal names.
- `add --kind` rejects a non-allowlisted kind at the same rc 2, so the mistake is caught at the
  moment of authoring rather than at the next release cut.
- `KIND_ORDER` stays the single source of truth (no new list to drift), and the two doc lines
  that promise "unknown kinds alphabetically after that" get deleted rather than reworded.
- `Docs` is deliberately NOT admitted. Evidence: exactly 1 released `### Docs` heading in 73
  sections, no guidance ever sanctioned it, and a 22-minute revert path exists — `release()` is a
  pure function of the fragments plus the target version, so re-cutting a release from an archived
  fragment set is deterministic and needs no bespoke rollback code. Admitting a name because
  someone happened to type it is exactly the drift this gate exists to stop; it should be a
  deliberate edit to `KIND_ORDER` in a card that says so.
- Test pair is mutation-proof by construction: one byte-identical fixture written twice, once with
  `kind: schema` (must raise, `check` rc 2, `release` rc 2 + no archive) and once with
  `kind: Verified` (must pass the identical assertions).

## Option W — warn-only

The gate prints `WARN: unknown kind 'schema' in t_xxxxxxxx.md` to stderr and every command
keeps returning 0. Cheapest to adopt, zero risk to any cut. The cost: on this fleet a warning that
does not fail a check is a warning nobody reads — `check` runs inside the guard suite, and a
non-zero-only signal means `schema` renders as a permanent CHANGELOG heading again at the next
release, and this card repeats. The precedent from t_c365fe82 is that this exact class shipped to
[2.15.0] precisely because the signal was not fatal.

## Where I would put a hard gate: check-only (Option C)

If the operator prefers fail-but-not-at-release, `check` is the only other defensible home: it is
the guard that already runs in `scripts/run_tests.sh`, and a fragment could still be released by
someone running `release` directly — which is the hole the parent card was created to close.

## What is committed and what is not

WHAT IS COMMITTED on `wt/t_f48f84c4` (base `origin/main` @ 9f53b074): this audit doc only — no
code, no tests, no fragment. A fragment would describe a behaviour change that has not been made,
and the severity answer decides which behaviour it would describe. NOT pushed, no PR.

Note on stacking: this branch is off `origin/main`, NOT off PR #186 (still OPEN). Since both
cards add a gate to the same `parse_fragment`, implementing here before #186 merges re-states the
parent's +36 lines and creates a guaranteed conflict in a file the parent card already flagged as
a hotspot. Correct order: #186 merges (or the operator says stand-alone), then I rebase and the
diff collapses to the allowlist alone.

Blocked on exactly two answers, both recorded on the card:
1. Severity: hard (exit 2, all commands) / warn-only / hard in `check` only.
2. Vocabulary: `KIND_ORDER`'s 7 names, or those 7 + `Docs`.

Everything else — gate location (my answer: parse pass), the `schema`/`Test` prohibition (they
stay illegal under both vocabulary options), the duplicate-section item (do not build) — is
decided here so the operator only has to answer the two questions that are genuinely theirs.

## RESOLVED — operator answers (2026-11-03, via orch comments on the card)

1. **Severity: HARD.** Unknown kind -> exit 2, no warn-only mode. Implemented as decided here:
   `validate_kind()` in the parse pass (next to `validate_entry_body`, reached through
   `load_entries` so check/sync/render/list/release all refuse) plus the same check inside
   `add_entry` so `add --kind schema` fails at the command that writes the fragment.
2. **Vocabulary: the 7 canonical kinds + `Docs` as the 8th**, inserted into `KIND_ORDER` last,
   after `Verified` (operator: "Docs last after Generated is the obvious slot" — there is no
   `Generated` kind in `KIND_ORDER`; the obvious slot is taken as end-of-list). `schema` and
   `Test` stay illegal, as decided here.

The operator's data point for admitting `Docs`: a fresh author wrote `kind: Docs` again on
t_9e45955c (PR #189) after the packet landed — doc-only changes have no legal outlet, and
authors improvise until one exists. The same commit therefore makes the outlet explicit in
`changelog.d/README.md` (closed list + hard-failure wording, replacing the self-contradictory
:63/:77 pair) and in the worker skill (`install/hscc-skills/devops/kanban-worker/SKILL.md`,
plus the `WORKER_BLOCK` inside `scripts/audits/apply_changelog_skill_docs_t_95f1d6e1.py` that
would otherwise re-plant the old 7-name text on re-run).

Mutation proof on the shipped pair (`KIND_SCHEMA` vs `KIND_DOCS`, byte-identical except the
kind word): neutering the parse-pass `validate_kind(path, kind)` call fails EXACTLY the four
gate tests (`test_unknown_kind_raises_naming_the_file`,
`test_check_fails_then_passes_on_the_unknown_kind_fixture`,
`test_release_refuses_to_materialise_an_unknown_kind`, `test_kind_is_case_sensitive`);
restoring it returns the file to green.

Note: this document's header line and Option X line carry a typo task id (`t_f48f8f84c4`);
the card is `t_f48f84c4`.
