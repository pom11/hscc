"""Smoke-test checks for a future `hscc verify` command.

Each check returns {"name": str, "ok": bool|None, "detail": str} and never raises.
ok=None means the check could not be verified (not a pass, not a hard fail).
"""

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

try:
    import subprocess
except ImportError:  # pragma: no cover - subprocess is stdlib, safety net only
    subprocess = None

try:
    import yaml
except ImportError:
    yaml = None

try:
    from . import serving as _serving
except Exception:  # pragma: no cover - import-time safety
    _serving = None

try:
    from . import health as _health
except Exception:  # pragma: no cover - import-time safety
    _health = None


def _repo_root():
    """Absolute path to the checked-out repo root from verify.py's location.

    verify.py always ships at ``<repo>/hscc_daemon/verify.py``, so the repo
    root is two levels up. Used to locate the existing scripts/ payload
    (api_route_sweep.py, the plugin trees to diff against) rather than
    duplicating their logic here.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _capture(cmd, timeout=None):
    """Run ``cmd`` (no shell) and return (returncode, combined output).

    Degrades to a synthetic failure instead of raising, so a check never
    crashes verify. ``cmd`` is a list — never a shell string — so no path or
    argument can be interpreted as a shell metacharacter.
    """
    if subprocess is None:
        return (2, "subprocess unavailable")
    timeout = timeout or 120
    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        out = (cp.stdout or "") + ("\n" + cp.stderr if cp.stderr else "")
        return cp.returncode, out
    except FileNotFoundError:
        return (2, f"command not found: {cmd[0]}")
    except (OSError, ValueError) as exc:
        return (2, str(exc))


def check_plugins(plugins_dir=None):
    """Check that the hscc-commands plugin dir exists and contains core commands.

    Looks for \"workers-up\", \"cluster-restart\", \"template\" strings in __init__.py.
    """
    if plugins_dir is None:
        plugins_dir = os.path.expanduser("~/.hermes/plugins")
    else:
        plugins_dir = os.path.expanduser(plugins_dir)

    plugin_path = os.path.join(plugins_dir, "hscc-commands")
    init_file = os.path.join(plugin_path, "__init__.py")

    required = ["workers-up", "cluster-restart", "template"]

    try:
        if not os.path.isdir(plugin_path):
            return {"name": "plugins", "ok": True, "detail": f"skipped: {plugin_path} not found"}
        if not os.path.isfile(init_file):
            return {"name": "plugins", "ok": True, "detail": f"skipped: {init_file} not found"}

        with open(init_file, "r") as f:
            source = f.read()

    except (OSError, IOError) as exc:
        return {"name": "plugins", "ok": True, "detail": f"skipped: {exc}"}

    missing = [s for s in required if s not in source]
    if not missing:
        return {"name": "plugins", "ok": True, "detail": "all core commands found"}
    return {"name": "plugins", "ok": False, "detail": f"missing: {', '.join(missing)}"}


def check_multiplex(gateway_state=None, config=None, profiles_dir=None):
    """Check multiplex configuration and profile coverage.

    Reads multiplex_profiles from config and served_profiles from gateway state.
    OK if multiplex is truthy, served_profiles non-empty, and covers all profile dirs.
    """
    if config is None:
        config = os.path.expanduser("~/.hermes/config.yaml")
    else:
        config = os.path.expanduser(config)

    if gateway_state is None:
        gateway_state = os.path.expanduser("~/.hermes/gateway_state.json")
    else:
        gateway_state = os.path.expanduser(gateway_state)

    if profiles_dir is None:
        profiles_dir = os.path.expanduser("~/.hermes/profiles")
    else:
        profiles_dir = os.path.expanduser(profiles_dir)

    # Load config
    try:
        if yaml is None:
            return {"name": "multiplex", "ok": True, "detail": "skipped: pyyaml not installed"}
        if not os.path.isfile(config):
            return {"name": "multiplex", "ok": True, "detail": "skipped: config not found"}
        with open(config, "r") as f:
            cfg = yaml.safe_load(f)
    except (OSError, IOError):
        return {"name": "multiplex", "ok": True, "detail": "skipped: cannot read config"}

    if cfg is None:
        return {"name": "multiplex", "ok": True, "detail": "skipped: empty config"}

    if not cfg.get("multiplex_profiles"):
        return {"name": "multiplex", "ok": True, "detail": "multiplex disabled"}

    # Load gateway state
    try:
        if not os.path.isfile(gateway_state):
            return {"name": "multiplex", "ok": None, "detail": "multiplex enabled but gateway state missing — coverage unverified"}
        with open(gateway_state, "r") as f:
            gw = json.load(f)
    except (OSError, IOError, json.JSONDecodeError):
        return {"name": "multiplex", "ok": None, "detail": "multiplex enabled but cannot read gateway state — coverage unverified"}

    served = gw.get("served_profiles", [])
    if not served:
        return {"name": "multiplex", "ok": False, "detail": "served_profiles is empty"}

    served_set = set(served)

    # Check profile dirs are covered
    try:
        if os.path.isdir(profiles_dir):
            subdirs = [
                entry.name
                for entry in os.scandir(profiles_dir)
                if entry.is_dir()
            ]
            missing = [d for d in subdirs if d not in served_set]
        else:
            missing = []
    except OSError:
        missing = []

    if missing:
        return {"name": "multiplex", "ok": False, "detail": f"profiles not served: {', '.join(missing)}"}

    return {"name": "multiplex", "ok": True, "detail": f"all {len(served)} profiles served"}


def _intentional_window_verdict():
    """Return the intentional-autodown window verdict, or None.

    Reuses ``autodown.classify()`` — the SINGLE decision table the watchdog
    (lifecycle.py:219), trigger engine (trigger.py:162) and check_workers
    resurrection guard (health.py:1024) all consult — rather than re-deriving
    the rule here. Returns one of:

    ``"expected_down"``
        The watchdog block is latched with ``intentional == "autodown"`` AND
        autodown state is confirmed ``"down"`` — the serving layer is OFF BY
        DESIGN (an operator-configured teardown).
    ``"waking"``
        The block is latched with ``intentional == "autodown"`` AND autodown
        state is ``"waking"`` — a NORMAL, expected transition: the wake is
        bringing the serving layer up, so the streams are legitimately not
        healthy yet. NOT a fault; the layer is coming back, not off.

    Returns None in every other case (no intentional block, state up/error/
    missing, or classify() raising) ⇒ verify treats ``ok is False`` exactly as
    before (a failure). This is the gate for excusing ``hscc verify`` streams
    that are unhealthy because of an intentional autodown transition. It is NOT
    sufficient on its own: a stream is excused only when it ALSO carries its own
    ``intentional == "autodown"`` marker (see check_daemon_streams), so a real
    fault in an untagged stream still fails even during the window.

    Fail-safe: any inability to read the block/config (or classify raising)
    ⇒ None ⇒ verify treats ``ok is False`` exactly as before (a failure).
    """
    try:
        from . import autodown
        from .lifecycle import load_watchdog_block
        verdict = autodown.classify(
            load_watchdog_block(), autodown.load_config())
        if verdict in ("expected_down", "waking"):
            return verdict
        return None
    except Exception:
        return None


def _intentional_window_label(verdict):
    """Human wording for an intentional-autodown window verdict, so an operator
    can tell "off on purpose" (down) from "coming back" (waking) from a real
    fault. ``expected_down`` ⇒ "intentionally down by autodown"; ``waking`` ⇒
    "waking from autodown (serving layer starting)". A ``None`` verdict (no
    window) is not labelled — callers only invoke it inside the window."""
    if verdict == "waking":
        return "waking from autodown (serving layer starting)"
    return "intentionally down by autodown"


def _live_stream_intervals():
    """Return the live daemon's per-stream check cadence (stream -> seconds).

    Reads ``daemon_ops.PERIODIC_INTERVALS`` — the single source of truth the
    daemon actually runs on. Purely defensive: if the import ever fails, return
    an empty map so staleness falls back to the flat ``max_age_s`` window
    (identical to the pre-fix behaviour, no worse). Never raises.
    """
    try:
        from .daemon_ops import PERIODIC_INTERVALS
        return dict(PERIODIC_INTERVALS)
    except Exception:
        return {}


def check_daemon_streams(state_dir=None, max_age_s=None, stream_intervals=None):
    """Check all daemon state files are healthy and recent.

    OK if every *.json has ok==True and last_check within max_age_s of now,
    EXCEPT streams that are deliberately down by an intentional autodown.

    An intentional, operator-configured teardown is not a fault: while the
    whole serving layer is down by design, the serving streams (watchdog, dgx,
    gateway, proxy, workers) truthfully report ``ok: False`` because the models
    really are stopped. A health command that reads RED during normal operation
    would train the operator to ignore it — and a scheduled check would page
    for nothing. So a stream whose data carries its own ``intentional ==
    "autodown"`` marker, AND an intentional autodown is confirmed in effect
    (``classify() == expected_down``), is treated as a NON-failing "expected"
    state that still tells the truth: the detail names the intentional autodown
    so ``hscc verify``'s human output says the cluster is intentionally down
    rather than silently claiming everything is normal.

    Genuine failures are NOT blanket-suppressed: any ``ok is False`` stream
    WITHOUT the intentional marker (or outside a confirmed intentional window)
    still fails — the intended negative control.

    Staleness is per-stream, keyed off the stream's OWN cadence
    (``daemon_ops.PERIODIC_INTERVALS`` — the live loop's source of truth), not
    one flat window. A stream that only checks every 900s (nas) must not be
    flagged stale by a 600s flat window — that was the "cries wolf on a healthy
    cluster" bug (verify.py:206 max_age_s=600 < daemon_ops.py nas:900). Each
    stream's limit is ``max(max_age_s, 2*interval + JITTER)``, so slow streams
    get room for real jitter while fast streams stay tight and any genuinely
    dead stream is still caught within ~2 missed ticks. ``stream_intervals``
    is injectable for tests; defaulting to the live cadence.
    """
    if state_dir is None:
        state_dir = os.path.expanduser("~/.hscc/state")
    else:
        state_dir = os.path.expanduser(state_dir)

    if max_age_s is None:
        max_age_s = 600
    if stream_intervals is None:
        stream_intervals = _live_stream_intervals()

    # How long each stream may stay silent before it is "stale": at least the
    # caller's flat window, but never less than ~2 full check cycles — so a
    # healthy slow stream (nas: 900s) is never flagged stale between ticks,
    # while a genuinely dead one (more than ~2 missed ticks) still is.
    _JITTER_S = 90
    _EXTRA_CYCLES = 2

    def _limit_for(stream):
        interval = stream_intervals.get(stream)
        if not interval:
            return max_age_s
        return max(max_age_s, _EXTRA_CYCLES * interval + _JITTER_S)

    try:
        if not os.path.isdir(state_dir):
            return {"name": "daemon_streams", "ok": True, "detail": "skipped: state dir not found"}
        entries = os.listdir(state_dir)
    except OSError as exc:
        return {"name": "daemon_streams", "ok": True, "detail": f"skipped: {exc}"}

    json_files = [e for e in entries if e.endswith(".json")]
    if not json_files:
        return {"name": "daemon_streams", "ok": True, "detail": "no state files found"}

    now = datetime.now(timezone.utc)
    issues = []
    expected = []
    window_verdict = _intentional_window_verdict()
    window_label = (_intentional_window_label(window_verdict)
                    if window_verdict else None)

    for fn in sorted(json_files):
        filepath = os.path.join(state_dir, fn)
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
        except (OSError, IOError, json.JSONDecodeError):
            issues.append(f"{fn}: unreadable")
            continue

        ok = data.get("ok")
        if ok is False:
            # A stream is excused ONLY when an intentional autodown window is
            # in effect (down by design or waking) AND the stream itself says
            # it is unhealthy because of the intentional transition. Either
            # condition missing ⇒ genuine failure.
            if (window_verdict and data.get("intentional") == "autodown"):
                reason = data.get("message") or data.get("reason") or "autodown"
                expected.append(f"{fn}: {window_label} ({reason})")
                # fall through to the recency check below — stale is still stale
            else:
                issues.append(f"{fn}: ok=False")

        # Check recency via last_check or timestamp (applies to ALL streams,
        # intentional or not — an intentionally-down stream still must publish
        # fresh state every tick; silence there is a real fault).
        ts_str = data.get("last_check") or data.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if now - ts > timedelta(seconds=_limit_for(fn[:-5])):
                    issues.append(f"{fn}: stale ({ts_str})")
            except (ValueError, TypeError):
                issues.append(f"{fn}: unparseable timestamp ({ts_str})")

    expected_note = (
        f"{window_label}: {'; '.join(expected)}"
        if expected else ""
    )
    if issues:
        detail = "; ".join(issues)
        if expected_note:
            detail = f"{expected_note}; REAL FAILURES: {detail}"
        return {"name": "daemon_streams", "ok": False, "detail": detail}

    if expected_note:
        return {
            "name": "daemon_streams", "ok": True,
            "detail": f"ok={len(json_files) - len(expected)} healthy, {expected_note}",
        }

    return {"name": "daemon_streams", "ok": True, "detail": f"all {len(json_files)} streams healthy"}


def check_proxy(url=None, timeout=None):
    """Check that the local proxy responds with a non-empty model list.

    GETs the URL; OK if HTTP 200 and body has a non-empty 'data' list.

    NON-failing during an intentional autodown: when the whole serving layer
    is in an intentional window — either torn down by operator config
    (``classify() == expected_down``) or waking (``classify() == "waking"``,
    models still loading) — the LiteLLM proxy is part of the (down / coming
    up) serving layer and truthfully lists no models yet; that is the expected
    state, not a fault. So, mirroring ``check_daemon_streams``
    (verify.py:198-252), the result is then a NON-failing report that names
    the window, rather than a RED "no models" that would train operators to
    ignore it during a normal power-save or wake.

    Genuine proxy failures are NOT blanket-suppressed: the gate key is the
    SINGLE intentional decision table ``autodown.classify()`` restricted to the
    intentional window (``expected_down``/``waking``, verify.py:126). With no
    intentional block — or while the layer should be up (``should_be_up``) —
    the proxy check behaves exactly as before and a
    broken/unreachable/proxy-with-no-models still fails. We cannot distinguish
    "no models because the fleet is intentionally down/waking" from "proxy is
    genuinely broken" inside that window (they are indistinguishable at the
    HTTP layer, and the proxy is SUPPOSED to be down), so the safe reading
    while in the intentional window is to not report a fault; any real proxy
    problem surfaces as soon as the wake completes.
    """
    if url is None:
        url = "http://localhost:4000/v1/models"
    if timeout is None:
        timeout = 4

    window_verdict = _intentional_window_verdict()
    if window_verdict:
        window_label = _intentional_window_label(window_verdict)
        return {"name": "proxy", "ok": True,
                "detail": f"{window_label}: serving layer not serving "
                          f"models, none expected"}

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return {"name": "proxy", "ok": False, "detail": f"HTTP {resp.status}"}
            body = json.loads(resp.read().decode())
            data = body.get("data", [])
            if not data:
                return {"name": "proxy", "ok": False, "detail": "no models in data list"}
            return {"name": "proxy", "ok": True, "detail": f"{len(data)} models available"}
    except urllib.error.HTTPError as exc:
        return {"name": "proxy", "ok": False, "detail": f"HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"name": "proxy", "ok": False, "detail": f"connection error: {exc.reason}"}
    except json.JSONDecodeError:
        return {"name": "proxy", "ok": False, "detail": "response is not valid JSON"}
    except Exception as exc:
        return {"name": "proxy", "ok": False, "detail": str(exc)}


def check_config_wiring(config=None):
    """Check config.yaml has required HSCC wiring.

    Requires: multiplex_profiles truthy, kanban.max_in_progress is int,
    toolsets contains 'hscc-cluster'.
    """
    if config is None:
        config = os.path.expanduser("~/.hermes/config.yaml")
    else:
        config = os.path.expanduser(config)

    try:
        if yaml is None:
            return {"name": "config_wiring", "ok": True, "detail": "skipped: pyyaml not installed"}
        if not os.path.isfile(config):
            return {"name": "config_wiring", "ok": True, "detail": "skipped: config not found"}
        with open(config, "r") as f:
            cfg = yaml.safe_load(f)
    except (OSError, IOError):
        return {"name": "config_wiring", "ok": True, "detail": "skipped: cannot read config"}

    if cfg is None:
        return {"name": "config_wiring", "ok": True, "detail": "skipped: empty config"}

    missing = []

    if not cfg.get("multiplex_profiles"):
        missing.append("multiplex_profiles")

    kanban = cfg.get("kanban", {})
    if not isinstance(kanban, dict) or not isinstance(kanban.get("max_in_progress"), int):
        missing.append("kanban.max_in_progress")

    # toolsets can be a list or a JSON-encoded string
    toolsets = cfg.get("toolsets", [])
    if isinstance(toolsets, str):
        try:
            toolsets = json.loads(toolsets)
        except (json.JSONDecodeError, TypeError):
            toolsets = []
    if isinstance(toolsets, list) and "hscc-cluster" not in toolsets:
        missing.append("toolsets: hscc-cluster")

    if missing:
        return {"name": "config_wiring", "ok": False, "detail": f"missing: {', '.join(missing)}"}

    return {"name": "config_wiring", "ok": True, "detail": "all wiring checks passed"}


def _iter_keys(node, key="base_url", _path=""):
    """Yield ``(key_path, value)`` for every value stored under ``key``.

    Walks nested dicts and lists so a base_url is found whether it lives under
    ``model``, an ``auxiliary`` subsection (e.g. ``auxiliary.compression``),
    a ``compact``/``strong`` block, a fallback chain, or any other placement.
    ``key_path`` is a dotted path (e.g. ``model.base_url``) so a finding can
    name exactly which key in a profile is wrong.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            kp = ("%s.%s" % (_path, k)) if _path else str(k)
            if k == key:
                yield kp, v
            else:
                yield from _iter_keys(v, key, kp)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_keys(item, key, _path)


