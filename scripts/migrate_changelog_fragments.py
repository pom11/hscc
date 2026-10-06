#!/usr/bin/env python3
"""ONE-SHOT: move CHANGELOG.md's existing ``[Unreleased]`` entries into
``changelog.d/<task-id>.md`` fragments (t_95f1d6e1).

Not a runtime tool and not wired into any test runner — it exists so the
migration is reproducible/auditable. Run from the repo root:

    python3 scripts/migrate_changelog_fragments.py            # dry run
    python3 scripts/migrate_changelog_fragments.py --apply

Attribution: every Unreleased line is ``git blame``d back to the docs commit
that added it; that commit's ``wt/t_<id>`` branch (its work branch, or the
branch that carried it into main) decides which fragment file the entry goes
to. Entries the blame cannot attribute to a task id go to
``changelog.d/pre-migration.md`` and are reported at the end — nothing is
dropped silently, and the script refuses to write if the re-assembled entries
do not cover every non-blank line of the original block.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import changelog_fragments as cf  # noqa: E402

REPO_ROOT = cf.REPO_ROOT


def sh(*args, cwd=REPO_ROOT) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout


def blame_authors(start: int, end: int):
    """Map absolute line numbers -> committing sha for lines [start, end].

    `git blame --line-porcelain` header form: `<sha> <orig_line> <final_line>`.
    """
    out = sh("git", "blame", "-L", f"{start},{end}", "--line-porcelain",
             "CHANGELOG.md")
    sha_for_line = {}
    cur = None
    for line in out.split("\n"):
        m = re.match(r"^([0-9a-f]{40}) (\d+) (\d+)", line)
        if m:
            cur = (m.group(1), int(m.group(3)))
        elif line.startswith("\t") and cur is not None:
            sha_for_line[cur[1]] = cur[0]
    return sha_for_line


def landing_task(sha: str, base_ref: str = "HEAD"):
    """Which kanban card's landing merge brought `sha` into the mainline.

    Walks the first-parent chain of `base_ref` and finds the *earliest* merge
    whose subject names a task id and which carries `sha` via a side parent
    (i.e. `sha` is not already an ancestor of that merge's first parent). That
    merge's subject — "merge: <title> (t_xxxxxxxx)" — is the card that authored
    the entry, which is exactly the fragment owner.

    Falls back to "a wt/t_<id> branch contains this sha" and finally to None
    (the caller then files the entry under `pre-migration`).
    """
    chain = sh("git", "rev-list", "--first-parent", base_ref).split()
    merges = []
    for c in chain[:400]:
        parents = sh("git", "rev-list", "--parents", "-n", "1", c).split()[1:]
        if len(parents) < 2:
            continue
        subject = sh("git", "log", "-1", "--format=%s", c).strip()
        m = re.search(r"(t_[0-9a-f]{6,})", subject)
        if m:
            merges.append((c, parents, m.group(1)))
    # earliest merge (closest to the entry) that admitted the sha from a side line
    for c, parents, task in reversed(merges):
        if _is_ancestor(sha, c) and not any(_is_ancestor(sha, p) for p in parents[:1]):
            return task
    # fallback: a worker branch named for a card still carries it
    refs = sh("git", "branch", "--all", "--contains", sha)
    best_key, best_task = None, None
    for ref in refs.split("\n"):
        ref = ref.strip().lstrip("* ").strip()
        if not ref or ref == "HEAD" or " -> " in ref:
            continue
        ref = ref.replace("refs/heads/", "").replace("remotes/origin/", "")
        m = re.search(r"(t_[0-9a-f]{6,})$", ref)
        if not m:
            continue
        key = (0 if ref.startswith("remotes/") else 1, len(ref))
        if best_key is None or key > best_key:
            best_key, best_task = key, m.group(1)
    return best_task


def _is_ancestor(sha: str, ref: str) -> bool:
    r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, ref],
                       cwd=REPO_ROOT, capture_output=True, text=True)
    return r.returncode == 0


def split_entries(block_lines):
    """[(kind, [lines], first_index)] from the inside of an [Unreleased] block.

    Positions are tracked rather than re-found by text search, so two
    byte-identical bullets in different places still blame correctly.
    """
    out = []
    kind = None
    cur = None
    cur_start = None
    for idx, line in enumerate(block_lines):
        if line.startswith("### "):
            # flush the pending entry BEFORE switching kind, otherwise the last
            # entry of each section is silently dropped (found by the
            # losslessness check below — it is the reason it exists).
            if cur is not None:
                out.append((kind, cur, cur_start))
                cur, cur_start = None, None
            kind = line[4:].strip()
            continue
        if re.match(r"^## \[", line):
            break
        if line.startswith("- "):
            if cur is not None:
                out.append((kind, cur, cur_start))
            cur, cur_start = [line], idx
        elif cur is not None and line.strip():
            cur.append(line)
        elif not line.strip():
            continue
    if cur is not None:
        out.append((kind, cur, cur_start))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the fragments")
    ap.add_argument("--fragments-dir", default=str(cf.FRAGMENTS_DIR))
    ap.add_argument("--changelog", default=str(cf.CHANGELOG))
    ns = ap.parse_args(argv)

    changelog = Path(ns.changelog)
    frag_dir = Path(ns.fragments_dir)
    text = changelog.read_text(encoding="utf-8")
    preamble, block, postamble = cf.split_changelog(text)
    if block is None:
        print("no [Unreleased] section found", file=sys.stderr)
        return 1
    block_lines = block.split("\n")
    start = text[: text.index(block)].count("\n") + 1  # 1-indexed line of '## [Unreleased]'
    end = start + len(block_lines) - 1

    sha_for_line = blame_authors(start, end)
    entries = split_entries(block_lines[1:])
    if not entries:
        print("nothing to migrate", file=sys.stderr)
        return 1

    by_task = OrderedDict()  # task id -> [(kind, lines)]
    unmapped = []
    for kind, lines, first in entries:
        # `entries` was built from block_lines[1:], so absolute 1-indexed line
        # of block_lines[0] ('## [Unreleased]') is `start`.
        sha = sha_for_line.get(start + 1 + first)
        task = landing_task(sha) if sha else None
        key = task or "pre-migration"
        if not task:
            unmapped.append((kind, lines[0][:70], (sha or "")[:8]))
        by_task.setdefault(key, []).append((kind, lines))

    # --- safety: every non-blank entry line must land in exactly one fragment
    original_lines = [l.rstrip() for l in block_lines
                      if l.strip() and not l.startswith("## ")
                      and not l.startswith("### ") and not l.startswith("<!--")]
    moved = [l.rstrip() for group in by_task.values() for _, lines in group for l in lines]
    if sorted(original_lines) != sorted(moved):
        missing = [l for l in original_lines if l not in moved]
        extra = [l for l in moved if l not in original_lines]
        print("SAFETY: migration is not lossless — refusing to write",
              file=sys.stderr)
        for l in missing[:10]:
            print(f"  LOST:   {l}", file=sys.stderr)
        for l in extra[:10]:
            print(f"  EXTRA:  {l}", file=sys.stderr)
        return 1

    print(f"{len(entries)} entries -> {len(by_task)} fragment files "
          f"({len(original_lines)} entry lines, all accounted for)\n")
    for task, group in by_task.items():
        kinds = []
        for kind, lines in group:
            kinds.append(kind)
        print(f"  changelog.d/{task}.md  ::  {len(group)} entries "
              f"({', '.join(kinds)})")
    if unmapped:
        print("\n  not attributable to a task id (-> pre-migration.md):")
        for kind, head, sha in unmapped:
            print(f"    [{kind}] {head!r} @{sha}")

    if not ns.apply:
        print("\ndry run — pass --apply to write")
        return 0

    frag_dir.mkdir(parents=True, exist_ok=True)
    for task, group in by_task.items():
        chunks = []
        for kind, lines in group:
            chunks.append("\n".join([f"kind: {kind}", f"task: {task}", ""] + lines))
        (frag_dir / f"{task}.md").write_text("\n---\n".join(chunks) + "\n",
                                             encoding="utf-8")

    # CHANGELOG.md keeps its structure; the [Unreleased] body is replaced by a
    # pointer so the generated block is not duplicated in two places. The
    # fragments are the record; `sync` re-materialises them for the release.
    pointer = "\n".join([
        cf.UNRELEASED_HEADING,
        "",
        cf.GENERATED_MARKER,
        "",
        "_Entries now live in [`changelog.d/`](changelog.d/) — one "
        "`<task-id>.md` per kanban card. Run `python3 "
        "scripts/changelog_fragments.py sync` to materialise them here "
        "(the release step does; see [changelog.d/README.md]"
        "(changelog.d/README.md))._",
    ])
    new_text = cf.assemble(preamble, pointer, postamble)
    tmp = changelog.with_name(changelog.name + ".tmp-migrate")
    tmp.write_text(new_text, encoding="utf-8")
    import os
    os.replace(tmp, changelog)
    print(f"\nwrote {len(by_task)} fragment files + CHANGELOG.md pointer block")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
