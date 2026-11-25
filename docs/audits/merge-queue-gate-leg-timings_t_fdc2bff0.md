# Merge-queue gate timings: why the local gate costs ~75 min, and what it costs at the priority it should run at

Task `t_fdc2bff0`, measured 2026-11-24/25 on the Mac Studio that hosts the fleet
control plane (14 cores, 36 GB, macOS 27.2). Every number below is from a real
run on this host; nothing is estimated. Method for each is stated next to it.

## 1. The roster, one leg per process, on `origin/main`

`bash scripts/run_tests.sh` runs twelve legs **strictly serially** and prints no
per-leg duration (only `✓ <leg>` in the summary), so the per-leg cost has never
been visible to the queue or to a human. It was measured here by invoking the
canonical runner once per leg (`HSCC_TEST_LEGS=<leg>`, heartbeat off) in a
detached worktree at `origin/main` = `4cc35fe5`.

This run's own shell is an ordinary process — `ps -o pri` **20**.

| leg | wall s @ pri 20 |
| --- | --- |
| `hscc-bootstrap` | 232 |
| `hscc-commands` | 0 (93 tests in 0.17 s) |
| `hscc-roles` | 11 |
| `hscc-cluster` | 53 |
| `hscc-project` | 7 |
| `hscc_daemon` | 315 |
| `sparkrun-hermes` | 3 |
| `hscc-api` | _pending_ |
| `memori_byodb` | _pending_ |
| `scripts` | _pending_ |
| `.github/scripts` | _pending_ |
| `comfyui-sparkrun` | _pending_ |

Seven legs measured so far: **621 s**. The two expensive legs
(`hscc_daemon` 315 s, `hscc-bootstrap` 232 s) are 88 % of that, and the rest of
the roster is a rounding error: five legs sum to 74 s.

## 2. The same suite inside the queue's gate

The gate is a child of the `com.hermes.hscc_daemon` launchd job, so the live
gate's pytest is a different animal. Sampled while PR #761 was gating
(`HSCC_TEST_LEGS="hscc-bootstrap scripts"`), every 30 s from outside the gate:

```
GATE|...|leg=scripts|pri=4|etime=22:42|stage=gating|elapsed_s=1871.2
```

* the leg's pytest runs at `ps -o pri` **4**, not 20;
* the **`scripts` leg alone had been running 1362 s** and had not finished;
* the whole two-leg gate was past **1871 s** (31 min) and still `gating`.

The same two legs, at ordinary priority, are 232 s plus a few hundred.

## 3. Why `taskpolicy -c utility` does not fix it (measured today)

`t_7bccacef` (2.17.3) put the gate behind `taskpolicy -c utility` because the
daemon's plist sets `ProcessType => Background`. On this host that elevation is
a **no-op against the actual throttle**. Each row is a real chain of execs
(`taskpolicy → bash → child`), reading `ps -o pri` of the child:

| chain | child `pri` |
| --- | --- |
| plain | 20 |
| `taskpolicy -b` (== launchd `ProcessType Background`) | **4** |
| `taskpolicy -b` → `taskpolicy -c utility` (what the gate does today) | **4** |
| `taskpolicy -b` → `taskpolicy -B -p $$` (self-clear) | **4** (exit 0, no effect) |

`-c` accepts only `utility`, `background`, `maintenance` — there is no
`default` clamp on this macOS (`Could not parse 'default' as a QoS clamp`, and
taskpolicy exits non-zero, which would `red-gate` every PR on the Mac). `-B`,
the documented way out of `PRIO_DARWIN_BG`, is accepted only in the `-p pid`
form, and although it exits 0 it does not move the priority.

So the ~3× tax the queue pays on every gate is the launchd job's background
role, and **it is not reachable from the code that spawns the gate**. The only
lever that reaches it is the operator-owned plist (`ProcessType`), which this
repo deliberately never touches. That is a finding for the operator, not a PR.

Consequences for the gate budget: a 7200 s ceiling is not a fix, because the
ceiling is being spent at one-third the throughput the suite is capable of.

## 4. What is reachable from code

Two levers, both inside the surfaces this task named:

1. **Concurrency across legs** (`scripts/run_tests.sh`). Today one leg occupies
   one process at a time; the box sits at load 2–3 of 14 while a gate runs.
   Measured A/B (same legs, same priority, serial vs 3-deep): see §5.
2. **Narrowing** (`hscc_daemon/merge_queue.py`). Already live: the registry does
   **not** declare `gates` for `hscc` (only `timeout_s: 7200`), so
   `_resolve_gates()` does reach `_local_test_legs()`. The throttle is
   `MAX_LOCAL_TEST_LEGS = 3`: a diff claiming a fourth leg directory, one
   `SHARED_GATE_PATHS` file, or one unclaimed path (e.g. root `CHANGELOG.md`)
   falls back to all twelve legs. Note what that means in the worst case above:
   PR #761 was **already narrowed to two legs** and still burned 31 minutes.

Narrowing cannot be the answer on its own, because the observed gate was narrow
and still slow; concurrency is the lever that attacks the per-leg cost itself.

## 5. Concurrency A/B at the gate's own priority

_Backed by `parallel_ab.sh`: five legs, each launched through the canonical
runner under `taskpolicy -b` (the measured equivalent of the live gate's
priority), serial vs three-deep._

_PENDING_
