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
  the repository. Plain `.git/hooks` is not cloned, so it cannot be the mechanism;
  the committed `.githooks/pre-commit` plus `core.hooksPath .githooks` is.
  `install_hooks.py` is a new bootstrap stage (3c) and is idempotent: `verified`
  with zero writes on re-run, repairs a missing exec bit (git silently ignores a
  non-executable hook). It arms a checkout **only when both halves — the hook and
  `scripts/address_guard.py` — exist in that checkout**: `core.hooksPath` lives in
  the COMMON config and is relative, so writing it from a partial checkout would
  make every sibling worktree report itself armed while it silently finds no hook
  (measured live during this card's review; `posture()` / `install_hooks.py
  --check` reports the truth per checkout: `armed` / `unarmed` / `armed-but-absent`
  / `not-a-repo`). Reports `skipped` — never a faked success — for a non-checkout
  runtime dir or an incomplete revision. When complete, the COMMON-config write
  means every linked worktree — the dispatcher's worker worktrees included, even
  ones created outside the repo directory — is armed by ONE install. Stdlib-only,
  one `git diff` plus one `git cat-file --batch`: measured **0.133 s** over the
  repo's 1070 tracked files (13.3 MB), because a slow leak check gets bypassed
  with `--no-verify`.
