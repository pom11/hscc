#!/usr/bin/env python3
"""vLLM orchestrator health check - HTTP GET /health on port 8000."""

import json
import urllib.request
import urllib.error
from .base import AbstractHandler, HandlerResult

DEFAULT_VLLM_URL = "http://localhost:8000/health"


class VLLMHandler(AbstractHandler):
    def __init__(self, url: str = DEFAULT_VLLM_URL):
        self.url = url

    @property
    def name(self) -> str:
        return "vllm"

    def check(self) -> HandlerResult:
        """Check vLLM health via HTTP.

        Returns:
          healthy:  HTTP 200-299 with valid JSON
          unhealthy: HTTP 5xx or JSON without 'status' key
          unknown:  timeout, connection refused, parse error
        """
        try:
            req = urllib.request.Request(self.url, method="GET")
            req.add_header("User-Agent", "hscc-daemon/1.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                code = resp.status
                body = resp.read().decode()

            if 200 <= code < 300:
                try:
                    data = json.loads(body)
                    if isinstance(data, dict) and "status" in data:
                        return HandlerResult(
                            status="healthy",
                            detail={"code": code, "response": data}
                        )
                except (json.JSONDecodeError, TypeError):
                    pass  # Not JSON but 200 - still healthy
                return HandlerResult(status="healthy", detail={"code": code, "body_len": len(body)})
            else:
                return HandlerResult(
                    status="unhealthy",
                    detail={"code": code, "error": f"HTTP {code}"}
                )
        except urllib.error.HTTPError as e:
            if e.code >= 500:
                return HandlerResult(status="unhealthy", detail={"code": e.code, "error": str(e)})
            else:
                return HandlerResult(status="unknown", detail={"code": e.code, "error": f"HTTP {e.code}"})
        except (urllib.error.URLError, OSError) as e:
            return HandlerResult(status="unknown", detail={"error": f"connection: {e}"})
        except Exception as e:
            return HandlerResult(status="unknown", detail={"error": f"unexpected: {e}"})
