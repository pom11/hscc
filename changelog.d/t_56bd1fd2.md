kind: Changed
task: t_56bd1fd2

- **A red test leg now blocks a merge, including for an admin.** The legs were
  advisory by default: `HSCC_TESTS_ENFORCE` was unset, so `leg_verdict.sh` turned
  every failed leg into a `::warning::` and left the job `success`, and `main` had
  **no branch protection at all** — a red leg was therefore information nobody was
  obliged to read, which is how #587 merged `hscc-roles` red and #589 merged over
  it. The posture is now: `HSCC_TESTS_ENFORCE=true` as a repository variable, and
  branch protection on `main` requiring the single check **`legs`** with
  `strict=false` and `enforce_admins=true`.
* `legs` and not `leg *` is the required context, deliberately. `decide_legs.sh`
  runs only the legs a change's paths can break, so a PR reports a *subset* (1 of
  12 legs on this card's own probe PR, 2 of 12 on #593). A `leg *` requirement
  would leave the never-running legs permanently "expected" and nothing could ever
  merge. The aggregator carries every selected leg's verdict and still reddens when
  a leg could not run.
* `strict=false` so a head is not forced into a rebase before every merge; the
  push-to-main run of the same workflow is the full roster and is what catches a
  wrong selection map. `enforce_admins=true` because the fleet's merge path *is*
  the admin token — required checks an admin bypasses are decorative on the only
  path that merges. Two one-command escapes (set the variable `false`, or rewrite
  the protection with `enforce_admins: false`) are written into the workflow
  comment, together with the fact that a **harness** red — a killed leg, no
  summary — is red by design and the variable does not clear it.
---
kind: Verified
task: t_56bd1fd2

- Gate measured before the flip, at `94828d99`: `select` chose
  `legs: FULL SUITE (12)`, every one of the 12 `leg *` check-runs `success` with
  **zero** red-leg annotations, aggregator `legs` → `all selected legs green`. The
  annotation scan is the measurement, not the conclusions: at `df7d517e` all 12
  jobs read `success` while three carried the advisory red.
- Block proven by a throwaway branch carrying one `assert 1 == 0`
  (PR #605, closed and deleted): `leg hscc-roles` job **red** —
  `1 failed, 357 passed, 10 skipped`, `✗ hscc-roles (pytest exit 1)`,
  `::error::a test leg is red (HSCC_TESTS_ENFORCE is on)` — `legs` **red** —
  `select: success` / `leg matrix: failure` → `at least one selected leg is red or
  could not run` — and `mergeStateStatus: BLOCKED` while `mergeable: MERGEABLE`, as
  the admin (`pom11`).
* The control is that the red was the **test**: `pytest exit 1` with a real
  `━━━ Summary ━━━`, not `leg_verdict.sh`'s `no summary` or `not a test-failure
  code` branches. The live control on the other side is #598, merged by `pom11`
  two minutes after the flip because its `legs` was green.
