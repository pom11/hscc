# changelog.d — one file per card

This directory is where a changelog entry is written. **Workers never edit
[`../CHANGELOG.md`](../CHANGELOG.md).**

## Why

`CHANGELOG.md`'s `[Unreleased]` section used to be edited directly by every
card. Every card prepended into the *same* `### Fixed` / `### Verified` hunk,
so two cards landing in the same window always conflicted — confirmed twice
(`t_163fa09f` @ `7864796`, `t_9462260b` @ merge `29d5e97`, landed @ `f7f15d3`).
Each collision cost a manual keep-both merge commit on the landing branch, and
the resolution was always mechanical: no text was ever actually in dispute.

One file per card removes the collision by construction. A card writes only
`changelog.d/<its-own-task-id>.md`; two cards can never name the same file, so
their diffs are always disjoint and merge cleanly — regardless of landing
order. `[Unreleased]` is a generated view, not an authoring surface.

## The rule for a worker

Add **one file**, named after your kanban task id:

```
changelog.d/t_95f1d6e1.md
```

That is the whole requirement. Do not touch `CHANGELOG.md`; a diff that touches
it is a review finding.

You can write the file by hand, or let the tool do it:

```bash
python3 scripts/changelog_fragments.py add --task t_95f1d6e1 --kind Fixed \
  --body "- **\`doctor\` false-failed on workers.** …prose…"
```

## Fragment format

Entries are separated by a line containing exactly `---`. Each entry is a
`kind:` header, an optional `task:` / `order:` header, then the entry body
**verbatim** — write it exactly as it should appear under the section heading,
so keep the leading `- ` and your own continuation indentation.

```markdown
kind: Fixed
task: t_95f1d6e1

- **`doctor` false-failed on every dispatched worker.** Hermes sets
  `HERMES_HOME` to the profile dir, so the check joined the wrong path.
  `hermes_root_from_home()` now resolves home → profiles-grandparent.
---
kind: Verified
order: 1

- `hscc-bootstrap/tests/test_doctor.py`: 69 -> **72** cases, green with
  `HERMES_HOME` exported to a profile dir.
```

`task:` defaults to the file stem, so it is optional. `order:` (integer,
default `0`) only breaks ties between entries inside one file.

Allowed `kind` values are the Keep a Changelog sections — `Added`, `Changed`,
`Deprecated`, `Removed`, `Fixed`, `Security` — plus HSCC's **`Verified`**, which
is rendered last. `Verified` is where the landing evidence goes (suite counts,
both-interpreter runs, audit-doc paths), exactly as before.

A malformed fragment (missing `kind`, empty body, non-integer `order`) fails
loudly in `check` rather than silently vanishing at release. A fragment whose
file name is not a kanban task id also fails `check` — the name IS the
ownership rule; `misc.md` would put two cards back into one shared file.

## Ordering

Deterministic, so re-running the assembler is a no-op:

1. kinds in Keep a Changelog order, `Verified` last, unknown kinds
   alphabetically after that;
2. within a kind: `(task id, order, index-in-file)`.

Task ids sort lexically. That is deliberate: it is stable across merges and
needs no timestamp, so the assembled block is identical no matter which order
the fragments were merged in.

## How `[Unreleased]` stays correct

Between releases, the committed `## [Unreleased]` block in `CHANGELOG.md` is a
**pointer** — one line saying entries live here. So the whole of
`CHANGELOG.md`, including its version history above, stays out of worker diffs.

`python3 scripts/changelog_fragments.py check` accepts exactly two shapes:

- the pointer block (normal committed state), or
- the block `sync` would write from the current fragments.

Anything else means the generated area was hand-edited, and the command exits
`1` with a diff. That is the guard that keeps the rule enforceable: a worker
that forgot the rule fails the check instead of quietly setting up the next
merge conflict.

### Commands

| command | what it does |
|---|---|
| `check` | (default) exit 1 unless `[Unreleased]` is pointer-or-in-sync |
| `render` | print the assembled `[Unreleased]` block |
| `sync` | rewrite the block in `CHANGELOG.md` from the fragments |
| `list` | show every entry, its kind, and its owning card |
| `add` | append an entry to a card's fragment |
| `rescue` | landing step: move a legacy branch's hand-edited entries into a fragment |
| `release --version V` | cut the release, see below |

## Release step

```bash
python3 scripts/changelog_fragments.py release --version 2.5.5   # --date optional
```

Materialises the fragments into a permanent `## [2.5.5] - <date>` section in
`CHANGELOG.md` (kinds in order, entries ordered by task id), then moves the
consumed fragments to `changelog.d/archive/2.5.5/` so they stay auditable but
out of the working set. Bump `VERSION` in the same commit as the release, as
before.

Because the fragments are the source of truth up to that moment, a release
never has to resolve a merge conflict — the file it writes (`CHANGELOG.md`) is
only ever touched by the release commit.

## Landing a legacy branch (pre-scheme `wt/*` branches)

Branches written before this scheme still edit `CHANGELOG.md` directly, so such
a branch conflicts with the pointer block **once**. Do not hand-merge it:

```bash
python3 scripts/changelog_fragments.py rescue --task t_xxxxxxxx --branch wt/t_xxxxxxxx
git checkout HEAD -- CHANGELOG.md    # drop the branch's CHANGELOG.md hunk
git add changelog.d/t_xxxxxxxx.md && git commit
```

`rescue` compares the branch's `[Unreleased]` block against the merge base,
moves the entries the branch added into the task-named fragment, and **refuses
to write** if it finds no added entries — so an entry cannot be silently
swallowed during the transition.

## Migration note

The `[Unreleased]` entries that existed at introduction were moved in by
`scripts/migrate_changelog_fragments.py`, which attributes each entry to its
card from the first-parent landing merge (`merge: … (t_xxxxxxxx)`) and refuses
to write unless every non-blank entry line is accounted for. Its output: 13
entries across `t_267f9d88`, `t_9462260b`, `t_163fa09f`, 143/143 lines
preserved. The one-shot script stays in `scripts/` for auditability; it is not
part of any runtime path.
