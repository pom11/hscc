# Operator identity that is neither an address nor a home path: the decision (t_25f7a806)

`t_34de41c3` cleared the home-path class (`scan_tracked` → 0, `HOME_PATHS_ENFORCE =
True`, PR #818). While grepping that diff for leaks before pushing, a fourth spelling
of the same disclosure turned up: the operator's **username**, written bare, which
neither branch of `FORBIDDEN` can see — `_ADDRESS_BRANCH` matches IP literals,
`_HOME_BRANCH` matches `/Users/…` and `/home/…`.

This card asks whether that class belongs in the guard at all, and this file is the
answer, with the numbers that produced it. Throughout, `<operator>` is the documented
placeholder: the real macOS short name appears nowhere in this document, in the
detector, or in any commit message, because a security control that publishes the
value it exists to hide has already lost.

## Measured baseline

All numbers come from the shipping detector over `git ls-files` on `origin/main`
(`b5331610`), not from `git grep`: the guard skips `SKIP_SUFFIXES` assets and
sanctions named fixture users, so the two populations differ.

| measurement | value |
|---|---|
| `len(FORBIDDEN.pattern)` | **279** of the redactor's 4096 cap (asserted < cap/2) |
| `scan_tracked(origin/main)` → `blocking()` / `advisory()` | **0 / 0** — the tree is clean to the shipping guard |
| the 3 published shapes matched by `FORBIDDEN.search` | **MISS** on every one |
| raw occurrences of the short name over tracked files | **21 lines in 8 files** |

The card body listed three files; the measured set is eight. The three published
shapes are each invisible to the guard, verified by planting them and watching
`FORBIDDEN` decline:

| shape | example (redacted) | shipping `FORBIDDEN` |
|---|---|---|
| bare username in captured command output | ``no crontab for <operator>`` | MISS |
| username inside an identifier | ``<operator>_hermes`` | MISS |
| **path turned into a slug** | `-Users-<operator>` inside a `/private/tmp/…` scratchpad path | MISS |

The slug is the sharpest edge, and the reason the class is worth a rule rather than a
grep: `/Users/` never occurs as a substring, so `git grep "/Users/"`, the guard and a
reviewer scanning for a home path all agree the line is clean. Any tool that
flattens a path into a filename — and this repo's own worktrees, scratchpads and CI
caches do exactly that — produces this spelling by default.

The eight files, so a scrub card can be sized without re-measuring:

```
CHANGELOG.md:19708                                  1   captured `crontab -l` output
changelog.d/archive/2.15.0/t_048fe42e.md:51         1   the same line, archived copy
docs/audits/t_3969ace6_memori_capture.md            10  entityId / DB role `<operator>_hermes`
memori_byodb/tests/test_config_fallback.py           5  fixture entity_id, same identifier
docs/audits/2026-11-06-hermes-0.23-compat.md         1   the slug form
docs/audits/t_9e8732b8_memori_profile_capture.md     1   same identifier, one line
scripts/README.md:87                                 1   captured `crontab -l` output
scripts/hermetic_spawn_guard.py:280                  1   comment: "/Users" + "/<operator>/x"
```

Two of those were not in the card's table and are the same disclosure class: the
second audit doc, and a **test fixture** whose `entity_id` default carries the name.

## Decision 1 — the class IS in scope, but only by runtime identity, never by literal

`t_3480956c` declined the username rule for a reason worth quoting, because this card
inherits it exactly: writing the real name into the guard would "publish it in the
security control itself", and an enumerated deny-list of operator names has the same
cost plus a second one — it only ever covers the machines someone has already named.

The objection is about the *literal*, not the *rule*, and it dissolves if the detector
never holds the literal. `Path.home().name` is the operator's username, available at
runtime on the machine whose identity is at risk. A branch assembled from it at import
time names nobody until the guard runs, and the tracked bytes stay clean forever —
the same trick this repo already uses for the address fixtures (`"10" + ".0.0.5"`) and
for the hostname shape on t_68ccc902. So:

* **IN SCOPE**, as one more branch inside the ONE `FORBIDDEN` (Decision 2), derived
  from the runtime identity (this decision), reported without the identity
  (Decision 3).
* **The deny-list shape stays OUT.** Enumerating names is what t_3480956c refused and
  a runtime lookup makes it strictly worse: a list drifts from reality and silently
  re-blinds the guard for every machine not on it.
* **The rule declines itself when the identity is not an identity.** If the derived
  name is absent, shorter than four characters, in `SANCTIONED_HOME_USERS`
  (`runner`, `hscc-fixture`) or in a set of generic service names (`root`, `admin`,
  `ci`, `runner`, `builder`, …), there is no username branch at all. Without this the
  guard would flag every occurrence of the word `root` on a machine owned by someone
  called `root`, which is how a security control earns `--no-verify`.

The honest cost of deriving rather than enumerating: **the verdict is
machine-dependent**. On the operator's machine and in the local pre-commit hook the
class bites; on a GitHub-hosted runner `Path.home().name` is `runner`, which is
sanctioned, so CI is blind to it. That is no worse than today — CI has never seen this
class — and it is where the leak actually happens that matters: a human commits from a
machine that *does* know its owner. The blindness is stated in the guard's own
docstring and pinned by a test, so it cannot be mistaken for coverage.

## Decision 2 — inside the ONE `FORBIDDEN`, and the report never carries the value

Two rules from this repo's own history apply without negotiation:

* `.github/scripts/redact_guard_report.py` masks the public job log using `FORBIDDEN`
  and nothing else. A second compiled pattern would be invisible to redaction and
  would print the leak it exists to stop. So the username branch joins the one pattern
  (279 → ~321 chars of 4096; the cap test keeps its reasoning, not its threshold).
* Offenders are `rel:line: value`, and tracked paths contain colons, so the class is
  decided *after* the pattern by a string comparison on the match text, and every
  consumer classifies through a suffix constant with `str.endswith`.

On top of those, this class needs something the other two do not. The redactor only
masks what *its* `FORBIDDEN` matches, and its `FORBIDDEN` is derived from the
environment it runs in. If a report generated on the operator's Mac is ever redacted
somewhere else — the pattern arrives over a pipe from a child process on a different
host — the derived name there is not the name in the text, and the redactor is blind
exactly where it matters. The class must therefore not depend on that coincidence:
**a username offender string is `rel:line: <operator>` with the match replaced by the
placeholder.** The detector knows the name; the report never prints it. That is a
stronger guarantee than "the redactor masks it", and it is pinned in both directions
(the report must contain the placeholder, and must not contain the derived name).

`scripts/ledger_scrub.py` gets the same treatment from the other side: it scrubs with
`FORBIDDEN.subn`, so once the branch exists it would otherwise rewrite a username to
**`10.0.0.x`** — an address placeholder standing in for a person. Its placeholder
mapping learns the identity placeholder. `find_in_text` is untouched: its
one-hit-per-line contract belongs to that scrubber and is pinned elsewhere, so the
multi-match needs get `all_hits`, which already exists.

## Decision 3 — advisory first, with the flip proven

`OPERATOR_IDENTITIES_ENFORCE = False` at landing, `HSCC_ADDRESS_GUARD_IDENTITY_ENFORCE`
able only to strengthen it (never mute it — the same asymmetry as the home knob, so a
CI job cannot export its way out of the rule).

Why advisory here when the tree measures clean after the scrub, which is the posture
t_68ccc902 chose for hostnames: a runtime-derived rule is only as predictable as the
identity it derives from. The merge queue runs this suite **on the operator's host**,
where the rule is live, and a fork or a contributor's checkout derives *their* name —
so the first enforcement failure could land on a machine this card has never measured.
Advisory first, then flip once the scrubbing is merged and the post-flip `blocking()`
count has been measured at zero on the tree that flips.

Proving the flip bites is not optional — an advisory class with no proof is a
permanent advisory — so the CLI proof is the shape `t_34de41c3` used: a planted name
must exit 1 with the knob, must still exit 1 with the knob set to a falsy value (the
env var is not an off switch), and a real address must block whatever the knob says.

## Decision 3 taken — the flip (t_d42e6d48)

`OPERATOR_IDENTITIES_ENFORCE = True`, shipped by the card this section names, on the
condition Decision 3 set: `blocking()` measured at zero on the tree that flips, on a
host where the branch is live.

What "the tree that flips" turned out to mean, and why it is not the parent branch:
#829 was written against `main` at `b5331610`, and `main` had moved by the time the
flip was measured (`164c2da8`). The merge queue does not test the #829 branch — it
tests the merge. So the measurement ran on `origin/main` merged with the #829 branch
(a local throwaway merge), not on the branch alone.

Measured on that tree, on the operator's host, identity branch derived and therefore
live (1 name — the same host the merge queue runs on, so the branch was not inert):

| measurement | result |
| --- | --- |
| tracked files scanned (`git ls-files` → `scan_tracked`) | 2571 |
| offenders of any class | **0** |
| `blocking()` with `HSCC_ADDRESS_GUARD_IDENTITY_ENFORCE=1` | **0** |
| `python3 scripts/address_guard.py --tracked --repo .` | exit **0**, empty report |
| `len(FORBIDDEN.pattern)` | 325 (401 with #820's hostname branch merged too) |

Zero offenders means no scrub was owed and no false positive had to be answered by
tightening the branch. The `pytest-of-*` case the flip card was told to weigh did not
appear in the tracked tree — the tmp paths that carry it are never committed, so the
class never sees them.

**The flip-changes-the-assertion half is the part that proves the flip.** While the
class was advisory the CLI test asserted rc 0; a flip that left that pin passing would
have left the shipped posture covered by nothing. It now asserts rc 1 **by default**,
plus rc 1 with a falsy env value (the knob is still not an off switch), plus rc 1 for a
real address whatever the knob says. The report, knob and same-line-hazard tests were
re-derived from the shipped posture the same way, and the report test gained the case
the flip created: a home path and a name in one report, where two enforced classes now
share one header — the home title wins, so the identity section is still the only thing
telling a reader that the fix for a person is not `10.0.0.x`.

What the flip did **not** change: CI stays blind to this class, because a hosted runner
derives `runner` and the class declines itself for it. The teeth are on the machine
that commits, which is where a name is actually written. And a third party's name stays
invisible — the limitation below is untouched by enforcement.

## Scrub targets, by audience

| audience | shape | becomes |
|---|---|---|
| captured command output | `no crontab for <operator>` | `no crontab for <user>` |
| a DB role / entityId in an audit | `<operator>_hermes` | `hermes_role` |
| a path flattened into a slug | `/private/tmp/…/-Users-<operator>/…/scratchpad/x.md` | `/private/tmp/…/scratchpad/x.md` |
| a test fixture `entity_id` | `<operator>_hermes` | `hermes_role` |
| a comment about literal concatenation | the real short name inside a concatenated path literal | the documented `<operator>` placeholder, which was always what the comment meant |

Every edit keeps its line count, so line-number citations elsewhere in `docs/` cannot
drift.

## Known limitation this card does NOT close

The rule names *one* identity: whoever runs it. A name belonging to a *third* party —
a colleague quoted in an audit, a customer in a fixture — is invisible to it, exactly
as it is invisible today, and no shape rule can fix that without an enumeration this
card has just rejected. Closing that needs a different mechanism (a pre-commit secret
scanner over diffs), not a wider `FORBIDDEN`.

## Status

* Scrub: 21 occurrences across 8 files, its own commit, lands with this card.
* Detector: `OPERATOR_IDENTITIES_ENFORCE` shipped **advisory** here and **flipped to
  True** by t_d42e6d48, on the measurement in "Decision 3 taken" above.
* Parented on t_68ccc902 (`scripts/address_guard.py`, same region). **That PR was
  still OPEN and CONFLICTING when this card was worked** — the board handoff said
  merged. The username branch is written to meet #820 halfway: `_offender()` and
  `_is_advisory_now()` keep #820's names and shapes, so the rebase picks one copy of
  each rather than forking the dispatcher. Measured at flip time, when both were still
  open: merging #820 into `main` + #829 conflicts in **14 hunks of
  `scripts/address_guard.py`** (its test-file append merges clean). Sharing the helper
  names was necessary, not sufficient — whoever lands the second of the two should
  expect a manual pass over the branch constants, the offender dispatcher and `report()`'s
  header ladder, in that order.
