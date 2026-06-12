#!/usr/bin/env python3
"""Container lifecycle check - docker inspect for orchestrator container."""

import subprocess
from .base import AbstractHandler, HandlerResult

DEFAULT_CONTAINER_NAME = "hscc-orchestrator"


class ContainerHandler(AbstractHandler):
    def __init__(self, container_id: str = DEFAULT_CONTAINER_NAME):
        self.container_id = container_id

    @property
    def name(self) -> str:
        return "container"

    def check(self) -> HandlerResult:
        """Check if the orchestrator container is running.

        Returns:
          healthy:  container exists and state == "running"
          unhealthy: container exists but state != "running"
          unknown:  docker command fails, container not found
        """
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", self.container_id],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                stderr = result.stderr.strip().lower()
                if "no such" in stderr or "not found" in stderr:
                    return HandlerResult(status="unhealthy", detail={"error": "container not found"})
                return HandlerResult(
                    status="unknown",
                    detail={"error": f"docker inspect failed: {result.stderr.strip()}"}
                )

            state = result.stdout.strip()
            if state == "running":
                uptime = self._get_uptime()
                return HandlerResult(status="healthy", detail={"state": state, "uptime": uptime})
            else:
                return HandlerResult(status="unhealthy", detail={"state": state})
        except subprocess.TimeoutExpired:
            return HandlerResult(status="unknown", detail={"error": "docker inspect timed out"})
        except FileNotFoundError:
            return HandlerResult(status="unknown", detail={"error": "docker not found"})
        except Exception as e:
            return HandlerResult(status="unknown", detail={"error": str(e)})

    def _get_uptime(self) -> str:
        """Get container uptime if available, else '-'. Returns human-readable string."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", self.container_id],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            pass
        return "-"
