#!/usr/bin/env python3
"""Hermes gateway health check - HTTP GET /health on port 18789."""

import urllib.request
import urllib.error
from .base import AbstractHandler, HandlerResult

DEFAULT_GATEWAY_URL = "http://localhost:18789/health"


class GatewayHandler(AbstractHandler):
    def __init__(self, url: str = DEFAULT_GATEWAY_URL):
        self.url = url

    @property
    def name(self) -> str:
        return "gateway"

    def check(self) -> HandlerResult:
        """Check gateway health via HTTP.

        Returns:
          healthy:  HTTP 200-299
          unhealthy: HTTP >= 400
          unknown:  timeout, connection refused, any error
        """
        try:
            req = urllib.request.Request(self.url, method="GET")
            req.add_header("User-Agent", "hscc-daemon/1.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
                if 200 <= code < 300:
                    return HandlerResult(status="healthy", detail={"code": code})
                else:
                    return HandlerResult(status="unhealthy", detail={"code": code})
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                return HandlerResult(status="unhealthy", detail={"code": e.code, "error": str(e)})
            return HandlerResult(status="unknown", detail={"code": e.code, "error": f"HTTP {e.code}"})
        except (urllib.error.URLError, OSError) as e:
            return HandlerResult(status="unknown", detail={"error": f"connection: {e}"})
        except Exception as e:
            return HandlerResult(status="unknown", detail={"error": f"unexpected: {e}"})
