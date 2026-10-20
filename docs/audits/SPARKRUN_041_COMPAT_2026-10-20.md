# sparkrun v0.4.1 compatibility record — 2026-10-20 live cutover

Precedent: `SPARKRUN_040_COMPAT_2026-10-19.md` (the 0.3.10 -> 0.4.0 bump).
This record covers `v0.4.0 -> v0.4.1`. Live cutover ran **2026-10-20 08:37**
via the detached `sparkrun_proxy_venv_cutover.sh` (operator-run).

## What is live now

- `~/.local/bin/sparkrun` -> `~/sparkrun/.venv-sparkrun-041/bin/sparkrun`
  (editable install; the venv's `__editable__.sparkrun-0.4.1.pth` resolves to
  `~/dev/sparkrun-041-verify/src`).
- Checkout `~/dev/sparkrun-041-verify` on fork branch
  `feat/proxy-least-in-flight-041` @ `1aaa6479` = upstream tag `v0.4.1`
  **plus one fork commit** — the macOS-spawn test fix (module-level noop
  target in `_a_dead_pid`; a lambda is unpicklable under the spawn start
  method). `git log v0.4.1..HEAD` shows exactly that one commit.
- Rollback line kept intact: branch `-040` @ checkout `~/dev/sparkrun-live`
  / venv `.venv-sparkrun-040`. Rollback = repoint symlink to `-040` +
  `proxy stop`/`start` — detached only, never from a session riding :4000.

## Why the bump is safe for hscc's path

- `git diff v0.4.0 v0.4.1 -- src/sparkrun/proxy/config.py
  src/sparkrun/plugins/litellm/engine.py src/sparkrun/serving/` is **empty**
  (verified 2026-10-20 post-cutover). The surfaces hscc depends on —
  nested `gateways.litellm.router_settings` read, `ProxyConfig.save`, the
  balancer emission in the engine — are byte-identical across the two tags.
- `router_settings.routing_strategy: simple-shuffle` remains the expected
  base strategy with the balancer callback live (unchanged doctrine).
- hscc-side guards re-pointed in the same PR: `dep_bump_guard.FORK_BRANCH` /
  `SPARKRUN_CHECKOUT` + docstring to the `-041` topology; `sparkrun_bridge`
  live-build comment updated (it asserted the `040` venv chain).

## Test evidence

- **sparkrun 0.4.1 proxy/balancer tests (operator, live-adjacent run):**
  563 pass, 1 fail — `test_record_from_a_previous_boot_names_no_process`
  (`tests/test_proxy_foreground_supervision.py`), which **fails identically
  on 0.4.0**: pre-existing, not a 0.4.1 regression.
- **Prior side-by-side verification (2026-10-19, parked #55 comments):**
  targeted gate 151/151 identical to 0.4.0; full suite `333 failed / 13048
  passed / 95 skipped` vs `270 / 12724 / 95` baseline — delta fully
  accounted (+59 from 9 new upstream test files, +4 from `c9062945`, zero
  regressions among common tests). Host-dependent; the Linux/DGX
  classification owed before treating raw failure counts as signal.
- **hscc side:** full `scripts/run_tests.sh` gate rides the stamp on the
  merged tree before this PR lands (code card bar). Live post-cutover
  checks: `/v1/models` 200, both model aliases 200 with real content,
  `sparkrun_balancer.handler` present in the generated config, daemon
  kickstarted + running.

## Cutover-script regressions found during the live run (fixed on #56)

1. STEP 2 raced two separate `lsof` calls; a listener closing between them
   yielded an empty owner that hit the abort branch — false rollback.
   Now one `lsof` call per iteration; empty owner = port free.
2. Rollback (and restart) raced the draining proxy: litellm (child of the
   uvx wrapper) lives ~20s finishing ESTABLISHED streaming conns, so
   `proxy start` refused ("already running (PID ...)"). Now
   `snapshot_proxy_pids` before stop + bounded `wait_proxy_down`
   (60s + SIGTERM of the identified snapshot set) before any start, in
   both the forward path and `fail_rollback`.
