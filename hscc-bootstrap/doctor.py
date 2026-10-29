"""HSCC preflight doctor (D13).

Checks every prerequisite a fresh HSCC install needs and explains failures in
plain language with a one-line fix. Used by bootstrap.sh (Stage 1) and runnable
standalone: `python3 doctor.py`.

Each check returns a Check(name, ok, detail, fix, fatal). `run_doctor()` returns
a summary dict; `main()` prints a ✓/✗ checklist and exits non-zero if any FATAL
check failed (so bootstrap can hard-stop).

## ONE-TIME migration to the alias scheme (Card D, v1.5.1)
Existing live configs (pre-v1.5.1) hold CONCRETE model ids — that was the
original bug. After the serving layer advertises stable aliases
(`orchestrator-model` / `worker-model`) and writers emit aliases, the fleet
converges on aliases. `doctor --fix` performs a ONE-TIME conversion of existing
live configs: any concrete model id whose paired `base_url` is UNAMBIGUOUSLY
the orchestrator or worker proxy endpoint is replaced with the matching alias.
Remote/cloud endpoints are never guessed at — operator-chosen endpoints
survive untouched.

SAFETY GATE: before any entry is converted, the endpoint is PROBED (reusing the
`/models` machinery of the "models served" check — `_models_url` + Bearer auth)
and confirmed to actually serve the target alias. If the alias is absent from
the served list, or the endpoint is unreachable, that entry is left UNCONVERTED
and reported loudly (never convert blind — an alias the serving layer does not
resolve would fail every call with 404). Once converged, the serving layer
resolves the alias back to the concrete served id, so converting a confirmed
alias is safe.
"""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import List, Optional

import _theme
import backup_util
import doctor_node_recovery
import doctor_role_soul


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    fatal: bool = True   # a failed fatal check should hard-stop bootstrap


def _python_ok() -> Check:
    v = sys.version_info
    ok = v >= (3, 9)
    return Check("python", ok,
                 detail=f"{v.major}.{v.minor}.{v.micro}",
                 fix="Install Python 3.9+ (the Hermes venv python is preferred).",
                 fatal=True) if not ok else Check(
        "python", True, detail=f"{v.major}.{v.minor}.{v.micro}")


def _pyyaml_ok() -> Check:
    try:
        import yaml  # noqa: F401
        return Check("pyyaml", True, detail="importable")
    except ImportError:
        return Check("pyyaml", False,
                     detail="PyYAML not importable",
                     fix="pip install pyyaml (or run with the Hermes venv python).",
                     fatal=True)


def _sparkrun_ok() -> Check:
    path = shutil.which("sparkrun")
    if not path:
        return Check("sparkrun", False, detail="not on PATH",
                     fix="Install sparkrun and ensure it's on PATH.", fatal=True)
    return Check("sparkrun", True, detail=path)


def _sparkrun_cluster_ok(_runner=None) -> Check:
    runner = _runner or _run_cluster_list
    raw = runner()
    if not raw:
        return Check("sparkrun cluster", False,
                     detail="no cluster configured / sparkrun not reachable",
                     fix="sparkrun cluster add <name> <host1> <host2> ...",
                     fatal=True)
    return Check("sparkrun cluster", True, detail="configured")


def hermes_root_from_home(hermes_home) -> Optional[str]:
    """Hermes ROOT for a home that may be PROFILE-scoped; None if no install.

    Hermes sets ``HERMES_HOME`` to the *profile* dir
    (``<root>/profiles/<name>``) whenever it runs under a profile — that is its
    normal run mode, not a misconfiguration (upstream
    ``hermes_constants.get_hermes_home`` resolves config/SOUL/skills relative to
    that profile dir, while ``get_default_hermes_root`` strips the
    ``profiles/<name>`` tail for root-level ops). So a profile-scoped home is
    legitimate and must be tolerated.

    Returns the root when ``<candidate>/hermes-agent`` exists, trying in order:
    the home itself, then the grandparent of a ``<root>/profiles/<name>`` path,
    then the platform default ``~/.hermes``. Returns None when none of them
    carries an install — i.e. Hermes really is absent. Same
    "parent dir is named ``profiles``" rule ``hscc-roles/rolelib.py`` already
    uses, so the fleet resolves homes consistently.
    """
    candidates: List[str] = []

    def _push(path):
        if not path:
            return
        path = os.path.expanduser(str(path))
        if path not in candidates:
            candidates.append(path)

    _push(hermes_home)
    home = os.path.expanduser(str(hermes_home)) if hermes_home else ""
    if home:
        parent = os.path.dirname(home)
        if parent and os.path.basename(parent) == "profiles":
            _push(os.path.dirname(parent))
    _push(os.path.join(os.path.expanduser("~"), ".hermes"))

    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "hermes-agent")):
            return cand
    return None


def hermes_install_dir(hermes_home=None) -> str:
    """Path of the ``hermes-agent`` checkout for an optionally profile-scoped home.

    Falls back to the first candidate path when Hermes is absent, so callers can
    report an honest "missing" detail naming where it was looked for.
    """
    root = hermes_root_from_home(hermes_home)
    if root:
        return os.path.join(root, "hermes-agent")
    home = hermes_home or os.environ.get("HERMES_HOME") \
        or os.path.join(os.path.expanduser("~"), ".hermes")
    return os.path.join(os.path.expanduser(str(home)), "hermes-agent")


def _hermes_ok(hermes_home: str) -> Check:
    """Hermes install check — fatal, and honest in BOTH directions.

    A profile-scoped ``HERMES_HOME`` must NOT be reported as a missing install
    (the checkout lives at the root, so the old code false-failed every worker
    run, which sets HERMES_HOME to a profile dir), but a machine with no
    ``hermes-agent`` anywhere still has to fail fatally.
    """
    root = hermes_root_from_home(hermes_home)
    if root is None:
        missing = os.path.join(os.path.expanduser(str(hermes_home)),
                               "hermes-agent") if hermes_home \
            else "~/.hermes/hermes-agent"
        return Check("hermes", False, detail=f"{missing} missing",
                     fix="Install Hermes (expected at ~/.hermes/hermes-agent).",
                     fatal=True)
    agent = os.path.join(root, "hermes-agent")
    detail = agent if root == os.path.expanduser(str(hermes_home)) \
        else f"{agent} (hermes root resolved from profile home {hermes_home})"
    return Check("hermes", True, detail=detail)


def _disk_ok(path: str, min_gb: float = 5.0) -> Check:
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        ok = free_gb >= min_gb
        return Check("disk space", ok, detail=f"{free_gb:.1f} GB free",
                     fix=f"Free up disk — need ≥{min_gb} GB on {path}.",
                     fatal=False)
    except OSError as e:
        return Check("disk space", False, detail=str(e), fatal=False)


def _nas_ok(_runner=None, _cluster_raw=None) -> Check:
    """NAS reachability (non-fatal — NAS is optional but recommended for weight
    staging). Uses the sparkrun cluster's cache_dir + a ping to the NAS host if
    discoverable.

    ``_runner`` supplies the NAS identifier directly (legacy injection point).
    ``_cluster_raw`` supplies the raw `sparkrun cluster list` JSON instead, so a
    caller that already injects the cluster runner can stop that subprocess
    reaching the live fleet config — see ``run_doctor``.
    """
    nas = _runner() if _runner else _detect_nas(_cluster_raw)
    if not nas:
        return Check("nas", True, detail="none configured (optional)", fatal=False)
    # nas may be a mount path (cache_dir) and/or an ip; just report it — a deep
    # mount probe needs ssh to a worker, which the live heal tools (nas_diagnose)
    # do. Doctor only flags presence here.
    return Check("nas", True, detail=str(nas), fatal=False)


