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


def decide_escalation(task, *, fail_limit=3, strong_profile=None):
    """Decide what to do with a failing kanban task.

    Acting escalation (reassign to the strong tier) is OPT-IN: it runs only
    when the operator explicitly configured a strong profile. With no
    ``strong_profile`` (the default), at-threshold failures surface to a HUMAN
    instead of silently rewriting the card's assignee — reassigning to a
    profile that may not be able to execute the card corrupted the assignment,
    bounced the card through pointless dispatch cycles, and left the operator
    to manually ``assign``+``unblock`` it back.

    Args:
        task: dict with keys like id, assignee, consecutive_failures,
              status, last_failure_error.  Missing keys are tolerated.
        fail_limit: number of consecutive failures before escalation.
        strong_profile: profile name considered the "strong tier". When
              falsy/None, no reassignment happens — the decision is "human".

    Returns:
        dict with at least {"action": ...} and context fields depending
        on the chosen action.
    """
    consecutive = task.get("consecutive_failures", 0) or 0
    assignee = task.get("assignee", "") or ""

    if consecutive < fail_limit:
        return {"action": "none"}

    category = classify_failure(task.get("last_failure_error"))

    if strong_profile and assignee != strong_profile:
        return {
            "action": "escalate",
            "reassign_to": strong_profile,
            "category": category,
            "reason": (
                f"{consecutive} consecutive failures on {assignee}; "
                f"escalating to strong tier ({strong_profile})"
            ),
        }

    # Human branch: either the configured strong profile is itself the failing
    # assignee, or no strong profile was configured (acting escalation opt-in
    # OFF). Name the actual failing assignee so the alert is actionable regardless
    # of which case.
    shown_assignee = assignee or strong_profile or "unassigned"
    return {
        "action": "human",
        "category": category,
        "reason": (
            f"{shown_assignee} ({strong_profile or 'no strong profile'}) "
            f"also failing after {consecutive} attempts; needs a human"
        ),
    }
