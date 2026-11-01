"""Smoke-test checks for a future `hscc verify` command.

Each check returns {"name": str, "ok": bool|None, "detail": str} and never raises.
ok=None means the check could not be verified (not a pass, not a hard fail).
"""

import json
import os
import re
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

try:
    from . import sparkrun_bin as _sparkrun_bin
except Exception:  # pragma: no cover - import-time safety
    _sparkrun_bin = None


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


# ---------------------------------------------------------------------------
# GAP-F-8 — deployed cluster-tool assertion (the plugins row's second leg)
# ---------------------------------------------------------------------------
# The plugins row used to assert only that hscc-commands/__init__.py mentions
# three strings, so it printed ✓ while the DEPLOYED hscc-cluster payload was
# missing whole tools (live: orch_message.py absent from the 2.14.0 payload
# while main registers it — audit F GAP-F-8 == audit A GAP-A-5). "A check
# that asserts nothing is not green" (GAP-F-2 principle, made cross-cutting):
# the row now unions the repo-side tool registry (hscc-cluster/__init__.py
# `_*_TOOLS` tables + `from . import <mod>` handler modules) with the repo
# plugin.yaml `provides_tools` manifest — t_19dbb9de has not landed, so the
# manifest alone would inherit its GAP-A-7 under-declaration hole — and
# requires each declared tool AND its handler module to exist in the
# DEPLOYED tree. A missing tool is named by name (and its module by file
# name) under the explicit category "missing vs deployed payload": that is
# how the KNOWN-pending-release gap (GAP-A-5, closes with the 2.15.0
# bootstrap) stays distinguishable from every other failure class instead
# of hiding under a ✓. Read-only: parses files, writes nothing.

_CLUSTER_TOOLS_CATEGORY = "cluster tools missing vs deployed payload"
# A deployed __init__.py without register() is not a registry at all — the
# payload sha-diff (check_plugin_payload) owns broken-file content; this row
# stays unassertive rather than inventing a verdict from garbage.
_REGISTER_MARKER = "def register("


def _registered_tool_names(init_text):
    """Tool names from `_…_TOOLS = [ ("name", …), … ]` tables in a plugin
    __init__.py (regex over text — same no-exec safety the file's other
    parses use). One capture per tuple head: `(` + string literal."""
    tools = set()
    for block in re.findall(r"^\w*_TOOLS\s*=\s*\[.*?^\]", init_text,
                            re.MULTILINE | re.DOTALL):
        tools.update(re.findall(r'^\s*\(\s*"([^"]+)"', block, re.MULTILINE))
    return tools


def _imported_handler_modules(init_text):
    """Sibling-module handler files a plugin __init__.py requires: the
    `from . import mod [as alias]` lines that make register() importable at
    all, resolved to real module names (aliases mapped back)."""
    mods, aliases = set(), {}
    for name, alias in re.findall(
            r"^from \. import (\w+) as (\w+)", init_text, re.MULTILINE):
        aliases[alias] = name
    for name in re.findall(r"^from \. import (\w+)", init_text, re.MULTILINE):
        mods.add(name)
    return {aliases.get(m, m) for m in mods}


def _manifest_tools(plugin_dir):
    """`provides_tools` names from a plugin.yaml, via yaml when importable
    else a line scan (verify must not gain a hard PyYAML dependency)."""
    path = os.path.join(plugin_dir, "plugin.yaml")
    try:
        with open(path, "r") as f:
            text = f.read()
    except (OSError, IOError):
        return set()
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                declared = data.get("provides_tools") or []
                if isinstance(declared, list):
                    return {str(t) for t in declared if t}
        except Exception:
            pass  # fall through to the line scan
    names, inside = set(), False
    for line in text.splitlines():
        if line.startswith("provides_tools:"):
            inside = True
            continue
        if inside:
            m = re.match(r"^\s+-\s*([\w.\-]+)\s*$", line)
            if m:
                names.add(m.group(1))
            elif line.strip() and not line.startswith((" ", "\t")):
                inside = False
    return names


