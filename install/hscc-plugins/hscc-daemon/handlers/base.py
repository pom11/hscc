#!/usr/bin/env python3
"""Base class for all daemon health check handlers."""

import abc
import json
import signal
import time
from typing import Any


HANDLER_TIMEOUT_SEC = 10


class HandlerResult:
    """Structured result from a single handler check."""

    __slots__ = ("status", "detail", "elapsed_ms")

    def __init__(
        self, status: str, detail: dict | None = None, elapsed_ms: float | None = None
    ):
        if status not in ("healthy", "unhealthy", "unknown"):
            raise ValueError(f"Invalid status: {status!r}. Must be healthy/unhealthy/unknown.")
        self.status = status
        self.detail = detail or {}
        self.elapsed_ms = elapsed_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


class AbstractHandler(metaclass=abc.ABCMeta):
    """Base class for all health check handlers.

    Subclasses implement `check()` which returns a HandlerResult.
    The `run()` wrapper handles timeout and exception catching:
    - Timeout -> returns HandlerResult(status="unknown", detail={"error": "timeout"})
    - Exception -> returns HandlerResult(status="unknown", detail={"error": str(e)})
    """

    @abc.abstractmethod
    def check(self) -> HandlerResult:
        """Perform the health check. Must not throw - return unknown on failure."""
        ...

    def run(self) -> HandlerResult:
        """Run check with timeout and exception safety."""
        start = time.monotonic()
        try:
            result = self._check_with_timeout()
            result.elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            return result
        except Exception as e:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return HandlerResult(status="unknown", detail={"error": str(e), "elapsed_ms": elapsed})

    def _check_with_timeout(self) -> HandlerResult:
        """Implement timeout via signal (Unix only)."""

        class TimeoutError(Exception):
            pass

        def handler(signum, frame):
            raise TimeoutError(
                f"Handler {self.name} timed out after {HANDLER_TIMEOUT_SEC}s"
            )

        old = signal.signal(signal.SIGALRM, handler)
        signal.alarm(int(HANDLER_TIMEOUT_SEC))
        try:
            result = self.check()
            signal.alarm(0)  # cancel alarm
            return result
        except TimeoutError:
            signal.alarm(0)
            return HandlerResult(status="unknown", detail={"error": f"timeout after {HANDLER_TIMEOUT_SEC}s"})
        finally:
            signal.signal(signal.SIGALRM, old)

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Handler name used in reports (e.g., 'vllm', 'gateway')."""
        ...
