"""HSCC preflight doctor (D13).

Checks every prerequisite a fresh HSCC install needs and explains failures in
plain language with a one-line fix. Used by bootstrap.sh (Stage 1) and runnable
standalone: `python3 doctor.py`.

Each check returns a Check(name, ok, detail, fix, fatal). `run_doctor()` returns
a summary dict; `main()` prints a ✓/✗ checklist and exits non-zero if any FATAL
check failed (so bootstrap can hard-stop).
"""

from __future__ import annotations

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

    Sends ``Authorization: Bearer <api_key>`` when a key is provided (the
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


def run_doctor_fix(config_path: Optional[str] = None,
                   hermes_home: Optional[str] = None,
                   *, _cluster_runner=None) -> dict:
    """Run doctor + fix all non-fatal HSCC config drift.

    Reads the current config, runs checks, then calls enable_plugins.enable()
    if there are non-fatal failures. Reports what was wrong and what was fixed.

    Returns the same dict as run_doctor() plus "fixes_applied" list describing
    each corrected key.
    """
    checks_result = run_doctor(hermes_home, _cluster_runner=_cluster_runner)
    fixes_applied: list[str] = []

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

    return {**checks_result, "fixes_applied": fixes_applied}


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

    if not res["ok"]:
        print(f"\n  ✗ preflight FAILED: {', '.join(res['fatal_failures'])}")
        return 1
    print("\n  ✓ preflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
