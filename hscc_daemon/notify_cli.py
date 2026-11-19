"""``hscc notify`` — the operator alert feed, from the CLI.

The verb is the human/orchestrator side of ``/v1/alerts`` (design:
``docs/design/2026-11-19-main-orchestrator-design.md``). It does not write the
feed directly: the API is the one writer and the one place auth, dedupe and
rate limiting are enforced, so the CLI is a thin authenticated client — exactly
like an app would be.

    hscc notify "<title>" "<body>" [--level info|action|critical] [--key K]
    hscc notify "<body>"                      # legacy one-argument form
    hscc notify list [--since <id>] [--limit <n>]
    hscc notify ack <id>
    hscc notify ... --json                    # the API's response

WHY THE API AND NOT THE FILE. A second writer would need a second copy of the
dedupe/rate-limit rules, and the moment those drift the feed's promise ("the
same condition does not re-alert") is only true for one caller. The API is
already up on this host whenever the feed is readable; if it is down, saying
so with the remedy (``hscc api start``) is more honest than writing a file the
other readers cannot see yet.

CONNECTION RESOLUTION. In order:

  1. ``--api <url>`` (explicit; wins outright),
  2. ``$HSCC_API_URL``,
  3. ``<hscc root>/api-state.json`` — the runtime snapshot of what the living
     server actually bound, loopback addresses first,
  4. ``<hscc root>/api.json`` — configured port, loopback assumed,
  5. ``http://127.0.0.1:8787`` — the server default.

The token is ``<hscc root>/api-token``, the same fail-closed credential every
other route uses. The hscc root honours ``$HSCC_HOME`` (tests and alternate
installs); production resolves ``~/.hscc``.

OUTPUT. Posts print one line: created, deduplicated, or rate-limited with the
wait. ``list`` prints newest first with id, age, level, key and title; ``ack``
confirms the id. ``--json`` prints the API payload. Exit codes: 0 for a created
OR suppressed alert (suppression is the feed working as designed) and for a
successful list/ack, 1 for usage/connection/API errors. A 4xx from the server
is a caller bug and exits 1 with the server's own sentence, never a traceback.
"""

from __future__ import annotations

import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from hscc_daemon.alerts import (
    DEFAULT_LEVEL,
    LEVELS,
    hscc_root,
    normalise_level,
)

__all__ = [
    "parse_notify_args", "resolve_api_urls", "read_token", "post_alert",
    "list_alerts", "ack_alert", "main", "USAGE",
]

DEFAULT_API_PORT = 8787
REQUEST_TIMEOUT_S = 10

USAGE = (
    'Usage: hscc notify "<title>" "<body>" [--level info|action|critical] '
    "[--key <dedupe-key>] [--json]\n"
    "       hscc notify list [--since <id>] [--limit <n>] [--json]\n"
    "       hscc notify ack <id> [--json]"
)

#: Flags that take a value. Anything else flag-shaped is a usage error, never
#: a silently ignored token (a typo must not post the wrong alert).
_VALUE_FLAGS = ("--level", "--key", "--api", "--since", "--limit")


class NotifyError(Exception):
    """A connection or API failure with an operator-readable sentence."""


def _split_flag(token):
    """``--level=action`` -> ``("--level", "action")``; bare -> ``(None, None)``."""
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value
    return None, None


def _positive_int(value, name, allow_zero=False):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer")
    if number < 0 or (number == 0 and not allow_zero):
        comparator = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {comparator} integer")
    return number


