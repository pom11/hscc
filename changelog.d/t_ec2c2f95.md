kind: Security
task: t_ec2c2f95

- **The address guard now runs at commit time, not only under pytest.** This is
  a PUBLIC repo and real operator LAN/tailnet addresses have reached tracked
  files **twice**; most recently the orchestrator's ledger tick committed a
  verbatim `mount_nfs` command carrying the real NAS host and subnet, and it
  reached `origin/main`. `hscc_daemon/tests/test_no_real_addresses_committed.py`
  detected that leak correctly and still could not stop it, because its only
  trigger was somebody running the suite — and a docs-only commit never does.
  The guard was sound; its **trigger point** was wrong. Detection is now factored
  into one module, `scripts/address_guard.py`, shared by both gates:
  `.githooks/pre-commit` (the committed hook directory, armed by
  `core.hooksPath`) scans the **staged tree from the index**, and the pytest gate
  scans the tracked tree. A blocked commit prints the offending `file:line` plus
  the documented placeholders (`10.0.0.x` LAN, `100.64.0.1` tailnet). The hook
  reads staged blobs, never the working tree — the 2026-08-30 audit's pre-push
  check grepped the worktree, which had already been scrubbed while the committed
  blob still carried the address.
---
kind: Added
task: t_ec2c2f95

- **`.githooks/` + `hscc-bootstrap/install_hooks.py`** — hooks can now ship with
  the repository. Plain `.git/hooks` is not cloned, so it cannot be a mechanism;
  the committed `.githooks/pre-commit` plus `core.hooksPath .githooks` is.
  `install_hooks.py` is a new bootstrap stage (3c) and is idempotent: `verified`
  with zero writes on re-run, repairs a missing exec bit (git silently ignores a
  non-executable hook), and reports `skipped` — never a faked success — for a
  non-checkout runtime dir or a revision without the committed hooks. It writes
  `core.hooksPath` to the **COMMON** git config, so the dispatcher's worker
  worktrees inherit the guard from ONE install, including worktrees created
  outside the repo directory. Stdlib-only, one `git diff` plus one
  `git cat-file --batch`: ~0.13 s over the repo's 1070 tracked files, because a
  slow leak check gets bypassed with `--no-verify`.