def _origin(url):
    """``(scheme, host, port)`` for a url, or None if unparseable.

    Path is ignored, so a profile's trailing ``/v1`` never matters — the
    origin (scheme+host+port) is what must match a served endpoint. Port is
    defaulted to the scheme's well-known port when absent.
    """
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme.lower() == "https" else 80
        return (parsed.scheme.lower(), parsed.hostname.lower(), port)
    except (ValueError, AttributeError, TypeError):
        return None


def _collect_profile_urls(profiles_dir):
    """Map ``{profile_name: [base_url, ...]}`` from every config.yaml.

    Returns None when the profiles dir is missing/unreadable (caller reports
    skipped). Profiles without a config.yaml or with no base_urls contribute
    nothing. Unreadable YAML is skipped rather than flagged — the point of the
    check is to catch a WRONG endpoint, not to police config syntax.
    """
    if yaml is None:
        return {}
    if not os.path.isdir(profiles_dir):
        return None
    try:
        entries = list(os.scandir(profiles_dir))
    except OSError:
        return None
    out = {}
    for entry in entries:
        if not entry.is_dir():
            continue
        cfg = os.path.join(entry.path, "config.yaml")
        if not os.path.isfile(cfg):
            continue
        try:
            data = yaml.safe_load(open(cfg))
        except Exception:
            continue
        items = [(kp, u.strip()) for kp, u in _iter_keys(data, "base_url")
                 if isinstance(u, str) and u.strip()]
        if items:
            out[entry.name] = items
    return out


