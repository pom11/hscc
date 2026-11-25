kind: Changed
task: t_a8dc2f78

- **`app-build-checks` and `address-guard` now take `push` from `main` only**,
  the form `.github/workflows/pytest-legs.yml` already uses, so a pull request
  no longer pays for a second, identical copy of the whole gate on every branch
  commit. `app-build-checks` was the expensive one: the ubuntu selector
  `decide_app_build.sh` ends with `emit true "full-build-event:${EVENT}"`, so a
  push event always answers `apps=true` and the heavy macOS compile runs (9-27
  min per the workflow's own comment). Concurrency is `cancel-in-progress: true`
  on `${{ github.ref }}`, so the next push to the same branch cancelled the
  in-flight `apple` job, and `compile` — `needs: [changes, apple]` with
  `if: always()` — executed anyway and turned the cancel into a FAILURE stamped
  on that head SHA forever. Measured on PR #836: run 45688152056 (event `push`,
  head `678da1fe`) = `apple compile` CANCELLED + `compile` red, while run
  45688163856 (event `pull_request`, the same SHA) = `apple compile` SKIPPED +
  `compile` green. `hscc_daemon/merge_queue.py::_truthy_check` maps CANCELLED to
  not-green, so the merge queue read that SHA as red, handed the card back, and
  each retry's `update-branch` push cancelled the previous run again — a
  self-inflicted loop that cannot heal; PRs #827/#784/#715 carried the same
  non-green `apple compile`/`compile` at the time of writing. Restricting the
  trigger removes the duplicate, so nothing is left to cancel on a PR head, and
  returns macOS capacity to the pool that t_31679ad1 gated the compile for in
  the first place. `pull_request` is untouched in both files: a workflow that
  stops reporting on the PR head blocks every PR forever.
