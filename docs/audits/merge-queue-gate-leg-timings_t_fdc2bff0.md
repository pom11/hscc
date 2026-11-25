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
| `hscc-api` | 456 |
| `memori_byodb` | 1 |
| `scripts` | 192 |
| `.github/scripts` | 16 |
| `comfyui-sparkrun` | 37 |
| **roster** | **1323 s (22 min), every leg green** |

The distribution is the finding: `hscc-api` + `hscc_daemon` + `hscc-bootstrap`
are 1003 s = **76 % of the suite**, those three plus `scripts` are 90 %, and the
other seven legs together cost 128 s. Any change that only touches "the rest of
the roster" cannot move this gate.

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

### 5.1 The gate's own tax, leg by leg

The `scripts` leg is the clean comparison, because both sides ran exactly it:

| | `scripts` leg |
| --- | --- |
| this repo's runner at pri 20 (§1) | **192 s** |
| the live queue gate at pri 4 (§2) | **≥ 1362 s and still running** |

≥ **7.1x** on one leg, from the execution environment alone. The two-leg gate
PR #761 was holding (`hscc_daemon scripts`, 424 s of suite at pri 20) had burned
**1871 s** — the same ≥4x. And the twelve-leg roster extrapolates to ~5900 s at
that ratio, which is the ~75 min the queue has been spending.

### 5.2 Concurrency does pay at the gate's priority, but is capped by one leg

Five legs, each run through the canonical runner under `taskpolicy -b` (the
measured equivalent of the live gate's priority, pri 4), one arm serial and one
three-deep, same machine, same moment:

| arm | wall |
| --- | --- |
| serial (what `run_tests.sh` does today) | 188 s |
| three-deep | **96 s (1.96x)** |

Every leg was green in both arms (`rc=0` ×5 ×2), so these five legs do not
interfere when concurrent, and no leg slowed down when its neighbours ran: the
worst leg took 93 s parallel against 107 s serial. The box had headroom — load
2–3 on 14 cores while a gate ran.

What that bounds, applied to the real roster: a p=3 pack can never beat the
longest leg, and `hscc-api` at pri 4 is ~456 × 4 ≈ **30 min on its own**. So
leg-level concurrency alone does **not** get a full-twelve-leg gate under 30
min. It does get the *narrowed* gate there: the gate observed in §2 is two
legs (`hscc_daemon` + `scripts` ≈ 2280 s serial at pri 4), and the best possible
two-deep pack is the larger of them ≈ **24 min < 30 min** — which is the target,
for the shape of PR the queue actually processes.

### 5.3 The ranking that follows from the measurement

1. **The launchd job's background role is the biggest single tax** (≥4–7x) and
   it is not reachable from the code that spawns the gate (§3). It is one line
   of operator-owned plist (`ProcessType`), and it puts the whole roster at the
   measured 22 min without touching a test. This is a finding **for the
   operator**, not a PR: this repo deliberately never edits that plist.
2. **Bounded leg concurrency** is the biggest win that *is* in code: ~2x
   measured, and enough to bring a typical narrowed gate inside 30 min. It needs
   three things this audit could not settle in its own budget, each of which is
   a verification rather than a guess: per-leg output to separate files so the
   `FAILED path::test` digest stays parseable, the heartbeat's
   "pytest is a child of the runner" discovery reworked for N concurrent legs
   (t_ecb84764 built that signal for a reason), and an interference run over the
   *whole* roster — the five legs above are green, but `hscc-api` and
   `hscc_daemon` bind loopback sockets and must be shown to still be green with
   neighbours.
3. **Narrowing** (`MAX_LOCAL_TEST_LEGS = 3`, `merge_queue.py:219`) is already
   live and already being used by the gates observed here; raising it only helps
   the diffs that touch four or more leg directories, and cannot beat §5.2's
   longest-leg ceiling either.