def check_profile_endpoints(serving_path=None, profiles_dir=None,
                            proxy_base=None, loopback_hosts=None):
    """Check every profile base_url points at an endpoint the fleet actually serves.

    A profile's ``base_url`` must resolve to an origin (scheme+host+port) the
    cluster actually serves:
      * the **orchestrator endpoint**, derived from ``~/.hscc/serving.json``
        (nodes[0] of the first orchestrator unit + the serving port →
        ``http://<head>:<port>[/v1]``);
      * the **worker proxy** (the LiteLLM proxy, default
        ``http://localhost:4000``);
      * an explicitly allow-listed **loopback** host for local tooling
        (defaults ``localhost``, ``127.0.0.1``, ``::1`` — any port).

    Any other base_url is a red flag — most commonly a stale pointer to a host
    the cluster no longer serves (e.g. every orchestrator profile left pointing
    at a dead ``10.0.0.x`` placeholder after the orchestrator moved). This is
    the guard that would have caught that whole-class regression before it
    silently broke every orchestrator-routed profile.

    Each finding reports the profile name, the dotted key path, and the value
    (e.g. ``orch-01: model.base_url http://10.99.99.99:8000/v1 not in serving
    endpoints``). Comparison is by origin, so trailing ``/v1`` or any other
    path on a profile's base_url is ignored. Never raises. ``ok`` is None
    (unverified, not pass/fail) when the orchestrator endpoint cannot be
    derived because serving.json is missing/unparseable or has no orchestrator
    unit.
    """
    if _serving is None:
        return {"name": "profile_endpoints", "ok": None,
                "detail": "unverified: serving module unavailable"}

    if serving_path is None:
        serving_path = _serving.SERVING_JSON
    else:
        serving_path = os.path.expanduser(serving_path)

    if profiles_dir is None:
        profiles_dir = _serving.PROFILES_DIR
    else:
        profiles_dir = os.path.expanduser(profiles_dir)

    proxy_base = proxy_base or "http://localhost:4000"
    loopback_hosts = loopback_hosts or {"localhost", "127.0.0.1", "::1"}

    serving_data = _serving.load_serving(serving_path)
    if serving_data is None:
        return {"name": "profile_endpoints", "ok": None,
                "detail": "unverified: serving.json missing/unparseable — "
                          "cannot derive orchestrator endpoint"}

    orch_ep = _serving.orchestrator_endpoint(serving_data)
    if not orch_ep:
        return {"name": "profile_endpoints", "ok": None,
                "detail": "unverified: no orchestrator unit in serving.json"}

    allowed = {_origin(orch_ep), _origin(proxy_base)}

    by_profile = _collect_profile_urls(profiles_dir)
    if by_profile is None:
        return {"name": "profile_endpoints", "ok": True,
                "detail": "skipped: profiles dir not found"}

    issues = []
    checked = 0
    for prof, items in sorted(by_profile.items()):
        for key_path, url in items:
            checked += 1
            origin = _origin(url)
            if origin is None:
                issues.append(f"{prof}: {key_path} unparseable base_url {url!r}")
            elif origin in allowed or origin[1] in loopback_hosts:
                continue
            else:
                issues.append(
                    f"{prof}: {key_path} {url} not in serving endpoints")

    if issues:
        return {"name": "profile_endpoints", "ok": False,
                "detail": f"{len(issues)} offending url(s): " + "; ".join(issues)}

    return {"name": "profile_endpoints", "ok": True,
            "detail": f"all {checked} base_urls across {len(by_profile)} "
                      f"profiles served by cluster endpoints"}