def _cluster_tools_verdict(plugins_dir, repo_root):
    """Check repo-declared hscc-cluster tools against the DEPLOYED tree.

    Returns (ok, extra_detail, next_step) where ok is True (asserted clean),
    False (drift — `extra_detail` names every missing tool/file), or None
    (UNASSERTIVE — `extra_detail` says why). Never raises, never writes.
    """
    deployed_dir = os.path.join(plugins_dir, "hscc-cluster")
    repo_dir = os.path.join(repo_root, "hscc-cluster") if repo_root else None

    def unassertive(why):
        return None, f"cluster tools not asserted ({why})", None

    if not os.path.isdir(deployed_dir):
        return unassertive("hscc-cluster not deployed")
    if not repo_dir or not os.path.isfile(os.path.join(repo_dir, "__init__.py")):
        return unassertive("no repo hscc-cluster/__init__.py to assert against")
    try:
        same_tree = os.path.realpath(repo_dir) == os.path.realpath(deployed_dir)
    except OSError:
        same_tree = False
    if same_tree:
        # repo root IS the deployed tree (in-place layout): comparing a tree
        # with itself is vacuous — a ✓ here would be a fabricated assertion.
        return unassertive("repo tree is the deployed tree — in-place layout")

    try:
        with open(os.path.join(repo_dir, "__init__.py"), "r") as f:
            repo_init = f.read()
        with open(os.path.join(deployed_dir, "__init__.py"), "r") as f:
            dep_init = f.read()
    except (OSError, IOError) as exc:
        return unassertive(f"unreadable __init__.py: {exc}")
    if _REGISTER_MARKER not in repo_init or _REGISTER_MARKER not in dep_init:
        return unassertive("an __init__.py lacks register() — not a registry")

    expected_tools = (_registered_tool_names(repo_init)
                      | _manifest_tools(repo_dir))
    if not expected_tools:
        return unassertive("repo side declares no tools")
    # Files the deployed payload must carry for the repo-side registry to be
    # deployable at all: every handler module the repo __init__ imports.
    expected_files = {m + ".py" for m in _imported_handler_modules(repo_init)}

    deployed_tools = (_registered_tool_names(dep_init)
                      | _manifest_tools(deployed_dir))
    missing_tools = sorted(expected_tools - deployed_tools)
    missing_files = sorted(f for f in expected_files
                           if not os.path.isfile(os.path.join(deployed_dir, f)))
    # A tool whose module file is absent counts even if the deployed manifest
    # name-matched it — the file IS the tool.
    missing_files = sorted(set(missing_files))
    if not missing_tools and not missing_files:
        try:
            with open(os.path.join(plugins_dir, "VERSION")) as f:
                ver = f.read().strip()
        except (OSError, IOError):
            ver = ""
        ver_note = f" (deployed payload {ver})" if ver else ""
        return (True, f"cluster tools asserted against deployed payload: "
                      f"{len(expected_tools)} tools + {len(expected_files)} "
                      f"modules all present{ver_note}", None)

    named = [f for f in missing_files] + [t for t in missing_tools]
    try:
        with open(os.path.join(plugins_dir, "VERSION")) as f:
            ver = f.read().strip()
        ver_note = f" (deployed payload VERSION {ver})" if ver else ""
    except (OSError, IOError):
        ver_note = ""
    detail = (f"{_CLUSTER_TOOLS_CATEGORY}: {', '.join(named)}{ver_note} — "
              f"declared by repo {repo_dir} (__init__.py registry"
              f"{' + plugin.yaml' if _manifest_tools(repo_dir) else ''})")
    next_step = ("deploy the repo payload: hscc-bootstrap/bootstrap.sh (or "
                 "install_payload.py) then re-run verify. If this is a "
                 "pending-release gap (deployed payload predates the repo "
                 "tools — GAP-A-5), it closes with the next release bootstrap.")
    return False, detail, next_step


