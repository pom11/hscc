kind: Security
task: t_ad3bd82e

- **The operator's absolute home path is gone from code, scripts and tests.**
  `pom11/hscc` is public, so a concrete `/Users/<operator>` path names the
  machine and account it was written on. Functional code now derives the value
  at runtime (`Path.home()` / `os.path.expanduser`), shell uses
  `${VAR:-$HOME/...}`, and fixtures use the sanctioned `/Users/hscc-fixture`
  user — so the suites run the same on any host.
- `hscc-bootstrap/enable_plugins.py` no longer ships a default
  `out_of_tree_repos` list naming private projects. The env-var shape is kept;
  an install that needs out-of-tree worktrees sets
  `HSCC_OUT_OF_TREE_WORKTREE_REPOS` explicitly.
- A tracked-tree grep for the operator's home path over the non-docs tree
  returns rc 1 (no output), and the pre-existing `/Users/*` fixtures are
  untouched.