def run_chat_roundtrip():
    """Deep, OPT-IN check: prove a chat message reaches the model and returns.

    Delegates to ``scripts/verify_chat_roundtrip.py`` — the single operator-grade
    end-to-end proof (POST /v1/orchestrator/chat → poll to a terminal state →
    assert a real reply → assert the orchestrator unit's
    ``vllm:generation_tokens_total`` moved). That distinguishes "the API
    accepted the message" from "the model actually answered" — the gap that hid
    the outage where every orchestrator profile pointed at a dead host while the
    cluster looked idle.

    This is OPT-IN because it fires a real prompt at the live orchestrator and
    can take up to the chat timeout (~600 s) to complete — far too slow and too
    invasive for the default fast smoke test. It is invoked by ``hscc verify
    --chat``. Never raises. Returns the standard ``{name, ok, detail}`` shape,
    with ``ok`` True only when the full round trip (including the model-token
    movement) succeeds.
    """
    import subprocess
    import sys as _sys

    # The script lives in scripts/ in the repo, but INSTALLED the package sits
    # directly under ~/.hermes/plugins/, where "../scripts" does not exist —
    # the deploy step copies the script in beside this module instead. Check
    # both, or the check works from a checkout and fails everywhere real.
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "verify_chat_roundtrip.py"),          # installed
        os.path.join(os.path.dirname(here), "scripts",
                     "verify_chat_roundtrip.py"),                # repo
    )
    script = next((c for c in candidates if os.path.exists(c)), candidates[-1])
    try:
        proc = subprocess.run(
            [_sys.executable, script, "--json"],
            capture_output=True, text=True, timeout=700)
    except subprocess.TimeoutExpired:
        return {"name": "chat_roundtrip", "ok": False,
                "detail": "chat round trip exceeded 700s budget"}
    if proc.returncode != 0:
        try:
            detail = json.loads(proc.stdout).get("error", "round trip failed")
        except (json.JSONDecodeError, AttributeError):
            detail = (proc.stdout or proc.stderr).strip() or "round trip failed"
        return {"name": "chat_roundtrip", "ok": False, "detail": detail}
    try:
        data = json.loads(proc.stdout)
        return {
            "name": "chat_roundtrip", "ok": True,
            "detail": "job %s replied %r, orch generation_tokens %s->%s (+%s), %.1fs"
                      % (data.get("job_id"), data.get("reply"),
                         data.get("tokens_before"), data.get("tokens_after"),
                         data.get("delta"), data.get("elapsed")),
        }
    except (json.JSONDecodeError, TypeError):
        return {"name": "chat_roundtrip", "ok": True,
                "detail": "round trip succeeded (machine output unparseable)"}
