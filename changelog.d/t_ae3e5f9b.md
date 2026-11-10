kind: Fixed
task: t_ae3e5f9b

- **`.fastembed_cache/` (87 MB of ONNX weights) can no longer ride into a commit
  on `git add -A`.** The directory was untracked and matched by no rule:
  `git check-ignore -v .fastembed_cache` exited 1 and `git status --short` listed
  `?? .fastembed_cache/`. This is the same class as the repo-root `db.sqlite`
  that a `git add -A` did commit in PR #275, and one `git add -A` from any worker
  would put 87 MB of model weights into the object store **permanently** — a
  `git rm` afterwards does not get the blobs back out.
  The rule is `.fastembed_cache/` in the *Runtime data — machine-specific state,
  never source* block, not next to the sqlite rules the card pointed at:
  **`*.sqlite` / `*.sqlite3` are not on `main`.** They were added by `071fdb4e`
  and `git merge-base --is-ancestor 071fdb4e main` says NO — that commit exists
  only on `fix/t_56f66242-pr275-db-sqlite`, `wt/t_cda8adf0` and `prep/t_899bff0b-post-pr275`,
  so the sqlite gap that PR #275 exposed is still open on `main` and needs its
  own card. Runtime state is also the truer home: this is not a source-tree
  artifact at all.
  **What writes it, so the rule is justified rather than swallowed:** no `.py` in
  this repo mentions fastembed (`grep -rn "fastembed" --include=*.py .` → 0 hits),
  because it is a *transitive* write. `memori_byodb/_client.py:90` does
  `from memori import Memori`, and the memori SDK (3.3.6) vendors the Rust
  `fastembed` crate — `memori_python.abi3.so` carries the literal
  `.fastembed_cache` default, the `FASTEMBED_CACHE_DIR` override, and the model
  table entry `AllMiniLML6V2 → Qdrant/all-MiniLM-L6-v2-onnx / model.onnx`, which
  is byte-for-byte the directory content
  (`models--Qdrant--all-MiniLM-L6-v2-onnx/{blobs,refs,snapshots}`). memori's
  default embedding model is exactly that one (`memori/_config.py:62`
  `self.model = "all-MiniLM-L6-v2"`). Because the cache path resolves under the
  **current working directory**, the download lands wherever an agent or test
  run happens to cwd — which is why this 87 MB copy at the repo root (Oct 28) is
  one of **seven** identical ones on this host, spread over `dev/hscc`, two
  `dev/gateclerk/.worktrees` roots and two `ecofire-bc` workspaces (Oct 8 → Nov 5):
  the cache follows cwd, not the project. A repo therefore cannot redirect it
  (that would need `FASTEMBED_CACHE_DIR` set by whoever spawns the agent — filed
  as follow-up work); it can only refuse to commit it. Hence ignore, not delete:
  the directory is a cache other runs reuse, and re-fetching costs bandwidth.

---

kind: Verified
task: t_ae3e5f9b

- **The rule matches, measured against the real 87 MB directory and against a
  probe dir.** `git check-ignore -v .fastembed_cache/x` in this branch's worktree
  prints `.gitignore:27:.fastembed_cache/`. The card's baseline was re-verified on
  `main` first: there `git check-ignore -v .fastembed_cache` exits **1 with no
  output** while `git status --short` lists `?? .fastembed_cache/`.
  Against the *actual* 87 MB directory in the `/Users/desac/dev/hscc`
  checkout — this branch cannot modify that checkout, because merging is not mine
  to do — the same rule was applied read-only via
  `-c core.excludesFile=<this .gitignore>`: `check-ignore -v .fastembed_cache` then
  prints `.gitignore:27:.fastembed_cache/` and `git status --short` no longer lists
  `?? .fastembed_cache/`, with `du -sh` still `87M` and `blobs/refs/snapshots`
  intact. Nothing in the main checkout was written, deleted or cleaned.
- **One pitfall recorded, because it looks like a failure.** `git check-ignore -v
  .fastembed_cache` prints nothing for a path that does not exist *yet*, even with
  the rule in place: a trailing-slash pattern matches directories only, and git
  cannot prove an absent path is one. Measured in the same worktree — path absent
  → rc 1; the same path as a directory → rc 0 with the rule line; any path *under*
  it (`.fastembed_cache/x`) → rc 0 with the rule line while it is still absent.
  So the rule is right and the bare command is the unreliable one; the `!!` line in
  `git status --short --ignored` is the check that does not lie either way.
- **Non-vacuity in both directions.** With a scratch `.fastembed_cache/` holding a
  file, `git status --short` omits it while still listing a sibling scratch dir
  created the same moment (so the check is not blind), and
  `git status --short --ignored` lists the cache as `!! .fastembed_cache/`. Both
  scratch dirs were created by the probe and removed by it; the real cache was
  never touched.
- **Diff scope is two files.** `.gitignore` gains 9 lines (1 pattern + 8 comment);
  `changelog.d/t_ae3e5f9b.md` is new. Nothing else in `.gitignore` was reordered,
  reworded or removed — `git diff --numstat` reports `9 0 .gitignore`, i.e. zero
  deletions. `python3 scripts/changelog_fragments.py check` exits 0
  (`OK — [Unreleased] is pointer, 7 fragment entries consistent`); `CHANGELOG.md`
  is untouched.

