"""lint.py — card quality lint: flag cards missing quality markers.

Pure decision logic, no I/O. Which elements a card must have is decided
here; module line counts are *injected* by the caller so tests never touch
the filesystem, and a referenced module whose line count is unknown is never
flagged as large (the ``never report a state you have not verified``
principle). Production ``commands/lint.py`` resolves referenced modules to
real files and counts their lines; tests pass a controlled mapping instead.

A card PASSES (no issues) when it carries the quality markers appropriate to
its situation:

1. a ``VERIFY:`` line — how the work will be checked;
2. an acceptance criterion — how *done* is judged (an ``ACCEPTANCE`` /
   ``ACCEPT:`` marker or a ``TESTS:`` section);
3. concrete file/function references (``path/to/mod.py:123`` or
   ``mod.py:func_name``) whenever the card references a module over
   ``module_line_threshold`` lines — the measured evidence that naming exact
   functions and line numbers is what turns an abstract task (1h45m, zero
   commits) into a doable one (succeeds first try). Card quality *is*
   throughput.
"""

from __future__ import annotations

import re

# A module is "large" (and so demands concrete references in any card that
# references it) when it exceeds this many lines.
DEFAULT_MODULE_LINE_THRESHOLD = 500

# A ``VERIFY:`` line, case-insensitive, optionally bulleted/indented.
_VERIFY_RE = re.compile(r"^\s*(?:-\s+)?verify\s*:", re.MULTILINE | re.IGNORECASE)
# An acceptance marker: an ``ACCEPTANCE`` / ``ACCEPT:`` line, or a ``TESTS:`` /
# ``TEST:`` section enumerating how done is judged.
_ACCEPTANCE_RE = re.compile(
    r"^\s*(?:-\s+)?(?:acceptance|accept)\s*:?",
    re.MULTILINE | re.IGNORECASE,
)
_TESTS_RE = re.compile(
    r"^\s*(?:-\s+)?tests?\s*:", re.MULTILINE | re.IGNORECASE
)
# Any mention of a ``.py`` module path. Normalised (see referenced_modules).
_MODULE_RE = re.compile(r"([A-Za-z0-9_./-]+\.py)\b")
# A concrete file/function reference: a ``.py`` path followed by ``:`` and a
# non-empty token (a line number or a function/method name).
_CONCRETE_REF_RE = re.compile(
    r"[A-Za-z0-9_./-]+\.py\s*:\s*[A-Za-z0-9_]+",
    re.IGNORECASE,
)


def _has_verify(body: str) -> bool:
    return bool(_VERIFY_RE.search(body))


def _has_acceptance(body: str) -> bool:
    return bool(_ACCEPTANCE_RE.search(body) or _TESTS_RE.search(body))


def referenced_modules(body: str) -> list[str]:
    """The deduped, order-preserving list of ``.py`` module paths the body
    mentions, normalised (leading ``./`` stripped). Callers resolve these to
    real files and count their lines; this function stays pure."""
    seen: dict[str, None] = {}
    for m in _MODULE_RE.findall(body):
        norm = m
        while norm.startswith("./"):
            norm = norm[2:]
        if norm not in seen:
            seen[norm] = None
    return list(seen)


def _large_modules(
    body: str,
    module_line_counts: dict[str, int] | None,
    threshold: int,
) -> list[str]:
    """Referenced modules with a KNOWN line count over ``threshold``.

    A referenced module whose line count is absent from ``module_line_counts``
    (i.e. unresolved) is deliberately NOT flagged — we refuse to call a module
    large without verifying its size.
    """
    counts = module_line_counts or {}
    large: list[str] = []
    for m in referenced_modules(body):
        if (counts.get(m) or 0) > threshold:
            large.append(m)
    return large


def _has_concrete_refs(body: str) -> bool:
    return bool(_CONCRETE_REF_RE.search(body))


def lint_card(
    card: dict,
    *,
    module_line_counts: dict[str, int] | None = None,
    module_line_threshold: int = DEFAULT_MODULE_LINE_THRESHOLD,
) -> list[str]:
    """Lint one card. Returns the list of issues; an empty list means PASS.

    Injects ``module_line_counts`` ({normalized_module_path: line_count} for
    every module the card might reference, resolved by the caller) so this
    function never touches the filesystem. Never mutates ``card``.
    """
    body = card.get("body") or ""
    issues: list[str] = []

    if not _has_verify(body):
        issues.append("missing VERIFY: line")

    if not _has_acceptance(body):
        issues.append("missing acceptance criterion")

    large = _large_modules(body, module_line_counts, module_line_threshold)
    if large and not _has_concrete_refs(body):
        issues.append(
            "references large module(s) "
            + ", ".join(large)
            + f" (over {module_line_threshold} lines) without concrete "
            "file/function references"
        )

    return issues
