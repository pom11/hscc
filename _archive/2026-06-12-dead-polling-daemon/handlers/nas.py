#!/usr/bin/env python3
"""NAS health check - check the local NAS mount, SSH fallback."""

import os
import shutil
import subprocess
import sys
from .base import AbstractHandler, HandlerResult

DEFAULT_NAS_HOST = "nas.local"
DEFAULT_NAS_PATH = "/"
DISK_WARN_THRESHOLD = 90  # percent - alert if above this
# Platform-conventional local NAS mount; env-overridable.
DEFAULT_NAS_MOUNT = os.environ.get(
    "HSCC_NAS_MOUNT",
    "/Volumes/NAS" if sys.platform == "darwin" else "/mnt/nas")


class NASHandler(AbstractHandler):
    def __init__(
        self,
        host: str = DEFAULT_NAS_HOST,
        path: str = DEFAULT_NAS_PATH,
        key_path: str | None = None,
        local_mount: str = DEFAULT_NAS_MOUNT,
    ):
        self.host = host
        self.path = path
        self.key_path = key_path
        self.mount_path = local_mount

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
        """Check NAS disk space via local mount /Volumes/NAS.

        Falls back to SSH if /Volumes/NAS is not mounted.

        Returns:
          healthy: disk usage < 90%
          unhealthy: disk usage >= 90%
          unknown: mount missing and SSH fails
        """
        try:
            # Try local mount first
            if os.path.exists(self.mount_path):
                try:
                    st = shutil.disk_usage(self.mount_path)
                    usage_pct = int(st.used * 100 / st.total) if st.total > 0 else 99
                    detail = {
                        "method": "local_mount",
                        "disk_total": f"{st.total // (1024**3)}G",
                        "disk_used": f"{st.used // (1024**3)}G",
                        "disk_avail": f"{st.free // (1024**3)}G",
                        "disk_pct": usage_pct,
                    }
                    status = "unhealthy" if usage_pct >= DISK_WARN_THRESHOLD else "healthy"
                    return HandlerResult(status=status, detail=detail)
                except OSError as e:
                    return HandlerResult(status="unknown", detail={"error": f"disk_usage failed: {e}"})
            
            # Fallback: SSH to NAS
            cmd = self._build_ssh_command()
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            if result.returncode != 0:
                return HandlerResult(
                    status="unknown",
                    detail={"error": f"ssh failed: {result.stderr.strip()}", "mount_missing": True}
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
