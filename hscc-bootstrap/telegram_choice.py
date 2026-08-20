"""Resolve the optional Telegram integration decision for bootstrap.

Telegram is an OPTIONAL out-of-band fallback, not a prerequisite. Bootstrap
asks ``Add Telegram integration? [y/N]`` (default No) rather than assuming it.

Centralized in this module so the resolution is pytest-testable without ever
running bootstrap.sh against the live ``~/.hermes`` — and so the decision is a
single, reusable source of truth instead of inline bash (testable, consistent,
and the prompt has one authoritative shape).

Resolution order (first match wins):
  1. Explicit override — ``HSCC_TELEGRAM`` env (accepts yes/y/true/1 or
     no/n/false/0). Forces the decision without prompting; for unattended runs.
  2. ``--yes`` (assume_yes) — documented default: **no** (declining must never
     break an install, and an unattended run must never hang waiting on input).
  3. Interactive prompt — ``Add Telegram integration? [y/N]``, default no.

Returns a JSON dict ``{"telegram": "yes"|"no", "source": ...}`` where source is
one of ``env`` / ``env-invalid`` / ``yes-default`` / ``prompt``.
"""

import json
import os
import sys

_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}

PROMPT_TEXT = "Add Telegram integration? [y/N]: "


def resolve_telegram(env=None, assume_yes=False, prompt=None):
    """Resolve the Telegram decision. Returns dict {telegram, source}.

    Args:
        env: mapping to read ``HSCC_TELEGRAM`` from (default: os.environ).
        assume_yes: True when bootstrap ran with ``--yes``.
        prompt: callable(prompt_text) -> answer, replacing ``input`` (tests
            inject a fake so they never block on real stdin).
    """
    if env is None:
        env = os.environ
    if prompt is None:
        prompt = input

    override = (env.get("HSCC_TELEGRAM") or "").strip().lower()
    if override:
        if override in _YES:
            return {"telegram": "yes", "source": "env"}
        if override in _NO:
            return {"telegram": "no", "source": "env"}
        # Unrecognized explicit value: default to No (safe — declining must
        # never break an install) and note that the value was ignored.
        return {"telegram": "no", "source": "env-invalid"}

    if assume_yes:
        return {"telegram": "no", "source": "yes-default"}

    answer = (prompt(PROMPT_TEXT) or "").strip().lower()
    if answer in _YES:
        return {"telegram": "yes", "source": "prompt"}
    return {"telegram": "no", "source": "prompt"}


if __name__ == "__main__":
    print(json.dumps(resolve_telegram()))
