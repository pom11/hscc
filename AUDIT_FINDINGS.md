# Audit: daemon self-management (escalate + escalate_watcher + autoscale)

## Finding 1

**Location:** hscc_daemon/escalate.py:27
**Severity:** high
**Defect:** The `"memory" in text` substring check inside the OOM branch at line 27 is so broad that any error containing the word "memory" gets classified as "oom", swallowing the test-failure and other categories. The OOM check (line 27) runs before the test-failure check (line 31), so a test error mentioning memory is misclassified.
**Concrete failure scenario:** Error text: `"OutOfMemoryError during test_load_data: assertion on row 5"`. The `"memory"` check at line 27 matches (lowercased: "outofmemoryerror during test_load_data: assertion on row 5" contains "memory"). Returns `"oom"`. The task is then treated as an OOM failure rather than a test failure, causing operators to debug GPU memory when the real problem is an assertion error in the test suite.

## Finding 2

**Location:** hscc_daemon/escalate.py:31
**Severity:** high
**Defect:** The `"failed" in text` substring check inside the test-failure branch at line 31 is so broad that any error containing the word "failed" gets classified as "test-failure", swallowing the "other" and "tooling" categories.
**Concrete failure scenario:** Error text: `"Build failed due to missing dependency libfoo"`. The check at line 31 matches `"failed"` and returns `"test-failure"`. The task is then escalated as a test failure when it's actually a build/dependency issue. Operators looking for test failures will not fix the real problem.

## Finding 3

**Location:** hscc_daemon/escalate_watcher.py:126
**Severity:** medium
**Defect:** The return value of `_reassign()` at line 126 is ignored. The action is appended to `actions` regardless of whether the reassign succeeded or failed. The returned list then contains "escalate" entries that were never actually acted upon, masking real failures.
**Concrete failure scenario:** The hermes CLI is unreachable (network error, binary not found, hermes server down). `_default_reassign` returns `False` at line 52 or line 54. But line 126 doesn't check the return value, so `actions.append(...)` still executes with `"action": "escalate"`. The caller sees a successful escalation report but the task was never reassigned. The failing task continues on its original assignee, potentially triggering the same escalation again and again on each scan, creating a silent loop where the task never actually gets reassigned.

## Clean files

**hscc_daemon/escalate_watcher.py:** No correctness issues with the SQL query (no injection, correct status filter, correct column names). Subprocess uses list-based `subprocess.run` (no shell injection). Best-effort error handling is documented at the top of the file.

**hscc_daemon/autoscale.py:** No correctness issues. Bounds checking is correct (`min`/`max` clamping on both scale-up and scale-down paths). Scale-down condition (`waiting <= low_waiting and running == 0 and current_workers > min_workers`) correctly requires full idleness. Missing-key handling via `or 0` at lines 36-37 is present. The caller at `hscc.py:547` derives `current_workers` from `nodes_ok` which is always >= 0.