def parse_notify_args(argv):
    """Parse the verb's argv (after ``notify``) into an options dict.

    Post forms are ``title`` and ``body``. One positional is the legacy
    "message only" form: it becomes the body with the title ``HSCC`` — the old
    verb sent a desktop notification, and an operator who types the old shape
    should not get a usage error, but the alert should still say where it came
    from. ``list`` and ``ack`` are subcommands and therefore cannot be a bare
    one-argument body.

    Returns ``(opts, error)``; ``error`` is a sentence suitable for stderr.
    """
    argv = list(argv or [])
    opts = {
        "command": "post",
        "title": None,
        "body": "",
        "level": DEFAULT_LEVEL,
        "key": None,
        "api": None,
        "json": False,
        "help": False,
        "since": None,
        "limit": None,
        "id": None,
    }
    positionals = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            opts["help"] = True
            i += 1
            continue
        if tok == "--json":
            opts["json"] = True
            i += 1
            continue
        name, inline = _split_flag(tok)
        if name is not None:
            if name not in _VALUE_FLAGS:
                return None, f"unknown flag '{name}' (see: hscc notify --help)"
            opts[name.lstrip("-")] = inline
            i += 1
            continue
        if tok in _VALUE_FLAGS:
            if i + 1 >= len(argv):
                return None, f"{tok} needs a value"
            opts[tok.lstrip("-")] = argv[i + 1]
            i += 2
            continue
        if tok.startswith("-") and tok != "-":
            return None, f"unknown flag '{tok}' (see: hscc notify --help)"
        positionals.append(tok)
        i += 1

    if opts["help"]:
        return opts, None

    command = positionals[0] if positionals else None
    if command == "list":
        opts["command"] = "list"
        if len(positionals) > 1:
            return None, "hscc notify list takes flags only (see --help)"
        if opts["key"] is not None:
            return None, "--key does not apply to 'hscc notify list'"
        if opts["level"] != DEFAULT_LEVEL:
            return None, "--level does not apply to 'hscc notify list'"
        try:
            if opts["since"] is not None:
                opts["since"] = _positive_int(opts["since"], "since",
                                              allow_zero=True)
            if opts["limit"] is not None:
                opts["limit"] = _positive_int(opts["limit"], "limit")
        except ValueError as exc:
            return None, str(exc)
        return opts, None

    if command == "ack":
        opts["command"] = "ack"
        if len(positionals) != 2:
            return None, "usage: hscc notify ack <id>"
        if opts["key"] is not None:
            return None, "--key does not apply to 'hscc notify ack'"
        if opts["level"] != DEFAULT_LEVEL:
            return None, "--level does not apply to 'hscc notify ack'"
        try:
            opts["id"] = _positive_int(positionals[1], "alert id")
        except ValueError as exc:
            return None, str(exc)
        return opts, None

    if opts["since"] is not None or opts["limit"] is not None:
        return None, "--since/--limit apply only to 'hscc notify list'"
    if not positionals:
        return None, USAGE
    if len(positionals) > 2:
        return None, "too many arguments — quote the title and body separately"
    if len(positionals) == 1:
        opts["title"], opts["body"] = "HSCC", positionals[0]
    else:
        opts["title"], opts["body"] = positionals

    if not opts["title"].strip():
        return None, "title must not be empty"
    try:
        opts["level"] = normalise_level(opts["level"])
    except ValueError as exc:
        return None, f"{exc} (got {opts['level']!r})"
    if opts["key"] is not None:
        opts["key"] = opts["key"].strip()
        if not opts["key"]:
            return None, "key must not be empty when given"
    return opts, None


def _loopback_first(hosts):
    """Loopback is the cheapest/most likely local route; keep the rest after."""
    def rank(host):
        return 0 if host.startswith("127.") or host in ("localhost", "::1") else 1
    return sorted(hosts, key=rank)


def _url_for(host, port):
    host = str(host).strip()
    if not host or host in ("0.0.0.0", "::"):
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return f"http://{host}:{port}"


def resolve_api_urls(explicit=None, hscc_dir=None):
    """Candidate API bases, most-specific first (see the module docstring).

    A ``--api`` flag or ``$HSCC_API_URL`` is a complete answer, not a hint:
    returning other candidates after it would silently contact a different
    server than the one the caller named (and would make a hermetic test with
    an unreachable explicit address wander onto the operator's live API).
    """
    urls = []

    def add(url):
        if not url:
            return
        url = str(url).strip().rstrip("/")
        if url and url not in urls:
            urls.append(url)

    add(explicit)
    if urls:
        return urls
    add(os.environ.get("HSCC_API_URL"))
    if urls:
        return urls

    root = hscc_root(hscc_dir)
    try:
        state = json.loads((root / "api-state.json").read_text())
    except (OSError, ValueError):
        state = {}
    if isinstance(state, dict):
        port = state.get("port", DEFAULT_API_PORT)
        hosts = state.get("hosts") or [state.get("host") or "127.0.0.1"]
        for host in _loopback_first([h for h in hosts if isinstance(h, str)]):
            add(_url_for(host, port))

    try:
        config = json.loads((root / "api.json").read_text())
    except (OSError, ValueError):
        config = {}
    if isinstance(config, dict):
        # A bind may be the literal 'loopback'/'tailscale'; only the concrete
        # default is a usable URL here, and api-state (step 3) covers the rest.
        add(_url_for("127.0.0.1", config.get("port", DEFAULT_API_PORT)))

    add(f"http://127.0.0.1:{DEFAULT_API_PORT}")
    return urls


def read_token(hscc_dir=None):
    """The API bearer token, or ``None`` when it has not been created yet."""
    try:
        token = (hscc_root(hscc_dir) / "api-token").read_text().strip()
    except OSError:
        return None
    return token or None


