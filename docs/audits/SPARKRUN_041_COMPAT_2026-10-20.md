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

- **sparkrun proxy/balancer tests — the reproducible pair (selection +
  tally), verify run `t_ebdb02a5`, 2026-10-20:**

  ```
  cd <checkout> && <venv>/bin/python -m pytest -q -p no:cacheprovider \
      tests/test_litellm_balancer.py tests/test_proxy*.py        # 24 files
  ```

  | checkout @ head | venv | tally |
  |---|---|---|
  | `~/dev/sparkrun-041-verify` @ `1aaa6479` (0.4.1) | `.venv-sparkrun-041` | **1 failed, 497 passed** (498 collected) |
  | `~/dev/sparkrun-live` @ `b5c60edd` (0.4.0) | `.venv-sparkrun-040` | **1 failed, 497 passed** |

  Identical selection, identical tally on both tags, RC=1 both. The single
  failure is the same node id, assertion and line on each:
  `tests/test_proxy_foreground_supervision.py::test_record_from_a_previous_boot_names_no_process`
  (`assert engine.current_pid() is None`).

  Pre-existing, not a 0.4.1 regression — proven more strongly than by any
  rerun: the whole `src/sparkrun/proxy` package **and that test file** are
  byte-identical across `v0.4.0..v0.4.1`, so no behaviour change can explain
  it. Mechanism: `_boot_id()` reads `/proc/sys/kernel/random/boot_id`, which
  does not exist on macOS, so `_from_previous_boot()` can never be true on
  this host. Carry as a known host-dependent failure; the Linux/DGX
  classification is still owed before raw failure counts are treated as
  signal.
- **Superseded figure — historical note, NOT reproducible (do not reuse):**
  this record previously cited the operator's live-adjacent run as "563 pass,
  1 fail". No selection named as "the proxy/balancer test set" reproduces
  that. The verify run enumerated the plausible surfaces — the 24 files above
  collect **498** (re-checked here with `--collect-only`, no test bodies run),
  `+ tests/test_litellm_query_contract.py` = 522, `-k "proxy or balancer"`
  over `tests/` = 527, whole tree = 13476 — and counted 106,991 file-subset
  solutions for the remaining 66-test gap, so the number cannot be
  reconstructed: a pass count not tied to a selection is not gate evidence.
  Recorded here only as the figure that was reported at cutover time.
  Corroboration of the *single-failure* claim (independent of any tally): the
  operator's own `.pytest_cache/v/cache/lastfailed` in both checkouts holds
  exactly one entry inside this set — that same test. Full accounting in PR
  #55 comment `6286046217` (verify run `t_ebdb02a5`).
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