def _detect_nas(_cluster_raw=None):
    """Best-effort NAS identifier from the sparkrun cluster (cache_dir).

    ``_cluster_raw`` is an injectable stand-in for `sparkrun cluster list`
    (returns the raw JSON string) so tests do not shell out to the live fleet.
    """
    raw = (_cluster_raw or _run_cluster_list)()
    if not raw:
        return None
    try:
        import json
        clusters = json.loads(raw)
        chosen = next((c for c in clusters if c.get("default")), clusters[0])
        return (chosen.get("cache_dir") or "").strip() or None
    except (ValueError, IndexError, KeyError):
        return None


def _gateway_running() -> Check:
    try:
        r = subprocess.run(["pgrep", "-f", "hermes_cli.main gateway"],
                           capture_output=True, timeout=5)
        running = r.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        running = False
    # informational only — bootstrap can run before the gateway is up
    return Check("gateway", running,
                 detail="running" if running else "not running",
                 fix="Start it after bootstrap.", fatal=False)


def _models_url(base_url: str) -> str:
    """Normalize an endpoint base_url to an OpenAI-style ``/models`` probe URL.

    The version path is PRESERVED: an OpenAI-compatible server serves the
    model list at ``{base_url}/models`` (e.g. ``http://host:port/v1/models``),
    NOT at a version-stripped root (``http://host:port/models``). So we append
    ``/models`` to the base_url as configured, never strip ``/v1``.

    ``http://host:port/v1``   -> ``http://host:port/v1/models``
    ``http://host:port/v1/``  -> ``http://host:port/v1/models``
    ``http://host:port``      -> ``http://host:port/models``
    Returns "" for missing/blank input.
    """
    url = (base_url or "").strip()
    if not url:
        return ""
    url = url.rstrip("/")
    # If the configured base_url carries a /v1 version prefix, keep (exactly)
    # one of them and append /models to it — do NOT strip the version path.
    if "/v1" in url:
        head = url.split("/v1", 1)[0].rstrip("/")
        return head + "/v1/models"
    return url + "/models"


# ── BEGIN mirrored block: lan_curl_fallback (t_391b084e) ──────────────────
# macOS binds Privacy > Local Network to the *signing identity* of the
# executable, so an adhoc-signed interpreter — a Homebrew keg's Python.app, a
# python.org Framework build — is denied on EVERY private-range connect, no
# matter how often the Python entries are re-toggled. urllib then reports
# `OSError(65, 'No route to host')` (EHOSTUNREACH) for a host that is up and
# answering, while Apple-signed /usr/bin/curl — which keeps its own TCC grant
# — reaches the same URL fine. That is what made `hscc cluster monitor --live`
# render every backend `down` on a healthy fleet (host stats survived only
# because they ride ssh). Do NOT try to fix this with TCC/system changes: it is
# an OS identity rule, not our bug. The workaround is to retry exactly that one
# failure through curl.
#
# THREE PLUGINS CARRY THIS BLOCK VERBATIM (hscc_daemon/cluster_live.py,
# hscc-bootstrap/doctor.py, hscc-cluster/cluster_template.py). It is NOT a
# cross-plugin import on purpose: each plugin deploys standalone into
# ~/.hermes/plugins and the hyphenated dirs are not importable package names,
# so none of them can import another. The block is pinned byte-identical by
# hscc-cluster/tests/test_lan_curl_fallback.py::test_mirrored_block_is_identical
# — edit all three copies in the same commit, or that test fails.
#
# Deliberately narrow. The fallback fires ONLY when urllib reports errno 65
# SPECIFICALLY on a PRIVATE host:
#   * Linux EHOSTUNREACH is 113 and means there genuinely is no route, so it
#     must NOT be retried by anything — a "helpful" fallback there would hide a
#     dead node behind a second probe.
#   * A public host is not the Local Network story at all (TCC gates only local
#     traffic), so falling back there would paper over a real firewall or DNS
#     problem.
#   * Loopback never falls back: TCC does not gate localhost, so errno 65
#     there is not this bug.
# HTTPError, timeouts and every other errno propagate UNCHANGED, so callers
# keep their existing 'probe error' vs 'unreachable' distinction.
import ipaddress
import queue
import urllib.error
import urllib.parse

LAN_CURL_FALLBACK_NOTICE = "LAN via curl fallback (Local Network permission)"

_CURL_BIN = "/usr/bin/curl"
_CURL_UNUSABLE_HINT = (" — /usr/bin/curl fallback unavailable: restore it, or grant "
                       "macOS Local Network to this interpreter")

# One-shot ticket: filled at import, consumed by the first successful fallback.
# Consume-once is the whole point — `cluster monitor --live` re-renders every
# tick and the daemon loop ticks forever, so the notice must not repeat. A
# maxsize-1 Queue rather than a bool because the monitor scrapes backends from
# a thread pool, where a read-then-write flag would let two workers announce.
_CURL_FALLBACK_TICKET: "queue.Queue" = queue.Queue(maxsize=1)
_CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)

# 100.64.0.0/10 is CGNAT — where Tailscale hands out addresses — and Python
# does NOT classify it private, so it is named explicitly. fe80::/10 and
# fc00::/7 cover IPv6 link-local and ULA.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_ULA_NET = ipaddress.ip_network("fc00::/7")


def _lan_private_host(host) -> bool:
    """True for a private/LAN host the Local Network gate can deny.

    RFC1918, 100.64.0.0/10 (tailnet CGNAT), 169.254.0.0/16 + fe80::/10
    link-local, fc00::/7 ULA, and ``.local`` mDNS names. Loopback — numeric or
    ``localhost`` — is False: TCC does not gate it, so errno 65 there is not
    this bug. A name that is not an IP is False unless it is ``.local``, so a
    public hostname can never reach the fallback.
    """
    h = (host or "").strip().rstrip(".").lower()
    if not h or h == "localhost":
        return False
    if h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    # Family guard: `IPv4Address in <IPv6Network>` raises TypeError, so a
    # membership test must only run against the network of the same family.
    if ip.version == 4:
        return bool(ip.is_private or ip.is_link_local or ip in _CGNAT_NET)
    return bool(ip.is_private or ip.is_link_local or ip in _ULA_NET)


def _lan_errno(exc):
    """The errno behind a urllib failure, or None when it carries none.

    ``URLError.reason`` is the OSError raised by the connect. ``HTTPError`` is
    a URLError subclass whose reason is a *status string*, so it lands here as
    None — a status is never a connectivity denial, and never a fallback.
    """
    return getattr(getattr(exc, "reason", None), "errno", None)


def _lan_curl_notice_once() -> None:
    """Print the fallback notice the first time only, then stay quiet.

    stderr, not stdout: the two probe mirrors print structured output on stdout
    (doctor's checklist, template's ``--json``) and must not be polluted, and
    the daemon log picks stderr up — one line, then silence.
    """
    import sys
    try:
        _CURL_FALLBACK_TICKET.get_nowait()
    except queue.Empty:
        return
    print(LAN_CURL_FALLBACK_NOTICE, file=sys.stderr, flush=True)


def _lan_curl_reset_notice() -> None:
    """Refill the one-shot notice ticket.

    Test seam only — no runtime path calls it. A test that asserts 'the notice
    appears exactly once across N fallbacks' has to start from a known state,
    and the suite raises far more than one fallback per process, so without
    this the assertion would silently depend on test order.
    """
    while not _CURL_FALLBACK_TICKET.empty():
        _CURL_FALLBACK_TICKET.get_nowait()
    _CURL_FALLBACK_TICKET.put_nowait(LAN_CURL_FALLBACK_NOTICE)


def _lan_curl_run(argv, timeout):
    """Run one curl argv LIST (never a shell) -> (returncode, stdout, stderr).

    Injectable seam: the suite replaces this module attribute with a stub that
    inspects argv — which is also how 'the api_key never reaches argv' is
    proven, since every test asserts on the argv the stub was handed.
    """
    import subprocess
    p = subprocess.run(argv, capture_output=True, timeout=timeout + 10)
    return p.returncode, p.stdout, p.stderr