def check_plugins(plugins_dir=None, repo_root=None):
    """Check that the hscc-commands plugin dir exists and contains core commands.

    Looks for \"workers-up\", \"cluster-restart\", \"template\" strings in __init__.py.

    GAP-F-8 second leg: also asserts the DEPLOYED hscc-cluster payload
    actually carries every tool the repo-side registry/manifest declares
    (see _cluster_tools_verdict). The core-commands verdict strings are
    preserved verbatim for existing consumers; the cluster leg can only ADD
    signal — every unassertive case keeps the row's legacy shape. ``repo_root``
    defaults to verify.py's own tree (same convention as check_plugin_payload).
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
    core_ok = not missing
    detail = ("all core commands found" if core_ok
              else f"missing: {', '.join(missing)}")

    try:
        c_ok, c_extra, c_next = _cluster_tools_verdict(
            plugins_dir, repo_root or _repo_root())
    except Exception as exc:  # pragma: no cover - defense: never abort the row
        c_ok, c_extra, c_next = None, f"cluster tools not asserted ({exc})", None
    result = {"name": "plugins", "ok": core_ok and c_ok is not False,
              "detail": f"{detail}; {c_extra}"}
    if c_ok is False:
        result["next_step"] = c_next
    return result


def check_multiplex(gateway_state=None, config=None, profiles_dir=None):
    """Check multiplex configuration and profile coverage.

    Reads multiplex_profiles from config and served_profiles from gateway state.
    OK if multiplex is truthy, served_profiles is non-empty, and it covers every
    REAL profile under ``profiles_dir`` — as decided by the shared
    ``profile_roster`` gate (Hermes' own rule), not "every directory", so
    tombstone dirs and marker-less stubs are never demanded of the gateway.
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

    # Check profile dirs are covered. Uses the SHARED roster helper so this
    # check and the daemon's multiplex stream (health._check_multiplex_profiles
    # -> gateway.json) demand exactly the same set (t_1c545ebb): Hermes builds
    # ``served_profiles`` from its own enumeration, which is not "every
    # directory under profiles/" — dot-directories (``.deleted`` is Hermes'
    # tombstone dir), names outside the profile-id grammar, and marker-less
    # stub directories are never served, and must never be counted as missing.
    from . import profile_roster
    if os.path.isdir(profiles_dir):
        expected = profile_roster.list_profiles_on_disk(profiles_dir)
        missing = [d for d in expected if d not in served_set]
    else:
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


def _json_shape_name(value):
    """Short, stable name for a JSON value's shape, used when a state file is
    reported as ``unrecognized schema`` (GAP-F-1): the operator should see
    ``list`` / ``str`` / ``int`` / ``float`` / ``bool`` / ``null``, not a dump
    of the file's contents (a ledger can hold 8 days of samples)."""
    if value is None:
        return "null"
    if isinstance(value, list):
        return f"list len={len(value)}"
    if isinstance(value, bool):
        return "bool"
    return type(value).__name__


def _watcher_stream_names():
    """Stamp names owned by the cron watchers (GAP-B-4), as a set.

    Their liveness verdict belongs to the doctor row
    (``cron_watchers.doctor_check``), NOT to ``check_daemon_streams``: the
    daily dep-watcher would be RED under the flat 600 s window 23 h of every
    day, and a dead watcher must warn rather than fail the cluster smoke test.
    Kept in sync with the cadence map above by the same import-defence rule —
    an unavailable module means no exclusions, which only restores the
    pre-GAP-B-4 behaviour for streams that did not exist before it.
    """
    try:
        from .cron_watchers import STREAMS
        return set(STREAMS)
    except Exception:
        return set()


def check_daemon_streams(state_dir=None, max_age_s=None, stream_intervals=None):
    """Check all daemon state files are healthy and recent.

    OK if every *.json has ok==True and last_check within max_age_s of now,
    EXCEPT streams that are deliberately down by an intentional autodown.

    SCHEMA TOLERANCE (GAP-F-1). The state dir is shared with non-stream
    writers — cards-monitor's ``kmon_orch_tokens.json`` token ledger is a
    top-level JSON list — so no file shape may ever crash this check or abort
    the loop: a non-dict JSON file is reported ``unrecognized schema`` and
    skipped. A file that is a dict but carries neither ``ok`` nor
    ``last_check``/``timestamp`` asserts nothing, so it is reported
    ``not-checked`` and the row goes UNVERIFIED (``ok=None``), never green
    (GAP-F-2 — an aggregate that asserts nothing must not claim health).

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
    # Cron-watcher stamps (GAP-B-4) are judged by the doctor row, not here —
    # see _watcher_stream_names(). Excluded before the count so the healthy
    # total stays "streams the daemon owns".
    watcher_streams = _watcher_stream_names()

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
    # Cron-watcher stamps are NOT daemon streams: their liveness verdict belongs
    # to the GAP-B-4 doctor row (cron_watchers.doctor_check), which knows a
    # daily cadence. Judged here they would be RED 23 hours of every day under
    # the flat 600 s window, and a dead watcher must warn rather than fail the
    # cluster smoke test. Excluded before the count so the healthy total stays
    # "streams the daemon owns".
    watcher_files = [f for f in json_files if f[:-5] in watcher_streams]
    json_files = [f for f in json_files if f[:-5] not in watcher_streams]
    if not json_files:
        if watcher_files:
            return {"name": "daemon_streams", "ok": True,
                    "detail": "no daemon stream files yet "
                              f"({len(watcher_files)} cron-watcher stamp(s) "
                              "excluded — see the 'cron watchers' check)"}
        return {"name": "daemon_streams", "ok": True, "detail": "no state files found"}

    now = datetime.now(timezone.utc)
    issues = []
    expected = []
    unchecked = []      # GAP-F-2: dict-shaped but nothing assertable
    unrecognized = []   # GAP-F-1: non-dict JSON (e.g. the cards-monitor ledger)
    checked = 0
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

        # GAP-F-1 (schema tolerance). The state dir is shared with writers
        # that are NOT daemon streams: cards-monitor's token ledger
        # (kanban_live.py LEDGER_PATH, kmon_orch_tokens.json) is a top-level
        # JSON LIST of [ts, in, out] samples. This check used to call
        # ``data.get("ok")`` unconditionally and die with AttributeError —
        # which did not just fail the row, it killed the ENTIRE verify table
        # (every later check, incl. the served-profile endpoint guard, never
        # ran and `verify --json` printed nothing). A file whose shape this
        # check does not understand is named and skipped: never crashed on,
        # never silently counted as healthy, and never aborts the loop.
        if not isinstance(data, dict):
            unrecognized.append(
                f"{fn}: unrecognized schema ({_json_shape_name(data)})")
            continue

        ok = data.get("ok")
        has_ts = bool(data.get("last_check") or data.get("timestamp"))
        if ok is None and not has_ts:
            # GAP-F-2 (assert-nothing is not green). Live shapes this used to
            # wave through as "healthy" while asserting nothing at all:
            # orch_waker.json (stopgap {last,pending,sent}), kanban_paused.json
            # ({}), reasoning_policy.json (config snapshot). No `ok`, no
            # timestamp ⇒ nothing here is verifiable ⇒ the row goes
            # UNVERIFIED (ok=None) and names the file — "could not check
            # this", never a false green.
            unchecked.append(f"{fn}: not-checked (no ok/timestamp)")
            continue

        checked += 1
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

    # Everything this check could NOT assert anything about (bad shape or no
    # ok/timestamp) is surfaced by name on EVERY verdict path, so a real
    # failure can never hide the un-verifiable files and vice versa.
    aside = "; ".join(unrecognized + unchecked)

    expected_note = (
        f"{window_label}: {'; '.join(expected)}"
        if expected else ""
    )
    if issues:
        detail = "; ".join(issues)
        if expected_note:
            detail = f"{expected_note}; REAL FAILURES: {detail}"
        if aside:
            detail = f"{detail}; {aside}"
        return {"name": "daemon_streams", "ok": False, "detail": detail}

    # Nothing failed. But a file this check could not assert anything about
    # makes the row UNVERIFIED (ok=None — run_all's established vocabulary:
    # "could not check this" is not "this is broken" and not "all green").
    partial = bool(unchecked or unrecognized)
    head = (f"checked={checked} healthy" if partial
            else f"all {len(json_files)} streams healthy")
    if expected_note:
        return {
            "name": "daemon_streams", "ok": None if partial else True,
            "detail": (f"ok={checked - len(expected)} healthy, {expected_note}"
                       + (f"; {aside}" if aside else "")),
        }
    if partial:
        return {"name": "daemon_streams", "ok": None,
                "detail": f"{head}; {aside}"}

    return {"name": "daemon_streams", "ok": True, "detail": head}


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


def check_proxy_router_settings(proxy_config=None, litellm_config=None):
    """WARN-level drift check: live litellm_config carries proxy.yaml's router intent.

    Pure-read comparison of ``~/.config/sparkrun/proxy.yaml`` (what the
    operator asked for: ``gateways.litellm.router_settings`` — falling back to
    the top-level duplicate only on the 0.3.10 rollback line — plus
    ``gateways.litellm.balancing: least-in-flight``) against the generated
    ``~/.cache/sparkrun/proxy/litellm_config.yaml`` (what the proxy actually
    runs). All logic lives in :mod:`hscc_daemon.proxy_router`, which mirrors
    sparkrun's own emission rules.

    This is the post-bump tripwire for the fork-branch failure (see
    :mod:`hscc_daemon.dep_bump_guard`): a sparkrun bump that checks out
    upstream main instead of rebasing ``feat/proxy-least-in-flight-041`` drops
    the ``router_settings`` passthrough and the least-in-flight balancer
    with NO error anywhere. The proxy keeps serving; routing silently
    reverts. This check is what makes that visible.

    Operator rule encoded here: WARN, never fail. Drift reports ``ok: True``
    with a ``WARN:``-prefixed detail (never trains operators to ignore a
    red proxy verdict over a soft policy mismatch); ``ok: None`` only when
    the inputs cannot be read at all. Never writes anything, never restarts.
    """
    from . import proxy_router
    res = proxy_router.router_drift(
        proxy_config=proxy_config, litellm_config=litellm_config)
    ok = None if res["status"] == "unverified" else True
    return {"name": "proxy_router_settings", "ok": ok, "detail": res["detail"]}


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


# ── provider_routing: hermes talks ONLY to the proxy under uniform_pool ────

# The applied-template state path is resolved AT CALL TIME (not baked in at
# import) so the hscc_daemon/tests conftest ~/.hscc redirect — which patches
# os.path.expanduser for runtime-computed paths — keeps the check hermetic.
_APPLIED_TEMPLATE_STATE_REL = "~/.hscc/applied_template.json"

# Resolve the fleet's node address set through sparkrun's OWN host resolution
# (the sanctioned path — health.py runs the same `resolve_hosts(None, None,
# None, mgr, config.default_hosts)` under sparkrun's venv python). Pure host
# resolution: no ssh, no status collection, unlike _SPARKRUN_STATUS_SCRIPT.
# Returns [] when no default cluster resolves, so the caller reports
# "unverified", never a false pass. NEVER hand-parse template yaml or hardcode
# ranges — sparkrun owns the fleet.
_FLEET_HOSTS_SCRIPT = (
    "import json,sys\n"
    "from sparkrun.core.config import SparkrunConfig,get_config_root\n"
    "from sparkrun.core.cluster_manager import ClusterManager\n"
    "from sparkrun.core.hosts import resolve_hosts\n"
    "config=SparkrunConfig()\n"
    "mgr=ClusterManager(get_config_root())\n"
    "hosts=resolve_hosts(None,None,None,mgr,config.default_hosts)\n"
    "print(json.dumps(sorted(set(str(h) for h in (hosts or [])))))\n"
)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def _fleet_node_hosts(*, venv_python=None, run=None, timeout=25):
    """Fleet node hostnames from sparkrun's own host resolution.

    Returns ``(hosts, error)``: ``(set, None)`` on success (possibly an empty
    set when sparkrun reports no default cluster — the caller must treat that
    as UNVERIFIED, not "fleet is empty"); ``(None, reason)`` when the address
    set could not be obtained at all. The shell-out runs sparkrun's venv
    python (resolved PATH-independently via ``sparkrun_bin``, like health.py)
    with the pure ``_FLEET_HOSTS_SCRIPT`` — no ssh, no node contact.
    ``venv_python``/``run`` are injectable for tests.
    """
    if venv_python is None:
        venv_python = (_sparkrun_bin.sparkrun_venv_python()
                       if _sparkrun_bin is not None else None)
    if not venv_python:
        return None, "sparkrun venv python unavailable"

    def _default_runner(argv, timeout):
        import subprocess as _sp          # local import, like run_chat_roundtrip
        cp = _sp.run(argv, capture_output=True, text=True, timeout=timeout)
        return cp.returncode, cp.stdout

    if run is None and subprocess is None:
        return None, "subprocess unavailable"
    runner = run or _default_runner
    try:
        rc, out = runner([venv_python, "-c", _FLEET_HOSTS_SCRIPT], timeout)
    except Exception as exc:
        return None, f"sparkrun host resolution failed ({exc})"
    if rc != 0:
        return None, f"sparkrun host resolution exited {rc}"
    try:
        hosts = json.loads(out or "")
    except ValueError:
        return None, "sparkrun host resolution returned no JSON"
    if not isinstance(hosts, list):
        return None, "sparkrun host resolution returned a non-list"
    return {str(h) for h in hosts}, None


def _routing_candidate_urls(data):
    """Yield ``(key_path, url)`` base_urls the routing guard inspects.

    Covers every placement the card names — the top-level ``model`` block,
    ``providers[].base_url``, ``fallback_providers`` entries, and auxiliary
    model entries — by walking every ``base_url`` key wherever it is nested
    (the same walker check_profile_endpoints uses), then adding any string
    under a ``url``/``endpoint`` key inside a ``fallback_providers`` subtree
    (fallback chains store the address under either name across hermes
    versions). Key paths are dotted and de-duplicated per path.
    """
    seen = set()
    for kp, v in _iter_keys(data, "base_url"):
        if isinstance(v, str) and v.strip():
            seen.add((kp, v.strip()))
    for kp, fb in _iter_keys(data, "fallback_providers"):
        chains = fb if isinstance(fb, list) else [fb]
        for entry in chains:
            for sub, url in _iter_keys(entry, "url"):
                if isinstance(url, str) and url.strip():
                    seen.add((f"{kp}.{sub}" if sub else kp, url.strip()))
            for sub, url in _iter_keys(entry, "endpoint"):
                if isinstance(url, str) and url.strip():
                    seen.add((f"{kp}.{sub}" if sub else kp, url.strip()))
    return sorted(seen)


def check_provider_routing(config=None, profiles_dir=None,
                           applied_state_path=None, fleet_hosts=None,
                           venv_python=None, run=None, timeout=25):
    """Guard: under a uniform_pool template, NO hermes config may name a fleet
    node — hermes talks ONLY to the proxy (:4000); that is the whole point of
    hscc on a uniform pool.

    Fail path: while the APPLIED template is ``uniform_pool`` (read from
    ``~/.hscc/applied_template.json``, injectable via ``applied_state_path``),
    any base_url — in the root ``~/.hermes/config.yaml`` OR every
    ``~/.hermes/profiles/*/config.yaml`` — whose host is a FLEET NODE is a
    finding (covers model.base_url, providers[].base_url, fallback_providers
    and auxiliary model entries; see ``_routing_candidate_urls``). Loopback
    (localhost/127.0.0.1/::1) proxy URLs pass on any port.

    Skip/pass (documented): when the applied template is NOT uniform_pool
    (including an applied state predating the recorded flag) the direct-node
    form is legitimate and the check passes with ``skipped:`` in the detail.
    When the applied state or the fleet host set cannot be obtained the check
    is UNVERIFIED (ok=None) — "could not check" is not "broken" and, crucially,
    never a silent pass: this guard exists precisely because every apply used
    to re-infect a hand-fixed config.

    The fleet node address set comes from sparkrun's own host resolution
    (``_FLEET_HOSTS_SCRIPT`` under sparkrun's venv python — the sanctioned
    path, health.py's pattern), never from hand-parsed template yaml, hardcoded
    ranges, or ssh to nodes.

    PURE-READ: never writes or repairs any config, never restarts anything.
    Returns the standard {name, ok, detail} shape; never raises.
    """
    if yaml is None:
        return {"name": "provider_routing", "ok": None,
                "detail": "unverified: pyyaml not installed"}

    # ── 1. Is the uniform_pool invariant live? ────────────────────────────
    state_path = os.path.expanduser(
        applied_state_path or _APPLIED_TEMPLATE_STATE_REL)
    try:
        with open(state_path) as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"name": "provider_routing", "ok": None,
                "detail": "unverified: no applied-template state "
                          f"({state_path}) — cannot tell if the pool invariant "
                          "applies"}
    except (json.JSONDecodeError, OSError) as exc:
        return {"name": "provider_routing", "ok": None,
                "detail": f"unverified: applied-template state unreadable ({exc})"}
    if not isinstance(state, dict) or not state.get("uniform_pool"):
        # Non-uniform (or pre-flag) template: direct node endpoints are
        # legitimate there — the role split is real and there may be no proxy.
        return {"name": "provider_routing", "ok": True,
                "detail": "skipped: applied template is not uniform_pool"
                          " — direct node endpoints are legitimate"}

    # ── 2. Fleet node addresses (sparkrun's own resolution) ───────────────
    if fleet_hosts is None:
        fleet_hosts, err = _fleet_node_hosts(venv_python=venv_python,
                                             run=run, timeout=timeout)
        if err:
            return {"name": "provider_routing", "ok": None,
                    "detail": f"unverified: {err}"}
    if not fleet_hosts:
        return {"name": "provider_routing", "ok": None,
                "detail": "unverified: sparkrun resolved no fleet hosts — "
                          "an empty node set is UNKNOWN, not empty (fail-closed)"}
    fleet_hosts = {str(h).strip().lower() for h in fleet_hosts}

    # ── 3. Scan root config + every profile config, pure-read ─────────────
    targets = []
    if config is None:
        targets.append(("root", os.path.expanduser("~/.hermes/config.yaml")))
    else:
        targets.append(("root", os.path.expanduser(config)))
    profiles_dir = profiles_dir if profiles_dir is not None else (
        _serving.PROFILES_DIR if _serving is not None
        else os.path.expanduser("~/.hermes/profiles"))
    profiles_dir = os.path.expanduser(profiles_dir)
    if os.path.isdir(profiles_dir):
        try:
            entries = sorted(os.scandir(profiles_dir), key=lambda e: e.name)
        except OSError:
            entries = []
        for entry in entries:
            if entry.is_dir():
                targets.append((f"profile:{entry.name}",
                                os.path.join(entry.path, "config.yaml")))

    issues = []
    checked = 0
    unreadable = 0
    for source, path in targets:
        if not os.path.isfile(path):
            continue
        try:
            data = yaml.safe_load(open(path))
        except Exception:
            # A config we cannot parse is not evidence of bad routing;
            # config_wiring/other checks police readability itself.
            unreadable += 1
            continue
        for key_path, url in _routing_candidate_urls(data):
            checked += 1
            origin = _origin(url)
            if origin is None:
                continue  # unparseable url is config_wiring's problem, not ours
            host = origin[1]
            if host in _LOOPBACK_HOSTS:
                continue
            if host in fleet_hosts:
                issues.append(f"{source}: {key_path} {url} points at fleet "
                              f"node {host} while uniform_pool routes hermes "
                              "only through the proxy")

    if issues:
        return {"name": "provider_routing", "ok": False,
                "detail": f"{len(issues)} direct node endpoint(s) under "
                          "uniform_pool: " + "; ".join(issues)}
    note = (f"no fleet-node base_url across {checked} url(s) in "
            f"{len(targets)} hermes config(s)")
    if unreadable:
        note += f" ({unreadable} unreadable config(s) skipped)"
    return {"name": "provider_routing", "ok": True, "detail": note}


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
        # Same dual-layout resolution as the chat round-trip script: installed,
        # hscc_daemon sits directly under ~/.hermes/plugins, where a
        # repo-relative "scripts/" does not exist. Shipping the sweep inside the
        # package means one path is right in both layouts.
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = (
            os.path.join(here, "api_route_sweep.py"),                    # installed
            os.path.join(_repo_root(), "scripts", "api_route_sweep.py"), # repo
        )
        script = next((c for c in candidates if os.path.exists(c)), candidates[0])
    # The sweep derives the app's routes by PARSING HSCCClient.swift. That file
    # ships only in a repo checkout, so in an installed layout the check cannot
    # run at all. That is "unverified", not a failure: the API may be perfectly
    # healthy, and reporting red here would train the operator to ignore reds.
    client_src = os.path.join(_repo_root(), "ios-app", "Sources", "HSCC",
                              "HSCCClient.swift")
    if not os.path.isfile(client_src):
        return {"name": "api_routes", "ok": None,
                "detail": "unverified: HSCCClient.swift not present (installed "
                          "layout has no repo tree to derive routes from)",
                "next_step": "run hscc verify from a checked-out repo tree to "
                             "sweep the app's routes"}
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


def _generation_is_advancing(node, port, window=20.0):
    """True if the unit's vLLM generation counter climbs across `window` secs.

    This is the difference between congestion and a wedge. Failure to read the
    metric returns False (no evidence of life), leaving the caller's original
    verdict intact.
    """
    import time as _time
    import urllib.request

    def _sample():
        try:
            url = "http://%s:%s/metrics" % (node, port)
            with urllib.request.urlopen(urllib.request.Request(url),
                                        timeout=8) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return None
        for line in body.splitlines():
            if line.startswith("vllm:generation_tokens_total"):
                m = re.search(r"\}\s+([0-9.]+)", line)
                if m:
                    return float(m.group(1))
        return None

    first = _sample()
    if first is None:
        return False
    _time.sleep(window)
    second = _sample()
    return second is not None and second > first


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
            # "wedged" means HTTP 200 with no tokens inside the probe window.
            # Under real load that is ALSO what a busy-but-healthy unit looks
            # like: the probe queues behind production traffic. "closed_early"
            # (t_e37a74c7) means the stream terminated inside the window with
            # zero tokens — often just a transport blip. Both are corroborated
            # against the generation counter before being called a failure:
            # a counter that is still climbing proves the engine is
            # generating, just not for us. Only a frozen counter is real.
            if why in ("wedged", "closed_early") and _generation_is_advancing(node, port):
                continue
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

    # Default scope: the deployable plugin set is the SINGLE source of truth in
    # hscc-bootstrap/install_payload.py::DEFAULT_PAYLOAD — the list bootstrap
    # actually installs. Loading it by file path (not re-globbing `hscc-*` here)
    # means verify can never drift from what deploy ships. It correctly excludes
    # non-plugin dirs like hscc-cli (the CLI source) while including hscc_daemon
    # (underscore). Only dirs present in THIS checked-out repo are diffable.
    if names is None:
        payload = []
        install_payload_py = os.path.join(
            repo_root, "hscc-bootstrap", "install_payload.py")
        if os.path.isfile(install_payload_py):
            try:
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "hscc_install_payload", install_payload_py)
                if spec is not None and spec.loader is not None:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    payload = getattr(mod, "DEFAULT_PAYLOAD", []) or []
            except Exception:
                payload = []
        repo_dirs = sorted(
            name for name in payload
            if os.path.isdir(os.path.join(repo_root, name))
        )
    else:
        repo_dirs = list(names)

    if not repo_dirs:
        return {"name": "plugin_payload", "ok": None,
                "detail": "unverified: no deployable plugin dirs found "
                          "(install_payload.DEFAULT_PAYLOAD not loadable)",
                "next_step": "run hscc verify from a checked-out repo tree"}

    issues = []
    checked = 0
    for d in repo_dirs:
        repo_dir = os.path.join(repo_root, d)
        inst_dir = os.path.join(plugins_dir, d)
        # What THIS repo ships as deployable payload for the dir. Resource-only
        # payloads (e.g. assets/*.png, configs/*.template) contain no source/
        # docs files, so there is nothing to diff — skip rather than flag.
        repo = _payload_snapshot(repo_dir)
        if not repo:
            continue
        checked += len(repo)
        inst = snapshot(inst_dir) if snapshot else _payload_snapshot(inst_dir)
        if not inst:
            issues.append(
                f"{d}: installed at {inst_dir} missing or has no payload — "
                f"merged but not deployed")
            continue
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


def check_project_chat_ready(registry_path=None, profiles_dir=None):
    """Check every registered project is still chattable.

    The invariant we enforce forever: for EVERY project in the flightdeck
    registry, both must hold —

      1. a profile named ``<name>-orch`` exists under ``profiles_dir``
      2. a Hermes session titled ``<name>`` exists in that profile's
         ``state.db`` (``select count(*) from sessions where title = ?``)

    If either is missing, the operator's iOS chat for that project is dead.
    This is the continuous guard behind the creation-path fixes: regardless of
    how a project got into the registry, it must never again exist in a state
    where it cannot be chatted with.

    Injectable ``registry_path`` (default ``~/.flightdeck/registry.yaml``) and
    ``profiles_dir`` (default ``~/.hermes/profiles``) let tests drive the whole
    check against temp dirs — exactly like ``check_plugin_payload`` takes
    injectable repo_root/plugins_dir so tests never touch the live runtime.
    The check is read-only: it opens each ``state.db`` with ``mode=ro`` and
    never writes a profile or session.

    Returns ``ok=None`` (not False) when the invariant cannot be determined —
    no registry, no profiles dir, or a registry that cannot be parsed — so a
    check that could not run never turns the overall verdict red. When it CAN
    run, it reports EVERY failing project and names which half is missing
    (profile vs session) so the fix is obvious.
    """
    import sqlite3 as _sqlite3

    if registry_path is None:
        registry_path = os.path.expanduser("~/.flightdeck/registry.yaml")
    if profiles_dir is None:
        profiles_dir = os.path.expanduser("~/.hermes/profiles")

    if not os.path.isfile(registry_path):
        return {"name": "project_chat_ready", "ok": None,
                "detail": f"unverified: no registry at {registry_path}",
                "next_step": "create the flightdeck registry first"}
    if not os.path.isdir(profiles_dir):
        return {"name": "project_chat_ready", "ok": None,
                "detail": f"unverified: no profiles dir at {profiles_dir}",
                "next_step": "install Hermes profiles first"}

    # Load project names from the registry. Reuse the SAME parsing the
    # flightdeck registry module ships (load_registry -> _name_for) so we never
    # drift from how names are actually derived (explicit ``name`` else the
    # repo basename). Loaded by file path, mirroring how check_plugin_payload
    # loads install_payload.py, so hscc_daemon stays decoupled from the
    # flightdeck package layout on sys.path.
    try:
        projects = _load_registry_projects(registry_path)
    except Exception as exc:
        return {"name": "project_chat_ready", "ok": None,
                "detail": f"unverified: could not read registry {registry_path}: {exc}",
                "next_step": "fix the registry file"}
    if projects is None:
        return {"name": "project_chat_ready", "ok": None,
                "detail": f"unverified: could not load registry {registry_path}",
                "next_step": "run from an environment with the flightdeck registry "
                            "available"}

    if not projects:
        return {"name": "project_chat_ready", "ok": True,
                "detail": "no projects in the registry — nothing to verify"}

    issues = []
    checked = 0
    for proj in projects:
        profile_dir = os.path.join(profiles_dir, proj["profile"])
        half = None
        if not os.path.isdir(profile_dir):
            half = "profile"
        else:
            db_path = os.path.join(profile_dir, "state.db")
            if not _state_db_has_session(db_path, proj["name"], _sqlite3):
                half = "session"
        checked += 1
        if half is not None:
            issues.append(
                f"{proj['name']}: missing {half} "
                f"(expected profile {proj['profile']} at {profile_dir}"
                + (" with a session titled " + proj["name"] if half == "session" else "")
                + ")")

    if issues:
        return {"name": "project_chat_ready", "ok": False,
                "detail": "; ".join(issues),
                "next_step": "ensure the <name>-orch profile exists and has a "
                             "Hermes session titled <name> (re-run the project "
                             "create/repair lifecycle)"}
    return {"name": "project_chat_ready", "ok": True,
            "detail": f"all {checked} registered projects are chattable "
                      f"(profile <name>-orch + session titled <name> present)"}


def _load_registry_projects(registry_path):
    """Load the flightdeck registry and return [{name, profile}] for each project.

    Returns ``None`` when the flightdeck registry module cannot be located or
    loaded (so the caller reports ``ok=None``, not a false failure). Project
    names come from the real ``load_registry`` so the ``name``-else-repo-default
    rule is applied exactly as flightdeck does it.
    """
    repo_root = _repo_root()
    registry_py = os.path.join(
        repo_root, "hscc-project", "flightdeck", "core", "registry.py")
    if not os.path.isfile(registry_py):
        return None
    try:
        import importlib.util
        import sys as _sys
        spec = importlib.util.spec_from_file_location(
            "hscc_flightdeck_registry", registry_py)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        # register in sys.modules BEFORE exec: registry.py uses @dataclass,
        # whose metaclass looks up the module by name in sys.modules.
        _sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        projects = mod.load_registry(registry_path)
    except Exception:
        return None
    return [
        {"name": p.name, "profile": f"{p.name}-orch"}
        for p in projects
    ]


def _state_db_has_session(db_path, title, sqlite3_mod):
    """True if ``<profile>/state.db`` has a session row titled ``title``.

    Opens the DB read-only (``mode=ro``) — a busy profile's state.db is never
    contended and the check never writes. Any failure (missing file, locked
    DB, bad query) returns False so a project with an unreadable state.db is
    surfaced as missing rather than silently passed.
    """
    if not os.path.isfile(db_path):
        return False
    try:
        conn = sqlite3_mod.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = conn.execute(
                "select count(*) from sessions where title = ?", (title,))
            row = cur.fetchone()
            return bool(row and row[0] > 0)
        finally:
            conn.close()
    except Exception:
        return False


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


def check_declaration_agreement(applied_state_path=None, config=None,
                                profiles_dir=None, proxy_config=None,
                                litellm_config=None, balancer_module=None,
                                _http_get=None):
    """Declaration<->live agreement for the v4 template declarations (spec §6).

    PURE-READ (never repairs, never restarts): the applied template's
    ``declarations`` record (``~/.hscc/applied_template.json``, spec §7 —
    written by C2 / t_70814001) is compared, across the four spec §6 legs
    plus the t_be4b94ca per-unit alias-coverage leg, against live state:

      1. hermes_endpoints  — each declared endpoint consumer carries the
         declared target's URL/alias in the root config and each matched
         profile (the §6.1 fleet-node scan stays in check_provider_routing,
         unchanged — this is its positive half);
      2. proxy_declaration — ``~/.config/sparkrun/proxy.yaml`` agrees with the
         applied ``proxy:`` block, reading ``router_settings`` NESTED FIRST
         per the 2026-10-19 addendum (the ONE implementation,
         :func:`hscc_daemon.proxy_router.router_settings_source`);
      3. balancer_pair     — proxy.yaml asking for ``balancing`` requires the
         generated config's ``litellm_settings.callbacks`` AND the
         ``sparkrun_balancer.py`` module next to it; drift is WARN, never
         fail, never repaired (cross-ref t_39d7c915 — ONE implementation
         referenced, not duplicated);
      4. live_model_info   — each routed target's ``/v1/model/info`` model set
         covers the ids the applied ``endpoints:`` routes hermes to.

    Fail-closed doctrine (t_ac795424): any leg that cannot be read (no
    applied state, no proxy.yaml, unreachable proxy) is UNVERIFIED (ok=None),
    never a silent pass. Remediation text names ``hscc cluster monitor
    --live`` as the load-instrument (spec §3.2). All logic lives in
    :mod:`hscc_daemon.declaration_agreement`; every external surface is
    injectable so tests use fakes only.
    """
    from . import declaration_agreement as _da
    res = _da.run_declaration_agreement(
        applied_state_path=applied_state_path, config=config,
        profiles_dir=profiles_dir, proxy_config=proxy_config,
        litellm_config=litellm_config, balancer_module=balancer_module,
        _http_get=_http_get)
    out = {"name": res["name"], "ok": res["ok"], "detail": res["detail"]}
    if res.get("next_step"):
        out["next_step"] = res["next_step"]
    return out


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
        check_proxy_router_settings,
        check_config_wiring,
        check_profile_endpoints,
        check_provider_routing,
        check_declaration_agreement,
        check_api_routes,
        check_chat_roundtrip,
        check_plugin_payload,
        check_project_chat_ready,
    ]

    results = []
    for check_fn in checks:
        import inspect
        sig = inspect.signature(check_fn)
        params = set(sig.parameters.keys())
        kwargs = {k: v for k, v in overrides.items() if k in params}
        results.append(check_fn(**kwargs))

    # ok is None ⇒ UNVERIFIED, not failed. A check that could not run (no repo
    # tree to parse, serving.json absent) must not turn the overall verdict
    # red: "I could not check this" is not "this is broken", and conflating
    # them teaches the operator to ignore a red verdict.
    return {
        "checks": results,
        "ok": all(r["ok"] is not False for r in results),
        "unverified": [r["name"] for r in results if r.get("ok") is None],
    }
