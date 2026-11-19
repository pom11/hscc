"""``hscc notify`` — post one alert to the HSCC feed.

The verb is the human/orchestrator side of ``POST /v1/alerts`` (design:
``docs/design/2026-11-19-main-orchestrator-design.md`` §10). It does not write
the feed directly: the API is the one writer and the one place auth, dedupe and
rate limiting are enforced, so the CLI is a thin authenticated client — exactly
like a phone would be.

    hscc notify "<title>" "<body>" [--level info|action|critical] [--key K]
    hscc notify "<body>"                      # legacy one-argument form
    hscc notify test                          # send a test push to the apps
    hscc notify ... --json                    # the API's response, verbatim

WHY THE API AND NOT THE FILE. A second writer would need a second copy of the
dedupe/rate-limit rules, and the moment those drift the feed's promise ("the
same condition does not re-alert") is only true for one caller. The API is
already up on this host whenever the apps can see alerts; if it is down, saying
so with the remedy (``hscc api start``) is more honest than writing a file the
apps cannot read yet.

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

OUTPUT. Human output is one line: created, deduplicated, or rate-limited with
the wait. ``--json`` prints the API payload unchanged so a script can read
``alert.id`` or ``deduped``. Exit codes: 0 for a created OR suppressed alert
(suppression is the feed working as designed), 1 for usage/connection/API
errors. A 4xx from the server is a caller bug and exits 1 with the server's
own sentence, never a traceback.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from hscc_daemon.alerts import (
    DEFAULT_LEVEL,
    LEVELS,
    hscc_root,
    normalise_level,
)

__all__ = [
    "parse_notify_args", "resolve_api_urls", "read_token", "post_alert",
    "main", "USAGE",
]

DEFAULT_API_PORT = 8787
REQUEST_TIMEOUT_S = 10

USAGE = (
    'Usage: hscc notify "<title>" "<body>" [--level info|action|critical] '
    "[--key <dedupe-key>] [--json]   (or: hscc notify test [--json])"
)

#: Flags that take a value. Anything else flag-shaped is a usage error, never
#: a silently ignored token (a typo must not post the wrong alert).
_VALUE_FLAGS = ("--level", "--key", "--api")


class NotifyError(Exception):
    """A connection or API failure with an operator-readable sentence."""


def _split_flag(token):
    """``--level=action`` -> ``("--level", "action")``; bare -> ``(None, None)``."""
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        return name, value
    return None, None


def parse_notify_args(argv):
    """Parse the verb's argv (after ``notify``) into an options dict.

    Positionals are ``title`` and ``body``. One positional is the legacy
    "message only" form: it becomes the body with the title ``HSCC`` — the old
    verb sent a desktop notification, and an operator who types the old shape
    should not get a usage error, but the alert should still say where it came
    from.

    Returns ``(opts, error)``; ``error`` is a sentence suitable for stderr.
    """
    argv = list(argv or [])
    opts = {
        "title": None,
        "body": "",
        "level": DEFAULT_LEVEL,
        "key": None,
        "api": None,
        "json": False,
        "help": False,
        "test": False,
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
    if not positionals:
        return None, USAGE
    if len(positionals) > 2:
        return None, "too many arguments — quote the title and body separately"
    if len(positionals) == 1 and positionals[0].lower() == "test":
        # `hscc notify test` is the transport self-check, not an alert whose
        # body happens to be the word "test". It goes straight to the same
        # sender through POST /v1/devices/test, so it works with no feed row.
        if opts["key"]:
            return None, "--key does not apply to 'hscc notify test'"
        opts["test"] = True
        opts["title"], opts["body"] = "HSCC test notification", ""
        return opts, None
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


def _post_json(url, token, payload, path="/v1/alerts", label="alert"):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + path,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            message = (payload.get("error") or {}).get("message") or raw
        except ValueError:
            message = raw or exc.reason
        raise NotifyError(
            f"API refused the {label} ({exc.code}): {message}"
        ) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NotifyError(f"cannot reach the HSCC API at {url}: {exc}") from exc


def post_alert(opts, hscc_dir=None):
    """POST the parsed options to /v1/alerts. Returns ``(status, payload)``.

    Tries each candidate base in turn; a connection failure moves to the next,
    an HTTP error from a reachable server is final (retrying another address
    would replay a caller mistake).
    """
    token = read_token(hscc_dir)
    if not token:
        raise NotifyError(
            "no API token found under the hscc root — start the API once with "
            "'hscc api start' so ~/.hscc/api-token exists"
        )
    payload = {
        "title": opts["title"],
        "body": opts["body"],
        "level": opts["level"],
    }
    if opts.get("key"):
        payload["key"] = opts["key"]

    urls = resolve_api_urls(opts.get("api"), hscc_dir=hscc_dir)
    errors = []
    for url in urls:
        try:
            return _post_json(url, token, payload)
        except NotifyError as exc:
            errors.append(str(exc))
            if "API refused" in str(exc):
                raise
    detail = errors[-1] if errors else "no candidate address"
    raise NotifyError(
        f"{detail}. Is the API running? Start it with 'hscc api start'."
    )


def post_test(opts, hscc_dir=None):
    """POST /v1/devices/test — the sender's own one-push self-check."""
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
            # `confirm: true` is the API's mutation gate — the route 409s
            # without it, like every other POST the apps send. The human
            # already confirmed by typing `hscc notify test`, which is exactly
            # what the CLI's own --apply/--confirm guard does; the flag is how
            # that intent crosses the wire.
            return _post_json(url, token, {"confirm": True},
                              path="/v1/devices/test", label="test push")
        except NotifyError as exc:
            errors.append(str(exc))
            if "API refused" in str(exc):
                raise
    detail = errors[-1] if errors else "no candidate address"
    raise NotifyError(
        f"{detail}. Is the API running? Start it with 'hscc api start'."
    )


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


def _test_summary(payload):
    if payload.get("sent"):
        count = payload.get("sent_count", 0)
        return f"test push sent to {count} device{'s' if count != 1 else ''}"
    reason = payload.get("reason") or "not_sent"
    detail = payload.get("detail")
    return "test push not sent (" + reason + ")" + (
        f": {detail}" if detail else "")


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
            "'hscc notify test' sends one test push to every registered device\n"
            "(POST /v1/devices/test) without adding an alert to the feed.",
            file=out,
        )
        return 0
    try:
        if opts.get("test"):
            status, payload = post_test(opts)
        else:
            status, payload = post_alert(opts)
    except NotifyError as exc:
        print(f"hscc notify: {exc}", file=err)
        return 1
    if opts["json"]:
        print(json.dumps(payload, indent=2), file=out)
    elif opts.get("test"):
        print(_test_summary(payload), file=out)
    else:
        print(_summary(payload), file=out)
    if opts.get("test") and not payload.get("sent"):
        return 1
    return 0