def _lan_curl_unusable(original, cause) -> Exception:
    """What the caller sees when curl could not be run at all.

    Same ``URLError`` family the caller already handles, and its text still
    carries the ORIGINAL errno-65 message — the hint is appended, never
    substituted, so a reader still learns the endpoint was denied.
    """
    return urllib.error.URLError(f"{original}{_CURL_UNUSABLE_HINT} [{cause}]")


def _lan_curl_argv(url, timeout, header_file=None) -> list:
    """The curl argv.

    ``-sS`` quiet but loud on error, ``--fail`` so a 4xx/5xx becomes exit 22
    (urllib's HTTPError equivalent), ``-L`` because urllib follows redirects
    too, ``--max-time`` so a hung node cannot hang the monitor. The URL is
    always the LAST token, and no token ever holds a secret.
    """
    argv = [_CURL_BIN, "-sS", "--fail", "-L", "--max-time", str(timeout)]
    if header_file:
        argv += ["-H", f"@{header_file}"]
    argv.append(url)
    return argv


def _lan_curl_write_header(api_key) -> str:
    """Write the Bearer header to a 0600 temp file and return its path.

    The api_key must NEVER be an argv token: ``ps`` shows every argument of a
    child process for the life of the request, so ``-H "Authorization: Bearer
    <key>"`` would leak the key to anyone on the box. curl reads the header
    from the file instead (``-H @path``), and the caller unlinks it.
    """
    import os
    import tempfile
    fd, path = tempfile.mkstemp(prefix="hscc-lan-hdr-")
    try:
        os.fchmod(fd, 0o600)  # mkstemp is 0600 already; pin it explicitly
        with os.fdopen(fd, "w") as fh:
            fh.write(f"Authorization: Bearer {api_key}\n")
    except OSError:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _lan_curl_http_error(url, stderr) -> Exception:
    """Map curl's exit-22 back onto urllib's HTTPError family.

    Parity matters more than the number: callers branch on HTTPError to say
    'endpoint reachable, probe/auth wrong' as opposed to 'unreachable', so the
    fallback raises the same family urllib would, carrying the status curl
    reported. ``code`` is 0 when curl did not name one — the exception TYPE is
    what callers branch on.
    """
    import re
    m = re.search(r"returned error: (\d{3})",
                  (stderr or b"").decode("utf-8", "replace"))
    code = int(m.group(1)) if m else 0
    reason = f"curl fallback: HTTP {code}" if code else "curl fallback: HTTP error"
    return urllib.error.HTTPError(url, code, reason, None, None)  # type: ignore[arg-type]


def _lan_curl_fallback(url, timeout, exc, api_key=None):
    """Retry one denied GET through /usr/bin/curl -> the body bytes, or None.

    Returns the response body when the retry succeeded; None means 'not
    eligible, or curl could not reach it either' and the caller MUST re-raise
    the original error unchanged. ``api_key`` is only ever sent as a header
    from a 0600 temp file — never in argv, never in a message.
    """
    import os
    if _lan_errno(exc) != 65:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not _lan_private_host(parsed.hostname):
        return None
    header_file = None
    try:
        if api_key:
            header_file = _lan_curl_write_header(api_key)
        rc, out, err = _lan_curl_run(_lan_curl_argv(url, timeout, header_file),
                                     timeout)
    except Exception as cause:      # curl missing / not executable / timed out
        raise _lan_curl_unusable(exc, cause) from exc
    finally:
        if header_file:
            try:
                os.unlink(header_file)
            except OSError:
                pass
    if rc == 22:
        raise _lan_curl_http_error(url, err) from exc
    if rc != 0:
        # curl could not reach it either — it really is down. Let the original
        # errno-65 error stand with the caller's own wording.
        return None
    _lan_curl_notice_once()
    return out
# ── END mirrored block: lan_curl_fallback ──────────────────────────────────

def _http_get_default(url: str, api_key: str | None = None) -> str:
    """Fetch ``url`` and return its body as text.

    Sends ``Authorization: Bearer <api_key>`` when a key is provided (the
    configs carry api_key next to base_url; an endpoint that requires a key
    returns 401 without it). Raises ``urllib.error.HTTPError`` on a non-2xx
    HTTP status (endpoint reachable but the path/auth is wrong) and
    ``urllib.error.URLError``/other for network failures, so callers can tell
    a probe/config error apart from an unreachable endpoint.
    """
    import urllib.error
    import urllib.request
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as e:
        # A macOS Local-Network denial (errno 65 on a private host) retries
        # once through /usr/bin/curl; everything else propagates unchanged, so
        # callers still tell a probe error apart from an unreachable endpoint.
        body = _lan_curl_fallback(url, 10, e, api_key=api_key)
        if body is None:
            raise
        return body.decode("utf-8")


def _collect_model_entries(cfg: dict) -> list:
    """Return ``(model_id, base_url, api_key, config_key)`` for every model.

    Sources: top-level ``model``, ``delegation``, each ``fallback_providers[]``,
    and every ``auxiliary.*`` entry. Entries missing a model id or base_url are
    skipped (no probe URL means nothing to verify). ``api_key`` is carried from
    the same config entry so the probe can authenticate.
    """
    entries = []

    def add(model, base_url, api_key, key):
        if model and isinstance(model, str) and model.strip():
            entries.append((model.strip(), (base_url or "").strip(),
                            (api_key or "").strip() or None, key))

    top = cfg.get("model")
    if isinstance(top, dict):
        add(top.get("default"), top.get("base_url"), top.get("api_key"),
            "model.default")

    dlg = cfg.get("delegation")
    if isinstance(dlg, dict):
        add(dlg.get("model"), dlg.get("base_url"), dlg.get("api_key"),
            "delegation.model")

    for i, fp in enumerate(cfg.get("fallback_providers") or []):
        if isinstance(fp, dict):
            add(fp.get("model"), fp.get("base_url"), fp.get("api_key"),
                f"fallback_providers[{i}].model")

    aux = cfg.get("auxiliary")
    if isinstance(aux, dict):
        for task, ent in aux.items():
            if isinstance(ent, dict):
                add(ent.get("model"), ent.get("base_url"), ent.get("api_key"),
                    f"auxiliary.{task}.model")

    return entries


