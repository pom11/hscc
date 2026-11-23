kind: Fixed
task: t_71ed3b1c

- **Load tests now own their burners.** The 2026-11-23 load test spawned twelve
  `while :; do :; done` burners and its `kill $BURNERS` never reached them:
  they ran orphaned at ~90% CPU each and held the box at load 24 for 3h40m.
  A PID list is not ownership, and cleanup after the command is not a trap — it
  does not run when the test hangs or is interrupted. The new
  `scripts/load_burners.sh` harness owns all three legs:
  * every burner (and the command under test) is started under `set -m`, so
    `$!` is a **process-group** id and cleanup uses `kill -- -PGID`, never a
    bare PID;
  * EXIT, INT, TERM and HUP all route through one idempotent cleanup that
    TERMs each group, waits a bounded 0.2 s grace, then KILLs each group;
  * a watchdog enforces a **hard timeout** and TERMs the harness. The command
    runs in the background and is awaited with `wait` on purpose: a POSIX shell
    defers a trap while waiting for a foreground child (measured here, TERM was
    held for the full 30 s of a foreground `sleep`), so the obvious foreground
    shape would let a hung test outlive its timeout and keep its burners.
  Burner argv carries a unique tag (`HSCC_LOAD_BURNER_TAG`) so survivors are
  greppable without matching unrelated processes. No other committed load test
  spawned burners (`git grep BURNERS` was empty), so there was nothing to
  convert; the guard below is what stops the next inline copy.
- `scripts/tests/test_load_burners.py` (7 items) is the guard. It spawns the
  real harness, first proves two tagged burners actually exist (each leading
  its own PGID), then signals the harness and asserts `pgrep` finds zero
  survivors for TERM/INT/HUP. It also pins the hard-timeout exit (124, bounded),
  command-status propagation, and a static audit that fails if any file under a
  `tests/` dir inlines `while :; do :; done` instead of using the harness.
---
kind: Verified
task: t_71ed3b1c

- `scripts/tests/test_load_burners.py`: **7 passed in 2.98 s** with the hermes
  venv interpreter, and a direct smoke run of the real harness
  (`scripts/load_burners.sh 1 5 -- sh -c 'echo ok'`, exit 0) — the harness is a
  script, so both were run rather than inferred from pytest. Burner counts stay
  at 1–2 and each run is under a second, so CI never loads the box.
