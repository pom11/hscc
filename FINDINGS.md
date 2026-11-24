# t_0a723a58 — TestCallersPipeReachesEof flakes in CI, never locally

Working notes. Branch `wt/t_0a723a58`, base `810adc8f`.

## Observed CI failure (real log, not inference)

Run `45307910039` (main @ `t_6283c79f` merge), job `leg hscc_daemon` (138156225803):

```
E   AssertionError: the `hscc api start` caller is still alive — the test would
    then be measuring its own fd, not the daemon's
hscc_daemon/tests/test_api_cli_detach_stdio.py:434: AssertionError
```

That is `test_start_releases_the_callers_stdout`, line 434: `assert proc.poll() is not None`.

## Root cause (hypothesis 1, from the log + reading `_handle_start`)

The pipe has THREE write-end holders: the driver process, the intermediate child,
and the grandchild. EOF needs all three closed.

The grandchild writes `report.json` *after* `_detach_stdio()` + `save_pid()`. The
driver process exits via a different, much slower path: `save_pid` (a file write) +
a **rich console panel print** + full CPython interpreter teardown. There is no
happens-before edge between "grandchild wrote the report" and "driver exited", so
`proc.poll()` at line 434 is a race between two independently scheduled processes.
Under CI load the driver loses → assertion fires. Locally the driver wins → passes.

Same root cause is the likely `assert eof` failure at line 440: EOF timing is
`max(driver exit, grandchild detach)`, and the driver exit is the unbounded long pole.

## The flake is ONE assertion, not the timeouts (6/6 sampled CI runs)

| CI run | job | failing test | file:line |
|---|---|---|---|
| 45307910039 (main) | 138156225803 | `test_start_releases_the_callers_stdout` | :434 |
| 45307808592 (#PR) | 138155983751 | `test_start_releases_the_callers_stdout` | :434 |
| 45306437875 | 138151383405 | `test_a_stray_holding_the_pipe_is_detected` | :479 |
| 45300542264 | 138131753947 | `test_a_stray_holding_the_pipe_is_detected` | :479 |
| 45298048191 | 138123405151 | `test_a_stray_holding_the_pipe_is_detected` | :479 |
| 45292853427 | 138105834601 | `test_a_stray_holding_the_pipe_is_detected` | :479 |

Both lines are the **same statement**: `assert proc.poll() is not None`.

Verbatim CI text (`assert None is not None` is line 479 — the same poll, messageless):

```
>           assert proc.poll() is not None
E           AssertionError: assert None is not None
E            +  where None = poll()
E            +    where poll = <Popen: returncode: None args: ['/Users/runner/work/hscc/hscc/.venv/bin/pyth...>.poll
```

Note the failure is on the line **after** `info = _wait_for_report(report, proc)`
returned — so the stray was demonstrably alive and inside `_stub_serve`, and the
*caller* was the thing that had not finished.

**Not one observed failure is `assert eof` (:440) or `assert not eof` (:486).**
`EOF_DEADLINE`/`NO_EOF_WINDOW`/`STRAY_SECONDS` have never flipped. The premise in
the card ("any assertion expressed as a duration") is half right: the class is
flaky, but the mechanism is a **state sampled at the wrong instant**, not a
window that is too short. Raising any timeout would not have touched it.

## Why `proc.poll()` is a race and `proc.wait()` is not

Three processes hold the caller's stdout write end: driver, intermediate child,
grandchild. `report.json` is written by the **grandchild** (after
`_detach_stdio()` + `save_pid()`). The **driver** exits on its own unrelated path:
`save_pid()` (file I/O) + a rich-console panel + full CPython teardown. There is
no happens-before edge between the two, so "the caller has already exited" was a
scheduling coin flip. `waitpid` makes it a fact.

## Negative result: pipe identity by inode does NOT work on macOS

Planned to prove "the stray holds *this* pipe" by comparing `(st_dev, st_ino)` of
the two ends. Probe (`$SCRATCH/probe_pipe_ino.py`, hermes venv, macOS):

```
read end : 0x0 1272839486304350031 True
write end: 0x0 10894816063677563751 True
SAME     : False
SAME ACROSS FORK: False
```

Both ends report `st_dev == 0` and **different inodes**; `MATCHES: False` for the
`Popen(stdout=PIPE)` case too. Would have been red in CI and red locally. Dropped.

Replacement: the stray `os.write(1, SENTINEL)`s **to fd 1** from the same code in
both postures. Bytes arriving on the test's read end can only come from a process
holding the write end — positive proof, portable, no identity needed. In the fixed
posture the same write lands on /dev/null and is never seen, which is itself the
discriminator.

## Fix shape

- `proc.poll()` peeks → `proc.wait()` (waitpid). Same observable, observed by a
  happens-before edge.
- "no EOF within 4 s" → read the sentinel (state: bytes arrived) then one
  `select(0)` probe returning `data`/`eof`/`empty`. "Still held" is read as a
  state; the 4 s stays a hang guard for the sentinel's arrival.
- Every ceiling keeps its current value. Nothing raised; no skip/xfail/retry.

## Status

- [x] CI evidence: 6/6 failures are the `proc.poll()` peek
- [x] pipe-inode approach tested and rejected (macOS)
- [ ] local red-before reproduction (deschedule the caller deliberately)
- [ ] fix + green