def _check_models_served(hermes_home=None, *, _http_get=None) -> Check:
    """Verify every configured model id is actually served by its endpoint.

    Non-fatal (warning): a stale model id is drift to fix (every call 404s),
    not a reason to hard-stop bootstrap.

    Three distinct outcomes are reported (never collapsed):
      - endpoint UNREACHABLE (network/timeout) -> ok (explain + skip), because
        we can't verify anything and it must not false-alarm;
      - endpoint reachable but the ``/models`` path returns 404/401 (or an
        unparsable body) -> PROBE/CONFIG ERROR, reported loudly as a real
        problem rather than a non-event;
      - a model id absent from a successfully-parsed served list -> the
        mismatch, which names the config key + endpoint + served ids.

    ``_http_get(url, api_key=None) -> str`` receives the RESOLVED probe URL
    (e.g. ``http://host:port/v1/models``) plus the entry's api_key for auth and
    returns the response body text; injectable for tests (mirrors how
    ``_cluster_runner`` is injected elsewhere).
    """
    import json
    from urllib.error import HTTPError

    home = (hermes_home or os.environ.get("HERMES_HOME")
            or os.path.expanduser("~/.hermes"))
    config_path = os.path.join(home, "config.yaml")

    try:
        import yaml
        with open(config_path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:
        return Check("models served", True,
                     detail=f"config unreadable ({exc}); skipped", fatal=False)

    if not isinstance(cfg, dict):
        return Check("models served", True,
                     detail="config has no model entries; skipped", fatal=False)

    entries = _collect_model_entries(cfg)
    if not entries:
        return Check("models served", True,
                     detail="no configured model ids; nothing to verify",
                     fatal=False)

    getter = _http_get or _http_get_default

    # Group by resolved probe URL, deduping duplicate endpoints and keeping
    # first-seen order so detail output is stable. The probe api_key is taken
    # from the first entry at that endpoint that carries one.
    by_url = {}
    for model_id, base_url, api_key, key in entries:
        url = _models_url(base_url)
        if not url:
            continue
        group = by_url.setdefault(url, [])
        if not any(entry[2] for entry in group) and api_key:
            group.insert(0, (model_id, key, api_key))
            continue
        group.append((model_id, key, None))

    if not by_url:
        return Check("models served", True,
                     detail="no model endpoints found; nothing to verify",
                     fatal=False)

    unreachable = []
    probe_errors = []
    problems = []
    checked = []
    for url, pairs in by_url.items():
        api_key = next((k for _, _, k in pairs if k), None)
        try:
            raw = getter(url, api_key=api_key)
        except HTTPError as exc:
            # Endpoint reachable but the /models path/auth is wrong -> a real
            # config/probe problem, report it loudly (not a non-event).
            probe_errors.append(
                f"{url} returned HTTP {exc.code} on the /models probe "
                f"(endpoint reachable; wrong path or missing/bad auth key)")
            continue
        except Exception as exc:
            # Network/timeout -> can't verify, explain and skip, no false alarm.
            unreachable.append(f"{url} unreachable ({exc}); skipped")
            continue
        try:
            data = json.loads(raw)
            served = [str(m.get("id")) for m in (data.get("data") or [])
                      if isinstance(m, dict) and m.get("id")]
        except Exception:
            probe_errors.append(
                f"{url} returned an unparsable body on the /models probe; skipped")
            continue

        served_line = ", ".join(served) if served else "(none)"
        served_set = set(served)
        for model_id, key, _ in pairs:
            checked.append(f"{key} -> {model_id} @ {url}")
            if model_id not in served_set:
                problems.append(
                    f"{key} ('{model_id}') not served at {url}; "
                    f"served: {served_line}")

    fail_bits = []
    fail_bits += problems
    # Probe/config errors are real problems too — report them alongside
    # mismatches so the operator sees an unhealthy endpoint, not silence.
    fail_bits += probe_errors

    if fail_bits:
        fix = ("Set each misconfigured model id to one already served by its "
               "endpoint (see detail), or re-run apply_template to regenerate "
               "the config from current templates. If an endpoint 404s on its "
               "/models path, fix the base_url (or add the correct auth key).")
        return Check("models served", False,
                     detail="; ".join(fail_bits), fix=fix, fatal=False)

    if unreachable and not checked:
        return Check("models served", True,
                     detail="; ".join(unreachable) + " — nothing verified",
                     fatal=False)

    bits = checked + unreachable
    if not bits:
        return Check("models served", True,
                     detail="no endpoints probed", fatal=False)

    return Check("models served", True,
                 detail="; ".join(bits) + " — all configured models served",
                 fatal=False)


def _run_cluster_list() -> str:
    try:
        r = subprocess.run(["sparkrun", "cluster", "list", "--json"],
                           capture_output=True, text=True, timeout=15)
        out = (r.stdout or "").strip()
        return out if (r.returncode == 0 and out and out != "[]") else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


# ── proxy-outage 2026-10-11 report-only checks (t_proxy_outage_20261011) ────
#
# Three fleet-state checks the outage proved worth having BEFORE the next
# outage: (4) sparkrun's own interpreter can be denied macOS Local Network
# (errno 65 on every private-range connect while nc/ssh work fine); (5) the
# saved sparkrun cluster can drift from serving.json (it lacked the
# orchestrator node during the outage); (6) duplicate sparkrun jobs on one
# node+port (the intent-hash duplicate class) and stale serving.json endpoint
# records. All three are warnings — report only, never fix, never fatal.


def _node_hosts(_cluster_raw=None) -> List[str]:
    """Every host of the default sparkrun cluster (for the node-recovery check).

    Reuses the ONE cluster-list runner in this module; ``_cluster_raw`` is the
    same injectable stand-in the NAS check uses, so the suite never shells out
    to the live fleet. Unresolvable is ``[]`` — the node-recovery check then
    renders an honest ERROR, never a vacuous pass.
    """
    import json
    raw = (_cluster_raw or _run_cluster_list)()
    if not raw:
        return []
    try:
        clusters = json.loads(raw)
        chosen = next((c for c in clusters if c.get("default")),
                      (clusters[0] if clusters else {}))
        return [h for h in (chosen.get("hosts") or []) if h]
    except (ValueError, IndexError, KeyError, AttributeError):
        return []


def _serving_units(_serving_raw=None):
    """Units list from serving.json (injectable raw text for tests)."""
    import json
    raw = _serving_raw
    if raw is None:
        path = os.path.expanduser(os.environ.get("HSCC_SERVING_JSON",
                                                 "~/.hscc/serving.json"))
        try:
            with open(path) as fh:
                raw = fh.read()
        except OSError:
            return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    units = data.get("units") if isinstance(data, dict) else None
    return [u for u in (units or []) if isinstance(u, dict)]


def _sparkrun_interpreter():
    """(cli_path, interpreter) for the sparkrun CLI: which → ~/.local/bin,
    then read the shebang. (None, None) when the CLI cannot be found.

    The 2026-10-11 incident class: sparkrun installed under a Homebrew or
    python.org Framework python — a binary whose TCC identity may lack the
    macOS Local Network grant, so EVERY private-range connect from it fails
    errno 65 while ssh/nc from other binaries work.
    """
    cli = shutil.which("sparkrun") or os.path.expanduser("~/.local/bin/sparkrun")
    if not os.path.isfile(cli) or not os.access(cli, os.X_OK):
        return None, None
    try:
        with open(cli, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return cli, None
    if not first.startswith(b"#!"):
        return cli, None
    shebang = first[2:].decode("utf-8", "replace").strip()
    # "#!/usr/bin/env python3" style → the interpreter is resolved at exec;
    # take the literal path when absolute, else None (env/relative).
    parts = shebang.split()
    interp = parts[0] if parts else ""
    if interp.startswith("/"):
        return cli, interp
    if parts and parts[0].endswith("/env") and len(parts) > 1:
        return cli, shutil.which(parts[1])  # best effort via PATH
    return cli, None


_INTERP_DENY_HINT = ("macOS Local Network permission blocks this Python — "
                    "rebuild sparkrun on a uv Python")
_FRAMEWORK_HINT = ("sparkrun runs on a Homebrew/Framework python — its TCC "
                   "identity may lack the Local Network grant; a uv-built "
                   "sparkrun is the supported install")


def _sparkrun_lan_reachability(_venv_python=None, _probe=None,
                               _serving_raw=None) -> Check:
    """TCP-probe every serving node FROM SPARKRUN'S OWN INTERPRETER (report only).

    A python with no Local Network grant answers errno 65 for every
    private-range connect, so the outage looked like "fleet down" while the
    fleet was fine — the ONLY probe that catches it is one that runs under
    sparkrun's interpreter, not doctor's own. Connect target is port 22:
    what sparkrun itself must reach.
    """
    cli, interp = (None, None)
    if _venv_python is None:
        cli, interp = _sparkrun_interpreter()
    else:
        interp = _venv_python
    if not interp:
        return Check("sparkrun lan reachability", True,
                     detail="sparkrun interpreter unresolved — skipped",
                     fatal=False)

    nodes = []
    for u in _serving_units(_serving_raw):
        for n in (u.get("nodes") or []):
            if n and n not in nodes:
                nodes.append(n)
    if not nodes:
        return Check("sparkrun lan reachability", True,
                     detail="no serving nodes to probe — skipped", fatal=False)

    deny_detected = False
    failures = []
    for node in nodes:
        if _probe is not None:
            err = _probe(interp, node)
        else:
            err = _tcp_probe_error(interp, node, 22)
        if err is None:
            continue
        failures.append(f"{node}: {err}")
        if "errno 65" in err or "No route to host" in err:
            deny_detected = True

    detail_bits = []
    is_homebrew = interp.startswith("/opt/homebrew/") or interp.startswith(
        "/usr/local/Cellar/") or interp.startswith("/Library/Frameworks/")
    if is_homebrew:
        detail_bits.append(_FRAMEWORK_HINT)
    if deny_detected:
        return Check("sparkrun lan reachability", False,
                     detail="; ".join(failures) + " — " + _INTERP_DENY_HINT,
                     fix=_INTERP_DENY_HINT + " (uv venv), re-grant Local "
                         "Network to it in System Settings > Privacy.",
                     fatal=False)
    if failures:
        return Check("sparkrun lan reachability", False,
                     detail="; ".join(failures)
                     + ("" if not detail_bits else " — " + "; ".join(detail_bits)),
                     fix="Check node reachability / ssh.", fatal=False)
    if detail_bits:
        return Check("sparkrun lan reachability", True,
                     detail=f"all {len(nodes)} serving node(s) reachable; "
                            + "; ".join(detail_bits), fatal=False)
    return Check("sparkrun lan reachability", True,
                 detail=f"all {len(nodes)} serving node(s) reachable from "
                        f"{os.path.basename(interp)}", fatal=False)


def _tcp_probe_error(interpreter, host, port, timeout=5):
    """None when <interpreter> can TCP-connect host:port; else the error text.

    Runs the connect INSIDE the given interpreter so the verdict belongs to
    that binary's TCC identity — the entire point (doctor's own python having
    the grant says nothing about sparkrun's).
    """
    script = (
        "import socket,sys\n"
        "try:\n"
        "    s=socket.create_connection((sys.argv[1],int(sys.argv[2])),5)\n"
        "    s.close()\n"
        "except OSError as e:\n"
        "    print(f'errno={e.errno} {e.strerror}')\n"
        "    sys.exit(1)\n"
    )
    try:
        r = subprocess.run([interpreter, "-c", script, host, str(port)],
                           capture_output=True, text=True, timeout=timeout + 5)
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return f"probe could not run: {e}"
    if r.returncode == 0:
        return None
    return (r.stdout or r.stderr or "connect failed").strip()


def _run_cluster_default() -> str:
    try:
        r = subprocess.run(["sparkrun", "cluster", "default", "--json"],
                           capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _sparkrun_default_cluster_matches_serving(_runner=None,
                                              _serving_raw=None) -> Check:
    """Warn when `sparkrun cluster default` hosts differ from serving.json nodes.

    During the outage the saved cluster LACKED the orchestrator node, so any
    sparkrun operation scoped to the cluster (including the proxy relaunch)
    silently excluded it. Report only — re-writing a saved cluster is an
    operator decision.
    """
    import json
    raw = (_runner or _run_cluster_default)()
    if not raw:
        return Check("sparkrun cluster vs serving", True,
                     detail="no default cluster / sparkrun not reachable — skipped",
                     fatal=False)
    try:
        data = json.loads(raw)
    except ValueError:
        return Check("sparkrun cluster vs serving", True,
                     detail="unparseable cluster default output — skipped",
                     fatal=False)
    cluster_hosts = {h for h in (data.get("hosts") or []) if h}
    serving_nodes = set()
    for u in _serving_units(_serving_raw):
        serving_nodes.update(n for n in (u.get("nodes") or []) if n)
    if not cluster_hosts or not serving_nodes:
        return Check("sparkrun cluster vs serving", True,
                     detail="nothing to compare — skipped", fatal=False)
    missing_in_cluster = serving_nodes - cluster_hosts
    extra_in_cluster = cluster_hosts - serving_nodes
    if not missing_in_cluster and not extra_in_cluster:
        return Check("sparkrun cluster vs serving", True,
                     detail=f"cluster hosts == serving nodes ({len(cluster_hosts)})",
                     fatal=False)
    bits = []
    if missing_in_cluster:
        bits.append(f"serving nodes MISSING from sparkrun cluster: "
                    f"{sorted(missing_in_cluster)}")
    if extra_in_cluster:
        bits.append(f"cluster hosts not in serving.json: {sorted(extra_in_cluster)}")
    return Check("sparkrun cluster vs serving", False,
                 detail="; ".join(bits),
                 fix="sparkrun cluster update <name> --hosts <full serving set>",
                 fatal=False)


def _run_cluster_status(cluster) -> str:
    if not cluster:
        return ""
    try:
        r = subprocess.run(["sparkrun", "status", "--cluster", cluster, "--json"],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return ""


def _serving_consistency(_status_runner=None, _serving_raw=None,
                         cluster=None) -> Check:
    """Flag duplicate sparkrun jobs on one node+port + stale endpoint records.

    The 2026-10-11 duplicate class: force-recreates with a changed
    --served-model-name carry a different sparkrun intent hash, so the OLD
    container is left running beside the NEW one on the same node+port. One
    of the pair maps to serving.json's serve_cmd / (node,port); the unmapped
    extra is the duplicate. The inverse — a serving.json (node,port) with no
    running container — is a stale record: something the fleet believes is
    served but sparkrun cannot see. Report only.
    """
    import json
    if cluster is None:
        cluster = os.environ.get("HSCC_CLUSTER", "hscc")
    raw = (_status_runner or _run_cluster_status)(cluster)
    units = _serving_units(_serving_raw)
    if not raw or not units:
        return Check("serving consistency", True,
                     detail="no status data or no serving units — skipped",
                     fatal=False)
    try:
        data = json.loads(raw)
    except ValueError:
        return Check("serving consistency", True,
                     detail="unparseable sparkrun status JSON — skipped",
                     fatal=False)

    # (host, port) -> [container entries] from groups + solo_entries
    running = {}
    for _cid, group in (data.get("groups") or {}).items():
        meta = group.get("meta") or {}
        port = meta.get("port")
        for c in group.get("containers") or []:
            host = c.get("host")
            if host and port is not None:
                running.setdefault((host, int(port)), []).append(c)
    for entry in data.get("solo_entries") or []:
        meta = entry.get("meta") or {}
        host, port = entry.get("host"), meta.get("port")
        if host and port is not None:
            running.setdefault((host, int(port)), []).append(entry)

    # serving.json's authoritative (host, port) set: every unit's PRIMARY
    # (nodes[0]) on the unit's port; tp peers expose no endpoint of their own.
    mapped = set()
    for u in units:
        nodes = [n for n in (u.get("nodes") or []) if n]
        port = u.get("port")
        if nodes and port is not None:
            mapped.add((nodes[0], int(port)))

    problems = []
    for (host, port), containers in sorted(running.items()):
        if len(containers) > 1 and (host, port) in mapped:
            problems.append(
                f"{host}:{port} has {len(containers)} sparkrun containers; "
                f"serving.json maps only one — the extra is the duplicate class "
                f"(intent-hash drift on a served-name change), stop the unmapped one")
    for (host, port) in sorted(mapped):
        if (host, port) not in running:
            problems.append(
                f"serving.json record {host}:{port} has no running container "
                f"(stale endpoint record)")
    if problems:
        return Check("serving consistency", False,
                     detail="; ".join(problems),
                     fix="Report only — verify each duplicate/stale pair with "
                         "`sparkrun status --cluster "
                         f"{cluster}` before stopping anything.",
                     fatal=False)
    return Check("serving consistency", True,
                 detail=f"{len(mapped)} serving.json endpoint(s) == sparkrun "
                        f"containers, no duplicates", fatal=False)


def _refused_role_soul_scan() -> dict:
    """In-suite stand-in for the role-SOUL scan when no seam was injected.

    The check's real target is SOUL.md under rolelib.PROFILES_DIR — the operator's
    LIVE profile root when the suite runs from a dispatched worker profile. Same
    fence shape as ``_refused_transport`` above: the check comes back an ERROR
    naming the missing seam, never a silent pass and never a real read.
    """
    return {"name": doctor_role_soul.check_name(), "ok": False, "fatal": False,
            "rows": [], "scanned": 0,
            "detail": "role-SOUL scan refused inside a pytest process — inject "
                      "_role_soul_scan (fixture profile dirs)",
            "fix": "Pass _role_soul_scan=lambda: {...} built from tmp profile "
                   "fixtures; a suite must never read the operator's profiles."}


def run_doctor(hermes_home: Optional[str] = None, *, _cluster_runner=None,
               _http_get=None, _serving_raw=None, _venv_python=None,
               _lan_probe=None, _cluster_default_runner=None,
               _status_runner=None, fleet_checks=None, _ssh_factory=None,
               hscc_dir: Optional[str] = None, _role_soul_scan=None) -> dict:
    """Run all checks.

    Fleet checks (lan reachability / cluster-vs-serving / serving
    consistency) shell out to sparkrun and TCP-probe the fleet — READ-ONLY,
    but still fleet I/O. ``fleet_checks=True`` forces them, False skips them
    (reported as skipped); the default (None) auto-detects: they run in a
    human CLI process and skip inside a pytest process, so the test suite can
    never accidentally probe the live fleet by calling run_doctor(). A test
    that WANTS them passes fleet_checks=True plus the stub seams.

    ``_ssh_factory`` / ``hscc_dir`` are the node-recovery check's seams: the
    per-node ssh transport (production: sparkrun's own remote execution) and
    the dir its ``cluster.json`` opt-out block is read from. Both are injected
    in tests so no doctor test can reach a node.

    ``_role_soul_scan`` is the role-SOUL identity check's seam (t_c823ebc9): a
    zero-arg callable returning the Check-shaped dict. Inside a pytest process
    the scan MUST be injected, exactly like ``_ssh_factory`` — its default
    target reads SOUL.md under the live profiles root, which in the operator's
    env is real profile state. Uninjected inside a suite it renders as an ERROR
    naming the missing seam, never as a pass.
    """
    if fleet_checks is None:
        fleet_checks = "pytest" not in sys.modules
    home = (hermes_home or os.environ.get("HERMES_HOME")
            or os.path.expanduser("~/.hermes"))
    checks: List[Check] = [
        _python_ok(),
        _pyyaml_ok(),
        _sparkrun_ok(),
        _sparkrun_cluster_ok(_cluster_runner),
        _hermes_ok(home),
        # _nas_ok also shells out to `sparkrun cluster list`, so the injected
        # runner has to reach it too — otherwise every run_doctor() call in the
        # suite pays a real 15s subprocess against the live cluster config.
        _nas_ok(_cluster_runner),
        _disk_ok(os.path.expanduser("~")),
        _gateway_running(),
        # Models-served is non-fatal (warning): drift to fix, not a reason to
        # hard-stop. It probes each distinct endpoint once; a probe failure is
        # reported as ok (no false alarm), so it never flips preflight `ok`.
        _check_models_served(home, _http_get=_http_get),
    ]
    if fleet_checks:
        # A pytest process must never acquire sparkrun's production ssh
        # transport, even when a test forces fleet_checks=True: the transport
        # has to be injected. Without it the node-recovery check renders as an
        # ERROR naming the missing seam — an honest "cannot verify", never a
        # silent skip and never a real ssh attempt against the live fleet.
        def _refused_transport(_host):
            def refused(_cmd, _timeout):
                return {"ok": False, "stdout": "", "code": None,
                        "stderr": "production ssh transport refused inside a "
                                  "pytest process — inject _ssh_factory"}
            return refused
        nr_factory = _ssh_factory or (
            _refused_transport if "pytest" in sys.modules else None)
        nr_res = doctor_node_recovery.check(
            _node_hosts(_cluster_runner), ssh_factory=nr_factory,
            hscc_dir=hscc_dir)
        # Same fence for the role-SOUL audit: it reads SOUL.md files under
        # rolelib.PROFILES_DIR, which in the operator's env is the LIVE profile
        # root. A pytest process must never read it, so inside a suite the scan
        # has to be injected (fixture dirs); without it the check renders as an
        # ERROR naming the missing seam — an honest "cannot verify", never a
        # silent pass and never a real read of the operator's profiles.
        rs_scan = _role_soul_scan or (
            _refused_role_soul_scan if "pytest" in sys.modules
            else doctor_role_soul.check)
        rs_res = rs_scan()

        # Only the five Check fields go into the list, so every entry of
        # `checks` — and therefore `doctor --json` — keeps one shape. The
        # per-node rows stay available to callers of
        # doctor_node_recovery.check() (the CLI, and the tests).
        checks += [
            Check(name=nr_res["name"], ok=nr_res["ok"], detail=nr_res["detail"],
                  fix=nr_res["fix"], fatal=nr_res["fatal"]),
            # Role-SOUL identity drift (t_c823ebc9): WARNING, never fatal — a
            # profile running under another role's identity is a loud red line
            # to fix, not a reason to hard-stop an install.
            Check(name=rs_res["name"], ok=rs_res["ok"], detail=rs_res["detail"],
                  fix=rs_res["fix"], fatal=rs_res["fatal"]),
            _sparkrun_lan_reachability(_venv_python=_venv_python,
                                       _probe=_lan_probe,
                                       _serving_raw=_serving_raw),
            _sparkrun_default_cluster_matches_serving(
                _runner=_cluster_default_runner, _serving_raw=_serving_raw),
            _serving_consistency(_status_runner=_status_runner,
                                 _serving_raw=_serving_raw),
        ]
    else:
        checks.append(Check("sparkrun lan reachability", True,
                            detail="skipped (fleet checks off)", fatal=False))
        # Same honesty for the node-recovery check: a skipped check prints as
        # skipped, never as "armed/ok" and never as absence-of-output.
        checks.append(Check(doctor_node_recovery.CHECK_NAME, True,
                            detail="skipped (fleet checks off)", fatal=False))
        # ...and the same for role-SOUL identity, so a suite run prints an
        # explicit skipped line instead of quietly not auditing anything. The
        # name comes through check_name() so the skipped line can never print a
        # different key than the real check would have used.
        checks.append(Check(doctor_role_soul.check_name(), True,
                            detail="skipped (fleet checks off)", fatal=False))
    fatal_failed = [c for c in checks if not c.ok and c.fatal]
    return {
        "ok": not fatal_failed,
        "checks": [asdict(c) for c in checks],
        "fatal_failures": [c.name for c in fatal_failed],
    }


# ---------------------------------------------------------------------------
# Card D: one-time CONCRETE → alias conversion for live configs.
# See module docstring. Conservative endpoint-based mapping plus a SAFETY
# GATE: an entry is converted ONLY after the endpoint is probed (`/models`,
# reusing `_models_url` + Bearer auth from the "models served" check above,
# via `_probe_served_models`) and confirmed to serve the target alias.
# ---------------------------------------------------------------------------

# Stable logical aliases the serving layer advertises (Card A1).
ORCHESTRATOR_ALIAS = "orchestrator-model"
WORKER_ALIAS = "worker-model"

# Endpoint discriminators — mirror enable_plugins / generator topology:
#   orchestrator = loopback/local at the orch vLLM port, or the known orch host.
#   worker       = loopback/proxy at :4000 (sparkrun LiteLLM load-balancer).
_ORCH_HOST = "10.0.0.244"
_ORCH_PORT = 8000
_WORKER_HOST = "10.0.0.245"
_WORKER_PORT = 4000


def _classify_endpoint(base_url):
    """Classify a base_url as 'orchestrator', 'worker', or None (indeterminate).

    Conservative: only unambiguous loopback / localnode / known-cluster-host
    matches resolve. Any remote/cloud base_url (e.g. api.openai.com,
    api.anthropic.com) or anything we cannot clearly attribute returns None, so
    the migration never guesses an endpoint.
    """
    if not base_url or not isinstance(base_url, str):
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url.strip())
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return None
    if not host or port is None:
        return None

    if host in ("localhost", "127.0.0.1"):
        if port == _ORCH_PORT:
            return "orchestrator"
        if port == _WORKER_PORT:
            return "worker"
        return None
    if host == _ORCH_HOST:
        return "orchestrator" if port == _ORCH_PORT else None
    if host == _WORKER_HOST:
        return "worker" if port == _WORKER_PORT else None
    return None


def _probe_served_models(base_url, api_key=None, *, _http_get=None):
    """Probe an endpoint's ``/models`` and report what it serves.

    Reuses the same probe machinery as the ``models served`` doctor check
    (``_models_url`` for URL normalization preserving ``/v1``, and
    ``_http_get`` / ``_http_get_default`` for the Bearer-authenticated GET) -
    endpoint probing is NOT re-implemented here.

    Returns ``(status, served_ids)`` where ``status`` is one of:
      - ``"ok"``          - endpoint reachable and ``/models`` parsed; ``served_ids``
                            is the list of served model ids (possibly empty).
      - ``"unreachable"`` - network/timeout; nothing could be verified.
      - ``"error"``       - endpoint reachable but ``/models`` returned a non-2xx
                            HTTP status or an unparseable body (probe/config
                            problem).

    ``_http_get(url, api_key=None) -> str`` is injectable for tests (mirrors
    ``_check_models_served``) so the gate can be exercised without the network.
    """
    getter = _http_get or _http_get_default
    url = _models_url(base_url)
    if not url:
        return ("error", [])
    import json
    from urllib.error import HTTPError
    try:
        raw = getter(url, api_key=api_key)
    except HTTPError as exc:
        return ("error", [f"{url} returned HTTP {exc.code} on /models probe"])
    except Exception as exc:
        return ("unreachable", [f"{url} unreachable ({exc})"])
    try:
        data = json.loads(raw)
        served = [m.get("id") for m in (data.get("data") or [])
                  if isinstance(m, dict) and m.get("id")]
    except Exception as exc:
        return ("error", [f"{url} body not parseable as /models JSON ({exc})"])
    return ("ok", served)


def _convert_orchestrator_ids_to_alias(cfg, alias=ORCHESTRATOR_ALIAS,
                                       worker_alias=WORKER_ALIAS, *,
                                       _http_get=None,
                                       refused=None) -> list[str]:
    """Convert concrete model ids pointing at orch/worker endpoints to aliases.

    SAFETY GATE (Card D revision): an entry is ONLY converted after the endpoint
    is probed and the target alias is confirmed present in the served ``/models``
    list. This directly guards the failure mode that caused the 404 incident -
    converting to an alias the serving layer does not actually resolve.

    Per entry:
      - endpoint is remote/indeterminate (see ``_classify_endpoint``) -> left
        untouched, never guessed (no probe needed).
      - endpoint reachable and serves the alias -> model id set to the alias.
      - endpoint reachable but the alias is ABSENT from the served list -> NOT
        converted, and reported loudly on ``refused`` (which key/endpoint and
        what IS served).
      - endpoint unreachable -> NOT converted, reported loudly (never convert
        blind).

    Returns the list of changed key paths (e.g. ``["model.default"]``).
    Already-alias values are skipped (idempotent). ``cfg`` is mutated in place;
    callers write back (with backup) only if the returned list is non-empty.
    ``_http_get`` is injectable for tests; ``refused`` (a list) collects the
    loud skip reports when provided.
    """
    changed: list[str] = []
    if refused is None:
        refused = []
    if not isinstance(cfg, dict):
        return changed

    # Probe results keyed by base_url so several entries sharing one endpoint
    # do not re-hit the network. Each value is (status, served_ids, detail).
    _cache: dict = {}

    def gate(pair, key_path, want):
        """Probe-gate one candidate. Returns True only if we may convert."""
        base_url = pair.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            refused.append(
                f"! NOT CONVERTED {key_path}: no base_url; cannot probe endpoint")
            return False
        if base_url not in _cache:
            status, served = _probe_served_models(
                base_url, api_key=pair.get("api_key"), _http_get=_http_get)
            _cache[base_url] = (status, served)
        status, served = _cache[base_url]
        if status == "unreachable":
            refused.append(
                f"! NOT CONVERTED {key_path}: endpoint {base_url} unreachable "
                "- refusing to convert blind")
            return False
        if status == "error":
            refused.append(
                f"! NOT CONVERTED {key_path}: probe of {base_url} failed "
                "(reachable but /models problem); refusing to convert")
            return False
        if want not in served:
            served_str = ", ".join(served) if served else "(none reported)"
            refused.append(
                f"! NOT CONVERTED {key_path}: alias '{want}' NOT served by "
                f"{base_url} - served: {served_str}; refusing to convert "
                "(would produce a 404). Operator must resolve before migrate.")
            return False
        return True

    def convert_pair(pair, key_path, model_key, want):
        """Probe-gate and (if safe) convert one model/base_url pair in place."""
        if not isinstance(pair, dict):
            return False
        model = pair.get(model_key)
        if not isinstance(model, str) or not model:
            return False
        if model in (alias, worker_alias):
            return False  # already an alias - idempotent
        if not gate(pair, key_path, want):
            return False
        pair[model_key] = want
        changed.append(key_path)
        return True

    def _treat(pair, orch_kind, alias, worker_alias):
        """Resolve the target alias for ``pair``, or None if indeterminate.

        Returns the alias when the paired base_url is UNAMBIGUOUSLY the
        orchestrator or worker proxy. Returns None for a remote/cloud or
        otherwise indeterminate endpoint — such entries are never probed and
        never guessed at (operator-chosen endpoints survive untouched).
        """
        kind = _classify_endpoint(pair.get("base_url"))
        if kind is None:
            return None
        return alias if kind == orch_kind else worker_alias

    # Root model block - orchestrator-pointing in a live fleet.
    root_m = cfg.get("model")
    if isinstance(root_m, dict) and "default" in root_m:
        want = _treat(root_m, "orchestrator", alias, worker_alias)
        if want is not None:
            convert_pair(root_m, "model.default", "default", want)

    # Delegation - worker proxy usually.
    dlg = cfg.get("delegation")
    if isinstance(dlg, dict) and "model" in dlg:
        want = _treat(dlg, "orchestrator", alias, worker_alias)
        if want is not None:
            convert_pair(dlg, "delegation.model", "model", want)

    # fallback_providers[] - each may point at orch or worker.
    fps = cfg.get("fallback_providers")
    if isinstance(fps, list):
        for i, fp in enumerate(fps):
            if not isinstance(fp, dict):
                continue
            want = _treat(fp, "orchestrator", alias, worker_alias)
            if want is not None:
                convert_pair(fp, f"fallback_providers[{i}].model", "model",
                             want)

    # Every auxiliary.<task> block (compression + text aux + vision/web_extract).
    aux_root = cfg.get("auxiliary")
    if isinstance(aux_root, dict):
        for task, aux in aux_root.items():
            if not isinstance(aux, dict):
                continue
            want = _treat(aux, "orchestrator", alias, worker_alias)
            if want is not None:
                convert_pair(aux, f"auxiliary.{task}.model", "model", want)

    return changed


def _walk_path(cfg, path):
    """Resolve a key path in ``cfg``; supports '.' and '[i]' (e.g.
    'fallback_providers[0].model', 'auxiliary.compression.model')."""
    import re
    # Tokenize "fallback_providers[0].model" -> ["fallback_providers", "0", "model"]
    tokens = [
        t for t in re.split(r"[.\[]", path.replace("]", "")) if t != ""
    ]
    cur = cfg
    for t in tokens:
        if isinstance(cur, dict):
            if t not in cur:
                return None
            cur = cur[t]
        elif isinstance(cur, list):
            try:
                cur = cur[int(t)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _write_yaml(config_path, cfg):
    """Backup + write ``cfg`` to ``config_path`` (mirror enable_plugins).

    Uses the shared backup helper so doctor's backups are atomic (never a
    truncated ``.bak``) and retain-limited like every other HSCC writer
    (t_9462260b) — the old ``shutil.copy`` here is what left 26
    ``config.yaml.bak-*`` plus a zero-byte rollback point behind.
    """
    import yaml
    backup_util.backup_file(config_path)
    with open(config_path, "w") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False, default_flow_style=False)


def run_doctor_fix(config_path: Optional[str] = None,
                   hermes_home: Optional[str] = None,
                   *, _cluster_runner=None, _http_get=None) -> dict:
    """Run doctor + fix all non-fatal HSCC config drift.

    Reads the current config, runs checks, then calls enable_plugins.enable()
    if there are non-fatal failures. Reports what was wrong and what was fixed.

    Returns the same dict as run_doctor() plus "fixes_applied" list describing
    each corrected key and "alias_conversion_refused" (the loud safety-gate skip
    reports from the Card D migration). ``_http_get`` is injectable for tests
    (mirrors ``_check_models_served``) so the endpoint probe never needs the
    real network in the test suite.
    """
    checks_result = run_doctor(hermes_home, _cluster_runner=_cluster_runner)
    fixes_applied: list[str] = []
    alias_conversion_refused: list[str] = []

    has_nonfatal_failures = any(
        not c["ok"] and not c.get("fatal") for c in checks_result["checks"]
    )

    if config_path:
        # Capture pre-fix snapshot for drift reporting
        snapshot = {}
        try:
            import yaml
            if os.path.exists(config_path):
                with open(config_path) as fh:
                    snapshot = yaml.safe_load(fh) or {}
        except Exception:
            snapshot = {}

        # Reconcile config via enable_plugins (idempotent, preserves operator caps)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from enable_plugins import enable as _enable
        result = _enable(config_path)

        # Read post-fix config for drift reporting
        fixed_cfg = {}
        try:
            import yaml
            with open(config_path) as fh:
                fixed_cfg = yaml.safe_load(fh) or {}
        except Exception:
            pass

        # Build "was X -> set Y" report for each changed key
        for section, keys in result.items():
            # `result` sections are lists of config key names; `hooks_file`
            # (the hook-script install status dict) is surfaced alongside but
            # is not a config section — skip it. `goal_judge_profiles` is a
            # list of PROFILE FILE paths the sweep wired (t_proxy_outage_20261011),
            # not keys of THIS config — reported separately below.
            if not isinstance(keys, list) or section == "goal_judge_profiles":
                continue
            for k in keys:
                old_val = _get_nested(snapshot, section, k)
                new_val = _get_nested(fixed_cfg, section, k)
                if old_val is None or old_val == "":
                    fixes_applied.append(
                        f"{section}/{k}: was missing -> set {new_val}"
                    )
                else:
                    fixes_applied.append(
                        f"{section}/{k}: was {old_val} -> set {new_val}"
                    )

        for prof in result.get("goal_judge_profiles") or []:
            fixes_applied.append(f"auxiliary.goal_judge wired in profile file {prof}")

        # Card D: one-time CONCRETE → alias migration. Runs on the reconciled
        # config only; never touches ~/.hermes directly (config_path is the
        # sole target). SAFETY GATE: an entry is converted ONLY when the
        # endpoint is probed and confirmed to serve the alias (see
        # _convert_orchestrator_ids_to_alias). Concrete ids pointing
        # unambiguously at the orchestrator or worker proxy are replaced with
        # the alias; remote/cloud endpoints are left untouched; endpoints that
        # are unreachable or do not serve the alias are left UNCONVERTED and
        # reported loudly on alias_conversion_refused.
        try:
            import yaml
            with open(config_path) as fh:
                cur_cfg = yaml.safe_load(fh) or {}
        except Exception:
            cur_cfg = {}
        if isinstance(cur_cfg, dict):
            pre_cfg = copy.deepcopy(cur_cfg)
            alias_changes = _convert_orchestrator_ids_to_alias(
                cur_cfg, _http_get=_http_get, refused=alias_conversion_refused)
            for path in alias_changes:
                old_val = _walk_path(pre_cfg, path)
                new_val = _walk_path(cur_cfg, path)
                fixes_applied.append(
                    f"{path}: was {old_val} -> set {new_val}"
                )
            if alias_changes:
                _write_yaml(config_path, cur_cfg)

    return {**checks_result, "fixes_applied": fixes_applied,
            "alias_conversion_refused": alias_conversion_refused}


def _get_nested(cfg: dict, section: str, key: str):
    """Safely extract cfg[section][key] for drift reporting."""
    try:
        val = cfg.get(section, {})
        if isinstance(val, dict):
            val = val.get(key)
        if val is None:
            return None
        if isinstance(val, (dict, list)):
            return str(val)
        return val
    except Exception:
        return None


def _home_from_argv(argv) -> str:
    """``--home PATH`` wins over ambient HERMES_HOME.

    One explicit home is then threaded into *both* the config path and the
    hermes check, so the two can never disagree and a caller (or a test) is not
    at the mercy of whatever HERMES_HOME the surrounding process happens to
    carry. Without this, `main(["--fix"])` reads and WRITES
    $HERMES_HOME/config.yaml no matter what the caller intended.
    """
    for i, tok in enumerate(argv):
        if tok == "--home" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--home="):
            return tok.split("=", 1)[1]
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def main(argv=None) -> int:
    import json
    argv = argv if argv is not None else sys.argv[1:]
    fix_mode = "--fix" in argv
    home = _home_from_argv(argv)
    config_path = os.path.join(os.path.expanduser(home), "config.yaml")

    if fix_mode:
        res = run_doctor_fix(config_path=config_path, hermes_home=home)
    else:
        res = run_doctor(home)

    if "--json" in argv:
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1

    # Human view — themed Rich (make_console degrades to plain on a non-tty).
    _console = _theme.make_console()
    lines: list[str] = []
    for c in res["checks"]:
        mark = "✓" if c["ok"] else ("✗" if c["fatal"] else "○")
        line = f"  {mark} {c['name']}: {c['detail']}"
        if not c["ok"] and c["fix"]:
            line += f"\n      → {c['fix']}"
        lines.append(line)

    # Print fixes if we ran in --fix mode
    if fix_mode and res.get("fixes_applied"):
        lines.append("")
        lines.append("  🛠 Applied fixes:")
        for fix_line in res["fixes_applied"]:
            lines.append(f"    {fix_line}")

    # Print safety-gate refusals LOUDLY (Card D): entries we deliberately did
    # NOT convert because the endpoint did not serve the alias or was
    # unreachable. These need operator attention before re-running --fix.
    if fix_mode and res.get("alias_conversion_refused"):
        lines.append("")
        lines.append("  ⚠ ALIAS MIGRATION SKIPPED (safety gate):")
        for line in res["alias_conversion_refused"]:
            lines.append(f"    {line}")

    _console.print(_theme.panel("doctor", "\n".join(
        _theme.escape(line) for line in lines)))

    if not res["ok"]:
        _console.print(_theme.panel(
            "preflight",
            "[error]" + _theme.escape(
                "✗ FAILED: " + ", ".join(res['fatal_failures'])) + "[/error]",
        ))
        return 1
    _console.print(_theme.panel(
        "preflight", "[ok]" + _theme.escape("✓ OK") + "[/ok]"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