def check_api_routes(script=None, python=None, timeout=None):
    """Prove the API is up and every route the app calls actually answers.

    Reuses ``scripts/api_route_sweep.py`` — the existing sweep that discovered
    the 2026-09-01 dead-chat regression — by shelling out to it. No route
    discovery is duplicated here; the sweep script owns the iOS-client route
    list and the read-only GET sweep. ``verify`` just runs it and interprets
    the exit code:

      * exit 0 → API up, every swept route answered with parseable JSON.
      * exit 1 → some route the app calls did not answer (API reachable but a
        screen would be dead).
      * exit 2 → the sweep could not run at all (no API host/token) — the API
        is not up, which is a real failure for "is the app going to work?".

    ``ok`` is None (unverified, not pass/fail) only when the sweep script
    itself is not found, i.e. verify is not running from a checked-out repo
    tree.
    """
    if script is None:
        script = os.path.join(_repo_root(), "scripts", "api_route_sweep.py")
    if not os.path.isfile(script):
        return {"name": "api_routes", "ok": None,
                "detail": "unverified: scripts/api_route_sweep.py not found "
                          f"({script})",
                "next_step": "run hscc verify from a checked-out repo tree"}

    py = python or os.environ.get("HSCC_TEST_PY") or "python3"
    rc, out = _capture([py, script, "--json"], timeout=timeout)
    if rc == 0:
        return {"name": "api_routes", "ok": True,
                "detail": "API is up; every route the app calls answers"}
    if rc == 2:
        # api_route_sweep returns 2 when it has no API host or token — i.e. the
        # API is not actually up. This is a genuine "would the app work?" fail.
        return {"name": "api_routes", "ok": False,
                "detail": "API not reachable: " + _first_line(out),
                "next_step": "start the API (hscc api start / the daemon) and "
                             "confirm `hscc api status` reports Listening"}

    # rc == 1 → some swept route did not answer. The sweep's --json output names
    # them; surface the failing routes (and their HTTP status) as the cause.
    failed = _sweep_failures(out)
    detail = "; ".join(failed) if failed else _first_line(out)
    return {"name": "api_routes", "ok": False,
            "detail": "route(s) the app calls did not answer: " + detail,
            "next_step": "open the failing route(s) above; that screen would be "
                         "dead for the operator"}