def _request_json(url, token, method, path, payload=None, label="alert"):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": "Bearer " + token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url + path, data=data, method=method, headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
            message = (body.get("error") or {}).get("message") or raw
        except ValueError:
            message = raw or exc.reason
        raise NotifyError(
            f"API refused the {label} ({exc.code}): {message}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NotifyError(f"cannot reach the HSCC API at {url}: {exc}") from exc


def _call(opts, hscc_dir, path, method="POST", payload=None, label="alert"):
    """Call one route, trying each candidate base until one connects.

    An HTTP error from a reachable server is final (retrying another address
    would replay a caller mistake).
    """
    token = read_token(hscc_dir)
    if not token:
        raise NotifyError(
            "no API token found under the hscc root — start the API once with "
            "'hscc api start' so ~/.hscc/api-token exists"
        )
    urls = resolve_api_urls(opts.get("api"), hscc_dir=hscc_dir)
    errors = []
    for url in urls:
        try:
            return _request_json(url, token, method, path, payload, label)
        except NotifyError as exc:
            errors.append(str(exc))
            if "API refused" in str(exc):
                raise
    detail = errors[-1] if errors else "no candidate address"
    raise NotifyError(
        f"{detail}. Is the API running? Start it with 'hscc api start'."
    )


def post_alert(opts, hscc_dir=None):
    """POST the parsed options to /v1/alerts. Returns ``(status, payload)``."""
    payload = {
        "title": opts["title"],
        "body": opts["body"],
        "level": opts["level"],
    }
    if opts.get("key"):
        payload["key"] = opts["key"]
    return _call(opts, hscc_dir, "/v1/alerts", payload=payload)


def list_alerts(opts, hscc_dir=None):
    """GET the feed (newest first in the CLI's presentation)."""
    query = {}
    if opts.get("since") is not None:
        query["since"] = str(opts["since"])
    if opts.get("limit") is not None:
        query["limit"] = str(opts["limit"])
    path = "/v1/alerts"
    if query:
        path += "?" + urllib.parse.urlencode(query)
    status, payload = _call(
        opts, hscc_dir, path, method="GET", label="alert feed")
    payload["alerts"] = list(reversed(payload.get("alerts") or []))
    return status, payload


def ack_alert(opts, hscc_dir=None):
    """POST /v1/alerts/<id>/ack — mark one alert seen."""
    return _call(
        opts, hscc_dir, f"/v1/alerts/{opts['id']}/ack",
        payload={}, label="alert ack")


def _summary(payload):
    """One plain line for a human (no Rich markup; safe on a pipe)."""
    alert = payload.get("alert") or {}
    if payload.get("created"):
        ident = alert.get("id")
        level = alert.get("level", DEFAULT_LEVEL)
        key = alert.get("key")
        return f"alert {ident} posted ({level})" + (f" key={key}" if key else "")
    if payload.get("deduped"):
        return f"alert {alert.get('id')} unchanged (deduped)"
    retry = payload.get("retry_after_s")
    wait = f", retry in {float(retry):.0f}s" if retry is not None else ""
    return f"alert suppressed ({payload.get('reason', 'rate_limited')}{wait})"


def _age_label(ts, now=None):
    """``2m`` / ``3h`` / ``4d`` from an alert's UTC ``...Z`` timestamp."""
    try:
        stamp = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        seconds = max(0, int((now - stamp).total_seconds()))
    except (TypeError, ValueError):
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _list_summary(payload):
    """Newest-first human lines: id, age, level, key, title."""
    alerts = payload.get("alerts") or []
    if not alerts:
        return "no alerts"
    lines = []
    for alert in alerts:
        age = _age_label(alert.get("ts"))
        level = alert.get("level", DEFAULT_LEVEL)
        key = f"  key={alert['key']}" if alert.get("key") else ""
        acked = "  (acked)" if alert.get("acked") else ""
        title = alert.get("title") or ""
        lines.append(f"#{alert.get('id')}  {age:>4}  {level}{key}{acked}  {title}")
    return "\n".join(lines)


def _ack_summary(payload):
    alert = payload.get("alert") or {}
    return f"alert {alert.get('id')} acknowledged"


def main(argv, out=None, err=None):
    """The verb. Returns the process exit code (0 success incl. suppression)."""
    import sys

    out = out or sys.stdout
    err = err or sys.stderr
    opts, error = parse_notify_args(argv)
    if error:
        print(f"hscc notify: {error}", file=err)
        return 1
    if opts["help"]:
        print(USAGE, file=out)
        print(
            "\nPosts one alert to the HSCC alert feed (POST /v1/alerts). A keyed\n"
            "alert is deduplicated until its content changes or it is acknowledged;\n"
            "per-key and global rate limits apply. Flags: --level, --key, --json,\n"
            "--api <url>.\n"
            "'hscc notify list' reads the feed newest first (--since <id>,\n"
            "--limit <n>); 'hscc notify ack <id>' marks one alert seen.",
            file=out,
        )
        return 0
    try:
        if opts["command"] == "list":
            status, payload = list_alerts(opts)
        elif opts["command"] == "ack":
            status, payload = ack_alert(opts)
        else:
            status, payload = post_alert(opts)
    except NotifyError as exc:
        print(f"hscc notify: {exc}", file=err)
        return 1
    if opts["json"]:
        print(json.dumps(payload, indent=2), file=out)
    elif opts["command"] == "list":
        print(_list_summary(payload), file=out)
    elif opts["command"] == "ack":
        print(_ack_summary(payload), file=out)
    else:
        print(_summary(payload), file=out)
    return 0
