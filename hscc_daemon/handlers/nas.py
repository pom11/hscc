#!/usr/bin/env python3
"""NAS health check - SSH to NAS and check disk usage percentage."""

import subprocess
from .base import AbstractHandler, HandlerResult

DEFAULT_NAS_HOST = "nas.local"
DEFAULT_NAS_PATH = "/"
DISK_WARN_THRESHOLD = 90  # percent - alert if above this


class NASHandler(AbstractHandler):
    def __init__(
        self,
        host: str = DEFAULT_NAS_HOST,
        path: str = DEFAULT_NAS_PATH,
        key_path: str | None = None,
    ):
        self.host = host
        self.path = path
        self.key_path = key_path

    @property
    def name(self) -> str:
        return "nas"

    def _build_ssh_command(self) -> list[str]:
        """Build SSH command to check disk space on NAS."""
        cmd = [
            "ssh",
            "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=no",
        ]
        if self.key_path:
            cmd.extend(["-i", self.key_path])
        cmd.extend([
            self.host,
            f"df -h {self.path} | awk 'NR==2 {{print $5}}' | tr -d '%'",
        ])
        return cmd

    def check(self) -> HandlerResult:
        """Check NAS disk space via SSH.

        Returns:
          healthy: disk usage < 90%
          unhealthy: disk usage >= 90%
          unknown: SSH connection fails, timeout, parse error
        """
        try:
            cmd = self._build_ssh_command()
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if result.returncode != 0:
                return HandlerResult(
                    status="unknown",
                    detail={"error": f"ssh failed: {result.stderr.strip()}"}
                )

            usage_str = result.stdout.strip()
            try:
                usage_pct = int(usage_str)
            except ValueError:
                return HandlerResult(
                    status="unknown",
                    detail={"error": f"unparseable disk output: {usage_str!r}"}
                )

            if usage_pct >= DISK_WARN_THRESHOLD:
                return HandlerResult(
                    status="unhealthy",
                    detail={"disk_pct": usage_pct, "threshold": DISK_WARN_THRESHOLD}
                )
            else:
                return HandlerResult(status="healthy", detail={"disk_pct": usage_pct})
        except subprocess.TimeoutExpired:
            return HandlerResult(status="unknown", detail={"error": "SSH timed out"})
        except FileNotFoundError:
            return HandlerResult(status="unknown", detail={"error": "ssh command not found"})
        except Exception as e:
            return HandlerResult(status="unknown", detail={"error": str(e)})