def check_chat_roundtrip(serving_path=None, probe=None, timeout=None,
                         max_tokens=None):
    """Prove a real chat round-trip reaches a served model and returns text.

    This is NOT an HTTP-reachability handshake. It POSTs a tiny STREAMING
    chat-completions request ("Reply with the single word: ok.") to every
    serving.json unit and requires REAL generated text back within a short
    window — the same probe the daemon's engine-wedge monitor runs
    (``health._probe_unit_generation``), reused so there is one definition of
    "the model actually answered."

    Excused (non-failing, named) during an intentional autodown when the
    serving layer is down by design — mirrors ``check_proxy``. ``ok`` is None
    (unverified) only when serving.json is missing/unparseable or the probe
    module is unavailable.
    """
    if probe is None:
        if _health is None or not hasattr(_health, "_probe_unit_generation"):
            return {"name": "chat_roundtrip", "ok": None,
                    "detail": "unverified: chat probe unavailable"}
        probe = _health._probe_unit_generation
    if timeout is None:
        timeout = getattr(_health, "ENGINE_WEDGE_TIMEOUT", 10) if _health else 10

    if serving_path is None:
        if _serving is None:
            return {"name": "chat_roundtrip", "ok": None,
                    "detail": "unverified: serving module unavailable"}
        serving_path = _serving.SERVING_JSON
    else:
        serving_path = os.path.expanduser(serving_path)

    # Intentional autodown ⇒ serving layer down by design; nothing to chat
    # with yet, and that is expected. Mirror check_proxy's excuse.
    window_verdict = _intentional_window_verdict()
    if window_verdict:
        return {"name": "chat_roundtrip", "ok": True,
                "detail": f"{_intentional_window_label(window_verdict)}: "
                          f"serving layer not serving, chat not expected"}

    serving_data = None
    if _serving is not None:
        serving_data = _serving.load_serving(serving_path)
    if not serving_data:
        return {"name": "chat_roundtrip", "ok": None,
                "detail": "unverified: serving.json missing/unparseable — "
                          "cannot reach a model",
                "next_step": "install/write ~/.hscc/serving.json, then re-run"}

    units = [u for u in (serving_data.get("units") or [])
             if (u.get("nodes") or [])]
    if not units:
        return {"name": "chat_roundtrip", "ok": None,
                "detail": "unverified: no units in serving.json to probe"}

    failures = []
    checked = 0
    for u in units:
        node = u.get("nodes")[0]
        try:
            port = int(u.get("port"))
        except (TypeError, ValueError):
            port = None
        if not port:
            # Zero or absent unit port means nothing is affirmed to be serving
            # here; do not guess a port and do not hardcode one.
            failures.append(f"{node}: no serving port declared")
            continue
        checked += 1
        try:
            res = probe(node, port, timeout=timeout, max_tokens=max_tokens)
        except Exception as exc:
            res = {"ok": False, "error": str(exc), "status": None}
        if not res.get("ok"):
            why = res.get("error") or res.get("status") or "no text returned"
            failures.append(f"model on {node}:{port} did not answer ({why})")

    if failures:
        return {"name": "chat_roundtrip", "ok": False,
                "detail": "; ".join(failures),
                "next_step": "that model is not generating — check the vLLM/"
                             "sparkrun serving unit and the worker proxy"}
    return {"name": "chat_roundtrip", "ok": True,
            "detail": f"real chat round-trip returned text from {checked} "
                      f"serving unit(s)"}


