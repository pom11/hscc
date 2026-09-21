"""Tests for decompose's ask-seam contract.

``decompose`` used to send its prompt into a project's Telegram topic and read
the orchestrator's reply back via :mod:`flightdeck.core.telegram` (the
``_default_ask`` seam, and the ``_proposal_accept`` predicate it passed for the
"keep polling or settle" decision). That transport has been removed.

All test cases in this file exercised the removed telemetry feed and correlated
reply-prompt behavior (``_default_ask`` reading ``telegram_read`` snapshots,
watermark correlation, multi-part concatenation, timeouts). They tested removed
functionality and have been deleted. The ``_proposal_accept`` predicate is no
longer exposed by the module either, so there is no surviving behavior unique
to this seam to test here; the remaining decompose logic (``parse_proposal``,
``_extract_json``, quality gates) is covered in ``test_decompose.py``.
"""
