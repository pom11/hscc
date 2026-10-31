"""Stdlib-only HTTP for the sparkrun proxy client (urllib; no third-party deps).

The pack deliberately ships ZERO runtime dependencies of its own: ComfyUI
already carries Pillow/numpy/torch, and the HSCC repo suite runs in a
torch-less venv where even `requests` is not guaranteed. Everything here is
`urllib.request` + `json` + `base64`, importable anywhere with a stdlib.

Error contract: every non-2xx response raises :class:`SparkrunProxyError`
carrying the status code and a truncated body snippet — the node surfaces it
to the ComfyUI UI, so "proxy said 400: <model> does not support image
tasks" beats a bare JSONDecodeError. Timeouts raise the same type (status
None) so callers never have to catch socket noise.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

#: Max characters of an error body surfaced to the UI. Long LiteLLM trace
#: bodies are noise in a node error popup.
MAX_ERROR_BODY_CHARS = 600

DEFAULT_TIMEOUT_S = 120.0
#: Discovery (GET /v1/models) is a UI-blocking call while the node graph
#: renders — keep it short so a dead proxy degrades the dropdown, not ComfyUI.
DISCOVERY_TIMEOUT_S = 5.0


class SparkrunProxyError(RuntimeError):
    """The proxy answered wrongly, or not at all.

    ``status`` is the HTTP code, or ``None`` for transport failures (DNS,
    refused connection, timeout). ``body`` is the truncated response body.
    """

    def __init__(self, message, *, status=None, body=""):
        super().__init__(message)
        self.status = status
        self.body = body


def _snip(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    text = text.strip()
    if len(text) > MAX_ERROR_BODY_CHARS:
        text = text[:MAX_ERROR_BODY_CHARS] + "…"
    return text


def join_url(base_url: str, path: str, *, absolute: str = "") -> str:
    """Join ``path`` under ``base_url``; an ``absolute`` URL from the proxy wins.

    ``base_url`` may or may not carry a trailing slash and may or may not end
    in ``/v1`` — both shapes are handled, so ``http://h:4000`` and
    ``http://h:4000/v1`` both reach ``.../v1/models``. ``absolute`` (a URL the
    proxy itself handed back, e.g. a video ``output.url``) is returned as-is
    when it is a real http(s) URL.
    """
    if absolute and (absolute.startswith("http://") or absolute.startswith("https://")):
        return absolute
    base = (base_url or "").rstrip("/")
    if path.startswith("/"):
        path = path.lstrip("/")
        # A leading slash means "relative to the /v1 root of the API", and the
        # proxy's documented surface lives under /v1 regardless of whether the
        # base_url spelled it out.
        if not base.endswith("/v1"):
            base = base + "/v1"
    return f"{base}/{path}"


def request(
    method: str,
    url: str,
    *,
    payload=None,
    body: bytes = b"",
    headers=None,
    timeout: float = DEFAULT_TIMEOUT_S,
):
    """One HTTP request → ``(status, headers_dict, body_bytes)``.

    ``payload`` (JSON-serialisable) is encoded and Content-Type set; pass
    ``body`` for pre-encoded bytes (multipart). Raises
    :class:`SparkrunProxyError` on any non-2xx (status/body carried) and on
    transport failure (status ``None``).
    """
    hdrs = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif body:
        data = body
    for k, v in (headers or {}).items():
        hdrs[k] = v

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return int(status), dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        err_body = _snip(e.read() or b"")
        raise SparkrunProxyError(
            f"{method} {url} -> HTTP {e.code}: {err_body}",
            status=e.code,
            body=err_body,
        ) from None
    except urllib.error.URLError as e:
        raise SparkrunProxyError(
            f"{method} {url} -> proxy unreachable: {e.reason}", status=None
        ) from None
    except (TimeoutError, socket.timeout) as e:
        raise SparkrunProxyError(
            f"{method} {url} -> timed out after {timeout}s", status=None
        ) from None


def request_json(method: str, url: str, *, payload=None, body: bytes = b"",
                 headers=None, timeout: float = DEFAULT_TIMEOUT_S):
    """``request`` + strict JSON decode. Non-JSON 2xx is a proxy fault too."""
    status, _, raw = request(method, url, payload=payload, body=body,
                             headers=headers, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SparkrunProxyError(
            f"{method} {url} -> HTTP {status} but body is not JSON: {_snip(raw)}",
            status=status,
            body=_snip(raw),
        ) from e


def get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT_S):
    return request_json("GET", url, timeout=timeout)


def post_json(url: str, payload, *, timeout: float = DEFAULT_TIMEOUT_S):
    return request_json("POST", url, payload=payload, timeout=timeout)


def post_multipart(url: str, fields: dict, files: dict, *, timeout: float = DEFAULT_TIMEOUT_S):
    """POST ``multipart/form-data``; ``files`` maps field→(filename, bytes, mime).

    ``fields`` are plain form fields (model, prompt, seed…). Returns the decoded
    JSON body. The proxy forwards these parts straight to OpenAI-compatible
    ``/v1/images/edits`` backends.
    """
    boundary = "----sparkrun" + os.urandom(12).hex()
    chunks = []
    for name, value in (fields or {}).items():
        if value is None:
            continue
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
        )
    for name, (filename, blob, mime) in (files or {}).items():
        mime = mime or mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
        chunks.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="{name}"; filename="{filename}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        )
        chunks.append(blob + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    return request_json(
        "POST", url, body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=timeout,
    )


def download(url: str, *, timeout: float = DEFAULT_TIMEOUT_S, max_bytes: int = 512 * 1024 * 1024) -> bytes:
    """GET a binary asset (video content). Capped so a broken URL cannot OOM ComfyUI."""
    status, headers, raw = request("GET", url, timeout=timeout)
    declared = headers.get("Content-Length") or headers.get("content-length")
    if declared and str(declared).isdigit() and int(declared) > max_bytes:
        raise SparkrunProxyError(
            f"{url}: Content-Length {declared} exceeds cap {max_bytes} bytes", status=status
        )
    if len(raw) > max_bytes:
        raise SparkrunProxyError(
            f"{url}: body {len(raw)} bytes exceeds cap {max_bytes}", status=status
        )
    return raw


def host_root(base_url: str) -> str:
    """``scheme://host:port`` of a base_url (no path, no trailing slash)."""
    parsed = urllib.parse.urlsplit(base_url or "")
    if not parsed.scheme or not parsed.netloc:
        raise SparkrunProxyError(f"not an absolute http(s) URL: {base_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def resolve_ref(base_url: str, ref: str) -> str:
    """Resolve a URL the proxy handed back (job ``output.url``, image ``url``).

    Three shapes occur in the wild and all three must work:

    * an absolute http(s) URL — used as-is (a signed CDN url must not be
      rewritten, or its signature breaks);
    * a root-relative path (``/v1/videos/abc/content``) — resolved against the
      proxy's host root, NOT under base_url's path (it already spells /v1);
    * a bare relative name (``abc.mp4``) — joined under base_url.
    """
    ref = (ref or "").strip()
    if not ref:
        raise SparkrunProxyError("empty URL reference")
    if ref.startswith("http://") or ref.startswith("https://"):
        return ref
    if ref.startswith("/"):
        return host_root(base_url) + ref
    return join_url(base_url, ref)


def b64_to_bytes(text: str) -> bytes:
    """Decode a b64 payload, tolerating whitespace/newlines LiteLLM may embed."""
    try:
        return base64.b64decode("".join((text or "").split()), validate=True)
    except Exception as e:  # binascii.Error subclasses ValueError
        raise SparkrunProxyError(f"proxy returned undecodable b64 payload: {e}") from e