# Base-name payload files (non-test, non-cache) that constitute a deployed
# plugin's payload. Subdirectories are walked recursively for the same kinds of
# source/docs files, so nested payloads (roles/, templates/, hooks/, skills/)
# are compared too. Test and cache artifacts are NOT part of the deployed
# payload and are excluded.
_PAYLOAD_EXCLUDE = {
    "tests", "__pycache__", ".pytest_cache", "test_", "tests_",
}
_PAYLOAD_EXT = (".py", ".yaml", ".yml", ".json", ".md", ".sh", ".toml", ".txt")


def _is_payload_file(rel):
    """True for a file that is part of a deployed plugin payload.

    Excludes test dirs, python caches, hidden files, binary assets and files
    without a source/docs extension — the things installation does NOT ship.
    """
    parts = rel.split("/")
    if any(p in _PAYLOAD_EXCLUDE for p in parts):
        return False
    if any(p.startswith("test_") or p.startswith("tests_") for p in parts[:-1]):
        return False
    leaf = parts[-1]
    if leaf.startswith(".") or leaf.endswith((".pyc", ".pyo")):
        return False
    return leaf.endswith(_PAYLOAD_EXT)


def _payload_snapshot(root):
    """Map ``{relative_path: sha256}`` for a dir's payload files.

    Content-addressed, so two trees with identical file bytes compare equal
    regardless of mtime or inode — the honest test of "the installed payload
    is what the repo ships." Compares by relative path on both sides.
    ``root`` may be None → empty snapshot (caller reports skipped).
    """
    if not root or not os.path.isdir(root):
        return {}
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _PAYLOAD_EXCLUDE
                       and not d.startswith((".", "test_"))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if not _is_payload_file(rel):
                continue
            try:
                out[rel] = _sha256(full)
            except OSError:
                continue
    return out


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_plugin_payload(repo_root=None, plugins_dir=None, names=None,
                         snapshot=None):
    """Check installed plugin payloads match what the repo deploys.

    "Merging is not deploying": a plugin merged/committed in the repo is only
    a real fix once it is installed. This diffs each repo plugin directory
    against its installed counterpart under ``~/.hermes/plugins`` for the
    payload files installation actually ships (source/docs files recursively,
    excluding tests/caches). A repo plugin with no installed directory is
    flagged (merged but never deployed); a payload file whose CONTENT differs
    from the repo counterpart (sha256 keyed by relative path) is flagged for
    that plugin.

    Reuses no duplicated logic — ``_payload_snapshot`` walks the same single
    source tree the repo publishes, and the comparison is mechanical. ``names``
    defaults to the repo plugin dirs that also exist under ``plugins_dir``.
    ``snapshot`` (root -> {...}) is injectable for tests.
    """
    if repo_root is None:
        repo_root = _repo_root()
    if plugins_dir is None:
        plugins_dir = os.path.expanduser("~/.hermes/plugins")

    # Default scope: repo top-level dirs whose name looks like a plugin and
    # that are present in the repo.
    if names is None:
        try:
            repo_dirs = sorted(
                d for d in os.listdir(repo_root)
                if os.path.isdir(os.path.join(repo_root, d))
                and d.startswith("hscc-")
            )
        except OSError:
            return {"name": "plugin_payload", "ok": None,
                    "detail": "unverified: cannot list repo root",
                    "next_step": "run hscc verify from a checked-out repo tree"}
    else:
        repo_dirs = list(names)

    if not repo_dirs:
        return {"name": "plugin_payload", "ok": None,
                "detail": "unverified: no plugin dirs found in repo root"}

    issues = []
    checked = 0
    for d in repo_dirs:
        repo_dir = os.path.join(repo_root, d)
        inst_dir = os.path.join(plugins_dir, d)
        inst = snapshot(inst_dir) if snapshot else _payload_snapshot(inst_dir)
        if not inst:
            issues.append(
                f"{d}: installed at {inst_dir} missing or has no payload — "
                f"merged but not deployed")
            continue
        repo = _payload_snapshot(repo_dir)
        checked += len(repo)
        missing = sorted(set(repo) - set(inst))
        changed = sorted(r for r in repo if r in inst and repo[r] != inst[r])
        if missing:
            issues.append(f"{d}: repo files not installed: {', '.join(missing)}")
        if changed:
            issues.append(
                f"{d}: installed payload differs from repo: {', '.join(changed)}")

    if issues:
        return {"name": "plugin_payload", "ok": False,
                "detail": "; ".join(issues),
                "next_step": "run hscc-bootstrap/bootstrap.sh (or the plugin's "
                             "install step) to deploy the repo state to "
                             "~/.hermes/plugins, then re-run"}
    return {"name": "plugin_payload", "ok": True,
            "detail": f"installed payload matches the repo across {len(repo_dirs)} "
                      f"plugins ({checked} payload files)"}


