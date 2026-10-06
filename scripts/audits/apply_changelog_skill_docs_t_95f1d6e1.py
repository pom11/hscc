#!/usr/bin/env python3
"""Insert the changelog-fragment rule into the two skill files.

Used instead of a fuzzy patcher because a `patch` call on install/hscc-skills/**
reported success without changing the file (silent revert), and a silently
missing contributor-doc edit is exactly the failure mode this card exists to
prevent. Every edit here is asserted on disk before and after.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WORKER = ROOT / "install/hscc-skills/devops/kanban-worker/SKILL.md"
REVIEWER = ROOT / "install/hscc-skills/sdlc-review/SKILL.md"

WORKER_BLOCK = """## Changelog entries (never edit `CHANGELOG.md`)

`CHANGELOG.md` is **not** an authoring surface in this repo — its
`[Unreleased]` section is a generated view of the `changelog.d/` directory. A
worker that edits it re-creates a serial merge conflict: two cards landing in
the same window both prepend into the same `### Fixed` / `### Verified` hunk,
and every such collision cost a manual keep-both merge commit (confirmed twice:
`t_163fa09f` @ `7864796`, `t_9462260b` @ merge `29d5e97`).

The rule:

- Add **one file per card**, named after your task id:
  `changelog.d/<your-task-id>.md`. Two cards can never name the same file, so
  the diffs are disjoint and always merge.
- Format: a `kind:` header (`Added`/`Changed`/`Deprecated`/`Removed`/`Fixed`/
  `Security`, or HSCC's `Verified` for landing evidence), an optional `task:` /
  `order:` header, then the entry body **verbatim** — keep the leading `- `.
  Multiple entries in one file are separated by a line containing exactly `---`.
- Or let the tool write it: `python3 scripts/changelog_fragments.py add
  --task <id> --kind Fixed --body "- **…** prose…"`.
- Verify with `python3 scripts/changelog_fragments.py check` (also asserts the
  committed `[Unreleased]` block was not hand-edited). A fragment named
  anything but a task id fails the check — a shared name like `misc.md` would
  reintroduce the collision.
- `CHANGELOG.md` in your diff is a review finding. The release step
  (`… changelog_fragments.py release --version X`) is what writes it.

Full format, ordering and release details: `changelog.d/README.md` at the repo
root — locate it with `git rev-parse --show-toplevel` (worktree nesting depth
varies, and the deployed skill dir under `~/.hermes/plugins` cannot link to it).

"""

REVIEWER_OLD = """1. **Diff is sound.** Read the actual diff (`git -C <worktree> diff <base>...HEAD`
   or `git log -p`). Look for correctness bugs, missed edge cases, silent
   failures, and anything that does not belong.
"""
REVIEWER_NEW = """1. **Diff is sound.** Read the actual diff (`git -C <worktree> diff <base>...HEAD`
   or `git log -p`). Look for correctness bugs, missed edge cases, silent
   failures, and anything that does not belong. Changelog check: a worker's
   diff must add `changelog.d/<task-id>.md` and must NOT touch `CHANGELOG.md`
   (its `[Unreleased]` block is generated — see `changelog.d/README.md`). A
   `CHANGELOG.md` hunk in a worker diff, or a fragment not named after the task
   id, is a REJECT; `python3 scripts/changelog_fragments.py check` proves it.
"""


def edit(path, apply_fn, marker):
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"skip (already applied): {path.name}")
        return
    before = len(text)
    new = apply_fn(text)
    assert new != text, f"edit produced no change for {path}"
    assert marker in new, f"marker missing after edit for {path}"
    path.write_text(new, encoding="utf-8")
    on_disk = path.read_text(encoding="utf-8")
    assert marker in on_disk and len(on_disk) == len(new), (
        f"{path} did not persist ({before} -> {len(on_disk)})")
    print(f"applied: {path} ({before} -> {len(on_disk)} bytes)")


def worker_edit(text):
    anchor = "## Do NOT\n"
    assert anchor in text, "worker skill: '## Do NOT' anchor missing"
    return text.replace(anchor, WORKER_BLOCK + anchor, 1)


def reviewer_edit(text):
    assert REVIEWER_OLD in text, "reviewer skill: diff-sound bar not found"
    return text.replace(REVIEWER_OLD, REVIEWER_NEW, 1)


edit(WORKER, worker_edit, "## Changelog entries (never edit `CHANGELOG.md`)")
edit(REVIEWER, reviewer_edit, "Changelog check:")
print("ok")
sys.exit(0)
