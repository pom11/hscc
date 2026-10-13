kind: Added
task: t_no_coauthor_20261013

- **`.githooks/commit-msg` strips attribution trailers** (`Co-Authored-By`,
  `Generated-By`, `Assisted-By`) from every commit message, in every clone and
  worktree (same `core.hooksPath` mechanism as the address guard). Agents were
  adding `Co-Authored-By: Hermes <hermes@nousresearch.com>`, which GitHub
  attributed to an unrelated account on this public repo.
