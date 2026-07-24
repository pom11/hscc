# Audit: daemon self-management (escalate + escalate_watcher + autoscale)

## Bug 1

**File:** `hscc_daemon/escalate.py:27`
**Severity:** high
**Defect:** The OOM keyword check includes `"memory"`, which is so broad that any failure message containing the word "memory" gets classified as `"oom"` even if the failure is a test failure. This means the OOM category swallows the test-failure category.
**Concrete failure scenario:** Error text is `"OutOfMemoryError in test_load_data: assertion on row 5"`. The check on line 27 matches `"memory"` first and returns `"oom"`. The downstream `decide_escalation` then routes the task based on the OOM category instead of test-failure. An operator debugging this would see "oom" and look for GPU/OOM issues, wasting time because the root cause is an assertion error in a test.

## Bug 2

**File:** `hscc_daemon/escalate.py:31`
**Severity:** high
**Defect:** The test-failure keyword check includes `"failed"` (lowercased), which is so broad that any error message containing "failed" — including non-test failures like build failures, startup failures, or validation failures — gets classified as `"test-failure"`. This means the test-failure category swallows the "other" and "tooling" categories.
**Concrete failure scenario:** Error text is `"System failed to initialize: missing config section"` or `"Build failed with exit code 2"`. The check on line 31 matches `"failed"` and returns `"test-failure"`. `decide_escalation` then treats the failure as a test issue, reassigning the task to the strong tier with an incorrect category. An operator would waste time looking for test failures when the real problem is a build or config issue.

## Bug 3

**File:** `hscc_daemon/escalate.py:47`
**Severity:** medium
**Defect:** The `fail_limit` parameter's semantics are off by one relative to the docstring. The docstring says "number of consecutive failures *before* escalation kicks in", which reads as "escalation fires after N failures". But the condition `consecutive < fail_limit` means escalation triggers *at* N failures (i.e., when `consecutive == fail_limit`), not after N. A user who sets `fail_limit=3` expecting escalation only after 3 failures have occurred will actually see it trigger at the 3rd failure.
**Concrete failure scenario:** User sets `fail_limit=3` thinking the task will get 3 chances (failures 1, 2, 3) before escalation. But at consecutive_failure=3 the condition `3 < 3` is false, so escalation triggers immediately. The task only gets 2 "free" failures before being escalated, not 3.

## File: `hscc_daemon/escalate_watcher.py` — no correctness issues found.

SQL query is safe (no injection, correct status filter, correct column names). Subprocess uses list-based `subprocess.run` (no shell injection). Best-effort error handling is intentional and documented. Idempotency is handled by `decide_escalation`: once a task is reassigned to the strong profile, the next scan produces `"human"` instead of re-escalating.

## File: `hscc_daemon/autoscale.py` — no correctness issues found.

Bounds checking (`min`/`max` clamping) is correct. Scale-down condition (`waiting <= low_waiting and running == 0`) correctly requires full idleness. Missing-key handling via `or 0` is present for both `waiting` and `running`.