def _first_line(text):
    """First non-empty line of a multi-line string, trimmed; else a fallback."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def _sweep_failures(out):
    """Extract ``["route (HTTP status)", ...]`` from the sweep's --json output.

    Returns [] when the output is not the sweep's JSON (e.g. a sweep run from
    a version that predates --json, or an interpreter-level error), so the
    caller falls back to the raw first line.
    """
    try:
        parsed = json.loads(out)
        rows = parsed.get("routes") or []
    except (ValueError, AttributeError, TypeError):
        return []
    return [f"{r.get('route')} ({r.get('status')})"
            for r in rows if not r.get("ok")]


def run_all(**overrides):
    """Run all checks, returning aggregated results.

    Accepts keyword overrides that match the parameter names of each check
    function. Returns {\"checks\": [...], \"ok\": bool}.
    """
    checks = [
        check_plugins,
        check_multiplex,
        check_daemon_streams,
        check_proxy,
        check_config_wiring,
        check_profile_endpoints,
        check_api_routes,
        check_chat_roundtrip,
        check_plugin_payload,
    ]

    results = []
    for check_fn in checks:
        import inspect
        sig = inspect.signature(check_fn)
        params = set(sig.parameters.keys())
        kwargs = {k: v for k, v in overrides.items() if k in params}
        results.append(check_fn(**kwargs))

    return {
        "checks": results,
        "ok": all(r["ok"] for r in results),
    }
