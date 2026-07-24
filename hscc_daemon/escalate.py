"""Failure-escalation decision logic for kanban tasks.

Pure functions — no I/O, no side effects.
Decides what to do with a repeatedly-failing kanban task.
"""


def classify_failure(error_text):
    """Classify an error string into a failure category.

    Args:
        error_text: The last_failure_error string, or None/empty.

    Returns:
        One of: "timeout", "oom", "test-failure", "tooling", "other".
    """
    if not error_text:
        return "other"

    text = str(error_text).lower()

    # Timeout
    if "timeout" in text or "timed out" in text:
        return "timeout"

    # OOM / memory
    if "oom" in text or "out of memory" in text or "killed" in text or "memory" in text:
        return "oom"

    # Tooling / import issues
    if "importerror" in text or "modulenotfound" in text or "traceback" in text or "import" in text:
        return "tooling"

    # Test failures
    if "test" in text or "assert" in text or "pytest" in text or "failed" in text:
        return "test-failure"

    return "other"


def decide_escalation(task, *, fail_limit=3, strong_profile="architect"):
    """Decide what to do with a failing kanban task.

    Args:
        task: dict with keys like id, assignee, consecutive_failures,
              status, last_failure_error.  Missing keys are tolerated.
        fail_limit: number of consecutive failures before escalation.
        strong_profile: profile name considered the "strong tier".

    Returns:
        dict with at least {"action": ...} and context fields depending
        on the chosen action.
    """
    consecutive = task.get("consecutive_failures", 0) or 0
    assignee = task.get("assignee", "") or ""

    if consecutive < fail_limit:
        return {"action": "none"}

    category = classify_failure(task.get("last_failure_error"))

    if assignee != strong_profile:
        return {
            "action": "escalate",
            "reassign_to": strong_profile,
            "category": category,
            "reason": (
                f"{consecutive} consecutive failures on {assignee}; "
                f"escalating to strong tier"
            ),
        }

    return {
        "action": "human",
        "category": category,
        "reason": (
            f"strong tier ({strong_profile}) also failing after "
            f"{consecutive} attempts; needs a human"
        ),
    }
