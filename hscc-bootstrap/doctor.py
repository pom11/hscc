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


def _hermes_ok(hermes_home: str) -> Check:
    agent = os.path.join(hermes_home, "hermes-agent")
    if not os.path.isdir(agent):
        return Check("hermes", False, detail=f"{agent} missing",
                     fix="Install Hermes (expected at ~/.hermes/hermes-agent).",
                     fatal=True)
    return Check("hermes", True, detail=agent)


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


def _nas_ok(_runner=None) -> Check:
    """NAS reachability (non-fatal — NAS is optional but recommended for weight
    staging). Uses the sparkrun cluster's cache_dir + a ping to the NAS host if
    discoverable."""
    runner = _runner or _detect_nas
    nas = runner()
    if not nas:
        return Check("nas", True, detail="none configured (optional)", fatal=False)
    # nas may be a mount path (cache_dir) and/or an ip; just report it — a deep
    # mount probe needs ssh to a worker, which the live heal tools (nas_diagnose)
    # do. Doctor only flags presence here.
    return Check("nas", True, detail=str(nas), fatal=False)


def _detect_nas():
    """Best-effort NAS identifier from the sparkrun cluster (cache_dir)."""
    raw = _run_cluster_list()
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


def _http_get_default(url: str, api_key: str | None = None) -> str:
    """Fetch ``url`` and return its body as text.

    Sends ``Authorization: Bearer *** when a key is provided (the
    configs carry api_key next to base_url; an endpoint that requires a key
    returns 401 without it). Raises ``urllib.error.HTTPError`` on a non-2xx
    HTTP status (endpoint reachable but the path/auth is wrong) and
    ``urllib.error.URLError``/other for network failures, so callers can tell
    a probe/config error apart from an unreachable endpoint.
    """
    import urllib.request
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


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

    home = hermes_home or os.path.expanduser("~/.hermes")
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


def run_doctor(hermes_home: Optional[str] = None, *, _cluster_runner=None,
               _http_get=None) -> dict:
    home = hermes_home or os.path.expanduser("~/.hermes")
    checks: List[Check] = [
        _python_ok(),
        _pyyaml_ok(),
        _sparkrun_ok(),
        _sparkrun_cluster_ok(_cluster_runner),
        _hermes_ok(home),
        _nas_ok(),
        _disk_ok(os.path.expanduser("~")),
        _gateway_running(),
        # Models-served is non-fatal (warning): drift to fix, not a reason to
        # hard-stop. It probes each distinct endpoint once; a probe failure is
        # reported as ok (no false alarm), so it never flips preflight `ok`.
        _check_models_served(home, _http_get=_http_get),
    ]
    fatal_failed = [c for c in checks if not c.ok and c.fatal]
    return {
        "ok": not fatal_failed,
        "checks": [asdict(c) for c in checks],
        "fatal_failures": [c.name for c in fatal_failed],
    }


# ---------------------------------------------------------------------------
# Card D: one-time CONCRETE → alias conversion for live configs.
# See module docstring. Conservative endpoint-based mapping: we only rewrite a
# concrete model id when its paired base_url is UNAMBIGUOUSLY the orchestrator
# or the worker proxy. Remote/cloud endpoints are never touched.
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
    """Backup + write ``cfg`` to ``config_path`` (mirror enable_plugins)."""
    import shutil
    import time
    import yaml
    shutil.copy(config_path,
                f"{config_path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
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


def main(argv=None) -> int:
    import json
    argv = argv if argv is not None else sys.argv[1:]
    fix_mode = "--fix" in argv
    config_path = os.path.expanduser("~/.hermes/config.yaml")

    if fix_mode:
        res = run_doctor_fix(config_path=config_path)
    else:
        res = run_doctor()

    if "--json" in argv:
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1

    for c in res["checks"]:
        mark = "✓" if c["ok"] else ("✗" if c["fatal"] else "○")
        line = f"  {mark} {c['name']}: {c['detail']}"
        if not c["ok"] and c["fix"]:
            line += f"\n      → {c['fix']}"
        print(line)

    # Print fixes if we ran in --fix mode
    if fix_mode and res.get("fixes_applied"):
        print(f"\n  🛠 Applied fixes:")
        for fix_line in res["fixes_applied"]:
            print(f"    {fix_line}")

    # Print safety-gate refusals LOUDLY (Card D): entries we deliberately did
    # NOT convert because the endpoint did not serve the alias or was
    # unreachable. These need operator attention before re-running --fix.
    if fix_mode and res.get("alias_conversion_refused"):
        print("\n  ⚠ ALIAS MIGRATION SKIPPED (safety gate):")
        for line in res["alias_conversion_refused"]:
            print(f"    {line}")

    if not res["ok"]:
        print(f"\n  ✗ preflight FAILED: {', '.join(res['fatal_failures'])}")
        return 1
    print("\n  ✓ preflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
